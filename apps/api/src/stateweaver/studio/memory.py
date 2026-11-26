"""Effective, scoped story memory and event coordinates (never execution history)."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from stateweaver.studio.contracts import (
    Actor,
    AssetState,
    Candidate,
    Clip,
    MemoryEntry,
    MemoryScope,
    MemorySource,
    StoryAnchor,
    StoryEvent,
    StudioProject,
    new_id,
    now,
)
from stateweaver.studio.repository import MemoryEntries, select_entities
from stateweaver.studio.store import StudioError, StudioPrerequisiteError


def require(value: object, message: str) -> None:
    if not value:
        raise StudioError(message)


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def anchor_key(p: StudioProject, anchor: StoryAnchor) -> tuple[float, int]:
    require(anchor.event_id in p.story_events, "剧情事件不存在")
    return p.story_events[anchor.event_id].order, int(anchor.position == "after")


def validate_scope(p: StudioProject, scope: MemoryScope) -> None:
    for ids, values, label in (
        (scope.asset_ids, p.assets, "资产"),
        (scope.set_ids, p.sets, "片场"),
        (scope.clip_ids, p.clips, "Clip"),
        (scope.event_ids, p.story_events, "剧情事件"),
    ):
        require(len(ids) == len(set(ids)), f"{label}范围不能重复")
        require(all(i in values for i in ids), f"范围中的{label}不存在")
    if scope.starts_at:
        anchor_key(p, scope.starts_at)
    if scope.ends_at:
        anchor_key(p, scope.ends_at)
    if scope.starts_at and scope.ends_at:
        require(
            anchor_key(p, scope.starts_at) <= anchor_key(p, scope.ends_at),
            "记忆适用事件范围的结束不能早于开始",
        )
    if scope.set_ids and scope.clip_ids:
        require(
            all(p.clips[i].set_id in scope.set_ids for i in scope.clip_ids),
            "Clip 不属于声明的片场范围",
        )
    if scope.event_ids and scope.clip_ids:
        require(
            all(
                not p.clips[i].story_event_ids
                or set(p.clips[i].story_event_ids) & set(scope.event_ids)
                for i in scope.clip_ids
            ),
            "Clip 已明确的剧情事件与记忆事件范围不相交；请修正范围，不能把多个条件当成任选其一",
        )


def correct_memory_scope(p: StudioProject, actor: Actor, data: dict[str, Any]) -> dict[str, Any]:
    """Correct applicability explicitly without rewriting the remembered content."""
    require(actor.role == "executive", "记忆适用范围纠正由总导演统一决定")
    record = memory_record(p, str(data.get("entry_id")))
    require(record, "待纠正的记忆不存在")
    assert record is not None
    owner, old = record
    memory = p.global_memory if owner == "global" else p.sets[owner].memory
    require(data.get("memory_revision") == memory.revision, "记忆已更新，请重新读取当前版本")
    require(old.status == "effective", "请纠正当前有效版本，历史记录保持不变")
    require(old.section != "screenplay", "已确认剧本事实的内容与适用范围不可改写")
    reason = str(data.get("reason", "")).strip()
    require(reason, "纠正范围需要依据和理由")
    scope = MemoryScope.model_validate(data["scope"])
    validate_scope(p, scope)
    if owner != "global":
        require(
            (not scope.set_ids or scope.set_ids == [owner])
            and all(p.clips[i].set_id == owner for i in scope.clip_ids),
            "片场本地记忆只能适用该片场，跨片场安排请记录到全局",
        )
    require(scope != old.scope, "适用范围没有改变，无需创建修订")
    updated = old.model_copy(deep=True)
    updated.id, updated.created_at = new_id(), now()
    updated.version += 1
    updated.scope = scope
    updated.reason = reason
    updated.supersedes_ids, updated.superseded_by = [old.id], None
    updated.sources.append(MemorySource(kind="memory", id=old.id, note="范围纠正依据：" + reason))
    old.status, old.superseded_by = "superseded", updated.id
    before = memory.revision
    memory.story.append(updated)
    memory.revision += 1
    return {
        "entry_id": updated.id,
        "supersedes_ids": [old.id],
        "scope": "global" if owner == "global" else "set",
        "set_id": None if owner == "global" else owner,
        "summary": old.text[:500],
        "reason": reason,
        "memory_revision_before": before,
        "memory_revision_after": memory.revision,
        "before_scope": old.scope.model_dump(mode="json"),
        "after_scope": scope.model_dump(mode="json"),
    }


def memory_record(p: StudioProject, identifier: str) -> tuple[str, MemoryEntry] | None:
    repos = p.__dict__.get("_repositories")
    if repos:
        entry = repos["memory_entries"].get(identifier)
        return (repos["memory_entries"].parents[identifier], entry) if entry else None
    for scope, memory in [
        ("global", p.global_memory),
        *[(s.id, s.memory) for s in p.sets.values()],
    ]:
        for entry in memory.story:
            if entry.id == identifier:
                return scope, entry
    return None


def selected_source(p: StudioProject, entry: MemoryEntry) -> bool:
    if entry.kind != "observation" and entry.basis != "observed":
        return True
    if entry.approved_by:
        actor = p.actors.get(entry.approved_by)
        return bool(
            actor
            and actor.role == "executive"
            and entry.approved_state_ids
            and entry.approved_attributes
            and entry.source_job_id
        )
    candidate = p.candidates.get(str(entry.source_candidate_id))
    clip = p.clips.get(candidate.clip_id) if candidate else None
    return bool(
        clip and not clip.archived_at and clip.accepted_candidate_id == entry.source_candidate_id
    )


def effective_entries(
    p: StudioProject,
    scope: str,
    *,
    context: dict[str, list[str]] | None = None,
    clip: Clip | None = None,
    limit: int | None = None,
) -> list[MemoryEntry]:
    memory = p.global_memory if scope == "global" else p.sets[scope].memory
    filters: dict[str, Any] = {"parent_id": scope, "status": "effective"}
    if context is not None:
        filters["memory_context"] = context
    if limit is not None:
        filters["limit"] = limit
        filters["effective_authority"] = True
    if isinstance(memory.story, MemoryEntries):
        entries = memory.story.entries.select(**filters)
    else:
        from stateweaver.studio.repository import matches

        entries = [e for e in memory.story if matches(e, filters, scope)]
    result = []
    for entry in entries:
        if entry.section == "log" or not selected_source(p, entry):
            continue
        if (
            clip
            and entry.approved_state_ids
            and not set(entry.approved_state_ids)
            & {s for x in clip.asset_plan for s in (x.start_state_id, x.end_state_id) if s}
        ):
            continue
        if (
            clip
            and clip.starts_at
            and entry.scope.ends_at
            and anchor_key(p, clip.starts_at) > anchor_key(p, entry.scope.ends_at)
        ):
            continue
        if clip and (clip.ends_at or clip.starts_at) and entry.scope.starts_at:
            end = clip.ends_at or clip.starts_at
            assert end is not None
            if anchor_key(p, end) < anchor_key(p, entry.scope.starts_at):
                continue
        result.append(entry)
    return sorted(result, key=lambda e: (e.created_at, e.id))


def validate_sources(p: StudioProject, sources: list[MemorySource]) -> None:
    for source in sources:
        exists = (
            source.id in {f.id for f in p.screenplay.facts}
            if source.kind == "screenplay_fact"
            else memory_record(p, source.id) is not None
            if source.kind in {"memory", "decision"}
            else source.id in p.candidates
            if source.kind == "candidate"
            else source.id in p.jobs
            if source.kind == "job"
            else source.id in p.media
            if source.kind == "media"
            else source.id in p.transcript and p.transcript[source.id].kind == "user"
        )
        require(exists, "记忆依据必须是本项目内存在的事实、记录或证据")


def write_memory(p: StudioProject, actor: Actor, data: dict[str, Any]) -> dict[str, Any]:
    scope = str(data.get("scope", "set"))
    if scope == "global":
        require(actor.role == "executive", "只有总导演可以写入全局记忆")
        memory = p.global_memory
    else:
        require(
            actor.role == "set_director" and actor.set_id in p.sets,
            "只有片场导演可以写入自己的片场记忆",
        )
        require(scope in {"set", actor.set_id}, "不能写入其他片场的记忆")
        scope = str(actor.set_id)
        memory = p.sets[scope].memory
    require(data.get("memory_revision") == memory.revision, "记忆版本冲突")
    entry = MemoryEntry.model_validate(data["entry"])
    require(entry.text.strip(), "记忆内容不能为空")
    require(not memory_record(p, entry.id), "记忆 ID 已存在，修正请明确 supersedes_ids")
    require(
        not entry.approved_by
        and not entry.approved_asset_id
        and not entry.source_job_id
        and not entry.approved_state_ids
        and not entry.approved_attributes,
        "局部证据需要通过总导演 approve_asset_observation 单独确认",
    )
    require(entry.status == "effective" and not entry.superseded_by, "新记忆应为当前有效记录")
    entry.actor_id = actor.id
    if entry.section == "legacy":
        entry.section = (
            ("events" if entry.kind == "observation" else "directives")
            if scope == "global"
            else "set_shared"
        )
    allowed = (
        {"screenplay", "events", "directives"}
        if scope == "global"
        else {"set_shared", "clip_plan", "clip_actual", "clip_continuation"}
    )
    require(entry.section in allowed, "记忆分区与写入范围不一致；执行日志请留在任务记录")
    require(entry.section != "screenplay", "剧本事实来自已确认剧本，不能通过记忆工具改写")
    if scope != "global":
        require(not entry.scope.set_ids or entry.scope.set_ids == [scope], "不能写入其他片场的记忆")
        entry.scope.set_ids = [scope]
        require(
            all(p.clips.get(i) and p.clips[i].set_id == scope for i in entry.scope.clip_ids),
            "不能写入其他片场的 Clip 记忆",
        )
        if entry.section.startswith("clip_"):
            require(len(entry.scope.clip_ids) == 1, "Clip 记忆必须明确一个所属 Clip")
    validate_scope(p, entry.scope)
    validate_sources(p, entry.sources)
    if entry.state_boundary:
        from .production_contracts import validate_state_boundary

        require(scope == "global", "Source state boundaries are executive global decisions")
        validate_state_boundary(p, actor, entry)
    require(all(i in p.media for i in entry.evidence_media_ids), "记忆证据必须来自本项目媒体")
    for attribute in entry.attributes:
        require(attribute.subject_id in p.assets, "剧情属性的主体不属于本项目资产目录")
    if entry.basis == "observed" or entry.kind == "observation":
        entry.basis = "observed"
        entry.kind = "observation"
        require(selected_source(p, entry), "只有当前已选候选的观察才能写入权威记忆")
        candidate = p.candidates[str(entry.source_candidate_id)]
        if scope != "global":
            require(p.clips[candidate.clip_id].set_id == scope, "不可写入其他片场的观察")
        else:
            require(
                entry.scope.clip_ids or entry.scope.set_ids or entry.scope.event_ids,
                "全局实际结果需明确传播到哪些事件、Set 或 Clip",
            )
            require(entry.reason.strip(), "总导演需要说明实际偏差的传播依据")
    if entry.basis in {"unknown", "provisional"}:
        require(entry.kind != "observation", "未知或临时制作决定不能标为实际观察")
    if entry.basis == "fact":
        require(
            entry.sources and all(s.kind in {"screenplay_fact", "user"} for s in entry.sources),
            "确认事实必须引用已确认剧本或用户记录；制作安排用计划，实际画面用观察",
        )
    if entry.basis == "provisional":
        require(
            actor.role == "executive" and entry.reason.strip() and entry.sources,
            "临时制作决定由总导演记录依据、适用范围和原因",
        )
        require(
            entry.scope.clip_ids or entry.scope.set_ids or entry.scope.event_ids,
            "临时制作决定需要明确适用范围",
        )
    current = effective_entries(p, scope)
    comparable = {
        "text",
        "section",
        "basis",
        "scope",
        "attributes",
        "source_candidate_id",
        "state_boundary",
    }
    signature = entry.model_dump(include=comparable, mode="json")
    duplicate = next(
        (e for e in current if e.model_dump(include=comparable, mode="json") == signature), None
    )
    if (
        duplicate
        and "asset_state_ids" not in data
        and (
            not entry.supersedes_ids
            or (
                entry.supersedes_ids == duplicate.supersedes_ids
                and entry.sources == duplicate.sources
                and entry.reason == duplicate.reason
            )
        )
    ):
        return {
            "entry_id": duplicate.id,
            "scope": scope,
            "summary": duplicate.text[:500],
            "no_progress": True,
        }
    replaced = []
    for identifier in entry.supersedes_ids:
        record = memory_record(p, identifier)
        require(record and record[0] == scope, "被替代记忆不属于当前范围")
        assert record is not None
        old = record[1]
        require(old.status == "effective", "只能替代当前有效版本")
        require(old.section != "screenplay", "已确认剧本事实不可替代")
        require(
            old.scope == entry.scope and old.section == entry.section,
            "替代必须具有相同适用范围和分区；局部新安排请单独记录，不能隐藏范围外的旧事实",
        )
        require(
            not old.record_key or old.record_key == entry.record_key,
            "替代应延续同一对象的 record_key，不能隐藏另一条约定",
        )
        require(
            not old.attributes
            or {(a.subject_id, a.property) for a in old.attributes}
            == {(a.subject_id, a.property) for a in entry.attributes},
            "替代需针对同一主体和属性，其他内容应单独记录",
        )
        require(entry.reason.strip(), "修正记忆需要替代理由")
        old.status, old.superseded_by = "superseded", entry.id
        replaced.append(old)
    entry.version = max((e.version for e in replaced), default=0) + 1
    before = memory.revision
    before_states = list(memory.asset_state_ids) if "asset_state_ids" in data else []
    if "asset_state_ids" in data:
        require(all(i in p.states for i in data["asset_state_ids"]), "引用的资产状态不存在")
        memory.asset_state_ids = list(data["asset_state_ids"])
    memory.story.append(entry)
    memory.revision += 1
    return {
        "entry_id": entry.id,
        "entry_kind": entry.kind,
        "section": entry.section,
        "scope": "global" if scope == "global" else "set",
        "set_id": actor.set_id,
        "summary": entry.text[:500],
        "supersedes_ids": entry.supersedes_ids,
        "memory_revision_before": before,
        "memory_revision_after": memory.revision,
        **(
            {
                "before_asset_state_ids": before_states[:64],
                "asset_state_ids": list(memory.asset_state_ids[:64]),
                "asset_state_count_before": len(before_states),
                "asset_state_count_after": len(memory.asset_state_ids),
                "asset_state_ids_truncated": max(len(before_states), len(memory.asset_state_ids))
                > 64,
            }
            if "asset_state_ids" in data
            else {}
        ),
    }


def state_at(
    p: StudioProject,
    asset_id: str,
    stamp: float,
    anchor: StoryAnchor | None = None,
    *,
    expected_state_id: str | None = None,
    clip_id: str | None = None,
    boundary: str = "start",
) -> AssetState:
    require(asset_id in p.assets, "资产不存在")
    if anchor is None:
        states = select_entities(p.states, parent_id=asset_id, story_time_max=stamp, limit=1)
        require(states, "资产在此故事时间尚无状态")
        return max(states, key=lambda s: s.story_time)
    point = anchor_key(p, anchor)
    states = [
        s
        for s in select_entities(p.states, parent_id=asset_id)
        if (not s.starts_at or anchor_key(p, s.starts_at) <= point)
        and (not s.ends_at or point <= anchor_key(p, s.ends_at))
        and anchor.event_id not in s.excluded_event_ids
    ]
    if len(states) != 1 or (expected_state_id and states[0].id != expected_state_id):
        reason = (
            "state_scope_overlap"
            if len(states) > 1
            else "state_missing"
            if not states
            else "state_mismatch"
        )
        asset = p.assets[asset_id]
        expected = p.states.get(str(expected_state_id))
        names = "、".join(f"{s.label} ({s.id})" for s in states) or "无适用状态"
        raise StudioPrerequisiteError(
            f"资产「{asset.name}」在剧情事件「{p.story_events[anchor.event_id].title}」"
            f"{anchor.position} 的{boundary}状态冲突：计划要求"
            f"「{expected.label if expected else expected_state_id or '唯一适用状态'}」，"
            f"匹配到 {names}",
            details={
                "reason": reason,
                "asset_id": asset.id,
                "asset_name": asset.name,
                "clip_id": clip_id,
                "boundary": boundary,
                "story_anchor": anchor.model_dump(mode="json"),
                "expected_state_id": expected_state_id,
                "expected_state_label": expected.label if expected else None,
                "matched_states": [
                    {
                        "state_id": s.id,
                        "label": s.label,
                        "starts_at": s.starts_at.model_dump(mode="json") if s.starts_at else None,
                        "ends_at": s.ends_at.model_dump(mode="json") if s.ends_at else None,
                    }
                    for s in sorted(states, key=lambda item: item.id)
                ],
                "owner_role": "executive",
                "blocks": "affected_clip_generation",
                "planning_allowed": True,
            },
            recovery="总导演核对事件前后位置，使用 correct_state_scope 明确各状态的生效范围；"
            "若计划状态本身不符，明确修改当前 Clip 的状态计划。其他片场和规划继续。",
        )
    require(states, "资产在此剧情事件位置尚无明确状态，请总导演补齐事件定位")
    return max(
        states,
        key=lambda s: anchor_key(p, s.starts_at) if s.starts_at else (float("-inf"), -1),
    )


def validate_clip_plan(p: StudioProject, clip: Clip, *, allow_unknown: bool = False) -> None:
    validate_scope(
        p,
        MemoryScope(
            clip_ids=[clip.id] if clip.id in p.clips else [],
            event_ids=clip.story_event_ids,
            starts_at=clip.starts_at,
            ends_at=clip.ends_at,
        ),
    )
    require(
        allow_unknown or clip.state_readiness == "ready",
        "Clip 状态仍待核对，请总导演记录有依据的制作决定后继续",
    )
    for anchor in (clip.starts_at, clip.ends_at):
        require(
            anchor is None or anchor.event_id in clip.story_event_ids,
            "Clip 起止锚点必须属于计划内剧情事件",
        )
    for plan in clip.asset_plan:
        require(
            plan.asset_id in p.assets and p.assets[plan.asset_id].purpose == "story",
            "剧情状态计划仅能使用剧情资产；动作白模和目标画面请作为 guidance 参考",
        )
        for state_id in (plan.start_state_id, plan.end_state_id):
            require(
                state_id is None
                or state_id in p.states
                and p.states[state_id].asset_id == plan.asset_id,
                "Clip 状态计划引用了错误资产",
            )
        stamp = clip.story_time if clip.story_time is not None else p.sets[clip.set_id].story_time
        try:
            require(
                state_at(
                    p,
                    plan.asset_id,
                    stamp,
                    clip.starts_at,
                    expected_state_id=plan.start_state_id,
                    clip_id=clip.id,
                ).id
                == plan.start_state_id,
                "Clip 起始状态必须符合剧情事件或故事时间，倒叙不能引用未来状态",
            )
            check_unchanged_end = clip.ends_at is not None or clip.story_end_time is not None
            if plan.end_state_id and (
                plan.end_state_id != plan.start_state_id or check_unchanged_end
            ):
                require(
                    clip.ends_at or clip.story_end_time is not None,
                    "状态变化需指定结束剧情事件位置",
                )
                require(
                    state_at(
                        p,
                        plan.asset_id,
                        clip.story_end_time or stamp,
                        clip.ends_at,
                        expected_state_id=plan.end_state_id,
                        clip_id=clip.id,
                        boundary="end",
                    ).id
                    == plan.end_state_id,
                    "Clip 目标状态与结束剧情事件不符",
                )
        except StudioPrerequisiteError:
            if not (allow_unknown):
                raise
        require(
            all(v in p.assets[plan.asset_id].required_views for v in plan.required_views),
            "Clip 所需视图不属于此资产",
        )
    require(
        len({x.asset_id for x in clip.asset_plan}) == len(clip.asset_plan),
        "Clip 状态计划不能重复资产",
    )


def seed_screenplay_memory(p: StudioProject, actor_id: str) -> None:
    """Mirror only immutable confirmed facts, with stable IDs and original text."""
    for fact in p.screenplay.facts:
        identifier = digest([p.id, "screenplay", fact.id])[:32]
        if memory_record(p, identifier):
            continue
        p.global_memory.story.append(
            MemoryEntry(
                id=identifier,
                text=fact.text,
                kind="constraint",
                section="screenplay",
                basis="fact",
                actor_id=actor_id,
                sources=[MemorySource(kind="screenplay_fact", id=fact.id)],
                reason="Confirmed screenplay",
                record_key=f"screenplay:{fact.id}",
            )
        )
        p.global_memory.revision += 1


def record_selected_outcome(
    p: StudioProject,
    candidate: Candidate,
    actor_id: str,
    *,
    update_memberships: bool = True,
    refresh: bool = False,
) -> None:
    """Selected actual state is concise evidence; the full review remains on the candidate."""
    clip = p.clips[candidate.clip_id]
    production_set = p.sets[clip.set_id]
    from .continuity import ensure_continuation_tail

    ensure_continuation_tail(p, candidate)
    identifier = digest([p.id, "selected-actual", candidate.id])[:32]
    existing = memory_record(p, identifier)
    if existing:
        if not refresh and existing[1].status == "effective" and selected_source(p, existing[1]):
            return
        identifier = digest(
            [p.id, "selected-actual", candidate.id, production_set.memory.revision]
        )[:32]
    parts = []
    from .adjudication import actual_end_state

    state = actual_end_state(candidate)
    if state:
        parts.extend(
            [
                f"动作：{state.action}",
                f"空间：{state.space}",
                f"机位：{state.camera}",
                f"人物：{state.identity}",
            ]
        )
    parts.extend(
        f"{p.assets[asset_id].name}：{value}"
        for asset_id, value in candidate.observed_asset_states.items()
        if asset_id in p.assets
    )
    if not parts:
        parts.append("已选视频缺少结构化实际末态；接续前需核对原始画面。")
    entry = MemoryEntry(
        id=identifier,
        text="\n".join(parts),
        kind="observation",
        section="clip_actual",
        basis="observed",
        source_candidate_id=candidate.id,
        actor_id=actor_id,
        scope=MemoryScope(
            set_ids=[production_set.id], clip_ids=[clip.id], event_ids=clip.story_event_ids
        ),
        sources=[MemorySource(kind="candidate", id=candidate.id)],
        evidence_media_ids=candidate.confirmed_end_state.evidence_media_ids
        if candidate.confirmed_end_state
        else [state.frame_media_id]
        if state
        else [],
        reason="记录导演确认的已选实际末态；原模型观察、选用类型和验收历史保留在候选详情"
        if candidate.confirmed_end_state
        else "历史末态来自原检查记录，尚需导演明确核对；选用类型及原验收保留在候选详情",
    )
    old_entries = (
        production_set.memory.story.entries.select(parent_id=production_set.id, status="effective")
        if isinstance(production_set.memory.story, MemoryEntries)
        else production_set.memory.story
    )
    for old in old_entries:
        if old.section == "clip_actual" and old.scope.clip_ids == [clip.id]:
            if old.status != "effective":
                continue
            old.status, old.superseded_by = "superseded", entry.id
            entry.supersedes_ids.append(old.id)
            entry.version = max(entry.version, old.version + 1)
    production_set.memory.story.append(entry)
    if update_memberships:
        for binding in candidate.bindings:
            if binding.state_id not in production_set.memory.asset_state_ids:
                production_set.memory.asset_state_ids.append(binding.state_id)
    production_set.memory.revision += 1


def plan_events(p: StudioProject, actor: Actor, data: dict[str, Any]) -> dict[str, Any]:
    require(
        actor.role == "executive" and p.screenplay.confirmed_at, "总导演在剧本确认后规划剧情事件"
    )
    ids = []
    for raw in data.get("events", []):
        event = StoryEvent.model_validate(raw)
        require(
            all(i in {f.id for f in p.screenplay.facts} for i in event.source_fact_ids),
            "事件引用的剧本事实不存在",
        )
        old = p.story_events.get(event.id)
        if old:
            require(str(data.get("reason", "")).strip(), "修改事件定位需要说明原因")
            event.history = [
                *old.history,
                {**old.model_dump(exclude={"history"}, mode="json"), "reason": data["reason"]},
            ]
            event.revision = old.revision + 1
            event.created_at = old.created_at
        require(
            not any(e.id != event.id and e.order == event.order for e in p.story_events.values()),
            "剧情事件顺序不能重复，可用小数插入事件",
        )
        p.story_events[event.id] = event
        ids.append(event.id)
    require(ids, "需要至少一个明确剧情事件")
    return {"event_ids": ids, "summary": f"已规划 {len(ids)} 个剧情事件"}
