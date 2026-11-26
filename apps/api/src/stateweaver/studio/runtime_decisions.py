"""Cheap actor polling with version-validated, read-only workflow decisions."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .context_cache import core_fingerprint, dependencies, dependencies_current, track_reads
from .contracts import Actor, StudioProject
from .harness import task_effort
from .memory import digest
from .review_sessions import desired_session
from .store import StudioStore


@dataclass(frozen=True)
class ActorDecision:
    signature: str
    core: str
    dependencies: list[dict[str, Any]]
    adaptive: bool
    session_key: str
    effort: str | None
    waiting: bool
    source_actor_controls: dict[str, bool]
    job_query_outages: dict[str, bool]


def source_actor_can_progress(project: StudioProject, actor_id: str) -> bool:
    """Track only a source director's usable control state, never its conversation."""
    actor = project.actors.get(actor_id)
    active = bool(actor and not actor.paused and actor.status in {"ready", "running", "waiting"})
    if project.__dict__.get("_track_reads"):
        project.__dict__.setdefault("_runtime_source_actor_controls", {})[actor_id] = active
    return active


def job_query_outages_current(project: StudioProject, previous: ActorDecision) -> bool:
    """Only outage start/end changes waiting; poll counters never rewrite a draft."""
    if not previous.job_query_outages:
        return True
    query = project.__dict__.get("_query_job_outages")
    current = (
        query(list(previous.job_query_outages))
        if query
        else {
            identifier: bool(job.query_outage_since)
            for identifier in previous.job_query_outages
            if (job := project.jobs.get(identifier)) is not None
        }
    )
    return current == previous.job_query_outages


def observed_job_query_outages(project: StudioProject) -> dict[str, bool]:
    repositories = project.__dict__.get("_repositories")
    jobs = repositories["jobs"] if repositories else project.jobs
    return {identifier: bool(job.query_outage_since) for identifier, job in dict.items(jobs)}


def read_actor_decision(
    store: StudioStore,
    project_id: str,
    actor_id: str,
    wait_reason: Callable[[StudioProject, Actor], bool],
    previous: ActorDecision | None = None,
) -> tuple[Actor, ActorDecision]:
    """Always read current control/lease state; reuse only verified derived work.

    Revision alone is insufficient: recovery and job claims can change relevant
    entities without advancing it. The existing dependency tracker validates both
    individual versions and query membership, including an initially empty query.
    Nothing here authorizes generation or writes a model's context-read receipt.
    """

    def read(project: StudioProject) -> tuple[Actor, ActorDecision]:
        actor = project.actors[actor_id]
        session = actor.sessions.get(actor.session_key)
        signature = digest(
            [
                project.status,
                project.deleted_at,
                project.issues,
                actor.id,
                actor.role,
                actor.set_id,
                actor.status,
                actor.paused,
                actor.context_epoch,
                actor.session_key,
                project.planning_context_policy,
                actor.planning_context_started_at,
                session.purpose if session else None,
                actor.recovery_attempts,
                actor.no_progress_count,
                actor.wake_requested,
                actor.wake_version,
                *([actor.runtime_wait.model_dump(mode="json") if actor.runtime_wait else None]),
            ]
        )
        if (
            previous is not None
            and signature == previous.signature
            and all(
                source_actor_can_progress(project, identifier) == active
                for identifier, active in previous.source_actor_controls.items()
            )
            and dependencies_current(project, previous.dependencies, previous.core)
            and job_query_outages_current(project, previous)
        ):
            return actor, previous
        track_reads(project)
        adaptive = True
        stopped = actor.paused or actor.status in {"blocked", "complete"}
        session_key = (
            desired_session(project, actor).key if adaptive and not stopped else actor.session_key
        )
        effort = task_effort(project, actor) if not stopped else None
        waiting = actor.status == "waiting" and not stopped and wait_reason(project, actor)
        return actor, ActorDecision(
            signature=signature,
            core=core_fingerprint(project),
            dependencies=dependencies(project),
            adaptive=adaptive,
            session_key=session_key,
            effort=effort,
            waiting=waiting,
            source_actor_controls=dict(project.__dict__.get("_runtime_source_actor_controls", {})),
            job_query_outages=observed_job_query_outages(project),
        )

    return store.read(project_id, read)
