"""Finite technical recovery without rewriting provider truth or quality reviews.

The supervisor supplies classified failures and persists these mutations in its
existing transaction. No helper performs I/O, releases provider capacity or posts
a request. Recovery policy is frozen with each job and survives process restarts.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any, Literal

from .candidate_lifecycle import (
    POLICY,
    clip_pending_work,
    fallback_selection_plan,
    selectable_candidates,
    technical_disposition_valid,
    terminal_recovery,
)
from .contracts import CandidateDisposition, Clip, Job, ProductionFailure, StudioProject
from .repository import select_entities
from .settings import StudioSettings


def _iso(stamp: float | None = None) -> str:
    return (
        datetime.fromtimestamp(stamp, UTC).isoformat()
        if stamp is not None
        else datetime.now(UTC).isoformat()
    )


def decisions_paused(project: StudioProject) -> bool:
    actors = [actor for actor in select_entities(project.actors) if actor.status != "complete"]
    return bool(actors) and all(actor.paused for actor in actors)


def initialize_policy(job: Job, settings: StudioSettings) -> dict[str, Any]:
    """Freeze bounded retry/outage policy once, including before an unknown POST."""
    policy = job.result.get("production_retry_policy")
    if not isinstance(policy, dict) or policy.get("policy") != POLICY:
        policy = {
            "policy": POLICY,
            "limit": settings.video_retry.max_retries,
            "base_delay_seconds": settings.video_retry.base_delay_seconds,
            "max_delay_seconds": settings.video_retry.max_delay_seconds,
            "query_outage_seconds": settings.production_query_outage_seconds,
        }
        job.result["production_retry_policy"] = policy
    return policy


def mark_terminal_job(
    job: Job,
    *,
    category: str,
    reason: str,
    outcome: Literal["failed", "unknown"] = "failed",
    stamp: float | None = None,
) -> dict[str, Any]:
    """End local recovery; an unknown provider task remains unknown and occupied."""
    existing = terminal_recovery(job)
    if existing is not None:
        return existing
    if not category.strip() or not reason.strip():
        raise ValueError("Technical termination requires an explicit category and reason.")
    marker = {
        "policy": POLICY,
        "terminal": True,
        "category": category,
        "reason": reason,
        "outcome": outcome,
        "attempts": len(job.result.get("technical_retry_history", [])),
        "provider_job_id": job.provider_job_id,
        "provider_status_at_termination": job.status,
        "created_at": _iso(stamp),
    }
    job.result["production_recovery"] = marker
    return marker


def schedule_technical_retry(
    project: StudioProject,
    job: Job,
    settings: StudioSettings,
    *,
    category: str,
    reason: str,
    stamp: float,
    definitely_not_accepted: bool = False,
    local_only: bool = False,
    retry_after_seconds: float | None = None,
) -> bool:
    """Queue at most the frozen retry budget, only after a safe classified failure.

    Callers must establish that a failed remote submission was not accepted. A
    known task ID is always queried, never blindly posted again. ``local_only``
    authorizes only analysis/extraction jobs and must not be used for generation.
    Returning False does not mean a provider task failed: inspect the audit.
    """
    shared_video = job.kind == "video" and bool(job.request.get("api_receipt_id"))
    if project.deleted_at or project.status in {"complete", "failed"}:
        return False
    if terminal_recovery(job) is not None or job.status == "complete":
        return False
    safe_local = local_only and job.kind in {"analysis", "reference"}
    if job.provider_job_id or (not safe_local and not definitely_not_accepted):
        return False
    # A paused owner may receive an already submitted failure. Persist its safe
    # deferred retry; the scheduler/claim gate keeps it unexecuted until resume.
    policy = initialize_policy(job, settings)
    history = job.result.setdefault("technical_retry_history", [])
    if len(history) >= int(policy["limit"]):
        mark_terminal_job(job, category=category, reason=reason, stamp=stamp)
        return False
    delay = min(
        float(policy["max_delay_seconds"]),
        float(policy["base_delay_seconds"]) * (2 ** min(len(history), 30)),
    )
    if retry_after_seconds is not None and math.isfinite(retry_after_seconds):
        delay = max(delay, max(0.0, retry_after_seconds))
    history.append(
        {
            "policy": POLICY,
            "attempt": len(history) + 1,
            "category": category,
            "reason": reason,
            "created_at": _iso(stamp),
            "next_poll_at": stamp + delay,
            "delay_seconds": delay,
            "local_only": safe_local,
            "definitely_not_accepted": definitely_not_accepted,
            "same_registration": shared_video,
        }
    )
    job.status = "queued"
    job.error = reason
    job.next_poll_at = stamp + delay
    return True


def query_outage_exhausted(job: Job, settings: StudioSettings, stamp: float) -> bool:
    """Measure an observed query outage, never normal generation runtime."""
    if not job.query_outage_since or job.query_error_count <= 0:
        return False
    try:
        started = datetime.fromisoformat(job.query_outage_since.replace("Z", "+00:00"))
        if started.tzinfo is None:
            return False
    except ValueError:
        return False
    policy = initialize_policy(job, settings)
    return stamp - started.timestamp() >= float(policy["query_outage_seconds"])


def _reference_consumers(project: StudioProject, job: Job) -> list[Clip]:
    """Only unavailable required views or explicit bindings create dependencies."""
    reference = project.references.get(job.target_id)
    if reference is None:
        return []
    state = project.states.get(reference.state_id)
    if state is None:
        return []
    # Another selected, reviewable or actively generating revision can still
    # satisfy this exact state/view. Failure of one revision cannot kill it.
    for alternate in select_entities(project.references, parent_id=state.id):
        if alternate.id == reference.id or alternate.view != reference.view:
            continue
        alternate_job = project.jobs.get(alternate.job_id or "")
        if alternate.status in {"selected", "candidate"} and alternate.media_id in project.media:
            return []
        if (
            alternate.status == "pending"
            and alternate_job
            and alternate_job.status in {"queued", "submitting", "running", "ambiguous"}
            and not terminal_recovery(alternate_job)
        ):
            return []
    consumers = []
    for clip in select_entities(project.clips):
        if clip.archived_at or clip.accepted_candidate_id or clip.terminal_failure:
            continue
        plan = project.reference_plans.get(clip.reference_plan_id or "")
        bindings = [
            *(plan.bindings if plan else []),
            *(clip.preparation.bindings if clip.preparation else []),
        ]
        explicit = any(binding.revision_id == reference.id for binding in bindings)
        required = any(
            item.start_state_id == state.id
            and reference.view
            in (
                item.required_views
                or (
                    project.assets[item.asset_id].required_views
                    if item.asset_id in project.assets
                    else []
                )
            )
            for item in clip.asset_plan
        )
        if explicit or required:
            consumers.append(clip)
    return consumers


def _fail_clip(
    clip: Clip,
    *,
    category: str,
    reason: str,
    job_ids: list[str],
    outcome: Literal["failed", "unknown"],
    upstream_clip_ids: list[str] | None = None,
) -> dict[str, Any] | None:
    if clip.archived_at or clip.accepted_candidate_id or clip.terminal_failure:
        return None
    clip.terminal_failure = ProductionFailure(
        category=category,
        reason=reason,
        job_ids=job_ids,
        outcome=outcome,
        upstream_clip_ids=upstream_clip_ids or [],
    )
    clip.status = "failed"
    return {
        "action": "failed",
        "clip_id": clip.id,
        "failure": clip.terminal_failure.model_dump(mode="json"),
    }


def _failed_prerequisites(project: StudioProject, clip: Clip) -> list[Clip]:
    production_set = project.sets[clip.set_id]
    failed = []
    if clip.order > 0:
        previous = project.clips.get(production_set.clip_ids[clip.order - 1])
        if previous and previous.terminal_failure:
            failed.append(previous)
    # Whole-Set dependencies are explicit, irrespective of the observed-handoff
    # policy; shared identity or film ordering alone is never a dependency.
    for identifier in production_set.depends_on:
        source = project.sets.get(identifier)
        if source:
            failed.extend(
                c for c in select_entities(project.clips, ids=source.clip_ids) if c.terminal_failure
            )
    for handoff in production_set.asset_handoffs:
        if handoff.mode != "observed" or handoff.confirmation is not None:
            continue
        if handoff.target_event_ids and not set(handoff.target_event_ids).intersection(
            clip.story_event_ids
        ):
            continue
        if handoff.state_ids and not set(handoff.state_ids).intersection(
            item.start_state_id for item in clip.asset_plan
        ):
            continue
        source = project.sets.get(str(handoff.source_set_id))
        if source:
            event_clips = [
                c
                for c in select_entities(project.clips, ids=source.clip_ids)
                if not c.archived_at and handoff.source_event_id in c.story_event_ids
            ]
            final_clip = max(event_clips, key=lambda c: c.order) if event_clips else None
            if final_clip and final_clip.terminal_failure:
                failed.append(final_clip)
    return list({item.id: item for item in failed}.values())


def reconcile_terminal_failures(project: StudioProject) -> list[dict[str, Any]]:
    """Retire impossible obligations, return settlement plans and fail only dependents.

    The caller applies ``action=settle`` through the ordinary final-selection
    transaction. Terminal dispositions never synthesize a successful review.
    Pending siblings remain fenced and may still produce a usable candidate.
    """
    if project.deleted_at or decisions_paused(project) or project.status in {"complete", "failed"}:
        return []
    affected: dict[str, list[Job]] = {}
    outcomes: list[dict[str, Any]] = []
    for job in select_entities(
        project.jobs,
        statuses=["queued", "submitting", "running", "ambiguous", "failed"],
    ):
        marker = terminal_recovery(job)
        if marker is None:
            continue
        candidate = (
            project.candidates.get(job.target_id) if job.kind in {"video", "analysis"} else None
        )
        if candidate is not None:
            clip = project.clips.get(candidate.clip_id)
            if clip is None or clip.archived_at:
                continue
            if job.kind == "video" and candidate.job_id != job.id:
                continue
            affected.setdefault(clip.id, []).append(job)
            if candidate.disposition is None and (job.kind == "video" or candidate.review is None):
                candidate.disposition = CandidateDisposition(
                    kind="technical_failure",
                    reason=marker["reason"],
                    job_ids=[job.id],
                    outcome=marker["outcome"],
                )
                outcomes.append(
                    {
                        "action": "candidate_failed",
                        "clip_id": clip.id,
                        "candidate_id": candidate.id,
                        "job_id": job.id,
                        "outcome": marker["outcome"],
                    }
                )
            elif candidate.disposition and candidate.disposition.kind == "technical_failure":
                if job.id not in candidate.disposition.job_ids:
                    candidate.disposition.job_ids.append(job.id)
                if marker["outcome"] == "unknown":
                    candidate.disposition.outcome = "unknown"
        elif job.kind == "image":
            for clip in _reference_consumers(project, job):
                affected.setdefault(clip.id, []).append(job)
    for identifier, jobs in affected.items():
        clip = project.clips[identifier]
        if clip.accepted_candidate_id or clip.terminal_failure or clip_pending_work(project, clip):
            continue
        plan = fallback_selection_plan(project, clip)
        if plan is not None:
            plan["reason"] = (
                "Technical recovery is exhausted; select from all actually reviewed "
                "playable candidates. " + plan["reason"]
            )
            outcomes.append(
                {
                    "action": "settle",
                    "clip_id": clip.id,
                    "plan": plan,
                    "job_ids": [j.id for j in jobs],
                }
            )
        elif not selectable_candidates(project, clip):
            markers = [terminal_recovery(job) for job in jobs]
            if all(marker and marker.get("outcome") == "unknown" for marker in markers):
                continue  # Unconfirmed outcomes do not establish an impossible Clip.
            failure = _fail_clip(
                clip,
                category="technical_recovery_exhausted",
                reason="No usable reviewed candidate remains after bounded technical recovery.",
                job_ids=[job.id for job in jobs],
                outcome="unknown"
                if any(m and m["outcome"] == "unknown" for m in markers)
                else "failed",
            )
            if failure:
                outcomes.append(failure)
    # Traverse until transitive hard dependencies are marked. Submitted work is
    # never cancelled here: it retains its own obligations and provider state.
    changed = True
    while changed:
        changed = False
        for clip in select_entities(project.clips):
            if clip.archived_at or clip.accepted_candidate_id or clip.terminal_failure:
                continue
            prerequisites = _failed_prerequisites(project, clip)
            if not prerequisites or clip_pending_work(project, clip):
                continue
            failure = _fail_clip(
                clip,
                category="dependency_failed",
                reason="A required production prerequisite cannot be delivered.",
                job_ids=list(
                    dict.fromkeys(
                        j
                        for c in prerequisites
                        if c.terminal_failure
                        for j in c.terminal_failure.job_ids
                    )
                ),
                upstream_clip_ids=[c.id for c in prerequisites],
                outcome="unknown"
                if any(
                    c.terminal_failure and c.terminal_failure.outcome == "unknown"
                    for c in prerequisites
                )
                else "failed",
            )
            if failure:
                outcomes.append(failure)
                changed = True
    return outcomes


def candidate_failure_counts(project: StudioProject, clip: Clip) -> dict[str, int]:
    """Keep technical termination visibly separate from actual quality verdicts."""
    candidates = select_entities(project.candidates, parent_id=clip.id)
    return {
        "technical_failed": sum(
            technical_disposition_valid(project, candidate) for candidate in candidates
        ),
        "quality_rejected": sum(
            candidate.review is not None and candidate.review.decision == "reject"
            for candidate in candidates
        ),
        "quality_accepted": sum(
            candidate.review is not None and candidate.review.decision == "accept"
            for candidate in candidates
        ),
        "unreviewed": sum(
            candidate.review is None and not technical_disposition_valid(project, candidate)
            for candidate in candidates
        ),
    }
