"""Event-level, observed asset handoffs. Film order never implies production order.

All gates and reads share this module. Source candidates are read to validate
provenance, but only executive-published observations cross a director boundary.
"""

from __future__ import annotations

from typing import Any

from .contracts import (
    AssetHandoff,
    AssetHandoffPlan,
    AssetState,
    Candidate,
    Clip,
    MemoryEntry,
    ProductionSet,
    ReadinessItem,
    SetDependencyReport,
    StatePreparationPlan,
    StudioProject,
)
from .memory import anchor_key, digest, memory_record, require
from .repository import select_entities
from .store import StudioError, StudioPrerequisiteError
from .workflow import can_plan_set

HANDOFF_INPUT_POLICY = "incoming-and-bound-states-v1"

HANDOFF_ACTIONS = {
    "configure_set_dependencies",
    "configure_state_preparation",
    "confirm_set_handoff",
    "adopt_observed_reference",
}


def executive_id(p: StudioProject) -> str:
    return select_entities(p.actors, subtype="executive")[0].id


def validate_source(p: StudioProject, set_id: str | None, event_id: str | None) -> ProductionSet:
    source = p.sets.get(str(set_id))
    require(source, f"实拍来源片场不存在：{set_id}")
    assert source is not None
    require(
        event_id in p.story_events and event_id in source.story_event_ids,
        f"实拍来源事件 {event_id} 不属于片场 {source.id} 的已发布事件范围",
    )
    return source


def validate_plan(p: StudioProject, production_set: ProductionSet, plan: AssetHandoffPlan) -> None:
    require(plan.reason.strip(), "资产承接分类必须说明剧情依据")
    require(plan.asset_id in production_set.asset_ids, "承接资产必须属于总导演发布的本片场资产")
    require(
        all(i in p.states and p.states[i].asset_id == plan.asset_id for i in plan.state_ids),
        "承接状态必须存在并属于指定资产",
    )
    require(
        set(plan.target_event_ids) <= set(production_set.story_event_ids),
        "承接的目标事件必须属于本片场",
    )
    if plan.mode == "independent":
        require(not plan.source_set_id and not plan.source_event_id, "独立资产不应声明实拍来源")
    elif plan.mode == "shared":
        source = p.sets.get(str(plan.source_set_id))
        require(
            source and source.id != production_set.id and plan.asset_id in source.asset_ids,
            "共享设定需明确另一片场及其相同资产；首次出现应声明 independent",
        )
        require(not plan.source_event_id, "共享固定设定不等待实拍事件")
    else:
        source = validate_source(p, plan.source_set_id, plan.source_event_id)
        require(source.id != production_set.id, "跨片场承接不能依赖自身；同片场使用真实前段接续")
        require(plan.asset_id in source.asset_ids, "来源片场未发布此资产")
        require(plan.state_ids, "实拍承接需要明确适用状态，不能笼统批准整个资产")
        require(
            plan.required_attributes and all(x.strip() for x in plan.required_attributes),
            "实拍承接需列明应继承的外观、位置、持有关系等具体属性",
        )


def validate_graph(p: StudioProject) -> None:
    visited: set[str] = set()
    stack: list[str] = []

    def visit(identifier: str) -> None:
        require(identifier in p.sets, f"生成依赖片场不存在：{identifier}")
        require(identifier not in stack, "生成依赖有环：" + " → ".join([*stack, identifier]))
        if identifier in visited:
            return
        stack.append(identifier)
        production_set = p.sets[identifier]
        for plan in production_set.asset_handoffs:
            validate_plan(p, production_set, plan)
        sources = [
            str(plan.source_set_id)
            for plan in production_set.asset_handoffs
            if plan.mode == "observed"
        ]
        for dependency in dict.fromkeys([*production_set.depends_on, *sources]):
            visit(dependency)
        stack.pop()
        visited.add(identifier)

    for identifier in p.sets:
        visit(identifier)


def source_clip(p: StudioProject, set_id: str | None, event_id: str | None) -> Clip | None:
    source = validate_source(p, set_id, event_id)
    # An event can span multiple Clips. Its LAST planned Clip completes the event;
    # unrelated later Clips in the same Set must not hold up the handoff.
    clips = [
        c
        for c in select_entities(p.clips, ids=source.clip_ids)
        if not c.archived_at and event_id in c.story_event_ids
    ]
    return max(clips, key=lambda c: c.order) if clips else None


