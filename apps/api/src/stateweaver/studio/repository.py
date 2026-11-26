"""Transaction-scoped domain repositories. Only accessed entities are hydrated.

The dict facade keeps domain commands independent of SQL, but is not an aggregate
cache: indexed selections and ID reads load individual rows, and flush validates
and writes only changed rows. Full serialization is reserved for explicit exports.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from copy import deepcopy
from typing import Any, TypeVar, cast

from pydantic import BaseModel, Field
from pydantic_core import to_jsonable_python
from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from stateweaver.persistence.models import StudioBindingRow, StudioEntityRow, StudioTranscriptRow
from stateweaver.studio.contracts import (
    Actor,
    AssetState,
    Candidate,
    Clip,
    ContextRead,
    DelegateTask,
    ExecutiveDocument,
    ExecutiveDocumentVersion,
    ExecutiveInboxItem,
    ExternalMediaReview,
    ImageQuarantine,
    Job,
    LogicalAsset,
    Media,
    MemoryEntry,
    ProductionSet,
    Proposal,
    ReferencePlan,
    ReferenceRevision,
    StoryEvent,
    StudioProject,
    TranscriptItem,
    VisualAnalysisTask,
)


class MemoryStateMembership(BaseModel):
    """An ordered occurrence, so explicit memory lists preserve duplicates too."""

    id: str
    scope: str
    state_id: str
    position: int = Field(ge=0)

    @property
    def actor_id(self) -> str:
        return self.state_id

    @property
    def created_at(self) -> str:
        return f"{self.position:020d}"


def memory_state_membership(scope: str, state_id: str, position: int) -> MemoryStateMembership:
    return MemoryStateMembership(
        id=hashlib.sha256(f"{scope}\0{position}".encode()).hexdigest(),
        scope=scope,
        state_id=state_id,
        position=position,
    )


MODELS: dict[str, type[BaseModel]] = {
    "actors": Actor,
    "sets": ProductionSet,
    "clips": Clip,
    "assets": LogicalAsset,
    "states": AssetState,
    "references": ReferenceRevision,
    "candidates": Candidate,
    "proposals": Proposal,
    "jobs": Job,
    "media": Media,
    "image_quarantines": ImageQuarantine,
    "external_reviews": ExternalMediaReview,
    "visual_tasks": VisualAnalysisTask,
    "delegate_tasks": DelegateTask,
    "executive_inbox": ExecutiveInboxItem,
    "executive_documents": ExecutiveDocument,
    "executive_document_versions": ExecutiveDocumentVersion,
    "transcript": TranscriptItem,
    "memory_entries": MemoryEntry,
    "memory_states": MemoryStateMembership,
    "story_events": StoryEvent,
    "reference_plans": ReferencePlan,
    "context_reads": ContextRead,
}
COLLECTIONS = set(MODELS) - {"memory_entries", "memory_states"}
HISTORY_IDS = {
    "assets": ("state_ids", "states"),
    "states": ("revision_ids", "references"),
    "clips": ("candidate_ids", "candidates"),
}
T = TypeVar("T", bound=BaseModel)


class EntityMap(dict[str, T]):
    def __init__(
        self,
        kind: str,
        project_id: str,
        query: Callable[..., list[Any]],
        hydrate: Callable[[str, Any], T],
        read_hook: Callable[[str, Any], None] | None = None,
    ) -> None:
        super().__init__()
        self.kind, self.project_id = kind, project_id
        self.query, self.hydrate, self.read_hook = query, hydrate, read_hook
        self.original: dict[str, dict[str, Any] | None] = {}
        self.rows: dict[str, Any] = {}
        self.deleted: set[str] = set()
        self.parents: dict[str, str] = {}
        self.replaced_scopes: set[str] = set()

    def __getitem__(self, key: str) -> T:
        if key in self.deleted:
            raise KeyError(key)
        if not dict.__contains__(self, key):
            rows = self.query(self.kind, ids=[key])
            if not rows:
                raise KeyError(key)
            self._load(rows[0])
        self._track_read(key)
        return dict.__getitem__(self, key)

    def _track_read(self, key: str) -> None:
        if self.read_hook is not None and key in self.rows:
            self.read_hook(self.kind, self.rows[key])

    def _load(self, row: Any) -> T:
        key = row.item_id if self.kind == "transcript" else row.id
        if dict.__contains__(self, key):
            return dict.__getitem__(self, key)
        value = self.hydrate(self.kind, row)
        if isinstance(value, (Actor, Job)):
            # Heartbeats update only these runtime columns, never business JSON.
            value.lease_owner = getattr(row, "lease_owner", value.lease_owner)
            value.lease_until = getattr(row, "lease_until", value.lease_until)
        dict.__setitem__(self, key, value)
        self.original[key] = entity_payload(self.kind, value)
        self.rows[key] = row
        return value

    def __setitem__(self, key: str, value: T) -> None:
        if key not in self.original:
            rows = self.query(self.kind, ids=[key])
            if rows:
                self._load(rows[0])
            else:
                self.original[key] = None
        self.deleted.discard(key)
        dict.__setitem__(self, key, value)

    def __delitem__(self, key: str) -> None:
        self[key]
        self.deleted.add(key)
        dict.__delitem__(self, key)

    def __contains__(self, key: object) -> bool:
        if not isinstance(key, str) or key in self.deleted:
            return False
        if dict.__contains__(self, key):
            self._track_read(key)
            return True
        return bool(self.query(self.kind, ids=[key], keys_only=True))

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default

    def select(self, **filters: Any) -> list[T]:
        # SQL sees pre-transaction values. Over-fetch enough to replace locally
        # changed/deleted matches, then merge, order and limit the visible state.
        sql_filters = dict(filters)
        limit = filters.get("limit")
        if limit:
            sql_filters["limit"] = limit + dict.__len__(self) + len(self.deleted)
        # Exact membership is already known for an ID selection. Loaded rows are
        # merged below (including in-transaction edits); querying them again would
        # decode their full JSON even though _load returns the same identity.
        # Missing IDs still go through SQL so read receipts track their absence.
        if "ids" in filters:
            sql_filters["ids"] = [
                key
                for key in filters["ids"]
                if not dict.__contains__(self, key) and key not in self.deleted
            ]
        rows = (
            self.query(self.kind, **sql_filters)
            if "ids" not in sql_filters or sql_filters["ids"]
            else []
        )
        values = {
            (row.item_id if self.kind == "transcript" else row.id): self._load(row)
            for row in rows
            if (row.item_id if self.kind == "transcript" else row.id) not in self.deleted
        }
        values.update(dict.items(self))
        result = [
            v
            for key, v in values.items()
            if key not in self.deleted and matches(v, filters, self.parents.get(key))
        ]

        def order(value: T) -> tuple[Any, str]:
            key = value.__dict__["id"]
            row = self.rows.get(key)
            stamp = (
                (getattr(row, "sequence", None) or 2**63)
                if self.kind == "transcript"
                else (getattr(row, "sort_key", None) or getattr(value, "created_at", "9999"))
            )
            return stamp, key

        result.sort(key=order, reverse=bool(filters.get("descending")))
        if filters.get("priority_order"):
            result.sort(key=lambda v: (-getattr(v, "priority", 0), *order(v)))
        if "story_time_max" in filters:
            result.sort(key=lambda v: getattr(v, "story_time", 0), reverse=True)
        result = result[:limit] if limit else result
        for value in result:
            self._track_read(value.__dict__["id"])
        return result

    def values(self) -> Any:
        return self.select()

    def items(self) -> Any:
        return [(value.__dict__["id"], value) for value in self.select()]

    def keys(self) -> Any:
        persisted = self.query(self.kind, keys_only=True)
        return dict.fromkeys(
            [key for key in persisted if key not in self.deleted] + list(dict.keys(self))
        ).keys()

    def __iter__(self) -> Iterator[str]:
        return iter(self.keys())

    def __len__(self) -> int:
        return len(self.keys())

    def update(self, other: Any = (), /, **kwargs: T) -> None:
        for key, value in dict(other, **kwargs).items():
            self[key] = value

    def pop(self, key: str, default: Any = None) -> Any:
        if key not in self:
            return default
        value = self[key]
        del self[key]
        return value

    def clear(self) -> None:
        for key in list(self):
            del self[key]

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, dict):
            return False
        return dict(self.items()) == dict(other.items())

    def __ne__(self, other: object) -> bool:
        return not self == other


def parent(value: BaseModel) -> str | None:
    if isinstance(value, (VisualAnalysisTask, DelegateTask, ExecutiveInboxItem)):
        return value.parent_actor_id
    if isinstance(value, ExecutiveDocumentVersion):
        return value.document_id
    if isinstance(value, ExecutiveDocument):
        return value.actor_id
    if isinstance(value, MemoryStateMembership):
        return value.scope
    for field in ("target_id", "clip_id", "state_id", "asset_id", "set_id"):
        if hasattr(value, field):
            return cast(str | None, getattr(value, field))
    return None


def matches(value: BaseModel, filters: dict[str, Any], owner: str | None = None) -> bool:
    for key, expected in filters.items():
        if key in {
            "limit",
            "keys_only",
            "after",
            "before",
            "descending",
            "unordered",
            "effective_authority",
            "priority_order",
        }:
            continue
        if key == "priority_after":
            cursor = (
                -getattr(value, "priority", 0),
                getattr(value, "created_at", "9999"),
                value.__dict__["id"],
            )
            if cursor <= tuple(expected):
                return False
            continue
        if key == "memory_context":
            scope = getattr(value, "scope", None)
            if scope and any(
                getattr(scope, field, []) and not set(getattr(scope, field)) & set(identifiers)
                for field, identifiers in expected.items()
            ):
                return False
            continue
        if key == "viewed_media_ids_any":
            if not set(getattr(value, "viewed_media_ids", [])).intersection(expected):
                return False
            continue
        actual = (
            owner or parent(value)
            if key == "parent_id"
            else getattr(value, key.removesuffix("s"), None)
            if key == "ids"
            else getattr(value, key, None)
        )
        if key == "statuses":
            actual = getattr(value, "status", None)
        if key == "subtype":
            actual = getattr(value, "kind", getattr(value, "role", getattr(value, "view", None)))
        if key in {"planned_state", "request_state"}:
            if not _state_usage_value(value, expected, request=key == "request_state"):
                return False
            continue
        if key in {"binding_revision", "binding_asset"}:
            field = "revision_id" if key == "binding_revision" else "asset_id"
            if not any(getattr(b, field) == expected for b in getattr(value, "bindings", [])):
                return False
            continue
        if key in {"planned_revision", "prepared_revision"}:
            record = getattr(value, "preparation", None) if key == "prepared_revision" else value
            if not record or not any(
                getattr(b, "revision_id", None) == expected for b in getattr(record, "bindings", [])
            ):
                return False
            continue
        if key == "story_time_max":
            if expected is not None and getattr(value, "story_time", float("inf")) > expected:
                return False
            continue
        if key == "parent_ids":
            actual = owner or parent(value)
        if key in {"ids", "statuses", "parent_ids"}:
            if actual not in expected:
                return False
        elif actual != expected:
            return False
    return True


def select_entities[T: BaseModel](values: dict[str, T], **filters: Any) -> list[T]:
    if isinstance(values, EntityMap):
        return values.select(**filters)
    return [v for v in values.values() if matches(v, filters)]


def entity_payload(kind: str, value: BaseModel) -> dict[str, Any]:
    exclude: Any = (
        {"sequence", "version", "body_ref", "text_length"} if kind == "transcript" else set()
    )
    if kind == "sets":
        exclude = {"memory": {"story", "asset_state_ids"}}
    elif kind in HISTORY_IDS:
        exclude = {HISTORY_IDS[kind][0]}
        if kind == "clips":
            exclude.add("quality_progress")
    return value.model_dump(mode="json", exclude=exclude)


def public_actor_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep private execution journals out of Studio and model read projections."""
    sessions = payload.get("sessions", {})
    if not any("role_journal" in session for session in sessions.values()):
        return payload
    return {
        **payload,
        "sessions": {
            key: {name: value for name, value in session.items() if name != "role_journal"}
            for key, session in sessions.items()
        },
    }


