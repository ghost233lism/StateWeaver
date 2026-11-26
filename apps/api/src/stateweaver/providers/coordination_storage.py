"""Provider coordination with recoverable snapshots on shared storage."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import time
from contextlib import closing
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Any
from uuid import uuid4


def _json_file(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(path.name + f".{os.getpid()}.tmp")
    with pending.open("w") as output:
        json.dump(value, output, sort_keys=True)
        output.flush()
        os.fsync(output.fileno())
    pending.replace(path)


def _open_read_only(path: Path, *, timeout: float = 5.0) -> sqlite3.Connection:
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=timeout)


def _valid_ledger(path: Path, *, quick: bool = True) -> bool:
    try:
        with closing(_open_read_only(path)) as connection:
            tables = {
                r[0]
                for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            return {
                "api_receipts",
                "api_events",
                "api_archive_sources",
                "api_archive_index",
            } <= tables and (
                not quick or connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
            )
    except sqlite3.DatabaseError:
        return False


def freeze_stage(
    source: Path, stage_root: Path, *, validate: bool = True, durable: bool = True
) -> tuple[Path, dict[str, Any]]:
    """Freeze once on local disk; the source read transaction ends before validation."""
    source = source.resolve()
    stage_root.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(
        prefix=".coordinator-snapshot-", suffix=".db", dir=stage_root
    )
    os.close(descriptor)
    stage = Path(name)
    started = time.time()
    try:
        with closing(_open_read_only(source, timeout=1)) as reader:
            reader.execute("PRAGMA query_only=ON")
            reader.execute("BEGIN")
            reader.execute("SELECT MAX(sequence) FROM api_events").fetchone()
            with closing(sqlite3.connect(stage, timeout=30)) as target:
                target.execute("PRAGMA journal_mode=DELETE")
                target.execute("PRAGMA synchronous=" + ("FULL" if durable else "OFF"))
                reader.backup(target, pages=256, sleep=0.01)
                target.execute("PRAGMA journal_mode=DELETE")
                target.commit()
                sequence = target.execute("SELECT MAX(sequence) FROM api_events").fetchone()[0] or 0
            reader.rollback()
        backed_up_at = time.time()
        valid = _valid_ledger(stage) if validate else _valid_ledger(stage, quick=False)
        if not valid:
            raise sqlite3.DatabaseError("Provider coordination snapshot is incomplete")
        completed_at = time.time()
        return stage, {
            "started_at": started,
            "event_sequence": sequence,
            "local_backup_seconds": backed_up_at - started,
            "local_validation_seconds": completed_at - backed_up_at,
            "local_snapshot_seconds": completed_at - started,
            "bytes": stage.stat().st_size,
        }
    except BaseException:
        stage.unlink(missing_ok=True)
        raise
    finally:
        for suffix in ("-wal", "-shm", "-journal"):
            stage.with_name(stage.name + suffix).unlink(missing_ok=True)


def snapshot(source: Path, destination: Path) -> dict[str, Any]:
    """Freeze and verify locally, then publish one sequential atomic copy."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    source, destination = source.resolve(), destination.resolve()
    stage, result = freeze_stage(source, Path(tempfile.gettempdir()))
    pending = destination.with_name(destination.name + f".{uuid4().hex}.pending")
    staged_at = time.time()
    try:
        with stage.open("rb") as input_file, pending.open("wb") as output_file:
            shutil.copyfileobj(input_file, output_file, length=1024 * 1024)
            output_file.flush()
            os.fsync(output_file.fileno())
        pending.replace(destination)
        completed_at = time.time()
        return {
            **result,
            "completed_at": completed_at,
            "duration_seconds": completed_at - result["started_at"],
            "local_snapshot_seconds": staged_at - result["started_at"],
            "destination_publish_seconds": completed_at - staged_at,
        }
    finally:
        pending.unlink(missing_ok=True)
        stage.unlink(missing_ok=True)


