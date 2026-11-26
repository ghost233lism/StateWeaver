"""Cross-workspace physical capacity, cooldowns and non-reposting request receipts.

Only safe identities, hashes, numeric usage and material receipts are journalled.
Unknown submissions keep their capacity reservation until explicitly reconciled;
an expired *unsubmitted* reservation alone can be safely released.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
from pathlib import Path
from threading import Lock, RLock
from typing import Any
from uuid import uuid4

from .audit import atomic_id, numeric_usage, structural
from .base import (
    HttpRequest,
    ProviderContractError,
    ProviderExternalError,
    ProviderHttpRejectionError,
    ProviderHttpStatusError,
    http_rejection_details,
    provider_config_hash,
)
from .history import (
    archived_events,
    archived_export,
    archived_owned_receipts,
    receipt_record,
    restore_receipt_row,
)
from .registry import APIService, wire_identity

ACTIVE = ("reserved", "submitting", "running", "collecting", "unknown")
_OCCUPIES_PHYSICAL_POOL = (
    "NOT (state IN ('running','collecting') AND provider_job_id IS NOT NULL AND "
    "COALESCE(json_extract(context,'$.operation')='video_generation',0) AND "
    "COALESCE(json_extract(result,'$.video_completion.provider_job_id')=provider_job_id,0))"
)


def _submission_interval_seconds(service: APIService) -> float:
    """Space reservations only when the image service declares a quota."""
    if service.kind != "image":
        return 0.0
    capabilities = service.capabilities or {}
    rpm = capabilities.get("rate_limit_per_minute")
    window = capabilities.get("rate_limit_window_seconds", 60)
    if isinstance(rpm, bool) or isinstance(window, bool):
        return 0.0
    if not isinstance(rpm, (int, float)) or not isinstance(window, (int, float)):
        return 0.0
    if not math.isfinite(float(rpm)) or not math.isfinite(float(window)):
        return 0.0
    if rpm <= 0 or window <= 0:
        return 0.0
    return float(window) / float(rpm)


IDENTITY_FIELDS = {
    "workspace",
    "project_id",
    "actor_id",
    "task_id",
    "job_id",
    "operation",
    "attempt",
    "recipe_hash",
    "input_hash",
    "config_hash",
    "binding_hash",
    "context_epoch",
    "workload",
    "deployment_hash",
    "rate_limit_policy",
    "primary_model",
    "parent_binding_hash",
    "unknown_retry_root_receipt_id",
}


class ProviderCapacityWait(ProviderExternalError):
    definitely_not_accepted = True

    def __init__(self, retry_at: float, reason: str = "physical_capacity") -> None:
        self.retry_at, self.reason = retry_at, reason
        super().__init__("Shared API capacity/cooldown is occupied; no submission was sent")


class ProviderSubmissionUnknown(ProviderExternalError):
    def __init__(self, receipt_id: str) -> None:
        self.receipt_id = receipt_id
        super().__init__("Original API submission is unknown; do not POST again")


class _ReceiptChanged(Exception):
    """Retry local admission preparation after a concurrent outcome changed."""


class ProviderReceiptLeaseLost(ProviderContractError):
    """A stale publisher may neither change a newer receipt nor authorize a POST."""


def _http_rejection_diagnostics(error: Exception) -> dict[str, str]:
    """Retain only the HTTP boundary's already bounded, redacted error fields."""
    from stateweaver.studio_media.submission_errors import ImageSubmissionRejected

    if isinstance(error, ImageSubmissionRejected):
        details = error.details
    elif isinstance(error, (ProviderHttpRejectionError, ProviderHttpStatusError)):
        details = error.rejection
    else:
        return {}
    if not isinstance(details, Mapping):
        return {}
    fields = {}
    for key in ("code", "type", "param", "message"):
        value = details.get(key)
        if not isinstance(value, (str, int)) or isinstance(value, bool):
            continue
        try:
            text = str(value)
        except ValueError:
            continue
        if len(text) <= (500 if key == "message" else 128):
            fields[key] = text
    if not fields:
        return {}
    # Reapply the same allowlist and redaction without saving a body or headers.
    # The original HTTP boundary already removed request-header credentials.
    redacted = http_rejection_details(
        json.dumps({"error": fields}).encode(), HttpRequest("POST", "http://redacted.invalid")
    )
    result = {}
    for key, text in redacted.items():
        try:
            structural(text)
        except ProviderContractError:
            continue
        result[key] = text
    return result


def classify_error(error: Exception, *, phase: str, known_job: bool = False) -> dict[str, Any]:
    status = getattr(error, "http_status", None)
    effective_status = getattr(error, "effective_http_status", status)
    failure_category = getattr(error, "failure_category", None)
    category = (
        "capacity_wait"
        if isinstance(error, ProviderCapacityWait)
        else "moderation_blocked"
        if failure_category == "moderation_blocked"
        else "policy_rejection"
        if failure_category == "policy_rejection"
        else "authentication"
        if status == 401
        else "model_permission"
        if status == 403
        else "rate_limit"
        if effective_status == 429
        else "temporarily_unavailable"
        if status == 503
        else "http_rejection"
        if isinstance(status, int) and 400 <= status < 500
        else "protocol_or_capability"
        if isinstance(error, (ProviderContractError, ValueError))
        else "query_unavailable"
        if known_job or phase != "submission"
        else "submission_unknown"
    )
    not_accepted = getattr(error, "definitely_not_accepted", False) is True
    outcome_known = getattr(error, "outcome_known", False) is True
    failure = {
        "category": category,
        "http_status": status,
        "effective_http_status": effective_status,
        "phase": phase,
        "known_job": known_job,
        "definitely_not_accepted": not_accepted,
        "retryable": (not_accepted or outcome_known) and not known_job and phase == "submission",
    }
    if details := _http_rejection_diagnostics(error):
        failure["upstream_rejection"] = details
    return failure


def _enable_wal(connection: sqlite3.Connection) -> None:
    """Retry only first-open journal lock contention, never a provider operation.

    Concurrent readers can all see DELETE mode before any process changes it.
    SQLite may reject that upgrade immediately despite its normal busy timeout.
    Check the persistent mode first and retry BUSY/LOCKED outside a transaction;
    neither a process-local mutex nor a new per-workspace ledger is sufficient.
    """
    deadline = time.monotonic() + 30
    connection.execute("PRAGMA busy_timeout=100")
    while True:
        try:
            if connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal":
                break
            if connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] != "wal":
                raise ProviderContractError("Shared API ledger requires WAL journalling")
            break
        except sqlite3.OperationalError as error:
            code = getattr(error, "sqlite_errorcode", None)
            remaining = deadline - time.monotonic()
            if (
                not isinstance(code, int)
                or code & 0xFF not in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
                or remaining <= 0
            ):
                raise
            time.sleep(min(0.02, remaining))
    connection.execute("PRAGMA busy_timeout=30000")