def selected_source(
    p: StudioProject, set_id: str | None, event_id: str | None
) -> tuple[Clip | None, Candidate | None]:
    clip = source_clip(p, set_id, event_id)
    candidate = p.candidates.get(str(clip.accepted_candidate_id)) if clip else None
    if not candidate or not candidate.media_id or candidate.media_id not in p.media:
        return clip, None
    return clip, candidate


def same_source_image(p: StudioProject, left_id: str, right_id: str) -> bool:
    """An excerpt alias preserves evidence only for the same image and candidate."""
    from .image_input_recovery import quarantine

    left, right = p.media.get(left_id), p.media.get(right_id)
    if not left or not right:
        return False
    if (
        not left.mime_type.startswith("image/")
        or not right.mime_type.startswith("image/")
        or quarantine(p, left_id)
        or quarantine(p, right_id)
    ):
        return False
    return left_id == right_id or bool(
        left.source_candidate_id
        and left.source_candidate_id == right.source_candidate_id
        and left.sha256 == right.sha256
    )


def observation(p: StudioProject, identifier: str) -> MemoryEntry:
    record = memory_record(p, identifier)
    require(record and record[0] == "global", "承接依据必须是总导演发布的全局实拍观察")
    assert record is not None
    entry = record[1]
    require(
        entry.status == "effective"
        and entry.basis == "observed"
        and entry.approved_by == executive_id(p)
        and entry.approved_asset_id
        and entry.approved_state_ids
        and entry.approved_attributes
        and entry.evidence_media_ids
        and entry.source_seconds is not None,
        "实拍观察尚未批准、已失效或缺少图片及时间点；计划图不能代替实拍观察",
    )
    candidate = p.candidates.get(str(entry.source_candidate_id))
    require(
        candidate and p.clips[candidate.clip_id].accepted_candidate_id == candidate.id,
        "实拍观察来自未选用或已替换的候选，请按当前选用结果重新核对",
    )
    job = p.jobs.get(str(entry.source_job_id))
    require(
        job
        and job.status == "complete"
        and job.request.get("operation") == "media_excerpt"
        and job.request.get("kind") == "image"
        and job.request.get("source_candidate_id") == entry.source_candidate_id
        and job.request.get("start_seconds") == entry.source_seconds
        and entry.evidence_media_ids == [job.result.get("media_id")],
        "实拍观察的候选、截图任务、时间点和媒体记录不一致",
    )
    assert candidate is not None and job is not None
    video = p.media.get(str(candidate.media_id))
    source = job.request.get("source", {})
    require(
        video
        and source.get("media_id") == video.id
        and source.get("sha256") == video.sha256
        and source.get("candidate_id") == candidate.id,
        "截图来源的视频版本或哈希已变化，请从当前实际选片重新取证",
    )
    from .image_input_recovery import quarantine

    for identifier in entry.evidence_media_ids:
        media = p.media.get(identifier)
        require(media and media.mime_type.startswith("image/"), "实拍观察图片不可用")
        require(not quarantine(p, identifier), "已隔离的图片不能认证实拍状态承接")
    return entry


def scope_covers(
    p: StudioProject, entry: MemoryEntry, production_set: ProductionSet, clip: Clip | None
) -> bool:
    scope = entry.scope
    if scope.set_ids and production_set.id not in scope.set_ids:
        return False
    if clip is None:
        if scope.clip_ids and not set(scope.clip_ids).intersection(production_set.clip_ids):
            return False
        if scope.event_ids and not set(scope.event_ids).intersection(
            production_set.story_event_ids
        ):
            return False
    if clip:
        if scope.clip_ids and clip.id not in scope.clip_ids:
            return False
        if scope.event_ids and not set(scope.event_ids) & set(clip.story_event_ids):
            return False
        if (
            scope.ends_at
            and clip.starts_at
            and anchor_key(p, clip.starts_at) > anchor_key(p, scope.ends_at)
        ):
            return False
        end = clip.ends_at or clip.starts_at
        if scope.starts_at and end and anchor_key(p, end) < anchor_key(p, scope.starts_at):
            return False
    return True


