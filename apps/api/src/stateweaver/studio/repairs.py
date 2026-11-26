"""Versioned video repair reservations and per-attempt reference reinforcement."""

from __future__ import annotations

from typing import Any, Literal

from .contracts import Actor, Clip, ReferencePlan, RepairAttempt, RepairPlan, StudioProject, now
from .memory import require
from .store import StudioStore


def pending_attempt(clip: Clip) -> RepairAttempt | None:
    return next(
        (
            item
            for item in clip.repair_attempts
            if item.id == clip.pending_repair_id and not item.cancelled_at
        ),
        None,
    )


def replan_repair_count(project: StudioProject, parents: list[str]) -> int:
    """Only this replacement's parents own its correction budget, never its siblings."""
    sources = [project.clips[identifier] for identifier in parents]
    return max((clip.repair_count for clip in sources), default=0) + int(
        replan_reserves_correction(project, sources)
    )


def replan_reserves_correction(project: StudioProject, clips: list[Clip]) -> bool:
    """A rendered rejected outcome needs a new correction; editing a plan does not."""
    reserve = False
    for clip in clips:
        for identifier in clip.candidate_ids:
            candidate = project.candidates[identifier]
            require(
                not candidate.media_id or candidate.review is not None,
                "重排前需完成已有候选的审核，不能用拆分计划跳过审核或重置额度",
            )
        if not clip.candidate_ids:
            continue
        latest = project.candidates[clip.candidate_ids[-1]]
        attempt = pending_attempt(clip)
        if attempt and not any(
            project.jobs[job_id].status == "complete" for job_id in attempt.job_ids
        ):
            continue  # Carry the registered, still unrendered correction.
        reserve |= bool(latest.media_id)
    return reserve


def inherit_replan_attempt(
    project: StudioProject, actor: Actor, clip: Clip, data: dict[str, Any]
) -> None:
    """Keep archived ledgers intact and bind the replacement's pending reservation."""
    if not clip.repair_count:
        return
    from .quality_progress import lineage

    ancestors = lineage(project, clip)[1:]
    source = next((c for c in ancestors if c.repair_plan), None)
    plan = (
        source.repair_plan.model_copy(deep=True) if source and source.repair_plan else RepairPlan()
    )
    # An archived candidate remains diagnostic history, not an editable video for
    # a newly split Clip. The new plan inherits preserved facts and defect notes.
    plan.mode, plan.source_candidate_id = "regenerate", None
    plan.change = str(data.get("reason") or clip.repair_reason or plan.change)
    plan.defects = plan.defects or [plan.change]
    plan.preserve = plan.preserve or ["已确认剧本、已选前段真实末态及对应资产状态"]
    clip.repair_plan, clip.pending_attempt_kind = plan, "ordinary"
    register_attempt(project, actor, clip, reuse=False)


def transfer_replanned_reservations(
    project: StudioProject, originals: list[Clip], replacements: list[Clip], reason: str
) -> None:
    """Retire only an unsubmitted reservation inherited by explicit replacements.

    Keep the archived ledger and record its successor reservation IDs. Submitted
    jobs, candidate reviews and quality lineage remain independent obligations.
    """

    for original in originals:
        reservation = pending_attempt(original)
        if reservation is None or reservation.job_ids:
            continue
        successors = [
            (clip.id, attempt.id)
            for clip in replacements
            if original.id in clip.plan_parent_ids
            and (attempt := pending_attempt(clip)) is not None
        ]
        require(successors, "A replanned repair reservation must have an inherited successor.")
        reservation.cancelled_at = now()
        reservation.cancellation_reason = (
            "Unsubmitted reservation transferred by replanning to "
            + ", ".join(
                f"Clip {clip_id} / reservation {attempt_id}" for clip_id, attempt_id in successors
            )
            + "; "
            + reason
        )


def register_attempt(project: StudioProject, actor: Actor, clip: Clip, *, reuse: bool) -> None:
    plan = clip.repair_plan
    assert plan is not None
    require(
        plan.defects and plan.preserve and plan.change.strip(),
        "修复需列出 defects、preserve 和 change",
    )
    if plan.mode == "edit":
        source = project.candidates.get(plan.source_candidate_id or "")
        require(
            source
            and source.clip_id == clip.id
            and source.media_id
            and source.review
            and project.jobs[source.job_id].status == "complete",
            "编辑来源必须是本 Clip 已生成并审核过的完整候选；先分析缺陷再决定编辑",
        )
    existing = pending_attempt(clip)
    if reuse and existing:
        existing.plan = plan.model_copy(deep=True)
        clip.pending_attempt_kind = existing.kind
        return
    if clip.pending_attempt_kind == "technical":
        return  # Initial technical retry has no quality-correction reservation.
    kind: Literal["ordinary", "executive"] = (
        "executive" if clip.pending_attempt_kind == "executive" else "ordinary"
    )
    attempt = RepairAttempt(
        kind=kind,
        ordinal=(clip.quality_episode.extra_used + 1)
        if kind == "executive" and clip.quality_episode
        else max(1, clip.repair_count),
        actor_id=actor.id,
        plan=plan.model_copy(deep=True),
    )
    clip.repair_attempts.append(attempt)
    clip.pending_repair_id = attempt.id


