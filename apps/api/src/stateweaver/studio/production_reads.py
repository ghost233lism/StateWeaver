"""Read-only production assistance for selection and handoff."""

from __future__ import annotations

from typing import Literal, cast

from stateweaver.studio.continuity import waiting_for_voices
from stateweaver.studio.contracts import (
    CandidateComparison,
    ClipReadiness,
    ComparedCandidate,
    ReadinessItem,
    ReferenceOption,
    ReferenceOptions,
    StudioProject,
)
from stateweaver.studio.memory import require, validate_clip_plan
from stateweaver.studio.proposals import clip_pending_proposals
from stateweaver.studio.reference_plans import allowed_states
from stateweaver.studio.repository import select_entities
from stateweaver.studio.store import StudioError, StudioPrerequisiteError
from stateweaver.studio.workflow import can_plan_set
from stateweaver.studio_media.reference_crop import is_reference_board


def clip_readiness(
    p: StudioProject, clip_id: str, *, _include_quality: bool = True
) -> ClipReadiness:
    clip = p.clips.get(clip_id)
    require(clip and not clip.archived_at, "Clip 不存在或已归档")
    assert clip is not None
    production_set = p.sets[clip.set_id]
    executive = select_entities(p.actors, subtype="executive")[0]
    director = p.actors[production_set.actor_id]
    proposals = clip_pending_proposals(p, clip)
    items: list[ReadinessItem] = []

    def missing(kind: str, text: str, entity: str, *, global_owner: bool = True) -> None:
        owner = executive if global_owner else director
        items.append(
            ReadinessItem(
                key=f"{kind}:{entity}",
                kind=kind,
                description=text,
                owner_id=owner.id,
                owner_role=owner.role,
                entity_id=entity,
                proposal_ids=[x.id for x in proposals],
            )
        )

    if not clip.accepted_candidate_id:
        from .playback import set_status

        playback = set_status(p, production_set)
        if not playback.can_submit:
            missing("set_duration", playback.reason, production_set.id)
            items[-1].details = playback.model_dump(mode="json")
        if not p.catalog_frozen_at:
            missing(
                "catalog",
                ("等待总导演确认资产目录；素材可按当前 Clip 的需要准备"),
                "global",
            )
        elif p.status not in {"producing", "blocked"}:
            missing("project_status", "项目尚未处于制作阶段", "global")
        if not clip.asset_plan or not clip.story_event_ids or not clip.starts_at:
            missing("plan", "需要明确本片段的剧情事件和资产起止状态", clip.id, global_owner=False)
        try:
            validate_clip_plan(p, clip)
        except StudioError as exc:
            missing(
                "state_plan",
                str(exc),
                clip.id,
                global_owner=isinstance(exc, StudioPrerequisiteError)
                or clip.state_readiness == "needs_verification",
            )
            if isinstance(exc, StudioPrerequisiteError):
                items[-1].details = exc.details
        from .set_handoffs import dependency_report

        items.extend(dependency_report(p, production_set.id, clip).blockers)
        if clip.order:
            previous = p.clips[production_set.clip_ids[clip.order - 1]]
            selected = p.candidates.get(str(previous.accepted_candidate_id))
            if not selected or not selected.media_id or not selected.end_state:
                missing(
                    "predecessor",
                    f"等待前段「{previous.title}」的已选视频和实际末态",
                    previous.id,
                    global_owner=False,
                )
            else:
                from .continuation_controls import needs_tail

                reference_plan = p.reference_plans.get(str(clip.reference_plan_id))
                tail = p.jobs.get(str(selected.continuation_reference_job_id))
                try:
                    tail_required = needs_tail(p, clip, reference_plan)
                except StudioError as exc:
                    missing("continuation_policy", str(exc), clip.id, global_owner=False)
                    tail_required = False
                if tail_required and (tail is None or tail.status != "complete"):
                    missing(
                        "continuation_tail",
                        "前段尾视频截取失败，请用同一已选候选和任务中的精确区间重新 prepare_media"
                        if tail and tail.status == "failed"
                        else "等待前段已选视频的固定尾段本地素材（新任务为 73 个真实帧）",
                        tail.id if tail else selected.id,
                        global_owner=False,
                    )
        reference_plan = p.reference_plans.get(str(clip.reference_plan_id))
        covered_states = (
            {
                state_id
                for video in reference_plan.video_inputs
                for state_id in video.get("visual_state_ids", [])
            }
            if (reference_plan)
            else set()
        )
        from .identity_bootstrap import awaiting_source as awaiting_bootstrap_source
        from .identity_bootstrap import decision as bootstrap_decision

        textual_identity = bootstrap_decision(p, clip)
        for plan in clip.asset_plan:
            for state_id in dict.fromkeys([plan.start_state_id, plan.end_state_id]):
                if not state_id:
                    continue
                state = p.states.get(state_id)
                if not state:
                    missing("state", "所需剧情状态尚未建立", state_id)
                    continue
                if (
                    textual_identity
                    and plan.asset_id == textual_identity.asset_id
                    and state_id == textual_identity.state_id
                    and awaiting_bootstrap_source(p, clip)
                ):
                    continue  # Explicit textual design is deferred, never supplied visual evidence.
                views = plan.required_views if state_id == plan.start_state_id else []
                needed = (
                    state_id == plan.start_state_id
                    and state_id not in covered_states
                    or bool(
                        reference_plan
                        and any(b.state_id == state_id for b in reference_plan.bindings)
                    )
                )
                regional_inputs = bool(
                    reference_plan
                    and any(
                        item.get("state_id") == state_id and item.get("region_approval_id")
                        for item in reference_plan.reference_inputs
                    )
                )
                if needed and not state.selected_revisions and not regional_inputs:
                    missing("reference", f"「{state.label}」尚无选用的参考图", state.id)
                for view in views:
                    revision = p.references.get(str(state.selected_revisions.get(view)))
                    if not revision or revision.status != "selected" or not revision.media_id:
                        missing(
                            "reference_view",
                            f"「{state.label}」缺少选用视图：{view}",
                            f"{state.id}:{view}",
                        )
        for asset_id in waiting_for_voices(p, clip):
            missing("voice", f"等待「{p.assets[asset_id].name}」的固定音色", asset_id)
        for proposal in proposals:
            missing("proposal", proposal.resolution or proposal.text, proposal.id)
        if clip.quality_episode and clip.quality_episode.status == "escalated":
            missing("quality", "普通修正已用完，等待总导演比较候选并处理", clip.id)
        if clip.status == "blocked" or director.status == "blocked":
            missing(
                "technical",
                "技术故障尚未恢复，请核对任务详情与原操作回执；不能重复提交未知任务",
                clip.id,
                global_owner=False,
            )
        for job in select_entities(
            p.jobs, actor_id=director.id, statuses=["queued", "submitting", "running", "ambiguous"]
        ):
            candidate = p.candidates.get(job.target_id)
            if candidate and candidate.clip_id == clip.id:
                missing("job", "等待已提交任务回收；请勿重复生成", job.id, global_owner=False)
        current_plan = p.reference_plans.get(str(clip.reference_plan_id))
        if not current_plan or current_plan.status != "finalized":
            missing(
                "reference_plan",
                "需要定稿当前参考清单并核对六段式提示词",
                clip.id,
                global_owner=False,
            )
        else:
            from .reference_plans import _creative_current
            from .task_context import context_fingerprint

            try:
                require(
                    current_plan.context_fingerprint == context_fingerprint(p, clip.id)
                    or _creative_current(p, current_plan),
                    "已定稿参考依赖已变化，请重新保存清单并核对提示词",
                )
            except StudioError as exc:
                missing("reference_plan_stale", str(exc), current_plan.id, global_owner=False)
                if isinstance(exc, StudioPrerequisiteError):
                    items[-1].details = exc.details
    current_plan = p.reference_plans.get(str(clip.reference_plan_id))
    planning_ready = can_plan_set(p, production_set)
    from .quality_progress import quality_progress

    progress = quality_progress(p, clip, readiness_items=items) if _include_quality else None
    if progress:
        existing_codes = {item.kind for item in items}
        for blocker in progress.blockers:
            if "generate_clip" not in blocker.operations or blocker.code in existing_codes:
                continue
            missing(
                blocker.code,
                blocker.reason,
                clip.id,
                global_owner=blocker.owner_actor_id == executive.id,
            )
            items[-1].details = blocker.details
    return ClipReadiness(
        clip_id=clip.id,
        quality_progress=progress,
        ready=not items,
        selected=bool(clip.accepted_candidate_id),
        items=items,
        existing_proposal_ids=[x.id for x in proposals],
        reference_plan_id=current_plan.id if current_plan else None,
        reference_plan_status=current_plan.status if current_plan else None,
        can_plan=planning_ready,
        can_generate=("generate_clip" in progress.allowed_operations)
        if progress
        else not items and not bool(clip.accepted_candidate_id),
        planning_items=[]
        if planning_ready
        else [
            ReadinessItem(
                key=f"set_handoff:{production_set.id}",
                kind="set_handoff",
                description="等待总导演交付片场剧情目标、入场状态和目标离场状态",
                owner_id=executive.id,
                owner_role=executive.role,
                entity_id=production_set.id,
            )
        ],
    )


