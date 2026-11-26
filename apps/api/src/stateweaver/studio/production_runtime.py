"""Versioned production waits, separate from hash-validated creative checkpoints.

This module records scheduling dependencies; it never submits a provider request,
changes a creative decision, resumes a pause, or grants quality attempts.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, Literal

from .contracts import Actor, RuntimeWait, RuntimeWaitTarget, StudioProject
from .memory import digest, require
from .repository import select_entities

LIVE_JOBS = ("queued", "submitting", "running")


def enabled(project: StudioProject) -> bool:
    return True


def record_domain_blocker(
    project: StudioProject,
    actor: Actor,
    action: str,
    error: dict[str, Any],
    arguments: dict[str, Any],
) -> None:
    """Retain a small correction packet, never request bodies or context tokens."""
    # Status requests cannot waive review/selection prerequisites. Return their
    # rejection to the model without turning a correctable request into a blocker.
    if action == "set_status":
        return
    if not enabled(project) or error.get("code") != "domain_conflict":
        return
    details = error.get("details") or {}
    if not isinstance(details, dict):
        details = {}
    target = details.get("clip_id") or arguments.get("clip_id") or arguments.get("candidate_id")
    packet: dict[str, Any] = {
        "category": str(details.get("kind") or "domain_conflict"),
        "action": action,
        "entity_id": str(target) if target else None,
        "description": str(error.get("message", "Production validation failed"))[:1000],
        "next_action": str(error.get("recovery", "Correct the reported field"))[:1000],
        "progress_version": actor.progress_version,
        "details": {
            key: details[key]
            for key in (
                "clip_id",
                "candidate_id",
                "label",
                "field",
                "asset_id",
                "asset_kind",
                "state_id",
                "state_role",
                "received_roles",
                "expected_role",
            )
            if key in details
        },
    }
    delivery = details.get("delivery") or {}
    if isinstance(delivery, dict):
        issues = details.get("issues") or delivery.get("blocking_issues", [])
        packet["details"]["blocking_issues"] = issues[:8] if isinstance(issues, list) else []
    old = actor.production_blocker or {}
    repeated = all(
        old.get(key) == packet.get(key)
        for key in ("category", "action", "entity_id", "description", "progress_version")
    )
    packet["occurrences"] = int(old.get("occurrences", 0)) + 1 if repeated else 1
    actor.production_blocker = packet


def actor_blocks_critical_path(project: StudioProject, actor: Actor) -> bool:
    """Ignore closed/superseded child sessions and completed Set owners."""
    if actor.paused or actor.status != "blocked":
        return False
    if actor.role in {"task_analyst", "material_executor"}:
        task = project.delegate_tasks.get(actor.delegate_task_id or "")
        return bool(task and task.status in {"queued", "running"})
    if actor.role == "visual_analyst":
        visual_task = project.visual_tasks.get(actor.visual_task_id or "")
        return bool(visual_task and visual_task.status in {"queued", "running"})
    if actor.role == "screenwriter":
        return not bool(project.screenplay.confirmed_at)
    if actor.role == "set_director":
        production_set = project.sets.get(str(actor.set_id))
        if not production_set or production_set.status in {"complete", "failed"}:
            return False
        if not production_set.clip_plan_complete:
            return True  # A blocked owner must still create the first usable Clip plan.
        return any(
            not clip.archived_at and not clip.accepted_candidate_id and not clip.terminal_failure
            for clip in (
                project.clips[identifier]
                for identifier in production_set.clip_ids
                if identifier in project.clips
            )
        )
    return actor.role == "executive" and project.status not in {"complete", "failed"}


def critical_blocked_actors(project: StudioProject) -> list[Actor]:
    return [
        actor for actor in project.actors.values() if actor_blocks_critical_path(project, actor)
    ]


def native_retry_scheduled(
    project: StudioProject,
    actor: Actor,
    *,
    actor_eligible: Callable[[StudioProject, Actor, float], bool] | None = None,
    stamp: float | None = None,
) -> bool:
    """A current same-context retry can wait without spending a recovery attempt."""
    stamp = time.time() if stamp is None else stamp
    if (
        not enabled(project)
        or project.deleted_at
        or project.status in {"failed", "complete"}
        or actor.status != "ready"
        or actor.paused
        or actor.native_session_blocked
        or actor.turn_id
        or actor.lease_owner
        or actor.recovery_not_before <= stamp
        or not actor.recovery_history
        or actor_eligible is None
    ):
        return False
    marker = actor.recovery_history[-1]
    if (
        marker.get("reason_category")
        not in {
            "native_capacity_wait",
            "native_actor_lease_wait",
            "native_startup_wait",
            "native_memory_wait",
            "native_disk_wait",
        }
        or marker.get("retry_after") != actor.recovery_not_before
        or marker.get("previous_thread_id") != actor.thread_id
        or marker.get("progress_version") != actor.progress_version
        or marker.get("rebuild") is not False
        or marker.get("budget_consumed") is not False
    ):
        return False
    # Check current business eligibility at the recorded retry time. This only
    # observes the scheduled work; dispatch still checks the actual current time.
    return actor_eligible(project, actor, actor.recovery_not_before)


def independent_work_ready(
    project: StudioProject,
    *,
    actor_eligible: Callable[[StudioProject, Actor, float], bool] | None = None,
    stamp: float | None = None,
) -> bool:
    """Read native scheduling evidence; a blocked sibling is not a global stop.

    The running app supplies its actual supervisor eligibility function. Without
    it, only already submitted/queued jobs, owned turns and scheduled recovery
    can establish progress; a stale ``ready`` or ``waiting`` label cannot.
    """
    if not enabled(project) or project.deleted_at or project.status in {"failed", "complete"}:
        return False
    from .candidate_lifecycle import terminal_recovery

    stamp = time.time() if stamp is None else stamp
    for job in select_entities(project.jobs, statuses=list(LIVE_JOBS)):
        owner = project.actors.get(job.actor_id)
        if owner and not terminal_recovery(job) and (job.status != "queued" or not owner.paused):
            return True
    for actor in project.actors.values():
        if (
            actor.paused
            or actor.native_session_blocked
            or actor.status in {"blocked", "complete", "idle"}
        ):
            continue
        if (
            actor.status == "running"
            and actor.turn_id
            and actor.lease_owner
            and actor.lease_until > stamp
        ):
            return True
        if actor.recovery_attempts and actor.recovery_not_before > stamp:
            return True
        if native_retry_scheduled(project, actor, actor_eligible=actor_eligible, stamp=stamp):
            return True
        if actor_eligible is not None and actor_eligible(project, actor, stamp):
            return True
    return False


def _target_value(project: StudioProject, target: RuntimeWaitTarget) -> Any:
    collection: Any = {
        "job": project.jobs,
        "set": project.sets,
        "clip": project.clips,
        "candidate": project.candidates,
        "delegate_task": project.delegate_tasks,
        "visual_task": project.visual_tasks,
        "proposal": project.proposals,
        "reference": project.references,
    }[target.kind]
    return collection.get(target.id)


def _status(kind: str, value: Any) -> str:
    """Only meaningful status transitions wake; polling/leases do not."""
    if kind == "candidate":
        return digest(
            [
                value.status,
                value.media_id,
                bool(value.analysis),
                bool(value.review),
                bool(value.disposition),
            ]
        )
    if kind == "clip":
        episode = value.quality_episode
        return digest(
            [
                value.status,
                value.accepted_candidate_id,
                value.archived_at,
                bool(value.terminal_failure),
                value.state_readiness,
                episode.status if episode else None,
                episode.extra_granted if episode else None,
                episode.extra_used if episode else None,
            ]
        )
    return str(value.status)


def _pending(project: StudioProject, kind: str, value: Any) -> bool:
    if value is None:
        return False
    if kind == "job":
        from .candidate_lifecycle import terminal_recovery

        return value.status in LIVE_JOBS and not terminal_recovery(value)
    if kind == "set":
        return value.status in {"planned", "producing"}
    if kind == "clip":
        return (
            not value.archived_at
            and not value.accepted_candidate_id
            and not value.terminal_failure
            and value.status not in {"failed", "cancelled"}
        )
    if kind == "candidate":
        from .candidate_lifecycle import candidate_disposition_valid

        return (
            value.status in {"generating", "review"}
            and value.review is None
            and not candidate_disposition_valid(project, value)
        )
    if kind in {"delegate_task", "visual_task"}:
        return value.status in {"queued", "running"}
    if kind == "reference":
        return value.status in {"pending", "candidate"}
    return value.status in {"pending", "accepted", "in_progress"}


def _target(kind: Any, value: Any) -> RuntimeWaitTarget:
    return RuntimeWaitTarget(kind=kind, id=value.id, status=_status(kind, value))


def infer_wait_job_id(project: StudioProject, actor: Actor) -> str | None:
    """Find one existing owned live request without consuming any wake signal."""
    if not enabled(project):
        return None
    return next(
        (
            job.id
            for job in select_entities(project.jobs, actor_id=actor.id, statuses=list(LIVE_JOBS))
            if _pending(project, "job", job)
        ),
        None,
    )


def _inferred_targets(project: StudioProject, actor: Actor) -> list[RuntimeWaitTarget]:
    targets = [
        _target("job", job)
        for job in select_entities(project.jobs, actor_id=actor.id, statuses=list(LIVE_JOBS))
        if _pending(project, "job", job)
    ]
    if actor.role == "executive":
        from .delegation import progressing_tasks

        targets.extend(
            _target("delegate_task", task) for task in progressing_tasks(project, actor.id)
        )
        targets.extend(
            _target("visual_task", task)
            for task in select_entities(project.visual_tasks, statuses=["queued", "running"])
        )
        targets.extend(
            _target("set", item)
            for item in select_entities(project.sets, statuses=["planned", "producing"])
        )
        targets.extend(
            _target("candidate", candidate)
            for candidate in select_entities(project.candidates, statuses=["generating", "review"])
            if _pending(project, "candidate", candidate)
        )
    elif actor.role == "set_director" and actor.set_id in project.sets:
        production_set = project.sets[str(actor.set_id)]
        from .playback import set_status as playback_status

        if not playback_status(project, production_set).can_submit:
            targets.append(_target("set", production_set))
        targets.extend(
            _target("set", project.sets[identifier])
            for identifier in production_set.depends_on
            if identifier in project.sets and _pending(project, "set", project.sets[identifier])
        )
        from .proposals import clip_pending_proposals

        for clip in select_entities(project.clips, ids=production_set.clip_ids):
            if clip.archived_at or clip.terminal_failure:
                continue
            targets.extend(
                _target("candidate", candidate)
                for candidate in select_entities(
                    project.candidates, parent_id=clip.id, statuses=["generating", "review"]
                )
                if _pending(project, "candidate", candidate)
            )
            if clip.accepted_candidate_id:
                continue
            if clip.quality_episode and clip.quality_episode.status in {"escalated", "intervening"}:
                targets.append(_target("clip", clip))
            if clip.state_readiness == "needs_verification":
                targets.append(_target("clip", clip))
            # Only the current Clip's missing required material views justify
            # waiting. A finished job can leave a reference awaiting review.
            for plan in clip.asset_plan:
                state = project.states.get(plan.start_state_id)
                asset = project.assets.get(plan.asset_id)
                if not state or not asset:
                    continue
                needed = set(plan.required_views or asset.required_views) - set(
                    state.selected_revisions
                )
                if not needed:
                    continue
                targets.extend(
                    _target("reference", revision)
                    for revision in select_entities(
                        project.references, parent_id=state.id, statuses=["pending", "candidate"]
                    )
                    if revision.view in needed
                )
            targets.extend(
                _target("proposal", proposal) for proposal in clip_pending_proposals(project, clip)
            )
            # Observed handoffs wait for the exact source event's last Clip,
            # not for unrelated future Clips or an administrative proposal.
            from .set_handoffs import dependency_report, source_clip

            report = dependency_report(project, production_set.id, clip)
            for blocker in report.blockers:
                detail = blocker.details
                if (
                    blocker.kind != "asset_handoff"
                    or detail.get("mode") != "observed"
                    or detail.get("status") != "waiting_source"
                ):
                    continue
                source = source_clip(
                    project, detail.get("source_set_id"), detail.get("source_event_id")
                )
                if source and source.id != clip.id and _pending(project, "clip", source):
                    targets.append(_target("clip", source))

            # A Set's unsubmitted future Clips are not independent reasons to
            # wait forever; only a real earlier Clip can supply their endpoint.
            if clip.order:
                previous = project.clips.get(production_set.clip_ids[clip.order - 1])
                if previous and _pending(project, "clip", previous):
                    targets.append(_target("clip", previous))
            break
    elif actor.delegate_task_id:
        task = project.delegate_tasks.get(actor.delegate_task_id)
        if task:
            targets.extend(
                _target("delegate_task", project.delegate_tasks[identifier])
                for identifier in task.request.depends_on_task_ids
                if identifier in project.delegate_tasks
                and _pending(project, "delegate_task", project.delegate_tasks[identifier])
            )
    unique = {(target.kind, target.id): target for target in targets}
    return [unique[key] for key in sorted(unique)]


def register_wait(
    project: StudioProject, actor: Actor, *, job_id: str | None = None, reason: str = ""
) -> dict[str, Any] | None:
    """Bind an accepted wait in the same transaction as ``set_status``.

    The service reconciles unread notifications before this call. A missing
    optional job ID can be inferred from real owned jobs. An unbound production
    wait remains actionable and must not exempt repeated idle turns from limits.
    """
    if not enabled(project) or actor.role == "screenwriter":
        return None
    if actor.status != "waiting":
        actor.runtime_wait = None
        return None
    if actor.paused or actor.native_session_blocked or actor.wake_requested:
        actor.runtime_wait = None
        return {"waiting": False, "waiting_reason": "actor_control_changed"}
    if job_id:
        job = project.jobs.get(job_id)
        require(job and job.actor_id == actor.id, "A provider wait must name this actor's job.")
        assert job is not None
        targets = [_target("job", job)] if _pending(project, "job", job) else []
    else:
        targets = _inferred_targets(project, actor)
    if not targets:
        actor.runtime_wait = None
        actor.status = "ready"
        actor.wake_requested = True
        return {"waiting": False, "waiting_reason": "no_wait_dependency"}
    kind: Literal["provider", "dependency", "review"] = (
        "provider"
        if any(target.kind == "job" for target in targets)
        else "review"
        if any(target.kind == "candidate" for target in targets)
        else "dependency"
    )
    actor.runtime_wait = RuntimeWait(
        kind=kind,
        targets=targets,
        wake_version=actor.wake_version,
        registered_turn_id=actor.turn_id,
        reason=reason,
    )
    return wait_state(project, actor)


def wait_state(project: StudioProject, actor: Actor) -> dict[str, Any]:
    """Read-only diagnostic shared by scheduling and status projections."""
    wait = actor.runtime_wait
    if not enabled(project) or wait is None:
        return {"waiting": False, "waiting_reason": "unregistered"}
    base: dict[str, Any] = {
        "kind": wait.kind,
        "reason": wait.reason,
        "registered_at": wait.registered_at,
        "targets": [target.model_dump(mode="json") for target in wait.targets],
        "wake_conditions": ["target_state_changed", "new_notification", "control_changed"],
    }
    if (
        actor.status != "waiting"
        or actor.paused
        or actor.native_session_blocked
        or actor.wake_requested
        or actor.wake_version != wait.wake_version
    ):
        return {**base, "waiting": False, "waiting_reason": "actor_control_changed"}
    polls: list[float] = []
    for target in wait.targets:
        value = _target_value(project, target)
        if (
            not _pending(project, target.kind, value)
            or _status(target.kind, value) != target.status
        ):
            return {**base, "waiting": False, "waiting_reason": "target_state_changed"}
        if target.kind == "job" and value.next_poll_at:
            polls.append(value.next_poll_at)
    return {
        **base,
        "waiting": True,
        "waiting_reason": "registered_dependency",
        "next_check_at": min(polls) if polls else None,
    }


def registered_wait(project: StudioProject, actor: Actor) -> bool:
    return bool(wait_state(project, actor)["waiting"])