@contextmanager
def mutation_savepoint(project: StudioProject) -> Iterator[None]:
    """Rollback one dependent approval group before the outer transaction flush.

    Capture only hydrated rows. Rows first read inside the group restore their
    repository original; newly created rows disappear. No aggregate hydration or
    external side effects are allowed inside a business approval group.
    """
    maps = project.__dict__.get("_repositories", {})
    if not maps:
        previous = project.model_copy(deep=True)
        try:
            yield
        except Exception:
            project.__dict__.update(previous.__dict__)
            raise
        return
    core = deepcopy(core_payload(project))
    cached = {
        name: {key: entity_payload(name, value) for key, value in dict.items(values)}
        for name, values in maps.items()
    }
    metadata = {
        name: (set(values.deleted), dict(values.parents), set(values.replaced_scopes))
        for name, values in maps.items()
    }
    try:
        yield
    except Exception:
        restored_core = StudioProject.model_validate(core)
        for key, value in core.items():
            if key == "global_memory":
                for field in value:
                    object.__setattr__(
                        project.global_memory, field, getattr(restored_core.global_memory, field)
                    )
            elif key not in COLLECTIONS:
                object.__setattr__(project, key, getattr(restored_core, key))
        for name, values in maps.items():
            for key, value in list(dict.items(values)):
                payload = cached[name].get(key, values.original.get(key))
                if payload is None:
                    dict.__delitem__(values, key)
                    values.original.pop(key, None)
                    values.rows.pop(key, None)
                    continue
                restored = MODELS[name].model_validate(payload)
                adapters = {
                    field: content
                    for field, content in value.__dict__.items()
                    if isinstance(content, (RelatedIds, MemoryEntries, MemoryStateIds))
                }
                if isinstance(restored, ProductionSet):
                    restored.memory.story = value.memory.story
                    restored.memory.asset_state_ids = value.memory.asset_state_ids
                value.__dict__.update(restored.__dict__)
                value.__dict__.update(adapters)
            values.deleted, values.parents, values.replaced_scopes = metadata[name]
        raise


