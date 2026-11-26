"""Numeric production observations, independent of creative entities/checkpoints."""

from __future__ import annotations

import hashlib
import logging
import time
from collections import Counter
from collections.abc import Callable, Sequence
from functools import wraps
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from sqlalchemy import func, or_, select

from stateweaver.persistence.models import StudioEntityRow, StudioRuntimeTimingRow

if TYPE_CHECKING:
    from .contracts import Actor, RequiredContextPage
    from .runtime_metrics import RuntimeMetrics
    from .store import StudioStore

LIVE = ("queued", "submitting", "running", "ambiguous")


def queue_epoch(actor: Actor) -> str:
    """A scheduling episode, without editing an actor or inspecting its prompt."""
    return hashlib.sha256(
        repr((actor.lease_generation, actor.status_since, actor.recovery_not_before)).encode()
    ).hexdigest()


def measured_context_read[**P](
    operation: Callable[P, RequiredContextPage],
) -> Callable[P, RequiredContextPage]:
    @wraps(operation)
    def measured(*args: P.args, **kwargs: P.kwargs) -> RequiredContextPage:
        from .runtime_metrics import DATABASE_TIME

        store = cast("StudioStore", args[0] if args else kwargs["store"])
        project_id = str(args[1] if len(args) > 1 else kwargs["project_id"])
        actor_id = str(args[2] if len(args) > 2 else kwargs["actor_id"])
        started, clock = time.time(), time.monotonic()
        counter = {"seconds": 0.0, "calls": 0.0}
        outer = DATABASE_TIME.get()
        token = DATABASE_TIME.set(counter)
        result = None
        error_type = None
        try:
            result = operation(*args, **kwargs)
            return result
        except Exception as error:
            error_type = type(error).__name__
            raise
        finally:
            DATABASE_TIME.reset(token)
            if outer is not None:
                for key in counter:
                    outer[key] += counter[key]
            details: dict[str, Any] = {
                "database_seconds": counter["seconds"],
                "database_calls": counter["calls"],
                "error_type": error_type,
            }
            if result is not None:
                details.update(
                    context_id=result.context_id,
                    snapshot_id=result.snapshot_id,
                    fingerprint=result.fingerprint,
                    page=result.page,
                    page_count=result.page_count,
                    mode=result.mode,
                    read_reason=result.read_reason,
                    served_bytes=len(result.content.encode()),
                    full_payload_bytes=result.full_payload_bytes if result.page == 0 else 0,
                    unchanged_delta=(
                        result.mode == "delta" and result.baseline_fingerprint == result.fingerprint
                    ),
                )
            store.metrics.record(
                project_id,
                "context_read",
                "required_context",
                started,
                time.monotonic() - clock,
                actor_id=actor_id,
                success=result is not None,
                details=details,
            )

    return measured


