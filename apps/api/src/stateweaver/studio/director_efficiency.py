"""Opt-in Set director mechanics; never change historical project policies."""

from __future__ import annotations

from typing import Any

from .contracts import Actor, ActorSession, StudioProject
from .memory import digest


def invalidate_context(
    project: StudioProject, actor: Actor, *, reason: str = "context_invalidated"
) -> None:
    actor.context_epoch += 1
    active_session = actor.sessions.get(actor.session_key)
    if active_session:
        session = actor.sessions.setdefault(actor.session_key, ActorSession(key=actor.session_key))
        session.context_epoch += 1
        from .contracts import now

        session.context_events.append(
            {"reason": reason, "epoch": session.context_epoch, "created_at": now()}
        )
        session.image_receipts.clear()
        session.viewed_media_ids.clear()
        session.image_count = 0
        actor.pending_image_deliveries = [
            delivery
            for delivery in actor.pending_image_deliveries
            if delivery.session_key != actor.session_key
        ]


def image_key(project: StudioProject, actor: Actor, sha256: str, profile: Any) -> str:
    session = actor.sessions.get(actor.session_key)
    return digest(
        [
            actor.id,
            actor.role,
            actor.session_key,
            actor.thread_id,
            actor.context_epoch,
            session.context_epoch if session else 0,
            sha256,
            profile,
        ]
    )


def compact_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep creative/observed facts; index operational state histories instead."""
    histories = {"preparation_history", "evolution_history"}

    def compact(value: Any) -> Any:
        if isinstance(value, list):
            return [compact(item) for item in value]
        if not isinstance(value, dict):
            return value
        result = {key: compact(item) for key, item in value.items() if key not in histories}
        available = {key: len(value[key]) for key in histories if value.get(key)}
        if available:
            result["history_available"] = {**result.get("history_available", {}), **available}
        return result

    return [compact(record) for record in records]


def repair_packet(project: StudioProject, clip_id: str) -> dict[str, Any] | None:
    from .repository import select_entities

    candidates = select_entities(
        project.candidates, parent_id=clip_id, statuses=["rejected"], descending=True, limit=1
    )
    if not candidates or not candidates[0].review:
        return None
    candidate = candidates[0]
    review = candidate.review
    assert review is not None
    clip = project.clips[clip_id]
    plan = clip.repair_plan
    return {
        "candidate_id": candidate.id,
        "reviewed_at": review.created_at,
        "decision": review.decision,
        "findings": [finding.model_dump(mode="json") for finding in review.findings],
        "preserve": plan.preserve if plan else [],
        "suggested_change": plan.change if plan else "",
        "limitations": review.limitations,
        "guidance": candidate.analysis.get("repair_guidance", []),
        "authority": (
            "Suggestions, not the final prompt. The planning session "
            "chooses repair mode, materials and the final six-section "
            "prompt."
        ),
    }


def continuity_packet(project: StudioProject, clip_id: str) -> dict[str, Any]:
    from .adjudication import actual_end_state
    from .continuation_controls import task_packet

    clip = project.clips[clip_id]
    previous = (
        project.clips[project.sets[clip.set_id].clip_ids[clip.order - 1]] if clip.order else None
    )
    candidate = project.candidates.get(previous.accepted_candidate_id or "") if previous else None
    end = actual_end_state(candidate) if candidate else None
    tail = project.jobs.get(candidate.continuation_reference_job_id or "") if candidate else None
    return {
        "required": previous is not None,
        **({"conditioning": control} if (control := task_packet(project, clip)) else {}),
        "previous_candidate_id": candidate.id if candidate else None,
        "actual_end_state": end.model_dump(mode="json") if end else None,
        "tail_job_id": tail.id if tail else None,
        "tail_status": tail.status if tail else "waiting" if previous else "not_required",
        "planned_continuity": clip.continuity,
        "check_fields": ["action", "space", "camera", "completed_actions", "unknowns"],
        "owner": "set_director",
        "instructions": (
            "Check the actual action phase, composition, camera movement "
            "and actions that must not be repeated. Machine binding checks "
            "do not replace visual inspection."
        ),
    }
