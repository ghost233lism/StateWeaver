"""Director-authored reference drafts, reconciliation and immutable submitted recipes."""

from __future__ import annotations

from typing import Any

from stateweaver.providers.base import ProviderContractError
from stateweaver.studio.adjudication import actual_end_state
from stateweaver.studio.continuity import (
    ensure_continuation_tail,
    timed_media_reference,
    waiting_for_voices,
)
from stateweaver.studio.contracts import (
    Actor,
    Candidate,
    Clip,
    ReferenceConflict,
    ReferencePlan,
    StudioProject,
    TimedMediaBinding,
    VideoReferenceInput,
    now,
)
from stateweaver.studio.image_input_recovery import previous_endpoint_gaps
from stateweaver.studio.memory import digest, require, validate_clip_plan
from stateweaver.studio.plan_index import bucket
from stateweaver.studio.reference_inputs import video_reference
from stateweaver.studio.repository import MODELS, matches, parent, select_entities
from stateweaver.studio.store import StudioError, StudioPrerequisiteError, StudioStore
from stateweaver.studio.task_context import context_fingerprint, require_context
from stateweaver.studio.workflow import CONTINUATION_SECONDS
from stateweaver.studio_media.h3_prompt import prepare_prompt
from stateweaver.studio_media.reference_crop import valid_h3_image_aspect


def owned_clip(p: StudioProject, actor: Actor, identifier: str) -> Clip:
    require(actor.role == "set_director", "参考清单由片场导演负责")
    clip = p.clips.get(identifier)
    require(clip and clip.set_id == actor.set_id and not clip.archived_at, "Clip 不属于当前片场")
    assert clip is not None
    return clip


def predecessor(p: StudioProject, clip: Clip) -> Candidate | None:
    if not clip.order:
        return None
    previous_clip = p.clips[p.sets[clip.set_id].clip_ids[clip.order - 1]]
    require(previous_clip.accepted_candidate_id, "必须先验收前一个 Clip")
    previous = p.candidates[str(previous_clip.accepted_candidate_id)]
    require(actual_end_state(previous) is not None, "前一 Clip 缺少实际末帧及动作空间状态")
    require(
        (previous.confirmed_end_state),
        "前一 Clip 的末态尚需导演确认，请查看画面后调用 confirm_candidate_state",
    )
    return previous


def allowed_states(clip: Clip) -> set[str]:
    return {i for item in clip.asset_plan for i in (item.start_state_id, item.end_state_id) if i}


def state_requirement(p: StudioProject, state_id: str) -> dict[str, Any]:
    state = p.states.get(state_id)
    if state is None:
        return {"state_id": state_id, "reason": "state_unavailable", "owner_role": "executive"}
    asset = p.assets[state.asset_id]
    ready = any(
        (revision := p.references.get(identifier)) is not None
        and revision.status == "selected"
        and revision.media_id
        for identifier in state.selected_revisions.values()
    )
    return {
        "asset_id": asset.id,
        "asset_name": asset.name,
        "state_id": state.id,
        "state_label": state.label,
        "detail_scope": f"state:{state.id}",
        "owner_role": "set_director" if ready else "executive",
    }


def validate_video_states(
    p: StudioProject,
    clip: Clip,
    item: dict[str, Any],
    binding: TimedMediaBinding,
    previous: Candidate | None,
) -> None:
    source = p.candidates[item["source_candidate_id"]]
    if binding.role == "edit_source":
        require(source.clip_id == clip.id, "编辑来源只能是当前 Clip 的完整候选")
        # Failed footage is an editable proposal, never evidence of actual story state.
        require(not binding.visual_state_ids, "待编辑视频不能声明已确认的剧情状态")
        item["visual_state_ids"] = []
        item["state_coverage"] = {"basis": "unaccepted_edit_source", "verified_state_ids": []}
        return
    if binding.role == "continuation":
        require(previous and previous.id == source.id, "接续视频必须来自当前选用的前一个 Clip")
        # A continuation is the selected predecessor's actual footage. Coverage
        # must remain traceable, never just trust state IDs typed into a request.
        if source.confirmed_end_state:
            traced = set(source.confirmed_end_state.visual_state_ids)
            basis = "director_confirmed_end_state"
        else:
            # Legacy selections retain their original recipes. Use only pinned
            # states without a planned transition; plans alone prove no new state.
            traced = {b.state_id for b in source.bindings}
            source_job = p.jobs.get(source.job_id)
            for source_plan in source_job.request.get("asset_plan", []) if source_job else []:
                if source_plan.get("end_state_id") not in {None, source_plan.get("start_state_id")}:
                    traced.discard(source_plan.get("start_state_id"))
            basis = "legacy_selected_pinned_states"
        declared = set(binding.visual_state_ids)
        require(
            not declared or declared <= traced,
            "接续视频声明的状态缺少前段选用绑定或导演末态确认依据",
        )
        starts = {plan.start_state_id for plan in clip.asset_plan}
        covered = (declared or traced) & starts
        item["visual_state_ids"] = sorted(covered)
        item["state_coverage"] = {
            "basis": basis,
            "source_candidate_id": source.id,
            "verified_state_ids": sorted(traced),
        }
        return
    source_job = p.jobs[source.job_id]
    plans = source_job.request.get("asset_plan", [])
    state_ids = {b.state_id for b in source.bindings}
    state_ids.update(
        i for plan in plans for i in (plan.get("start_state_id"), plan.get("end_state_id")) if i
    )
    require(state_ids, "视频来源缺少可追溯状态，先核对来源，不能猜测或跨状态借用")
    outside = state_ids - allowed_states(clip)
    if outside:
        raise StudioPrerequisiteError(
            "不允许借用 Clip 计划外状态的视频参考；总导演也不能豁免",
            details={
                "reason": "outside_plan_video_states",
                "clip_id": clip.id,
                "source_candidate_id": source.id,
                "source_detail_scope": f"candidate:{source.id}",
                "incompatible_states": [state_requirement(p, i) for i in sorted(outside)],
                "allowed_state_ids": sorted(allowed_states(clip)),
                "owner_role": "set_director",
            },
            recovery="查看列出的来源状态，另选覆盖当前计划状态的合规素材；"
            "需要全局状态或图片调整时明确交给总导演，不得删除冲突声明或豁免校验",
        )
    if binding.visual_state_ids:
        require(
            set(binding.visual_state_ids) == state_ids,
            "视频声明状态与来源绑定不一致，不能省略冲突状态",
        )
    item["visual_state_ids"] = sorted(state_ids)