class RelatedIds(list[str]):
    """Historical membership derives from indexed child rows, never parent JSON."""

    def __init__(self, children: EntityMap[Any], owner: str) -> None:
        super().__init__()
        self.children, self.owner = children, owner

    def __iter__(self) -> Iterator[str]:
        persisted = self.children.query(self.children.kind, parent_id=self.owner, keys_only=True)
        extra = [
            key
            for key, value in dict.items(self.children)
            if parent(value) == self.owner and key not in persisted
        ]
        return iter([key for key in persisted + extra if key not in self.children.deleted])

    def __len__(self) -> int:
        return len(list(iter(self)))

    def __bool__(self) -> bool:
        return bool(
            self.children.query(self.children.kind, parent_id=self.owner, keys_only=True, limit=1)
        ) or any(parent(value) == self.owner for value in dict.values(self.children))

    def __contains__(self, key: object) -> bool:
        value = self.children.get(str(key))
        return value is not None and parent(value) == self.owner

    def __getitem__(self, index: Any) -> Any:
        return list(iter(self))[index]

    def append(self, value: str) -> None:
        # A command creates the child before registering membership; the FK-like
        # parent attribute is authoritative. Never copy old identifiers on append.
        if value not in self:
            raise ValueError("Child must exist in the same parent before linking history")

    def __eq__(self, other: object) -> bool:
        return list(iter(self)) == other


