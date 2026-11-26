"""One playback ledger for budgets, role reads and UI; never a production ETA.

Only current Clip identities count. Submitted/selected recipes stay pinned; references,
archived plans and correction versions do not add playback. Decoder duration is a
separate observation and never opens a budget-approval loop.
"""

from __future__ import annotations

import math
from typing import Any

from .assembly import current_final_media_id
from .contracts import (
    Actor,
    Clip,
    PlaybackDuration,
    PlaybackSetStatus,
    ProductionSet,
    SetDurationAdjustment,
    StudioProject,
    now,
)
from .memory import digest
from .repository import select_entities
from .store import StudioPrerequisiteError

EPSILON = 0.001


def _seconds(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value > 0 else None


def accounted_duration(p: StudioProject, clip: Clip) -> float:
    """Select one recipe, not the sum of this Clip's attempts."""
    candidate = p.candidates.get(str(clip.accepted_candidate_id))
    if candidate is None:
        candidates = select_entities(
            p.candidates, parent_id=clip.id, statuses=["generating", "review"]
        )
        candidates = [
            c
            for c in candidates
            if (job := p.jobs.get(c.job_id)) is not None and job.status != "failed"
        ]
        candidate = max(candidates, key=lambda c: c.created_at, default=None)
    if candidate is not None:
        job = p.jobs.get(candidate.job_id)
        duration = _seconds(job.request.get("duration")) if job else None
        if duration is not None:
            return duration
    return clip.duration


def set_status(p: StudioProject, production_set: ProductionSet) -> PlaybackSetStatus:
    clips = [c for c in select_entities(p.clips, ids=production_set.clip_ids) if not c.archived_at]
    total = sum(accounted_duration(p, c) for c in clips)
    actual, missing = 0.0, 0
    for clip in clips:
        if not clip.accepted_candidate_id:
            continue
        candidate = p.candidates.get(clip.accepted_candidate_id)
        media = p.media.get(str(candidate.media_id)) if candidate else None
        duration = _seconds(media.duration) if media else None
        if duration is None:
            missing += 1
        else:
            actual += duration
    budget = production_set.duration_budget
    complete = bool(clips) and (production_set.clip_plan_complete)
    status: Any = "within_budget"
    reason = ""
    can_submit = True
    if budget is None:
        status, can_submit = "needs_budget", False
        reason = "The executive has not assigned this Set a target duration and allowed interval"
    elif total > budget.maximum_seconds + EPSILON:
        status, can_submit = "above_maximum", False
        reason = (
            f"Known planned duration for this Set: {total:g} seconds; "
            f"exceeds the approved maximum of {budget.maximum_seconds:g} seconds"
        )
    elif complete and total < budget.minimum_seconds - EPSILON:
        status, can_submit = "below_minimum", False
        reason = (
            f"Complete planned duration for this Set: {total:g} seconds; "
            f"below the approved minimum of {budget.minimum_seconds:g} seconds; "
            "Adjust the interval to natural pacing; do not stretch actions"
        )
    elif not clips:
        status = "unplanned"
    elif not complete:
        status = "incomplete"
    executive = next(iter(select_entities(p.actors, subtype="executive", limit=1)), None)
    pending = production_set.duration_adjustment
    return PlaybackSetStatus(
        set_id=production_set.id,
        title=production_set.title,
        budget=budget,
        budget_revision=production_set.duration_budget_revision,
        plan_complete=complete,
        planned_clips=len(clips),
        accounted_seconds=round(total, 6),
        selected_actual_seconds=round(actual, 6),
        selected_missing_duration=missing,
        status=status,
        can_submit=can_submit,
        reason=reason,
        owner_id=executive.id if executive else None,
        adjustment_id=pending.id if pending and pending.status == "pending" else None,
    )


def report(p: StudioProject, *, set_id: str | None = None) -> PlaybackDuration:
    """A scoped report never reads other directors' private Clip plans."""
    sets = [p.sets[set_id]] if set_id else select_entities(p.sets)
    statuses = [set_status(p, s) for s in sorted(sets, key=lambda s: s.order)]
    final_id = current_final_media_id(p) if not set_id else None
    final = p.media.get(final_id) if final_id else None
    estimate = p.screenplay.manuscript.duration_estimate if p.screenplay.manuscript else None
    return PlaybackDuration(
        policy=p.playback_policy,
        estimate=estimate,
        planned_seconds=round(sum(s.accounted_seconds for s in statuses), 6),
        selected_actual_seconds=round(sum(s.selected_actual_seconds for s in statuses), 6),
        selected_missing_duration=sum(s.selected_missing_duration for s in statuses),
        final_actual_seconds=_seconds(final.duration) if final else None,
        plan_complete=bool(statuses) and all(s.plan_complete for s in statuses),
        unplanned_set_ids=[s.set_id for s in statuses if not s.plan_complete],
        sets=statuses,
        notes=[
            (
                "The overall estimate guides natural pacing. The executive "
                "coordinates changes and records reasons; there is no fixed "
                "tolerance or hard maximum."
            ),
            (
                "Budgets use current Clip plans or original submitted/selected "
                "job durations; reference tails, latent history, failed "
                "versions and archived plans are not counted again."
            ),
            (
                "Actual playback duration is shown separately; ordinary "
                "generation tail differences do not require budget approval. "
                "Incomplete plans report only known portions."
            ),
        ]
        + (
            [
                (
                    "This report covers only this Set; the executive coordinates "
                    "the whole-film allocation."
                )
            ]
            if set_id
            else []
        ),
    )


def require_submission(p: StudioProject, clip: Clip) -> None:
    current = set_status(p, p.sets[clip.set_id])
    if not current.can_submit:
        raise StudioPrerequisiteError(
            current.reason,
            details={"kind": "set_duration", **current.model_dump(mode="json")},
            recovery=(
                "The plan is saved. The executive can adjust the interval "
                "through set_duration_budget, or the Set director can revise "
                "unsubmitted plans and recheck. Other Sets may continue."
            ),
        )


def sync_adjustment(p: StudioProject, production_set: ProductionSet, actor: Actor) -> bool:
    """Persist one current request and wake once per changed budget/plan decision."""
    current = set_status(p, production_set)
    old = production_set.duration_adjustment
    # Empty planning is not a shortfall; Set preparation can finish before planning.
    blocked = bool(current.planned_clips) and not current.can_submit
    fingerprint = digest(
        [current.accounted_seconds, current.plan_complete, current.budget_revision, current.status]
    )
    if old and old.status == "pending" and (not blocked or old.fingerprint != fingerprint):
        old.status, old.resolved_at = "resolved", now()
        production_set.duration_history.append({"adjustment": old.model_dump(mode="json")})
    if not blocked or old and old.status == "pending" and old.fingerprint == fingerprint:
        return False
    assert current.owner_id is not None
    pending = SetDurationAdjustment(
        fingerprint=fingerprint,
        planned_seconds=current.accounted_seconds,
        plan_complete=current.plan_complete,
        budget_revision=current.budget_revision,
        reason=current.reason,
        owner_id=current.owner_id,
        created_by=actor.id,
    )
    production_set.duration_adjustment = pending
    executive = p.actors[current.owner_id]
    executive.request_wake()
    if not executive.paused and executive.status in {"idle", "waiting"}:
        executive.status = "ready"
    # Keep a compact durable notification, not the entire private Clip plan.
    from .service import add_item

    add_item(
        p,
        executive.id,
        "status",
        f"Set {production_set.title} needs a duration-budget adjustment: {current.reason}",
        {
            "set_id": production_set.id,
            "adjustment_id": pending.id,
            "planned_seconds": current.accounted_seconds,
            "budget_revision": current.budget_revision,
            "owner_id": executive.id,
        },
    )
    return True
