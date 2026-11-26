"""Bind a release workspace to its SQLite file, media directories and provider ledger."""

import fcntl
import json
import os
import re
import sqlite3
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import ExitStack, closing, contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import event
from sqlalchemy.engine import Engine

from stateweaver.persistence.database import Database, sqlite_database_path
from stateweaver.providers.base import ProviderContractError, provider_config_hash
from stateweaver.providers.coordination import ProviderCoordinator

from .native_receipts import NativeReceipts
from .role_receipts import original_role_receipt, verify_pending_role_assets
from .settings import StudioSettings

# SQLite stores application_id in the database header, including in SQLite
# backups. It distinguishes this release's database from previous workspaces.
_RELEASE_APPLICATION_ID = 0x53575231
_PRODUCT = "stateweaver"
_STORAGE_FORMAT = "release-v1"
_ROOT_MARKER = ".agentvideo-storage.json"
_IDENTITY_VIEW = "_stateweaver_release_storage_identity"
_RESTORE_DATABASE = "Restore the original initialized StateWeaver database before startup"
_NEW_WORKSPACE = "Start the StateWeaver release with a new data directory and unused database"
_RESTORE_BINDING = "Restore the original StateWeaver release workspace binding before startup"


@dataclass(frozen=True)
class StorageIdentity:
    storage_id: str
    initialized: bool
    binding_hash: str


class V2DatabaseUnavailable(RuntimeError):
    """An initialized database was moved, replaced or became unreadable."""


class ActiveV2Database:
    """Keep an already-running API pinned to its initialized SQLite file."""

    def __init__(self, database: Database, canonical: Path, storage_id: str) -> None:
        self.engine: Engine = database.engine
        self.path = Path(database.engine.url.database or str(canonical)).absolute()
        self.uri = canonical.as_uri() + "?mode=rw"
        self.storage_id = storage_id
        self.file_identity = self._file_identity()

    def _file_identity(self) -> tuple[int, int]:
        try:
            stat = self.path.stat()
        except OSError as error:
            raise V2DatabaseUnavailable(_RESTORE_DATABASE) from error
        return stat.st_dev, stat.st_ino

    def _require_path(self) -> None:
        if self._file_identity() != self.file_identity:
            raise V2DatabaseUnavailable(_RESTORE_DATABASE)

    def _before_connect(
        self, _dialect: object, _record: object, args: list[Any], options: dict[str, Any]
    ) -> None:
        self._require_path()
        args[0] = self.uri
        options["uri"] = True

    def _check_connection(self, connection: sqlite3.Connection, *_: object) -> None:
        self._require_path()
        try:
            identifier = _database_storage_id(connection)
        except (ValueError, sqlite3.Error) as error:
            raise V2DatabaseUnavailable(_RESTORE_DATABASE) from error
        if identifier != self.storage_id:
            raise V2DatabaseUnavailable(_RESTORE_DATABASE)

    def verify(self) -> None:
        with self.engine.connect():
            pass

    def attach(self) -> None:
        event.listen(self.engine, "do_connect", self._before_connect)
        event.listen(self.engine, "connect", self._check_connection, insert=True)
        event.listen(self.engine, "checkout", self._check_connection)

    def close(self) -> None:
        event.remove(self.engine, "checkout", self._check_connection)
        event.remove(self.engine, "connect", self._check_connection)
        event.remove(self.engine, "do_connect", self._before_connect)


def guard_active_v2_database(settings: StudioSettings, database: Database) -> ActiveV2Database:
    storage_id = v2_storage_identity(settings)
    canonical, _ = sqlite_database_path(database.url)
    assert canonical is not None
    guard = ActiveV2Database(database, canonical, storage_id)
    guard.attach()
    try:
        guard.verify()
    except BaseException:
        guard.close()
        raise
    return guard


def _read_only(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)


def _marker_path(database: Path) -> Path:
    return database.with_name(database.name + ".stateweaver-release.json")


def _identity_path(database: Path) -> Path:
    return database.with_name(database.name + ".stateweaver-release-identity.json")


