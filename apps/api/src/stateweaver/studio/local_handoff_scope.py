"""Allow one exact independent auxiliary addition using its full Clip context.

This is not a global-context acknowledgement. Whole-table changes retain the
existing global requirement. The caller retains authority, CAS, graph validation,
confirmation preservation and the normal transactional command path.
"""

from __future__ import annotations

from typing import Any

from .contracts import Actor, AssetHandoffPlan, Clip, ProductionSet, StudioProject
from .materials import auxiliary_states
from .repository import select_entities
from .task_context import require_context


def isolated_addition(
    project: StudioProject,
    production_set: ProductionSet,
    plans: list[AssetHandoffPlan],
    data: dict[str, Any],
    clip: Clip,
) -> bool:
    """Prove this replacement list preserves everything outside one new auxiliary."""
    if (
        clip.archived_at
        or clip.set_id != production_set.id
        or clip.id not in production_set.clip_ids
        or data.get("expected_dependency_revision") != production_set.dependency_revision
        or data.get("depends_on") != production_set.depends_on
        or len(plans) != len(production_set.asset_handoffs) + 1
        or len({plan.id for plan in plans}) != len(plans)
    ):
        return False
    for before, after in zip(production_set.asset_handoffs, plans[:-1], strict=True):
        if before.model_dump(exclude={"confirmation"}, mode="json") != after.model_dump(
            mode="json"
        ):
            return False
    # The typed command cannot carry or rewrite confirmation. Even a direct
    # service caller supplying an extra field must not gain the narrow route.
    if any("confirmation" in item for item in data.get("asset_handoffs", [])):
        return False
    addition = plans[-1]
    if (
        addition.mode != "independent"
        or addition.source_set_id is not None
        or addition.source_event_id is not None
        or addition.required_attributes
        or len(addition.state_ids) != 1
        or len(addition.target_event_ids) != 1
        or any(h.asset_id == addition.asset_id for h in production_set.asset_handoffs)
    ):
        return False
    event_id = addition.target_event_ids[0]
    if (
        clip.story_event_ids != [event_id]
        or event_id not in production_set.story_event_ids
        or event_id not in project.story_events
    ):
        return False
    own_clips = select_entities(project.clips, parent_id=production_set.id)
    if {c.id for c in own_clips if not c.archived_at and event_id in c.story_event_ids} != {
        clip.id
    }:
        return False
    asset = project.assets.get(addition.asset_id)
    state = project.states.get(addition.state_ids[0])
    if (
        asset is None
        or state is None
        or asset.purpose not in {"auxiliary", "action"}
        or asset.id not in production_set.asset_ids
        or state.asset_id != asset.id
        or state.id not in asset.state_ids
    ):
        return False
    scope = asset.published_scope
    if (
        scope is None
        or scope.set_ids != [production_set.id]
        or scope.clip_ids != [clip.id]
        or scope.event_ids != [event_id]
        or set(scope.asset_ids) - {asset.id}
        or scope.starts_at is not None
        or scope.ends_at is not None
    ):
        return False
    preparation = state.preparation
    purpose = state.reference_purpose
    if (
        preparation is None
        or preparation.mode != "independent"
        or preparation.source_set_id is not None
        or preparation.source_event_id is not None
        or purpose is None
        or purpose.set_ids != [production_set.id]
        or purpose.clip_ids != [clip.id]
        or state.id not in auxiliary_states(project, clip)
    ):
        return False
    return not any(
        c.id != clip.id and any(plan.asset_id == asset.id for plan in c.asset_plan)
        for c in own_clips
        if not c.archived_at
    )


def require_configuration_context(
    project: StudioProject,
    actor: Actor,
    data: dict[str, Any],
    production_set: ProductionSet,
    plans: list[AssetHandoffPlan],
) -> None:
    target = "global"
    token = str(data.get("context_token") or "")
    identifier, _, fingerprint = token.partition(":")
    receipt = project.context_reads.get(identifier)
    if (
        receipt is not None
        and receipt.actor_id == actor.id
        and receipt.phase == "planning"
        and receipt.read_kind == "mandatory"
        and receipt.complete
        and receipt.protocol_version == 2
        and receipt.fingerprint == fingerprint
        and (clip := project.clips.get(receipt.target_id)) is not None
        and isolated_addition(project, production_set, plans, data, clip)
    ):
        target = clip.id
    # The original validator enforces actual complete pages, exact actor/target,
    # current epoch/core/dependencies and semantic content. No token is promoted.
    require_context(project, actor, data, "planning", target)
