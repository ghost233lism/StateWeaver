"""Task-relevant context with durable, complete-page reading receipts.

Project revision is intentionally not the context version: independent Sets can
advance while a director is reading. The digest covers only relevant effective
facts, plans, selected results, materials, proposals and (for review) evidence.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from typing import Any

from stateweaver.studio.adjudication import actual_end_state
from stateweaver.studio.context_cache import (
    context_read_current,
    core_fingerprint,
    dependencies,
    load_snapshot,
    save_snapshot,
    session_key,
    track_reads,
)
from stateweaver.studio.contracts import (
    Actor,
    Clip,
    ContextPhase,
    ContextRead,
    RequiredContextPage,
    SetDependencyReport,
    StudioProject,
)
from stateweaver.studio.image_input_recovery import quarantine
from stateweaver.studio.memory import digest, effective_entries, require
from stateweaver.studio.production_performance import measured_context_read
from stateweaver.studio.repository import select_entities
from stateweaver.studio.store import StudioContextRestartRequired, StudioStore
from stateweaver.studio.visibility import Visibility

PAGE_CONTENT_BYTES = 8000
CONTEXT_PROJECTION_VERSION = 6
CONTEXT_RECORD_KEY_VERSION = 1
CONTEXT_PROGRESS_MARKER_PREFIX = "required-context-turn:"


def record_context_turn_progress(actor: Actor) -> None:
    """Keep one turn-bound read signal in the existing, old-compatible field."""
    if not actor.turn_id:
        return
    actor.context_progress_turn_id = None
    actor.progress_markers = [
        marker
        for marker in actor.progress_markers
        if not marker.startswith(CONTEXT_PROGRESS_MARKER_PREFIX)
    ] + [CONTEXT_PROGRESS_MARKER_PREFIX + actor.turn_id]


def consume_context_turn_progress(actor: Actor, turn_id: str | None) -> bool:
    """Consume only read signals; preserve every business progress marker."""
    current_turn = actor.turn_id
    progressed = bool(
        current_turn
        and current_turn == turn_id
        and (
            actor.context_progress_turn_id == current_turn
            or CONTEXT_PROGRESS_MARKER_PREFIX + current_turn in actor.progress_markers
        )
    )
    actor.context_progress_turn_id = None
    actor.progress_markers = [
        marker
        for marker in actor.progress_markers
        if not marker.startswith(CONTEXT_PROGRESS_MARKER_PREFIX)
    ]
    return progressed


def resolve_target(p: StudioProject, target_id: str) -> tuple[str | None, Clip | None]:
    if target_id in p.candidates:
        clip = p.clips[p.candidates[target_id].clip_id]
        return clip.set_id, clip
    if target_id in p.clips:
        clip = p.clips[target_id]
        return clip.set_id, clip
    if target_id in p.sets:
        return target_id, None
    if target_id in p.proposals:
        proposal = p.proposals[target_id]
        ids = proposal.affected_clip_ids
        return proposal.set_id, p.clips[ids[0]] if len(ids) == 1 else None
    if target_id in p.visual_tasks:
        return None, None
    if target_id in p.delegate_tasks:
        return None, None
    require(target_id == "global" or target_id in p.states, "必读上下文的工作对象不存在")
    return None, None


def context_records(
    p: StudioProject,
    target_id: str,
    phase: ContextPhase,
    actor_id: str | None = None,
    *,
    include_preparation: bool = True,
) -> list[dict[str, Any]]:
    from .delegation import DELEGATE_ROLES

    if target_id in p.delegate_tasks and actor_id:
        from .delegation import executive_packet

        task = p.delegate_tasks[target_id]
        if p.actors[actor_id].role == "executive":
            require(task.parent_actor_id == actor_id, "报告不属于当前总导演")
            return executive_packet(p, task)
    if actor_id and p.actors[actor_id].role in DELEGATE_ROLES:
        from .delegation import packet as delegate_packet
        from .delegation import task_for as delegate_task_for

        actor = p.actors[actor_id]
        task = delegate_task_for(p, actor)
        require(
            target_id == task.id or target_id == task.request.state_id,
            "专项子对话请读取本任务 ID 或获准状态的任务包",
        )
        return delegate_packet(p, actor)
    if actor_id and p.actors[actor_id].role == "visual_analyst":
        from .visual_analysis import packet as visual_packet
        from .visual_analysis import task_for

        actor = p.actors[actor_id]
        require(
            target_id == task_for(p, actor).id and phase == "review",
            "看图子对话请读取 review / 本任务 ID 的任务包",
        )
        return visual_packet(p, actor)
    set_id, clip = resolve_target(p, target_id)
    if actor_id is None and set_id:
        actor_id = p.sets[set_id].actor_id
    visibility = Visibility(p, actor_id) if actor_id else None
    if visibility:
        visibility.target(target_id)
        if visibility.isolated and target_id == "global":
            set_id = visibility.set_id
    if phase == "review" and target_id in p.candidates:
        from .review_context import review_context_records
        from .review_sessions import candidate_session

        candidate = p.candidates[target_id]
        if candidate_session(candidate):
            packet = review_context_records(p, candidate)
            return visibility.records(packet) if visibility else packet
    sets = [p.sets[set_id]] if set_id else list(p.sets.values()) if target_id == "global" else []
    clips = [clip] if clip else [p.clips[i] for s in sets for i in s.clip_ids]
    if target_id in p.states:
        clips = [
            c
            for production_set in p.sets.values()
            for c in select_entities(p.clips, ids=production_set.clip_ids)
            if not c.archived_at
            and any(target_id in {x.start_state_id, x.end_state_id} for x in c.asset_plan)
        ]
        sets = [p.sets[i] for i in sorted({c.set_id for c in clips})]
    asset_ids = {x.asset_id for c in clips for x in c.asset_plan}
    if not clip:
        asset_ids = (
            set(p.sets[set_id].asset_ids)
            if (set_id)
            else set(p.assets)
            if target_id not in p.states
            else {p.states[target_id].asset_id}
        )
    if visibility and visibility.isolated:
        asset_ids.intersection_update(visibility.asset_ids)
    # No guessed semantic last-N window. Global rules and relevant per-subject
    # records are selected by indexed status/scope and all required pages follow.
    event_ids = {i for c in clips for i in c.story_event_ids}
    dimensions = {
        "asset_ids": sorted(asset_ids),
        "set_ids": [s.id for s in sets],
        "clip_ids": [c.id for c in clips],
        "event_ids": sorted(event_ids),
    }
    planning_scope = phase == "planning" and set_id is not None and clip is None
    if planning_scope:
        # During planning the absence of an event/Clip is not evidence of
        # non-applicability. Include all own- Set lineage and event conditions;
        # explicit scopes naming other Sets still exclude their memories.
        dimensions["clip_ids"] = [c.id for c in select_entities(p.clips, parent_id=set_id)]
        dimensions["event_ids"] = sorted(p.sets[str(set_id)].story_event_ids)
    if target_id == "global" and not (visibility and visibility.isolated):
        dimensions = None  # type: ignore[assignment]
    records: list[dict[str, Any]] = [
        {"type": "screenplay", "value": p.screenplay.model_dump(mode="json")},
        {"type": "task", "target_id": target_id, "phase": phase},
    ]
    for scope in ["global", *([set_id] if set_id else [])]:
        for entry in effective_entries(p, scope, context=dimensions, clip=clip):
            record = {"type": "memory", "scope": scope, "value": entry.model_dump(mode="json")}
            if planning_scope and (
                entry.scope.event_ids
                or entry.scope.clip_ids
                or entry.scope.starts_at
                or entry.scope.ends_at
            ):
                record["applicability"] = {
                    "status": "conditional",
                    "conditions": entry.scope.model_dump(mode="json"),
                    "instruction": (
                        "For complete planning; applies only when the listed "
                        "story-event, Clip and time conditions hold. Do not treat later"
                        " states as current facts."
                    ),
                }
            records.append(record)
    if phase == "planning":
        from .playback import report as playback_report

        records.append(
            {
                "type": "playback_summary",
                "value": playback_report(
                    p, set_id=visibility.set_id if visibility and visibility.isolated else set_id
                ).model_dump(mode="json", exclude={"sets"}),
            }
        )
    # Reuse upstream checks only within this read. Shared ancestry otherwise
    # rebuilds the same reports for every Set in a global planning packet.
    handoff_reports: dict[tuple[str, str | None], SetDependencyReport] = {}
    for production_set in sorted(sets, key=lambda x: x.id):
        records.append(
            {
                "type": "set",
                "value": production_set.model_dump(
                    exclude={
                        "memory",
                        "status",
                        "replan_repair_count",
                        "replan_parent_ids",
                        "handoff_history",
                        "duration_history",
                        "dependency_history",
                        "asset_handoffs",
                        "dependency_revision",
                    },
                    mode="json",
                ),
            }
        )
        from .set_handoffs import dependency_report

        report = dependency_report(p, production_set.id, clip, report_cache=handoff_reports)
        records.append(
            {
                "type": "set_dependencies",
                "value": {
                    "id": production_set.id,
                    **({"dependency_revision": report.dependency_revision} if clip is None else {}),
                    "items": report.items,
                    "blockers": [item.model_dump() for item in report.blockers],
                },
            }
        )
    state_ids = {
        i for c in clips for x in c.asset_plan for i in (x.start_state_id, x.end_state_id) if i
    }
    if clip:
        from .materials import auxiliary_states

        state_ids.update(auxiliary_states(p, clip))
        asset_ids.update(p.states[i].asset_id for i in state_ids)
    if not clip:
        for asset_id in asset_ids:
            state_ids.update(s.id for s in select_entities(p.states, parent_id=asset_id))
    for current in sorted(clips, key=lambda x: x.id):
        records.append(
            {
                "type": "clip_plan",
                "value": current.model_dump(
                    include={
                        "id",
                        "set_id",
                        "order",
                        "title",
                        "description",
                        "duration",
                        "asset_plan",
                        "story_time",
                        "story_end_time",
                        "story_event_ids",
                        "starts_at",
                        "ends_at",
                        "speaking_asset_ids",
                        "continuity",
                        "state_notes",
                        "state_readiness",
                        "plan_sources",
                    },
                    mode="json",
                ),
            }
        )
        if phase in {"planning", "references", "generation"}:
            # Persisted event/source contracts are creative plan facts, so they
            # must be visible and participate in these phases' fingerprints.
            records[-1]["value"].update(
                current.model_dump(include={"requirement_ids", "event_execution"}, mode="json")
            )
        if (
            include_preparation
            and current is clip
            and phase
            in {
                "planning",
                "references",
                "generation",
            }
        ):
            from .context_projection import clip_preparation

            records[-1]["value"]["preparation"] = clip_preparation(current)
        predecessor_id = (
            p.sets[current.set_id].clip_ids[current.order - 1] if current.order else None
        )
        from .continuation_controls import task_packet as continuation_packet

        if controls := continuation_packet(p, current):
            records.append(
                {"type": "continuation_policy", "clip_id": current.id, "value": controls}
            )
        predecessor = p.clips[predecessor_id] if predecessor_id else None
        if predecessor:
            selected = p.candidates.get(str(predecessor.accepted_candidate_id))
            selected_end = actual_end_state(selected) if selected else None
            records.append(
                {
                    "type": "previous_selected_actual",
                    "clip_id": predecessor.id,
                    "value": selected.model_dump(
                        include={
                            "id",
                            "media_id",
                            "end_state",
                            "confirmed_end_state",
                            "selection",
                            "observed_asset_states",
                            "bindings",
                        },
                        mode="json",
                    )
                    if selected
                    else None,
                    "continuation_end_state": selected_end.model_dump(mode="json")
                    if selected_end
                    else None,
                    "requires_director_confirmation": bool(
                        selected and not selected.confirmed_end_state
                    ),
                }
            )
            if selected_end and (blocked := quarantine(p, selected_end.frame_media_id)):
                records.append(
                    {
                        "type": "image_input_exclusion",
                        "media_id": selected_end.frame_media_id,
                        "value": blocked.model_dump(mode="json"),
                    }
                )
        if phase == "generation" and current.reference_plan_id:
            plan = p.reference_plans[current.reference_plan_id]
            records.append({"type": "final_reference_plan", "value": plan.model_dump(mode="json")})
    for asset_id in sorted(asset_ids):
        asset = p.assets[asset_id]
        records.append(
            {
                "type": "asset",
                "value": asset.model_dump(
                    include={
                        "id",
                        "name",
                        "kind",
                        "description",
                        "required_views",
                        "voice_bootstrap_clip_id",
                        "selected_voice_id",
                        "purpose",
                        "published_scope",
                        "publication_reason",
                    },
                    mode="json",
                ),
            }
        )
    for state_id in sorted(state_ids):
        state = p.states[state_id]
        event_ids.update(a.event_id for a in (state.starts_at, state.ends_at) if a)
        from .state_applicability import scoped_excluded_events

        exclusions = scoped_excluded_events(p, state, clip)
        event_ids.update(exclusions)
        records.append(
            {
                "type": "state",
                "value": state.model_dump(
                    exclude={
                        "revision_ids",
                        "repair_episodes",
                        "preparation_history",
                        "evolution_history",
                        "excluded_event_ids",
                    }
                    | ({"evolution"} if phase != "planning" or not state.evolution else set()),
                    mode="json",
                ),
            }
        )
        if exclusions:
            records[-1]["value"]["excluded_event_ids"] = exclusions
        from .set_handoffs import state_source_context

        records.append({"type": "state_source", "value": state_source_context(p, state)})
        for reference_id in sorted(state.selected_revisions.values()):
            ref = p.references[reference_id]
            records.append(
                {
                    "type": "selected_reference",
                    "value": ref.model_dump(
                        include={
                            "id",
                            "state_id",
                            "view",
                            "media_id",
                            "status",
                            "selection",
                            "source_observation_ids",
                        },
                        mode="json",
                    ),
                }
            )
            if ref.media_id in p.media:
                records.append(
                    {
                        "type": "media",
                        "value": p.media[str(ref.media_id)].model_dump(
                            exclude={"url"}, mode="json"
                        ),
                    }
                )
                if blocked := quarantine(p, str(ref.media_id)):
                    records.append(
                        {
                            "type": "image_input_exclusion",
                            "media_id": ref.media_id,
                            "value": blocked.model_dump(mode="json"),
                        }
                    )
                if actor_id:
                    from .external_review import external_review_item

                    external = external_review_item(
                        p,
                        str(ref.media_id),
                        "reference_inspection",
                        actor_id=actor_id,
                        clip_id=clip.id if clip else None,
                    )
                    if external and external[1].verdict == "approved":
                        review, item = external
                        records.append(
                            {
                                "type": "external_operator_review",
                                "media_id": ref.media_id,
                                "value": {
                                    "external_review_id": review.id,
                                    "reviewer_id": review.reviewer_id,
                                    "authorization_id": review.authorization_id,
                                    "scope": review.scope.model_dump(mode="json"),
                                    "source_sha256": item.source_sha256,
                                    "decision": item.verdict,
                                    "observation": item.observation,
                                    "native_model_inspection": False,
                                    "original_quarantine_retained": bool(blocked),
                                },
                            }
                        )
    events = (
        select_entities(p.story_events, ids=sorted(event_ids | set(p.sets[set_id].story_event_ids)))
        if (set_id)
        else select_entities(p.story_events, ids=sorted(event_ids))
        if clip
        else p.story_events.values()
    )
    for event in sorted(events, key=lambda e: (e.order, e.id)):
        if not clip or event.id in event_ids:
            records.append(
                {"type": "story_event", "value": event.model_dump(exclude={"history"}, mode="json")}
            )
    for proposal in select_entities(p.proposals, statuses=["pending", "accepted", "in_progress"]):
        if set_id and proposal.set_id != set_id:
            continue
        if (
            not clip
            or clip.id in proposal.affected_clip_ids
            or not proposal.affected_clip_ids
            and proposal.set_id == clip.set_id
        ):
            value = proposal.model_dump(
                exclude={"history", "material_requests_acknowledged_hash"}, mode="json"
            )
            if clip:
                value["requirements"] = [
                    r
                    for r in value["requirements"]
                    if not r["clip_ids"] or clip.id in r["clip_ids"]
                ]
                value["affected_clip_ids"] = [clip.id]
                # Other Clips progressing in this proposal must not invalidate
                # a locally ready reference draft.
                value.pop("resolution", None)
                if value["requirements"] and all(
                    r["status"] == "done" for r in value["requirements"]
                ):
                    continue
            records.append({"type": "proposal", "value": value})
    if phase == "review":
        if target_id in p.states:
            for reference in select_entities(p.references, parent_id=target_id):
                records.append(
                    {"type": "review_reference", "value": reference.model_dump(mode="json")}
                )
        elif target_id in p.candidates:
            from .image_input_recovery import available_evidence, review_gaps

            candidate = p.candidates[target_id]
            records.append(
                {
                    "type": "review_coverage",
                    "available_media_ids": available_evidence(p, candidate),
                    "coverage_gaps": [g.model_dump(mode="json") for g in review_gaps(p, candidate)],
                }
            )
            records.append(
                {
                    "type": "review_candidate",
                    "value": candidate.model_dump(mode="json"),
                }
            )
        elif clip:
            if actor_id and p.actors[actor_id].role == "executive":
                from .quality_context import decision_records

                records.extend(decision_records(p, clip))
            else:
                for candidate in select_entities(
                    p.candidates, parent_id=clip.id, statuses=["review", "rejected", "accepted"]
                ):
                    records.append(
                        {"type": "quality_candidate", "value": candidate.model_dump(mode="json")}
                    )
    if clip and actor_id and phase in {"references", "generation"}:
        from .reference_coverage import context_records as composite_reference_records

        records.extend(composite_reference_records(p, p.actors[actor_id], clip))
    visible = visibility.records(records) if visibility else records
    from .context_projection import compact_records

    return compact_records(visible)


def context_fingerprint(
    p: StudioProject,
    target_id: str,
    phase: ContextPhase = "references",
    actor_id: str | None = None,
) -> str:
    from .director_efficiency import compact_records

    records = context_records(p, target_id, phase, actor_id)
    if (
        actor_id
        and p.actors[actor_id].role == "set_director"
        or (not actor_id and (target_id in p.clips or target_id in p.candidates))
    ):
        records = compact_records(records)
    return digest(records)


def receipt_id(actor_id: str, target_id: str, phase: ContextPhase) -> str:
    return digest(["required-context-v1", actor_id, target_id, phase])[:32]


def _record_key(record: dict[str, Any]) -> str:
    value = record.get("value") or {}
    return ":".join(
        str(x or "")
        for x in (
            record["type"],
            record.get("scope"),
            value.get("id"),
            record.get("clip_id"),
            record.get("target_id") or record.get("media_id"),
        )
    )


def _indexed_records(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Give each record its own key, even when it has no distinct contextual ID."""
    counts: Counter[str] = Counter()
    indexed = {}
    for record in records:
        key = _record_key(record)
        ordinal = counts[key]
        counts[key] += 1
        # The prefix is disjoint from ordinary keys, which begin with a record type.
        indexed[f"#{ordinal}:{key}" if ordinal else key] = record
    return indexed