def reference_options(
    p: StudioProject, actor_id: str, clip_id: str, *, after: str | None = None, limit: int = 100
) -> ReferenceOptions:
    clip = p.clips.get(clip_id)
    require(clip and not clip.archived_at, "Clip 不存在或已归档")
    assert clip is not None
    validate_clip_plan(p, clip)
    options: list[ReferenceOption] = []
    from .materials import auxiliary_states

    state_ids = allowed_states(clip)
    selected_refs: set[str] = set()
    for plan in clip.asset_plan:
        for role, state_id in (("start", plan.start_state_id), ("target", plan.end_state_id)):
            if not state_id or role == "target" and state_id == plan.start_state_id:
                continue
            state = p.states[state_id]
            asset = p.assets[state.asset_id]
            for view, identifier in state.selected_revisions.items():
                revision = p.references.get(identifier)
                if not revision or revision.status != "selected" or not revision.media_id:
                    continue
                selected_refs.add(identifier)
                board = is_reference_board(view)
                options.append(
                    ReferenceOption(
                        id=f"image:{identifier}:{role}",
                        modality="image",
                        label=f"{asset.name} · {state.label} · {view}",
                        purpose="planned_state",
                        media_id=revision.media_id,
                        asset_id=asset.id,
                        state_id=state.id,
                        revision_id=identifier,
                        state_role=cast(Literal["start", "target"], role),
                        preparation="crop_or_use_board" if board else "inspect",
                        steps=[
                            "read_media 原图",
                            "prepare_reference 裁出所需单视图或说明使用整板的原因",
                            "read_media 准备结果",
                        ]
                        if board
                        else ["read_media 查看实际图片"],
                    )
                )
    for state_id in sorted(auxiliary_states(p, clip)):
        state = p.states[state_id]
        asset = p.assets[state.asset_id]
        for view, identifier in state.selected_revisions.items():
            revision = p.references.get(identifier)
            if not revision or revision.status != "selected" or not revision.media_id:
                continue
            selected_refs.add(identifier)
            options.append(
                ReferenceOption(
                    id=f"guidance:{identifier}",
                    modality="image",
                    label=f"{asset.name} · {state.label} · {view}",
                    purpose=asset.purpose,
                    media_id=revision.media_id,
                    asset_id=asset.id,
                    state_id=state.id,
                    revision_id=identifier,
                    state_role="guidance",
                    preparation="inspect",
                    steps=["查看辅助图片并说明用途；目标画面不代表已观察的剧情事实"],
                )
            )
    previous = None
    if clip.order:
        prev_clip = p.clips[p.sets[clip.set_id].clip_ids[clip.order - 1]]
        previous = p.candidates.get(str(prev_clip.accepted_candidate_id))
        if previous and previous.end_state:
            options.append(
                ReferenceOption(
                    id=f"boundary:{previous.id}",
                    modality="image",
                    label="前段实际末帧（自动占用 Picture 1）",
                    purpose="boundary",
                    media_id=previous.end_state.frame_media_id,
                    source_candidate_id=previous.id,
                    preparation="inspect",
                    steps=["read_media 查看实际末帧"],
                )
            )
    # Binding and parent indexes restrict this query to relevant accepted sources
    # and the current Clip's repairs, independent of unrelated Set/history growth.
    sources = {
        c.id: c
        for asset_id in {plan.asset_id for plan in clip.asset_plan}
        for c in select_entities(p.candidates, binding_asset=asset_id, statuses=["accepted"])
    }
    sources.update(
        {
            c.id: c
            for c in select_entities(
                p.candidates, parent_id=clip.id, statuses=["rejected", "review"]
            )
        }
    )
    if previous:
        sources[previous.id] = previous
    sources = {previous.id: previous} if previous else {}
    if clip.repair_plan and clip.repair_plan.mode == "edit":
        source = p.candidates.get(clip.repair_plan.source_candidate_id or "")
        sources = {source.id: source} if source and source.clip_id == clip.id else {}
    for candidate in sources.values():
        source_clip = p.clips[candidate.clip_id]
        if source_clip.archived_at or (
            source_clip.id != clip.id and source_clip.accepted_candidate_id != candidate.id
        ):
            continue
        media = p.media.get(str(candidate.media_id))
        if not media or not media.duration or media.duration < 2:
            continue
        source_states = {b.state_id for b in candidate.bindings}
        source_job = p.jobs[candidate.job_id]
        source_states.update(
            s
            for x in source_job.request.get("asset_plan", [])
            for s in (x.get("start_state_id"), x.get("end_state_id"))
            if s
        )
        is_previous = bool(previous and candidate.id == previous.id)
        full_edit = bool(
            source_clip.id == clip.id
            and candidate.review
            and clip.repair_plan
            and (clip.repair_plan.mode == "edit")
            and (candidate.id == clip.repair_plan.source_candidate_id)
        )
        if full_edit:
            if media.duration > 15 or source_job.status != "complete":
                continue
            options.append(
                ReferenceOption(
                    id=f"edit:{candidate.id}",
                    modality="video",
                    label="完整当前候选（编辑来源）",
                    purpose="edit_source",
                    media_id=media.id,
                    source_candidate_id=candidate.id,
                    roles=["edit_source"],
                    duration=media.duration,
                    preparation="ready",
                    steps=["直接绑定完整 source_candidate_id；不裁剪，不添加前段尾视频"],
                )
            )
            continue
        same_states = bool(source_states and source_states <= state_ids)
        if not is_previous and not same_states:
            continue
        roles = (
            ["continuation"]
            if is_previous
            else (
                ["motion", "identity"]
                if source_clip.accepted_candidate_id == candidate.id
                else ["edit_source"]
            )
        )
        options.append(
            ReferenceOption(
                id=f"video:{candidate.id}",
                modality="video",
                label=f"{source_clip.title} · 已选视频"
                if candidate.id == source_clip.accepted_candidate_id
                else f"{source_clip.title} · 修正来源",
                purpose="continuation" if is_previous else "same_state_video",
                media_id=media.id,
                source_candidate_id=candidate.id,
                roles=roles,
                duration=media.duration,
                preparation="excerpt",
                steps=[
                    "查看来源与实际状态",
                    "prepare_media 截取 2–15 秒；接续必须含真实末尾",
                    "读取已完成截取任务",
                ],
            )
        )
    eligible_sources = {o.source_candidate_id: o for o in options if o.modality == "video"}
    for job in select_entities(
        p.jobs,
        actor_id=actor_id,
        subtype="reference",
        statuses=["complete"],
        parent_ids=sorted(selected_refs | set(eligible_sources)),
    ):
        binding = job.result.get("binding", {})
        media_id = job.result.get("media_id")
        media = p.media.get(str(media_id))
        if not media:
            continue
        if binding.get("revision_id") in selected_refs:
            options.append(
                ReferenceOption(
                    id=f"prepared:{job.id}",
                    modality="image",
                    label="已准备的单视图或参考板",
                    purpose="planned_state",
                    media_id=media.id,
                    preparation_job_id=job.id,
                    asset_id=binding["asset_id"],
                    state_id=binding["state_id"],
                    revision_id=binding["revision_id"],
                    preparation="inspect",
                    steps=["read_media 查看精确准备结果"],
                )
            )
        source_id = str(job.request.get("source_candidate_id", ""))
        if source_id in eligible_sources and job.request.get("operation") == "media_excerpt":
            source_option = eligible_sources[source_id]
            if source_option.purpose == "edit_source":
                continue  # Explicit editing must never offer truncated prepared sources.
            kind = job.request.get("kind")
            if kind not in {"video", "audio"}:
                continue
            if not media.duration or not 2 <= media.duration <= 15:
                continue
            if (
                kind == "audio"
                and p.clips[p.candidates[source_id].clip_id].accepted_candidate_id != source_id
            ):
                continue
            if kind == "video" and "continuation" in source_option.roles:
                source_media = p.media[str(p.candidates[source_id].media_id)]
                if (
                    abs(
                        float(job.request.get("end_seconds", 0)) - float(source_media.duration or 0)
                    )
                    > 0.05
                ):
                    continue
            options.append(
                ReferenceOption(
                    id=f"prepared:{job.id}",
                    modality=kind,
                    label="已截取的视频参考" if kind == "video" else "已截取的声音参考",
                    purpose=source_option.purpose if kind == "video" else "sound_characteristics",
                    media_id=media.id,
                    preparation_job_id=job.id,
                    source_candidate_id=source_id,
                    roles=source_option.roles if kind == "video" else ["ambience", "sound"],
                    duration=media.duration,
                    preparation="ready",
                    steps=["核对区间、来源和用途"],
                )
            )
    for asset_plan in clip.asset_plan:
        asset = p.assets[asset_plan.asset_id]
        voice = next(
            (v for v in asset.voice_references if v.id == asset.selected_voice_id and v.media_id),
            None,
        )
        if voice:
            options.append(
                ReferenceOption(
                    id=f"voice:{voice.id}",
                    modality="audio",
                    label=f"{asset.name} · 当前固定音色",
                    purpose="voice",
                    media_id=voice.media_id,
                    asset_id=asset.id,
                    roles=["voice"],
                    voice_reference_id=voice.id,
                    source_candidate_id=voice.source_candidate_id,
                    preparation="ready",
                    duration=voice.end_seconds - voice.start_seconds,
                    steps=["绑定角色当前音色；按本段剧本生成台词"],
                )
            )
    options.sort(key=lambda item: item.id)
    remaining = [o for o in options if after is None or o.id > after]
    page = remaining[: min(max(limit, 1), 200)]
    groups: dict[str, list[ReferenceOption]] = {}
    for item in page:
        groups.setdefault(item.purpose, []).append(item)
    return ReferenceOptions(
        clip_id=clip.id,
        groups=groups,
        boundary_image_slots=int(bool(previous)),
        next_cursor=page[-1].id if len(remaining) > len(page) else None,
    )