class ProductionPerformance:
    def __init__(self, metrics: RuntimeMetrics) -> None:
        self.metrics = metrics

    def backlog(self, project_id: str) -> dict[str, Any]:
        """Count indexed live statuses; do not hydrate completed history bodies."""
        entity = StudioEntityRow
        filters = {
            "actors": ("ready", "running", "waiting", "blocked", "paused"),
            "jobs": LIVE,
            "candidates": ("review",),
            "references": ("pending", "candidate"),
        }
        result: dict[str, Any] = {}
        with self.metrics.database.session() as session:
            for kind, statuses in filters.items():
                statement = select(entity.subtype, entity.status, func.count()).where(
                    entity.project_id == project_id,
                    entity.kind == kind,
                    entity.status.in_(statuses),
                )
                if kind == "candidates":
                    statement = statement.where(
                        entity.payload["review"].as_string().is_(None),
                        entity.payload["disposition"]["kind"].as_string().is_(None),
                    )
                result[kind] = {
                    f"{subtype or kind}:{status}": int(count)
                    for subtype, status, count in session.execute(
                        statement.group_by(entity.subtype, entity.status)
                    )
                }
        return result

    @staticmethod
    def _pending_id(project_id: str, actor_id: str) -> str:
        return hashlib.sha256(f"actor-queue:{project_id}:{actor_id}".encode()).hexdigest()

    @staticmethod
    def _close(session: Any, pending: StudioRuntimeTimingRow, stamp: float, outcome: str) -> None:
        session.add(
            StudioRuntimeTimingRow(
                id=uuid4().hex,
                project_id=pending.project_id,
                actor_id=pending.actor_id,
                phase="actor_queue",
                operation=pending.operation,
                started_at=pending.started_at,
                ended_at=stamp,
                seconds=max(0, stamp - pending.started_at),
                success=outcome == "claimed",
                details={**pending.details, "outcome": outcome},
            )
        )
        session.delete(pending)

    def observe_queue(
        self, project_id: str, observations: Sequence[tuple[str, str, str, bool]], stamp: float
    ) -> None:
        """First observed eligibility, rather than creation/provider-wait time.

        Pending pointers are telemetry rows, so restarts retain the observation
        without adding fields to actors or changing a creative context version.
        Optional statistics must not delay dispatch behind a native business write.
        """
        if not observations:
            return
        lock = self.metrics.database.write_lock
        if not lock.acquire(blocking=False):
            return
        try:
            with self.metrics.database.session() as session:
                for actor_id, role, epoch, eligible in observations:
                    identifier = self._pending_id(project_id, actor_id)
                    pending = session.get(StudioRuntimeTimingRow, identifier)
                    if pending and (not eligible or pending.details.get("epoch") != epoch):
                        self._close(session, pending, pending.ended_at, "eligibility_changed")
                        session.flush()
                        pending = None
                    if not eligible:
                        continue
                    if pending is None:
                        session.add(
                            StudioRuntimeTimingRow(
                                id=identifier,
                                project_id=project_id,
                                actor_id=actor_id,
                                phase="actor_queue_pending",
                                operation=role,
                                started_at=stamp,
                                ended_at=stamp,
                                seconds=0,
                                success=False,
                                details={"epoch": epoch, "outcome": "pending"},
                            )
                        )
                    else:
                        pending.ended_at = stamp
        except Exception as error:
            logging.getLogger(__name__).warning(
                "Queue observation failed type=%s", type(error).__name__
            )
        finally:
            lock.release()

    def claimed(self, project_id: str, actor_id: str, epoch: str, stamp: float) -> None:
        try:
            with self.metrics.database.write_lock, self.metrics.database.session() as session:
                pending = session.get(
                    StudioRuntimeTimingRow, self._pending_id(project_id, actor_id)
                )
                if pending is not None:
                    matches = pending.details.get("epoch") == epoch
                    self._close(
                        session,
                        pending,
                        stamp if matches else pending.ended_at,
                        "claimed" if matches else "eligibility_changed",
                    )
        except Exception as error:
            logging.getLogger(__name__).warning(
                "Queue claim metric failed type=%s", type(error).__name__
            )

    def completed(
        self, project_id: str, actor_id: str, operation: str, entity_id: str, stamp: float
    ) -> None:
        """Record the first successful completion; idempotent replays add no throughput."""
        identifier = hashlib.sha256(
            f"production-progress:{project_id}:{operation}:{entity_id}".encode()
        ).hexdigest()
        try:
            with self.metrics.database.write_lock, self.metrics.database.session() as session:
                if session.get(StudioRuntimeTimingRow, identifier) is None:
                    session.add(
                        StudioRuntimeTimingRow(
                            id=identifier,
                            project_id=project_id,
                            actor_id=actor_id,
                            phase="production_progress",
                            operation=operation,
                            started_at=stamp,
                            ended_at=stamp,
                            seconds=0,
                            success=True,
                            details={"deduplication": "first_entity_completion"},
                        )
                    )
        except Exception as error:
            logging.getLogger(__name__).warning(
                "Completion metric failed type=%s", type(error).__name__
            )

    def snapshot(
        self, project_id: str, *, stamp: float | None = None, window_seconds: float = 3600
    ) -> dict[str, Any]:
        """Read-only, bounded distributions plus exact counts in a fixed wall window."""
        from .runtime_metrics import percentile

        stamp = time.time() if stamp is None else stamp
        if not 0 < window_seconds <= 86400:
            raise ValueError("Performance window must be between 0 and 86400 seconds.")
        row = StudioRuntimeTimingRow
        with self.metrics.database.session() as session:
            counts = {
                (phase, operation, success): int(count)
                for phase, operation, success, count in session.execute(
                    select(row.phase, row.operation, row.success, func.count())
                    .where(
                        row.project_id == project_id,
                        row.ended_at >= stamp - window_seconds,
                        row.ended_at <= stamp,
                        row.phase.in_(["context_read", "actor_queue", "production_progress"]),
                    )
                    .group_by(row.phase, row.operation, row.success)
                )
            }
            samples = {}
            for phase in ("context_read", "actor_queue"):
                samples[phase] = list(
                    session.scalars(
                        select(row)
                        .where(
                            row.project_id == project_id,
                            row.phase == phase,
                            row.ended_at >= stamp - window_seconds,
                            row.ended_at <= stamp,
                        )
                        .order_by(row.ended_at.desc(), row.id)
                        .limit(512)
                    )
                )
            pending = list(
                session.scalars(
                    select(row)
                    .join(
                        StudioEntityRow,
                        (StudioEntityRow.project_id == row.project_id)
                        & (StudioEntityRow.kind == "actors")
                        & (StudioEntityRow.id == row.actor_id),
                    )
                    .where(
                        row.project_id == project_id,
                        row.phase == "actor_queue_pending",
                        StudioEntityRow.paused.is_(False),
                        StudioEntityRow.status.in_(["ready", "waiting", "running"]),
                        or_(
                            StudioEntityRow.lease_owner.is_(None),
                            StudioEntityRow.lease_until <= stamp,
                        ),
                    )
                )
            )
        reads = samples["context_read"]
        identities = Counter(
            (r.actor_id, r.details.get("snapshot_id"), r.details.get("page"))
            for r in reads
            if r.success
        )
        queues = [r.seconds for r in samples["actor_queue"] if r.success]
        throughput = {
            operation: {
                "completed_operations": counts.get(("production_progress", operation, True), 0),
                "operations_per_hour": counts.get(("production_progress", operation, True), 0)
                * 3600
                / window_seconds,
            }
            for operation in ("review_candidate", "finish_set", "finish_project")
        }
        return {
            "as_of": stamp,
            "window_seconds": window_seconds,
            "distribution_sample_limit": 512,
            "backlog": self.backlog(project_id),
            "context_reads": {
                "calls": sum(v for (phase, _, _), v in counts.items() if phase == "context_read"),
                "failed_calls": counts.get(("context_read", "required_context", False), 0),
                "sample_calls": len(reads),
                "sample_database_calls": sum(r.details.get("database_calls", 0) for r in reads),
                "sample_database_seconds": sum(r.details.get("database_seconds", 0) for r in reads),
                "sample_served_bytes": sum(r.details.get("served_bytes", 0) for r in reads),
                "sample_unchanged_delta_calls": sum(
                    bool(r.details.get("unchanged_delta")) for r in reads if r.success
                ),
                "sample_duplicate_snapshot_pages": sum(max(0, v - 1) for v in identities.values()),
                "sample_p50_seconds": percentile([r.seconds for r in reads], 0.5),
            },
            "actor_queue": {
                "sample_claims": len(queues),
                "p50_seconds": percentile(queues, 0.5),
                "p95_seconds": percentile(queues, 0.95) if len(queues) >= 20 else None,
                "pending": [
                    {
                        "actor_id": r.actor_id,
                        "role": r.operation,
                        "first_observed_at": r.started_at,
                        "last_observed_at": r.ended_at,
                        "observed_age_seconds": max(0, stamp - r.started_at),
                    }
                    for r in pending
                ],
            },
            "throughput": throughput,
            "definitions": {
                "queue": (
                    "First observed eligible until lease claim; unobserved time is not inferred."
                ),
                "duplicates": (
                    "Repeated successful requests for the same actor/snapshot/page in the sample."
                ),
                "throughput": (
                    "First entity completions divided by the fixed trailing wall window; "
                    "idempotent replays add nothing, and this is not video quality."
                ),
                "history": (
                    "Uninstrumented historical work is unavailable; never inferred as measured."
                ),
            },
        }
