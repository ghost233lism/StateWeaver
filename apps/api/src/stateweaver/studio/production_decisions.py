"""Apply final selections only after the versioned candidate work fence closes."""

from __future__ import annotations

from typing import Any, Literal

from .candidate_lifecycle import (
    cancel_unsubmitted_repair_reservations,
    fallback_blocking_work,
    require_clip_settled,
    selectable_candidates,
)
from .contracts import (
    Actor,
    CandidateDisposition,
    Clip,
    QualityDecision,
    SelectionRecord,
    StudioProject,
)
from .memory import record_selected_outcome
from .store import StudioError


def apply_selection(
    project: StudioProject,
    actor: Actor,
    clip: Clip,
    plan: dict[str, Any],
    *,
    kind: Literal["quality_passed", "director_compromise", "system_fallback"],
) -> dict[str, Any]:
    """Preserve reviews and make the selection and its audit one atomic mutation."""
    if actor.role != "executive" and not (
        actor.role == "set_director" and actor.set_id == clip.set_id
    ):
        raise StudioError("Selection is outside the actor's production scope.")
    if (
        project.deleted_at
        or project.status in {"complete", "failed"}
        or project.sets[clip.set_id].status in {"complete", "failed"}
    ):
        raise StudioError(
            "A closed production version must be explicitly reopened before selection."
        )
    existing = project.candidates.get(str(plan["candidate_id"]))
    if (
        existing is not None
        and existing.clip_id == clip.id
        and clip.accepted_candidate_id == existing.id
        and existing.selection is not None
    ):
        # Selection may itself enqueue a continuation reference. An exact replay
        # acknowledges the committed choice without mutating or closing that work.
        return {"clip_id": clip.id, "candidate_id": existing.id, "no_progress": True}
    if kind == "system_fallback":
        cancel_unsubmitted_repair_reservations(
            project,
            clip,
            reason="Cancelled without submission when bounded recovery selected a saved fallback.",
        )
        pending = fallback_blocking_work(project, clip)
        if pending:
            kinds = ", ".join(dict.fromkeys(str(item["kind"]) for item in pending))
            raise StudioError(
                f"Clip {clip.id} still has unfinished candidate work that blocks fallback "
                f"({kinds})."
            )
        for sibling in project.candidates.values():
            job = project.jobs.get(sibling.job_id)
            if (
                sibling.clip_id == clip.id
                and sibling.id != str(plan["candidate_id"])
                and sibling.disposition is None
                and job is not None
                and job.status == "ambiguous"
            ):
                sibling.disposition = CandidateDisposition(
                    kind="superseded",
                    reason=(
                        "A saved reviewed fallback was selected while this unknown submission "
                        "remained in collection; any late result is retained without changing "
                        "the frozen selection."
                    ),
                    job_ids=[job.id],
                    actor_id=actor.id,
                    outcome="unknown",
                )
    else:
        require_clip_settled(project, clip)
    candidates = {candidate.id: candidate for candidate in selectable_candidates(project, clip)}
    candidate = candidates.get(str(plan["candidate_id"]))
    if candidate is None or candidate.review is None:
        raise StudioError(
            "Final selection requires a reviewed playable candidate and current inputs."
        )
    if clip.accepted_candidate_id:
        raise StudioError("A frozen selection must be revised explicitly before replacing it.")
    deviations = list(plan.get("deviations", []))
    if kind == "quality_passed" and (
        candidate.review.decision != "accept"
        or candidate.issues
        or any(finding.severity == "repair" for finding in candidate.review.findings)
    ):
        raise StudioError("A rejected review cannot be relabeled as a quality pass.")
    if kind != "quality_passed" and candidate.review.decision == "reject" and not deviations:
        deviations = [candidate.review.reason]
    candidate.selection = SelectionRecord(
        kind=kind,
        actor_id=actor.id,
        reason=str(plan["reason"]),
        deviations=deviations,
        delegated_task_ids=candidate.confirmed_end_state.delegated_task_ids
        if candidate.confirmed_end_state
        else [],
    )
    candidate.status = "accepted"
    clip.accepted_candidate_id = candidate.id
    clip.status = "accepted"
    if clip.quality_episode:
        clip.quality_episode.status = "resolved"
        clip.quality_episode.decisions.append(
            QualityDecision(
                actor_id=actor.id,
                action="select" if kind == "quality_passed" else "compromise",
                candidate_id=candidate.id,
                reason=str(plan["reason"]),
                deviations=deviations,
            )
        )
    issue = "quality:clip:" + clip.id
    if issue in project.issues:
        project.issues.remove(issue)
    record_selected_outcome(project, candidate, actor.id)
    return {
        **plan,
        "clip_id": clip.id,
        "selection_kind": kind,
        "review_preserved": True,
    }
