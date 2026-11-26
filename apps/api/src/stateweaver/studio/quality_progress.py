"""One read-only quality ledger for model tools, scheduling and user projections."""

from __future__ import annotations

from typing import Any

from .contracts import (
    Clip,
    QualityBlocker,
    QualityCounts,
    QualityProgress,
    ReadinessItem,
    StudioProject,
)
from .harness import video_extra_limit
from .repository import select_entities

LIVE = {"queued", "submitting", "running", "ambiguous"}


def lineage(project: StudioProject, clip: Clip) -> list[Clip]:
    pending, seen, result = [clip.id], set(), []
    while pending:
        identifier = pending.pop()
        if identifier in seen:
            continue
        seen.add(identifier)
        parent = project.clips.get(identifier)
        if parent is not None and parent.set_id == clip.set_id:
            result.append(parent)
            pending.extend(parent.plan_parent_ids)
    return result


def quality_progress(
    project: StudioProject,
    clip: Clip,
    *,
    readiness_items: list[ReadinessItem] | None = None,
) -> QualityProgress:
    """Count completed outcomes, not registered intentions or unrelated API retries."""
    from .candidate_lifecycle import candidate_disposition_valid, terminal_recovery

    counts = {name: QualityCounts() for name in ("initial", "ordinary", "executive")}
    ancestors = lineage(project, clip)
    attempts = {item.id: item for ancestor in ancestors for item in ancestor.repair_attempts}
    generated: set[str] = set()
    reviewed: set[str] = set()
    active_jobs: list[str] = []
    review_candidates: list[str] = []
    for ancestor in ancestors:
        previous_kind = "initial"
        for candidate in sorted(
            select_entities(project.candidates, parent_id=ancestor.id),
            key=lambda item: (item.created_at, item.id),
        ):
            job = project.jobs.get(candidate.job_id)
            if job is None:
                continue
            attempt = attempts.get(str(job.request.get("repair_attempt_id", "")))
            kind = (
                attempt.kind if attempt else str(job.request.get("quality_attempt_kind", "initial"))
            )
            kind = ancestor.quality_accounting.get(job.id, kind)
            if kind == "technical":
                kind = previous_kind
            if kind not in counts:
                kind = "initial"
            previous_kind = kind
            key = attempt.id if attempt else candidate.id
            if job.status == "complete" and candidate.media_id:
                if key not in generated:
                    counts[kind].generations += 1
                    generated.add(key)
                has_review = candidate.review is not None
                if has_review and key not in reviewed:
                    counts[kind].reviews += 1
                    reviewed.add(key)
                elif (
                    not has_review
                    and ancestor.id == clip.id
                    and not candidate_disposition_valid(project, candidate)
                ):
                    review_candidates.append(candidate.id)
            if ancestor.id == clip.id:
                active_jobs.extend(
                    item.id
                    for item in select_entities(project.jobs, parent_id=candidate.id)
                    if item.status in LIVE and not terminal_recovery(item)
                )
    pending = [item for item in clip.repair_attempts if not item.job_ids and not item.cancelled_at]
    episode = clip.quality_episode
    limit = episode.extra_limit if episode else video_extra_limit(project)
    granted, used = (episode.extra_granted, episode.extra_used) if episode else (0, 0)
    owner = project.sets[clip.set_id].actor_id
    executive = next((a.id for a in select_entities(project.actors, subtype="executive")), None)
    progress = QualityProgress(
        initial=counts["initial"],
        ordinary=counts["ordinary"],
        executive=counts["executive"],
        ordinary_limit=project.config.repairs_per_clip,
        ordinary_registered=clip.repair_count,
        ordinary_remaining=max(0, project.config.repairs_per_clip - clip.repair_count),
        extra_limit=limit,
        extra_approved=granted,
        extra_used=used,
        extra_approved_unused=max(0, granted - used),
        extra_grantable=max(0, limit - granted),
        pending_repairs=pending,
        responsible_actor_id=owner,
    )

    def block(
        code: str,
        reason: str,
        ids: list[str],
        operations: list[str],
        who: str | None = owner,
        details: dict[str, Any] | None = None,
    ) -> None:
        progress.blockers.append(
            QualityBlocker(
                code=code,
                reason=reason,
                related_ids=ids,
                operations=operations,
                owner_actor_id=who,
                details=details or {},
            )
        )

    if clip.archived_at or project.deleted_at:
        block(
            "archived",
            "已归档的记录保留历史，不再提交新任务",
            [clip.id],
            ["generate_clip", "repair_clip"],
        )
        return progress
    if clip.terminal_failure:
        block(
            "production_failed",
            clip.terminal_failure.reason,
            clip.terminal_failure.job_ids,
            [
                "generate_clip",
                "repair_clip",
                "resolve_quality",
                "finish_set",
                "assemble",
            ],
            details={"failure": clip.terminal_failure.model_dump(mode="json")},
        )
        return progress
    if active_jobs:
        block(
            "jobs_pending",
            "先回收当前生成、审片或提交待核对的任务",
            sorted(set(active_jobs)),
            ["generate_clip", "repair_clip", "resolve_quality"],
        )
    if review_candidates:
        block(
            "review_pending",
            "已有可播放候选尚未完成审核",
            review_candidates,
            ["generate_clip", "repair_clip", "resolve_quality"],
        )
        progress.allowed_operations.append("review_candidate")
    from .candidate_lifecycle import clip_pending_work

    settlement_pending = clip_pending_work(project, clip)
    if settlement_pending:
        block(
            "candidate_settlement_pending",
            "Complete all submitted candidate work and necessary reviews before final selection.",
            list(dict.fromkeys(str(item["entity_id"]) for item in settlement_pending)),
            ["resolve_quality", "finish_set", "assemble"],
            details={"pending_work": settlement_pending},
        )
    if clip.accepted_candidate_id:
        if not settlement_pending:
            progress.allowed_operations.append("continue_next_clip")
        return progress
    if readiness_items is None:
        from .production_reads import clip_readiness

        readiness_items = clip_readiness(project, clip.id, _include_quality=False).items
    for item in readiness_items:
        if item.kind != "job":  # The quality ledger already identifies all live jobs above.
            block(
                item.kind,
                item.description,
                [item.entity_id] if item.entity_id else [],
                ["generate_clip"],
                item.owner_id,
                item.details,
            )
    pending_attempt = attempts.get(clip.pending_repair_id or "")
    if pending_attempt and pending_attempt.cancelled_at:
        pending_attempt = None
    if (
        counts["ordinary"].generations > 0
        and counts["ordinary"].generations >= project.config.repairs_per_clip
        and (not counts["initial"].generations)
    ):
        block(
            "quality_accounting_conflict",
            "普通修正已有生成但缺少首版记录，请核对原始任务及规划继承，不得重复扣费生成",
            [clip.id],
            ["generate_clip", "repair_clip"],
            executive,
        )
    if (
        pending_attempt
        and pending_attempt.kind == "executive"
        and not pending_attempt.reinforcement_revision_ids
    ):
        block(
            "new_reference_required",
            "本次追加必须生成、检查并采用针对缺陷的新素材",
            [pending_attempt.id],
            ["generate_clip"],
            executive,
        )
        progress.allowed_operations.append("supply_repair_references")
    if clip.status == "escalated":
        progress.responsible_actor_id = executive
        if not active_jobs and not review_candidates and not settlement_pending:
            progress.allowed_operations.append("resolve_quality")
    elif (
        clip.status in {"repair", "blocked"}
        and not active_jobs
        and not review_candidates
        and not any("repair_clip" in item.operations for item in progress.blockers)
    ):
        progress.allowed_operations.append("repair_clip")
    if clip.status == "planned":
        progress.allowed_operations.append("prepare_references")
        if not any("generate_clip" in item.operations for item in progress.blockers):
            progress.allowed_operations.append("generate_clip")
    actor = project.actors.get(owner)
    if actor and actor.paused:
        block(
            "paused",
            "片场导演已暂停，保留当前进度",
            [owner],
            list(progress.allowed_operations),
        )
        progress.allowed_operations = []
    return progress
