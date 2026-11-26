"""Preserve scoped memory applicability when an unaccepted Clip suffix is replanned."""

from __future__ import annotations

import json

from .contracts import Actor, Clip, MemorySource, Proposal, StudioProject, new_id, now
from .memory import effective_entries
from .repository import select_entities


def inherit_memory_scopes(
    p: StudioProject, actor: Actor, previous: list[Clip], replacements: list[Clip]
) -> dict[str, list[str]]:
    """Only event/subject/explicit-lineage matches inherit; uncertain facts go to the executive."""
    if not previous or not replacements:
        return {"inherited": [], "needs_review": []}
    set_id = replacements[0].set_id
    old = {c.id: c for c in previous}
    result: dict[str, list[str]] = {"inherited": [], "needs_review": []}
    for scope in ("global", set_id):
        memory = p.global_memory if scope == "global" else p.sets[set_id].memory
        for entry in effective_entries(p, scope):
            affected = set(entry.scope.clip_ids) & set(old)
            if not affected or entry.basis == "observed" or entry.section == "screenplay":
                continue
            mapping: dict[str, list[str]] = {}
            ambiguous = False
            for identifier in sorted(affected):
                source = old[identifier]
                events = set(entry.scope.event_ids or source.story_event_ids)
                subjects = set(entry.scope.asset_ids) or {x.asset_id for x in source.asset_plan}
                matches = []
                for target in replacements:
                    explicit = any(
                        s.kind == "decision"
                        and s.id == identifier
                        and s.note == "explicit_replan_parent"
                        for s in target.plan_sources
                    )
                    event_match = bool(events & set(target.story_event_ids))
                    subject_match = not subjects or bool(
                        subjects & {x.asset_id for x in target.asset_plan}
                    )
                    # An explicit Clip map does not override an incompatible event
                    # condition already attached to the global memory.
                    if (explicit or event_match and subject_match) and (
                        not entry.scope.event_ids or event_match
                    ):
                        matches.append(target.id)
                mapping[identifier] = matches
                ambiguous |= not matches
            if ambiguous:
                key = "memory-scope-replan:" + entry.id + ":" + replacements[0].id
                existing = next(
                    (
                        x
                        for x in select_entities(
                            p.proposals, statuses=["pending", "accepted", "in_progress"]
                        )
                        if x.issue_key == key
                    ),
                    None,
                )
                if not existing:
                    proposal = Proposal(
                        actor_id=actor.id,
                        set_id=set_id,
                        issue_key=key,
                        text="Clip 重排后记忆适用范围需总导演核对。原记录："
                        + entry.id
                        + "\n"
                        + entry.text
                        + "\n请依据事件、资产和新旧计划明确适用范围，保留原文和映射依据。",
                        affected_clip_ids=[c.id for c in replacements],
                        history=[
                            {
                                "kind": "memory_scope_mapping",
                                "memory_id": entry.id,
                                "mapping": mapping,
                                "created_at": now(),
                            }
                        ],
                    )
                    p.proposals[proposal.id] = proposal
                result["needs_review"].append(entry.id)
                continue
            updated = entry.model_copy(deep=True)
            updated.id = new_id()
            updated.created_at = now()
            updated.version += 1
            updated.scope.clip_ids = sorted(
                (set(entry.scope.clip_ids) - affected)
                | {i for ids in mapping.values() for i in ids}
            )
            updated.supersedes_ids = [entry.id]
            updated.superseded_by = None
            updated.reason = "依据剧情事件、适用资产与 Clip 重排映射自动继承范围；原始内容不变。"
            updated.sources.append(
                MemorySource(
                    kind="memory",
                    id=entry.id,
                    note="Clip applicability mapping: " + json.dumps(mapping, sort_keys=True),
                )
            )
            entry.status, entry.superseded_by = "superseded", updated.id
            memory.story.append(updated)
            memory.revision += 1
            result["inherited"].append(updated.id)
    return result
