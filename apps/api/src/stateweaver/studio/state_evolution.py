"""Executive-confirmed story-state ancestry, separate from image input provenance.

This metadata never changes event applicability, reference selection or generation
dependencies. Missing historical ancestry stays explicit until an executive review.
"""

from __future__ import annotations

import hashlib
from typing import Any

from .contracts import (
    Actor,
    AssetState,
    StateEvolution,
    StateEvolutionEvidence,
    StateEvolutionInput,
    StudioProject,
)
from .store import StudioError


def _require(condition: object, message: str) -> None:
    if not condition:
        raise StudioError(message, 422)


def evidence_text(p: StudioProject, state: AssetState, evidence: StateEvolutionEvidence) -> str:
    if evidence.kind == "state":
        target = state if evidence.id == "self" else p.states.get(evidence.id)
        _require(target and target.asset_id == state.asset_id, "演变依据中的状态必须属于同一资产")
        assert target is not None
        return f"{target.label}\n{target.description}"
    if evidence.kind == "event":
        event = p.story_events.get(evidence.id)
        _require(event, "演变依据中的剧情事件不存在")
        assert event is not None
        return f"{event.title}\n{event.description}"
    if evidence.kind == "screenplay":
        _require(p.screenplay.confirmed_at, "演变依据必须引用已确认剧本")
        if evidence.id in {"confirmed", p.screenplay.confirmed_hash}:
            return p.screenplay.text
        fact = next((f for f in p.screenplay.facts if f.id == evidence.id), None)
        _require(fact, "演变依据中的已确认剧本事实不存在")
        assert fact is not None
        return fact.text
    repositories = getattr(p, "_repositories", None)
    matches = (
        repositories["memory_entries"].select(ids=[evidence.id], parent_id="global")
        if repositories
        else [entry for entry in p.global_memory.story if entry.id == evidence.id]
    )
    _require(matches, "演变依据中的全局剧情记忆不存在")
    return str(matches[0].text)


def record_state_evolution(
    p: StudioProject,
    actor: Actor,
    state_id: str,
    value: StateEvolutionInput,
    expected_revision: int,
    *,
    review_session_id: str | None = None,
) -> dict[str, Any]:
    _require(actor.role == "executive", "只有总导演可以确认状态演变来源")
    state = p.states.get(state_id)
    _require(state, "状态演变的目标不存在")
    assert state is not None
    before = state.evolution
    actual_revision = before.revision if before else 0
    if expected_revision != actual_revision:
        raise StudioError(
            f"状态 {state.label} 的演变记录已更新：期望修订 {expected_revision}，"
            f"实际修订 {actual_revision}；请重新核对后提交",
            409,
        )
    if value.parent_state_id:
        parent = p.states.get(value.parent_state_id)
        _require(parent, f"状态 {state.label} 的父状态不存在：{value.parent_state_id}")
        assert parent is not None
        _require(
            parent.asset_id == state.asset_id,
            f"状态 {state.label} 与父状态 {parent.label} 属于不同资产，不能建立演变关系",
        )
        seen = {state.id}
        cursor: AssetState | None = parent
        while cursor:
            _require(cursor.id not in seen, f"状态 {state.label} → {parent.label} 会形成演变循环")
            seen.add(cursor.id)
            ancestor = cursor.evolution.parent_state_id if cursor.evolution else None
            cursor = p.states.get(ancestor) if ancestor else None
            _require(not ancestor or cursor, "父状态的演变链不完整，请先确认其来源")
        if state.starts_at and parent.starts_at:
            from .memory import anchor_key

            _require(
                anchor_key(p, state.starts_at) >= anchor_key(p, parent.starts_at),
                f"状态 {state.label} 的起始事件早于父状态 {parent.label}，请核对演变来源",
            )
    evidence = []
    hashes: dict[str, str] = {}
    for source in value.evidence:
        text = evidence_text(p, state, source)
        quote = " ".join(source.quote.split())
        _require(
            quote and quote in " ".join(text.split()),
            f"演变依据 {source.kind}:{source.id} 的引文与原记录不符",
        )
        resolved = (
            source.model_copy(update={"id": state.id})
            if source.kind == "state" and source.id == "self"
            else source
        )
        evidence.append(resolved)
        hashes[f"{resolved.kind}:{resolved.id}"] = hashlib.sha256(text.encode()).hexdigest()
    normalized = value.model_copy(update={"evidence": evidence})
    if (
        before
        and StateEvolutionInput.model_validate(
            before.model_dump(include=set(StateEvolutionInput.model_fields))
        )
        == normalized
    ):
        return {"state_id": state.id, "evolution_revision": actual_revision, "no_progress": True}
    if before:
        state.evolution_history.append(before.model_copy(deep=True))
    state.evolution = StateEvolution(
        **normalized.model_dump(),
        revision=actual_revision + 1,
        actor_id=actor.id,
        evidence_hashes=hashes,
        review_session_id=review_session_id,
    )
    return {
        "asset_id": state.asset_id,
        "state_id": state.id,
        "parent_state_id": normalized.parent_state_id,
        "evolution_revision": state.evolution.revision,
        "evolution_kind": normalized.kind,
        "summary": normalized.reason,
    }