class MemoryStateIds(list[str]):
    """Indexed ordered memory membership; appends never expand preceding IDs."""

    def __init__(self, entries: EntityMap[MemoryStateMembership], scope: str) -> None:
        super().__init__()
        self.entries, self.scope = entries, scope
        self._next_position: int | None = None

    def _members(
        self, limit: int | None = None, descending: bool = False
    ) -> list[MemoryStateMembership]:
        if self.scope in self.entries.replaced_scopes:
            values = [v for v in dict.values(self.entries) if v.scope == self.scope]
            values.sort(key=lambda value: value.position, reverse=descending)
            return values[:limit] if limit else values
        return self.entries.select(parent_id=self.scope, limit=limit, descending=descending)

    def __iter__(self) -> Iterator[str]:
        return iter([entry.state_id for entry in self._members()])

    def __len__(self) -> int:
        if self.scope in self.entries.replaced_scopes:
            return sum(value.scope == self.scope for value in dict.values(self.entries))
        return len(self.entries.query("memory_states", parent_id=self.scope, keys_only=True)) + sum(
            value.scope == self.scope and self.entries.original[key] is None
            for key, value in dict.items(self.entries)
        )

    def __bool__(self) -> bool:
        return bool(self._members(limit=1))

    def __contains__(self, state_id: object) -> bool:
        if not isinstance(state_id, str):
            return False
        if any(v.scope == self.scope and v.state_id == state_id for v in dict.values(self.entries)):
            return True
        if self.scope in self.entries.replaced_scopes:
            return False
        return bool(
            self.entries.query(
                "memory_states",
                parent_id=self.scope,
                actor_id=state_id,
                limit=1,
                keys_only=True,
                unordered=True,
            )
        )

    def __getitem__(self, index: Any) -> Any:
        if (
            isinstance(index, slice)
            and index.start is not None
            and index.start < 0
            and index.stop is None
            and index.step in {None, 1}
        ):
            return [
                v.state_id for v in reversed(self._members(limit=-index.start, descending=True))
            ]
        if isinstance(index, int) and index < 0:
            values = self._members(limit=-index, descending=True)
            return values[-index - 1].state_id
        return list(iter(self))[index]

    def append(self, state_id: str) -> None:
        if self._next_position is None:
            latest = self._members(limit=1, descending=True)
            self._next_position = latest[0].position + 1 if latest else 0
        member = memory_state_membership(self.scope, state_id, self._next_position)
        self._next_position += 1
        # This position is new, or its scope is atomically replaced at flush.
        self.entries.original[member.id] = None
        self.entries.parents[member.id] = self.scope
        self.entries.rows.pop(member.id, None)
        self.entries.deleted.discard(member.id)
        dict.__setitem__(self.entries, member.id, member)

    def extend(self, values: Any) -> None:
        for state_id in values:
            self.append(state_id)

    def replace(self, values: list[str]) -> None:
        for key, value in list(dict.items(self.entries)):
            if value.scope == self.scope:
                dict.__delitem__(self.entries, key)
                self.entries.original.pop(key, None)
                self.entries.rows.pop(key, None)
                self.entries.parents.pop(key, None)
                self.entries.deleted.discard(key)
        self.entries.replaced_scopes.add(self.scope)
        self._next_position = 0
        self.extend(values)

    def __eq__(self, other: object) -> bool:
        return list(iter(self)) == other