def evidence_snapshot(p: StudioProject, entries: list[MemoryEntry]) -> list[dict[str, Any]]:
    return [
        {
            "observation_id": entry.id,
            "version": entry.version,
            "source_candidate_id": entry.source_candidate_id,
            "source_seconds": entry.source_seconds,
            "state_ids": entry.approved_state_ids,
            "attributes": entry.approved_attributes,
            "scope": entry.scope.model_dump(mode="json"),
            "media": [{"id": i, "sha256": p.media[i].sha256} for i in entry.evidence_media_ids],
        }
        for entry in entries
    ]


def source_snapshot(p: StudioProject, plan: AssetHandoffPlan) -> dict[str, Any]:
    clip, candidate = selected_source(p, plan.source_set_id, plan.source_event_id)
    actual = (candidate.confirmed_end_state or candidate.end_state) if candidate else None
    return {
        "plan": AssetHandoffPlan.model_validate(
            plan.model_dump(exclude={"confirmation"})
        ).model_dump(),
        "source_clip_id": clip.id if clip else None,
        "candidate_id": candidate.id if candidate else None,
        "media_sha256": p.media[str(candidate.media_id)].sha256 if candidate else None,
        "actual_end_state": actual.model_dump() if actual else None,
        "event_revision": p.story_events[str(plan.source_event_id)].revision,
        "states": [
            p.states[i].model_dump(
                exclude={
                    "revision_ids",
                    "repair_episodes",
                    "preparation_history",
                    "selected_revisions",
                }
            )
            for i in plan.state_ids
        ],
    }


def handoff_evidence(
    p: StudioProject,
    production_set: ProductionSet,
    plan: AssetHandoffPlan,
    ids: list[str],
    clip: Clip | None = None,
    *,
    source_checked: bool = False,
) -> list[MemoryEntry]:
    origin, source = selected_source(p, plan.source_set_id, plan.source_event_id)
    require(source, "等待来源事件对应 Clip 的真实选用结果；不需要等待整个片场完成")
    assert origin is not None and source is not None
    if not source_checked:
        prerequisites = dependency_report(p, origin.set_id, origin).blockers
        require(
            not prerequisites,
            "来源片段自身的实拍承接尚未核对："
            + (prerequisites[0].description if prerequisites else ""),
        )
    require(ids and len(ids) == len(set(ids)), "需要不重复的已批准实拍观察")
    entries = [observation(p, i) for i in ids]
    require(
        all(
            e.source_candidate_id == source.id
            and e.approved_asset_id == plan.asset_id
            and scope_covers(p, e, production_set, clip)
            for e in entries
        ),
        "观察来源或适用范围与此承接不匹配，请核对来源片场、事件、资产和目标范围",
    )
    for state_id in plan.state_ids:
        attributes = {
            attribute
            for entry in entries
            if state_id in entry.approved_state_ids
            for attribute in entry.approved_attributes
        }
        require(
            set(plan.required_attributes) <= attributes,
            f"状态 {state_id} 缺少已查看并批准的属性："
            + "、".join(sorted(set(plan.required_attributes) - attributes)),
        )
    return entries


def reference_fingerprint(p: StudioProject, ids: list[str]) -> str:
    values = []
    for identifier in ids:
        ref = p.references.get(identifier)
        require(
            ref
            and ref.media_id in p.media
            and ref.status == "selected"
            and p.states[ref.state_id].selected_revisions.get(ref.view) == identifier,
            f"承接参考 {identifier} 尚未选用或已被其他版本替换",
        )
        assert ref is not None
        values.append(
            [
                ref.model_dump(exclude={"prompt", "review_history"}),
                p.media[str(ref.media_id)].sha256,
            ]
        )
    return digest(values)


def relevant(
    plan: AssetHandoffPlan,
    clip: Clip | None,
    *,
    bound_state_ids: frozenset[str] = frozenset(),
) -> bool:
    if clip is None:
        return True
    # A planned end state is an output, not an incoming dependency. An explicitly
    # supplied target/guidance image still retains its real source requirement.
    states = {
        item.start_state_id
        for item in clip.asset_plan
        if item.asset_id == plan.asset_id and item.start_state_id
    } | bound_state_ids
    return (
        bool(states)
        and (not plan.state_ids or bool(states & set(plan.state_ids)))
        and (
            not plan.target_event_ids
            or bool(set(clip.story_event_ids) & set(plan.target_event_ids))
        )
    )