def restore_history_segments(local_path: Path, archive_root: Path) -> int:
    """Recover permanent segments committed after the last five-second checkpoint."""
    from .history import (
        ARCHIVE_SUMMARY_UPSERT,
        archive_connection,
        archive_summary,
        prepare_archive_summary,
    )

    with closing(sqlite3.connect(local_path)) as live:
        live.row_factory = sqlite3.Row
        prepare_archive_summary(live)
        known = {r[0] for r in live.execute("SELECT path FROM api_archive_sources")}
        recovered = 0
        for path in sorted(archive_root.glob("archive-*.db")):
            if str(path) in known:
                continue
            saved = archive_connection(str(path))
            live.execute("INSERT OR IGNORE INTO api_archive_sources(path) VALUES(?)", (str(path),))
            source = live.execute(
                "SELECT id FROM api_archive_sources WHERE path=?", (str(path),)
            ).fetchone()[0]
            for row in saved.execute("SELECT * FROM api_receipts"):
                hot = live.execute(
                    "SELECT generation,updated FROM api_receipts WHERE id=?", (row["id"],)
                ).fetchone()
                previous = live.execute(
                    "SELECT updated FROM api_archive_receipt_summary WHERE id=?", (row["id"],)
                ).fetchone()
                if hot and (hot["generation"], hot["updated"]) > (
                    row["generation"],
                    row["updated"],
                ):
                    continue
                if previous and previous["updated"] > row["updated"]:
                    continue
                live.execute(
                    "INSERT OR REPLACE INTO api_archive_index VALUES(?,?,?,?)",
                    (row["id"], row["identity"], row["provider_job_id"], source),
                )
                live.execute(ARCHIVE_SUMMARY_UPSERT, archive_summary(row))
                live.execute("DELETE FROM api_receipts WHERE id=?", (row["id"],))
                if row["state"] not in {
                    "complete",
                    "failed",
                    "cancelled",
                    "operator_abandoned_unknown",
                }:
                    columns = row.keys()
                    live.execute(
                        "INSERT INTO api_receipts ("
                        + ",".join(columns)
                        + ") VALUES ("
                        + ",".join("?" for _ in columns)
                        + ")",
                        tuple(row),
                    )
                recovered += 1
            high_water = saved.execute(
                "SELECT seq FROM sqlite_sequence WHERE name='api_events'"
            ).fetchone()
            if high_water:
                changed = live.execute(
                    "UPDATE sqlite_sequence SET seq=MAX(seq,?) WHERE name='api_events'",
                    (high_water[0],),
                ).rowcount
                if not changed:
                    live.execute(
                        "INSERT INTO sqlite_sequence VALUES('api_events',?)", (high_water[0],)
                    )
            usage = list(saved.execute("SELECT service_id,last_created FROM api_service_usage"))
            live.executemany(
                "INSERT INTO api_service_usage VALUES(?,?) ON CONFLICT(service_id) "
                "DO UPDATE SET last_created=MAX(last_created,excluded.last_created)",
                usage,
            )
            live.commit()
        return recovered