def resolve_inputs(p: StudioProject, actor: Actor, clip: Clip, plan: ReferencePlan) -> None:
    from .continuation_controls import plan_decision

    validate_clip_plan(p, clip)
    require(clip.asset_plan, "请先明确 Clip 的资产起始/目标状态计划，再选择参考")
    require(
        clip.starts_at and clip.story_event_ids,
        "新生成需要明确剧情事件锚点，不能只按片长或旧故事秒数猜测状态",
    )
    require(not waiting_for_voices(p, clip), "此 Clip 的发声人物尚未建立固定音色，请等待总导演")
    previous = predecessor(p, clip)
    control = plan_decision(p, clip, plan)
    if control:
        plan.use_previous_clip_reference = control["enabled"]
        plan.continuation_decision = control
    editing = bool(clip.repair_plan and clip.repair_plan.mode == "edit")
    if editing:
        assert clip.repair_plan is not None
        source_id = clip.repair_plan.source_candidate_id
        require(source_id, "编辑模式需在修复方案中指定当前 Clip 的待编辑候选")
        if not plan.video_references:
            plan.video_references = [
                TimedMediaBinding(
                    source_candidate_id=source_id,
                    role="edit_source",
                    purpose="完整待修复候选；保留正确内容，只按修复方案修改缺陷，不作为已接受剧情事实",
                )
            ]
        require(
            len(plan.video_references) == 1
            and plan.video_references[0].role == "edit_source"
            and plan.video_references[0].source_candidate_id == source_id,
            "编辑模式仅传当前 Clip 的完整待编辑视频，不叠加前段尾视频；真实前段末帧继续保留",
        )
    else:
        require(
            len(plan.video_references) <= int(previous is not None)
            and all(item.role == "continuation" for item in plan.video_references),
            "视频槽位只用于前段尾 73 帧（约 3.042 秒），其他参考请使用图片和音频；"
            "失败视频不可作为编辑来源",
        )
        if control and not control["tail_video"]:
            require(not plan.video_references, "接续已关闭，不得提交前段尾视频参考")
        if previous and not plan.video_references and (control is None or control["tail_video"]):
            tail = ensure_continuation_tail(p, previous)
            assert tail is not None
            if tail.status != "complete":
                raise StudioPrerequisiteError(
                    "前段尾 73 帧正在准备，请等待本地任务完成后保存参考清单",
                    details={
                        "reason": "continuation_tail_pending",
                        "clip_id": clip.id,
                        "job_id": tail.id,
                        "detail_scope": f"job:{tail.id}",
                        "owner_role": "set_director",
                        "planning_allowed": True,
                    },
                    recovery=(
                        "读取该任务结果；本地截取失败时按原候选的精确尾 73 帧区间"
                        "重新 prepare_media，不重生成视频"
                    ),
                )
            plan.video_references = [
                TimedMediaBinding(
                    preparation_job_id=tail.id,
                    role="continuation",
                    purpose="沿前段最后 73 个真实帧的动作速度、运动方向和机位轨迹，"
                    "在真实末帧之后继续",
                )
            ]
    resolved = [video_reference(p, actor, item, clip_id=clip.id) for item in plan.bindings]
    require(resolved or (previous), "Ref2VA 必须引用实际视觉资产")
    from .endpoint_coverage import bind_alias, physical_image_sources
    from .reference_coverage import approved_states

    composite_states = approved_states(p, clip, resolved)
    bind_alias(p, clip, previous, resolved)
    previous_state = actual_end_state(previous) if previous else None
    plan.reference_inputs = [source for _, source in resolved]
    plan.previous_candidate_id = previous.id if previous else None
    plan.previous_end_state = previous_state.model_copy(deep=True) if previous_state else None
    image_sources = physical_image_sources(plan)
    require(len(image_sources) <= 9, "图片参考最多 9 份，包含前段实际末帧，请明确取舍")
    invalid_images = []
    for index, source in enumerate(image_sources, start=1):
        media = p.media[str(source["media_id"])]
        if media.width and media.height and not valid_h3_image_aspect(media.width, media.height):
            invalid_images.append(
                {
                    "picture_index": index,
                    "media_id": media.id,
                    "detail_scope": f"media:{media.id}",
                    "width": media.width,
                    "height": media.height,
                    "prepared_reference_id": source.get("prepared_reference_id"),
                    "revision_id": source.get("revision_id"),
                    "previous_end_frame": bool(previous_state and index == 1),
                }
            )
    if invalid_images:
        raise StudioPrerequisiteError(
            "H3 参考图片宽高比须在 1:4 至 4:1 之间："
            + "、".join(
                f"Picture {item['picture_index']} ({item['width']}x{item['height']})"
                for item in invalid_images
            ),
            details={
                "reason": "invalid_reference_image_aspect",
                "clip_id": clip.id,
                "owner_role": "set_director",
                "images": invalid_images,
                "min_aspect_ratio": 0.25,
                "max_aspect_ratio": 4,
            },
            recovery="查看列出的精确图片及原始参考，重新裁取完整对象或选择合规同状态素材；"
            "保留旧裁图并重新定稿清单，不拉伸图片。前段真实末帧不可擅自裁剪或替换，异常时交技术核对",
        )
    states = {item.asset_id: item for item in clip.asset_plan}
    for authored, (binding, source) in zip(plan.bindings, resolved, strict=True):
        state = p.states[binding.state_id]
        revision = p.references[binding.revision_id]
        require(
            (
                revision.status == "selected"
                and state.selected_revisions.get(revision.view) == revision.id
            )
            or bool(source.get("region_approval_id")),
            "新视频只能使用当前选用的图片修订，历史版本请先由总导演重新选用",
        )
        if authored.state_role == "guidance":
            from .materials import auxiliary_states

            require(
                binding.state_id in auxiliary_states(p, clip),
                "guidance 仅用于总导演已发布且适用于本 Clip 范围的辅助资产",
            )
            require(str(source.get("purpose", "")).strip(), "辅助参考需说明具体指导用途")
            source["state_role"] = "guidance"
            continue
        expected = states.get(binding.asset_id)
        if expected is None and source.get("composite_state_coverage"):
            require(str(source.get("purpose", "")).strip(), "Composite reference needs a purpose.")
            source["state_role"] = authored.state_role
            continue
        require(expected, "参考资产不在当前 Clip 的状态计划内")
        assert expected is not None
        require(
            binding.state_id
            == (
                expected.end_state_id
                if authored.state_role == "target"
                else expected.start_state_id
            ),
            "参考必须明确对应本 Clip 计划内的起始或目标状态，不能跨状态借用",
        )
        require(str(source.get("purpose", "")).strip(), "每份参考需要说明用途，再据此编写提示词")
        source["state_role"] = authored.state_role
    plan.reference_inputs = [source for _, source in resolved]
    plan.video_inputs = [
        full_edit_source(p, clip, item)
        if item.source_candidate_id
        else timed_media_reference(p, actor, clip, item, "video")
        for item in plan.video_references
    ]
    plan.audio_inputs = [
        timed_media_reference(p, actor, clip, item, "audio") for item in plan.audio_references
    ]
    for item, authored_video in zip(plan.video_inputs, plan.video_references, strict=True):
        validate_video_states(p, clip, item, authored_video, previous)
    for items in (plan.video_inputs, plan.audio_inputs):
        require(
            len(items) <= 3 and sum(float(x["duration"]) for x in items) <= 15,
            "视频和音频各最多 3 份、总时长各不超过 15 秒，请明确取舍参考",
        )
    require(
        len(image_sources) + len(plan.video_inputs) + len(plan.audio_inputs) <= 12,
        "混合参考最多 12 份，请明确取舍，不能自动截断",
    )
    for identifier in clip.speaking_asset_ids:
        voice_id = p.assets[identifier].selected_voice_id
        require(
            not voice_id or any(x.get("voice_reference_id") == voice_id for x in plan.audio_inputs),
            "发声人物需要绑定当前固定音色，不能静默省略",
        )
    from .identity_bootstrap import covers as textual_identity_covers

    video_states = {s for video in plan.video_inputs for s in video.get("visual_state_ids", [])}
    missing_states = [
        state_requirement(p, expected.start_state_id)
        for expected in clip.asset_plan
        if not any(
            b.asset_id == expected.asset_id and b.state_id == expected.start_state_id
            for b, _ in resolved
        )
        and expected.start_state_id not in video_states | composite_states
        and not textual_identity_covers(p, clip, expected.asset_id, expected.start_state_id)
    ]
    if missing_states:
        names = "、".join(
            f"{item.get('asset_name', '')} · {item.get('state_label', item['state_id'])}"
            for item in missing_states
        )
        raise StudioPrerequisiteError(
            "Clip 的必要起始资产状态尚未绑定实际参考：" + names,
            details={
                "reason": "missing_reference_coverage",
                "clip_id": clip.id,
                "missing_states": missing_states,
            },
            recovery="按 missing_states 查询对应状态的当前选用素材或合规视频覆盖；"
            "owner_role=executive 的缺材由片场导演明确提案交接，重新定稿后再提交",
        )
    missing_views = []
    for expected in clip.asset_plan:
        if textual_identity_covers(p, clip, expected.asset_id, expected.start_state_id):
            continue
        absent = [
            view
            for view in expected.required_views
            if view not in p.states[expected.start_state_id].selected_revisions
        ]
        if absent:
            missing_views.append(
                {
                    **state_requirement(p, expected.start_state_id),
                    "missing_views": absent,
                    "owner_role": "executive",
                }
            )
    if missing_views:
        raise StudioPrerequisiteError(
            "当前 Clip 即将使用的必要视图尚未准备好",
            details={
                "reason": "missing_required_views",
                "clip_id": clip.id,
                "states": missing_views,
            },
            recovery="按 states 中明确的状态和 missing_views 向总导演提交或关联素材提案；"
            "不要生成尚未就绪的 Clip",
        )
    continuations = [x for x in plan.video_inputs if x["role"] == "continuation"]
    if continuations:
        require(
            len(continuations) == 1 and plan.video_inputs[0] is continuations[0] and previous,
            "接续尾段只能有一份，并作为 Video 1",
        )
        assert previous is not None
        duration = p.media[str(previous.media_id)].duration
        require(
            duration is not None
            and abs(float(continuations[0].get("end_seconds", 0)) - duration) < 0.05,
            "接续参考必须包含前一 Clip 的实际末尾",
        )
        assert duration is not None
        current_tail = continuations[0].get("continuation_policy") == "real-tail73-v1"
        seconds = CONTINUATION_SECONDS if current_tail else 3.0
        tolerance = 0.001 if current_tail else 0.05
        require(
            abs(float(continuations[0].get("start_seconds", -1)) - (duration - seconds)) < tolerance
            and abs(float(continuations[0]["duration"]) - seconds) < tolerance
            and (not current_tail or continuations[0].get("frame_count") == 73),
            "同片场续接必须使用前段真实末尾的完整 73 帧（约 3.042 秒）；"
            "历史 3 秒记录保留，不能从中间截取或缩短",
        )
    plan.previous_candidate_id = previous.id if previous else None
    actual = actual_end_state(previous) if previous else None
    plan.previous_end_state = actual.model_copy(deep=True) if actual else None
    plan.inspection_gaps = previous_endpoint_gaps(p, previous) if previous else []