def handoff_status(
    p: StudioProject,
    production_set: ProductionSet,
    plan: AssetHandoff,
    clip: Clip | None = None,
    *,
    ancestry: frozenset[str] = frozenset(),
    report_cache: dict[tuple[str, str | None], SetDependencyReport] | None = None,
) -> dict[str, Any]:
    source_event = p.story_events.get(str(plan.source_event_id))
    value: dict[str, Any] = {
        **plan.model_dump(exclude={"confirmation"}),
        "asset_name": p.assets[plan.asset_id].name,
        "source_event_title": source_event.title if source_event else None,
        "status": plan.mode,
        "owner_id": executive_id(p),
        "next_action": "可提前准备，按已发布设定选材",
    }
    if plan.mode != "observed":
        return value
    try:
        origin, source = selected_source(p, plan.source_set_id, plan.source_event_id)
        value["source_version"] = digest(source_snapshot(p, plan))
        if source and origin:
            prerequisites = dependency_report(
                p,
                origin.set_id,
                origin,
                ancestry=ancestry | {production_set.id},
                report_cache=report_cache,
            ).blockers
            if prerequisites:
                value["status"] = "waiting_source"
                value["next_action"] = (
                    "来源片段已保留，但其自身承接仍待总导演核对：" + prerequisites[0].description
                )
                return value
        value["status"] = "waiting_source" if source is None else "needs_confirmation"
        value["next_action"] = (
            "等待来源事件对应 Clip 选用；本片场可继续规划"
            if source is None
            else "总导演查看来源实拍、发布适用观察并核对参考图后确认承接"
        )
        confirmation = plan.confirmation
        if confirmation:
            entries = handoff_evidence(
                p, production_set, plan, confirmation.observation_ids, clip, source_checked=True
            )
            current = digest([source_snapshot(p, plan), evidence_snapshot(p, entries)])
            require(
                current == confirmation.source_fingerprint, "来源选片、末态、事件或状态约定已变化"
            )
            require(
                reference_fingerprint(p, confirmation.reference_ids)
                == confirmation.reference_fingerprint,
                "已批准的参考版本或图片内容已变化",
            )
            value["status"] = "ready"
            value["next_action"] = "使用已确认的观察与参考；提交仍检查本 Clip 的其他前置条件"
            value["observation_ids"] = confirmation.observation_ids
            value["reference_ids"] = confirmation.reference_ids
            value["confirmed_at"] = confirmation.created_at
    except StudioError as error:
        value["status"] = "stale" if plan.confirmation else "invalid"
        value["next_action"] = str(error)
    return value


def blocker(p: StudioProject, code: str, entity: str, text: str, **details: Any) -> ReadinessItem:
    return ReadinessItem(
        key=f"{code}:{entity}",
        kind=code,
        description=text,
        owner_id=executive_id(p),
        owner_role="executive",
        entity_id=entity,
        details={"can_plan": True, "can_generate": False, **details},
    )