class MemoryEntries(list[MemoryEntry]):
    """Appending a memory entry never loads or rewrites earlier entries."""

    def __init__(self, entries: EntityMap[MemoryEntry], scope: str) -> None:
        super().__init__()
        self.entries, self.scope = entries, scope

    def __iter__(self) -> Iterator[MemoryEntry]:
        return iter(self.entries.select(parent_id=self.scope))

    def __len__(self) -> int:
        return len(
            self.entries.query("memory_entries", parent_id=self.scope, keys_only=True)
        ) + sum(
            self.entries.parents.get(k) == self.scope and self.entries.original[k] is None
            for k in dict.keys(self.entries)
        )

    def append(self, value: MemoryEntry) -> None:
        self.entries.parents[value.__dict__["id"]] = self.scope
        self.entries[value.__dict__["id"]] = value

    def __getitem__(self, index: Any) -> Any:
        return list(iter(self))[index]

    def __eq__(self, other: object) -> bool:
        return list(iter(self)) == other

    def extend(self, values: Any) -> None:
        for value in values:
            self.append(value)


class RepositoryProject(StudioProject):
    """Explicit export hydrates collections; hot paths use individual repositories."""

    def __setattr__(self, name: str, value: Any) -> None:
        repositories = self.__dict__.get("_repositories", {})
        if name in COLLECTIONS and name in repositories:
            target = repositories[name]
            target.clear()
            target.update(value)
            return
        super().__setattr__(name, value)

    def model_dump_json(self, **kwargs: Any) -> str:
        import json

        indent = kwargs.pop("indent", None)
        return json.dumps(
            self.model_dump(mode="json", **kwargs),
            ensure_ascii=False,
            sort_keys=True,
            indent=indent,
        )

    def model_dump(self, **kwargs: Any) -> dict[str, Any]:
        # This method exists for explicit diagnostics/export and domain fixtures,
        # never for HTTP/MCP projections or persistence flush.
        exclude = kwargs.get("exclude") or set()
        data = super().model_dump(**{**kwargs, "exclude": set(COLLECTIONS) | set(exclude)})
        for name in COLLECTIONS - set(exclude):
            data[name] = {
                key: value.model_dump(mode=kwargs.get("mode", "python"))
                for key, value in getattr(self, name).items()
            }
            if name in HISTORY_IDS:
                field, _ = HISTORY_IDS[name]
                for key, value in getattr(self, name).items():
                    data[name][key][field] = list(getattr(value, field))
        if "global_memory" not in exclude:
            data["global_memory"]["asset_state_ids"] = list(self.global_memory.asset_state_ids)
            data["global_memory"]["story"] = [
                v.model_dump(mode=kwargs.get("mode", "python")) for v in self.global_memory.story
            ]
        for production_set in data.get("sets", {}).values():
            source = self.sets[production_set["id"]]
            production_set["memory"]["asset_state_ids"] = list(source.memory.asset_state_ids)
            production_set["memory"]["story"] = [
                v.model_dump(mode=kwargs.get("mode", "python")) for v in source.memory.story
            ]
        return data