def prepare_hot_storage(
    canonical_path: Path, local_path: Path, archive_path: Path
) -> dict[str, Any]:
    """Build a local hot ledger during a drained cutover; never changes canonical state.

    The immutable original snapshot is permanent. The operator publishes the client
    marker only after the single existing API has loaded this local ledger.
    """
    if local_path.exists() or archive_path.exists():
        raise FileExistsError("Hot ledger or immutable snapshot already exists")
    archive = snapshot(canonical_path, archive_path)
    from .history import archived_events

    # A restored hot row can already have older cold events. Make the new
    # permanent snapshot cumulative before it becomes an immutable source.
    with closing(_open_read_only(canonical_path)) as original:
        original.row_factory = sqlite3.Row
        restored = original.execute(
            "SELECT r.id FROM api_receipts r JOIN api_archive_index h ON h.id=r.id"
        ).fetchall()
        with closing(sqlite3.connect(archive_path)) as frozen:
            for row in restored:
                for event in archived_events(original, row["id"]):
                    keys = list(event)
                    frozen.execute(
                        "INSERT OR IGNORE INTO api_events ("
                        + ",".join(keys)
                        + ") VALUES ("
                        + ",".join("?" for _ in keys)
                        + ")",
                        [event[key] for key in keys],
                    )
            frozen.commit()
    archive_path.chmod(0o440)
    snapshot(archive_path, local_path)
    from .history import compact_history

    moved = compact_history(local_path, archive_path, include_expired_reservations=True)
    with closing(sqlite3.connect(local_path)) as connection:
        source = connection.execute(
            "SELECT id FROM api_archive_sources WHERE path=?", (str(archive_path.resolve()),)
        ).fetchone()[0]
        connection.execute(
            "INSERT OR REPLACE INTO api_archive_index "
            "SELECT id,identity,provider_job_id,? FROM api_receipts",
            (source,),
        )
        archived_active_events = connection.execute("DELETE FROM api_events").rowcount
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.execute("VACUUM")
        states = dict(connection.execute("SELECT state,COUNT(*) FROM api_receipts GROUP BY state"))
        integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
    if integrity != "ok":
        raise sqlite3.DatabaseError("Local provider ledger failed quick_check")
    return {
        "canonical_path": str(canonical_path),
        "local_path": str(local_path),
        "immutable_archive": str(archive_path),
        "archive": archive,
        "compaction": {**moved, "archived_active_events": archived_active_events},
        "hot_states": states,
        "hot_bytes": local_path.stat().st_size,
        "integrity": integrity,
    }


def archive_settled(
    local_path: Path, archive_root: Path, *, batch_size: int = 1000
) -> dict[str, Any]:
    """Keep settled payloads and their cumulative events in immutable cold segments."""
    from .history import archived_events, compact_history

    with closing(_open_read_only(local_path)) as reader:
        reader.row_factory = sqlite3.Row
        rows = reader.execute(
            "SELECT id FROM api_receipts WHERE state IN "
            "('complete','failed','cancelled','operator_abandoned_unknown') LIMIT ?",
            (batch_size,),
        ).fetchall()
        if not rows:
            return {"archived_receipts": 0, "archived_events": 0}
        identifiers = [row["id"] for row in rows]
        past_events = {
            identifier: archived_events(reader, identifier) for identifier in identifiers
        }
    temporary = local_path.with_name(f"archive-{uuid4().hex}.db")
    archive_root.mkdir(parents=True, exist_ok=True)
    archive_path = archive_root / temporary.name
    try:
        snapshot(local_path, temporary)
        with closing(sqlite3.connect(temporary)) as connection:
            connection.execute("CREATE TEMP TABLE keep_receipts(id TEXT PRIMARY KEY)")
            connection.executemany(
                "INSERT INTO keep_receipts VALUES(?)", [(i,) for i in identifiers]
            )
            connection.execute(
                "DELETE FROM api_receipts WHERE id NOT IN (SELECT id FROM keep_receipts)"
            )
            connection.execute(
                "DELETE FROM api_events WHERE receipt_id NOT IN (SELECT id FROM keep_receipts)"
            )
            for events in past_events.values():
                for event in events:
                    keys = list(event)
                    connection.execute(
                        "INSERT OR IGNORE INTO api_events ("
                        + ",".join(keys)
                        + ") VALUES ("
                        + ",".join("?" for _ in keys)
                        + ")",
                        [event[key] for key in keys],
                    )
            connection.commit()
            connection.execute("VACUUM")
        snapshot(temporary, archive_path)
        archive_path.chmod(0o440)
        moved = compact_history(local_path, archive_path, batch_size=100)
        return {**moved, "archive_path": str(archive_path)}
    finally:
        temporary.unlink(missing_ok=True)