def dependency_report(
    p: StudioProject,
    set_id: str,
    clip: Clip | None = None,
    *,
    ancestry: frozenset[str] = frozenset(),
    report_cache: dict[tuple[str, str | None], SetDependencyReport] | None = None,
) -> SetDependencyReport:
    require(set_id not in ancestry, "生成依赖有环，请总导演修正来源约定")
    cache = report_cache if report_cache is not None else {}
    key = (set_id, clip.id if clip else None)
    if key in cache:
        return cache[key]
    production_set = p.sets[set_id]
    report = SetDependencyReport(
        set_id=production_set.id,
        policy=p.handoff_policy,
        film_order=production_set.order + 1,
        can_plan=can_plan_set(p, production_set),
        dependency_revision=production_set.dependency_revision,
        preserved_clip_ids=[
            c.id
            for c in select_entities(p.clips, ids=production_set.clip_ids)
            if c.accepted_candidate_id
        ],
    )
    for dependency in production_set.depends_on:
        source = p.sets.get(dependency)
        report.items.append(
            {
                "id": dependency,
                "mode": "whole_set",
                "source_set_id": dependency,
                "status": "ready" if source and source.status == "complete" else "waiting_source",
            }
        )
        if not source or source.status != "complete":
            report.blockers.append(
                blocker(
                    p, "set_dependency", dependency, f"等待明确声明的前置片场 {dependency} 完成"
                )
            )
    recipe = p.reference_plans.get(str(clip.reference_plan_id)) if clip else None
    bindings = recipe.bindings if recipe else []
    plans = [
        plan
        for plan in production_set.asset_handoffs
        if relevant(
            plan,
            clip,
            bound_state_ids=frozenset(
                item.state_id for item in bindings if item.asset_id == plan.asset_id
            ),
        )
    ]
    requirements: list[tuple[str, str | None]] = (
        [(item.asset_id, item.start_state_id) for item in clip.asset_plan if item.start_state_id]
        if clip
        else [(item, None) for item in production_set.asset_ids]
    )
    requirements.extend((item.asset_id, item.state_id) for item in bindings)
    for asset_id, state_id in dict.fromkeys(requirements):
        if any(
            plan.asset_id == asset_id
            and (not state_id or not plan.state_ids or state_id in plan.state_ids)
            for plan in plans
        ):
            continue
        report.blockers.append(
            blocker(
                p,
                "handoff_unclassified",
                f"{production_set.id}:{asset_id}:{state_id or '*'}",
                f"资产「{p.assets[asset_id].name}」尚未声明独立、共享设定或继承实拍；由总导演补齐",
                asset_id=asset_id,
                expected_state_id=state_id,
                set_id=production_set.id,
            )
        )
    statuses = {}
    for plan in plans:
        status = handoff_status(
            p, production_set, plan, clip, ancestry=ancestry, report_cache=cache
        )
        statuses[plan.id] = status
        report.items.append(status)
        if status["status"] not in {"independent", "shared", "ready"}:
            report.blockers.append(
                blocker(
                    p,
                    "asset_handoff",
                    plan.id,
                    f"「{p.assets[plan.asset_id].name}」承接未就绪：{status['next_action']}",
                    **status,
                )
            )
        elif clip and not clip.accepted_candidate_id and plan.mode == "observed":
            recipe = p.reference_plans.get(str(clip.reference_plan_id))
            unverified = (
                [
                    b.revision_id
                    for b in recipe.bindings
                    if b.asset_id == plan.asset_id
                    and b.state_id in plan.state_ids
                    and b.revision_id not in status.get("reference_ids", [])
                ]
                if recipe
                else []
            )
            if unverified:
                report.blockers.append(
                    blocker(
                        p,
                        "handoff_reference",
                        plan.id,
                        "当前清单使用了未经此次实拍承接核对的参考版本，请总导演确认或替换",
                        asset_id=plan.asset_id,
                        expected_state_ids=plan.state_ids,
                        reference_ids=unverified,
                    )
                )
    if clip:
        report.blockers.extend(state_generation_blockers(p, production_set, clip, plans, statuses))
    cache[key] = report
    return report


def require_handoffs(p: StudioProject, clip: Clip) -> None:
    blocked = dependency_report(p, clip.set_id, clip).blockers
    if blocked:
        raise StudioPrerequisiteError(
            blocked[0].description,
            details={
                "kind": "set_handoff",
                "clip_id": clip.id,
                "can_plan": True,
                "owner_id": executive_id(p),
                "blockers": [b.model_dump() for b in blocked],
            },
            recovery="由总导演根据 blockers 核对资产承接；本片场可继续规划，不可重复提交生成",
        )


def validate_state_preparation(
    p: StudioProject, state: AssetState, plan: StatePreparationPlan
) -> None:
    require(plan.reason.strip(), "状态图准备方式需要说明依据")
    if plan.mode == "independent":
        require(not plan.source_set_id and not plan.source_event_id, "独立状态图不应声明实拍来源")
    else:
        source = validate_source(p, plan.source_set_id, plan.source_event_id)
        require(state.asset_id in source.asset_ids, "来源片场未发布此资产")
        if plan.mode == "observed":
            require(
                plan.required_attributes and all(s.strip() for s in plan.required_attributes),
                "派生状态图需明确继承的实拍属性",
            )


