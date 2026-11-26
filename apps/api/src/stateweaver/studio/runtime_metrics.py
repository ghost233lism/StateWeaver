"""Measured Harness work; concurrent/inclusive spans are never added as ETA."""

from __future__ import annotations

import asyncio
import logging
import math
import sqlite3
import time
from contextvars import ContextVar
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import event, func, select
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from stateweaver.persistence.database import Database
from stateweaver.persistence.models import StudioRuntimeTimingRow

DATABASE_TIME: ContextVar[dict[str, float] | None] = ContextVar(
    "studio_database_time", default=None
)


def union_seconds(spans: list[tuple[float, float]]) -> float:
    total, end = 0.0, 0.0
    for start, finish in sorted(spans):
        total += max(0, finish - max(start, end))
        end = max(end, finish)
    return total


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)]


def _before_sql(
    connection: Any, cursor: Any, statement: Any, parameters: Any, context: Any, executemany: Any
) -> None:
    context.studio_started = time.monotonic() if DATABASE_TIME.get() is not None else None


def _after_sql(
    connection: Any, cursor: Any, statement: Any, parameters: Any, context: Any, executemany: Any
) -> None:
    counter = DATABASE_TIME.get()
    if counter is not None and context.studio_started is not None:
        counter["seconds"] += time.monotonic() - context.studio_started
        counter["calls"] += 1


class RuntimeMetrics:
    def __init__(self, database: Database) -> None:
        from .production_performance import ProductionPerformance

        self.database = database
        self.performance = ProductionPerformance(self)
        if not event.contains(database.engine, "before_cursor_execute", _before_sql):
            event.listen(database.engine, "before_cursor_execute", _before_sql)
            event.listen(database.engine, "after_cursor_execute", _after_sql)

    def record(
        self,
        project_id: str | None,
        phase: str,
        operation: str,
        started: float,
        seconds: float,
        *,
        actor_id: str | None = None,
        success: bool = True,
        details: dict[str, Any] | None = None,
    ) -> None:
        # Optional telemetry must not queue behind production writer work.
        # RLock is reentrant: do not open a second SQLite writer from its owner.
        owned = getattr(self.database.write_lock, "_is_owned", None)
        if owned is not None and owned():
            return
        if not self.database.write_lock.acquire(blocking=False):
            return
        try:
            with self.database.session() as session:
                sqlite_connection = None
                previous_timeout = 0
                if self.database.engine.dialect.name == "sqlite":
                    sqlite_connection = cast(
                        sqlite3.Connection, session.connection().connection.driver_connection
                    )
                    previous = sqlite_connection.execute("PRAGMA busy_timeout").fetchone()
                    assert previous is not None
                    previous_timeout = int(previous[0])
                    sqlite_connection.execute("PRAGMA busy_timeout=0")
                try:
                    session.add(
                        StudioRuntimeTimingRow(
                            id=uuid4().hex,
                            project_id=project_id,
                            actor_id=actor_id,
                            phase=phase,
                            operation=operation[:128],
                            started_at=started,
                            ended_at=started + seconds,
                            seconds=max(0, seconds),
                            success=success,
                            details=details or {},
                        )
                    )
                    # Obtain the WAL writer without waiting for another process.
                    session.flush()
                finally:
                    if sqlite_connection is not None:
                        # Restore before returning this connection to the pool,
                        # including after a failed flush and transaction rollback.
                        sqlite_connection.execute(f"PRAGMA busy_timeout={previous_timeout}")
        except Exception as error:
            # Telemetry cannot roll back a completed business operation.
            logging.getLogger(__name__).warning("Timing write failed type=%s", type(error).__name__)
        finally:
            self.database.write_lock.release()

    def summary(self, project_id: str) -> list[dict[str, Any]]:
        row = StudioRuntimeTimingRow
        with self.database.session() as session:
            groups = session.execute(
                select(
                    row.phase,
                    func.count(),
                    func.sum(row.seconds),
                    func.min(row.started_at),
                    func.max(row.ended_at),
                )
                .where(row.project_id == project_id)
                .where(row.phase != "actor_queue_pending")
                .group_by(row.phase)
            ).all()
            result = []
            for phase, count, total, started, ended in groups:
                samples = list(
                    session.scalars(
                        select(row)
                        .where(
                            row.project_id == project_id,
                            row.phase == phase,
                        )
                        .order_by(row.ended_at.desc())
                        .limit(512)
                    )
                )
                values = [sample.seconds for sample in samples]
                successful = [sample.seconds for sample in samples if sample.success]
                result.append(
                    {
                        "phase": phase,
                        "count": count,
                        "total_seconds": float(total),
                        "sample_count": len(samples),
                        "started_at": started,
                        "ended_at": ended,
                        "p50_seconds": percentile(values, 0.5),
                        "p95_seconds": percentile(values, 0.95) if len(values) >= 20 else None,
                        "successful_sample_count": len(successful),
                        "successful_p50_seconds": percentile(successful, 0.5),
                        "sample_active_seconds": union_seconds(
                            [(sample.started_at, sample.ended_at) for sample in samples]
                        ),
                        "database_seconds": sum(
                            float(s.details.get("database_seconds", 0)) for s in samples
                        ),
                        "inclusive_of": ["mcp"]
                        if phase in {"director", "review", "planning"}
                        else ["director", "mcp"]
                        if phase == "material_delivery"
                        else ["context_read"]
                        if phase == "mcp"
                        else [],
                    }
                )
            return result


class HTTPMetrics:
    """Time JSON API work without buffering media, SSE or WebSockets."""

    def __init__(self, app: ASGIApp, metrics: RuntimeMetrics) -> None:
        self.app, self.metrics = app, metrics

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        unbound_project_path = path
        bound_prefix = "/api/v1/studio/v2/stores/"
        if path.startswith(bound_prefix):
            _, separator, rest = path.removeprefix(bound_prefix).partition("/")
            if separator:
                unbound_project_path = f"/api/v1/studio/v2/{rest}"
        if (
            scope["type"] != "http"
            or not any(
                unbound_project_path == prefix or unbound_project_path.startswith(prefix + "/")
                for prefix in ("/api/v1/studio/projects", "/api/v1/studio/v2/projects")
            )
            or any(part in path.split("/") for part in ("media", "events", "mcp"))
        ):
            await self.app(scope, receive, send)
            return
        started, monotonic = time.time(), time.monotonic()
        counter = {"seconds": 0.0, "calls": 0.0}
        token = DATABASE_TIME.set(counter)
        status = 500

        async def measured_send(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, measured_send)
        finally:
            seconds = time.monotonic() - monotonic
            DATABASE_TIME.reset(token)
            route = scope.get("route")
            # Route patterns avoid identifiers and query arguments in metrics.
            operation = getattr(route, "path", "unknown")
            operation = operation.replace(
                "/api/v1/studio/v2/stores/{binding}/", "/api/v1/studio/v2/", 1
            )
            if operation.startswith(("/projects", "/api/v1/studio/v2/projects")):
                operation = "/api/v1/studio" + operation.removeprefix("/api/v1/studio/v2")
            if status != 412 or not path.startswith(bound_prefix):
                await asyncio.to_thread(
                    self.metrics.record,
                    scope.get("path_params", {}).get("project_id"),
                    "http",
                    f"{scope.get('method', 'GET')} {operation}",
                    started,
                    seconds,
                    success=status < 400,
                    details={
                        "database_seconds": counter["seconds"],
                        "database_calls": counter["calls"],
                    },
                )
