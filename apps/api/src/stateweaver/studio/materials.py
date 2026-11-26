"""Explicitly published auxiliary guidance, separate from observed story states."""

from __future__ import annotations

from .contracts import Clip, LogicalAsset, StudioProject
from .memory import anchor_key
from .repository import select_entities


def applies(project: StudioProject, clip: Clip, asset: LogicalAsset) -> bool:
    scope = asset.published_scope
    if scope.set_ids and clip.set_id not in scope.set_ids:
        return False
    if scope.clip_ids and clip.id not in scope.clip_ids:
        return False
    if scope.event_ids and not set(scope.event_ids).intersection(clip.story_event_ids):
        return False
    if scope.asset_ids and not set(scope.asset_ids).intersection(
        p.asset_id for p in clip.asset_plan
    ):
        return False
    if scope.starts_at and (
        not clip.starts_at
        or anchor_key(project, clip.starts_at) < anchor_key(project, scope.starts_at)
    ):
        return False
    clip_end = clip.ends_at or clip.starts_at
    if scope.ends_at and (
        not clip_end or anchor_key(project, clip_end) > anchor_key(project, scope.ends_at)
    ):
        return False
    return asset.id in project.sets[clip.set_id].asset_ids


def auxiliary_states(project: StudioProject, clip: Clip) -> set[str]:
    return {
        state.id
        for asset in select_entities(project.assets, ids=project.sets[clip.set_id].asset_ids)
        if asset.purpose != "story" and applies(project, clip, asset)
        for state in select_entities(project.states, parent_id=asset.id)
    }
