"""Bounded, read-only lifecycle diagnostics from authoritative production rows.

Counts are SQL aggregates, not candidate-history hydration. Polls, quality
corrections and technical retries are reported separately. No diagnostic can
select a candidate or change a project's completion state.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session, aliased

from stateweaver.persistence.models import StudioEntityRow

from .contracts import StudioProject
from .production_runtime import enabled, wait_state


def _terminal_recovery(row: Any) -> Any:
    audit = row.payload["result"]["production_recovery"]
    return and_(
        func.coalesce(audit["policy"].as_string(), "") == "coherent-v1",
        func.coalesce(audit["terminal"].as_integer(), 0) == 1,
        func.coalesce(audit["outcome"].as_string(), "").in_(["failed", "unknown"]),
        func.coalesce(audit["reason"].as_string(), "") != "",
        func.coalesce(audit["category"].as_string(), "") != "",
    )


def _retired_candidate() -> Any:
    """SQL equivalent of technical_disposition_valid, without history hydration."""
    candidate = StudioEntityRow
    job = aliased(StudioEntityRow)
    identifiers = func.json_each(candidate.payload, "$.disposition.job_ids").table_valued("value")
    invalid_job = (
        select(1)
        .select_from(
            identifiers.outerjoin(
                job,
                (job.project_id == candidate.project_id)
                & (job.kind == "jobs")
                & (job.id == identifiers.c.value),
            )
        )
        .where(
            or_(
                job.id.is_(None),
                job.subtype.not_in(["video", "analysis"]),
                job.parent_id != candidate.id,
                and_(job.subtype == "video", job.id != candidate.payload["job_id"].as_string()),
                ~_terminal_recovery(job),
            )
        )
        .correlate(candidate)
        .exists()
    )
    return and_(
        func.coalesce(candidate.payload["disposition"]["kind"].as_string(), "")
        == "technical_failure",
        func.coalesce(func.json_array_length(candidate.payload, "$.disposition.job_ids"), 0) > 0,
        ~invalid_job,
    )


def summary(
    project: StudioProject, session: Session, *, detail_limit: int = 32
) -> dict[str, Any] | None:
    if not enabled(project):
        return None
    detail_limit = max(0, min(detail_limit, 128))
    active_clips = [clip for clip in project.clips.values() if not clip.archived_at]
    clip_rows = aliased(StudioEntityRow)
    active_clip_ids = select(clip_rows.id).where(
        clip_rows.project_id == project.id,
        clip_rows.kind == "clips",
        clip_rows.payload["archived_at"].as_string().is_(None),
    )
    candidates = (
        StudioEntityRow.project_id == project.id,
        StudioEntityRow.kind == "candidates",
        StudioEntityRow.parent_id.in_(active_clip_ids),
    )
    retired = _retired_candidate()
    candidate_states = {
        status: int(count)
        for status, count in session.execute(
            select(StudioEntityRow.status, func.count())
            .where(*candidates, ~retired)
            .group_by(StudioEntityRow.status)
        )
    }
    reviewed = int(
        session.scalar(
            select(func.count()).where(
                *candidates,
                StudioEntityRow.payload["review"]["decision"].as_string().in_(["accept", "reject"]),
            )
        )
        or 0
    )
    returned = int(
        session.scalar(
            select(func.count()).where(
                *candidates,
                StudioEntityRow.payload["media_id"].as_string().is_not(None),
            )
        )
        or 0
    )
    pending_review = int(
        session.scalar(
            select(func.count()).where(
                *candidates,
                StudioEntityRow.payload["media_id"].as_string().is_not(None),
                StudioEntityRow.payload["review"]["decision"].as_string().is_(None),
                ~retired,
            )
        )
        or 0
    )
    pending_assessments = int(
        session.scalar(
            select(func.count()).where(
                *candidates,
                StudioEntityRow.payload["review"]["decision"].as_string().in_(["accept", "reject"]),
                StudioEntityRow.payload["review"]["selection_assessment"]["story_coverage"]
                .as_string()
                .is_(None),
                ~retired,
            )
        )
        or 0
    )
    selection_kind = StudioEntityRow.payload["selection"]["kind"].as_string()
    selection_counts = {
        kind: int(count)
        for kind, count in session.execute(
            select(selection_kind, func.count())
            .join(
                clip_rows,
                (clip_rows.project_id == StudioEntityRow.project_id)
                & (clip_rows.kind == "clips")
                & (clip_rows.payload["accepted_candidate_id"].as_string() == StudioEntityRow.id),
            )
            .where(*candidates)
            .group_by(selection_kind)
        )
    }
    job_conditions = (
        StudioEntityRow.project_id == project.id,
        StudioEntityRow.kind == "jobs",
    )
    job_counts = {
        f"{kind}:{status}": int(count)
        for kind, status, count in session.execute(
            select(StudioEntityRow.subtype, StudioEntityRow.status, func.count())
            .where(*job_conditions)
            .group_by(StudioEntityRow.subtype, StudioEntityRow.status)
        )
    }
    live_jobs = int(
        session.scalar(
            select(func.count()).where(
                *job_conditions,
                StudioEntityRow.status.in_(["queued", "submitting", "running", "ambiguous"]),
                ~_terminal_recovery(StudioEntityRow),
            )
        )
        or 0
    )
    technical_dispositions = int(
        session.scalar(select(func.count()).where(*candidates, retired)) or 0
    )
    technical, query_failures, terminal_recoveries = session.execute(
        select(
            func.coalesce(
                func.sum(
                    func.coalesce(
                        func.json_array_length(
                            StudioEntityRow.payload, "$.result.technical_retry_history"
                        ),
                        0,
                    )
                ),
                0,
            ),
            func.coalesce(func.sum(StudioEntityRow.payload["query_error_count"].as_integer()), 0),
            func.coalesce(
                func.sum(case((_terminal_recovery(StudioEntityRow), 1), else_=0)),
                0,
            ),
        ).where(*job_conditions)
    ).one()
    candidate_progress = session.execute(
        select(
            func.max(StudioEntityRow.payload["review"]["created_at"].as_string()),
            func.max(StudioEntityRow.payload["selection"]["created_at"].as_string()),
        ).where(*candidates)
    ).one()
    job_progress = session.execute(
        select(
            func.max(StudioEntityRow.payload["completed_at"].as_string()),
            func.max(StudioEntityRow.payload["submitted_at"].as_string()),
        ).where(*job_conditions)
    ).one()
    progress_times = [
        stamp
        for stamp in [
            *(actor.last_progress_at for actor in project.actors.values()),
            *candidate_progress,
            *job_progress,
        ]
        if stamp
    ]
    waiting = [actor for actor in project.actors.values() if actor.status == "waiting"]
    waiting.sort(key=lambda actor: (actor.set_id or "", actor.id))
    waits = []
    for actor in waiting[:detail_limit]:
        state = wait_state(project, actor)
        targets = state.get("targets", [])
        waits.append(
            {
                "actor_id": actor.id,
                "set_id": actor.set_id,
                **state,
                "targets": targets[:detail_limit],
                "target_count": len(targets),
                "targets_truncated": len(targets) > detail_limit,
            }
        )
    selected = sum(bool(clip.accepted_candidate_id) for clip in active_clips)
    failed_clips = sum(bool(getattr(clip, "terminal_failure", None)) for clip in active_clips)
    complete = bool(
        project.status == "complete"
        and project.final_media_id
        and active_clips
        and selected == len(active_clips)
        and not pending_review
        and not pending_assessments
        and not live_jobs
        and not failed_clips
    )
    return {
        "policy": "coherent-v1",
        "delivery_state": (
            "complete"
            if complete
            else "inconsistent_completion"
            if project.status == "complete"
            else project.status
        ),
        "counts": {
            "planned_clips": len(active_clips),
            "selected_clips": selected,
            "failed_clips": failed_clips,
            "generated_candidates": returned,
            "reviewed_candidates": reviewed,
            "pending_review_candidates": pending_review,
            "pending_selection_assessments": pending_assessments,
            "generating_candidates": candidate_states.get("generating", 0),
            "technical_dispositions": technical_dispositions,
            "active_job_obligations": live_jobs,
            "quality_passed_selections": selection_counts.get("quality_passed", 0),
            "director_compromise_selections": selection_counts.get("director_compromise", 0),
            "system_fallback_selections": selection_counts.get("system_fallback", 0),
            "user_selections": selection_counts.get("user_selection", 0),
            "selected_without_kind": selection_counts.get(None, 0),
            "waiting_actors": len(waiting),
        },
        "jobs": job_counts,
        "actors": dict(Counter(actor.status for actor in project.actors.values())),
        "technical_recovery": {
            "retry_records": int(technical),
            "current_query_error_counts": int(query_failures),
            "terminal_recoveries": int(terminal_recoveries),
            "polls_are_retries": False,
        },
        "last_meaningful_at": max(progress_times) if progress_times else None,
        "waits": waits,
        "waits_truncated": len(waiting) > detail_limit,
        "detail_limit": detail_limit,
    }
