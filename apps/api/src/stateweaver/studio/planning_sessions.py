"""Short planning contexts over durable Clip rounds, never a new quality ledger."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Literal

from .contracts import Actor, ActorSession, Clip, StudioProject, now
from .memory import digest, require
from .repository import select_entities

LIVE = {"queued", "submitting", "running", "ambiguous"}


def enabled(project: StudioProject, actor: Actor) -> bool:
    return actor.role == "set_director" and bool(actor.planning_context_started_at)


def start_at_boundary(project: StudioProject, actor: Actor) -> None:
    if actor.role != "set_director" or actor.planning_context_started_at:
        return
    production_set = project.sets[str(actor.set_id)]
    if actor.paused or project.status != "producing" or production_set.status == "complete":
        return
    # An existing generation/review must finish under its original organization.
    for clip in select_entities(project.clips, ids=production_set.clip_ids):
        if clip.archived_at or clip.accepted_candidate_id:
            continue
        if select_entities(
            project.candidates, parent_id=clip.id, statuses=["generating", "review"], limit=1
        ):
            return
        if select_entities(project.jobs, actor_id=actor.id, statuses=sorted(LIVE), limit=1):
            return
    actor.planning_context_started_at = now()


def round_key(project: StudioProject, clip: Clip) -> str:
    """Reviewed outcome advances the round; reservations/technical retries do not.

    A rejection opens the next repair-planning context before registration, so
    registering/editing the repair in that context cannot immediately replace it.
    """
    reviewed = [
        item
        for item in select_entities(project.candidates, parent_id=clip.id)
        if item.review is not None
    ]
    latest = max(reviewed, key=lambda item: (item.created_at, item.id)) if reviewed else None
    if latest and latest.review and latest.review.decision == "reject":
        return f"after:{latest.id}"
    if clip.pending_repair_id:
        return f"repair:{clip.pending_repair_id}"
    return "initial"


def unplanned_suffix_ready(project: StudioProject, actor: Actor) -> bool:
    """Missing story structure is independent of earlier material/video/review waits."""
    if not enabled(project, actor):
        return False
    from .production_contracts import missing_planned_events
    from .workflow import can_plan_set

    production_set = project.sets[str(actor.set_id)]
    return (
        bool(production_set.clip_ids)
        and not production_set.clip_plan_complete
        and can_plan_set(project, production_set)
        and bool(missing_planned_events(project, production_set))
    )


def covered_plan_work(
    project: StudioProject, actor: Actor
) -> Literal["confirmation", "duration"] | None:
    """Confirm covered plans independently of filming; pacing remains a real task."""
    if (
        not enabled(project, actor)
        or actor.paused
        or actor.native_session_blocked
        or project.deleted_at
        or project.status in {"complete", "failed"}
    ):
        return None
    from .playback import EPSILON, set_status
    from .production_contracts import missing_planned_events
    from .workflow import can_plan_set

    production_set = project.sets[str(actor.set_id)]
    if (
        production_set.status in {"complete", "failed"}
        or not production_set.clip_ids
        or production_set.clip_plan_complete
        or not can_plan_set(project, production_set)
        or missing_planned_events(project, production_set)
    ):
        return None
    budget = production_set.duration_budget
    if budget is None:
        return None  # The executive still owns the missing allocation.
    seconds = set_status(project, production_set).accounted_seconds
    if not budget.minimum_seconds - EPSILON <= seconds <= budget.maximum_seconds + EPSILON:
        return "duration"
    return "confirmation"


def desired_planning(project: StudioProject, actor: Actor) -> ActorSession:
    from .preparations import frontier, needs_lookahead

    production_set = project.sets[str(actor.set_id)]
    if not production_set.clip_ids:
        return ActorSession(key=f"plan:{production_set.id}:set", planning_kind="set")
    if unplanned_suffix_ready(project, actor):
        anchor = production_set.clip_ids[-1]
        return ActorSession(
            key=f"plan:{production_set.id}:suffix:{anchor}",
            planning_round="suffix",
            planning_kind="set",
        )
    completion = covered_plan_work(project, actor)
    if completion:
        return ActorSession(
            key=f"plan:{production_set.id}:{completion}",
            planning_round=completion,
            planning_kind="set",
        )
    clips = frontier(project, production_set.id)
    if needs_lookahead(project, actor):
        upcoming = clips[1]
        return ActorSession(
            key=f"draft:{upcoming.id}",
            planning_clip_id=upcoming.id,
            planning_round="draft",
            planning_kind="lookahead",
        )
    if clips:
        clip = clips[0]
        # Do not oscillate between an already saved draft and generation wait.
        # Candidate review has already been prioritized by desired_session.
        current = actor.sessions.get(actor.session_key)
        if clip.status in {"generating", "review"} and current and current.planning_kind:
            return current
        logical_round = round_key(project, clip)
        return ActorSession(
            key=f"plan:{clip.id}:{logical_round}",
            planning_clip_id=clip.id,
            planning_round=logical_round,
            planning_kind="clip",
        )
    if not production_set.clip_plan_complete:
        selected = [
            clip
            for clip in select_entities(project.clips, ids=production_set.clip_ids)
            if not clip.archived_at and clip.accepted_candidate_id
        ]
        last = max(selected, key=lambda clip: clip.order, default=None)
        anchor = last.id if last else "unplanned"
        return ActorSession(
            key=f"plan:{production_set.id}:suffix:{anchor}",
            planning_round="suffix",
            planning_kind="set",
        )
    return ActorSession(key=f"plan:{production_set.id}:finish", planning_kind="set")


def save_checkpoint(project: StudioProject, actor: Actor, session: ActorSession) -> None:
    """Persist exact business pointers before leaving a context, not chat facts."""
    from .adjudication import actual_end_state
    from .quality_progress import quality_progress

    clip_id = session.planning_clip_id
    if session.candidate_id:
        clip_id = project.candidates[session.candidate_id].clip_id
    if not clip_id:
        from .preparations import frontier

        pending = frontier(project, str(actor.set_id))
        clip_id = pending[0].id if pending else None
    checkpoint: dict[str, Any] = {
        "version": 1,
        "actor_id": actor.id,
        "set_id": actor.set_id,
        "session_key": session.key,
        "round": session.planning_round,
        "memory_revision": project.sets[str(actor.set_id)].memory.revision,
        "global_memory_revision": project.global_memory.revision,
        "recorded_at": now(),
        "authority": (
            "Business-record pointers; rejected footage is not a selected "
            "observation. Recheck current records on recovery."
        ),
    }
    if clip_id:
        clip = project.clips[clip_id]
        require(clip.set_id == actor.set_id, "规划检查点不能跨片场")
        plan = project.reference_plans.get(clip.reference_plan_id or "")
        candidates = select_entities(project.candidates, parent_id=clip.id)
        reviewed = [candidate for candidate in candidates if candidate.review]
        latest = max(reviewed, key=lambda item: (item.created_at, item.id)) if reviewed else None
        previous = (
            project.clips[project.sets[clip.set_id].clip_ids[clip.order - 1]]
            if clip.order
            else None
        )
        selected = (
            project.candidates.get(previous.accepted_candidate_id or "") if previous else None
        )
        end = actual_end_state(selected) if selected else None
        state_ids = {
            identifier
            for item in clip.asset_plan
            for identifier in (item.start_state_id, item.end_state_id)
            if identifier
        }
        if plan:
            state_ids.update(item.state_id for item in plan.bindings)
        material_versions = {
            identifier: project.states[identifier].selected_revisions
            for identifier in sorted(state_ids)
            if identifier in project.states
        }
        checkpoint.update(
            {
                "clip_id": clip.id,
                "clip_plan_hash": digest(
                    clip.model_dump(
                        include={
                            "description",
                            "duration",
                            "asset_plan",
                            "starts_at",
                            "ends_at",
                            "story_event_ids",
                            "continuity",
                            "plan_sources",
                            "state_readiness",
                        },
                        mode="json",
                    )
                ),
                "reference_plan": {
                    "id": plan.id,
                    "revision": plan.revision,
                    "status": plan.status,
                    "prompt_hash": digest(plan.prompt),
                }
                if plan
                else None,
                "bound_reference_ids": [item.revision_id for item in plan.bindings] if plan else [],
                "material_versions": material_versions,
                "preparation": {
                    "id": clip.preparation.id,
                    "revision": clip.preparation.revision,
                    "status": clip.preparation.status,
                    "pending_requirements": clip.preparation.pending_requirements,
                    "dependency_changes": clip.preparation.dependency_changes,
                }
                if clip.preparation
                else None,
                "review": {
                    "candidate_id": latest.id,
                    "report_hash": digest(latest.review.model_dump()),
                    "evidence_media_ids": latest.review.evidence_media_ids,
                }
                if latest and latest.review
                else None,
                "quality": quality_progress(project, clip).model_dump(mode="json"),
                "pending_repair_id": clip.pending_repair_id,
                "pending_job_ids": [
                    job.id
                    for candidate in candidates
                    for job in select_entities(project.jobs, parent_id=candidate.id)
                    if job.status in LIVE
                ],
                "previous_selected_candidate_id": selected.id if selected else None,
                "previous_end_state_hash": digest(end.model_dump()) if end else None,
            }
        )
    if "runtime_handoff" in session.checkpoint:
        checkpoint["runtime_handoff"] = deepcopy(session.checkpoint["runtime_handoff"])
    # Hash validation makes partial/corrupt handoff data detectable; no business
    # state, allowance or observed memory is written by this operation.
    checkpoint["fingerprint"] = digest(checkpoint)
    session.checkpoint = deepcopy(checkpoint)


def assignment(project: StudioProject, actor: Actor) -> dict[str, Any] | None:
    if not enabled(project, actor):
        return None
    session = actor.sessions.get(actor.session_key)
    if not session or session.purpose != "planning" or not session.planning_kind:
        return None
    checkpoint = session.checkpoint
    if checkpoint:
        require(
            checkpoint.get("fingerprint")
            == digest({k: v for k, v in checkpoint.items() if k != "fingerprint"}),
            "规划检查点校验失败",
        )
    return {
        "session_key": session.key,
        "kind": session.planning_kind,
        "clip_id": session.planning_clip_id,
        "round": session.planning_round,
        "checkpoint": checkpoint,
        "next_read": {
            "phase": "references" if session.planning_clip_id else "planning",
            "target_id": session.planning_clip_id or actor.set_id,
        },
        "instruction": (
            "Read the current business task packet. Checkpoints are not "
            "authorization, current facts or video-submission receipts."
        ),
    }
