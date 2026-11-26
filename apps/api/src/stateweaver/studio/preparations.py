"""Editable one-Clip lookahead, kept separate from executable reference recipes."""

from __future__ import annotations

from typing import Any

from .contracts import (
    Actor,
    Clip,
    ClipPreparation,
    StudioProject,
)
from .memory import digest, require
from .reference_inputs import video_reference
from .repository import MODELS, matches, select_entities
from .store import StudioError
from .task_context import context_fingerprint, require_context


def component_snapshots(
    project: StudioProject, clip: Clip, preparation: ClipPreparation
) -> tuple[dict[str, Any], dict[str, Any]]:
    from .task_context import context_records

    stable: dict[str, Any] = {}
    continuity: dict[str, Any] = {}
    previous_frame = None
    if clip.order:
        from .adjudication import actual_end_state

        previous = project.clips[project.sets[clip.set_id].clip_ids[clip.order - 1]]
        candidate = project.candidates.get(previous.accepted_candidate_id or "")
        end = actual_end_state(candidate) if candidate else None
        previous_frame = end.frame_media_id if end else None
    for record in context_records(
        project, clip.id, "references", preparation.actor_id, include_preparation=False
    ):
        kind = record["type"]

        if kind == "task" or (kind == "proposal"):
            continue
        value = record.get("value") or {}
        identifier = (
            (record.get("clip_id") if kind == "previous_selected_actual" else value.get("id"))
            or record.get("media_id")
            or "global"
        )
        key = f"{kind}:{record.get('scope', '')}:{identifier}"
        if kind == "previous_selected_actual" or (
            kind == "image_input_exclusion" and record.get("media_id") == previous_frame
        ):
            continuity[key] = record
        else:
            stable[key] = record
    from .production_efficiency import proposal_definitions

    stable["proposal_definitions"] = {
        "type": "proposal_definitions",
        "value": {"proposals": proposal_definitions(project, clip.id)},
    }
    for binding in preparation.bindings:
        ref = project.references.get(binding.revision_id)
        state = project.states.get(binding.state_id)
        stable[f"bound_reference:{binding.revision_id}"] = {
            "type": "bound_reference",
            "value": {
                "id": binding.revision_id,
                "asset_id": binding.asset_id,
                "state_id": binding.state_id,
                "selected_revision": state.selected_revisions.get(ref.view)
                if state and ref
                else None,
                "media_id": ref.media_id if ref else None,
            },
        }
    for identifier in sorted(
        {
            i
            for i in [
                *(item.prepared_reference_id for item in preparation.bindings),
                *(item.preparation_job_id for item in preparation.audio_references),
            ]
            if i
        }
    ):
        job = project.jobs.get(identifier)
        stable[f"prepared_source:{identifier}"] = {
            "type": "prepared_source",
            "value": {
                "id": identifier,
                "status": job.status if job else "missing",
                "request": job.request if job else None,
                "result": {key: value for key, value in job.result.items() if key != "timings"}
                if job
                else None,
            },
        }
    for item in preparation.audio_references:
        if item.voice_reference_id and item.asset_id in project.assets:
            asset = project.assets[str(item.asset_id)]
            voice = next(
                (v for v in asset.voice_references if v.id == item.voice_reference_id), None
            )
            stable[f"prepared_voice:{asset.id}:{item.voice_reference_id}"] = {
                "type": "prepared_voice",
                "value": {
                    "id": asset.id,
                    "label": asset.name,
                    "selected_voice_id": asset.selected_voice_id,
                    "voice_reference": voice.model_dump(mode="json") if voice else None,
                },
            }
    if clip.order:
        previous = project.clips[project.sets[clip.set_id].clip_ids[clip.order - 1]]
        candidate = project.candidates.get(previous.accepted_candidate_id or "")
        tail = (
            project.jobs.get(candidate.continuation_reference_job_id or "") if candidate else None
        )
        continuity["tail"] = {
            "type": "tail",
            "value": {
                "id": previous.id,
                "candidate_id": previous.accepted_candidate_id,
                "job_id": tail.id if tail else None,
                "status": tail.status if tail else "waiting",
                "media_id": tail.result.get("media_id") if tail else None,
            },
        }
    return stable, continuity