def full_edit_source(p: StudioProject, clip: Clip, binding: TimedMediaBinding) -> dict[str, Any]:
    require((binding.role == "edit_source"), "此流程不支持完整视频编辑输入")
    source = p.candidates.get(binding.source_candidate_id or "")
    require(
        source and source.clip_id == clip.id and source.media_id and source.review,
        "编辑源必须是当前 Clip 已审核的完整候选",
    )
    assert source is not None and source.media_id
    require(p.jobs[source.job_id].status == "complete", "编辑来源生成尚未完成")
    media = p.media[source.media_id]
    require(
        media.duration and 2 <= media.duration <= 15,
        "完整编辑源超出 H3 单视频 2–15 秒范围；请选择重新生成，不能截短源视频冒充完整编辑",
    )
    return {
        "media_id": media.id,
        "sha256": media.sha256,
        "source_candidate_id": source.id,
        "source_job_id": source.job_id,
        "duration": media.duration,
        "start_seconds": 0,
        "end_seconds": media.duration,
        "role": "edit_source",
        "purpose": binding.purpose,
        "full_source": True,
    }


def source_fingerprint(plan: ReferencePlan) -> str:
    return digest(
        {
            **(
                {"continuation_decision": plan.continuation_decision}
                if plan.continuation_decision
                else {}
            ),
            "images": plan.reference_inputs,
            "videos": plan.video_inputs,
            "audio": plan.audio_inputs,
            "previous": plan.previous_candidate_id,
            "end_state": plan.previous_end_state.model_dump(mode="json")
            if plan.previous_end_state
            else None,
            **(
                {"inspection_gaps": [gap.model_dump(mode="json") for gap in plan.inspection_gaps]}
                if plan.inspection_gaps
                else {}
            ),
        }
    )


