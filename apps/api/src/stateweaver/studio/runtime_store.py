"""Indexed scheduling, lease renewal and provider capacity readback."""

from __future__ import annotations

import time
from collections.abc import Iterable, Sequence
from typing import Any

from sqlalchemy import ColumnElement, func, or_, select, text, tuple_
from sqlalchemy.orm import aliased

from stateweaver.persistence.models import (
    StudioBindingRow,
    StudioEntityRow,
    StudioProjectRow,
)
from stateweaver.studio.contracts import Candidate, Job, StudioProject
from stateweaver.studio.repository import EntityMap, select_entities
from stateweaver.studio.store import StudioError, StudioStore

Identity = tuple[str, str]


class RuntimeLeaseLost(StudioError):
    """Unpublished text is recovered from native history by the next lease owner."""


def _video_occupies_pool() -> ColumnElement[bool]:
    task = StudioEntityRow.payload["provider_job_id"].as_string()
    completed = StudioEntityRow.payload["result"]["video_remote_completion_id"].as_string()
    return or_(
        StudioEntityRow.status != "running", task.is_(None), completed.is_(None), completed != task
    )


class RuntimeStore:
    def __init__(self, store: StudioStore) -> None:
        self.store = store
        self._last_waiting_scan = float("-inf")

    def scheduled(self, stamp: float) -> dict[str, dict[str, list[str]]]:
        """Only due runtime candidates cross this query, never histories."""
        result: dict[str, dict[str, list[str]]] = {}
        # Explicit wakes remain immediate; legacy dependency waits retain a
        # low-rate fallback so a missed historical wake cannot strand a Set.
        include_waiting = stamp - self._last_waiting_scan >= 15 or stamp < self._last_waiting_scan
        if include_waiting:
            self._last_waiting_scan = stamp
        with self.store.database.session() as session:
            available = or_(
                StudioEntityRow.lease_owner.is_(None), StudioEntityRow.lease_until <= stamp
            )
            actors = session.execute(
                select(
                    StudioEntityRow.project_id,
                    StudioEntityRow.id,
                    StudioEntityRow.status,
                    StudioEntityRow.due_at,
                )
                .where(
                    StudioEntityRow.kind == "actors",
                    StudioEntityRow.status.in_(["ready", "running", "waiting"]),
                    StudioEntityRow.paused.is_(False),
                    StudioEntityRow.due_at <= stamp,
                    available,
                )
                .order_by(StudioEntityRow.project_id, StudioEntityRow.id)
            ).all()
            # Only current candidates may need the old JSON timer or a wake flag.
            # An idle/paused batch does not open any actor or history payload.
            needed = [
                (project_id, actor_id)
                for project_id, actor_id, status, due_at in actors
                if not due_at or status == "waiting" and not include_waiting
            ]
            metadata = (
                {
                    (project_id, actor_id): (wake, recovery)
                    for project_id, actor_id, wake, recovery in session.execute(
                        select(
                            StudioEntityRow.project_id,
                            StudioEntityRow.id,
                            StudioEntityRow.payload["wake_requested"].as_boolean(),
                            StudioEntityRow.payload["recovery_not_before"].as_float(),
                        ).where(
                            StudioEntityRow.kind == "actors",
                            tuple_(StudioEntityRow.project_id, StudioEntityRow.id).in_(needed),
                        )
                    )
                }
                if needed
                else {}
            )
            for project_id, actor_id, status, _due_at in actors:
                wake, recovery = metadata.get((project_id, actor_id), (False, 0))
                if recovery and recovery > stamp:
                    continue
                if status == "waiting" and not include_waiting and not wake:
                    continue
                result.setdefault(project_id, {"actors": [], "jobs": []})["actors"].append(actor_id)
            job_actor = aliased(StudioEntityRow)
            unpaused_actor = (
                select(job_actor.id)
                .where(
                    job_actor.project_id == StudioEntityRow.project_id,
                    job_actor.kind == "actors",
                    job_actor.id == StudioEntityRow.actor_id,
                    job_actor.paused.is_(False),
                )
                .exists()
            )
            jobs = session.execute(
                select(StudioEntityRow.project_id, StudioEntityRow.id)
                .where(
                    StudioEntityRow.kind == "jobs",
                    StudioEntityRow.status.in_(["queued", "running"]),
                    StudioEntityRow.due_at <= stamp,
                    or_(StudioEntityRow.status == "running", unpaused_actor),
                    available,
                )
                .order_by(StudioEntityRow.project_id, StudioEntityRow.id)
            )
            for project_id, job_id in jobs:
                result.setdefault(project_id, {"actors": [], "jobs": []})["jobs"].append(job_id)
        return result

    def provider_inflight(self) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        with self.store.database.session() as session:
            for project_id, job_id in session.execute(
                select(StudioEntityRow.project_id, StudioEntityRow.id).where(
                    StudioEntityRow.kind == "jobs",
                    StudioEntityRow.subtype.in_(["image", "video"]),
                    StudioEntityRow.status.in_(["submitting", "running", "ambiguous"]),
                )
            ):
                result.setdefault(project_id, []).append(job_id)
        return result

    def record_video_remote_completion(self, project_id: str, job: Job) -> None:
        """Keep the accepted Job collectible without reserving a local GPU slot."""
        from .job_receipts import request_hash

        def record(project: StudioProject) -> None:
            current = project.jobs[job.id]
            if (
                current.kind != "video"
                or current.status != "running"
                or not job.provider_job_id
                or current.provider_job_id != job.provider_job_id
                or current.lease_owner != job.lease_owner
                or current.lease_generation != job.lease_generation
                or request_hash(current) != request_hash(job)
            ):
                raise RuntimeLeaseLost("Original H3 collection lease or request changed")
            current.result["video_remote_completion_id"] = job.provider_job_id

        self.store.mutate(project_id, record, kind="video_remote_complete", advance_revision=False)

    def expired(self, stamp: float) -> dict[str, dict[str, list[str]]]:
        result: dict[str, dict[str, list[str]]] = {}
        with self.store.database.session() as session:
            rows = session.execute(
                select(StudioEntityRow.project_id, StudioEntityRow.kind, StudioEntityRow.id).where(
                    StudioEntityRow.kind.in_(["actors", "jobs"]),
                    StudioEntityRow.lease_owner.is_not(None),
                    StudioEntityRow.lease_until <= stamp,
                )
            )
            for project_id, kind, entity_id in rows:
                result.setdefault(project_id, {"actors": [], "jobs": []})[kind].append(entity_id)
        return result

    def video_inflight(self, pending_tasks: Iterable[Identity]) -> int:
        return self._media_inflight("video", pending_tasks)

    def image_inflight(self, pending_tasks: Iterable[Identity]) -> int:
        return self._media_inflight("image", pending_tasks)

    def analysis_inflight(self, pending_tasks: Iterable[Identity]) -> int:
        return self._media_inflight("analysis", pending_tasks)

    def image_cooldown_until(self) -> float:
        """Compatibility: one failed image must not cool unrelated models/jobs."""
        return 0.0

    def _media_inflight(self, kind: str, pending_tasks: Iterable[Identity]) -> int:
        with self.store.database.session() as session:
            query = (
                select(func.count())
                .select_from(StudioEntityRow)
                .where(
                    StudioEntityRow.kind == "jobs",
                    StudioEntityRow.subtype == kind,
                    # SQLite otherwise uses only the kind prefix for the OR
                    # below and reads every historical job to test its lease.
                    StudioEntityRow.status.in_(["submitting", "running", "queued"]),
                    or_(
                        StudioEntityRow.status.in_(["submitting", "running"]),
                        (StudioEntityRow.status == "queued")
                        & StudioEntityRow.lease_owner.is_not(None)
                        & (StudioEntityRow.lease_until > time.time()),
                    ),
                )
            )
            if kind == "video":
                query = query.where(_video_occupies_pool())
            count = session.scalar(query) or 0
            # Pending local claims are not yet counted as persisted submissions.
            for project_id, job_id in pending_tasks:
                count += int(
                    session.scalar(
                        select(StudioEntityRow.id).where(
                            StudioEntityRow.project_id == project_id,
                            StudioEntityRow.kind == "jobs",
                            StudioEntityRow.id == job_id,
                            StudioEntityRow.subtype == kind,
                            StudioEntityRow.status == "queued",
                            or_(
                                StudioEntityRow.lease_owner.is_(None),
                                StudioEntityRow.lease_until <= time.time(),
                            ),
                        )
                    )
                    is not None
                )
            return count

    def video_reservations(
        self, stamp: float, *, require_registered_actor: bool = False
    ) -> list[dict[str, Any]]:
        """Read physical video bindings and receipt identities without loading job recipes."""
        payload = StudioEntityRow.payload
        query = select(
            StudioEntityRow.project_id,
            StudioEntityRow.id,
            payload["request"]["provider_route"].label("request_route"),
            payload["result"]["provider_route"].label("result_route"),
            payload["request"]["api_receipt_id"].as_string().label("request_receipt"),
            payload["result"]["api_receipt_id"].as_string().label("result_receipt"),
            payload["provider_job_id"].as_string().label("provider_job_id"),
        )
        leased = StudioEntityRow.lease_owner.is_not(None) & (StudioEntityRow.lease_until > stamp)
        if require_registered_actor:
            # The submission check uses the same project/actor scope as routing.
            # The scheduler's advisory scan also sees unresolved reservations.
            actor = aliased(StudioEntityRow)
            query = query.join(
                actor,
                (actor.project_id == StudioEntityRow.project_id)
                & (actor.kind == "actors")
                & (actor.id == StudioEntityRow.actor_id),
            ).join(StudioProjectRow, StudioProjectRow.id == StudioEntityRow.project_id)
            leased &= StudioEntityRow.lease_owner != ""
        with self.store.database.session() as session:
            rows = session.execute(
                query.where(
                    StudioEntityRow.kind == "jobs",
                    StudioEntityRow.subtype == "video",
                    _video_occupies_pool(),
                    StudioEntityRow.status.in_(["submitting", "running", "ambiguous", "queued"]),
                    or_(
                        StudioEntityRow.status.in_(["submitting", "running", "ambiguous"]),
                        (StudioEntityRow.status == "queued") & leased,
                    ),
                )
            )
            return [
                {
                    "project_id": row.project_id,
                    "id": row.id,
                    "route": row.request_route or row.result_route or {},
                    "api_receipt_id": row.request_receipt or row.result_receipt,
                    "provider_job_id": row.provider_job_id,
                }
                for row in rows
            ]

    def video_routes_inflight(self) -> dict[str, int]:
        """Read only live video reservations; histories and media bodies are excluded."""
        route = func.coalesce(
            StudioEntityRow.payload["request"]["provider_route"]["id"].as_string(),
            StudioEntityRow.payload["result"]["provider_route"]["id"].as_string(),
        )
        with self.store.database.session() as session:
            rows = session.execute(
                select(route, func.count())
                .where(
                    StudioEntityRow.kind == "jobs",
                    StudioEntityRow.subtype == "video",
                    _video_occupies_pool(),
                    StudioEntityRow.status.in_(["submitting", "running", "ambiguous", "queued"]),
                    or_(
                        StudioEntityRow.status.in_(["submitting", "running", "ambiguous"]),
                        (StudioEntityRow.status == "queued")
                        & StudioEntityRow.lease_owner.is_not(None)
                        & (StudioEntityRow.lease_until > time.time()),
                    ),
                )
                .group_by(route)
            )
            return {str(identifier or "legacy"): int(count) for identifier, count in rows}

    def queued_video_project_ids(self) -> list[str]:
        """Fetch route pins only for projects with unpaused, queued video work."""
        with self.store.database.session() as session:
            actor = aliased(StudioEntityRow)
            return list(
                session.scalars(
                    select(StudioEntityRow.project_id)
                    .distinct()
                    .join(
                        actor,
                        (actor.project_id == StudioEntityRow.project_id)
                        & (actor.kind == "actors")
                        & (actor.id == StudioEntityRow.actor_id),
                    )
                    .join(StudioProjectRow, StudioProjectRow.id == StudioEntityRow.project_id)
                    .where(
                        StudioEntityRow.kind == "jobs",
                        StudioEntityRow.subtype == "video",
                        StudioEntityRow.status == "queued",
                        actor.paused.is_(False),
                        StudioProjectRow.summary["deleted_at"].as_string().is_(None),
                    )
                )
            )

    def active_sets(
        self, stamp: float, pending_tasks: Iterable[Identity], *, role: str = "set_director"
    ) -> set[Identity]:
        with self.store.database.session() as session:
            active = {
                (pid, aid)
                for pid, aid in session.execute(
                    select(StudioEntityRow.project_id, StudioEntityRow.id).where(
                        StudioEntityRow.kind == "actors",
                        StudioEntityRow.subtype == role,
                        StudioEntityRow.lease_owner.is_not(None),
                        StudioEntityRow.lease_until > stamp,
                    )
                )
            }
            for project_id, actor_id in pending_tasks:
                if session.scalar(
                    select(StudioEntityRow.id).where(
                        StudioEntityRow.project_id == project_id,
                        StudioEntityRow.kind == "actors",
                        StudioEntityRow.id == actor_id,
                        StudioEntityRow.subtype == role,
                    )
                ):
                    active.add((project_id, actor_id))
            return active

    def heartbeat(
        self,
        owner: str,
        actor_ids: Iterable[Identity],
        job_ids: Iterable[Identity],
        until: float,
        *,
        lease_seconds: float | None = None,
    ) -> None:
        actor_ids, job_ids = tuple(actor_ids), tuple(job_ids)
        if not actor_ids and not job_ids:
            return
        with self.store.database.write_lock, self.store.database.session() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            # Lock contention can outlast the deadline computed by the caller.
            # Start the finite renewal interval only after both writer locks
            # have been acquired; ownership still gates every individual row.
            if lease_seconds is not None:
                until = time.time() + lease_seconds
            session.execute(
                text(
                    "UPDATE studio_entities SET lease_until=:until "
                    "WHERE project_id=:project_id AND kind=:kind AND id=:entity_id "
                    "AND lease_owner=:owner"
                ),
                [
                    {
                        "until": until,
                        "project_id": project_id,
                        "kind": kind,
                        "entity_id": entity_id,
                        "owner": owner,
                    }
                    for kind, identities in (("actors", actor_ids), ("jobs", job_ids))
                    for project_id, entity_id in identities
                ],
            )

    def affected_candidates(self, project: StudioProject, revision_id: str) -> list[Candidate]:
        if not isinstance(project.candidates, EntityMap):
            return [
                c
                for c in project.candidates.values()
                if any(b.revision_id == revision_id for b in c.bindings)
            ]
        with self.store.database.session() as session:
            ids = set(
                session.scalars(
                    select(StudioBindingRow.candidate_id).where(
                        StudioBindingRow.project_id == project.id,
                        StudioBindingRow.revision_id == revision_id,
                    )
                )
            )
        # See transaction-local binding changes as well as committed index rows.
        ids.update(
            key
            for key, value in dict.items(project.candidates)
            if any(b.revision_id == revision_id for b in value.bindings)
        )
        return [
            c
            for c in select_entities(project.candidates, ids=list(ids))
            if any(b.revision_id == revision_id for b in c.bindings)
        ]

    def has_analysis(
        self, project: StudioProject, candidate_id: str, statuses: Sequence[str] | None = None
    ) -> bool:
        jobs = project.jobs
        loaded = dict.values(jobs) if isinstance(jobs, EntityMap) else jobs.values()
        if any(
            j.target_id == candidate_id
            and j.kind == "analysis"
            and (statuses is None or j.status in statuses)
            for j in loaded
        ):
            return True
        if not isinstance(jobs, EntityMap):
            return False
        # Loaded rows may have changed status in this transaction. Exclude them
        # from the committed index after checking their current values above.
        excluded = set(dict.keys(jobs)) | jobs.deleted
        stmt = select(StudioEntityRow.id).where(
            StudioEntityRow.project_id == project.id,
            StudioEntityRow.kind == "jobs",
            StudioEntityRow.parent_id == candidate_id,
            StudioEntityRow.subtype == "analysis",
        )
        if statuses is not None:
            stmt = stmt.where(StudioEntityRow.status.in_(statuses))
        if excluded:
            stmt = stmt.where(StudioEntityRow.id.not_in(excluded))
        with self.store.database.session() as session:
            return session.scalar(stmt.limit(1)) is not None