def dependency_changes(
    before: dict[str, Any], after: dict[str, Any], component: str
) -> list[dict[str, Any]]:
    def changed_paths(old: Any, new: Any, prefix: str = "") -> list[str]:
        if old == new:
            return []
        if not isinstance(old, dict) or not isinstance(new, dict):
            return [prefix.rstrip(".") or "value"]
        fields = []
        for field in sorted(old.keys() | new.keys()):
            if old.get(field) == new.get(field):
                continue
            path = prefix + field
            if isinstance(old.get(field), dict) and isinstance(new.get(field), dict):
                fields.extend(changed_paths(old[field], new[field], path + "."))
            else:
                fields.append(path)
        return fields

    changes = []
    for key in sorted(before.keys() | after.keys()):
        old, new = before.get(key), after.get(key)
        if old == new:
            continue
        old_value, new_value = (old or {}).get("value", {}), (new or {}).get("value", {})
        fields = changed_paths(old_value, new_value)
        old_metadata = old_value if isinstance(old_value, dict) else {}
        new_metadata = new_value if isinstance(new_value, dict) else {}
        record = new_metadata or old_metadata
        changes.append(
            {
                "component": component,
                "dependency": key,
                "entity_id": record.get("id"),
                "asset_id": record.get("asset_id"),
                "state_id": record.get("state_id"),
                "label": record.get("name") or record.get("label") or record.get("title"),
                "changed_fields": sorted(fields) or ["membership"],
                "before_revision": old_metadata.get(
                    "selected_revisions", old_metadata.get("selected_revision")
                ),
                "after_revision": new_metadata.get(
                    "selected_revisions", new_metadata.get("selected_revision")
                ),
                "previous_candidate_id": old_metadata.get("candidate_id", old_metadata.get("id"))
                if component == "continuity"
                else None,
                "current_candidate_id": new_metadata.get("candidate_id", new_metadata.get("id"))
                if component == "continuity"
                else None,
                "affected_fields": [
                    "opening_description",
                    "previous_end_frame",
                    "previous_tail",
                    "previous_end_state",
                ]
                if component == "continuity"
                else ["prompt_draft", "bindings", "audio_references", "action_notes"],
                "next_action": "按前段新选用版本重新绑定接续部分，稳定内容可保留"
                if component == "continuity"
                else "仅核对并更新受影响的故事/素材内容后保存预准备",
            }
        )
    return changes


def frontier(project: StudioProject, set_id: str) -> list[Clip]:
    return [
        project.clips[identifier]
        for identifier in project.sets[set_id].clip_ids
        if not project.clips[identifier].archived_at
        and not project.clips[identifier].accepted_candidate_id
    ][:2]


def needs_lookahead(project: StudioProject, actor: Actor) -> bool:
    if not project.config.prepare_next_clip or not actor.set_id:
        return False
    upcoming = frontier(project, actor.set_id)
    if len(upcoming) < 2 or upcoming[0].status not in {"generating", "review"}:
        return False
    clip = upcoming[1]
    return clip.preparation is None or (
        clip.preparation.enabled and clip.preparation.status == "stale"
    )