def require_state_source(p: StudioProject, state: AssetState, ids: list[str]) -> list[MemoryEntry]:
    plan = state.preparation
    if not plan:
        if state.is_base:
            return []
        raise StudioPrerequisiteError(
            f"「{p.assets[state.asset_id].name}/{state.label}」尚未声明状态图准备方式",
            details={
                "kind": "state_preparation",
                "state_id": state.id,
                "asset_id": state.asset_id,
                "owner_id": executive_id(p),
                "allowed_modes": ["independent", "observed", "transition_target"],
            },
            recovery="总导演调用 configure_state_preparation 声明准备方式后再生成图片",
        )
    validate_state_preparation(p, state, plan)
    if plan.mode != "observed":
        return []
    _, source = selected_source(p, plan.source_set_id, plan.source_event_id)
    if not source:
        raise StudioPrerequisiteError(
            f"「{state.label}」需等 {plan.source_set_id}/{plan.source_event_id} 的实际选片，"
            "当前可先准备文字和视角",
            details={
                "kind": "state_source",
                "state_id": state.id,
                "asset_id": state.asset_id,
                "source_set_id": plan.source_set_id,
                "source_event_id": plan.source_event_id,
                "owner_id": executive_id(p),
                "can_prepare_description": True,
                "can_generate_reference": False,
            },
            recovery="等待来源 Clip 选用后查看、批准截图；准备描述和视角无需等待",
        )
    entries = [observation(p, i) for i in ids]
    require(
        entries
        and all(
            e.source_candidate_id == source.id
            and e.approved_asset_id == state.asset_id
            and state.id in e.approved_state_ids
            for e in entries
        ),
        "派生状态图必须显式传入当前选用实拍的已批准 source_observation_ids",
    )
    approved = {attribute for entry in entries for attribute in entry.approved_attributes}
    require(set(plan.required_attributes) <= approved, "实拍观察未覆盖此状态要求继承的全部属性")
    return entries


def state_generation_blockers(
    p: StudioProject,
    production_set: ProductionSet,
    clip: Clip,
    plans: list[AssetHandoff],
    statuses: dict[str, dict[str, Any]],
) -> list[ReadinessItem]:
    result = []
    for item in clip.asset_plan:
        state = p.states.get(item.start_state_id)
        if not state:
            continue  # The normal state-plan gate reports missing states.
        preparation = state.preparation
        actual_previous = False
        if clip.order:
            previous = p.clips[production_set.clip_ids[clip.order - 1]]
            candidate = p.candidates.get(str(previous.accepted_candidate_id))
            actual_previous = bool(
                candidate
                and candidate.confirmed_end_state
                and state.id in candidate.confirmed_end_state.visual_state_ids
            )
        handed = [plan for plan in plans if plan.mode == "observed" and state.id in plan.state_ids]
        confirmed = [plan for plan in handed if statuses[plan.id]["status"] == "ready"]
        try:
            if preparation and preparation.mode == "transition_target":
                require(
                    actual_previous or confirmed,
                    "目标设计图不能作为已发生的入场状态；需真实前段末态或总导演实拍承接确认",
                )
            elif preparation and preparation.mode == "observed":
                if actual_previous:
                    continue
                if preparation.source_set_id == production_set.id:
                    origin = source_clip(p, preparation.source_set_id, preparation.source_event_id)
                    require(
                        origin is not None and origin.order < clip.order,
                        "状态图的实拍来源是本段自身或后续片段，会形成等待循环；"
                        "由总导演改为 transition_target 或纠正来源事件",
                    )
                matching = [
                    plan
                    for plan in confirmed
                    if plan.source_set_id == preparation.source_set_id
                    and plan.source_event_id == preparation.source_event_id
                ]
                if matching:
                    ids = list(
                        dict.fromkeys(
                            i
                            for plan in matching
                            if plan.confirmation
                            for i in plan.confirmation.observation_ids
                        )
                    )
                else:
                    recipe = p.reference_plans.get(str(clip.reference_plan_id))
                    ids = (
                        list(
                            dict.fromkeys(
                                i
                                for binding in recipe.bindings
                                if binding.state_id == state.id
                                for i in p.references[binding.revision_id].source_observation_ids
                            )
                        )
                        if recipe
                        else []
                    )
                require_state_source(p, state, ids)
            elif not preparation and not state.is_base:
                require_state_source(p, state, [])
        except StudioError as error:
            result.append(
                blocker(
                    p,
                    "state_preparation",
                    state.id,
                    f"「{p.assets[state.asset_id].name}/{state.label}」：{error}",
                    asset_id=state.asset_id,
                    expected_state_id=state.id,
                    preparation=preparation.model_dump() if preparation else None,
                )
            )
    return result