def core_payload(project: StudioProject) -> dict[str, Any]:
    # Read scalar fields directly; do not invoke a model serializer on lazy maps.
    result: dict[str, Any] = {}
    for name in StudioProject.model_fields:
        if name in COLLECTIONS:
            continue
        value = getattr(project, name)
        if name == "global_memory":
            result[name] = value.model_dump(mode="json", exclude={"story", "asset_state_ids"})
        else:
            result[name] = to_jsonable_python(value)
    return result


def _state_usage_condition(state_id: str, *, request: bool) -> ColumnElement[bool]:
    """Select bound IDs only; never hydrate every recipe/job to find a state use."""
    prefix = "$.request" if request else "$"
    conditions: list[ColumnElement[bool]] = []
    for field in ("bindings", "reference_inputs", "video_inputs", "video_references"):
        members = func.json_each(StudioEntityRow.payload, f"{prefix}.{field}").table_valued("value")
        states = func.json_each(
            func.json_extract(members.c.value, "$.visual_state_ids")
        ).table_valued("value")
        conditions.append(
            select(members.c.value)
            .where(
                or_(
                    func.json_extract(members.c.value, "$.state_id") == state_id,
                    select(states.c.value).where(states.c.value == state_id).exists(),
                )
            )
            .exists()
        )
    if request:
        conditions.append(
            func.json_extract(StudioEntityRow.payload, "$.request.state_snapshot.id") == state_id
        )
    return or_(*conditions)


def _state_usage_value(value: BaseModel, state_id: str, *, request: bool) -> bool:
    payload: dict[str, Any] = (
        getattr(value, "request", {})
        if request
        else {
            "bindings": [b.model_dump() for b in getattr(value, "bindings", [])],
            "reference_inputs": getattr(value, "reference_inputs", []),
            "video_inputs": getattr(value, "video_inputs", []),
            "video_references": [b.model_dump() for b in getattr(value, "video_references", [])],
        }
    )
    return any(
        isinstance(binding, dict)
        and (binding.get("state_id") == state_id or state_id in binding.get("visual_state_ids", []))
        for field in ("bindings", "reference_inputs", "video_inputs", "video_references")
        for binding in payload.get(field, [])
    ) or (
        request
        and isinstance(payload.get("state_snapshot"), dict)
        and payload["state_snapshot"].get("id") == state_id
    )