def promote_preparations(project: StudioProject) -> None:
    """Materialize only untouched, valid lookahead as an unsubmitted reference draft."""
    from .continuation_controls import needs_tail
    from .contracts import ReferencePlan, now
    from .reference_plans import resolve_inputs
    from .reference_plans import source_fingerprint as plan_fingerprint

    if not project.config.prepare_next_clip:
        return
    for clip in select_entities(project.clips, statuses=["planned", "repair", "escalated"]):
        preparation = clip.preparation
        if (
            clip.archived_at
            or clip.accepted_candidate_id
            or clip.reference_plan_id
            or not preparation
            or not preparation.enabled
            or preparation.user_disabled
            or preparation.status in {"consumed", "disabled"}
            or preparation.stable_status != "ready"
            or preparation.pending_requirements
            or not clip.order
        ):
            continue
        previous = project.clips[project.sets[clip.set_id].clip_ids[clip.order - 1]]
        if not previous.accepted_candidate_id:
            continue
        candidate = project.candidates.get(previous.accepted_candidate_id)
        if not candidate:
            preparation.invalidation_reasons = [
                f"前段选用候选 {previous.accepted_candidate_id} 不存在；请先核对来源记录"
            ]
            continue
        tail = project.jobs.get(candidate.continuation_reference_job_id or "")
        try:
            tail_required = needs_tail(project, clip)
        except StudioError:
            continue
        if (
            not candidate.confirmed_end_state
            or tail_required
            and (not tail or tail.status != "complete")
        ):
            continue
        source_ids = [
            candidate.media_id,
            candidate.confirmed_end_state.end_state.frame_media_id,
            *([tail.result.get("media_id")] if tail_required and tail else []),
        ]
        if any(not identifier or identifier not in project.media for identifier in source_ids):
            preparation.invalidation_reasons = [
                f"候选 {candidate.id} 的真实接续素材登记不完整："
                + "、".join(str(identifier or "缺少素材 ID") for identifier in source_ids)
                + "；请先核对原视频、末帧和尾段，不重新生成视频"
            ]
            continue
        actor = project.actors[project.sets[clip.set_id].actor_id]
        if actor.id != preparation.actor_id or actor.paused:
            continue
        plan = ReferencePlan(
            clip_id=clip.id,
            actor_id=actor.id,
            bindings=[item.model_copy(deep=True) for item in preparation.bindings],
            audio_references=[item.model_copy(deep=True) for item in preparation.audio_references],
            prompt=preparation.prompt_draft,
            preparation_id=preparation.id,
            preparation_revision=preparation.revision,
            automatic_draft=True,
            reason="有效预准备自动转为待核对草稿；尚未经导演定稿或提交",
        )
        try:
            require_promotable(project, clip, preparation.id)
            resolve_inputs(project, actor, clip, plan)
        except StudioError as error:
            preparation.invalidation_reasons = [str(error)]
            continue
        plan.context_fingerprint = context_fingerprint(project, clip.id, "references", actor.id)
        plan.dependency_fingerprint = plan_fingerprint(plan)
        from .production_efficiency import creative_fingerprint

        plan.creative_fingerprint = creative_fingerprint(project, clip.id)
        plan.changes = [{"field": "continuity", "source_candidate_id": candidate.id, "at": now()}]
        project.reference_plans[plan.id] = plan
        clip.reference_plan_id = plan.id
        actor.request_wake()


def source_fingerprint(project: StudioProject, preparation: ClipPreparation) -> str:
    jobs = {
        identifier
        for identifier in [
            *(item.prepared_reference_id for item in preparation.bindings),
            *(item.preparation_job_id for item in preparation.audio_references),
        ]
        if identifier
    }
    voices = []
    for item in preparation.audio_references:
        if item.voice_reference_id and item.asset_id in project.assets:
            asset = project.assets[str(item.asset_id)]
            voices.append(
                [
                    asset.id,
                    asset.selected_voice_id,
                    [
                        v.model_dump(mode="json")
                        for v in asset.voice_references
                        if v.id == item.voice_reference_id
                    ],
                ]
            )
    return digest(
        [
            [
                [identifier, project.jobs[identifier].status, project.jobs[identifier].result]
                for identifier in sorted(jobs)
                if identifier in project.jobs
            ],
            voices,
        ]
    )