def state_source_context(p: StudioProject, state: AssetState) -> dict[str, Any]:
    plan = state.preparation
    value: dict[str, Any] = {
        "id": state.id,
        "asset_id": state.asset_id,
        "preparation": plan.model_dump() if plan else None,
        "preparation_revision": state.preparation_revision,
    }
    if plan and plan.mode == "observed":
        try:
            clip, source = selected_source(p, plan.source_set_id, plan.source_event_id)
            actual = (source.confirmed_end_state or source.end_state) if source else None
            # No unapproved candidate/Clip IDs or private plans cross the role boundary.
            value["source_version"] = digest(
                [
                    clip.id if clip else None,
                    source.id if source else None,
                    p.media[str(source.media_id)].sha256 if source else None,
                    actual.model_dump() if actual else None,
                    p.story_events[str(plan.source_event_id)].revision,
                ]
            )
            value["status"] = "needs_observation" if source else "waiting_source"
        except StudioError as error:
            value.update(status="invalid", reason=str(error))
    else:
        value["status"] = "design_ready" if plan or state.is_base else "unclassified"
    return value


def notify_changed_sources(p: StudioProject) -> None:
    """Targeted wakeups on semantic source changes, never on job polling ticks."""
    maps = p.__dict__.get("_repositories")
    if not maps:
        return
    changed_sets: set[str] = set()
    changed_assets: set[str] = set()
    changed_events: set[str] = set()
    fields = {
        "clips": ("accepted_candidate_id", "archived_at", "story_event_ids"),
        "candidates": ("media_id", "confirmed_end_state", "end_state"),
        "states": ("preparation", "selected_revisions", "starts_at", "ends_at", "attributes"),
        "memory_entries": ("status", "version", "approved_attributes", "scope"),
        "story_events": ("revision",),
    }
    from .context_cache import context_changes

    for kind, identifier, value, before, _ in context_changes(p)[1]:
        if kind not in fields or value is None:
            continue
        before = before or {}
        current = value.model_dump(mode="json")
        if all(before.get(key) == current.get(key) for key in fields[kind]):
            continue
        if kind == "clips":
            changed_sets.add(value.set_id)
        elif kind == "candidates":
            clip = p.clips[value.clip_id]
            if clip.accepted_candidate_id == identifier:
                changed_sets.add(clip.set_id)
        elif kind == "states":
            changed_assets.add(value.asset_id)
        elif kind == "story_events":
            changed_events.add(identifier)
        elif value.approved_asset_id:
            changed_assets.add(value.approved_asset_id)
    if not changed_sets and not changed_assets and not changed_events:
        return
    from .handoff_actions import wake_target

    executive_work = False
    for production_set in p.sets.values():
        relevant_plans = [
            plan
            for plan in production_set.asset_handoffs
            if plan.mode == "observed"
            and (
                plan.source_set_id in changed_sets
                or plan.asset_id in changed_assets
                or plan.source_event_id in changed_events
            )
        ]
        if not relevant_plans:
            continue
        wake_target(p, production_set.id)
        executive_work |= any(
            handoff_status(p, production_set, plan)["status"]
            in {"needs_confirmation", "stale", "invalid"}
            for plan in relevant_plans
        )
    if executive_work:
        actor = p.actors[executive_id(p)]
        if actor.status != "complete":
            actor.request_wake()
            if (
                not actor.paused
                and not actor.native_session_blocked
                and actor.status in {"idle", "waiting"}
            ):
                actor.status = "ready"
