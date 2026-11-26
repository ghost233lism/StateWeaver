"""Explicit event applicability; never a visual state or selected-footage rewrite."""

from __future__ import annotations

from typing import Any

from .contracts import AssetState, Clip, ReferencePlan, StoryAnchor, StudioProject
from .memory import anchor_key, require
from .repository import select_entities


def validate_excluded_events(p: StudioProject, state: AssetState) -> None:
    """Resolve event IDs against this asset's published production scope."""
    if not state.excluded_event_ids:
        return
    asset = p.assets[state.asset_id]
    publication = asset.published_scope
    sets = select_entities(p.sets)
    for event_id in state.excluded_event_ids:
        require(event_id in p.story_events, "状态排除的剧情事件不存在")
        before = StoryAnchor(event_id=event_id, position="before")
        after = StoryAnchor(event_id=event_id, position="after")
        require(
            (not state.starts_at or anchor_key(p, state.starts_at) <= anchor_key(p, before))
            and (not state.ends_at or anchor_key(p, after) <= anchor_key(p, state.ends_at)),
            "排除的完整事件不在此状态的事件边界内",
        )
        require(
            (not publication.event_ids or event_id in publication.event_ids)
            and (
                not publication.starts_at
                or anchor_key(p, publication.starts_at) <= anchor_key(p, before)
            )
            and (
                not publication.ends_at
                or anchor_key(p, after) <= anchor_key(p, publication.ends_at)
            ),
            "排除事件不在此资产的发布范围内",
        )
        targets = {
            s.id
            for s in sets
            if state.asset_id in s.asset_ids
            and event_id in s.story_event_ids
            and (not publication.set_ids or s.id in publication.set_ids)
        }
        if publication.clip_ids:
            targets &= {
                clip.set_id
                for clip in select_entities(p.clips, ids=publication.clip_ids)
                if not clip.archived_at and clip_covers_event(p, clip, event_id)
            }
        require(targets, "排除事件不属于已发布此资产的片场范围")


def clip_covers_event(p: StudioProject, clip: Clip, event_id: str) -> bool:
    """Use declared events and their whole planned interval, not media timestamps."""
    if (
        event_id in clip.story_event_ids
        or any(execution.event_id == event_id for execution in clip.event_execution)
        or any(anchor and anchor.event_id == event_id for anchor in (clip.starts_at, clip.ends_at))
    ):
        return True
    if not (clip.starts_at and clip.ends_at):
        return False
    return anchor_key(p, clip.starts_at) <= anchor_key(
        p, StoryAnchor(event_id=event_id, position="after")
    ) and anchor_key(p, StoryAnchor(event_id=event_id, position="before")) <= anchor_key(
        p, clip.ends_at
    )


def scoped_excluded_events(p: StudioProject, state: AssetState, clip: Clip | None) -> list[str]:
    """A Clip depends only on exclusions which can affect its declared event interval.

    Complete state/global planning retains every exclusion. An unrelated event
    must not change an already usable Clip's creative hash or required-read body.
    """
    return [
        event_id
        for event_id in state.excluded_event_ids
        if clip is None or clip_covers_event(p, clip, event_id)
    ]


def _recipe_uses_state(plan: ReferencePlan, state_id: str) -> bool:
    return (
        any(binding.state_id == state_id for binding in plan.bindings)
        or any(state_id in binding.visual_state_ids for binding in plan.video_references)
        or _request_uses_state(
            {"bindings": plan.reference_inputs, "video_inputs": plan.video_inputs}, state_id
        )
    )


def _request_uses_state(request: dict[str, Any], state_id: str) -> bool:
    return (
        any(
            isinstance(binding, dict) and binding.get("state_id") == state_id
            for binding in request.get("bindings", [])
        )
        or any(
            isinstance(binding, dict) and state_id in binding.get("visual_state_ids", [])
            for binding in request.get("video_inputs", [])
        )
        or isinstance(request.get("state_snapshot"), dict)
        and request["state_snapshot"].get("id") == state_id
    )


def protect_submitted_boundaries(p: StudioProject, old: AssetState, intended: AssetState) -> None:
    """A new exclusion cannot reinterpret an original submitted event interval.

    Read only Clips covering a newly excluded event and their indexed recipes,
    candidates and direct legacy jobs. Current pointers are not a history fence.
    Ordinary boundary correction without a new exclusion keeps its old behavior.
    """
    added = set(intended.excluded_event_ids) - set(old.excluded_event_ids)
    if not added:
        return
    set_ids = [s.id for s in select_entities(p.sets) if old.asset_id in s.asset_ids]
    # The existing physical-binding index also protects a submitted historical
    # candidate even if its Set's current catalog no longer lists this asset.
    submitted = select_entities(p.candidates, binding_asset=old.asset_id)
    clip_ids = {
        candidate.clip_id
        for candidate in submitted
        if any(binding.state_id == old.id for binding in candidate.bindings)
    }
    state_recipes = select_entities(p.reference_plans, planned_state=old.id)
    clip_ids.update(
        recipe.clip_id for recipe in state_recipes if recipe.job_id or recipe.status == "submitted"
    )
    state_jobs = select_entities(p.jobs, subtype="video", request_state=old.id)
    state_job_clip_ids: set[str] = set()
    for job in state_jobs:
        target_clip = p.clips.get(job.target_id)
        candidate = p.candidates.get(job.target_id) if target_clip is None else None
        require(target_clip or candidate, "原状态的已创建视频任务缺少可核对的 Clip/候选定位")
        if target_clip is not None:
            state_job_clip_ids.add(target_clip.id)
        else:
            assert candidate is not None
            state_job_clip_ids.add(candidate.clip_id)
    clip_ids.update(state_job_clip_ids)
    clips = {
        clip.id: clip
        for clip in select_entities(p.clips, parent_ids=set_ids)
        + select_entities(p.clips, ids=sorted(clip_ids))
    }
    for clip in clips.values():
        if not any(clip_covers_event(p, clip, e) for e in added):
            continue
        planned = any(
            plan.asset_id == old.asset_id and old.id in {plan.start_state_id, plan.end_state_id}
            for plan in clip.asset_plan
        )
        candidates = select_entities(p.candidates, parent_id=clip.id)
        candidate_use = any(
            any(binding.state_id == old.id for binding in candidate.bindings)
            for candidate in candidates
        )
        recipes = select_entities(p.reference_plans, parent_id=clip.id)
        recipe_use = any(
            (recipe.job_id or recipe.status == "submitted") and _recipe_uses_state(recipe, old.id)
            for recipe in recipes
        )
        legacy_jobs = select_entities(p.jobs, parent_id=clip.id)
        legacy_job_use = (
            any(_request_uses_state(job.request, old.id) for job in legacy_jobs)
            or clip.id in state_job_clip_ids
        )
        require(
            not (
                planned
                and (
                    clip.accepted_candidate_id
                    or clip.candidate_ids
                    or candidates
                    or any(recipe.job_id or recipe.status == "submitted" for recipe in recipes)
                    or legacy_jobs
                )
                or candidate_use
                or recipe_use
                or legacy_job_use
            ),
            "不能排除已提交或已选 Clip 绑定的原状态事件；保留原片段和配方",
        )
