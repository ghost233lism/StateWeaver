"""Reference-purpose validation and non-mutating historical tree projections."""

from __future__ import annotations

from typing import Any

from sqlalchemy import and_, case, func, or_, select, true
from sqlalchemy.orm import Session

from stateweaver.persistence.models import StudioBindingRow, StudioDisplayRow, StudioEntityRow

from .contracts import Actor, AssetState, AssetStateTree, StateReferenceUse, StudioProject
from .store import StudioPrerequisiteError


def validate_reference_purpose(p: StudioProject, actor: Actor, state: AssetState) -> None:
    purpose = state.reference_purpose

    def reject(message: str, **details: Any) -> None:
        raise StudioPrerequisiteError(
            f"资产「{p.assets[state.asset_id].name}」状态「{state.label}」：{message}",
            details={
                "kind": "state_reference_purpose",
                "asset_id": state.asset_id,
                "state_id": state.id,
                "owner_id": actor.id,
                **details,
            },
            recovery=(
                "先建立带资产和剧情目标的片场，再用 reference_purpose 关联所需片场或 Clip、"
                "required_materials 与 prepare_when；无需等待分片或制图。"
                "纯剧情过程写入剧情记忆与提示词，不单独建立资产状态。"
            ),
        )

    if purpose is None:
        reject("新建状态必须有明确参考用途 reference_purpose")
        return
    set_ids = set(purpose.set_ids)
    for identifier in purpose.clip_ids:
        clip = p.clips.get(identifier)
        if clip is None or clip.archived_at:
            reject("参考用途关联的 Clip 不存在或已归档", clip_id=identifier)
            return
        production_set = p.sets.get(clip.set_id)
        if production_set is None or identifier not in production_set.clip_ids:
            reject("参考用途关联的 Clip 不在当前片场计划中", clip_id=identifier)
            return
        set_ids.add(clip.set_id)
    for identifier in sorted(set_ids):
        production_set = p.sets.get(identifier)
        if production_set is None:
            reject("参考用途关联的片场不存在", set_id=identifier)
        elif state.asset_id not in production_set.asset_ids:
            reject("参考用途的片场尚未发布此资产", set_id=identifier)
    if purpose.prepare_when == "after_source_selected" and (
        state.preparation is None or state.preparation.mode != "observed"
    ):
        reject("等待实拍制图需同时登记 observed preparation 及来源片场、事件和继承属性")