def actual_image_ids(plan: ReferencePlan) -> set[str]:
    result = {x["media_id"] for x in plan.reference_inputs}
    if plan.previous_end_state:
        result.add(plan.previous_end_state.frame_media_id)
    return result


def save_reference_plan(p: StudioProject, actor: Actor, data: dict[str, Any]) -> dict[str, Any]:
    clip = owned_clip(p, actor, str(data.get("clip_id")))
    preparation = None
    if data.get("preparation_id"):
        from .preparations import require_promotable

        preparation = require_promotable(p, clip, str(data["preparation_id"]))
    if clip.status == "blocked" and clip.candidate_ids:
        last = p.candidates[clip.candidate_ids[-1]]
        job = p.jobs.get(last.job_id)
        require(
            not (job and job.status == "failed" and not last.media_id),
            "原视频任务已明确失败且无可用视频；请先调用 repair_clip 准备技术重试"
            "（不扣普通质量修正次数），再用当前 expected_plan_id 新建参考清单；"
            "旧任务、清单和精确绑定继续保留",
        )
    require(
        clip.status in {"planned", "repair", "escalated"},
        "已创建任务的参考绑定固定；请在下一次生成前新建清单",
    )
    receipt = require_context(p, actor, data, "references", clip.id)
    old = p.reference_plans.get(str(clip.reference_plan_id))
    require(
        not old or data.get("expected_plan_id") == old.id, "参考清单版本冲突，请读取当前清单后修改"
    )
    conflicts = [ReferenceConflict.model_validate(x) for x in data.get("conflicts", [])]
    if old:
        by_id = {x.id: x for x in conflicts}
        for conflict in old.conflicts:
            if conflict.id not in by_id:
                conflicts.append(conflict.model_copy(deep=True))
    plan = ReferencePlan(
        clip_id=clip.id,
        actor_id=actor.id,
        parent_id=old.id if old else None,
        revision=old.revision + 1 if old else 1,
        bindings=[VideoReferenceInput.model_validate(x) for x in data.get("bindings", [])],
        video_references=[
            TimedMediaBinding.model_validate(x) for x in data.get("video_references", [])
        ],
        audio_references=[
            TimedMediaBinding.model_validate(x) for x in data.get("audio_references", [])
        ],
        conflicts=conflicts,
        reason=str(data.get("reason", "")),
        context_fingerprint=receipt.fingerprint,
        context_dependencies=receipt.dependencies,
        preparation_id=preparation.id if preparation else None,
        use_previous_clip_reference=data.get("use_previous_clip_reference"),
    )
    resolve_inputs(p, actor, clip, plan)
    for conflict in conflicts:
        if conflict.status == "not_applicable":
            prior = next((x for x in old.conflicts if x.id == conflict.id), None) if old else None
            require(prior, "只有保留原记录的历史冲突可标记不适用于当前清单")
            assert prior is not None
            require(
                conflict.conflicting_media_ids == prior.conflicting_media_ids
                and conflict.evidence_media_ids == prior.evidence_media_ids,
                "不适用处置须保留原冲突来源和历史证据，不是撤回原观察",
            )
    validate_conflict_applicability(plan)
    if old:
        require(plan.reason.strip(), "修改参考清单需要记录原因")
        for conflict in conflicts:
            prior = next((x for x in old.conflicts if x.id == conflict.id), None)
            if prior:
                corrected = (
                    conflict.conflicting_media_ids != prior.conflicting_media_ids
                    or conflict.status == "withdrawn"
                    and prior.status != "withdrawn"
                )
                if corrected:
                    require(
                        conflict.correction_reason.strip() and conflict.evidence_media_ids,
                        "纠正或撤回冲突记录需要说明误判原因和已查看的画面依据；原记录保留在上一版清单",
                    )
                    require(
                        set(prior.conflicting_media_ids) <= set(conflict.evidence_media_ids),
                        "冲突纠正需核对原来列出的全部素材，不能静默删去冲突来源",
                    )
        if old.status not in {"submitted", "legacy"}:
            old.status = "superseded"
    plan.dependency_fingerprint = source_fingerprint(plan)
    if preparation:
        plan.prompt = preparation.prompt_draft
        preparation.status = "consumed"
        # Capture the context after this transaction's own lifecycle change.
        # Later edits to the preparation or its sources still invalidate the plan.
        plan.context_fingerprint = context_fingerprint(p, clip.id, "references", actor.id)
    from .production_efficiency import creative_fingerprint

    plan.creative_fingerprint = creative_fingerprint(p, clip.id)
    p.reference_plans[plan.id] = plan
    clip.reference_plan_id = plan.id
    return {
        "reference_plan_id": plan.id,
        "clip_id": clip.id,
        "summary": f"已保存第 {plan.revision} 版参考清单；下一步检查素材并编写提示词",
    }


