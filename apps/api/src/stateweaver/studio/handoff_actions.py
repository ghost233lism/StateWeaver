"""Executive writes for versioned Set and visual-state handoffs."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .contracts import (
    Actor,
    AssetHandoff,
    AssetHandoffPlan,
    HandoffConfirmation,
    ReferenceRevision,
    StatePreparationPlan,
    StudioProject,
    now,
)
from .image_input_recovery import inspected_evidence
from .memory import digest, require
from .set_handoffs import (
    dependency_report,
    evidence_snapshot,
    handoff_evidence,
    observation,
    reference_fingerprint,
    require_state_source,
    same_source_image,
    source_snapshot,
    validate_graph,
    validate_state_preparation,
)
from .task_context import require_context

if TYPE_CHECKING:
    from .service import StudioService


def authority(actor: Actor) -> None:
    require(actor.role == "executive", "跨片场承接和状态来源由总导演统一协调")


def wake_target(p: StudioProject, set_id: str) -> None:
    actor = p.actors[p.sets[set_id].actor_id]
    if actor.status == "complete":
        return
    actor.request_wake()
    if (
        not actor.paused
        and not actor.native_session_blocked
        and actor.status in {"idle", "waiting"}
    ):
        actor.status = "ready"


def configure_dependencies(p: StudioProject, actor: Actor, data: dict[str, Any]) -> dict[str, Any]:
    authority(actor)
    production_set = p.sets.get(str(data.get("set_id")))
    require(production_set, "片场不存在")
    assert production_set is not None
    plans = [AssetHandoffPlan.model_validate(item) for item in data["asset_handoffs"]]
    require(len({item.id for item in plans}) == len(plans), "承接 ID 不能重复")
    from .local_handoff_scope import require_configuration_context

    require_configuration_context(p, actor, data, production_set, plans)
    require(
        data.get("expected_dependency_revision") == production_set.dependency_revision,
        "片场依赖版本已变化，请读取当前依赖后修订",
    )
    require(str(data.get("reason", "")).strip(), "依赖修订需要说明剧情依据和受影响范围")
    old = [item.model_dump(mode="json") for item in production_set.asset_handoffs]
    replacements = []
    for plan in plans:
        previous = next(
            (item for item in production_set.asset_handoffs if item.id == plan.id), None
        )
        replacement = AssetHandoff(**plan.model_dump())
        if previous and previous.model_dump(exclude={"confirmation"}) == plan.model_dump():
            replacement.confirmation = previous.confirmation
        replacements.append(replacement)
    dependencies = list(dict.fromkeys(data["depends_on"]))
    if (
        old == [item.model_dump(mode="json") for item in replacements]
        and dependencies == production_set.depends_on
    ):
        return {"no_progress": True, "summary": "依赖未变化", "set_id": production_set.id}
    before = {"depends_on": production_set.depends_on, "asset_handoffs": old}
    production_set.depends_on, production_set.asset_handoffs = dependencies, replacements
    validate_graph(p)
    production_set.dependency_revision += 1
    production_set.dependency_history.append(
        {
            "revision": production_set.dependency_revision,
            "before": before,
            "after": {
                "depends_on": dependencies,
                "asset_handoffs": [item.model_dump(mode="json") for item in replacements],
            },
            "actor_id": actor.id,
            "reason": data["reason"],
            "at": now(),
        }
    )
    wake_target(p, production_set.id)
    return {
        "set_id": production_set.id,
        "summary": data["reason"],
        **dependency_report(p, production_set.id).model_dump(),
    }


def configure_state(p: StudioProject, actor: Actor, data: dict[str, Any]) -> dict[str, Any]:
    authority(actor)
    state = p.states.get(str(data.get("state_id")))
    require(state, "资产状态不存在")
    assert state is not None
    require_context(p, actor, data, "planning", state.id)
    require(
        data.get("expected_preparation_revision") == state.preparation_revision,
        "状态图准备约定已变化，请重新读取",
    )
    plan = StatePreparationPlan.model_validate(data["preparation"])
    validate_state_preparation(p, state, plan)
    if state.preparation == plan:
        return {"no_progress": True, "state_id": state.id, "summary": "状态图准备约定未变化"}
    state.preparation_history.append(
        {
            "before": state.preparation.model_dump() if state.preparation else None,
            "after": plan.model_dump(),
            "actor_id": actor.id,
            "at": now(),
            "revision": state.preparation_revision + 1,
        }
    )
    state.preparation, state.preparation_revision = plan, state.preparation_revision + 1
    return {
        "state_id": state.id,
        "asset_id": state.asset_id,
        "preparation": plan.model_dump(),
        "preparation_revision": state.preparation_revision,
        "summary": plan.reason,
    }


def confirm_handoff(
    service: StudioService, p: StudioProject, actor: Actor, data: dict[str, Any]
) -> dict[str, Any]:
    authority(actor)
    require_context(p, actor, data, "planning", "global")
    production_set = p.sets.get(str(data.get("set_id")))
    require(production_set, "片场不存在")
    assert production_set is not None
    require(
        data.get("expected_dependency_revision") == production_set.dependency_revision,
        "片场依赖版本已变化，请重新读取",
    )
    handoff = next(
        (item for item in production_set.asset_handoffs if item.id == data.get("handoff_id")), None
    )
    require(handoff and handoff.mode == "observed", "请先声明此资产的实拍承接要求")
    assert handoff is not None
    require(
        str(data.get("reason", "")).strip(), "请说明实拍中的哪些属性已匹配，以及哪些参考可继续使用"
    )
    entries = handoff_evidence(p, production_set, handoff, list(data["observation_ids"]))
    refs = list(dict.fromkeys(data.get("reference_ids", [])))
    reference_signature = reference_fingerprint(p, refs)
    require(
        all(p.references[i].state_id in handoff.state_ids for i in refs),
        "核对的参考必须属于此次承接的状态",
    )
    media = {str(p.references[i].media_id) for i in refs} | {
        i for entry in entries for i in entry.evidence_media_ids
    }
    require(
        media <= inspected_evidence(p, actor, list(media), service.store),
        "总导演或授权看图子对话需实际查看来源截图和拟保留的参考图",
    )
    evidence = evidence_snapshot(p, entries)
    previous = handoff.confirmation.model_dump() if handoff.confirmation else None
    handoff.confirmation = HandoffConfirmation(
        actor_id=actor.id,
        reason=data["reason"],
        observation_ids=[entry.id for entry in entries],
        reference_ids=refs,
        source_fingerprint=digest([source_snapshot(p, handoff), evidence]),
        evidence=evidence,
        reference_fingerprint=reference_signature,
    )
    production_set.dependency_revision += 1
    production_set.dependency_history.append(
        {
            "revision": production_set.dependency_revision,
            "handoff_id": handoff.id,
            "before_confirmation": previous,
            "confirmation": handoff.confirmation.model_dump(),
            "actor_id": actor.id,
            "reason": data["reason"],
            "at": now(),
        }
    )
    wake_target(p, production_set.id)
    return {
        "set_id": production_set.id,
        "handoff_id": handoff.id,
        "summary": data["reason"],
        "dependency_revision": production_set.dependency_revision,
        "evidence": evidence,
        "reference_ids": refs,
    }


def adopt_reference(
    service: StudioService, p: StudioProject, actor: Actor, data: dict[str, Any]
) -> dict[str, Any]:
    if actor.role == "material_executor":
        from .delegation import material_authority

        material_authority(p, actor, "adopt_observed_reference", data)
    else:
        authority(actor)
    state = p.states.get(str(data.get("state_id")))
    require(state, "资产状态不存在")
    assert state is not None
    require_context(p, actor, data, "references", state.id)
    entry = observation(p, str(data["observation_id"]))
    require(
        entry.approved_asset_id == state.asset_id and state.id in entry.approved_state_ids,
        "实拍截图尚未获准用于此资产状态",
    )
    require_state_source(p, state, [entry.id])
    require(data["view"] in p.assets[state.asset_id].required_views, "请先声明此视角")
    require(str(data.get("reason", "")).strip(), "请说明为何此真实画面足以直接复用")
    media_id = str(data["media_id"])
    require(
        any(same_source_image(p, media_id, source_id) for source_id in entry.evidence_media_ids),
        "只可复用批准观察中的原始图片或同来源的相同图片",
    )
    require(
        media_id in inspected_evidence(p, actor, [media_id], service.store), "先查看实际画面再复用"
    )
    ref = ReferenceRevision(
        state_id=state.id,
        view=data["view"],
        prompt="实拍截图直接复用；未请求图像生成。",
        media_id=media_id,
        status="candidate",
        reason=data["reason"],
        source_media_ids=[media_id],
        source_observation_ids=[entry.id],
    )
    p.references[ref.id] = ref
    state.revision_ids.append(ref.id)
    # The normal selection path preserves previous versions, impact checks and
    # quality allowances. Reusing footage does not spend a generated-image repair.
    result = service._select_reference(p, actor, {"revision_id": ref.id, "reason": data["reason"]})
    return {**result, "source_observation_id": entry.id, "summary": data["reason"]}