def save_preparation(project: StudioProject, actor: Actor, data: dict[str, Any]) -> dict[str, Any]:
    from .reference_plans import owned_clip

    clip = owned_clip(project, actor, str(data.get("clip_id")))
    upcoming = frontier(project, str(actor.set_id))
    require(
        clip.id in {c.id for c in upcoming},
        "只提前准备紧邻的下一条 Clip",
    )
    require(
        project.config.prepare_next_clip or clip.id == upcoming[0].id,
        "用户已关闭下一 Clip 预准备；正式轮到该 Clip 后再准备",
    )
    require(
        clip.status in {"planned", "repair", "escalated", "blocked"},
        "已提交的生成输入固定，不能修改其预准备",
    )
    receipt = require_context(project, actor, data, "references", clip.id)
    old = clip.preparation
    require(not old or not old.user_disabled, "用户已禁用此 Clip 预准备，需用户重新启用")
    require(
        data.get("expected_preparation_id") == (old.id if old else None),
        "预准备版本已变化，请读取当前草稿后修改",
    )
    reason = str(data.get("reason") or "").strip()
    require(reason, "修改预准备需要说明原因")
    fields = (
        {
            name: getattr(old, name)
            for name in (
                "enabled",
                "prompt_draft",
                "action_notes",
                "bindings",
                "audio_references",
                "pending_requirements",
            )
        }
        if old
        else {}
    )
    fields.update(
        {
            name: value
            for name, value in data.items()
            if name
            in {
                "enabled",
                "prompt_draft",
                "action_notes",
                "bindings",
                "audio_references",
                "pending_requirements",
            }
            and value is not None
        }
    )
    preparation = ClipPreparation(
        **fields,
        actor_id=actor.id,
        parent_id=old.id if old else None,
        revision=old.revision + 1 if old else 1,
        reason=reason,
        context_fingerprint=receipt.fingerprint,
        context_dependencies=receipt.dependencies,
    )
    planned = {
        state_id
        for item in clip.asset_plan
        for state_id in (item.start_state_id, item.end_state_id)
        if state_id
    }
    from .materials import auxiliary_states

    auxiliary = auxiliary_states(project, clip)
    for item in preparation.bindings:
        require(
            item.state_id in planned
            or item.state_role == "guidance"
            and item.state_id in auxiliary,
            "预准备图片必须属于本 Clip 计划状态或已发布辅助资产",
        )
        _, source = video_reference(project, actor, item, clip_id=clip.id)
        require(
            project.states[item.state_id].selected_revisions.get(source["view"]) == item.revision_id
            or bool(source.get("region_approval_id")),
            "预准备需使用该状态当前选用的图片；缺失素材请记录为待补条件",
        )
    for audio in preparation.audio_references:
        require(audio.role in {"voice", "ambience", "sound"}, "预准备的音频需要明确声音用途")
        if audio.preparation_job_id:
            job = project.jobs.get(audio.preparation_job_id)
            require(
                job
                and job.status == "complete"
                and job.actor_id
                in {actor.id, next(a.id for a in project.actors.values() if a.role == "executive")},
                "音频素材尚未准备好或无权访问；请记录为待补条件",
            )
        else:
            asset = project.assets.get(str(audio.asset_id))
            require(
                asset and asset.selected_voice_id == audio.voice_reference_id,
                "预准备音色必须为资产当前选用版本",
            )
    preparation.source_fingerprint = source_fingerprint(project, preparation)
    preparation.stable_snapshot, preparation.continuity_snapshot = component_snapshots(
        project, clip, preparation
    )
    preparation.stable_status = "ready"
    preparation.continuity_status = (
        "waiting"
        if clip.order
        and not preparation.continuity_snapshot.get("tail", {}).get("value", {}).get("candidate_id")
        else "ready"
    )
    preparation.status = "draft" if preparation.enabled else "disabled"
    if old:
        clip.preparation_history.append(old.model_copy(deep=True))
    clip.preparation = preparation
    return {
        "clip_id": clip.id,
        "preparation_id": preparation.id,
        "revision": preparation.revision,
        "status": preparation.status,
        "summary": "预准备已保存；正式清单仍需绑定前段选用后的真实末帧、尾 3 秒和末态",
    }