class ProviderCoordinator:
    def __new__(
        cls, path: Any, *arguments: Any, direct: bool = False, **options: Any
    ) -> ProviderCoordinator:
        if cls is ProviderCoordinator and not direct:
            from .coordinator_client import selected_client

            remote = selected_client(path.resolve())
            if remote is not None:
                return remote
        return super().__new__(cls)

    def __init__(self, path: Path, *, direct: bool = False) -> None:
        self.path = path.resolve()
        self._initialized = False
        self._initialization_lock = Lock()
        self._writer_lock = RLock()
        self.admission_paused = False

    @property
    def is_remote(self) -> bool:
        return False

    def close(self) -> None:
        """Direct coordinators hold no permanent live connection."""

    def _prefetch(self, value: str, *, identity: bool = False) -> sqlite3.Row | None:
        with self.connection(write=False) as reader:
            return receipt_record(reader, value, identity=identity)

    @staticmethod
    def _hot_receipt(
        db: sqlite3.Connection,
        value: str,
        prefetched: sqlite3.Row | None,
        *,
        identity: bool = False,
    ) -> sqlite3.Row | None:
        field = "identity" if identity else "id"
        current = db.execute(f"SELECT * FROM api_receipts WHERE {field}=?", (value,)).fetchone()
        return current if current is not None else restore_receipt_row(db, prefetched)

    def _initialize(self) -> None:
        if self._initialized:
            return
        with self._initialization_lock:
            if self._initialized:
                return
            self._initialize_schema()
            self._initialized = True

    def prepare(self) -> None:
        """Finish ledger setup before a caller enters another database transaction."""
        self._initialize()

    def _initialize_schema(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=30)) as connection:
            _enable_wal(connection)
            if connection.execute("PRAGMA user_version").fetchone()[0] >= 4:
                return
            connection.executescript(
                "BEGIN IMMEDIATE;"
                "CREATE TABLE IF NOT EXISTS api_service_limits ("
                "group_id TEXT NOT NULL, service_id TEXT NOT NULL, capacity INTEGER, "
                "version TEXT NOT NULL DEFAULT 'unknown', "
                "capacity_source TEXT NOT NULL DEFAULT 'declared', "
                "PRIMARY KEY(group_id,service_id));"
                "CREATE TABLE IF NOT EXISTS api_endpoint_groups ("
                "wire_hash TEXT PRIMARY KEY, resource_group TEXT NOT NULL, capacity INTEGER, "
                "version TEXT NOT NULL DEFAULT 'unknown');"
                "CREATE TABLE IF NOT EXISTS api_cooldowns ("
                "scope TEXT PRIMARY KEY, until_at REAL NOT NULL);"
                "CREATE TABLE IF NOT EXISTS api_receipts ("
                "id TEXT PRIMARY KEY, identity TEXT UNIQUE NOT NULL, service_id TEXT NOT NULL, "
                "resource_group TEXT NOT NULL, model TEXT NOT NULL, config_hash TEXT NOT NULL, "
                "wire_hash TEXT, service_version TEXT NOT NULL DEFAULT 'unknown', "
                "request_hash TEXT NOT NULL, state TEXT NOT NULL, provider_job_id TEXT, "
                "owner TEXT, lease_until REAL NOT NULL, created REAL NOT NULL, "
                "updated REAL NOT NULL, "
                "generation INTEGER NOT NULL DEFAULT 1, execution_generation INTEGER, "
                "local_request_hash TEXT, "
                "context TEXT NOT NULL, result TEXT NOT NULL DEFAULT '{}');"
                "CREATE INDEX IF NOT EXISTS ix_api_active ON api_receipts(resource_group,state);"
                "CREATE INDEX IF NOT EXISTS ix_api_provider_id ON api_receipts(provider_job_id);"
                "CREATE TABLE IF NOT EXISTS api_events ("
                "sequence INTEGER PRIMARY KEY AUTOINCREMENT, receipt_id TEXT NOT NULL, "
                "operation TEXT NOT NULL, at REAL NOT NULL, status TEXT NOT NULL, "
                "metadata TEXT NOT NULL);"
                "CREATE INDEX IF NOT EXISTS ix_api_events ON api_events(receipt_id,sequence);"
                "CREATE TABLE IF NOT EXISTS api_archive_index ("
                "id TEXT PRIMARY KEY, identity TEXT UNIQUE NOT NULL, provider_job_id TEXT, "
                "archive_source INTEGER NOT NULL);"
                "CREATE TABLE IF NOT EXISTS api_archive_sources ("
                "id INTEGER PRIMARY KEY,path TEXT UNIQUE NOT NULL);"
                "CREATE INDEX IF NOT EXISTS ix_api_archive_provider ON api_archive_index("
                "provider_job_id,archive_source);"
                "CREATE INDEX IF NOT EXISTS ix_api_native_actor ON api_receipts("
                "json_extract(context,'$.project_id'),json_extract(context,'$.actor_id'),id);"
                "CREATE TABLE IF NOT EXISTS api_service_usage ("
                "service_id TEXT PRIMARY KEY,last_created REAL NOT NULL);"
                "CREATE TABLE IF NOT EXISTS api_readiness ("
                "wire_hash TEXT NOT NULL, version TEXT NOT NULL, capacity INTEGER NOT NULL, "
                "observed_at REAL NOT NULL, PRIMARY KEY(wire_hash,version));"
            )
            columns = {r[1] for r in connection.execute("PRAGMA table_info(api_service_limits)")}
            if "version" not in columns:
                connection.execute(
                    "ALTER TABLE api_service_limits ADD COLUMN "
                    "version TEXT NOT NULL DEFAULT 'unknown'"
                )
            for table, additions in (
                (
                    "api_service_limits",
                    {"capacity_source": "TEXT NOT NULL DEFAULT 'legacy_unknown'"},
                ),
                (
                    "api_endpoint_groups",
                    {"capacity": "INTEGER", "version": "TEXT NOT NULL DEFAULT 'unknown'"},
                ),
                (
                    "api_receipts",
                    {
                        "wire_hash": "TEXT",
                        "service_version": "TEXT NOT NULL DEFAULT 'unknown'",
                        "generation": "INTEGER NOT NULL DEFAULT 1",
                        "execution_generation": "INTEGER",
                        "local_request_hash": "TEXT",
                    },
                ),
            ):
                columns = {r[1] for r in connection.execute(f"PRAGMA table_info({table})")}
                for name, declaration in additions.items():
                    if name not in columns:
                        connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS ix_api_job ON api_receipts("
                "json_extract(context,'$.workspace'),json_extract(context,'$.project_id'),"
                "json_extract(context,'$.job_id'))"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS ix_api_native_actor ON api_receipts("
                "json_extract(context,'$.project_id'),json_extract(context,'$.actor_id'),id)"
            )
            connection.execute("PRAGMA user_version=4")
            connection.commit()

    @contextmanager
    def connection(
        self, *, write: bool = True, timeout_seconds: float = 30, submission_wait: bool = False
    ) -> Iterator[sqlite3.Connection]:
        """Readers run independently; only the owner hot transaction is serialized."""
        self._initialize()
        if write and not self._writer_lock.acquire(timeout=timeout_seconds):
            if submission_wait:
                raise ProviderCapacityWait(time.time() + 0.5, "ledger_writer_busy")
            error = sqlite3.OperationalError("Coordinator writer is busy")
            error.sqlite_errorcode = sqlite3.SQLITE_BUSY
            raise error
        try:
            with closing(sqlite3.connect(self.path, timeout=timeout_seconds)) as connection:
                connection.row_factory = sqlite3.Row
                try:
                    connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                except sqlite3.OperationalError as error:
                    code = getattr(error, "sqlite_errorcode", None)
                    if (
                        write
                        and submission_wait
                        and timeout_seconds < 30
                        and isinstance(code, int)
                        and code & 0xFF in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
                    ):
                        raise ProviderCapacityWait(
                            time.time() + 0.5, "ledger_writer_busy"
                        ) from error
                    raise
                try:
                    yield connection
                except BaseException:
                    connection.rollback()
                    raise
                else:
                    connection.commit()
        finally:
            if write:
                self._writer_lock.release()

    def reserve(
        self,
        service: APIService,
        model: str,
        context: Mapping[str, Any],
        request_hash: str,
        config_hash: str,
        *,
        owner: str,
        ttl: float = 120,
        capacity: int | None = None,
        writer_timeout_seconds: float = 30,
    ) -> dict[str, Any]:
        while True:
            try:
                row = self._reserve_row(
                    service,
                    model,
                    context,
                    request_hash,
                    config_hash,
                    owner=owner,
                    ttl=ttl,
                    capacity=capacity,
                    writer_timeout_seconds=writer_timeout_seconds,
                )
                break
            except _ReceiptChanged:
                continue
        return self._receipt(row)

    def _reserve_row(
        self,
        service: APIService,
        model: str,
        context: Mapping[str, Any],
        request_hash: str,
        config_hash: str,
        *,
        owner: str,
        ttl: float = 120,
        capacity: int | None = None,
        writer_timeout_seconds: float = 30,
    ) -> sqlite3.Row:
        service.binding(model)
        if not service.enabled:
            raise ProviderContractError("Cannot reserve a disabled API service")
        if set(context) - IDENTITY_FIELDS:
            raise ProviderContractError("API receipt context accepts identities/hashes only")
        safe_context = structural(context)
        _validate_context(safe_context)
        context_json = json.dumps(safe_context)
        endpoint_hash = wire_identity(
            kind=service.kind, protocol=service.protocol, base_url=service.base_url
        )
        identity = provider_config_hash(safe_context)
        stamp = time.time()
        retry_failed = False
        # Reusing a still-owned or terminal receipt does not change admission.
        # Do the indexed lookup before taking the shared writer lock; begin()
        # still fences the owner/generation immediately before any HTTP request.
        with self.connection(write=False) as reader:
            reused = receipt_record(reader, identity, identity=True)
            if reused is not None:
                failure = json.loads(reused["result"]).get("error", {})
                retry_failed = (
                    reused["state"] == "failed"
                    and (
                        failure.get("category") == "rate_limit"
                        or (
                            failure.get("category") == "temporarily_unavailable"
                            and failure.get("http_status") == 503
                            and failure.get("phase") == "submission"
                        )
                    )
                    and failure.get("definitely_not_accepted") is True
                    and not reused["provider_job_id"]
                )
                unchanged = reused["state"] != "cancelled" and not retry_failed
                if reused["state"] == "reserved":
                    unchanged = reused["owner"] == owner and reused["lease_until"] > stamp
                if unchanged:
                    if (
                        reused["request_hash"],
                        reused["config_hash"],
                        reused["service_id"],
                        reused["model"],
                    ) != (request_hash, config_hash, service.id, model):
                        raise ProviderContractError(
                            "API receipt identity is pinned to another request"
                        )
                    endpoint = reader.execute(
                        "SELECT resource_group FROM api_endpoint_groups WHERE wire_hash=?",
                        (
                            wire_identity(
                                kind=service.kind,
                                protocol=service.protocol,
                                base_url=service.base_url,
                            ),
                        ),
                    ).fetchone()
                    if endpoint and endpoint[0] != service.resource_group:
                        raise ProviderContractError(
                            "One API endpoint has conflicting shared resource groups"
                        )
                    if reused["state"] != "reserved":
                        return reused
                    usage = reader.execute(
                        "SELECT last_created FROM api_service_usage WHERE service_id=?",
                        (reused["service_id"],),
                    ).fetchone()
                    if usage and usage[0] >= reused["created"]:
                        return reused
        if self.admission_paused:
            raise ProviderCapacityWait(time.time() + 1, "coordinator_maintenance")

        def admitted(db: sqlite3.Connection, row: sqlite3.Row) -> sqlite3.Row:
            db.execute(
                "INSERT INTO api_service_usage VALUES(?,?) ON CONFLICT(service_id) "
                "DO UPDATE SET last_created=excluded.last_created "
                "WHERE api_service_usage.last_created<excluded.last_created",
                (row["service_id"], row["created"]),
            )
            return row

        with self.connection(timeout_seconds=writer_timeout_seconds, submission_wait=True) as db:
            if self.admission_paused:
                raise ProviderCapacityWait(time.time() + 1, "coordinator_maintenance")
            wire_hash, endpoint_capacity = self._check_endpoint_group(
                db, service, wire_hash=endpoint_hash
            )
            # This pool is normally empty. The workspace-first JSON index would
            # scan unrelated historical requests while holding the WAL writer.
            if (
                service.kind == "video"
                and db.execute(
                    "SELECT id FROM api_receipts INDEXED BY ix_api_active "
                    "WHERE resource_group='unresolved-video' "
                    "AND state IN ('submitting','running','collecting','unknown') "
                    "AND json_extract(context,'$.workspace')=? "
                    "AND json_extract(context,'$.job_id')=? LIMIT 1",
                    (safe_context.get("workspace"), safe_context.get("job_id")),
                ).fetchone()
            ):
                raise ProviderCapacityWait(stamp + 2, "unresolved_original_video_route")
            existing = self._hot_receipt(db, identity, reused, identity=True)
            if existing and existing["state"] == "reserved" and existing["lease_until"] <= stamp:
                db.execute(
                    "UPDATE api_receipts SET state='cancelled',updated=? WHERE id=?",
                    (stamp, existing["id"]),
                )
                existing = receipt_record(db, existing["id"])
            if existing:
                if (
                    existing["request_hash"],
                    existing["config_hash"],
                    existing["service_id"],
                    existing["model"],
                ) != (
                    request_hash,
                    config_hash,
                    service.id,
                    model,
                ):
                    raise ProviderContractError("API receipt identity is pinned to another request")
                if reused is None or existing["result"] != reused["result"]:
                    # Drop the transaction before preparing/parsing a newly returned
                    # result; endpoint/readiness changes above roll back too.
                    raise _ReceiptChanged()
                definite_retry = existing["state"] == "failed" and retry_failed
                if existing["state"] != "cancelled" and not definite_retry:
                    if existing["state"] == "reserved" and existing["owner"] != owner:
                        raise ProviderCapacityWait(existing["lease_until"], "reservation_owner")
                    return admitted(db, existing)
            declared = [c for c in (service.capacity, endpoint_capacity) if c is not None]
            declared_limit = min(declared) if declared else None
            group = db.execute(
                "SELECT capacity,version,capacity_source FROM api_service_limits "
                "WHERE group_id=? AND service_id=?",
                (service.resource_group, service.id),
            ).fetchone()
            if (
                group
                and group[0] is not None
                and group[1] == service.version
                and group[2] == "declared"
            ):
                declared_limit = (
                    min(declared_limit, group[0]) if declared_limit is not None else group[0]
                )
            # Old limits mixed ready workers with declared physical capacity.
            # Repair only against the same-wire declaration, which still fences
            # aliases and topology changes; never increase that physical bound.
            db.execute(
                "INSERT INTO api_service_limits("
                "group_id,service_id,capacity,version,capacity_source) "
                "VALUES(?,?,?,?,'declared') "
                "ON CONFLICT(group_id,service_id) DO UPDATE SET "
                "capacity=excluded.capacity,version=excluded.version,capacity_source='declared' "
                "WHERE api_service_limits.capacity IS NOT excluded.capacity "
                "OR api_service_limits.version IS NOT excluded.version "
                "OR api_service_limits.capacity_source IS NOT 'declared'",
                (service.resource_group, service.id, declared_limit, service.version),
            )
            readiness = db.execute(
                "SELECT capacity,observed_at FROM api_readiness WHERE wire_hash=? AND version=?",
                (wire_hash, service.version),
            ).fetchone()
            bounds = [c for c in (declared_limit, capacity) if c is not None]
            if readiness:
                bounds.append(readiness[0] if stamp - readiness[1] <= 90 else 0)
            limit = min(bounds) if bounds else None
            model_cooldown_scope = service.resource_group + ":" + model
            # Historical image-wide spacing must not throttle fallback models.
            # Image 429 handlers already record the exact affected model.
            cooldown = db.execute(
                "SELECT MAX(until_at) FROM api_cooldowns WHERE scope IN (?,?)",
                (
                    model_cooldown_scope if service.kind == "image" else service.resource_group,
                    model_cooldown_scope,
                ),
            ).fetchone()[0]
            if cooldown and cooldown > stamp:
                raise ProviderCapacityWait(float(cooldown), "provider_cooldown")
            # Unbounded cloud text/image APIs do not occupy an exclusive GPU
            # deployment merely because their wire protocol differs. Scanning
            # every active cloud receipt here holds the shared writer while
            # reading historical payload pages. Keep this mode gate for video
            # and every service with a declared physical bound.
            if service.kind not in {"text", "image"} or limit is not None:
                competing = db.execute(
                    "SELECT 1 FROM api_receipts WHERE resource_group=? "
                    "AND state IN ('reserved','submitting','running','collecting','unknown') "
                    "AND NOT(state='reserved' AND lease_until<=?) "
                    f"AND {_OCCUPIES_PHYSICAL_POOL} "
                    "AND ((wire_hash IS NOT NULL AND (wire_hash!=? OR service_version!=?)) "
                    "OR (wire_hash IS NULL AND service_id!=?) "
                    "OR (json_extract(context,'$.deployment_hash') IS NOT NULL AND "
                    "? IS NOT NULL AND json_extract(context,'$.deployment_hash')!=?)) LIMIT 1",
                    (
                        service.resource_group,
                        stamp,
                        wire_hash,
                        service.version,
                        service.id,
                        safe_context.get("deployment_hash"),
                        safe_context.get("deployment_hash"),
                    ),
                ).fetchone()
                if competing:
                    raise ProviderCapacityWait(stamp + 2, "shared_resource_mode")
            if limit is not None:
                occupied = db.execute(
                    "SELECT COUNT(*) FROM api_receipts WHERE resource_group=? "
                    "AND state IN ('reserved','submitting','running','collecting','unknown') "
                    "AND NOT(state='reserved' AND lease_until<=?) "
                    f"AND {_OCCUPIES_PHYSICAL_POOL}",
                    (service.resource_group, stamp),
                ).fetchone()[0]
                if occupied >= limit:
                    raise ProviderCapacityWait(stamp + 2)
            # Slow storage admission must not consume the newly granted lease.
            stamp = time.time()
            if existing:
                db.execute(
                    "UPDATE api_receipts SET state='reserved',owner=?,lease_until=?,updated=?, "
                    "generation=generation+1,execution_generation=NULL,local_request_hash=NULL "
                    "WHERE id=?",
                    (owner, stamp + ttl, stamp, existing["id"]),
                )
                row = db.execute(
                    "SELECT * FROM api_receipts WHERE id=?", (existing["id"],)
                ).fetchone()
                assert row is not None
                interval = _submission_interval_seconds(service)
                if interval:
                    db.execute(
                        "INSERT INTO api_cooldowns(scope,until_at) VALUES(?,?) "
                        "ON CONFLICT(scope) DO UPDATE SET until_at=MAX(until_at,excluded.until_at)",
                        (service.resource_group + ":" + model, stamp + interval),
                    )
                return admitted(db, row)
            identifier = uuid4().hex
            db.execute(
                "INSERT INTO api_receipts(id,identity,service_id,resource_group,model,config_hash,"
                "request_hash,state,owner,lease_until,created,updated,context,"
                "wire_hash,service_version) "
                "VALUES(?,?,?,?,?,?,?,'reserved',?,?,?,?,?,?,?)",
                (
                    identifier,
                    identity,
                    service.id,
                    service.resource_group,
                    model,
                    config_hash,
                    request_hash,
                    owner,
                    stamp + ttl,
                    stamp,
                    stamp,
                    context_json,
                    wire_hash,
                    service.version,
                ),
            )
            row = receipt_record(db, identifier, restore=True)
            assert row is not None
            interval = _submission_interval_seconds(service)
            if interval:
                db.execute(
                    "INSERT INTO api_cooldowns(scope,until_at) VALUES(?,?) "
                    "ON CONFLICT(scope) DO UPDATE SET until_at=MAX(until_at,excluded.until_at)",
                    (service.resource_group + ":" + model, stamp + interval),
                )
            return admitted(db, row)

    def release_unsubmitted(self, identifier: str, *, owner: str, generation: int) -> None:
        """Fence an unused reservation before changing its route; never undo a POST."""
        prefetched = self._prefetch(identifier)
        with self.connection(timeout_seconds=0.25, submission_wait=True) as db:
            row = self._hot_receipt(db, identifier, prefetched)
            if row is None or row["owner"] != owner or row["generation"] != generation:
                raise ProviderReceiptLeaseLost("Unsubmitted reservation fence has changed")
            if (
                row["state"] not in {"reserved", "cancelled"}
                or row["provider_job_id"]
                or row["execution_generation"] is not None
                or row["local_request_hash"] is not None
            ):
                raise ProviderContractError(
                    "Only a reservation without submission intent can reroute"
                )
            if row["state"] == "reserved":
                db.execute(
                    "UPDATE api_receipts SET state='cancelled',generation=generation+1,updated=? "
                    "WHERE id=?",
                    (time.time(), identifier),
                )

    def begin(
        self,
        identifier: str,
        *,
        owner: str,
        generation: int | None = None,
        execution_generation: int | None = None,
        local_request_hash: str | None = None,
        writer_timeout_seconds: float = 30,
    ) -> dict[str, Any]:
        # This transaction precedes the HTTP request; a busy writer is a safe wait.
        prefetched = self._prefetch(identifier)
        if prefetched is not None and prefetched["state"] != "reserved":
            if generation is not None and prefetched["generation"] != generation:
                raise ProviderCapacityWait(time.time() + 2, "stale_reservation_generation")
            if prefetched["state"] == "unknown" and not prefetched["provider_job_id"]:
                raise ProviderSubmissionUnknown(identifier)
            return self._receipt(prefetched)
        may_submit = False
        with self.connection(timeout_seconds=writer_timeout_seconds, submission_wait=True) as db:
            row = self._hot_receipt(db, identifier, prefetched)
            if row is None:
                raise ProviderContractError("API submission requires a durable reservation")
            if generation is not None and row["generation"] != generation:
                raise ProviderCapacityWait(time.time() + 2, "stale_reservation_generation")
            if row["state"] == "unknown" and not row["provider_job_id"]:
                raise ProviderSubmissionUnknown(identifier)
            if row["state"] == "reserved":
                if self.admission_paused:
                    raise ProviderCapacityWait(time.time() + 1, "coordinator_maintenance")
                if row["owner"] != owner or row["lease_until"] <= time.time():
                    raise ProviderCapacityWait(time.time() + 2, "expired_reservation")
                db.execute(
                    "UPDATE api_receipts SET state='submitting',updated=?,execution_generation=?, "
                    "local_request_hash=? WHERE id=?",
                    (time.time(), execution_generation, local_request_hash, identifier),
                )
                may_submit = True
        result = self._receipt(row)
        if may_submit:
            result.update(
                state="submitting",
                may_submit=True,
                execution_generation=execution_generation,
                local_request_hash=local_request_hash,
            )
        return result

    def observe_capacity(
        self, service: APIService, capacity: int, *, observed_at: float | None = None
    ) -> None:
        """Short-lived ready capacity, never a mutation of declared topology."""
        _number(capacity, integer=True)
        now = time.time()
        stamp = now if observed_at is None else observed_at
        _number(stamp)
        wire_hash = wire_identity(
            kind=service.kind, protocol=service.protocol, base_url=service.base_url
        )
        # Workers share readiness. A recent identical observation need not take
        # the writer lock, but only after the declared endpoint is confirmed.
        with self.connection(write=False) as reader:
            previous = reader.execute(
                "SELECT e.resource_group,e.capacity,e.version,r.capacity,r.observed_at "
                "FROM api_endpoint_groups e JOIN api_readiness r ON r.wire_hash=e.wire_hash "
                "AND r.version=e.version WHERE e.wire_hash=?",
                (wire_hash,),
            ).fetchone()
            if (
                previous is not None
                and previous[0] == service.resource_group
                and previous[1] == service.capacity
                and previous[2] == service.version
                and previous[3] == capacity
                and 0 <= stamp - previous[4] < 30
                and 0 <= now - previous[4] < 30
            ):
                return
        with self.connection() as db:
            wire_hash, _ = self._check_endpoint_group(db, service, wire_hash=wire_hash)
            db.execute(
                "INSERT INTO api_readiness VALUES(?,?,?,?) ON CONFLICT(wire_hash,version) "
                "DO UPDATE SET capacity=excluded.capacity,observed_at=excluded.observed_at "
                "WHERE excluded.observed_at>=api_readiness.observed_at AND "
                "(excluded.capacity IS NOT api_readiness.capacity "
                "OR excluded.observed_at>=api_readiness.observed_at+30)",
                (wire_hash, service.version, capacity, stamp),
            )

    def claim_collection(
        self,
        identifier: str,
        *,
        owner: str,
        execution_generation: int,
        local_request_hash: str,
        ttl: float = 120,
        writer_timeout_seconds: float = 30,
    ) -> dict[str, Any]:
        """Fence a new local collection lease without changing the remote task."""
        prefetched = self._prefetch(identifier)
        with self.connection(timeout_seconds=writer_timeout_seconds, submission_wait=True) as db:
            row = self._hot_receipt(db, identifier, prefetched)
            if row is None:
                raise ProviderContractError("Collection requires the original API receipt")
            if not row["provider_job_id"] and row["state"] not in {"complete", "failed"}:
                raise ProviderSubmissionUnknown(identifier)
            previous = row["execution_generation"]
            if previous is not None and (
                previous > execution_generation
                or (previous == execution_generation and row["owner"] not in {None, owner})
            ):
                raise ProviderReceiptLeaseLost(
                    "API collection lease generation has already advanced"
                )
            if row["local_request_hash"] not in {None, local_request_hash}:
                raise ProviderContractError("Collection cannot change the original local recipe")
            db.execute(
                "UPDATE api_receipts SET owner=?,generation=generation+1,execution_generation=?,"
                "local_request_hash=?,lease_until=?,updated=? WHERE id=?",
                (
                    owner,
                    execution_generation,
                    local_request_hash,
                    time.time() + ttl,
                    time.time(),
                    identifier,
                ),
            )
            claimed = db.execute("SELECT * FROM api_receipts WHERE id=?", (identifier,)).fetchone()
            assert claimed is not None
        return self._receipt(claimed)

    def adopt(
        self,
        service: APIService,
        model: str,
        context: Mapping[str, Any],
        request_hash: str,
        config_hash: str,
        *,
        provider_job_id: str | None,
        writer_timeout_seconds: float = 30,
    ) -> dict[str, Any]:
        row = self._adopt_row(
            service,
            model,
            context,
            request_hash,
            config_hash,
            provider_job_id=provider_job_id,
            writer_timeout_seconds=writer_timeout_seconds,
        )
        return self._receipt(row)

    def _adopt_row(
        self,
        service: APIService,
        model: str,
        context: Mapping[str, Any],
        request_hash: str,
        config_hash: str,
        *,
        provider_job_id: str | None,
        writer_timeout_seconds: float = 30,
    ) -> sqlite3.Row:
        """Account an already submitted legacy job, without spending or sending again."""
        service.binding(model)
        if set(context) - IDENTITY_FIELDS:
            raise ProviderContractError("API receipt context accepts identities/hashes only")
        safe_context = structural(context)
        _validate_context(safe_context)
        context_json = json.dumps(safe_context)
        endpoint_hash = wire_identity(
            kind=service.kind, protocol=service.protocol, base_url=service.base_url
        )
        identity, stamp = provider_config_hash(safe_context), time.time()
        prefetched = self._prefetch(identity, identity=True)
        if prefetched is not None:
            if (
                prefetched["request_hash"] != request_hash
                or prefetched["service_id"] != service.id
                or prefetched["model"] != model
                or prefetched["config_hash"] != config_hash
                or prefetched["provider_job_id"] not in {None, provider_job_id}
            ):
                raise ProviderContractError(
                    "Recovered API receipt conflicts with its original request"
                )
            with self.connection(write=False) as reader:
                endpoint = reader.execute(
                    "SELECT resource_group FROM api_endpoint_groups WHERE wire_hash=?",
                    (
                        wire_identity(
                            kind=service.kind, protocol=service.protocol, base_url=service.base_url
                        ),
                    ),
                ).fetchone()
                if endpoint and endpoint[0] != service.resource_group:
                    raise ProviderContractError(
                        "One API endpoint has conflicting shared resource groups"
                    )
            return prefetched
        with self.connection(timeout_seconds=writer_timeout_seconds, submission_wait=True) as db:
            wire_hash, _ = self._check_endpoint_group(db, service, wire_hash=endpoint_hash)
            old = self._hot_receipt(db, identity, prefetched, identity=True)
            if old:
                if (
                    old["request_hash"] != request_hash
                    or old["service_id"] != service.id
                    or old["model"] != model
                    or old["config_hash"] != config_hash
                    or old["provider_job_id"]
                    not in {
                        None,
                        provider_job_id,
                    }
                ):
                    raise ProviderContractError(
                        "Recovered API receipt conflicts with its original request"
                    )
                return old
            identifier = uuid4().hex
            db.execute(
                "INSERT INTO api_receipts(id,identity,service_id,resource_group,model,config_hash,"
                "request_hash,state,provider_job_id,lease_until,created,updated,context,"
                "wire_hash,service_version) "
                "VALUES(?,?,?,?,?,?,?,?,?,0,?,?,?,?,?)",
                (
                    identifier,
                    identity,
                    service.id,
                    service.resource_group,
                    model,
                    config_hash,
                    request_hash,
                    "running" if provider_job_id else "unknown",
                    provider_job_id,
                    stamp,
                    stamp,
                    context_json,
                    wire_hash,
                    service.version,
                ),
            )
            row = receipt_record(db, identifier, restore=True)
            assert row is not None
            return row

    @staticmethod
    def _check_endpoint_group(
        db: sqlite3.Connection, service: APIService, *, wire_hash: str
    ) -> tuple[str, int | None]:
        # Separate runtimes may load different registries. A renamed endpoint
        # still cannot acquire a second physical pool by changing its group.
        previous = db.execute(
            "SELECT resource_group,capacity,version FROM api_endpoint_groups WHERE wire_hash=?",
            (wire_hash,),
        ).fetchone()
        if previous and previous[0] != service.resource_group:
            raise ProviderContractError("One API endpoint has conflicting shared resource groups")
        limit = service.capacity
        if previous and previous[1] is not None and previous[2] == service.version:
            limit = min(limit, previous[1]) if limit is not None else previous[1]
        db.execute(
            "INSERT INTO api_endpoint_groups(wire_hash,resource_group,capacity,version) "
            "VALUES(?,?,?,?) ON CONFLICT(wire_hash) DO UPDATE SET "
            "capacity=excluded.capacity,version=excluded.version "
            "WHERE api_endpoint_groups.capacity IS NOT excluded.capacity "
            "OR api_endpoint_groups.version IS NOT excluded.version",
            (wire_hash, service.resource_group, limit, service.version),
        )
        return wire_hash, limit

    def event(
        self,
        identifier: str,
        operation: str,
        status: str,
        metadata: Mapping[str, Any] | None = None,
        *,
        writer_timeout_seconds: float = 30,
    ) -> None:
        # Do not accept arbitrary transport/model bodies at the audit boundary.
        for value in (identifier, operation, status):
            _id(value)
        allowed = {
            "http_status",
            "effective_http_status",
            "category",
            "phase",
            "known_job",
            "definitely_not_accepted",
            "retryable",
            "bytes",
            "elapsed_seconds",
            "actual_model",
            "usage",
            "event_type",
            "provider_request_id",
            "wire_request_hash",
            "request_bytes",
            "method",
        }
        data = dict(metadata or {})
        if set(data) - allowed:
            raise ProviderContractError("API event contains non-audit fields")
        if "usage" in data:
            data["usage"] = numeric_usage(data["usage"])
        _audit_scalars(data)
        structural(data)
        metadata_json = json.dumps(data)
        with self.connection(timeout_seconds=writer_timeout_seconds) as db:
            db.execute(
                "INSERT INTO api_events(receipt_id,operation,at,status,metadata) VALUES(?,?,?,?,?)",
                (identifier, operation, time.time(), status, metadata_json),
            )

    def update(
        self,
        identifier: str,
        state: str,
        *,
        provider_job_id: str | None = None,
        result: Mapping[str, Any] | None = None,
        owner: str | None = None,
        generation: int | None = None,
        writer_timeout_seconds: float = 30,
    ) -> None:
        if not isinstance(state, str) or state not in {*ACTIVE, "complete", "failed", "cancelled"}:
            raise ProviderContractError("Invalid API receipt state")
        allowed = {
            "actual_model",
            "usage",
            "media",
            "requested_latent",
            "applied_latent",
            "error",
            "provider_request_id",
            "outcome",
            "video_completion",
        }
        data = {k: v for k, v in (result or {}).items() if v is not None}
        if set(data) - allowed:
            raise ProviderContractError("API result contains non-receipt fields")
        _validate_result(data)
        if provider_job_id is not None and atomic_id(provider_job_id) is None:
            raise ProviderContractError("API receipt cannot change its original provider ID")
        media_routes = []
        for media in data.get("media", []):
            match = re.fullmatch(
                r"/api/v2/tasks/([A-Za-z0-9_-]+)/media/([0-9a-f]{64})", media.get("url", "")
            )
            if match:
                media_routes.append((match[1], match[2], media.get("sha256")))
        prefetched = self._prefetch(identifier)
        while True:
            if prefetched is None:
                raise ProviderContractError("API receipt does not exist")
            task_id = json.loads(prefetched["context"]).get("task_id") if media_routes else None
            if any(task != task_id or sha != media_sha for task, sha, media_sha in media_routes):
                raise ProviderContractError("Comparison media route differs from receipt ownership")
            merged_json = json.dumps({**json.loads(prefetched["result"]), **data})
            with self.connection(timeout_seconds=writer_timeout_seconds) as db:
                old = self._hot_receipt(db, identifier, prefetched)
                if not old:
                    raise ProviderContractError("API receipt does not exist")
                if old["result"] != prefetched["result"] or old["context"] != prefetched["context"]:
                    prefetched = old
                    continue
                if (owner is not None and old["owner"] != owner) or (
                    generation is not None and old["generation"] != generation
                ):
                    raise ProviderReceiptLeaseLost(
                        "API receipt publication lost its owner/generation lease"
                    )
                if provider_job_id is not None and (
                    old["provider_job_id"] not in {None, provider_job_id}
                ):
                    raise ProviderContractError(
                        "API receipt cannot change its original provider ID"
                    )
                if completion := data.get("video_completion"):
                    prior = json.loads(old["result"]).get("video_completion")
                    if (
                        state not in {"running", "collecting"}
                        or old["state"] not in {"running", "collecting", "unknown"}
                        or not owner
                        or generation is None
                        or old["execution_generation"] is None
                        or old["local_request_hash"] is None
                        or old["provider_job_id"] != completion["provider_job_id"]
                        or provider_job_id != completion["provider_job_id"]
                        or json.loads(old["context"]).get("operation") != "video_generation"
                        or (prior is not None and prior != completion)
                    ):
                        raise ProviderContractError(
                            "Video completion requires its accepted original collection receipt"
                        )
                transitions = {
                    "reserved": {"reserved", "submitting", "failed", "cancelled"},
                    "submitting": {
                        "submitting",
                        "running",
                        "collecting",
                        "unknown",
                        "complete",
                        "failed",
                    },
                    "running": {
                        "running",
                        "collecting",
                        "unknown",
                        "complete",
                        "failed",
                        "cancelled",
                    },
                    "collecting": {
                        "collecting",
                        "running",
                        "unknown",
                        "complete",
                        "failed",
                        "cancelled",
                    },
                    "unknown": {"unknown", "running", "collecting", "complete", "failed"},
                    "complete": {"complete"},
                    "failed": {"failed"},
                    "cancelled": {"cancelled"},
                }
                if state not in transitions[old["state"]]:
                    raise ProviderContractError(
                        "API receipt state transition requires reconciliation"
                    )
                db.execute(
                    "UPDATE api_receipts SET state=?,provider_job_id=COALESCE(?,provider_job_id),"
                    "result=?,updated=? WHERE id=?",
                    (state, provider_job_id, merged_json, time.time(), identifier),
                )
            return

    def cooldown(self, group: str, seconds: float, *, model: str | None = None) -> None:
        scope = group + ":" + model if model else group
        with self.connection() as db:
            db.execute(
                "INSERT INTO api_cooldowns VALUES(?,?) ON CONFLICT(scope) DO UPDATE SET "
                "until_at=MAX(until_at,excluded.until_at)",
                (scope, time.time() + max(0, seconds)),
            )

    def get(self, identifier: str) -> dict[str, Any]:
        with self.connection(write=False) as db:
            row = receipt_record(db, identifier)
            if not row:
                raise ProviderContractError("API receipt does not exist")
            return self._receipt(row)

    def by_provider_id(
        self, provider_id: str, *, workspace: str, project_id: str, actor_id: str
    ) -> dict[str, Any]:
        with self.connection(write=False) as db:
            rows = db.execute(
                "SELECT * FROM api_receipts WHERE provider_job_id=? AND "
                "json_extract(context,'$.workspace')=? AND json_extract(context,'$.project_id')=? "
                "AND json_extract(context,'$.actor_id')=?",
                (provider_id, workspace, project_id, actor_id),
            ).fetchall()
            combined = {
                row["id"]: row
                for row in archived_owned_receipts(
                    db, provider_id, workspace=workspace, project_id=project_id, actor_id=actor_id
                )
            }
            combined.update({row["id"]: row for row in rows})
            rows = list(combined.values())
            if len(rows) != 1:
                raise ProviderContractError("Provider response is not owned by this native actor")
            return self._receipt(rows[0])

    def events(self, identifier: str) -> list[dict[str, Any]]:
        with self.connection(write=False) as db:
            rows = {row["sequence"]: row for row in archived_events(db, identifier)}
            rows.update(
                {
                    row["sequence"]: dict(row)
                    for row in db.execute(
                        "SELECT * FROM api_events WHERE receipt_id=? ORDER BY sequence",
                        (identifier,),
                    )
                }
            )
            return [
                {**rows[key], "metadata": json.loads(rows[key]["metadata"])} for key in sorted(rows)
            ]

    def native_receipt_ids(
        self,
        project_id: str,
        actor_id: str,
        *,
        candidate_ids: Sequence[str] | None = None,
    ) -> list[str]:
        if candidate_ids is not None and not candidate_ids:
            return []
        candidates = sorted(set(candidate_ids)) if candidate_ids is not None else None
        chunks: list[Sequence[str] | None] = (
            [candidates[offset : offset + 500] for offset in range(0, len(candidates), 500)]
            if candidates is not None
            else [None]
        )
        with self.connection(write=False) as db:
            has_archive = bool(
                db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='api_archive_receipt_summary'"
                ).fetchone()
            )
            identifiers: set[str] = set()
            for chunk in chunks:
                suffix = " AND id IN (" + ",".join("?" for _ in chunk) + ")" if chunk else ""
                parameters = (project_id, actor_id, *(chunk or ()))
                identifiers.update(
                    str(row[0])
                    for row in db.execute(
                        "SELECT id FROM api_receipts INDEXED BY ix_api_native_actor WHERE "
                        "json_extract(context,'$.project_id')=? "
                        "AND json_extract(context,'$.actor_id')=?" + suffix,
                        parameters,
                    )
                )
                if has_archive:
                    identifiers.update(
                        str(row[0])
                        for row in db.execute(
                            "SELECT id FROM api_archive_receipt_summary "
                            "INDEXED BY ix_api_archive_actor WHERE project_id=? AND actor_id=?"
                            + suffix,
                            parameters,
                        )
                    )
            return sorted(identifiers)

    def model_cooldown_hint(
        self, service: APIService, model: str, stamp: float
    ) -> tuple[bool, float]:
        with self.connection(write=False) as db:
            cooldown = db.execute(
                "SELECT until_at FROM api_cooldowns WHERE scope=? AND until_at>?",
                (service.resource_group + ":" + model, stamp),
            ).fetchone()
            if cooldown is None:
                return False, 0
            hint = db.execute(
                "SELECT updated,json_extract(result,'$.error.retry_after_seconds') "
                "FROM api_receipts WHERE resource_group=? AND model=? AND state='failed' "
                "AND json_type(result,'$.error.retry_after_seconds') IN ('integer','real') "
                "ORDER BY updated DESC LIMIT 1",
                (service.resource_group, model),
            ).fetchone()
        if hint is None or not math.isfinite(float(hint[1])) or hint[1] < 0:
            return True, 0
        return True, max(0.0, min(float(cooldown[0]), float(hint[0]) + float(hint[1])) - stamp)

    def inflight(self, *, service_ids: Sequence[str] | None = None) -> dict[str, int]:
        services = tuple(dict.fromkeys(service_ids)) if service_ids is not None else None
        if services == ():
            return {}
        predicate = ""
        parameters: list[Any] = [time.time()]
        if services is not None:
            predicate = " AND service_id IN (" + ",".join("?" for _ in services) + ")"
            parameters.extend(services)
        with self.connection(write=False) as db:
            return {
                str(row[0]): int(row[1])
                for row in db.execute(
                    "SELECT service_id,COUNT(*) FROM api_receipts "
                    "WHERE state IN ('reserved','submitting','running','collecting','unknown') "
                    "AND NOT (state='reserved' AND lease_until<=?) "
                    f"AND {_OCCUPIES_PHYSICAL_POOL}" + predicate + " GROUP BY service_id",
                    parameters,
                )
            }

    def active(
        self,
        *,
        resource_groups: Sequence[str] | None = None,
        operation: str | None = None,
    ) -> list[dict[str, Any]]:
        groups = tuple(dict.fromkeys(resource_groups)) if resource_groups is not None else None
        if groups == ():
            return []
        predicates = [
            "state IN ('reserved','submitting','running','collecting','unknown')",
            "NOT(state='reserved' AND lease_until<=?)",
        ]
        parameters: list[Any] = [time.time()]
        if groups is not None:
            predicates.append("resource_group IN (" + ",".join("?" for _ in groups) + ")")
            parameters.extend(groups)
        if operation is not None:
            predicates.append("json_extract(context,'$.operation')=?")
            parameters.append(operation)
        # Filter before decoding potentially large unrelated native response records.
        with self.connection(write=False) as db:
            return [
                self._receipt(r)
                for r in db.execute(
                    "SELECT * FROM api_receipts WHERE "
                    + " AND ".join(predicates)
                    + " ORDER BY created,id",
                    parameters,
                )
            ]

    def export(self) -> list[dict[str, Any]]:
        """Inspect all stored receipts for a local ledger maintenance check."""
        with self.connection(write=False) as db:
            hot = db.execute("SELECT * FROM api_receipts")
            rows = sorted(
                [*hot, *archived_export(db)],
                key=lambda row: (row["created"], row["id"]),
            )
            return [self._receipt(row) for row in rows]

    @staticmethod
    def _receipt(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["context"], value["result"] = (
            json.loads(value["context"]),
            json.loads(value["result"]),
        )
        value["may_submit"] = False
        return value


def _validate_result(data: dict[str, Any]) -> None:
    if "usage" in data:
        data["usage"] = numeric_usage(data["usage"])
    for key in {"actual_model", "provider_request_id"}.intersection(data):
        if data[key] is not None and atomic_id(data[key]) is None:
            raise ProviderContractError("API receipt identifier is invalid")
    if "error" in data:
        error = _shape(
            data["error"],
            {
                "category",
                "http_status",
                "effective_http_status",
                "phase",
                "known_job",
                "code",
                "definitely_not_accepted",
                "retryable",
                "outcome_known",
                "retry_after_seconds",
                "upstream_rejection",
            },
        )
        if "upstream_rejection" in error:
            diagnostics = _shape(error["upstream_rejection"], {"code", "type", "param", "message"})
            if any(
                not isinstance(value, str) or len(value) > (500 if key == "message" else 128)
                for key, value in diagnostics.items()
            ):
                raise ProviderContractError("API rejection diagnostics must be bounded strings")
            redacted = http_rejection_details(
                json.dumps({"error": diagnostics}).encode(),
                HttpRequest("POST", "http://redacted.invalid"),
            )
            if diagnostics != redacted:
                raise ProviderContractError("API rejection diagnostics must already be redacted")
        _audit_scalars({key: value for key, value in error.items() if key != "upstream_rejection"})
        structural(error)
    for key in {"requested_latent", "applied_latent"}.intersection(data):
        _latent_receipt(data[key])
        structural(data[key])
    if "media" in data:
        _media_receipts(data["media"])
    if "video_completion" in data:
        completion = _shape(
            data["video_completion"],
            {"provider_job_id", "num_frames", "invalid_frame_count", "invalid_latent_receipt"},
        )
        if atomic_id(completion.get("provider_job_id")) is None:
            raise ProviderContractError("Video completion must identify its original task")
        if "num_frames" in completion:
            _number(completion["num_frames"], integer=True)
            if not completion["num_frames"]:
                raise ProviderContractError("Video completion frame count must be positive")
        if "invalid_frame_count" in completion and (
            completion["invalid_frame_count"] is not True or "num_frames" in completion
        ):
            raise ProviderContractError("Video completion frame count proof is inconsistent")
        if "invalid_latent_receipt" in completion and (
            completion["invalid_latent_receipt"] is not True or "applied_latent" in data
        ):
            raise ProviderContractError("Video completion latent proof is inconsistent")
    if "outcome" in data:
        outcome = _shape(data["outcome"], {"status", "provider_job_id", "media", "result"})
        if not isinstance(outcome.get("status"), str) or outcome["status"] not in {
            "complete",
            "failed",
        }:
            raise ProviderContractError("Recovery outcome is not a material result")
        if (
            outcome.get("provider_job_id") is not None
            and atomic_id(outcome["provider_job_id"]) is None
        ):
            raise ProviderContractError("Recovery provider ID is invalid")
        _media_receipts(outcome.get("media", []))
        result = _shape(
            outcome.get("result", {}),
            {
                "media_id",
                "api_receipt_id",
                "api_receipt_generation",
                "recovered_from_shared_receipt",
                "provenance",
                "video_delivery",
            },
        )
        provenance = result.get("provenance", {})
        _shape(
            provenance,
            {
                "actual_model",
                "usage",
                "media",
                "requested_latent",
                "applied_latent",
                "error",
                "provider_request_id",
            },
        )
        _validate_result(provenance)
        if "video_delivery" in result:
            validate_video_delivery(
                outcome["status"],
                outcome.get("media", []),
                result.get("media_id"),
                result["video_delivery"],
            )
        for key in {"media_id", "api_receipt_id"}.intersection(result):
            _id(result[key])
        _number(result.get("api_receipt_generation"), integer=True)
        if "recovered_from_shared_receipt" in result and not isinstance(
            result["recovered_from_shared_receipt"], bool
        ):
            raise ProviderContractError("Recovery flag must be boolean")
        structural({k: v for k, v in result.items() if k != "provenance"})


def validate_video_delivery(
    status: str, records: Sequence[Mapping[str, Any]], media_id: str | None, value: Any
) -> dict[str, Any]:
    delivery = _shape(value, {"raw_media_id", "receipt"})
    numbers = {
        "raw_duration_seconds",
        "delivered_duration_seconds",
        "requested_delivery_seconds",
        "fps",
    }
    counts = {"raw_audio_streams", "delivered_audio_streams"}
    flags = {"joint_audio_inference_disabled", "padding_or_stretching"}
    proof = _shape(
        delivery.get("receipt"), {"raw_sha256", "delivered_sha256"} | numbers | counts | flags
    )
    if len(proof) != len(numbers) + len(counts) + len(flags) + 2:
        raise ProviderContractError("Video delivery requires its complete bounded receipt")
    _id(delivery.get("raw_media_id"))
    for key in numbers | counts:
        if key != "requested_delivery_seconds" and proof[key] is None:
            raise ProviderContractError("Video delivery measurements must be numeric")
        _number(proof[key], integer=key in counts)
    _id(media_id)
    media = {item.get("id"): item for item in records if item.get("mime_type") == "video/mp4"}
    raw = media.get(delivery.get("raw_media_id"), {})
    delivered = media.get(media_id, {})
    if (
        status != "complete"
        or not delivery.get("raw_media_id")
        or not media_id
        or not raw
        or not delivered
        or len(media) != len(records)
        or set(media) != {delivery["raw_media_id"], media_id}
        or raw["sha256"] != proof.get("raw_sha256")
        or delivered["sha256"] != proof.get("delivered_sha256")
        or raw.get("duration") != proof.get("raw_duration_seconds")
        or delivered.get("duration") != proof.get("delivered_duration_seconds")
        or any(proof.get(key) is not False for key in flags)
    ):
        raise ProviderContractError("Video delivery proof does not match its retained media")
    return proof


def _shape(value: Any, fields: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - fields:
        raise ProviderContractError("API recovery metadata violates its exact structural schema")
    return value


def _id(value: Any) -> None:
    if value is not None and atomic_id(value) is None:
        raise ProviderContractError("API audit identifiers must be atomic strings")


def _number(value: Any, *, integer: bool = False) -> None:
    if value is None:
        return
    valid = not isinstance(value, bool) and isinstance(value, int if integer else (int, float))
    if valid:
        try:
            valid = math.isfinite(value) and value >= 0
        except (OverflowError, ValueError):
            valid = False
    if not valid:
        raise ProviderContractError("API audit numbers must be finite nonnegative scalars")


def _audit_scalars(data: Mapping[str, Any]) -> None:
    for key, value in data.items():
        if key in {"bytes", "request_bytes", "http_status", "effective_http_status"}:
            _number(value, integer=True)
        elif key in {"elapsed_seconds", "retry_after_seconds"}:
            _number(value)
        elif key in {"known_job", "definitely_not_accepted", "retryable", "outcome_known"}:
            if value is not None and not isinstance(value, bool):
                raise ProviderContractError("API audit flags must be boolean")
        elif key != "usage":
            _id(value)


def _latent_receipt(value: Any) -> None:
    fields = {
        "mode",
        "binding",
        "image_index",
        "video_index",
        "source_candidate_id",
        "endpoint_sha256",
        "tail_sha256",
        "tail_frame_count",
        "applied",
    }
    data = _shape(value, fields)
    for key, item in data.items():
        if key in {"image_index", "video_index", "tail_frame_count"}:
            _number(item, integer=True)
        elif key == "applied":
            if item is not None and not isinstance(item, bool):
                raise ProviderContractError("Latent applied flag must be boolean")
        elif key.endswith("sha256"):
            if (
                item is not None
                and item != ""
                and (not isinstance(item, str) or not re.fullmatch(r"[0-9a-f]{64}", item))
            ):
                raise ProviderContractError("Latent receipt hash is invalid")
        else:
            _id(item)


def _validate_context(data: dict[str, Any]) -> None:
    for key, value in data.items():
        if key in {"attempt", "context_epoch"}:
            _number(value, integer=True)
        elif key == "workspace":
            if not isinstance(value, str):
                raise ProviderContractError("API workspace must be a path string")
        elif key != "workload":
            _id(value)
    workload = _shape(
        data.get("workload", {}),
        {
            "duration",
            "video_settings",
            "bindings",
            "video_inputs",
            "audio_inputs",
            "previous_end_state",
            "repair_plan",
            "latent_continuation",
        },
    )
    _number(workload.get("duration"))
    settings = _shape(workload.get("video_settings", {}), {"short_edge", "aspect_ratio"})
    _number(settings.get("short_edge"), integer=True)
    if "aspect_ratio" in settings and (
        not isinstance(settings["aspect_ratio"], str)
        or not re.fullmatch(r"[1-9]\d*:[1-9]\d*", settings["aspect_ratio"])
    ):
        raise ProviderContractError("Routing aspect ratio is invalid")
    for key in {"bindings", "video_inputs", "audio_inputs"}.intersection(workload):
        if not isinstance(workload[key], list):
            raise ProviderContractError("Routing input counts must be lists")
        for item in workload[key]:
            _shape(item, {"duration"} if key == "video_inputs" else set())
            _number(item.get("duration"))
    previous = _shape(workload.get("previous_end_state", {}), {"present"})
    if "present" in previous and not isinstance(previous["present"], bool):
        raise ProviderContractError("Routing predecessor presence must be boolean")
    repair = _shape(workload.get("repair_plan", {}), {"mode"})
    _id(repair.get("mode"))
    _latent_receipt(workload.get("latent_continuation", {}))


def _media_receipts(value: Any) -> None:
    if not isinstance(value, list):
        raise ProviderContractError("Media receipts must be a list")
    for item in value:
        media = _shape(
            item,
            {
                "id",
                "sha256",
                "mime_type",
                "byte_size",
                "width",
                "height",
                "duration",
                "url",
                "owner_actor_id",
                "source_candidate_id",
                "source_revision_id",
                "asset_id",
            },
        )
        for key in {
            "id",
            "owner_actor_id",
            "source_candidate_id",
            "source_revision_id",
            "asset_id",
        }.intersection(media):
            _id(media[key])
        for key in {"byte_size", "width", "height"}.intersection(media):
            _number(media[key], integer=True)
        _number(media.get("duration"))
        if not re.fullmatch(
            r"(?:/api/v1/studio/projects/[A-Za-z0-9_-]+/media/[A-Za-z0-9_-]+|"
            r"/api/v2/tasks/[A-Za-z0-9_-]+/media/[0-9a-f]{64})",
            str(media.get("url", "")),
        ):
            raise ProviderContractError("Recovery permits only exact local material routes")
        if not re.fullmatch(r"[0-9a-f]{64}", str(media.get("sha256", ""))):
            raise ProviderContractError("Recovery media needs an exact content hash")
        if not re.fullmatch(r"[A-Za-z0-9.+-]+/[A-Za-z0-9.+-]+", str(media.get("mime_type", ""))):
            raise ProviderContractError("Recovery media MIME type is invalid")
        structural({k: v for k, v in media.items() if k != "url"})
