"""Separate raw visual evidence from the director's decision used for continuity."""

from __future__ import annotations

from typing import Any

from .contracts import Actor, Candidate, EndState, EndStateConfirmation, StudioProject
from .memory import require
from .store import StudioStore
from .workflow import SELF_REVIEW


def actual_end_state(candidate: Candidate) -> EndState | None:
    confirmation = candidate.confirmed_end_state
    return confirmation.end_state if confirmation else candidate.end_state


def confirm_end_state(
    p: StudioProject,
    actor: Actor,
    candidate: Candidate,
    data: dict[str, Any],
    store: StudioStore,
    *,
    required: bool = False,
) -> None:
    supplied = data.get("final_end_state")
    if supplied is None:
        require(
            not required or candidate.confirmed_end_state,
            "请查看实际末帧并提交 final_end_state，明确接续采用的动作、空间、机位和人物状态",
        )
        return
    clip = p.clips[candidate.clip_id]
    delegated_review = False
    if actor.role == "task_analyst":
        from .delegation import task_for

        task = task_for(p, actor)
        delegated_review = bool(
            task.request.kind == "candidate_review"
            and candidate.id in task.allowed_scope.candidate_ids
            and task.status in {"queued", "running"}
        )
    require(
        actor.role == "executive"
        or actor.role == "set_director"
        and actor.set_id == clip.set_id
        or delegated_review,
        "只能由负责片场或获准候选审核委托确认实际末态",
    )
    frame_id = (
        candidate.analysis.get("end_frame_media_id")
        if (candidate.analysis.get("check_version") == SELF_REVIEW)
        else candidate.end_state.frame_media_id
        if candidate.end_state
        else None
    )
    require(frame_id and candidate.media_id, "候选缺少可播放视频与实际末帧")
    state = EndState.model_validate(supplied)
    require(
        state.frame_media_id == frame_id,
        "确认必须保留该候选实际末帧，不能替换为参考图",
    )
    require(
        all(value.strip() for value in (state.action, state.space, state.camera, state.identity)),
        "实际末态的动作、空间、机位和人物描述不能为空",
    )
    reason = str(data.get("end_state_reason") or data.get("reason") or "").strip()
    require(reason, "确认或纠正末态需要说明画面依据及与模型观察的差异")
    evidence = list(dict.fromkeys(data.get("end_state_media_ids") or [state.frame_media_id]))
    from .image_input_recovery import current_evidence, inspected_evidence, quarantine, review_gaps

    gaps = review_gaps(p, candidate)
    frame_missing = quarantine(p, state.frame_media_id) is not None
    allowed = set(candidate.analysis.get("required_review_media_ids", [])) | {state.frame_media_id}
    allowed = {media_id for media_id in allowed if not quarantine(p, media_id)}
    require(
        evidence
        and (frame_missing or state.frame_media_id in evidence)
        and (not frame_missing or set(evidence) & current_evidence(p, candidate))
        and set(evidence) <= allowed,
        "末态依据必须包含实际末帧，且来自当前候选的检查画面",
    )
    require(
        set(evidence) <= inspected_evidence(p, actor, evidence, store),
        "请先 read_media 查看末态依据，不能仅复述模型或剧本",
    )
    visual_ids = list(dict.fromkeys(data.get("visible_state_ids", [])))
    if frame_missing:
        require(not visual_ids, "实际末帧未审，不能认证末帧可见资产状态；请记录暂定描述和未知项")
        reason = "有限末态判断：实际末帧未审，仅依据其余已看证据；" + reason
        for key in ("action", "space", "camera", "identity"):
            setattr(state, key, "末帧未审，暂定描述：" + getattr(state, key))
    allowed_states = {b.state_id for b in candidate.bindings}
    source_job = p.jobs.get(candidate.job_id)
    plans = source_job.request.get("asset_plan", []) if source_job else []
    for plan in plans:
        allowed_states.update(
            i for i in (plan.get("start_state_id"), plan.get("end_state_id")) if i
        )
    allowed_states.update(
        i for plan in clip.asset_plan for i in (plan.start_state_id, plan.end_state_id) if i
    )
    allowed_states = {
        i for i in allowed_states if p.assets[p.states[i].asset_id].purpose == "story"
    }
    require(
        set(visual_ids) <= allowed_states,
        "只能确认本候选绑定或计划内实际可见的状态；新的全局状态交总导演处理",
    )
    assets = [p.states[i].asset_id for i in visual_ids]
    require(len(assets) == len(set(assets)), "实际末态中每项资产只能确认一个状态")
    from .visual_analysis import delegation_receipts

    confirmation = EndStateConfirmation(
        end_state=state,
        actor_id=actor.id,
        reason=reason,
        evidence_media_ids=evidence,
        visual_state_ids=visual_ids,
        analysis_request_id=candidate.analysis.get("request_id"),
        coverage_gaps=gaps,
        delegated_task_ids=delegation_receipts(p, actor, evidence, store),
    )
    candidate.confirmed_end_state = confirmation
    candidate.end_state_history.append(confirmation)
    candidate.end_state = state


def confirm_candidate_state(
    p: StudioProject, actor: Actor, data: dict[str, Any], store: StudioStore
) -> dict[str, Any]:
    candidate = p.candidates.get(str(data.get("candidate_id")))
    require(
        candidate and candidate.status in {"review", "rejected", "accepted"},
        "候选尚无可核对的实际结果",
    )
    assert candidate is not None
    confirm_end_state(p, actor, candidate, data, store, required=True)
    if p.clips[candidate.clip_id].accepted_candidate_id == candidate.id:
        from .memory import record_selected_outcome

        record_selected_outcome(p, candidate, actor.id, refresh=True)
    return {
        "candidate_id": candidate.id,
        "summary": "已确认接续采用的实际末态；原模型报告和已提交任务保持可追溯",
    }
