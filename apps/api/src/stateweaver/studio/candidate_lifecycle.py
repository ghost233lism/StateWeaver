"""Candidate-owned work and final-selection fences for coherent production.

The fence is derived from durable jobs, candidates and repair reservations, rather
than a Clip's selected/status flag. It performs no provider calls or mutations.
Historical projects without the versioned policy retain their original behavior.
"""

from __future__ import annotations

from typing import Any

from .contracts import Actor, Candidate, Clip, ProductionSet, StudioProject, now
from .repository import select_entities
from .store import StudioError

POLICY = "coherent-v1"
LIVE_JOBS = frozenset({"queued", "submitting", "running", "ambiguous"})
TERMINAL_JOBS = frozenset({"complete", "failed"})
ASSESSMENT_ORDER = {"met": 0, "minor_deviation": 1, "major_deviation": 2, "unknown": 3}
ASSESSMENT_DIMENSIONS = ("story_coverage", "identity_consistency", "visual_quality")


def terminal_recovery(job: Any) -> dict[str, Any] | None:
    """Only a versioned recovery audit may retire an unfinished local obligation."""
    marker = job.result.get("production_recovery") if job is not None else None
    if (
        isinstance(marker, dict)
        and marker.get("policy") == POLICY
        and marker.get("terminal") is True
        and marker.get("outcome") in {"failed", "unknown"}
        and marker.get("reason")
        and marker.get("category")
    ):
        return marker
    return None


def technical_disposition_valid(project: StudioProject, candidate: Candidate) -> bool:
    disposition = candidate.disposition
    if disposition is None or disposition.kind != "technical_failure":
        return False
    if not disposition.job_ids:
        return False
    unknown = False
    for identifier in disposition.job_ids:
        job = project.jobs.get(identifier)
        if (
            job is None
            or job.target_id != candidate.id
            or job.kind not in {"video", "analysis"}
            or (job.kind == "video" and job.id != candidate.job_id)
            or terminal_recovery(job) is None
        ):
            return False
        marker = terminal_recovery(job)
        unknown = unknown or bool(marker and marker["outcome"] == "unknown")
    return disposition.outcome == ("unknown" if unknown else "failed")


def candidate_disposition_valid(project: StudioProject, candidate: Candidate) -> bool:
    """Validate terminal creative disposition without treating it as a quality pass."""
    if technical_disposition_valid(project, candidate):
        return True
    disposition = candidate.disposition
    if (
        disposition is None
        or disposition.kind not in {"cancelled", "superseded"}
        or (not disposition.job_ids)
    ):
        return False
    return all(
        (job := project.jobs.get(identifier)) is not None
        and job.target_id == candidate.id
        and job.kind in {"video", "analysis"}
        and (job.kind != "video" or job.id == candidate.job_id)
        for identifier in disposition.job_ids
    )


def clip_pending_work(project: StudioProject, clip: Clip) -> list[dict[str, Any]]:
    """Return every unfinished obligation, including siblings of a selected take.

    A failed provider job without media is a terminal technical failure, not a
    review. Conversely, returned media cannot disappear from the review queue just
    because another take was selected or its generation job reports a failure.
    """
    pending: list[dict[str, Any]] = []
    candidates = select_entities(project.candidates, parent_id=clip.id)
    by_id = {candidate.id: candidate for candidate in candidates}

    def add(kind: str, identifier: str, candidate_id: str | None, reason: str) -> None:
        pending.append(
            {
                "kind": kind,
                "entity_id": identifier,
                "candidate_id": candidate_id,
                "clip_id": clip.id,
                "reason": reason,
            }
        )

    for identifier in clip.candidate_ids:
        if identifier not in by_id:
            add(
                "candidate_registration", identifier, identifier, "Registered candidate is missing."
            )
    for candidate in candidates:
        if candidate_disposition_valid(project, candidate):
            continue
        job = project.jobs.get(candidate.job_id)
        if job is None:
            add("job_registration", candidate.job_id, candidate.id, "Generation job is missing.")
        elif job.kind != "video" or job.target_id != candidate.id:
            add("job_ownership", job.id, candidate.id, "Generation ownership does not match.")
        elif job.status not in TERMINAL_JOBS:
            add("generation", job.id, candidate.id, "Generation has not reached a terminal result.")
        if job is not None and job.status == "complete" and not candidate.media_id:
            add("result_registration", job.id, candidate.id, "Returned video is not registered.")
        for child in select_entities(project.jobs, parent_id=candidate.id):
            if child.id == candidate.job_id:
                continue
            if child.status not in TERMINAL_JOBS:
                add(child.kind, child.id, candidate.id, "Candidate evidence work is unfinished.")
        if candidate.media_id:
            media = project.media.get(candidate.media_id)
            if media is None:
                add(
                    "media_registration",
                    candidate.media_id,
                    candidate.id,
                    "Media record is missing.",
                )
            if candidate.review is None:
                add("review", candidate.id, candidate.id, "Returned candidate has no final review.")
            elif not getattr(candidate.review, "selection_assessment", None):
                add(
                    "selection_assessment",
                    candidate.id,
                    candidate.id,
                    "Review lacks the recorded comparison required for final selection.",
                )
        elif candidate.status == "review":
            add("result_registration", candidate.id, candidate.id, "Review candidate has no video.")
    for attempt in clip.repair_attempts:
        if not attempt.cancelled_at and not attempt.job_ids:
            add(
                "repair_reservation",
                attempt.id,
                None,
                "A reserved repair has neither been submitted nor explicitly cancelled.",
            )
        for identifier in attempt.job_ids:
            registered = project.jobs.get(identifier)
            if registered is None:
                add("job_registration", identifier, None, "Reserved repair job is missing.")
            elif (
                registered.kind != "video"
                or registered.target_id not in by_id
                or by_id[registered.target_id].job_id != registered.id
                or (registered.request.get("repair_attempt_id") != attempt.id)
            ):
                add(
                    "repair_job_ownership",
                    identifier,
                    registered.target_id,
                    "The repair reservation is not bound to its own candidate generation.",
                )
    return pending