def supply_references(
    project: StudioProject,
    actor: Actor,
    data: dict[str, Any],
    store: StudioStore,
    *,
    authorized_task_id: str | None = None,
) -> dict[str, Any]:
    authorized = False
    if authorized_task_id:
        from .delegation import stale_reasons, task_for

        task = task_for(project, actor)
        existing = pending_attempt(project.clips[str(data["clip_id"])])
        authorized = bool(
            task.id == authorized_task_id
            and actor.role == "material_executor"
            and (task.request.kind == "material_preparation")
            and (task.status in {"queued", "running"})
            and (not stale_reasons(project, task))
            and (
                task.repair_supply_targets.get(str(data["clip_id"]))
                == data.get("repair_attempt_id")
            )
            and (
                set(data.get("revision_ids", []))
                <= set(task.output_revision_ids)
                | set(existing.reinforcement_revision_ids if existing else [])
            )
        )
    require(
        (actor.role == "executive" or authorized),
        "仅总导演或其精确预授权的素材任务可供给追加素材",
    )
    clip = project.clips[str(data["clip_id"])]
    attempt = pending_attempt(clip)
    require(
        attempt
        and attempt.kind == "executive"
        and not attempt.job_ids
        and attempt.id == data.get("repair_attempt_id"),
        "追加修复登记已变化或已提交；请读取 quality_progress 后针对当次登记补素材",
    )
    assert attempt is not None
    ids = list(dict.fromkeys(data.get("revision_ids", [])))
    require(ids and str(data.get("reason", "")).strip(), "需注明新素材及其针对的缺陷和使用指导")
    from .image_input_recovery import inspected_evidence, quarantine

    for identifier in ids:
        reference = project.references.get(identifier)
        require(
            reference and reference.media_id and reference.job_id, "补强素材必须是已生成的图片版本"
        )
        assert reference is not None and reference.media_id and reference.job_id
        require(
            reference.created_at >= attempt.registered_at
            and project.jobs[reference.job_id].status == "complete"
            and project.jobs[reference.job_id].kind == "image"
            and project.jobs[reference.job_id].target_id == reference.id
            and project.states[reference.state_id].selected_revisions.get(reference.view)
            == identifier,
            "每次追加需在登记后生成并选用新参考版本；旧素材或上次追加的素材不能重复充抵",
        )
        require(
            not quarantine(project, reference.media_id)
            and (
                reference.media_id
                in inspected_evidence(project, actor, [reference.media_id], store)
                or authorized
                and identifier in attempt.reinforcement_revision_ids
            ),
            "总导演或其授权看图子对话需实际检查新素材后才能提供给片场导演",
        )
    attempt.reinforcement_revision_ids = ids
    attempt.reinforcement_reason = str(data["reason"])
    director = project.actors[project.sets[clip.set_id].actor_id]
    if not director.native_session_blocked:
        director.request_wake()
        if not director.paused and director.status != "running":
            director.status = "ready"
    return {
        "clip_id": clip.id,
        "repair_attempt_id": attempt.id,
        "revision_ids": ids,
        "summary": data["reason"],
    }


def validate_submission(
    project: StudioProject, clip: Clip, plan: ReferencePlan
) -> RepairAttempt | None:
    from .quality_progress import quality_progress

    attempt = pending_attempt(clip)
    progress = quality_progress(project, clip, readiness_items=[])
    if attempt and attempt.kind == "ordinary":
        require(
            progress.ordinary.generations < project.config.repairs_per_clip,
            "普通修正实际生成已达上限；未提交登记不能绕过 quality_progress 的额度校验",
        )
    elif not attempt:
        require(
            not progress.initial.generations,
            "已有首版生成，请先审核并登记修正；取消的登记不能用于新提交",
        )
    if not attempt or attempt.kind != "executive":
        return attempt
    require(attempt.reinforcement_revision_ids, "本次追加尚未补强：请总导演生成、检查并发布新素材")
    used = {binding.revision_id for binding in plan.bindings}
    require(
        used.intersection(attempt.reinforcement_revision_ids),
        "本次追加的生成清单必须实际采用至少一份当次新素材，不能只登记补强说明",
    )
    return attempt