def validate_conflict_applicability(plan: ReferencePlan) -> None:
    actual = actual_image_ids(plan) | {x["media_id"] for x in plan.video_inputs}
    for conflict in plan.conflicts:
        if conflict.status != "not_applicable":
            continue
        require(
            not set(conflict.conflicting_media_ids) & actual,
            "冲突来源仍在当前清单中，不能标记为历史不适用",
        )
        require(
            conflict.applicability_reason.strip()
            and conflict.current_evidence_media_ids
            and set(conflict.current_evidence_media_ids) <= actual_image_ids(plan),
            "历史冲突不适用需说明当前输入如何替代，并提供当前清单内的图片证据",
        )


def _paired_endpoint_replacement(
    plan: ReferencePlan, project: StudioProject, conflict: ReferenceConflict
) -> bool:
    """An identical current-state alias does not require disabling real continuation."""
    from .set_handoffs import same_source_image

    previous = project.candidates.get(plan.previous_candidate_id or "")
    previous_clip = project.clips.get(previous.clip_id) if previous else None
    endpoint = plan.previous_end_state.frame_media_id if plan.previous_end_state else None
    replacements = set(conflict.replacement_media_ids)
    return bool(
        plan.use_previous_clip_reference is True
        and plan.continuation_decision.get("policy") == "director-paired-v2"
        and plan.continuation_decision.get("enabled") is True
        and previous
        and previous_clip
        and previous_clip.accepted_candidate_id == previous.id
        and actual_end_state(previous) == plan.previous_end_state
        and replacements
        and replacements <= {item["media_id"] for item in plan.reference_inputs}
        and all(
            same_source_image(project, identifier, str(endpoint)) for identifier in replacements
        )
        and any(
            item.get("role") == "continuation" and item.get("source_candidate_id") == previous.id
            for item in plan.video_inputs
        )
    )


