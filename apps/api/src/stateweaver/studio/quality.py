"""Finite creative repair episodes and executive decisions, separate from API failures."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .contracts import (
    Actor,
    Clip,
    QualityDecision,
    QualityEpisode,
    RepairPlan,
    SelectionRecord,
    StudioProject,
)
from .harness import video_extra_limit
from .repository import select_entities
from .store import StudioError

if TYPE_CHECKING:
    from .service import StudioService

EXECUTIVE_COMPROMISE_POLICY = "reviewed-candidate-no-quota-v1"


def ensure(condition: object, message: str) -> None:
    if not condition:
        raise StudioError(message)


def notify_executive(project: StudioProject, reason: str) -> None:
    for actor in project.actors.values():
        if (
            actor.role == "executive"
            and actor.status != "complete"
            and not actor.native_session_blocked
        ):
            actor.summary = reason
            actor.request_wake()
            if not actor.paused and actor.status != "running":
                actor.status = "ready"


def clip_episode(project: StudioProject, clip: Clip) -> QualityEpisode:
    if clip.quality_episode is None:
        clip.quality_episode = QualityEpisode(
            ordinary_attempts=clip.repair_count, extra_limit=video_extra_limit(project)
        )
    return clip.quality_episode


def ordinary_clip_corrections_complete(project: StudioProject, clip: Clip) -> bool:
    """An allocated counter alone does not prove a correction was generated/reviewed.

    Include archived planning ancestors so repartitioning preserves completed work.
    Technical retries count only when they finally produce a reviewed playable result.
    """
    limit = project.config.repairs_per_clip
    from .quality_progress import quality_progress

    progress = quality_progress(project, clip)
    return (
        clip.repair_count >= limit
        and progress.initial.generations >= 1
        and progress.initial.reviews >= 1
        and progress.ordinary.generations >= limit
        and progress.ordinary.reviews >= limit
    )


def require_ordinary_clip_corrections(project: StudioProject, clip: Clip) -> None:
    ensure(
        ordinary_clip_corrections_complete(project, clip),
        f"常规修正尚未完成：需先完成首次生成和 {project.config.repairs_per_clip} 次常规修正的"
        "生成与检查，再追加或妥协选用；请先处理现有任务/候选，或调用 repair_clip 继续定向修正。"
        "技术失败、只安排修正或重划计划不能代替已完成的修正。",
    )


def escalate_clip(project: StudioProject, actor: Actor, clip: Clip, reason: str) -> bool:
    require_ordinary_clip_corrections(project, clip)
    episode = clip_episode(project, clip)
    episode.ordinary_attempts = max(episode.ordinary_attempts, clip.repair_count)
    changed = episode.status != "escalated"
    if episode.status != "escalated":
        episode.decisions.append(
            QualityDecision(actor_id=actor.id, action="escalate", reason=reason)
        )
    episode.status = "escalated"
    clip.status = "escalated"
    production_set = project.sets[clip.set_id]
    production_set.status = "producing"
    director = project.actors[production_set.actor_id]
    director.summary = "普通修正已用尽，等待总导演比较候选并给出方案"
    director.wake_requested = False
    if not director.paused:
        director.status = "waiting"
    issue = "quality:clip:" + clip.id
    if issue not in project.issues:
        project.issues.append(issue)
    if changed:
        comparison = "比较画面证据与导演结论"
        notify_executive(
            project, f"{clip.title} 需要质量接管：{comparison}，有限追加或明确妥协选用"
        )
    return changed


def image_episode(project: StudioProject, state_id: str, view: str) -> QualityEpisode:
    state = project.states[state_id]
    if view not in state.repair_episodes:
        state.repair_episodes[view] = QualityEpisode(
            extra_limit=project.config.executive_extra_attempts
        )
    return state.repair_episodes[view]


def ordinary_image_corrections_complete(project: StudioProject, state_id: str, view: str) -> bool:
    episode = project.states[state_id].repair_episodes.get(view)
    limit = project.config.repairs_per_reference
    if episode is None or episode.ordinary_attempts < limit:
        return False
    reviewed = 0
    for reference in select_entities(project.references, parent_id=state_id):
        if reference.view != view or not reference.media_id:
            continue
        if not reference.review_history and reference.selection is None:
            continue
        job = project.jobs.get(reference.job_id) if reference.job_id else None
        if job and (
            job.status != "complete" or job.request.get("quality_attempt_kind") == "executive"
        ):
            continue
        reviewed += 1
    return reviewed >= limit + 1


def require_ordinary_image_corrections(project: StudioProject, state_id: str, view: str) -> None:
    ensure(
        ordinary_image_corrections_complete(project, state_id, view),
        f"常规修正尚未完成：同一状态和视图需先完成首次生成和 "
        f"{project.config.repairs_per_reference} 次常规修正的生成与检查，再追加或妥协选用；"
        "请先回收/检查已有图片，或调用 generate_reference 继续定向修正。",
    )


def escalate_image(
    project: StudioProject, actor: Actor, state_id: str, view: str, reason: str
) -> bool:
    require_ordinary_image_corrections(project, state_id, view)
    episode = image_episode(project, state_id, view)
    changed = episode.status != "escalated"
    if episode.status != "escalated":
        episode.decisions.append(
            QualityDecision(actor_id=actor.id, action="escalate", reason=reason)
        )
    episode.status = "escalated"
    issue = f"quality:reference:{state_id}:{view}"
    if issue not in project.issues:
        project.issues.append(issue)
    if changed:
        notify_executive(project, "参考图普通修正已用尽，请调整方案或明确选择已有候选")
    return changed


def allocate(
    episode: QualityEpisode,
    actor: Actor,
    data: dict[str, Any],
    *,
    project: StudioProject | None = None,
) -> None:
    attempts = int(data.get("extra_attempts", 1))
    ensure(
        attempts > 0 and episode.extra_granted + attempts <= episode.extra_limit,
        "追加总次数已达上限，请妥协选用已有可用候选；更换方案不能重置次数",
    )
    ensure(data.get("fallback_id"), "追加尝试前请指定可接受的最终兜底候选 fallback_id")
    plan = RepairPlan.model_validate(data.get("repair_plan", {}))
    ensure(
        plan.defects and plan.preserve and plan.change.strip(),
        "追加方案需要列出 defects、preserve 和 change，避免把已经正确的内容改错",
    )
    episode.extra_granted += attempts
    episode.fallback_id = str(data["fallback_id"])
    episode.status = "intervening"
    episode.plan = plan
    episode.decisions.append(
        QualityDecision(
            actor_id=actor.id,
            action="retry",
            reason=str(data["reason"]),
            attempts=attempts,
            candidate_id=data.get("clip_id") and str(data["fallback_id"]),
            reference_id=data.get("state_id") and str(data["fallback_id"]),
            deviations=list(data.get("deviations", [])),
        )
    )


def quality_waiting(project: StudioProject, clip: Clip) -> bool:
    """Check whether a Clip is waiting for pending jobs or review work."""
    from .candidate_lifecycle import clip_pending_work

    return bool(clip_pending_work(project, clip))


def record_observed_states(
    project: StudioProject, clip: Clip, data: dict[str, Any]
) -> dict[str, str]:
    observations = data.get("observed_asset_states", {})
    ensure(isinstance(observations, dict), "实际资产状态应按资产记录观察说明")
    ensure(
        all(
            identifier in project.assets and isinstance(value, str) and value.strip()
            for identifier, value in observations.items()
        ),
        "实际状态只能记录本项目的资产，且必须说明实际观察到的内容",
    )
    planned = {item.asset_id for item in clip.asset_plan}
    ensure(
        all(
            identifier in planned and project.assets[identifier].purpose == "story"
            for identifier in observations
        ),
        "实际剧情状态只能记录本 Clip 计划内的剧情资产，不能把辅助白模或目标画面当成已发生事实",
    )
    return dict(observations)


def resolve_quality(
    service: StudioService, p: StudioProject, a: Actor, data: dict[str, Any]
) -> dict[str, Any]:
    ensure(a.role == "executive", "只有总导演能够追加质量尝试或妥协选用")
    ensure(data.get("reason"), "总导演需要记录接管理由和方案")
    ensure(data.get("decision") in {"retry", "select"}, "接管决策需要 retry 或 select")
    ensure(
        bool(data.get("clip_id")) != bool(data.get("state_id")),
        "需要明确接管一个 Clip 或参考视图",
    )
    if data.get("clip_id"):
        clip = p.clips.get(str(data["clip_id"]))
        ensure(clip is not None, "接管 Clip 不存在")
        assert clip is not None
        from .candidate_lifecycle import (
            cancel_unsubmitted_repair_reservations,
            clip_pending_work,
            selectable_candidates,
        )
        from .production_decisions import apply_selection

        selecting = data["decision"] == "select"
        if selecting and (
            clip.accepted_candidate_id
            or p.deleted_at
            or p.status in {"complete", "failed"}
            or p.sets[clip.set_id].status in {"complete", "failed"}
        ):
            return apply_selection(
                p,
                a,
                clip,
                {
                    "candidate_id": str(data.get("candidate_id", "")),
                    "reason": str(data["reason"]),
                    "deviations": list(data.get("deviations", [])),
                },
                kind="director_compromise",
            )
        episode = clip_episode(p, clip)
        if not selecting:
            ensure(episode.status in {"escalated", "intervening"}, "Clip 尚未进入质量接管")
            require_ordinary_clip_corrections(p, clip)
        if selecting:
            ensure(
                not [
                    item
                    for item in clip_pending_work(p, clip)
                    if item["kind"] != "repair_reservation"
                ],
                "请先回收本 Clip 已提交的生成与检查结果",
            )
        else:
            ensure(not quality_waiting(p, clip), "请先回收本 Clip 的生成与检查结果")
        usable = {candidate.id: candidate for candidate in selectable_candidates(p, clip)}
        if not selecting:
            ensure(
                data.get("fallback_id") in usable,
                "兜底候选需要可播放视频和实际末帧状态",
            )
            from .adjudication import confirm_end_state

            fallback = usable[str(data["fallback_id"])]
            confirm_end_state(p, a, fallback, data, service.store)
            allocate(episode, a, data, project=p)
            service._repair_clip(
                p,
                a,
                {
                    "clip_id": clip.id,
                    "reason": data["reason"],
                    "repair_plan": data.get("repair_plan", {}),
                },
            )
            return {
                "clip_id": clip.id,
                "summary": data["reason"],
                "extra_remaining": episode.extra_granted - episode.extra_used,
            }
        identifier = str(data.get("candidate_id", ""))
        ensure(identifier in usable, "请明确选择本 Clip 中可播放且有实际末帧的候选")
        candidate = usable[identifier]
        production_set = p.sets[clip.set_id]
        if clip.order:
            previous = p.clips[production_set.clip_ids[clip.order - 1]]
            ensure(
                candidate.previous_candidate_id == previous.accepted_candidate_id,
                "候选不是从当前已选前段接续的，请先处理依赖来源",
            )
        deviations = list(data.get("deviations", []))
        ensure(deviations, "妥协选用需要明确记录保留的缺陷或剧情偏差")
        from .adjudication import confirm_end_state

        confirm_end_state(p, a, candidate, data, service.store)
        cancelled_reservations = cancel_unsubmitted_repair_reservations(
            p,
            clip,
            reason="Executive selected a reviewed playable compromise: " + str(data["reason"]),
        )
        if "observed_asset_states" in data:
            candidate.observed_asset_states = record_observed_states(p, clip, data)
        apply_selection(
            p,
            a,
            clip,
            {
                "candidate_id": candidate.id,
                "reason": str(data["reason"]),
                "deviations": deviations,
            },
            kind="director_compromise",
        )
        director = p.actors[production_set.actor_id]
        if not director.native_session_blocked:
            director.summary = "总导演已选用候选，继续下一个连续 Clip"
            director.request_wake()
            if not director.paused and director.status != "running":
                director.status = "ready"
        if any(
            asset.voice_bootstrap_clip_id == clip.id and not asset.selected_voice_id
            for asset in p.assets.values()
        ):
            notify_executive(p, "人物首个发声 Clip 已选用，请提取并固定音色基准")
        return {
            "clip_id": clip.id,
            "candidate_id": candidate.id,
            "selection_kind": "director_compromise",
            "summary": data["reason"],
            "deviations": deviations,
            "cancelled_repair_reservations": cancelled_reservations,
        }
    state_id, view = str(data["state_id"]), str(data.get("view", ""))

    coherent_selection = data["decision"] == "select"
    ensure(
        state_id in p.states and (coherent_selection or view in p.states[state_id].repair_episodes),
        "参考图接管记录不存在",
    )
    episode = image_episode(p, state_id, view)
    if not coherent_selection:
        ensure(episode.status in {"escalated", "intervening"}, "参考视图尚未进入质量接管")
        require_ordinary_image_corrections(p, state_id, view)
        ensure(
            not any(
                r.view == view and r.status == "pending"
                for r in select_entities(p.references, parent_id=state_id)
            ),
            "请先回收此视图已提交的图片结果",
        )
    references = {
        r.id: r
        for r in select_entities(p.references, parent_id=state_id)
        if r.view == view and r.media_id and r.status in {"candidate", "selected", "rejected"}
    }
    if data["decision"] == "retry":
        ensure(data.get("fallback_id") in references, "兜底参考图不属于此状态和视图")
        allocate(episode, a, data, project=p)
        return {
            "state_id": state_id,
            "view": view,
            "summary": data["reason"],
            "extra_remaining": episode.extra_granted - episode.extra_used,
        }
    reference = references.get(str(data.get("revision_id")))
    ensure(reference is not None, "请明确选择此状态和视图的已生成参考图")
    assert reference is not None
    ensure(data.get("deviations"), "图片妥协需要记录保留的缺陷")
    from .visual_analysis import delegation_receipts

    reference.selection = SelectionRecord(
        kind="director_compromise",
        actor_id=a.id,
        reason=data["reason"],
        deviations=data["deviations"],
        delegated_task_ids=delegation_receipts(p, a, [str(reference.media_id)], service.store),
    )
    reference.status = "candidate"
    outcome = service._select_reference(p, a, {"revision_id": reference.id})
    episode.status = "resolved"
    episode.decisions.append(
        QualityDecision(
            actor_id=a.id,
            action="compromise",
            reason=data["reason"],
            reference_id=reference.id,
            deviations=data["deviations"],
        )
    )
    issue = f"quality:reference:{state_id}:{view}"
    if issue in p.issues:
        p.issues.remove(issue)
    return {
        **outcome,
        "selection_kind": "director_compromise",
        "summary": data["reason"],
    }
