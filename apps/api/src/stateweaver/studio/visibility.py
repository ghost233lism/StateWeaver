"""Set-director read boundary, shared by MCP reads and command source validation.

Publishing an asset or an evidence image does not publish the producing director's
candidate, prompt, job or conversation. Human projections remain unrestricted.
"""

from __future__ import annotations

from functools import cached_property
from typing import Any

from sqlalchemy import select

from stateweaver.persistence.models import StudioBodyRow, StudioTranscriptRow

from .contracts import MemoryEntry, StudioProject
from .memory import anchor_key, memory_record
from .repository import select_entities
from .store import StudioError, StudioStore

CATEGORIES = {
    "actor": "actors",
    "set": "sets",
    "clip": "clips",
    "candidate": "candidates",
    "asset": "assets",
    "state": "states",
    "reference": "references",
    "media": "media",
    "job": "jobs",
    "memory-entry": "memory_entries",
    "proposal": "proposals",
    "reference-plan": "reference_plans",
    "story-event": "story_events",
    "transcript": "transcript",
    "visual-task": "visual_tasks",
    "delegate-task": "delegate_tasks",
    "executive-document": "executive_documents",
    "executive-document-version": "executive_document_versions",
}


class Visibility:
    def __init__(self, project: StudioProject, actor_id: str) -> None:
        self.project = project
        self.actor = project.actors[actor_id]
        self.analyst = self.actor.role == "visual_analyst"
        from .delegation import DELEGATE_ROLES

        self.delegate = self.actor.role in DELEGATE_ROLES
        self.isolated = self.analyst or self.delegate or (self.actor.role == "set_director")
        self.set_id = self.actor.set_id
        self._allowed: dict[tuple[str, str], bool] = {}

    @cached_property
    def clip_ids(self) -> set[str]:
        if self.delegate:
            from .delegation import task_for

            return set(task_for(self.project, self.actor).allowed_scope.clip_ids)
        if self.analyst:
            return set()
        return {c.id for c in select_entities(self.project.clips, parent_id=self.set_id)}

    @cached_property
    def asset_ids(self) -> set[str]:
        if self.delegate:
            from .delegation import task_for

            return set(task_for(self.project, self.actor).allowed_scope.asset_ids)
        if self.analyst:
            return set()
        return set(self.project.sets[str(self.set_id)].asset_ids)

    @cached_property
    def event_ids(self) -> set[str]:
        if self.delegate:
            from .delegation import task_for

            return set(task_for(self.project, self.actor).allowed_scope.event_ids)
        if self.analyst:
            return set()
        return set(self.project.sets[str(self.set_id)].story_event_ids)

    def memory_allowed(self, entry: MemoryEntry, scope: str) -> bool:
        if self.delegate:
            from .delegation import task_for

            return (
                scope == "global"
                and entry.id in task_for(self.project, self.actor).allowed_scope.memory_ids
            )
        if self.analyst:
            return False  # Necessary scoped facts are supplied only in the assignment packet.
        if not self.isolated:
            return True
        if scope == self.set_id:
            return True
        if scope != "global":
            return False
        author = self.project.actors.get(str(entry.actor_id))
        if entry.section != "screenplay" and not (author and author.role == "executive"):
            return False
        for named, visible in (
            (entry.scope.set_ids, {self.set_id}),
            (entry.scope.clip_ids, self.clip_ids),
            (entry.scope.asset_ids, self.asset_ids),
            (entry.scope.event_ids, self.event_ids),
        ):
            if named and not visible.intersection(named):
                return False
        if entry.scope.starts_at or entry.scope.ends_at:
            orders = [self.project.story_events[i].order for i in self.event_ids]
            if not orders:
                return False
            if entry.scope.starts_at and anchor_key(self.project, entry.scope.starts_at) > (
                max(orders),
                1,
            ):
                return False
            if entry.scope.ends_at and anchor_key(self.project, entry.scope.ends_at) < (
                min(orders),
                0,
            ):
                return False
        return True

    @cached_property
    def published_media(self) -> set[str]:
        ids = {
            v.media_id
            for asset_id in self.asset_ids
            for v in self.project.assets[asset_id].voice_references
            if v.media_id
        }
        for entry in self.project.global_memory.story:
            if self.memory_allowed(entry, "global"):
                ids.update(entry.evidence_media_ids)
                ids.update(s.id for s in entry.sources if s.kind == "media")
        return ids

    def allowed(self, kind: str, identifier: str) -> bool:
        if self.delegate:
            from .delegation import allowed

            return allowed(self.project, self.actor, kind, identifier)
        if self.analyst:
            return self._analyst_allowed(kind, identifier)
        if not self.isolated:
            return True
        key = kind, identifier
        if key in self._allowed:
            return self._allowed[key]
        # Fail closed for a malformed cyclic provenance record.
        self._allowed[key] = False
        value = self._check(kind, identifier)
        self._allowed[key] = value
        return value

    def _analyst_allowed(self, kind: str, identifier: str) -> bool:
        from .visual_analysis import task_for

        task = task_for(self.project, self.actor)
        if kind == "actors":
            return identifier == self.actor.id
        if kind == "visual_tasks":
            return identifier == task.id
        if kind == "media":
            return identifier in task.media_ids
        if kind == "candidates":
            return identifier in task.candidate_ids
        if kind == "references":
            return identifier in task.revision_ids
        if kind == "transcript":
            item = self.project.transcript.get(identifier)
            return bool(item and item.actor_id == self.actor.id)
        return False

    def _check(self, kind: str, identifier: str) -> bool:
        p = self.project
        if kind == "memory_entries":
            record = memory_record(p, identifier)
            return bool(record and self.memory_allowed(record[1], record[0]))
        records = getattr(p, kind, {})
        item = records.get(identifier)
        if item is None:
            return False
        if kind in {"actors", "transcript"}:
            return bool((item.id if kind == "actors" else item.actor_id) == self.actor.id)
        if kind == "jobs":
            from .prepared_reference_access import authorized_preparation

            return item.actor_id == self.actor.id or authorized_preparation(p, self.actor, item)
        if kind == "sets":
            return identifier == self.set_id
        if kind == "clips":
            return bool(item.set_id == self.set_id)
        if kind in {"candidates", "reference_plans"}:
            return self.allowed("clips", item.clip_id)
        if kind == "proposals":
            return bool(item.set_id == self.set_id)
        if kind == "assets":
            return identifier in self.asset_ids
        if kind == "story_events":
            return identifier in self.event_ids
        if kind == "states":
            return item.asset_id in self.asset_ids
        if kind == "references":
            state = p.states[item.state_id]
            return self.allowed("states", state.id) and (
                identifier in state.selected_revisions.values()
                or item.selection is not None
                or any(
                    approval.clip_id in p.sets[str(self.set_id)].clip_ids
                    for approval in item.region_approvals
                )
            )
        if kind == "media":
            if item.owner_actor_id == self.actor.id or identifier in self.published_media:
                return True
            if item.source_candidate_id and self.allowed("candidates", item.source_candidate_id):
                return True
            if item.source_revision_id and self.allowed("references", item.source_revision_id):
                return True
            # Explicitly selected global references remain visible even with older metadata.
            return any(
                r.media_id == identifier and self.allowed("references", r.id)
                for asset_id in self.asset_ids
                for state in select_entities(p.states, parent_id=asset_id)
                for r in [
                    *select_entities(p.references, ids=list(state.selected_revisions.values())),
                    *(
                        r
                        for r in select_entities(
                            p.references, parent_id=state.id, statuses=["rejected"]
                        )
                        if r.region_approvals
                    ),
                ]
            )
        return False

    def require(self, kind: str, identifier: str) -> None:
        if not self.allowed(kind, identifier):
            raise StudioError(
                "此记录不在本片场的可读范围；跨片场信息请由总导演发布适用的剧情记忆或素材",
                403,
            )

    def target(self, identifier: str) -> None:
        if not self.isolated:
            return
        if identifier == "global":
            if self.analyst:
                raise StudioError("看图子对话只读取指定任务包", 403)
            return  # Only scoped global context is returned below.
        for kind in CATEGORIES.values():
            if kind == "memory_entries":
                if memory_record(self.project, identifier):
                    self.require(kind, identifier)
                    return
            elif identifier in getattr(self.project, kind, {}):
                self.require(kind, identifier)
                return
        raise StudioError("记录不存在或不在本片场的可读范围", 404)

    def scope(self, scope: str) -> None:
        if self.analyst or self.delegate:
            if scope == "overview":
                return
            kind, _, identifier = scope.partition(":")
            self.require(CATEGORIES.get(kind, kind), identifier)
            return
        if not self.isolated or scope in {"overview", "screenplay", "memory:global"}:
            return
        kind, _, identifier = scope.partition(":")
        if kind == "memory":
            self.require("sets", identifier)
        elif kind == "entity":
            self.target(identifier)
        elif kind in CATEGORIES:
            self.require(CATEGORIES[kind], identifier)
        else:
            raise StudioError("此详情不在本片场的可读范围", 403)

    def payload(self, value: dict[str, Any]) -> dict[str, Any]:
        """Filter related projections as well as their requested root entity."""
        if not self.isolated:
            return value
        result = dict(value)
        for kind in set(CATEGORIES.values()):
            if isinstance(result.get(kind), dict):
                result[kind] = {k: v for k, v in result[kind].items() if self.allowed(kind, k)}
        if self.analyst or self.delegate:
            result.pop("global_memory", None)
            result.pop("screenplay", None)
            result.pop("brief", None)
        elif "global_memory" in result:
            memory = dict(result["global_memory"])
            memory["story"] = [
                e for e in memory.get("story", []) if self.allowed("memory_entries", e["id"])
            ]
            memory["asset_state_ids"] = [
                i for i in memory.get("asset_state_ids", []) if self.allowed("states", i)
            ]
            result["global_memory"] = memory
        result["export_manifest"] = {}
        result["final_media_id"] = None
        result["issues"] = []
        return result

    def records(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not self.isolated:
            return records
        types = {
            "set": "sets",
            "clip_plan": "clips",
            "asset": "assets",
            "state": "states",
            "selected_reference": "references",
            "review_reference": "references",
            "media": "media",
            "story_event": "story_events",
            "proposal": "proposals",
            "review_candidate": "candidates",
            "quality_candidate": "candidates",
            "final_reference_plan": "reference_plans",
        }
        return [
            r
            for r in records
            if (
                self.allowed(types[r["type"]], r["value"]["id"])
                if r["type"] in types
                else self.memory_allowed(MemoryEntry.model_validate(r["value"]), r["scope"])
                if r["type"] == "memory"
                else self.allowed("clips", r["clip_id"])
                if r["type"] == "previous_selected_actual"
                else True
            )
        ]

    def body(self, store: StudioStore, body_ref: str) -> None:
        if not self.isolated:
            return
        with store.database.session() as session:
            if body_ref.startswith("live:") and not body_ref.startswith("live:snapshot:"):
                item = session.scalar(
                    select(StudioTranscriptRow).where(
                        StudioTranscriptRow.project_id == self.project.id,
                        StudioTranscriptRow.item_id == body_ref.removeprefix("live:"),
                    )
                )
                permitted = item is not None and item.actor_id == self.actor.id
            else:
                digest = body_ref.removeprefix("live:snapshot:")
                body = session.get(StudioBodyRow, (self.project.id, digest))
                permitted = body is not None and self.actor.id in (body.owner_actor_ids or [])
        if not permitted:
            raise StudioError("此正文不在本片场的可读范围", 403)

    def sources(self, value: Any, *, field: str = "") -> None:
        """Prevent guessed source IDs from laundering private records into jobs/memory."""
        if not self.isolated:
            return
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {"read_token", "context_token", "idempotency_key"}:
                    continue
                if key == "reference_roles" and isinstance(child, dict):
                    for identifier in child:
                        self.require("references", identifier)
                self.sources(child, field=key)
        elif isinstance(value, list):
            for child in value:
                self.sources(child, field=field)
        elif (
            isinstance(value, str)
            and len(value) <= 256
            and (field == "id" or field.endswith(("_id", "_ids")))
        ):
            for kind in set(CATEGORIES.values()) - {"memory_entries"}:
                if value in getattr(self.project, kind, {}):
                    self.require(kind, value)
            record = memory_record(self.project, value)
            if record and not self.memory_allowed(record[1], record[0]):
                self.require("memory_entries", value)