def query_rows(session: Session, project_id: str, kind: str, **filters: Any) -> list[Any]:
    table = StudioTranscriptRow if kind == "transcript" else StudioEntityRow
    id_column = table.item_id if table is StudioTranscriptRow else StudioEntityRow.id
    stmt: Select[Any]
    if filters.get("versions_only"):
        stmt = select(id_column, table.version).where(table.project_id == project_id)
    else:
        stmt = select(id_column if filters.get("keys_only") else table).where(
            table.project_id == project_id
        )
    if table is StudioEntityRow:
        stmt = stmt.where(StudioEntityRow.kind == kind)
    for name, value in filters.items():
        if name == "priority_after":
            priority = func.coalesce(func.json_extract(StudioEntityRow.payload, "$.priority"), 0)
            rank, stamp, identifier = value
            stmt = stmt.where(
                or_(
                    priority < -rank,
                    (priority == -rank) & (StudioEntityRow.sort_key > stamp),
                    (priority == -rank)
                    & (StudioEntityRow.sort_key == stamp)
                    & (id_column > identifier),
                )
            )
        elif name == "viewed_media_ids_any":
            members = func.json_each(StudioEntityRow.payload, "$.viewed_media_ids").table_valued(
                "value"
            )
            stmt = stmt.where(select(members.c.value).where(members.c.value.in_(value)).exists())
        elif name == "effective_authority" and value:
            from stateweaver.studio.memory_reads import effective_memory_condition

            stmt = stmt.where(effective_memory_condition())
        elif name == "memory_context":
            for field, identifiers in value.items():
                if field not in {"asset_ids", "set_ids", "clip_ids", "event_ids"}:
                    raise ValueError("Unknown memory context dimension")
                path = f"$.scope.{field}"
                members = func.json_each(StudioEntityRow.payload, path).table_valued("value")
                stmt = stmt.where(
                    or_(
                        func.coalesce(func.json_array_length(StudioEntityRow.payload, path), 0)
                        == 0,
                        select(members.c.value).where(members.c.value.in_(identifiers)).exists(),
                    )
                )
        elif name in {"planned_state", "request_state"}:
            stmt = stmt.where(_state_usage_condition(value, request=name == "request_state"))
        elif name == "binding_revision" or name == "binding_asset":
            field = (
                StudioBindingRow.revision_id
                if name == "binding_revision"
                else StudioBindingRow.asset_id
            )
            stmt = stmt.where(
                StudioEntityRow.id.in_(
                    select(StudioBindingRow.candidate_id).where(
                        StudioBindingRow.project_id == project_id, field == value
                    )
                )
            )
        elif name in {"planned_revision", "prepared_revision"}:
            path = "$.preparation.bindings" if name == "prepared_revision" else "$.bindings"
            bindings = func.json_each(StudioEntityRow.payload, path).table_valued("value")
            stmt = stmt.where(
                select(bindings.c.value)
                .where(func.json_extract(bindings.c.value, "$.revision_id") == value)
                .exists()
            )
        elif name == "parent_ids":
            stmt = stmt.where(StudioEntityRow.parent_id.in_(value))
        # None retains newest-first ordering without a time ceiling. Read receipts
        # persist this explicitly, including legacy Infinity normalized to null.
        elif name == "story_time_max" and value is not None:
            stmt = stmt.where(StudioEntityRow.story_time <= value)
        elif name == "story_time":
            stmt = stmt.where(StudioEntityRow.story_time == value)
        elif name == "ids":
            stmt = stmt.where(id_column.in_(value))
        elif name == "subtype" and table is StudioTranscriptRow:
            stmt = stmt.where(StudioTranscriptRow.kind == value)
        elif name == "statuses":
            stmt = stmt.where(StudioEntityRow.status.in_(value))
        elif name in {"parent_id", "actor_id", "status", "subtype", "lease_owner"}:
            stmt = stmt.where(getattr(table, name) == value)
    if not filters.get("unordered"):
        if filters.get("priority_order"):
            stmt = stmt.order_by(func.json_extract(StudioEntityRow.payload, "$.priority").desc())
        if "story_time_max" in filters:
            stmt = stmt.order_by(StudioEntityRow.story_time.desc())
        if kind == "transcript":
            stmt = stmt.order_by(
                StudioTranscriptRow.sequence.desc()
                if filters.get("descending")
                else StudioTranscriptRow.sequence
            )
        else:
            stmt = (
                stmt.order_by(StudioEntityRow.sort_key.desc(), id_column.desc())
                if filters.get("descending")
                else stmt.order_by(StudioEntityRow.sort_key, id_column)
            )
    if filters.get("limit"):
        stmt = stmt.limit(filters["limit"])
    if filters.get("versions_only"):
        return [list(row) for row in session.execute(stmt)]
    return list(session.scalars(stmt))