def validate_conflicts(plan: ReferencePlan, project: StudioProject | None = None) -> None:
    validate_conflict_applicability(plan)
    actual = actual_image_ids(plan) | {x["media_id"] for x in plan.video_inputs}
    for conflict in plan.conflicts:
        if conflict.status == "not_applicable":
            continue
        if conflict.status == "withdrawn":
            require(
                conflict.correction_reason.strip() and conflict.evidence_media_ids,
                "撤回误判需要记录原因和已查看的画面依据",
            )
            continue
        require(
            conflict.status == "resolved", "存在未消除的参考冲突，请先裁剪、换图/视频或请总导演修订"
        )

        require(
            conflict.resolution_method
            and conflict.reason.strip()
            and conflict.replacement_media_ids,
            "参考冲突需记录裁剪、替换或修订结果及依据",
        )
        retained = set(conflict.retained_compatible_media_ids)
        if retained:
            require(
                (project is not None),
                "Retained conflict components require the coherent-v1 policy.",
            )
            assert project is not None
            original = set(conflict.conflicting_media_ids)
            design_ids = {
                item["media_id"]
                for item in plan.reference_inputs
                if (item.get("reference_semantics") or {}).get("image_basis") == "design"
                and not (item.get("reference_semantics") or {}).get("observed_state_claim")
            }
            if retained <= design_ids:
                # A cropped composite can conflict with otherwise valid standalone
                # designs. Those inspected authorities are not predecessor endpoints.
                require(
                    retained <= original
                    and bool(original - retained)
                    and retained <= actual_image_ids(plan),
                    "Retained compatible designs must remain in the current input list.",
                )
                require(
                    conflict.correction_reason.strip()
                    and original <= set(conflict.evidence_media_ids)
                    and all(identifier in project.media for identifier in retained),
                    "Retained compatible designs require the inspected original conflict evidence.",
                )
            elif not retained & original:
                # An excluded source may be replaced by a local crop while the
                # independent genuine endpoint remains current-state evidence.
                # It exempts none of the original conflicting inputs below and
                # does not determine whether predecessor conditioning is on.
                endpoint = (
                    plan.previous_end_state.frame_media_id if plan.previous_end_state else None
                )
                previous = project.candidates.get(plan.previous_candidate_id or "")
                previous_clip = project.clips.get(previous.clip_id) if previous else None
                from .set_handoffs import same_source_image

                require(
                    previous
                    and previous_clip
                    and previous_clip.accepted_candidate_id == previous.id
                    and actual_end_state(previous) == plan.previous_end_state
                    and endpoint in retained
                    and retained <= actual_image_ids(plan)
                    and all(
                        same_source_image(project, identifier, str(endpoint))
                        for identifier in retained
                    ),
                    "Independent retained evidence must be the selected actual endpoint.",
                )
                replacements = set(conflict.replacement_media_ids)
                replacement_inputs = [
                    item for item in plan.reference_inputs if item["media_id"] in replacements
                ]
                require(
                    conflict.resolution_method == "crop"
                    and replacements == {item["media_id"] for item in replacement_inputs}
                    and all(
                        (preparation := item.get("preparation", {})).get("selection") == "crop"
                        and preparation.get("resolved_reference", {}).get("media_id") in original
                        and preparation.get("output_sha256") == item.get("sha256")
                        for item in replacement_inputs
                    )
                    and conflict.correction_reason.strip()
                    and (retained | replacements) <= set(conflict.evidence_media_ids)
                    and (retained | replacements) <= set(conflict.current_evidence_media_ids),
                    "Independent endpoint evidence requires "
                    "the inspected current crop replacement.",
                )
            else:
                endpoint = (
                    plan.previous_end_state.frame_media_id if plan.previous_end_state else None
                )
                from .set_handoffs import same_source_image

                require(
                    endpoint in retained
                    and endpoint in original
                    and retained <= actual_image_ids(plan)
                    and all(
                        same_source_image(project, identifier, str(endpoint))
                        for identifier in retained
                    )
                    and (
                        (
                            plan.use_previous_clip_reference is False
                            and plan.continuation_decision.get("enabled") is False
                            and not any(
                                item.get("role") == "continuation" for item in plan.video_inputs
                            )
                        )
                        or _paired_endpoint_replacement(plan, project, conflict)
                    ),
                    "Retained actual endpoint requires pair-off "
                    "or compatible paired-on continuation.",
                )
                # Preserve original/current evidence in both modes. Pair-off may
                # use a revised design; pair-on admits only an exact endpoint alias.
                require(
                    conflict.correction_reason.strip()
                    and original <= set(conflict.evidence_media_ids)
                    and (retained | set(conflict.replacement_media_ids))
                    <= set(conflict.current_evidence_media_ids)
                    and set(conflict.replacement_media_ids) <= actual_image_ids(plan),
                    "Retaining the compatible endpoint requires "
                    "inspected original and current conflict evidence.",
                )
        require(
            not (set(conflict.conflicting_media_ids) - retained) & actual,
            "矛盾的原始参考仍在清单中，不能只用提示词要求忽略",
        )
        # A resolved conflict stays in history after a later recipe stops using
        # its replacement. Its contradictory original remains forbidden above.


def patch_reference_plan(p: StudioProject, actor: Actor, data: dict[str, Any]) -> dict[str, Any]:
    old = p.reference_plans.get(str(data.get("reference_plan_id")))
    require(
        old and old.status in {"draft", "finalized", "needs_review"},
        "只能修改尚未提交的当前参考清单",
    )
    assert old is not None
    clip = owned_clip(p, actor, old.clip_id)
    require(clip.reference_plan_id == old.id, "参考清单版本冲突，请读取当前清单")
    require(str(data.get("reason", "")).strip(), "修改清单必须记录原因")
    fields = ("bindings", "video_references", "audio_references", "conflicts")
    payload: dict[str, Any] = {
        name: [item.model_dump(mode="json") for item in getattr(old, name)] for name in fields
    }
    if old.use_previous_clip_reference is not None:
        payload["use_previous_clip_reference"] = old.use_previous_clip_reference
    changes = []
    for name in (*fields, "prompt", "use_previous_clip_reference"):
        if name in data:
            before = old.prompt if name == "prompt" else payload.get(name)
            if data[name] != before:
                changes.append({"field": name, "before": before, "after": data[name]})
                if name != "prompt":
                    payload[name] = data[name]
    if (
        data.get("use_previous_clip_reference") is False
        and "video_references" not in data
        and payload["video_references"]
        and all(item["role"] == "continuation" for item in payload["video_references"])
    ):
        changes.append(
            {"field": "video_references", "before": payload["video_references"], "after": []}
        )
        payload["video_references"] = []
    require(changes or old.status == "needs_review", "清单内容未变化")
    outcome = save_reference_plan(
        p,
        actor,
        {
            **payload,
            "clip_id": clip.id,
            "expected_plan_id": old.id,
            "reason": data["reason"],
            "context_token": data.get("context_token"),
        },
    )
    new = p.reference_plans[outcome["reference_plan_id"]]
    new.prompt = str(data.get("prompt", old.prompt))
    new.changes = changes
    return {
        **outcome,
        "changes": changes,
        "prompt_requires_reconciliation": True,
        "summary": f"已保存第 {new.revision} 版清单；保留的提示词需按新素材重新核对",
    }


