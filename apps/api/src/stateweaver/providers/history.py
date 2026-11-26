"""Point lookups into immutable provider history; runtime capacity stays in the live ledger."""

from __future__ import annotations

import json
import sqlite3
import time
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from threading import local
from typing import Any, cast

_connections = local()


def _open_archive(path: str) -> sqlite3.Connection:
    uri = Path(path).resolve().as_uri()
    connection = sqlite3.connect(f"{uri}?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def archive_connection(path: str) -> sqlite3.Connection:
    connections = getattr(_connections, "archives", None)
    if connections is None:
        connections = _connections.archives = OrderedDict()
    if path not in connections:
        connections[path] = _open_archive(path)
        while len(connections) > 16:
            _, retired = connections.popitem(last=False)
            retired.close()
    connections.move_to_end(path)
    return cast(sqlite3.Connection, connections[path])


def archived_receipt(
    live: sqlite3.Connection, value: str, *, identity: bool = False
) -> sqlite3.Row | None:
    field = "identity" if identity else "id"
    location = live.execute(
        f"SELECT h.id,s.path AS archive_path FROM api_archive_index h "
        f"JOIN api_archive_sources s ON h.archive_source=s.id WHERE h.{field}=?",
        (value,),
    ).fetchone()
    if location is None:
        return None
    return cast(
        sqlite3.Row | None,
        archive_connection(location["archive_path"])
        .execute("SELECT * FROM api_receipts WHERE id=?", (location["id"],))
        .fetchone(),
    )


def receipt_record(
    live: sqlite3.Connection, value: str, *, identity: bool = False, restore: bool = False
) -> sqlite3.Row | None:
    field = "identity" if identity else "id"
    row = cast(
        sqlite3.Row | None,
        live.execute(f"SELECT * FROM api_receipts WHERE {field}=?", (value,)).fetchone(),
    )
    if row is not None:
        return row
    row = archived_receipt(live, value, identity=identity)
    if row is not None and restore:
        return restore_receipt_row(live, row)
    return row


def restore_receipt_row(live: sqlite3.Connection, row: sqlite3.Row | None) -> sqlite3.Row | None:
    """Restore an already prefetched immutable row, without archive I/O under the writer.

    Callers read receipt_record(..., restore=False) before their write transaction.
    A concurrent hot update wins over the older prefetched record.
    """
    if row is None:
        return None
    current = cast(
        sqlite3.Row | None,
        live.execute("SELECT * FROM api_receipts WHERE id=?", (row["id"],)).fetchone(),
    )
    if current is not None:
        return current
    columns = row.keys()
    live.execute(
        "INSERT OR IGNORE INTO api_receipts ("
        + ",".join(columns)
        + ") VALUES ("
        + ",".join("?" for _ in columns)
        + ")",
        tuple(row),
    )
    return cast(
        sqlite3.Row | None,
        live.execute("SELECT * FROM api_receipts WHERE id=?", (row["id"],)).fetchone(),
    )


def archived_owned_receipts(
    live: sqlite3.Connection, provider_id: str, *, workspace: str, project_id: str, actor_id: str
) -> list[sqlite3.Row]:
    locations = live.execute(
        "SELECT h.id,s.path AS archive_path FROM api_archive_index h "
        "JOIN api_archive_sources s ON h.archive_source=s.id WHERE h.provider_job_id=?",
        (provider_id,),
    ).fetchall()
    result = []
    for item in locations:
        row = (
            archive_connection(item["archive_path"])
            .execute("SELECT * FROM api_receipts WHERE id=?", (item["id"],))
            .fetchone()
        )
        if row is not None:
            context = json.loads(row["context"])
            if (context.get("workspace"), context.get("project_id"), context.get("actor_id")) == (
                workspace,
                project_id,
                actor_id,
            ):
                result.append(row)
    return result


def archived_events(live: sqlite3.Connection, identifier: str) -> list[dict[str, Any]]:
    location = live.execute(
        "SELECT s.path FROM api_archive_index h JOIN api_archive_sources s "
        "ON h.archive_source=s.id WHERE h.id=?",
        (identifier,),
    ).fetchone()
    if location is None:
        return []
    return [
        dict(row)
        for row in archive_connection(location[0]).execute(
            "SELECT * FROM api_events WHERE receipt_id=? ORDER BY sequence", (identifier,)
        )
    ]


def prepare_archive_summary(connection: sqlite3.Connection) -> None:
    connection.executescript(
        "CREATE TABLE IF NOT EXISTS api_archive_receipt_summary ("
        "id TEXT PRIMARY KEY,workspace TEXT,project_id TEXT,actor_id TEXT,"
        "task_id TEXT,operation TEXT,model TEXT NOT NULL,request_hash TEXT NOT NULL,"
        "state TEXT NOT NULL,execution_generation INTEGER,updated REAL NOT NULL,error TEXT);"
        "CREATE INDEX IF NOT EXISTS ix_api_archive_actor ON api_archive_receipt_summary("
        "project_id,actor_id,id);"
    )


ARCHIVE_SUMMARY_UPSERT = (
    "INSERT OR REPLACE INTO api_archive_receipt_summary "
    "(id,workspace,project_id,actor_id,task_id,operation,model,request_hash,"
    "state,execution_generation,updated,error) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)"
)


def archive_summary(row: sqlite3.Row) -> tuple[Any, ...]:
    context = json.loads(row["context"])
    error = json.loads(row["result"]).get("error")
    return (
        row["id"],
        *(
            context.get(key)
            for key in ("workspace", "project_id", "actor_id", "task_id", "operation")
        ),
        row["model"],
        row["request_hash"],
        row["state"],
        row["execution_generation"],
        row["updated"],
        json.dumps(error) if error else None,
    )


def compact_history(
    live_path: Path,
    archive_path: Path,
    *,
    batch_size: int = 500,
    include_expired_reservations: bool = False,
) -> dict[str, int]:
    """Archive unchanged records; snapshot reads and comparisons never hold the writer.

    Unknown/accepted physical requests stay hot. Expired unsubmitted reservations may
    optionally move cold, preserving their original state and exact identity lookup.
    """
    from .coordinator_client import coordinator

    ledger = coordinator(live_path, direct=True)
    ledger.prepare()
    moved = events = 0
    cursor = 0
    frozen = archive_connection(str(archive_path.resolve()))
    event_cutoff = frozen.execute("SELECT MAX(sequence) FROM api_events").fetchone()[0] or 0
    with sqlite3.connect(live_path, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        prepare_archive_summary(connection)
        connection.execute(
            "INSERT OR IGNORE INTO api_archive_sources(path) VALUES(?)",
            (str(archive_path.resolve()),),
        )
        source = connection.execute(
            "SELECT id FROM api_archive_sources WHERE path=?", (str(archive_path.resolve()),)
        ).fetchone()[0]
        connection.commit()
        while True:
            now = time.time()
            # All reads from shared storage, JSON comparisons and candidate preparation
            # finish before BEGIN IMMEDIATE. CAS below protects concurrent outcomes.
            rows = connection.execute(
                "SELECT rowid AS archive_rowid,* FROM api_receipts WHERE rowid>? "
                "AND (state IN ('complete','failed','cancelled','operator_abandoned_unknown') "
                "OR (? AND state='reserved' AND lease_until<=?)) ORDER BY rowid LIMIT ?",
                (cursor, include_expired_reservations, now, batch_size),
            ).fetchall()
            if not rows:
                break
            cursor = rows[-1]["archive_rowid"]
            candidates = []
            for row in rows:
                saved = frozen.execute(
                    "SELECT * FROM api_receipts WHERE id=?", (row["id"],)
                ).fetchone()
                saved_columns = saved.keys() if saved is not None else []
                if saved is not None and all(row[key] == saved[key] for key in saved_columns):
                    candidates.append((row, archive_summary(row)))
            if not candidates:
                continue
            connection.execute("BEGIN IMMEDIATE")
            try:
                for row, summary in candidates:
                    count = connection.execute(
                        "DELETE FROM api_receipts WHERE id=? AND state=? AND generation=? "
                        "AND updated=? AND owner IS ? AND lease_until=?",
                        (
                            row["id"],
                            row["state"],
                            row["generation"],
                            row["updated"],
                            row["owner"],
                            row["lease_until"],
                        ),
                    ).rowcount
                    if not count:
                        continue
                    connection.execute(
                        "INSERT OR REPLACE INTO api_archive_index VALUES(?,?,?,?)",
                        (row["id"], row["identity"], row["provider_job_id"], source),
                    )
                    connection.execute(
                        ARCHIVE_SUMMARY_UPSERT,
                        summary,
                    )
                    events += connection.execute(
                        "DELETE FROM api_events WHERE receipt_id=? AND sequence<=?",
                        (row["id"], event_cutoff),
                    ).rowcount
                    moved += 1
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        # Frozen service usage aggregation is a read, outside its short writer.
        usage = list(
            frozen.execute("SELECT service_id,MAX(created) FROM api_receipts GROUP BY service_id")
        )
        connection.executemany(
            "INSERT INTO api_service_usage VALUES(?,?) ON CONFLICT(service_id) "
            "DO UPDATE SET last_created=MAX(last_created,excluded.last_created)",
            usage,
        )
        connection.commit()
    return {"archived_receipts": moved, "archived_events": events}


def archived_export(live: sqlite3.Connection) -> Iterator[sqlite3.Row]:
    for source in live.execute("SELECT id,path FROM api_archive_sources").fetchall():
        with closing(_open_archive(source["path"])) as archive:
            cursor = archive.execute("SELECT * FROM api_receipts ORDER BY created,id")
            while rows := cursor.fetchmany(200):
                identifiers = [row["id"] for row in rows]
                slots = ",".join("?" for _ in identifiers)
                archived_ids = {
                    r[0]
                    for r in live.execute(
                        "SELECT id FROM api_archive_index WHERE archive_source=? "
                        f"AND id IN ({slots})",
                        [source["id"], *identifiers],
                    )
                }
                hot_ids = {
                    r[0]
                    for r in live.execute(
                        f"SELECT id FROM api_receipts WHERE id IN ({slots})",
                        identifiers,
                    )
                }
                for row in rows:
                    if row["id"] in archived_ids and row["id"] not in hot_ids:
                        yield row