def _read_identity(database: Path) -> StorageIdentity | None:
    path = _identity_path(database)
    if not path.exists():
        return None
    identity = json.loads(path.read_text())
    if not isinstance(identity, dict):
        raise ValueError("StateWeaver storage identity is invalid")
    storage_id = identity.get("storage_id")
    initialized = identity.get("initialized")
    binding_hash = identity.get("binding_hash")
    if (
        not isinstance(storage_id, str)
        or re.fullmatch(r"[0-9a-f]{32}", storage_id) is None
        or not isinstance(initialized, bool)
        or not isinstance(binding_hash, str)
        or re.fullmatch(r"[0-9a-f]{64}", binding_hash) is None
    ):
        raise ValueError("StateWeaver storage identity is invalid")
    return StorageIdentity(storage_id, initialized, binding_hash)


def v2_storage_identity(settings: StudioSettings) -> str:
    """Opaque ID for this storage binding, consistent across process restarts."""
    database, _ = sqlite_database_path(settings.database_url)
    identity = _read_identity(database) if database is not None else None
    if identity is None:
        raise ValueError("StateWeaver storage identity is missing")
    return identity.storage_id


def _database_storage_id(connection: sqlite3.Connection) -> str | None:
    if not connection.execute(
        "SELECT 1 FROM sqlite_schema WHERE type='view' AND name=?", (_IDENTITY_VIEW,)
    ).fetchone():
        return None
    row = connection.execute(f"SELECT storage_id FROM {_IDENTITY_VIEW}").fetchone()
    value = row[0] if row else None
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{32}", value) is None:
        raise ValueError("StateWeaver SQLite storage identity is invalid")
    return value


def _require_owned_database(database: Path, identity: StorageIdentity) -> None:
    if not database.exists():
        if identity.initialized:
            raise ValueError(_RESTORE_DATABASE)
        return

    error = _RESTORE_DATABASE if identity.initialized else _NEW_WORKSPACE
    if not database.is_file():
        raise ValueError(error)

    # Check the fixed SQLite header before opening the file in SQLite. Even a
    # read-only SQLite connection can create WAL sidecars in an old workspace.
    with database.open("rb") as stream:
        header = stream.read(72)
    if not identity.initialized and not header:
        # A stopped first boot may have created the file before stamping it.
        return
    if (
        len(header) != 72
        or not header.startswith(b"SQLite format 3\x00")
        or int.from_bytes(header[68:72], "big") != _RELEASE_APPLICATION_ID
    ):
        raise ValueError(error)
    try:
        with closing(_read_only(database)) as connection:
            if _database_storage_id(connection) != identity.storage_id:
                raise ValueError(_RESTORE_DATABASE)
    except sqlite3.Error as exc:
        raise ValueError(_RESTORE_DATABASE) from exc


def _check_database_binding(
    database: Path,
    binding: Mapping[str, str | None],
) -> StorageIdentity | None:
    for suffix in (
        ".agentvideo-codex.json",
        ".agentvideo-v2.json",
        ".agentvideo-v2-identity.json",
    ):
        original = database.with_name(database.name + suffix)
        if original.exists() or original.is_symlink():
            raise ValueError(_NEW_WORKSPACE)

    marker = _marker_path(database)
    bound = marker.exists()
    if bound and json.loads(marker.read_text()) != binding:
        raise ValueError("StateWeaver storage or provider ledger differs from the original binding")
    identity = _read_identity(database)
    if identity is None:
        if bound:
            raise ValueError(_RESTORE_BINDING)
        if database.exists() or any(
            database.with_name(database.name + suffix).exists()
            for suffix in ("-wal", "-shm", "-journal")
        ):
            raise ValueError(_NEW_WORKSPACE)
        return None
    if identity.binding_hash != provider_config_hash(binding):
        raise ValueError("StateWeaver storage identity differs from the original binding")
    if identity.initialized and not bound:
        raise ValueError(_RESTORE_BINDING)
    _require_owned_database(database, identity)
    return identity