def finalize_reference_plan(
    p: StudioProject, actor: Actor, data: dict[str, Any], store: StudioStore
) -> dict[str, Any]:
    plan = p.reference_plans.get(str(data.get("reference_plan_id")))
    require(plan, "参考清单不存在")
    assert plan is not None
    clip = owned_clip(p, actor, plan.clip_id)
    require(
        clip.reference_plan_id == plan.id and plan.status == "draft",
        "清单需先重新选材保存，不能直接定稿过期或已提交清单",
    )
    receipt = require_context(p, actor, data, "references", clip.id)
    require(
        receipt.fingerprint == plan.context_fingerprint or _creative_current(p, plan),
        "相关上下文已变化，请重新保存参考清单并核对提示词",
    )
    checked = plan.model_copy(deep=True)
    resolve_inputs(p, actor, clip, checked)
    require(
        source_fingerprint(checked) == plan.dependency_fingerprint,
        "参考实际素材或前段选用已变化，请重新保存清单",
    )
    media_ids = actual_image_ids(plan)
    # Only the genuine boundary of a selected, explicitly limited predecessor
    # can remain uninspected. A regular asset binding to that same ID still
    # needs inspection, as do corrections and all other references.
    required_images = {x["media_id"] for x in plan.reference_inputs} | (
        media_ids - {gap.media_id for gap in plan.inspection_gaps}
    )
    from .external_review import blocked_quarantine, media_is_external_approved

    external_inspections = {
        identifier
        for identifier in required_images
        if media_is_external_approved(
            p,
            identifier,
            "reference_inspection",
            actor_id=actor.id,
            clip_id=clip.id,
        )
    }
    missing = required_images - (
        store.inspected(p.id, actor.id, list(media_ids)) | external_inspections
    )

    session = actor.sessions.get(actor.session_key)
    missing |= required_images - (
        set(session.viewed_media_ids if session else []) | external_inspections
    )
    unavailable = sorted(
        i
        for i in missing
        if blocked_quarantine(
            p,
            i,
            "reference_inspection",
            actor_id=actor.id,
            clip_id=clip.id,
        )
    )
    if unavailable:
        raise StudioPrerequisiteError(
            "参考图片已隔离，不能当作已审素材定稿",
            details={
                "reason": "unreviewed_reference_images",
                "clip_id": clip.id,
                "quarantined_media_ids": unavailable,
                "previous_candidate_id": plan.previous_candidate_id,
                "owner_role": "set_director",
            },
            recovery="普通参考须选用未隔离且已审看的现有同状态素材；若是已选前段的真实末帧，"
            "先按有限审核规则记录末帧未审、未知项与其余画面依据，再重新保存清单。"
            "不能认证未审资产状态或重新读取隔离图片",
        )
    require(
        not missing,
        "请先 read_media 查看全部实际参考图及前段末帧，再编写最终提示词",
    )
    correction_ids = {
        i
        for conflict in plan.conflicts
        for i in (
            conflict.current_evidence_media_ids
            if conflict.status == "not_applicable"
            else conflict.evidence_media_ids
        )
    }
    require(all(i in p.media for i in correction_ids), "冲突纠正的画面依据不存在")
    external_correction_inspections = {
        identifier
        for identifier in correction_ids
        if media_is_external_approved(
            p,
            identifier,
            "reference_inspection",
            actor_id=actor.id,
            clip_id=clip.id,
        )
    }
    require(
        correction_ids
        <= (
            store.inspected(p.id, actor.id, list(correction_ids)) | external_correction_inspections
        ),
        "请先 read_media 查看冲突纠正或撤回所依据的全部素材",
    )
    validate_conflicts(plan, p)
    require(
        str(data.get("conflict_check", "")).strip(),
        "请记录实际素材之间的人物、物品和空间冲突检查结果",
    )
    require(
        str(data.get("prompt_reconciliation", "")).strip(),
        "请记录提示词如何对应最终素材顺序、状态和用途",
    )
    try:
        from .endpoint_coverage import physical_image_sources

        prompt = prepare_prompt(
            str(data.get("prompt", "")),
            len(physical_image_sources(plan)),
            continuation=bool(plan.previous_end_state)
            and plan.continuation_decision.get("enabled", True),
            video_count=len(plan.video_inputs),
            audio_count=len(plan.audio_inputs),
            video_continuation=any(x["role"] == "continuation" for x in plan.video_inputs),
            video_editing=(bool(clip.repair_plan and clip.repair_plan.mode == "edit")),
        )
    except ProviderContractError as exc:
        raise StudioError(str(exc)) from None
    plan.prompt = prompt
    if clip.order:
        check = data.get("continuity_check", {})
        fields = {"action", "space", "camera", "completed_actions", "unknowns"}
        require(
            set(check) == fields and all(value.strip() for value in check.values()),
            "请在原规划会话记录真实动作阶段、构图、运镜、不可重演动作和未知项的接续核对",
        )
        plan.continuity_check = check
    plan.context_dependencies = receipt.dependencies
    plan.prompt_reconciliation = str(data["prompt_reconciliation"])
    plan.conflict_check = str(data["conflict_check"])
    if plan.inspection_gaps:
        limitation = (
            "前段实际末帧已隔离，未完成审看；仅保留已选前段的真实接续输入，"
            "末态描述为暂定，不能据此认证资产状态或宣称接缝已验证。"
        )
        plan.prompt_reconciliation += "\n" + limitation
        plan.conflict_check += "\n" + limitation
    plan.status, plan.finalized_at = "finalized", now()
    plan.context_fingerprint = receipt.fingerprint
    return {
        "reference_plan_id": plan.id,
        "clip_id": clip.id,
        "summary": "参考与六段式提示词已核对，可以提交生成",
    }