def set_pending_work(project: StudioProject, production_set: ProductionSet) -> list[dict[str, Any]]:
    """Include archived planning rows so replanning cannot conceal submitted work."""
    clips = {clip.id: clip for clip in select_entities(project.clips, parent_id=production_set.id)}
    pending: list[dict[str, Any]] = []
    for identifier in production_set.clip_ids:
        if identifier not in clips:
            pending.append(
                {
                    "kind": "clip_registration",
                    "entity_id": identifier,
                    "clip_id": identifier,
                    "candidate_id": None,
                    "reason": "Active Clip is missing.",
                }
            )
    for clip in clips.values():
        pending.extend(clip_pending_work(project, clip))
    return pending


def _require_settled(pending: list[dict[str, Any]], scope: str) -> None:
    if pending:
        kinds = ", ".join(dict.fromkeys(str(item["kind"]) for item in pending))
        raise StudioError(
            f"{scope} still has unfinished candidate work ({kinds}); finish it first."
        )


def require_clip_settled(project: StudioProject, clip: Clip) -> None:
    _require_settled(clip_pending_work(project, clip), f"Clip {clip.id}")


def require_set_settled(project: StudioProject, production_set: ProductionSet) -> None:
    _require_settled(set_pending_work(project, production_set), f"Set {production_set.id}")


def require_project_settled(project: StudioProject) -> None:
    for production_set in select_entities(project.sets):
        require_set_settled(project, production_set)


def pending_review_candidates(project: StudioProject, actor: Actor) -> list[Candidate]:
    """Fair, candidate-owned queue, independent of existing Clip selections."""
    if actor.role != "set_director" or actor.set_id not in project.sets:
        return []
    production_set = project.sets[str(actor.set_id)]
    result: list[Candidate] = []
    for clip in select_entities(project.clips, ids=production_set.clip_ids):
        if clip.archived_at:
            continue
        for candidate in select_entities(
            project.candidates, parent_id=clip.id, statuses=["review"]
        ):
            if candidate_disposition_valid(project, candidate):
                continue
            if candidate.review is not None or not candidate.media_id:
                continue
            if select_entities(
                project.jobs,
                parent_id=candidate.id,
                subtype="analysis",
                statuses=list(LIVE_JOBS),
                limit=1,
            ):
                continue
            result.append(candidate)
    return sorted(result, key=lambda item: (item.created_at, item.id))


def selectable_candidates(project: StudioProject, clip: Clip) -> list[Candidate]:
    """A playable reviewed candidate must still match the selected predecessor."""
    if getattr(clip, "terminal_failure", None):
        return []
    predecessor_id = None
    production_set = project.sets[clip.set_id]
    if clip.order:
        previous = project.clips[production_set.clip_ids[clip.order - 1]]
        predecessor_id = previous.accepted_candidate_id
        if predecessor_id is None:
            return []
    result = []
    for candidate in select_entities(project.candidates, parent_id=clip.id):
        media = project.media.get(candidate.media_id or "")
        job = project.jobs.get(candidate.job_id)
        if (
            candidate.disposition is None
            and candidate.review is not None
            and candidate.end_state is not None
            and candidate.status in {"accepted", "rejected", "review"}
            and candidate.previous_candidate_id == predecessor_id
            and job is not None
            and job.status == "complete"
            and job.target_id == candidate.id
            and media is not None
            and media.mime_type.startswith("video/")
            and media.byte_size > 0
            and media.duration is not None
            and media.duration > 0
            and (getattr(candidate.review, "selection_assessment", None))
        ):
            result.append(candidate)
    return result