def _context_pages(content: str) -> list[str]:
    """Bound the JSON-escaped UTF-8 content, preserving every source character."""
    pages: list[str] = []
    start = 0
    while start < len(content):
        end = min(start + PAGE_CONTENT_BYTES, len(content))
        while True:
            size = len(json.dumps(content[start:end], ensure_ascii=False).encode())
            if size <= PAGE_CONTENT_BYTES:
                break
            end = start + max(1, (end - start) * (PAGE_CONTENT_BYTES - 2) // size)
        pages.append(content[start:end])
        start = end
    return pages or [""]


def _page_dependencies_current(p: StudioProject, receipt: ContextRead) -> bool:
    # Historical projects keep their persisted pagination behavior. New projects
    # use the same semantic freshness rule for pages and authorized commands.
    return context_read_current(
        p,
        receipt,
        semantic_fallback=(
            getattr(p.config, "production_lifecycle_policy", "current") == "coherent-v1"
        ),
    )


def _snapshot_projection_current(
    p: StudioProject,
    receipt: ContextRead,
    snapshot: dict[str, Any],
    projection_version: int,
) -> bool:
    """Keep exact v5 pages only when their full records equal the v6 projection."""
    if (
        snapshot.get("page_content_bytes") != PAGE_CONTENT_BYTES
        or snapshot.get("record_key_version") != CONTEXT_RECORD_KEY_VERSION
    ):
        return False
    if snapshot.get("projection_version") == projection_version:
        if receipt.phase in {"planning", "references", "generation"}:
            for record in snapshot.get("records", []):
                if record.get("type") != "clip_plan":
                    continue
                value = record.get("value")
                if not isinstance(value, dict):
                    return False
                clip = p.clips.get(str(value.get("id")))
                if clip is None:
                    return False
                fields = {"requirement_ids", "event_execution"}
                current = clip.model_dump(include=fields, mode="json")
                # Older same-version records may omit real persisted contracts.
                # Empty contracts serialize without keys and remain compatible.
                if any(value.get(field, []) != current.get(field, []) for field in fields):
                    return False
        return True
    if not (
        CONTEXT_PROJECTION_VERSION == projection_version == 6
        and snapshot.get("projection_version") == 5
        and getattr(p.config, "production_lifecycle_policy", "current") == "coherent-v1"
        and receipt.read_kind == "mandatory"
        and receipt.protocol_version == 2
        and receipt.actor_id in p.actors
        and receipt.session_key == session_key(p.actors[receipt.actor_id], p)
        and snapshot.get("session_key") == receipt.session_key
        and len(snapshot.get("pages", [])) == receipt.page_count
        and digest(snapshot.get("records", [])) == receipt.fingerprint
        and _page_dependencies_current(p, receipt)
    ):
        return False
    records = context_records(p, receipt.target_id, receipt.phase, receipt.actor_id)
    from .director_efficiency import compact_records

    if p.actors[receipt.actor_id].role == "set_director":
        records = compact_records(records)
    return records == snapshot.get("records")


@measured_context_read
def read_required_context(
    store: StudioStore,
    project_id: str,
    actor_id: str,
    phase: ContextPhase,
    target_id: str,
    page: int | None = 0,
    fingerprint: str | None = None,
    *,
    mode: str = "full",
    baseline_token: str | None = None,
    snapshot_id: str | None = None,
    full_verification_reason: str | None = None,
) -> RequiredContextPage:
    require(mode in {"full", "auto"}, "上下文读取模式无效")
    verification = (full_verification_reason or "").strip()
    require(
        not full_verification_reason or mode == "full" and len(verification) >= 12,
        "全文核验例外须使用 full 并说明具体核验对象与原因（至少 12 字符）",
    )
    # A caller choosing 'full' is not evidence that the valid local baseline is
    # missing. Only an explicit, archived verification reason bypasses deltas.
    incremental = not verification
    require(phase in {"planning", "references", "generation", "review"}, "上下文阶段无效")
    resume_incomplete = page is None and mode == "auto" and not verification
    page = 0 if page is None else page
    require(page >= 0, "必读上下文页码无效")
    identifier = receipt_id(actor_id, target_id, phase)
    reused_incomplete = False

    def require_current(condition: object, message: str) -> None:
        if not condition:
            raise StudioContextRestartRequired(message, phase=phase, target_id=target_id)

    def build(
        p: StudioProject,
    ) -> tuple[dict[str, Any], ContextRead, tuple[str | None, int, str | None]]:
        nonlocal page, fingerprint, snapshot_id, reused_incomplete
        require(actor_id in p.actors, "角色不存在")
        actor = p.actors[actor_id]
        binding = (actor.turn_id, actor.lease_generation, actor.lease_owner)
        visibility = Visibility(p, actor_id)
        visibility.target(target_id)
        # All contexts use the bounded page encoding. Global final review keeps
        # its dedicated projection revision as well.
        final_review_scope = phase == "review" and target_id == "global" and not visibility.isolated
        projection_version = CONTEXT_PROJECTION_VERSION + int(final_review_scope)
        # Recovery instructions may only name a verified same-project work target.
        resolve_target(p, target_id)
        actor_session = session_key(p.actors[actor_id], p)
        old = p.context_reads.get(identifier)
        projection_checks: dict[tuple[str, str | None, int], bool] = {}

        def projection_current(value: dict[str, Any], read: ContextRead, version: int) -> bool:
            key = (read.id, read.snapshot_id, version)
            if key not in projection_checks:
                projection_checks[key] = _snapshot_projection_current(p, read, value, version)
            return projection_checks[key]

        read_reason = "full_verification" if verification else "no_complete_baseline"
        if (
            resume_incomplete
            and not baseline_token
            and old
            and old.actor_id == actor_id
            and old.target_id == target_id
            and old.phase == phase
            and not old.complete
            and old.read_kind == "mandatory"
            and old.protocol_version == 2
            and old.session_key == actor_session
            and old.snapshot_id
            and 0 < old.next_page < old.page_count
            and (not fingerprint or fingerprint == old.fingerprint)
            and (not snapshot_id or snapshot_id == old.snapshot_id)
            and _page_dependencies_current(p, old)
        ):
            try:
                unfinished = load_snapshot(store, old.snapshot_id)
            except (OSError, ValueError):
                unfinished = None
            if (
                unfinished
                and unfinished.get("session_key") == actor_session
                and projection_current(unfinished, old, projection_version)
                and len(unfinished.get("pages", [])) == old.page_count
                and digest(unfinished.get("records", [])) == old.fingerprint
            ):
                # The receipt commits before the native tool body is delivered.
                # Repeat its last served page so a lost response never skips content.
                page = old.next_page - 1
                fingerprint, snapshot_id = old.fingerprint, old.snapshot_id
                reused_incomplete = True
                return unfinished, old.model_copy(deep=True), binding
        if not page and incremental:
            phases: tuple[ContextPhase, ...] = ("planning", "references", "generation", "review")
            choices = [
                p.context_reads.get(receipt_id(actor_id, target_id, other)) for other in phases
            ]
            complete = [
                r
                for r in choices
                if r
                and r.complete
                and r.read_kind == "mandatory"
                and r.protocol_version == 2
                and r.session_key == actor_session
                and r.snapshot_id
            ]
            if baseline_token:
                old = next(
                    (r for r in complete if baseline_token == f"{r.id}:{r.fingerprint}"), old
                )
            elif not old or not old.complete or old.session_key != actor_session:
                old = max(complete, key=lambda r: r.created_at) if complete else old
            if old and old.session_key != actor_session:
                read_reason = "session_changed"
        previous = None
        if old and old.snapshot_id and old.session_key == actor_session:
            try:
                previous = load_snapshot(store, old.snapshot_id)
            except (OSError, ValueError):
                previous = None
        previous_current = bool(
            previous
            and old
            and projection_current(
                previous,
                old,
                CONTEXT_PROJECTION_VERSION
                + int(
                    old.phase == "review" and old.target_id == "global" and not visibility.isolated
                ),
            )
        )
        if page:
            require_current(
                old
                and previous
                and old.fingerprint == fingerprint
                and (not snapshot_id or snapshot_id == old.snapshot_id)
                and projection_current(previous, old, projection_version),
                "相关记忆或会话已变化，请从第 0 页重新完整读取",
            )
            assert old is not None and previous is not None
            require_current(page <= old.next_page, "请按顺序读完必需分页，不可跳过上下文")
            require_current(
                _page_dependencies_current(p, old),
                "相关记忆或素材已变化，请从第 0 页重新读取必读上下文",
            )
            return previous, old.model_copy(deep=True), binding
        track_reads(p)
        if (
            not verification
            and previous
            and old
            and old.phase == phase
            and projection_current(previous, old, projection_version)
            and _page_dependencies_current(p, old)
        ):
            records = previous["records"]
            deps, core = old.dependencies, old.core_fingerprint
        else:
            records = context_records(p, target_id, phase, actor_id)
            from .director_efficiency import compact_records

            if p.actors[actor_id].role == "set_director":
                records = compact_records(records)
            deps, core = dependencies(p), core_fingerprint(p)
        version = digest(records)
        can_delta = bool(
            incremental
            and previous
            and previous_current
            and old
            and old.complete
            and old.read_kind == "mandatory"
            and old.protocol_version == 2
            and (not baseline_token or baseline_token == f"{old.id}:{old.fingerprint}")
        )
        baseline = old.fingerprint if can_delta and old else None
        if can_delta:
            read_reason = (
                "new_phase_delta" if old and old.phase != phase else "current_context_delta"
            )
        elif previous and not previous_current:
            read_reason = "pagination_budget_changed"
        elif incremental and baseline_token:
            read_reason = "baseline_unverified"
        after = _indexed_records(records)
        full_payload = [{"record_key": k, **r} for k, r in after.items()]
        if can_delta:
            assert previous is not None
            before = _indexed_records(previous["records"])
            payload: Any = {
                "upserts": [{"record_key": k, **r} for k, r in after.items() if before.get(k) != r],
                "removed_keys": sorted(set(before) - set(after)),
                "record_order": list(after),
                "baseline_fingerprint": baseline,
                "fingerprint": version,
            }
        else:
            payload = full_payload
        content = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        pages = _context_pages(content)
        snapshot = {
            "projection_version": projection_version,
            "record_key_version": CONTEXT_RECORD_KEY_VERSION,
            "page_content_bytes": PAGE_CONTENT_BYTES,
            "records": records,
            "pages": pages,
            "session_key": actor_session,
            "mode": "delta" if can_delta else "full",
            "baseline_fingerprint": baseline,
            "baseline_context_id": old.id if can_delta and old else None,
            "read_reason": read_reason,
            "full_verification_reason": verification or None,
            "payload_bytes": len(content.encode()),
            "full_payload_bytes": len(
                json.dumps(
                    full_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ),
        }
        receipt = ContextRead(
            id=identifier,
            actor_id=actor_id,
            target_id=target_id,
            phase=phase,
            fingerprint=version,
            page_count=len(pages),
            protocol_version=2,
            session_key=actor_session,
            dependencies=deps,
            core_fingerprint=core,
            mode="delta" if can_delta else "full",
            baseline_fingerprint=baseline,
        )
        return snapshot, receipt, binding

    snapshot, receipt, binding = store.read(project_id, build)
    require(0 <= page < receipt.page_count, "必读上下文页码无效")
    if not page and not reused_incomplete:
        receipt.snapshot_id = save_snapshot(store, snapshot)
    receipt.next_page = max(receipt.next_page, page + 1)
    receipt.complete = receipt.next_page == receipt.page_count

    def commit(p: StudioProject) -> None:
        require_current(
            session_key(p.actors[actor_id], p) == receipt.session_key,
            "会话基线已变化，请重新完整读取",
        )
        require_current(
            _page_dependencies_current(p, receipt),
            "相关记忆或素材在读取时变化，请重新读取",
        )
        if page or reused_incomplete:
            current = p.context_reads.get(identifier)
            require_current(
                current
                and current.id == receipt.id
                and current.actor_id == receipt.actor_id
                and current.target_id == receipt.target_id
                and current.phase == receipt.phase
                and current.read_kind == receipt.read_kind
                and current.protocol_version == receipt.protocol_version
                and current.session_key == receipt.session_key
                and current.snapshot_id == receipt.snapshot_id
                and current.fingerprint == receipt.fingerprint
                and current.page_count == receipt.page_count == len(snapshot["pages"])
                and current.mode == receipt.mode
                and current.baseline_fingerprint == receipt.baseline_fingerprint
                and current.dependencies == receipt.dependencies
                and current.core_fingerprint == receipt.core_fingerprint
                and current.created_at == receipt.created_at
                and 0 <= current.next_page <= current.page_count,
                "上下文分页已被更新，请从第 0 页重读",
            )
            assert current is not None
            # A delayed explicit page can race a later page just like an auto
            # resend. Preserve only this exact read's committed high water mark;
            # a fresh page-zero read must not inherit the previous read's cursor.
            receipt.next_page = max(receipt.next_page, current.next_page)
            receipt.complete = receipt.next_page == receipt.page_count
            if (
                current
                and current.read_kind == receipt.read_kind == "mandatory"
                and current.protocol_version == receipt.protocol_version == 2
                and current.session_key == receipt.session_key
                and current.fingerprint == receipt.fingerprint
                and receipt.next_page > current.next_page
                and binding[0]
                and (
                    p.actors[actor_id].turn_id,
                    p.actors[actor_id].lease_generation,
                    p.actors[actor_id].lease_owner,
                )
                == binding
            ):
                # Reading a new required page is useful work in this native turn.
                # Keep this separate from creative progress and recovery budgets.
                record_context_turn_progress(p.actors[actor_id])
        p.context_reads[identifier] = receipt

    updated = store.mutate(
        project_id, commit, actor_id=actor_id, kind="context.read", advance_revision=False
    )
    return RequiredContextPage(
        context_id=identifier,
        target_id=target_id,
        phase=phase,
        fingerprint=receipt.fingerprint,
        page=page,
        page_count=receipt.page_count,
        content=snapshot["pages"][page],
        next_page=receipt.next_page if receipt.next_page < receipt.page_count else None,
        context_token=f"{identifier}:{receipt.fingerprint}" if receipt.complete else None,
        revision=updated.revision,
        mode=receipt.mode,
        baseline_fingerprint=receipt.baseline_fingerprint,
        record_count=len(snapshot["records"]),
        snapshot_id=receipt.snapshot_id,
        baseline_context_id=snapshot.get("baseline_context_id"),
        read_reason=snapshot.get("read_reason", "legacy_snapshot"),
        payload_bytes=snapshot.get("payload_bytes", 0),
        full_payload_bytes=snapshot.get("full_payload_bytes", 0),
    )


@dataclass(frozen=True)
class CurrentContextBasis:
    """Server semantic state for dependency checks, never a reading receipt."""

    fingerprint: str
    dependencies: list[dict[str, Any]]


def full_context_required(project: StudioProject) -> bool:
    return project.config.full_context_policy == "strict"


def _current_context_basis(
    project: StudioProject, actor: Actor, phase: ContextPhase, target_id: str, token: str
) -> CurrentContextBasis:
    from .context_cache import validate_read_token

    # Accept only a genuine currently valid bounded tool read or completed full read.
    # This does not promote an incomplete pagination cursor or an old session receipt.
    validate_read_token(project, actor, token)
    track_reads(project)
    fingerprint = context_fingerprint(project, target_id, phase, actor.id)
    return CurrentContextBasis(fingerprint=fingerprint, dependencies=dependencies(project))


def require_context(
    p: StudioProject, actor: Actor, data: dict[str, Any], phase: ContextPhase, target_id: str
) -> ContextRead | CurrentContextBasis:
    approved = p.__dict__.get("_approved_delegate_context")
    if approved and actor.role == "executive" and target_id != approved[0]:
        task = p.delegate_tasks[approved[0]]
        scope = task.allowed_scope
        require(
            target_id == "global"
            or any(target_id in getattr(scope, f) for f in type(scope).model_fields),
            "批准的专项报告不能授权其他工作对象",
        )
        if not full_context_required(p):
            # The internal marker is created only after version-bound report approval.
            # Retain its exact scoped authorization, without inventing a full-read receipt.
            return _current_context_basis(p, actor, phase, target_id, str(approved[1]))
        # This marker is set only by atomic approval execution after complete,
        # version-bound reading. Actors cannot create it through an MCP payload.
        receipt = p.context_reads.get(receipt_id(actor.id, task.id, "review"))
        require(
            receipt and receipt.complete and approved[1] == f"{receipt.id}:{receipt.fingerprint}",
            "专项审批缺少有效报告阅读凭据",
        )
        assert receipt is not None
        return receipt
    Visibility(p, actor.id).target(target_id)
    if not full_context_required(p):
        return _current_context_basis(
            p, actor, phase, target_id, str(data.get("context_token") or "")
        )
    identifier = receipt_id(actor.id, target_id, phase)
    receipt = p.context_reads.get(identifier)
    require(
        receipt and receipt.complete, "请先通过 get_required_context 读完当前工作的全部必读上下文"
    )
    assert receipt is not None
    require(
        data.get("context_token") == f"{identifier}:{receipt.fingerprint}",
        "请提供完整阅读后返回的 context_token",
    )
    require(receipt.read_kind == "mandatory", "此凭据不是完整必读上下文")
    require(
        receipt.protocol_version == 2 and receipt.session_key == session_key(actor, p),
        "会话基线已变化，请重新完整读取",
    )
    require(
        context_read_current(p, receipt, semantic_fallback=True),
        "相关记忆、状态、前段实际结果或素材已更新，请重新读取必读上下文",
    )
    return receipt