def asset_state_tree(
    session: Session,
    project_id: str,
    asset_id: str,
    states: list[StudioEntityRow],
    *,
    completed: bool,
) -> AssetStateTree:
    """Read exact use, not image presence alone. No aggregate/history hydration.

    An end_state_id only describes the intended result. It is not proof that this
    state supplied (or will supply) a reference. Archived, unsubmitted plans and
    global memory membership likewise cannot justify a reference node.
    """
    tree = AssetStateTree(asset_id=asset_id)
    if not states:
        return tree
    ids = {state.id for state in states}
    image_states = set(
        session.scalars(
            select(StudioEntityRow.parent_id)
            .where(
                StudioEntityRow.project_id == project_id,
                StudioEntityRow.kind == "references",
                StudioEntityRow.parent_id.in_(ids),
            )
            .distinct()
        )
    )
    submitted = set(
        session.scalars(
            select(StudioBindingRow.state_id)
            .where(
                StudioBindingRow.project_id == project_id,
                StudioBindingRow.asset_id == asset_id,
            )
            .distinct()
        )
    )
    video_media: dict[str, set[str]] = {identifier: set() for identifier in ids}
    without_images = ids - image_states - submitted
    if without_images:
        videos = (
            func.json_each(StudioEntityRow.payload, "$.request.video_inputs")
            .table_valued("value")
            .alias("tree_videos")
        )
        coverage = (
            func.json_each(videos.c.value, "$.visual_state_ids")
            .table_valued("value")
            .alias("tree_video_states")
        )
        # Return matching IDs only. Original job recipes and images stay in their
        # indexed history; do not transfer all candidate/request bodies to Python.
        for identifier, media_id in session.execute(
            select(coverage.c.value, func.json_extract(videos.c.value, "$.media_id"))
            .select_from(StudioEntityRow)
            .join(videos, true())
            .join(coverage, true())
            .where(
                StudioEntityRow.project_id == project_id,
                StudioEntityRow.kind == "jobs",
                or_(
                    StudioEntityRow.subtype == "video",
                    and_(
                        StudioEntityRow.subtype.is_(None),
                        StudioEntityRow.payload["kind"].as_string() == "video",
                    ),
                ),
                coverage.c.value.in_(without_images),
            )
            .distinct()
        ):
            if media_id:
                video_media[identifier].add(media_id)
    planned: dict[str, set[str]] = {identifier: set() for identifier in ids}
    assigned: dict[str, set[str]] = {identifier: set() for identifier in ids}
    requested: set[str] = set()
    active_clips: dict[str, str] = {}
    display_match = and_(
        StudioDisplayRow.project_id == StudioEntityRow.project_id,
        StudioDisplayRow.kind == StudioEntityRow.kind,
        StudioDisplayRow.id == StudioEntityRow.id,
        StudioDisplayRow.version == StudioEntityRow.version,
    )
    # Even JSON scalar extraction reads the original SQLite overflow pages.
    # Version-matched display rows contain these exact current planning fields
    # without preparation snapshots or history; stale/missing rows fall back.
    plan_payload = case(
        (StudioDisplayRow.id.is_not(None), StudioDisplayRow.payload),
        else_=StudioEntityRow.payload,
    )
    for set_id, handoffs, clip_ids in session.execute(
        select(
            StudioEntityRow.id,
            plan_payload["asset_handoffs"],
            plan_payload["clip_ids"],
        )
        .select_from(StudioEntityRow)
        .outerjoin(StudioDisplayRow, display_match)
        .where(StudioEntityRow.project_id == project_id, StudioEntityRow.kind == "sets")
    ):
        active_clips.update((identifier, set_id) for identifier in clip_ids or [])
        for handoff in handoffs or []:
            if handoff.get("asset_id") == asset_id:
                for identifier in set(handoff.get("state_ids", [])) & ids:
                    assigned[identifier].add(set_id)
    # JSON scalar projection keeps large Clip/review histories out of Python.
    for clip_id, set_id, plans in session.execute(
        select(
            StudioEntityRow.id,
            StudioEntityRow.parent_id,
            plan_payload["asset_plan"],
        )
        .select_from(StudioEntityRow)
        .outerjoin(StudioDisplayRow, display_match)
        .where(
            StudioEntityRow.project_id == project_id,
            StudioEntityRow.kind == "clips",
            StudioEntityRow.id.in_(active_clips),
            plan_payload["archived_at"].as_string().is_(None),
        )
    ):
        if active_clips[clip_id] != set_id:
            continue
        for plan in plans or []:
            identifier = plan.get("start_state_id")
            if identifier in ids and plan.get("asset_id") == asset_id:
                planned[identifier].add(clip_id)
                if set_id:
                    assigned[identifier].add(set_id)
    for set_id, requests in session.execute(
        select(StudioEntityRow.parent_id, StudioEntityRow.payload["material_requests"]).where(
            StudioEntityRow.project_id == project_id,
            StudioEntityRow.kind == "proposals",
            StudioEntityRow.status.in_(["pending", "accepted", "in_progress"]),
        )
    ):
        for request in requests or []:
            identifier = request.get("state_id")
            if identifier in ids and request.get("asset_id") in {None, asset_id}:
                requested.add(identifier)
                if set_id:
                    assigned[identifier].add(set_id)
    for state in states:
        identifier = state.id
        purpose = state.payload.get("reference_purpose")
        basis: list[str] = []
        if identifier in image_states:
            basis.append("reference_history")
        if identifier in submitted:
            basis.append("submitted_reference")
        if video_media[identifier]:
            basis.append("video_reference_request")
        if planned[identifier]:
            basis.append("current_clip_input")
        if assigned[identifier] and not planned[identifier]:
            basis.append("set_handoff_or_material_request")
        if identifier in requested:
            basis.append("pending_material_request")
        if purpose:
            basis.append("explicit_reference_purpose")
            assigned[identifier].update(purpose.get("set_ids", []))
            planned[identifier].update(purpose.get("clip_ids", []))
        kind: Any = (
            "reference"
            if identifier in image_states or identifier in submitted or video_media[identifier]
            else "pending"
            if basis
            else "process"
            if completed
            else "unverified"
        )
        tree.states[identifier] = StateReferenceUse(
            kind=kind,
            basis=basis
            or ["no_reference_use_in_completed_project" if completed else "use_unverified"],
            set_ids=sorted(assigned[identifier]),
            clip_ids=sorted(planned[identifier]),
            video_media_ids=sorted(video_media[identifier]),
        )
    return tree


def reference_tree_changes(
    p: StudioProject, kind: str, before: dict[str, Any] | None, after: dict[str, Any] | None
) -> set[str]:
    """Invalidate only trees whose reference-use inputs changed, not each poll."""
    fields = {
        "assets": ("id", "name", "description", "required_views"),
        "states": (
            "id",
            "asset_id",
            "label",
            "story_time",
            "is_base",
            "selected_revisions",
            "description",
            "evolution",
            "reference_purpose",
            "preparation",
        ),
        "references": ("id", "state_id", "status", "media_id"),
        "clips": ("asset_plan", "archived_at", "set_id"),
        "sets": ("asset_handoffs", "clip_ids", "asset_ids"),
        "candidates": ("bindings",),
        "proposals": ("status", "material_requests"),
        "jobs": ("request",),
    }.get(kind)
    if kind == "jobs":
        old_videos = (before or {}).get("request", {}).get("video_inputs", [])
        new_videos = (after or {}).get("request", {}).get("video_inputs", [])
        if old_videos == new_videos:
            return set()
        state_ids = {
            identifier
            for video in old_videos + new_videos
            for identifier in video.get("visual_state_ids", [])
        }
        return {
            f"asset-tree:{state.asset_id}"
            for identifier in state_ids
            if (state := p.states.get(identifier))
        }
    if (
        fields is None
        or before is not None
        and after is not None
        and all(before.get(key) == after.get(key) for key in fields)
    ):
        return set()
    assets: set[str] = set()
    for payload in (before, after):
        if not payload:
            continue
        if kind == "assets":
            assets.add(payload["id"])
        elif kind == "states":
            assets.add(payload["asset_id"])
        elif kind == "references":
            state = p.states.get(payload["state_id"])
            if state:
                assets.add(state.asset_id)
        else:
            key = {
                "clips": "asset_plan",
                "sets": "asset_handoffs",
                "candidates": "bindings",
                "proposals": "material_requests",
            }[kind]
            for item in payload.get(key, []):
                if item.get("asset_id"):
                    assets.add(item["asset_id"])
                elif kind == "proposals" and item.get("state_id"):
                    state = p.states.get(item["state_id"])
                    if state:
                        assets.add(state.asset_id)
            if kind == "sets":
                assets.update(payload.get("asset_ids", []))
    return {f"asset-tree:{identifier}" for identifier in assets}