@contextmanager
def _binding_lock(marker: Path) -> Iterator[None]:
    marker.parent.mkdir(parents=True, exist_ok=True)
    with marker.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _write_binding(
    marker: Path, binding: Mapping[str, str | bool | None], *, exclusive: bool = False
) -> bool:
    with tempfile.NamedTemporaryFile(mode="w", dir=marker.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(dict(binding), stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
            if exclusive:
                try:
                    os.link(temporary, marker)
                except FileExistsError:
                    return False
            else:
                temporary.replace(marker)
            directory = os.open(marker.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            return True
        finally:
            temporary.unlink(missing_ok=True)


def _storage_binding(
    settings: StudioSettings, database: Path, ledger: Path
) -> dict[str, str | None]:
    work = settings.work_root.resolve()
    cas = settings.cas_root.resolve()
    scratch = settings.media_scratch_root.resolve() if settings.media_scratch_root else None
    roots = [work, cas, *([scratch] if scratch else [])]
    if any(database.is_relative_to(root) for root in roots) or any(
        left.is_relative_to(right) or right.is_relative_to(left)
        for index, left in enumerate(roots)
        for right in roots[index + 1 :]
    ):
        raise ValueError("StateWeaver requires distinct work and media locations")
    if ledger == database or (ledger.exists() and database.exists() and ledger.samefile(database)):
        raise ValueError("StateWeaver requires a separate provider ledger")
    return {
        "runtime": settings.role_runtime,
        "product": _PRODUCT,
        "storage_format": _STORAGE_FORMAT,
        "database": str(database),
        "work": str(work),
        "cas": str(cas),
        "media_scratch": str(scratch) if scratch else None,
        "provider_ledger": str(ledger),
    }


def _root_claims(settings: StudioSettings, database: Path) -> dict[Path, dict[str, str | None]]:
    claims: dict[Path, dict[str, str | None]] = {}
    for kind, path in (
        ("work", settings.work_root),
        ("cas", settings.cas_root),
        ("media_scratch", settings.media_scratch_root),
    ):
        if path is None:
            continue
        root = path.resolve()
        claims[root / _ROOT_MARKER] = {
            "runtime": settings.role_runtime,
            "product": _PRODUCT,
            "storage_format": _STORAGE_FORMAT,
            "database": str(database),
            "kind": kind,
        }
    return claims


def _check_root_claims(claims: dict[Path, dict[str, str | None]]) -> None:
    for marker, claim in claims.items():
        for directory in (marker.parent, *marker.parent.parents):
            existing = directory / _ROOT_MARKER
            if not existing.exists():
                continue
            stored = json.loads(existing.read_text())
            if stored != claim or directory != marker.parent:
                raise ValueError(
                    "Studio work or media directory belongs to another storage binding"
                )


def _raise_walk_error(error: OSError) -> None:
    raise error


def _check_descendant_claims(claims: dict[Path, dict[str, str | None]]) -> None:
    for marker in claims:
        for directory, _, names in os.walk(marker.parent, onerror=_raise_walk_error):
            if directory != str(marker.parent) and _ROOT_MARKER in names:
                raise ValueError(
                    "Studio work or media directory belongs to another storage binding"
                )


@contextmanager
def _lock_roots(claims: dict[Path, dict[str, str | None]]) -> Iterator[None]:
    _check_root_claims(claims)
    roots = {marker.parent for marker in claims}
    for root in roots:
        root.mkdir(parents=True, exist_ok=True)
    directories = roots | {parent for root in roots for parent in root.parents}
    with ExitStack() as locks:
        # A parent claim excludes a child claim during discovery and publication.
        # Sibling roots share the ancestor locks and can register concurrently.
        for directory in sorted(directories):
            try:
                descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            except PermissionError:
                if directory in roots:
                    raise
                # A search-only ancestor can still expose named ownership
                # records. The atomic publish-and-recheck handles this case.
                continue
            locks.callback(os.close, descriptor)
            fcntl.flock(descriptor, fcntl.LOCK_EX if directory in roots else fcntl.LOCK_SH)
        for marker in sorted(claims):
            locks.enter_context(_binding_lock(marker))
        _check_root_claims(claims)
        yield


def _claim_roots(claims: dict[Path, dict[str, str | None]]) -> None:
    created: dict[Path, dict[str, str | None]] = {}
    try:
        for marker, claim in claims.items():
            if not marker.exists() and _write_binding(marker, claim, exclusive=True):
                created[marker] = claim
        # Some shared mounts only enforce flock within each host. Publish
        # atomically, then look both ways before allowing runtime initialization.
        _check_root_claims(claims)
        _check_descendant_claims(created)
    except Exception:
        for marker in created:
            marker.unlink(missing_ok=True)
        raise


def _check_unused_roots(claims: Mapping[Path, object]) -> None:
    for marker in claims:
        if marker.parent.exists() and any(
            item not in {marker, marker.with_suffix(".lock")} for item in marker.parent.iterdir()
        ):
            raise ValueError(_NEW_WORKSPACE)


def ensure_v2_storage(settings: StudioSettings, *, ledger_path: Path | None = None) -> None:
    database, uri = sqlite_database_path(settings.database_url)
    if database is None or uri:
        raise ValueError("StateWeaver requires a separate on-disk SQLite database without a URI")

    ledger = (ledger_path or settings.provider_ledger_path).resolve()
    binding = _storage_binding(settings, database, ledger)
    marker = _marker_path(database)
    claims = _root_claims(settings, database)

    # Reject previous workspaces and foreign ownership before making even a
    # lock file. Repeat under the locks for concurrent initializers.
    _check_database_binding(database, binding)
    _check_root_claims(claims)
    if not marker.exists():
        _check_unused_roots(claims)
    with _binding_lock(marker):
        identity = _check_database_binding(database, binding)
        bound = marker.is_file()
        with _lock_roots(claims):
            if not bound:
                _check_unused_roots(claims)
            _claim_roots(claims)
            if identity is None:
                identity = StorageIdentity(uuid4().hex, False, provider_config_hash(binding))
                _write_binding(_identity_path(database), asdict(identity))
            if not bound:
                _write_binding(marker, binding)


def _original_receipt(coordinator: ProviderCoordinator, receipt_id: str) -> dict[str, object]:
    if not coordinator.is_remote and not coordinator.path.is_file():
        raise ValueError("StateWeaver provider ledger is missing original receipts")
    try:
        return coordinator.get(receipt_id)
    except ProviderContractError as error:
        raise ValueError("StateWeaver provider ledger is missing an original receipt") from error


def _receipt_scope(
    coordinator: ProviderCoordinator,
    receipt_id: str,
    *,
    project: str,
    job: str | None = None,
) -> dict[str, object]:
    receipt = _original_receipt(coordinator, receipt_id)
    context = receipt.get("context")
    if (
        not isinstance(context, dict)
        or context.get("project_id") != project
        or job is not None
        and context.get("job_id") != job
    ):
        raise ValueError("StateWeaver provider ledger receipt belongs to a different project/job")
    return receipt


def _verify_recoverable_receipts(
    database: Path, settings: StudioSettings, coordinator: ProviderCoordinator
) -> None:
    if database.is_file():
        with closing(_read_only(database)) as connection:
            if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='studio_entities'"
            ).fetchone():
                rows = connection.execute(
                    """WITH bound_jobs AS (
                           SELECT project_id, id, status,
                             coalesce(json_extract(payload, '$.request.api_receipt_id'),
                                      json_extract(payload, '$.result.api_receipt_id')) AS receipt,
                             json_extract(payload, '$.provider_job_id') AS provider_id
                           FROM studio_entities WHERE kind='jobs'
                       )
                       SELECT project_id, id, receipt, provider_id FROM bound_jobs
                       WHERE receipt IS NOT NULL
                         AND status IN ('queued','submitting','running','ambiguous')"""
                ).fetchall()
            else:
                rows = []
        for project_id, job_id, receipt_id, provider_id in rows:
            receipt = _receipt_scope(coordinator, receipt_id, project=project_id, job=job_id)
            if provider_id and receipt.get("provider_job_id") not in {None, provider_id}:
                raise ValueError("StateWeaver provider ledger has a different accepted job ID")

    # Unpublished native outcomes live in these per-workspace sidecars. Leave
    # malformed sidecar diagnostics to the existing receipt recovery code.
    for path in (settings.work_root / "native-provider-receipts").glob("*.json"):
        try:
            stored = json.loads(path.read_text())["receipt"]
            receipt_id = stored["id"]
            project_id = stored["context"]["project_id"]
        except (KeyError, TypeError, ValueError):
            continue
        _receipt_scope(coordinator, receipt_id, project=project_id)

    # Text-only work can already have a committed response with no media jobs
    # or unflushed sidecars. One saved response attests the same ledger even
    # when its original file path was recreated after a bad restore.
    for path in (settings.work_root / "native-provider-receipts" / "role-results").glob("*.json"):
        filename = re.fullmatch(r"([a-f0-9]{32})-[1-9][0-9]*", path.stem)
        if filename is None:
            continue
        try:
            stored = json.loads(path.read_text())
            request_hash = stored["request_hash"]
            response_id = stored["response"]["id"]
        except (KeyError, TypeError, ValueError):
            continue
        receipt = _original_receipt(coordinator, filename[1])
        if receipt.get("request_hash") != request_hash or receipt.get("provider_job_id") not in {
            None,
            response_id,
        }:
            raise ValueError("StateWeaver provider ledger has a different saved model response")
        break


def _verify_pending_roles(
    database: Path, settings: StudioSettings, coordinator: ProviderCoordinator
) -> None:
    with closing(_read_only(database)) as connection:
        if not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='studio_entities'"
        ).fetchone():
            return
        rows = connection.execute(
            """SELECT actor.project_id, actor.id, json_extract(session.value, '$.role_journal')
               FROM studio_entities actor, json_each(actor.payload, '$.sessions') session
               WHERE actor.kind='actors'
                 AND json_extract(session.value, '$.role_journal.awaiting_model')"""
        ).fetchall()
    workspace = str(settings.work_root.resolve())
    checked_assets: set[str] = set()
    for project_id, actor_id, value in rows:
        journal = json.loads(value)
        verify_pending_role_assets(settings.work_root, journal, checked_assets)
        if journal.get("receipt_protocol") != 1:
            raise ValueError("StateWeaver pending role uses an unsupported receipt protocol")
        attempts = journal.get("attempt_receipts", {})
        if not attempts:
            continue
        if not (coordinator.is_remote or coordinator.path.is_file()):
            raise ValueError("StateWeaver pending role has no original receipt to reconcile")
        try:
            for saved in attempts.values():
                original_role_receipt(coordinator, saved, workspace, project_id, actor_id)
        except (AttributeError, KeyError, TypeError, ProviderContractError) as error:
            raise ValueError("StateWeaver original role receipt is missing or changed") from error


def prepare_v2_runtime(settings: StudioSettings, coordinator: ProviderCoordinator) -> None:
    """Initialize the bound release database and verify receipt recovery."""
    database, _ = sqlite_database_path(settings.database_url)
    assert database is not None  # Already checked by ensure_v2_storage.
    marker = _marker_path(database)
    binding = _storage_binding(settings, database, coordinator.path.resolve())
    if _check_database_binding(database, binding) is None or not marker.is_file():
        raise ValueError(_RESTORE_BINDING)
    with _binding_lock(marker):
        identity = _check_database_binding(database, binding)
        if identity is None or not marker.is_file():
            raise ValueError(_RESTORE_BINDING)
        if database.is_file() and database.stat().st_size:
            _verify_recoverable_receipts(database, settings, coordinator)
            # A deferred outcome is already durable. Replay it against its original
            # row before comparing the ledger to the pending role checkpoints.
            NativeReceipts(
                coordinator, root=settings.work_root / "native-provider-receipts"
            ).recover()
            _verify_pending_roles(database, settings, coordinator)
        if identity.initialized:
            return
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            application_id = connection.execute("PRAGMA application_id").fetchone()[0]
            if application_id == 0:
                if connection.execute("SELECT 1 FROM sqlite_schema LIMIT 1").fetchone():
                    raise ValueError(_NEW_WORKSPACE)
                connection.execute(f"PRAGMA application_id={_RELEASE_APPLICATION_ID}")
                connection.execute(
                    f"CREATE VIEW {_IDENTITY_VIEW} AS SELECT '{identity.storage_id}' AS storage_id"
                )
            elif (
                application_id != _RELEASE_APPLICATION_ID
                or _database_storage_id(connection) != identity.storage_id
            ):
                raise ValueError(_RESTORE_DATABASE)
        _write_binding(
            _identity_path(database),
            asdict(StorageIdentity(identity.storage_id, True, identity.binding_hash)),
        )