def cancel_unsubmitted_repair_reservations(
    project: StudioProject, clip: Clip, *, reason: str
) -> list[str]:
    """Cancel only reservations that never produced a provider job."""
    cancelled: list[str] = []
    for attempt in clip.repair_attempts:
        if attempt.cancelled_at or attempt.job_ids:
            continue
        attempt.cancelled_at = now()
        attempt.cancellation_reason = reason
        cancelled.append(attempt.id)
        if clip.pending_repair_id == attempt.id:
            clip.pending_repair_id = None
    return cancelled


def fallback_blocking_work(project: StudioProject, clip: Clip) -> list[dict[str, Any]]:
    """Keep ownership/results strict while allowing empty reservations and unknown siblings."""
    blockers: list[dict[str, Any]] = []
    for item in clip_pending_work(project, clip):
        if item["kind"] == "repair_reservation":
            continue
        job = project.jobs.get(str(item["entity_id"]))
        if item["kind"] == "generation" and job and job.status == "ambiguous":
            continue
        blockers.append(item)
    return blockers


def _rank(candidate: Candidate) -> tuple[int, int, int, str, str]:
    assessment = getattr(candidate.review, "selection_assessment", None)
    values = [
        ASSESSMENT_ORDER.get(str(getattr(assessment, dimension, "unknown")), 3)
        for dimension in ASSESSMENT_DIMENSIONS
    ]
    return values[0], values[1], values[2], candidate.created_at, candidate.id


def _selection_plan(candidate: Candidate, *, kind: str, basis: str) -> dict[str, Any]:
    assert candidate.review is not None
    assessment = getattr(candidate.review, "selection_assessment", None)
    return {
        "candidate_id": candidate.id,
        "kind": kind,
        "basis": basis,
        "reason": (
            "Select a reviewed playable candidate after all candidate work is terminal. " + basis
        ),
        "deviations": list(
            dict.fromkeys(
                [
                    *candidate.issues,
                    *(
                        item.description
                        for item in candidate.review.findings
                        if item.severity == "repair"
                    ),
                    *candidate.review.limitations,
                    *(gap.reason for gap in candidate.review.coverage_gaps),
                ]
            )
        )
        or ([candidate.review.reason] if candidate.review.decision == "reject" else []),
        "selection_assessment": assessment.model_dump(mode="json") if assessment else None,
        "review_decision": candidate.review.decision,
        "review_created_at": candidate.review.created_at,
    }


def reviewed_selection_plan(project: StudioProject, clip: Clip) -> dict[str, Any] | None:
    """Pick a passing review only after every current candidate has been handled."""
    if clip_pending_work(project, clip):
        return None
    candidates = selectable_candidates(project, clip)
    current = next((item for item in candidates if item.id == clip.accepted_candidate_id), None)
    if current and current.selection:
        return _selection_plan(
            current, kind=current.selection.kind, basis="Retain recorded selection."
        )
    passed = [
        item
        for item in candidates
        if item.review
        and item.review.decision == "accept"
        and not item.issues
        and not any(finding.severity == "repair" for finding in item.review.findings)
    ]
    if not passed:
        return None
    return _selection_plan(
        min(passed, key=_rank),
        kind="quality_passed",
        basis="Passing review; story coverage, identity consistency, then visual quality.",
    )


def fallback_selection_plan(project: StudioProject, clip: Clip) -> dict[str, Any] | None:
    """Compute a fallback; the caller owns the exhausted-decision/technical gate.

    Ranks completed, reviewed candidates using recorded evidence and decisions.
    """
    if fallback_blocking_work(project, clip):
        return None
    candidates = selectable_candidates(project, clip)
    if not candidates:
        return None
    fallback_id = clip.quality_episode.fallback_id if clip.quality_episode else None
    preferred = next((item for item in candidates if item.id == fallback_id), None)
    if preferred:
        return _selection_plan(
            preferred,
            kind="system_fallback",
            basis="Execute the director's previously designated fallback after decision recovery.",
        )
    return _selection_plan(
        min(candidates, key=_rank),
        kind="system_fallback",
        basis="Recorded story coverage, identity consistency, visual quality; stable tie order.",
    )