class CoordinatorBackup:
    """One bounded backup thread inside the owner API, with a five-second cadence.

    Failed access to shared storage does not stall local submissions. No second scheduler, queue
    or process is started; snapshots never overlap or accumulate behind the writer.
    """

    def __init__(self, local_path: Path, recovery_path: Path, *, interval: float = 5.0) -> None:
        self.local_path, self.recovery_path = local_path, recovery_path
        self.interval = interval
        self._stop = Event()
        self._lock = Lock()
        self._thread: Thread | None = None
        self._archive_thread: Thread | None = None
        self._status: dict[str, Any] = {"state": "not_started", "interval_seconds": interval}
        self.status_path = recovery_path.with_suffix(".status.json")
        from .incremental_recovery import IncrementalRecovery

        self._incremental = IncrementalRecovery(recovery_path, valid=_valid_ledger)

    def status(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._status)

    def backup_once(self) -> dict[str, Any]:
        started = time.time()
        stage, frozen = freeze_stage(
            self.local_path, Path(tempfile.gettempdir()), validate=False, durable=False
        )
        try:
            staged_at = time.time()
            published = self._incremental.publish(stage, frozen["event_sequence"])
        except BaseException:
            if self._incremental.previous != stage:
                stage.unlink(missing_ok=True)
            raise
        completed_at = time.time()
        value = {
            "state": "ready",
            "interval_seconds": self.interval,
            "started_at": started,
            "completed_at": completed_at,
            "duration_seconds": completed_at - started,
            "local_snapshot_seconds": staged_at - started,
            "local_backup_seconds": frozen["local_backup_seconds"],
            "local_validation_seconds": frozen["local_validation_seconds"],
            "destination_publish_seconds": completed_at - staged_at,
            "bytes": stage.stat().st_size,
            **published,
        }
        _json_file(self.status_path, value)
        with self._lock:
            self._status = value
        return value

    def restore_if_missing(self) -> None:
        if not _valid_ledger(self.local_path):
            if not self.recovery_path.is_file():
                raise FileNotFoundError("No coordination database or recovery snapshot exists")
            if self.local_path.exists():
                quarantine = self.local_path.with_name(
                    self.local_path.name + f".invalid-{uuid4().hex}"
                )
                self.local_path.replace(quarantine)
                for suffix in ("-wal", "-shm"):
                    auxiliary = self.local_path.with_name(self.local_path.name + suffix)
                    if auxiliary.exists():
                        auxiliary.replace(quarantine.with_name(quarantine.name + suffix))
            self._incremental.restore(self.local_path, _valid_ledger)
        restore_history_segments(self.local_path, self.recovery_path.parent / "history")

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("Provider backup is already started")
        self.restore_if_missing()
        self._thread = Thread(target=self._run, name="provider-ledger-backup", daemon=True)
        self._thread.start()
        self._archive_thread = Thread(
            target=self._archive_loop, name="provider-cold-history", daemon=True
        )
        self._archive_thread.start()

    def _run(self) -> None:
        with closing(_open_read_only(self.local_path, timeout=1)) as reader:
            previous_version = None
            while not self._stop.is_set():
                started = time.monotonic()
                try:
                    version = reader.execute("PRAGMA data_version").fetchone()[0]
                    if version != previous_version:
                        self.backup_once()
                        previous_version = version
                    else:
                        with self._lock:
                            self._status = {**self._status, "checked_at": time.time()}
                except Exception as error:
                    with self._lock:
                        self._status = {
                            **self._status,
                            "state": "backup_unavailable",
                            "last_attempt_at": time.time(),
                            "error_type": type(error).__name__,
                        }
                self._stop.wait(max(0.0, self.interval - (time.monotonic() - started)))

    def _archive_loop(self) -> None:
        while not self._stop.wait(300):
            try:
                result = archive_settled(self.local_path, self.recovery_path.parent / "history")
                with self._lock:
                    self._status = {
                        **self._status,
                        "last_archive": result,
                        "archived_at": time.time(),
                    }
            except Exception as error:
                with self._lock:
                    self._status = {**self._status, "archive_error_type": type(error).__name__}

    def close(self) -> None:
        self._stop.set()
        for thread in (self._archive_thread, self._thread):
            if thread is not None:
                thread.join(timeout=30)
                if thread.is_alive():
                    raise TimeoutError("Provider backup/archive has not finished")
        try:
            self.backup_once()
        finally:
            self._incremental.close()