def submission_plan(
    p: StudioProject, actor: Actor, data: dict[str, Any], clip: Clip
) -> ReferencePlan:
    require_context(p, actor, data, "generation", clip.id)
    plan = p.reference_plans.get(str(data.get("reference_plan_id")))
    require(
        plan
        and plan.clip_id == clip.id
        and plan.id == clip.reference_plan_id
        and plan.status == "finalized",
        "生成前需要当前已定稿的 reference_plan_id，请先保存参考清单再核对提示词",
    )
    assert plan is not None
    require(plan.actor_id == actor.id, "参考清单不属于当前片场导演")

    if clip.order:
        require(
            set(plan.continuity_check)
            == {"action", "space", "camera", "completed_actions", "unknowns"}
            and all(value.strip() for value in plan.continuity_check.values()),
            "接续核对缺失，请返回原规划会话重新定稿，不能跳过真实前段检查",
        )
    require(
        plan.context_fingerprint == context_fingerprint(p, clip.id) or _creative_current(p, plan),
        "相关上下文已更新，请重新选材并定稿",
    )
    checked = plan.model_copy(deep=True)
    resolve_inputs(p, actor, clip, checked)
    require(
        source_fingerprint(checked) == plan.dependency_fingerprint,
        "实际素材已更新，请重新选材并定稿",
    )
    validate_conflicts(plan, p)
    for key in ("prompt", "bindings", "video_references", "audio_references"):
        if key in data:
            expected = getattr(plan, key)
            if isinstance(expected, list):
                expected = [x.model_dump(mode="json") for x in expected]
            require(
                data[key] == expected, "提交只能使用已定稿清单；更改素材或提示词请重新保存和定稿"
            )
    return plan


def invalidate_reference_plans(p: StudioProject) -> list[str]:
    """Check only live drafts; submitted recipes and historical versions are immutable."""
    affected = []
    from .context_cache import context_changes

    maps = p.__dict__.get("_repositories")
    core_changed, changes = context_changes(p)
    if maps and not changes and not core_changed:
        return []

    def relevant(plan: ReferencePlan) -> bool:
        if not maps or core_changed or not plan.context_dependencies:
            return True
        for dependency in plan.context_dependencies:
            for kind, identifier, value, before, parent_id in changes:
                if kind != dependency["kind"]:
                    continue
                signature = dependency["signature"]
                if any(
                    identifier == (item if isinstance(item, str) else item[0]) for item in signature
                ):
                    return True
                if value is not None and matches(value, dependency["filters"], parent_id):
                    return True
                if before is not None and matches(
                    MODELS[kind].model_validate(before), dependency["filters"], parent_id
                ):
                    return True
        return False

    filters: dict[str, Any] = {"statuses": ["draft", "finalized"]}
    lookup = p.__dict__.get("_affected_reference_plans")
    if maps and lookup and not core_changed:
        keys: set[str] = set()
        for kind, identifier, value, before, parent_id in changes:
            keys.update({bucket("kind", kind), bucket("id", kind, identifier)})
            for source in (value, MODELS[kind].model_validate(before) if before else None):
                if source is not None:
                    owner = str(parent_id or parent(source))
                    if kind == "memory_entries":
                        keys.add(bucket("memory-any", owner))
                    scope = getattr(source, "scope", None)
                    dimensions = scope.model_dump() if kind == "memory_entries" and scope else {}
                    scoped = {
                        name: ids
                        for name, ids in dimensions.items()
                        if name in {"asset_ids", "set_ids", "clip_ids", "event_ids"} and ids
                    }
                    if scoped:
                        for name, ids in scoped.items():
                            keys.update(bucket("memory", owner, name, str(i)) for i in ids)
                    else:
                        keys.add(bucket("parent", kind, owner))
        # Include local plans created in this transaction, before their index is flushed.
        filters["ids"] = list(set(lookup(keys)) | set(dict.keys(maps["reference_plans"])))
    for plan in select_entities(p.reference_plans, **filters):
        if not relevant(plan):
            continue
        if p.clips[plan.clip_id].archived_at:
            plan.status = "superseded"
            continue
        if not core_changed and (
            plan.context_fingerprint == context_fingerprint(p, plan.clip_id)
            or _creative_current(p, plan)
        ):
            checked = plan.model_copy(deep=True)
            try:
                resolve_inputs(p, p.actors[plan.actor_id], p.clips[plan.clip_id], checked)
                if source_fingerprint(checked) == plan.dependency_fingerprint:
                    continue
            except (StudioError, KeyError):
                pass
        plan.status = "needs_review"
        plan.invalidation_reasons = [
            "相关有效记忆、状态、当前选用素材或前段实际结果已更新；需重新选材并核对提示词"
        ]
        plan.affected_media_ids = sorted(actual_image_ids(plan))
        affected.append(plan.clip_id)
        actor = p.actors[plan.actor_id]
        actor.request_wake()
        if not actor.paused and actor.status in {"waiting", "idle"}:
            actor.status = "ready"
    return affected


def _creative_current(p: StudioProject, plan: ReferencePlan) -> bool:
    from .production_efficiency import creative_fingerprint

    return bool(
        plan.creative_fingerprint
        and plan.creative_fingerprint == creative_fingerprint(p, plan.clip_id)
    )