def require_promotable(project: StudioProject, clip: Clip, identifier: str) -> ClipPreparation:
    preparation = clip.preparation
    require(
        (
            preparation
            and preparation.id == identifier
            and preparation.enabled
            and (
                preparation.status == "draft"
                or (preparation.status == "stale" and preparation.stable_status == "ready")
            )
        ),
        "预准备已禁用、过期或版本不一致，请先更新草稿",
    )
    assert preparation is not None
    stable, _ = component_snapshots(project, clip, preparation)
    from .production_efficiency import stable_snapshot

    changes = dependency_changes(
        stable_snapshot(project, preparation.stable_snapshot), stable, "stable"
    )
    require(
        not changes,
        "稳定部分发生变化，请核对具体依赖后更新预准备："
        + "; ".join(x["dependency"] for x in changes),
    )
    # Actual continuation is freshly bound and inspected by reference_plans;
    # a changed predecessor does not discard unrelated creative preparation.
    return preparation


def invalidate_preparations(project: StudioProject) -> None:
    from .context_cache import context_changes

    core_changed, changes = context_changes(project)
    if not changes and not core_changed:
        return
    for clip in select_entities(
        project.clips, statuses=["planned", "repair", "escalated", "blocked"]
    ):
        preparation = clip.preparation
        if clip.archived_at or not preparation or preparation.status not in ({"draft", "stale"}):
            continue
        jobs = {i.prepared_reference_id for i in preparation.bindings} | {
            i.preparation_job_id for i in preparation.audio_references
        }
        if clip.order:
            previous = project.clips[project.sets[clip.set_id].clip_ids[clip.order - 1]]
            candidate = project.candidates.get(previous.accepted_candidate_id or "")
            if candidate:
                jobs.add(candidate.continuation_reference_job_id)
        affected = (
            core_changed
            or not preparation.context_dependencies
            or any(
                kind == "jobs"
                and identifier in jobs
                or any(
                    kind == dependency["kind"]
                    and any(
                        source is not None and matches(source, dependency["filters"], owner)
                        for source in (
                            value,
                            MODELS[kind].model_validate(before) if before else None,
                        )
                    )
                    for dependency in preparation.context_dependencies
                )
                for kind, identifier, value, before, owner in changes
            )
        )
        if not affected:
            continue
        if preparation.stable_snapshot:
            stable, continuity = component_snapshots(project, clip, preparation)
            from .production_efficiency import stable_snapshot

            stable_changes = dependency_changes(
                stable_snapshot(project, preparation.stable_snapshot), stable, "stable"
            )
            continuity_changes = dependency_changes(
                preparation.continuity_snapshot, continuity, "continuity"
            )
            preparation.dependency_changes = stable_changes + continuity_changes
            if not preparation.dependency_changes:
                continue
            preparation.stable_status = "stale" if stable_changes else "ready"
            preparation.continuity_status = (
                "stale" if continuity_changes else preparation.continuity_status
            )
            preparation.status = "stale"
            preparation.invalidation_reasons = [
                f"{change['component']} · "
                f"{change['label'] or change['entity_id'] or change['dependency']}："
                f"{', '.join(change['changed_fields'])} 已改变；{change['next_action']}"
                for change in preparation.dependency_changes
            ]
            director = project.actors[preparation.actor_id]
            director.request_wake()
            if not director.paused and director.status in {"idle", "waiting"}:
                director.status = "ready"
            continue
        preparation.status = "stale"
        preparation.invalidation_reasons = [
            "相关剧情、Clip 计划、选用图片/音色或前段实际末态已改变；核对并保存新版后再定稿"
        ]
        director = project.actors[preparation.actor_id]
        director.request_wake()
        if not director.paused and director.status in {"idle", "waiting"}:
            director.status = "ready"