def compare_candidates(
    p: StudioProject, clip_id: str, *, after: str | None = None, limit: int = 100
) -> CandidateComparison:
    clip = p.clips.get(clip_id)
    require(clip is not None, "Clip 不存在")
    assert clip is not None
    candidates = select_entities(p.candidates, parent_id=clip.id)
    candidates.sort(key=lambda c: (c.created_at, c.id))
    rows: list[ComparedCandidate] = []
    for index, candidate in enumerate(candidates):
        if after and (candidate.created_at + ":" + candidate.id) <= after:
            continue
        rows.append(
            ComparedCandidate(
                id=candidate.id,
                attempt=index + 1,
                status=candidate.status,
                media_id=candidate.media_id,
                selected=candidate.id == clip.accepted_candidate_id,
                selection=candidate.selection,
                review=candidate.review.model_dump(mode="json") if candidate.review else {},
                issues=candidate.issues,
                required_evidence_count=len(
                    candidate.analysis.get("required_review_media_ids", [])
                ),
            )
        )
    playable = [c.id for c in candidates if c.media_id]
    pair = [clip.accepted_candidate_id] if clip.accepted_candidate_id in playable else []
    pair.extend(i for i in reversed(playable) if i not in pair)
    count = min(max(limit, 1), 200)
    selected = rows[:count]
    last = next((c for c in candidates if selected and c.id == selected[-1].id), None)
    return CandidateComparison(
        clip_id=clip.id,
        candidates=selected,
        default_pair=pair[:2],
        next_cursor=(last.created_at + ":" + last.id) if last and len(rows) > count else None,
    )
