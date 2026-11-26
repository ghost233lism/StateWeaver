"""Indexed studio repositories, atomic scoped commands and durable cursor events."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
import traceback
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar

from sqlalchemy import and_, case, delete, func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased

from stateweaver.persistence.database import Database
from stateweaver.persistence.models import (
    StudioBindingRow,
    StudioBodyRow,
    StudioCommandRow,
    StudioDisplayRow,
    StudioEntityRow,
    StudioEventRow,
    StudioInspectionRow,
    StudioProjectRow,
    StudioRoutingSampleRow,
    StudioTextChunkRow,
    StudioTranscriptRow,
)
from stateweaver.studio.contracts import (
    BodyChunk,
    CommandResult,
    ContextPhase,
    EventBatch,
    ProjectList,
    ProjectSummary,
    StudioEvent,
    StudioProject,
    TranscriptItem,
    TranscriptPage,
    now,
)
from stateweaver.studio.plan_index import affected_ids, replace_index
from stateweaver.studio.projection_data import _project_payload, activity_data, transcript_data
from stateweaver.studio.repository import (
    COLLECTIONS,
    HISTORY_IDS,
    MODELS,
    EntityMap,
    MemoryEntries,
    MemoryStateIds,
    RelatedIds,
    RepositoryProject,
    core_payload,
    entity_payload,
    parent,
    public_actor_payload,
    query_rows,
)

__all__ = ["StudioError", "StudioStore", "transcript_data", "_project_payload"]


def encoded(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class StudioError(ValueError):
    def __init__(self, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.status = status


class StudioRevisionConflict(StudioError):
    """The command's expected project revision is no longer current."""


class StudioPrerequisiteError(StudioError):
    """A rejected command with explicitly constructed, project-scoped recovery details."""

    def __init__(self, message: str, *, details: dict[str, Any], recovery: str) -> None:
        super().__init__(message)
        self.details = details
        self.recovery = recovery


class StudioContextRestartRequired(StudioError):
    """An interrupted context read whose validated target can be read again safely."""

    def __init__(self, message: str, *, phase: ContextPhase, target_id: str) -> None:
        super().__init__(message)
        self.phase = phase
        self.target_id = target_id


class StudioTransactionRolledBack(StudioError):
    """Raised only after the local mutation transaction has actually rolled back."""

    def __init__(self, diagnostic_id: str | None = None) -> None:
        super().__init__("本地事务已回滚，未创建新的生成任务")
        self.diagnostic_id = diagnostic_id


ReadT = TypeVar("ReadT")


class StudioStore:
    def __init__(self, database: Database) -> None:
        self.database = database
        from .project_covers import ProjectCoverCache
        from .runtime_metrics import RuntimeMetrics

        self._project_cover_cache = ProjectCoverCache()
        self.metrics = RuntimeMetrics(database)
        db_path = database.engine.url.database
        self._temporary: tempfile.TemporaryDirectory[str] | None = None
        if not db_path or db_path == ":memory:":
            self._temporary = tempfile.TemporaryDirectory(prefix="studio-bodies-")
            self.body_root = Path(self._temporary.name)
        else:
            self.body_root = Path(db_path).absolute().parent / "studio-bodies"
        self.body_root.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def _write_transaction(self, diagnostic: dict[str, Any] | None = None) -> Iterator[Session]:
        try:
            with self.database.write_lock, self.database.session() as session:
                yield session
        except IntegrityError as error:
            raise StudioTransactionRolledBack() from error
        except TypeError as error:
            # The session context has rolled back before we publish diagnostics.
            # This also covers validators invoked by invalidation or flush.
            import platform

            import pydantic
            import pydantic_core

            diagnostic_id, _ = self.prepare_body(
                {
                    **(diagnostic or {}),
                    "kind": "studio_mutation_type_error",
                    "created_at": now(),
                    "python": platform.python_version(),
                    "pydantic": pydantic.__version__,
                    "pydantic_core": pydantic_core.__version__,
                    "stack": [
                        {"file": Path(f.filename).name, "line": f.lineno, "function": f.name}
                        for f in traceback.extract_tb(error.__traceback__)
                    ],
                }
            )
            raise StudioTransactionRolledBack(diagnostic_id) from error

    @contextmanager
    def _session(self, existing: Session | None = None) -> Iterator[Session]:
        if existing is not None:
            yield existing
        else:
            with self.database.session() as session:
                yield session

    def prepare_body(self, value: Any) -> tuple[str, int]:
        """Publish immutable bytes before taking a writer lock when already known.

        This does not register a body or make a transcript visible. The guarded
        mutation still owns registration, receipts and replay events atomically.
        A failed/lost-lease mutation can leave an unreferenced immutable file.
        """
        raw = encoded(value).encode()
        digest = hashlib.sha256(raw).hexdigest()
        destination = self.body_root / digest[:2] / digest
        if not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(dir=destination.parent)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(name, destination)
            finally:
                if os.path.exists(name):
                    os.unlink(name)
        return digest, len(raw)

    def _body(
        self, project_id: str, value: Any, session: Session, *, actor_id: str | None = None
    ) -> str:
        digest, size = self.prepare_body(value)
        body_row = session.get(StudioBodyRow, (project_id, digest))
        if body_row is None:
            body_row = StudioBodyRow(
                project_id=project_id,
                sha256=digest,
                size=size,
                owner_actor_ids=[actor_id] if actor_id else [],
            )
            session.add(body_row)
            session.flush([body_row])
        elif actor_id and actor_id not in body_row.owner_actor_ids:
            body_row.owner_actor_ids = [*body_row.owner_actor_ids, actor_id]
        return digest

    def _read_body(self, digest: str) -> dict[str, Any]:
        raw = (self.body_root / digest[:2] / digest).read_bytes()
        if hashlib.sha256(raw).hexdigest() != digest:
            raise StudioError("历史正文完整性校验失败", 500)
        return dict(json.loads(raw))

    def body(self, project_id: str, digest: str, offset: int = 0, limit: int = 65536) -> BodyChunk:
        from stateweaver.studio.history_bodies import read_archive, read_live

        if digest.startswith("live:"):
            return read_live(self, project_id, digest.removeprefix("live:"), offset, limit)
        return read_archive(self, project_id, digest, offset, limit)

    def _project(
        self,
        row: StudioProjectRow,
        session: Session | None = None,
        *,
        cache_missing_quarantines: bool = False,
    ) -> RepositoryProject:
        project = RepositoryProject.model_validate(deepcopy(row.payload))
        maps: dict[str, EntityMap[Any]] = {}
        snapshot = session.get_transaction() if session and cache_missing_quarantines else None
        quarantine_misses: set[str] = set()
        if snapshot is not None:
            object.__setattr__(project, "_snapshot_quarantine_misses", quarantine_misses)

        def query(kind: str, **filters: Any) -> list[Any]:
            with self._session(session) as connection:
                ids = filters.get("ids")
                missing_id = (
                    ids[0]
                    if isinstance(ids, list) and len(ids) == 1 and isinstance(ids[0], str)
                    else None
                )
                cacheable = (
                    kind == "image_quarantines"
                    and missing_id is not None
                    and set(filters) <= {"ids", "keys_only"}
                    and snapshot is not None
                    and snapshot.is_active
                    and connection.get_transaction() is snapshot
                    and project.__dict__.get("_snapshot_quarantine_misses") is quarantine_misses
                )
                # Absence is stable only in this explicit read/write snapshot.
                # EntityMap merges locally added quarantines before querying;
                # every cache hit still records the exact negative dependency below.
                rows = (
                    []
                    if cacheable and missing_id in quarantine_misses
                    else query_rows(connection, project.id, kind, **filters)
                )
                if cacheable and not rows and missing_id is not None:
                    quarantine_misses.add(missing_id)
                if project.__dict__.get("_track_reads") and (
                    kind not in {"context_reads", "transcript", "actors"}
                    or kind == "transcript"
                    and filters.get("subtype") == "user"
                ):
                    signature = (
                        list(rows)
                        if filters.get("keys_only")
                        else [
                            [r.item_id if kind == "transcript" else r.id, r.version] for r in rows
                        ]
                    )
                    project.__dict__.setdefault("_read_queries", {})[encoded([kind, filters])] = {
                        "kind": kind,
                        "filters": filters,
                        "signature": signature,
                    }
                    if not filters.get("keys_only"):
                        project.__dict__.setdefault("_tracked_read_ids", set()).update(
                            (kind, r.item_id if kind == "transcript" else r.id) for r in rows
                        )
                return rows

        def read_hook(kind: str, entity: Any) -> None:
            if not project.__dict__.get("_track_reads") or kind in {
                "context_reads",
                "transcript",
                "actors",
            }:
                return
            identifier = entity.id
            seen = project.__dict__.setdefault("_tracked_read_ids", set())
            if (kind, identifier) in seen:
                return
            seen.add((kind, identifier))
            filters = {"ids": [identifier]}
            project.__dict__.setdefault("_read_queries", {})[encoded([kind, filters])] = {
                "kind": kind,
                "filters": filters,
                "signature": [[identifier, entity.version]],
            }

        def versions(kind: str, filters: dict[str, Any]) -> list[Any]:
            with self._session(session) as connection:
                return query_rows(
                    connection,
                    project.id,
                    kind,
                    **{**filters, "versions_only": not filters.get("keys_only", False)},
                )

        def affected_plans(keys: set[str]) -> list[str]:
            with self._session(session) as connection:
                return affected_ids(connection, project.id, keys)

        def job_query_outages(identifiers: Sequence[str]) -> dict[str, bool]:
            if not identifiers:
                return {}
            with self._session(session) as connection:
                return {
                    identifier: bool(outage)
                    for identifier, outage in connection.execute(
                        select(
                            StudioEntityRow.id,
                            StudioEntityRow.payload["query_outage_since"].as_string(),
                        ).where(
                            StudioEntityRow.project_id == project.id,
                            StudioEntityRow.kind == "jobs",
                            StudioEntityRow.id.in_(identifiers),
                        )
                    )
                }

        def hydrate(kind: str, entity: Any) -> Any:
            payload = deepcopy(entity.payload)
            if kind in {"actors", "jobs"}:
                payload.update(lease_owner=entity.lease_owner, lease_until=entity.lease_until)
            if kind == "transcript":
                if payload.get("body_ref"):
                    body = self._read_body(payload["body_ref"])
                    payload.update(body)
                with self._session(session) as connection:
                    chunks = list(
                        connection.scalars(
                            select(StudioTextChunkRow.text)
                            .where(
                                StudioTextChunkRow.project_id == project.id,
                                StudioTextChunkRow.item_id == entity.item_id,
                            )
                            .order_by(StudioTextChunkRow.ordinal)
                        )
                    )
                if chunks:
                    payload["text"] = payload.get("text", "") + "".join(chunks)
                payload["data"] = transcript_data(payload.get("data", {}))
                payload.update(sequence=entity.sequence, version=entity.version)
            value: Any = MODELS[kind].model_validate(payload)
            if kind in HISTORY_IDS:
                field, child_kind = HISTORY_IDS[kind]
                object.__setattr__(value, field, RelatedIds(maps[child_kind], value.id))
            if kind == "sets":
                object.__setattr__(
                    value.memory, "story", MemoryEntries(maps["memory_entries"], value.id)
                )
                object.__setattr__(
                    value.memory, "asset_state_ids", MemoryStateIds(maps["memory_states"], value.id)
                )
            if kind in {"memory_entries", "memory_states"}:
                maps[kind].parents[value.id] = entity.parent_id
            return value

        for kind in MODELS:
            maps[kind] = EntityMap(kind, project.id, query, hydrate, read_hook=read_hook)
            if kind in COLLECTIONS:
                object.__setattr__(project, kind, maps[kind])
        object.__setattr__(
            project.global_memory, "story", MemoryEntries(maps["memory_entries"], "global")
        )
        object.__setattr__(
            project.global_memory,
            "asset_state_ids",
            MemoryStateIds(maps["memory_states"], "global"),
        )
        object.__setattr__(project, "_repositories", maps)
        object.__setattr__(project, "_query_versions", versions)
        object.__setattr__(project, "_query_job_outages", job_query_outages)
        object.__setattr__(project, "_affected_reference_plans", affected_plans)
        object.__setattr__(
            project,
            "_context_core_original",
            [
                project.screenplay.model_dump(mode="json"),
                project.catalog_frozen_at,
                project.config.model_dump(),
                project.handoff_policy,
            ],
        )
        return project

    def read(self, project_id: str, operation: Callable[[StudioProject], ReadT]) -> ReadT:
        """Build a scoped read in one SQLite snapshot, outside the write lock."""
        with self.database.session() as session:
            session.execute(text("BEGIN"))
            row = session.get(StudioProjectRow, project_id)
            if row is None:
                raise StudioError("项目不存在", 404)
            return operation(self._project(row, session, cache_missing_quarantines=True))

    def _replay_creation(
        self, session: Session, project_id: str, fingerprint: str
    ) -> StudioProject | None:
        row = session.get(StudioProjectRow, project_id)
        if row is None:
            return None
        receipt = session.get(StudioCommandRow, (project_id, "project:creation"))
        if receipt is None or receipt.fingerprint != fingerprint:
            raise StudioError("创建项目的幂等键已用于其他请求")
        if row.payload.get("deleted_at"):
            raise StudioError("项目已删除", 410)
        return self._project(row)

    def replay_creation(self, project_id: str, fingerprint: str) -> StudioProject | None:
        with self.database.session() as session:
            return self._replay_creation(session, project_id, fingerprint)

    def create(
        self, project: StudioProject, *, creation_fingerprint: str | None = None
    ) -> StudioProject:
        with self._write_transaction() as session:
            if creation_fingerprint is not None:
                session.execute(text("BEGIN IMMEDIATE"))
                if existing := self._replay_creation(session, project.id, creation_fingerprint):
                    return existing
            row = StudioProjectRow(
                id=project.id,
                revision=project.revision,
                payload=core_payload(project),
                summary={},
            )
            session.add(row)
            session.flush()
            scoped = self._project(row, session)
            for kind in COLLECTIONS:
                getattr(scoped, kind).update(getattr(project, kind))
            for entry in project.global_memory.story:
                scoped.global_memory.story.append(entry)
            scoped.global_memory.asset_state_ids.extend(project.global_memory.asset_state_ids)
            cursor = self._flush(scoped, row, session, "project_created", None, {})
            if creation_fingerprint is not None:
                session.add(
                    StudioCommandRow(
                        project_id=project.id,
                        key="project:creation",
                        fingerprint=creation_fingerprint,
                        cursor=cursor,
                    )
                )
        return self.get(project.id)

    def get(self, project_id: str) -> StudioProject:
        with self.database.session() as session:
            row = session.get(StudioProjectRow, project_id)
            if row is None:
                raise StudioError("项目不存在", 404)
            return self._project(row)

    def require_project(self, project_id: str) -> None:
        with self.database.session() as session:
            summary = session.scalar(
                select(StudioProjectRow.summary).where(StudioProjectRow.id == project_id)
            )
            if summary is None:
                raise StudioError("项目不存在", 404)
            if summary.get("deleted_at"):
                raise StudioError("项目已删除", 410)

    def list(self) -> list[StudioProject]:
        """Explicit diagnostic enumeration; runtime/API use indexed selectors."""
        with self.database.session() as session:
            return [self._project(row) for row in session.scalars(select(StudioProjectRow))]

    def projects(self, after: str | None = None, limit: int = 50) -> ProjectList:
        limit = min(max(limit, 1), 200)
        rank = case(
            (
                StudioProjectRow.summary["status"]
                .as_string()
                .in_(["preparing", "producing", "blocked"]),
                0,
            ),
            (StudioProjectRow.summary["status"].as_string() == "screenwriting", 1),
            else_=2,
        )
        with self.database.session() as session:
            # The cache stamps and source selections must describe one snapshot.
            session.execute(text("BEGIN"))
            stmt = (
                select(
                    StudioProjectRow.id,
                    StudioProjectRow.summary,
                    StudioProjectRow.updated_at,
                    rank.label("priority"),
                )
                .where(StudioProjectRow.summary["deleted_at"].as_string().is_(None))
                .order_by(rank, StudioProjectRow.updated_at.desc(), StudioProjectRow.id)
            )
            if after:
                try:
                    if not after.startswith("projects-v2:"):
                        raise ValueError
                    priority, updated, identifier = json.loads(base64.urlsafe_b64decode(after[12:]))
                    stamp = datetime.fromisoformat(updated)
                    if priority not in {0, 1, 2} or not isinstance(identifier, str):
                        raise ValueError
                except (ValueError, TypeError, KeyError):
                    raise StudioError("项目列表游标无效，请刷新列表", 422) from None
                stmt = stmt.where(
                    or_(
                        rank > priority,
                        and_(rank == priority, StudioProjectRow.updated_at < stamp),
                        and_(
                            rank == priority,
                            StudioProjectRow.updated_at == stamp,
                            StudioProjectRow.id > identifier,
                        ),
                    )
                )
            rows = list(session.execute(stmt.limit(limit + 1)))
            more = len(rows) > limit
            rows = rows[:limit]
            covers = self._project_cover_cache.get(
                session,
                {r.id: (int(r.summary.get("revision", 0)), r.updated_at) for r in rows},
            )
            return ProjectList(
                projects=[
                    ProjectSummary.model_validate({**r.summary, "preview_media": covers.get(r.id)})
                    for r in rows
                ],
                next_cursor=(
                    "projects-v2:"
                    + base64.urlsafe_b64encode(
                        json.dumps(
                            [rows[-1].priority, rows[-1].updated_at.isoformat(), rows[-1].id]
                        ).encode()
                    ).decode()
                    if rows and more
                    else None
                ),
                has_more=more,
            )

    def mutate(
        self,
        project_id: str,
        operation: Callable[[StudioProject], dict[str, Any] | None],
        *,
        expected_revision: int | None = None,
        key: str | None = None,
        fingerprint_data: Any = None,
        actor_id: str | None = None,
        kind: str = "project_updated",
        advance_revision: bool = True,
    ) -> StudioProject:
        fingerprint = hashlib.sha256(encoded(fingerprint_data).encode()).hexdigest()
        diagnostic: dict[str, Any] = {
            "project_id": project_id,
            "actor_id": actor_id,
            "operation_kind": kind,
        }
        with self._write_transaction(diagnostic) as session:
            session.execute(text("BEGIN IMMEDIATE"))
            row = session.get(StudioProjectRow, project_id)
            if row is None:
                raise StudioError("项目不存在", 404)
            receipt = session.get(StudioCommandRow, (project_id, key)) if key else None
            if receipt:
                if receipt.fingerprint != fingerprint:
                    raise StudioError("幂等键已用于其他操作")
                result = self._project(row)
                object.__setattr__(result, "_command_cursor", receipt.cursor or 0)
                return result
            if expected_revision is not None and row.revision != expected_revision:
                raise StudioRevisionConflict(f"版本冲突，请重新读取项目；当前版本为 {row.revision}")
            project = self._project(row, session, cache_missing_quarantines=True)
            diagnostic["project_revision"] = row.revision
            detail = operation(project) or {}
            from stateweaver.studio.context_cache import context_changes

            core_changed, context_updates = context_changes(project)
            if core_changed or context_updates:
                object.__setattr__(project, "_context_changes", (core_changed, context_updates))
                from stateweaver.studio.preparations import (
                    invalidate_preparations,
                    promote_preparations,
                )
                from stateweaver.studio.reference_plans import invalidate_reference_plans
                from stateweaver.studio.set_handoffs import notify_changed_sources

                notify_changed_sources(project)
                invalidate_reference_plans(project)
                invalidate_preparations(project)
                promote_preparations(project)
                if project.director_mode == "delegated-v1" and any(
                    name
                    in {
                        "states",
                        "references",
                        "candidates",
                        "clips",
                        "sets",
                        "memory_entries",
                        "story_events",
                        "delegate_tasks",
                    }
                    for name, _, _, _, _ in context_updates
                ):
                    from .delegation import refresh_blockers

                    refresh_blockers(project)
            if advance_revision:
                project.revision += 1
            project.updated_at = now()
            cursor = self._flush(project, row, session, kind, actor_id, detail)
            if key:
                session.add(
                    StudioCommandRow(
                        project_id=project_id, key=key, fingerprint=fingerprint, cursor=cursor
                    )
                )
            result = self._project(row)
            object.__setattr__(result, "_command_cursor", cursor)
        return result

    @staticmethod
    def display_entity(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        result = dict(payload)
        if kind == "actors":
            result["progress_markers"] = []
            result["recovery_history"] = []
            result.pop("sessions", None)
        if kind == "sets":
            result["memory"] = {**result.get("memory", {}), "story": [], "asset_state_ids": []}
            result["duration_history"] = []
            result["dependency_history"] = []
            result["handoff_history"] = []
        if kind == "clips":
            result["candidate_ids"] = []
            result.pop("preparation_history", None)
            if preparation := result.get("preparation"):
                # Display/event projections need the draft and actionable status,
                # not copies of the inputs used to validate that draft. The full
                # Clip detail and authoritative store retain these snapshots.
                result["preparation"] = {
                    key: value
                    for key, value in preparation.items()
                    if key not in {"stable_snapshot", "continuity_snapshot", "context_dependencies"}
                }
        if kind == "assets":
            result["state_ids"] = []
        if kind == "states":
            result["revision_ids"] = []
            result["preparation_history"] = []
            result["evolution_history"] = []
        if kind == "candidates":
            result["prompt"] = ""
            result.pop("review_batches", None)
            result["end_state_history"] = []
            result["analysis"] = {
                k: v
                for k, v in result.get("analysis", {}).items()
                if k in {"user_selection", "quality_passed"}
            }
        if kind == "references":
            result["prompt"] = ""
        if kind == "jobs":
            result["request"], result["result"] = {}, {}
        if kind == "reference_plans":
            result["prompt"] = ""
            for field in ("reference_inputs", "video_inputs", "audio_inputs"):
                result[field] = []
        return result

    def _flush(
        self,
        project: RepositoryProject,
        row: StudioProjectRow,
        session: Session,
        kind: str,
        actor_id: str | None,
        detail: dict[str, Any],
    ) -> int:
        # The ORM flush can make new rows visible inside this same transaction.
        # Keep cache reuse confined to the pre-flush snapshot/EntityMap overlay.
        project.__dict__.pop("_snapshot_quarantine_misses", None)
        maps: dict[str, EntityMap[Any]] = project.__dict__["_repositories"]
        changes: dict[str, dict[str, Any]] = {}
        versions: dict[str, int] = {}
        removed: dict[str, list[str]] = {}
        invalidated: set[str] = set()
        from .state_references import reference_tree_changes

        old_core = row.payload
        counts = {
            key: int(row.summary.get(key, 0))
            for key in ("set_count", "clip_count", "accepted_clip_count")
        }

        paused_changed = "paused" not in row.summary
        blocked_changed = "status" not in row.summary

        def summary_change(
            name: str, before: dict[str, Any] | None, after: dict[str, Any] | None
        ) -> None:
            nonlocal paused_changed, blocked_changed
            previous, current = before or {}, after or {}
            presence_changed = (before is None) != (after is None)
            if name == "actors":
                live_changed = presence_changed or any(
                    previous.get(field) != current.get(field) for field in ("status", "paused")
                )
                paused_changed |= live_changed
                blocked_changed |= live_changed or any(
                    previous.get(field) != current.get(field)
                    for field in ("last_error", "role", "visual_task_id", "delegate_task_id")
                )
            elif name in {"visual_tasks", "delegate_tasks"}:
                blocked_changed |= presence_changed or any(
                    previous.get(field) != current.get(field) for field in ("status", "actor_id")
                )
            if name == "sets":
                counts["set_count"] += int(after is not None) - int(before is not None)
            if name == "clips":
                was_active = bool(before is not None and not before.get("archived_at"))
                is_active = bool(after is not None and not after.get("archived_at"))
                counts["clip_count"] += int(is_active) - int(was_active)
                counts["accepted_clip_count"] += int(
                    is_active and (after or {}).get("status") == "accepted"
                ) - int(was_active and (before or {}).get("status") == "accepted")

        # New/replaced Set memory collections are split before serializing a Set.
        for production_set in dict.values(maps["sets"]):
            if not isinstance(production_set.memory.story, MemoryEntries):
                entries = list(production_set.memory.story)
                object.__setattr__(
                    production_set.memory,
                    "story",
                    MemoryEntries(maps["memory_entries"], production_set.id),
                )
                production_set.memory.story.extend(entries)
        if not isinstance(project.global_memory.story, MemoryEntries):
            entries = list(project.global_memory.story)
            object.__setattr__(
                project.global_memory, "story", MemoryEntries(maps["memory_entries"], "global")
            )
            project.global_memory.story.extend(entries)
        memories = [("global", project.global_memory)] + [
            (production_set.id, production_set.memory)
            for production_set in dict.values(maps["sets"])
        ]
        for scope, memory in memories:
            if not isinstance(memory.asset_state_ids, MemoryStateIds):
                state_ids = memory.asset_state_ids
                indexed_ids = MemoryStateIds(maps["memory_states"], scope)
                indexed_ids.replace(state_ids)
                object.__setattr__(memory, "asset_state_ids", indexed_ids)
        for scope in maps["memory_states"].replaced_scopes:
            session.execute(
                delete(StudioEntityRow).where(
                    StudioEntityRow.project_id == project.id,
                    StudioEntityRow.kind == "memory_states",
                    StudioEntityRow.parent_id == scope,
                )
            )
            invalidated.add("memory:" + scope)
        for name, repository in maps.items():
            for entity_id in repository.deleted:
                if name in {"actors", "sets", "clips"}:
                    session.execute(
                        delete(StudioDisplayRow).where(
                            StudioDisplayRow.project_id == project.id,
                            StudioDisplayRow.kind == name,
                            StudioDisplayRow.id == entity_id,
                        )
                    )
                summary_change(name, repository.original.get(entity_id), None)
                invalidated.update(
                    reference_tree_changes(project, name, repository.original.get(entity_id), None)
                )
                if name == "reference_plans":
                    replace_index(session, project.id, entity_id, {})
                if name == "transcript":
                    session.execute(
                        delete(StudioTranscriptRow).where(
                            StudioTranscriptRow.project_id == project.id,
                            StudioTranscriptRow.item_id == entity_id,
                        )
                    )
                else:
                    session.execute(
                        delete(StudioEntityRow).where(
                            StudioEntityRow.project_id == project.id,
                            StudioEntityRow.kind == name,
                            StudioEntityRow.id == entity_id,
                        )
                    )
                if name in {"memory_entries", "memory_states"}:
                    invalidated.add("memory:" + repository.parents[entity_id])
                else:
                    removed.setdefault(name, []).append(entity_id)
            for entity_id, value in dict.items(repository):
                payload = entity_payload(name, value)
                previous = repository.original.get(entity_id)
                if payload == previous:
                    continue
                if (
                    name == "actors"
                    and previous is not None
                    and public_actor_payload(previous) == public_actor_payload(payload)
                ):
                    # Persist private role progress without changing the public
                    # actor version or invalidating a scoped context read.
                    repository.rows[entity_id].payload = payload
                    continue
                invalidated.update(
                    reference_tree_changes(
                        project, name, repository.original.get(entity_id), payload
                    )
                )
                if name == "actors":
                    previous = repository.original.get(entity_id) or {}
                    if previous.get("status") != payload.get("status"):
                        value.status_since = payload["status_since"] = now()
                    if any(
                        previous.get(key) != payload.get(key)
                        for key in (
                            ("progress_version",)
                            if project.config.production_lifecycle_policy == "coherent-v1"
                            else ("progress_version", "summary")
                        )
                    ):
                        value.last_progress_at = payload["last_progress_at"] = now()
                MODELS[name].model_validate(payload)
                if name == "jobs" and value.kind == "video" and value.status == "complete":
                    from .routing import timing_sample

                    sample = timing_sample(project.id, value)
                    if (
                        sample
                        and session.get(StudioRoutingSampleRow, (project.id, entity_id)) is None
                    ):
                        session.add(StudioRoutingSampleRow(**sample))
                if name == "reference_plans":
                    replace_index(session, project.id, entity_id, payload)
                summary_change(name, repository.original.get(entity_id), payload)
                if name == "transcript":
                    saved = self._save_transcript(
                        project.id, value, session, repository.rows.get(entity_id)
                    )
                    projected = dict(saved.payload)
                    projected.update(sequence=saved.sequence, version=saved.version)
                    changes.setdefault(name, {})[entity_id] = projected
                    versions[f"{name}:{entity_id}"] = saved.version
                    continue
                entity = repository.rows.get(entity_id)
                if entity is None:
                    entity = StudioEntityRow(
                        project_id=project.id,
                        kind=name,
                        id=entity_id,
                        version=0,
                        sort_key=getattr(value, "created_at", now()),
                    )
                    session.add(entity)
                from .context_cache import context_entity_payload

                if entity.version == 0 or context_entity_payload(
                    name, repository.original.get(entity_id)
                ) != context_entity_payload(name, payload):
                    entity.version += 1
                entity.payload = payload
                if name in {"actors", "sets", "clips"}:
                    display = session.get(StudioDisplayRow, (project.id, name, entity_id))
                    if display is None:
                        display = StudioDisplayRow(project_id=project.id, kind=name, id=entity_id)
                        session.add(display)
                    display.version = entity.version
                    display.payload = self.display_entity(name, payload)
                entity.parent_id = repository.parents.get(entity_id) or parent(value)
                entity.actor_id = getattr(value, "actor_id", None)
                entity.status = getattr(value, "status", None)
                entity.story_time = getattr(value, "story_time", None)
                entity.subtype = getattr(
                    value, "kind", getattr(value, "role", getattr(value, "view", None))
                )
                entity.paused = getattr(value, "paused", False)
                entity.due_at = (
                    getattr(value, "recovery_not_before", 0)
                    if name == "actors"
                    else getattr(value, "next_poll_at", 0)
                )
                entity.lease_owner = getattr(value, "lease_owner", None)
                entity.lease_until = getattr(value, "lease_until", 0)
                if name in {"assets", "states", "references"}:
                    invalidated.add("memory:global")
                    set_ids = repository.query("sets", keys_only=True)
                    invalidated.update("memory:" + i for i in set_ids)
                if name == "candidates":
                    old_bindings = (repository.original.get(entity_id) or {}).get("bindings", [])
                    invalidated.update("usage:" + b["revision_id"] for b in old_bindings)
                    invalidated.update("usage:" + b.revision_id for b in value.bindings)
                    session.execute(
                        delete(StudioBindingRow).where(
                            StudioBindingRow.project_id == project.id,
                            StudioBindingRow.candidate_id == entity_id,
                        )
                    )
                    # This is a reverse usage index, not the ordered input recipe.
                    # Multiple inspected crops may intentionally use one revision.
                    indexed_revisions: set[str] = set()
                    for binding in value.bindings:
                        if binding.revision_id in indexed_revisions:
                            continue
                        indexed_revisions.add(binding.revision_id)
                        session.add(
                            StudioBindingRow(
                                project_id=project.id,
                                candidate_id=entity_id,
                                **binding.model_dump(),
                            )
                        )
                if name == "context_reads":
                    continue
                if name in {"memory_entries", "memory_states"}:
                    invalidated.add("memory:" + str(entity.parent_id))
                else:
                    changes.setdefault(name, {})[entity_id] = self.display_entity(name, payload)
                    versions[f"{name}:{entity_id}"] = entity.version
                if name == "sets":
                    invalidated.update({f"set:{entity_id}", f"memory:{entity_id}"})
                if name == "reference_plans":
                    invalidated.update({f"reference-plan:{entity_id}", f"clip:{value.clip_id}"})
                if name == "story_events":
                    invalidated.update({"memory:global", f"story-event:{entity_id}"})
                if name == "actors":
                    invalidated.add(f"actor:{entity_id}")
                if name == "visual_tasks":
                    invalidated.update(
                        {
                            f"visual-task:{entity_id}",
                            f"visual-tasks:{value.parent_actor_id}",
                            f"actor:{value.parent_actor_id}",
                            f"actor:{value.actor_id}",
                        }
                    )
                if name in {"delegate_tasks", "executive_inbox"}:
                    invalidated.update(
                        {f"delegate-task:{entity_id}", f"actor:{value.parent_actor_id}"}
                    )
                if name in {"executive_documents", "executive_document_versions"}:
                    invalidated.update(
                        {f"executive-document:{entity_id}", f"actor:{value.actor_id}"}
                    )
                if name in {"clips", "candidates", "references"}:
                    singular = {
                        "clips": "clip",
                        "candidates": "candidate",
                        "references": "reference",
                    }[name]
                    invalidated.add(f"{singular}:{entity_id}")
                    if name == "clips":
                        invalidated.add(f"set:{value.set_id}")
                    if name == "candidates":
                        invalidated.add(f"clip:{value.clip_id}")
                        clip = project.clips.get(value.clip_id)
                        if clip:
                            invalidated.add(f"set:{clip.set_id}")
        core = core_payload(project)
        if (
            kind == "role_checkpoint"
            and not (changes or removed or invalidated or detail)
            and {**core, "updated_at": old_core.get("updated_at")} == old_core
        ):
            return self.cursor(project.id, session)
        blocked_changed |= old_core.get("status") != core.get("status")
        row.payload, row.revision = core, project.revision
        row.updated_at = datetime.fromisoformat(project.updated_at)
        core_changes = {key: value for key, value in core.items() if old_core.get(key) != value}
        if "status" in core_changes:
            invalidated.add("asset-trees")
        if "screenplay" in core_changes:
            invalidated.add("screenplay")
            core_changes["screenplay"] = {
                **core_changes["screenplay"],
                "text": "",
                "facts": [],
                "manuscript": None,
            }
        if "global_memory" in core_changes:
            invalidated.add("memory:global")
        if "final_media_id" in core_changes or "export_manifest" in core_changes:
            invalidated.add("final")
        session.flush()
        # Counts change with touched entities, never by rescanning historical Clips.
        paused = bool(row.summary.get("paused", False))
        if paused_changed:
            live = (
                session.scalar(
                    select(func.count())
                    .select_from(StudioEntityRow)
                    .where(
                        StudioEntityRow.project_id == project.id,
                        StudioEntityRow.kind == "actors",
                        StudioEntityRow.status != "complete",
                        StudioEntityRow.paused.is_(False),
                    )
                )
                or 0
            )
            paused = not live
        # Runtime transcript/context bookkeeping does not change these predicates.
        # Reuse the last committed status; real actor/task edits still re-evaluate
        # the original closed-child rules in this same business transaction.
        summary_status = project.status if blocked_changed else row.summary["status"]
        if blocked_changed and summary_status not in {"complete", "screenwriting", "failed"}:
            closed_visual_task = aliased(StudioEntityRow)
            closed_delegate_task = aliased(StudioEntityRow)
            if session.scalar(
                select(StudioEntityRow.id)
                .where(
                    StudioEntityRow.project_id == project.id,
                    StudioEntityRow.kind == "actors",
                    StudioEntityRow.status == "blocked",
                    StudioEntityRow.paused.is_(False),
                    StudioEntityRow.payload["last_error"].as_string().is_not(None),
                    # A delegate's closed failure remains visible in its history.
                    # It is no longer an active actor blocking the whole production;
                    # the executive owns whether another review/decision is needed.
                    ~and_(
                        StudioEntityRow.subtype == "visual_analyst",
                        select(closed_visual_task.id)
                        .where(
                            closed_visual_task.project_id == project.id,
                            closed_visual_task.kind == "visual_tasks",
                            closed_visual_task.id
                            == StudioEntityRow.payload["visual_task_id"].as_string(),
                            closed_visual_task.actor_id == StudioEntityRow.id,
                            closed_visual_task.status.in_(["complete", "failed"]),
                        )
                        .exists(),
                    ),
                    ~and_(
                        StudioEntityRow.subtype.in_(["task_analyst", "material_executor"]),
                        select(closed_delegate_task.id)
                        .where(
                            closed_delegate_task.project_id == project.id,
                            closed_delegate_task.kind == "delegate_tasks",
                            closed_delegate_task.id
                            == StudioEntityRow.payload["delegate_task_id"].as_string(),
                            closed_delegate_task.actor_id == StudioEntityRow.id,
                            closed_delegate_task.status.in_(["complete", "failed", "blocked"]),
                        )
                        .exists(),
                    ),
                )
                .limit(1)
            ):
                summary_status = "blocked"
        row.summary = ProjectSummary(
            id=project.id,
            revision=project.revision,
            title=project.title,
            brief=project.brief[:400],
            status=summary_status,
            deleted_at=project.deleted_at,
            workflow_version=project.workflow_version,
            updated_at=project.updated_at,
            paused=paused,
            preview_media=None,
            **counts,
        ).model_dump(mode="json", exclude={"preview_media"})
        data: dict[str, Any] = {
            "revision": project.revision,
            "core": core_changes,
            "changes": changes,
            "removed": removed,
            "versions": versions,
            "invalidated": sorted(invalidated),
        }
        # Native/business detail can be arbitrarily large: retain one separate body,
        # not an additional project copy inside each replay event.
        if detail:
            item_id = detail.get("item_id")
            data.update({k: v for k, v in detail.items() if k not in {"item"}})
            if item_id and item_id in changes.get("transcript", {}):
                data["item"] = changes["transcript"][item_id]
        event = StudioEventRow(
            project_id=project.id,
            payload={
                "actor_id": actor_id,
                "kind": kind,
                "item_id": detail.get("item_id"),
                "created_at": now(),
                "data": data,
            },
        )
        session.add(event)
        session.flush()
        return event.cursor

    def _save_transcript(
        self,
        project_id: str,
        item: TranscriptItem,
        session: Session,
        row: StudioTranscriptRow | None = None,
    ) -> StudioTranscriptRow:
        session.flush()
        if row is None:
            row = session.scalar(
                select(StudioTranscriptRow).where(
                    StudioTranscriptRow.project_id == project_id,
                    StudioTranscriptRow.item_id == item.id,
                )
            )
        if row is None:
            row = StudioTranscriptRow(
                project_id=project_id,
                item_id=item.id,
                actor_id=item.actor_id,
                kind=item.kind,
                version=0,
            )
            session.add(row)
        body_ref = self._body(
            project_id, {"text": item.text, "data": item.data}, session, actor_id=item.actor_id
        )
        if row.payload and row.payload.get("data", {}).get("source") == "mcp.read_media":
            session.execute(
                delete(StudioInspectionRow).where(
                    StudioInspectionRow.project_id == project_id,
                    StudioInspectionRow.item_id == item.id,
                )
            )
        row.actor_id, row.kind = item.actor_id, item.kind
        projected = transcript_data(item.data)
        if len(encoded(projected)) > 8192:
            projected = {
                k: projected[k] for k in ("type", "tool", "action", "name") if k in projected
            }
        activity = item.activity
        if item.kind in {"tool", "memory_change", "asset_change"}:
            current_activity = activity_data(item.text, item.status, item.data)
            if item.data.get("type") == "mcpToolCall":
                activity = {**activity, **current_activity}
                if "error" not in current_activity:
                    activity.pop("error", None)
            else:
                activity = {**current_activity, **activity}
        row.version += 1
        row.payload = {
            **item.model_dump(mode="json", exclude={"sequence", "version", "text", "data"}),
            "text": item.text[:4096],
            "text_length": len(item.text),
            "data": projected,
            "activity": activity,
            "body_ref": body_ref,
        }
        session.execute(
            delete(StudioTextChunkRow).where(
                StudioTextChunkRow.project_id == project_id, StudioTextChunkRow.item_id == item.id
            )
        )
        if item.data.get("source") == "mcp.read_media" and item.data.get("media_id"):
            media_id = str(item.data["media_id"])
            media_row = session.get(StudioEntityRow, (project_id, "media", media_id))
            source_hash = item.data.get("source_sha256") or (
                media_row.payload["sha256"] if media_row else ""
            )
            identity = (project_id, item.actor_id, media_id, source_hash)
            inspected = session.get(StudioInspectionRow, identity)
            if inspected is None:
                inspected = StudioInspectionRow(
                    project_id=project_id,
                    actor_id=item.actor_id,
                    media_id=media_id,
                    source_sha256=source_hash,
                )
                session.add(inspected)
            inspected.item_id, inspected.payload = item.id, item.data
        session.flush()
        return row

    def inspected(self, project_id: str, actor_id: str, media_ids: Sequence[str]) -> set[str]:
        excluded = aliased(StudioEntityRow)
        with self.database.session() as session:
            return set(
                session.scalars(
                    select(StudioInspectionRow.media_id)
                    .join(
                        StudioEntityRow,
                        (StudioEntityRow.project_id == StudioInspectionRow.project_id)
                        & (StudioEntityRow.kind == "media")
                        & (StudioEntityRow.id == StudioInspectionRow.media_id)
                        & (
                            StudioEntityRow.payload["sha256"].as_string()
                            == StudioInspectionRow.source_sha256
                        ),
                    )
                    .where(
                        StudioInspectionRow.project_id == project_id,
                        StudioInspectionRow.actor_id == actor_id,
                        StudioInspectionRow.media_id.in_(media_ids),
                        ~select(excluded.id)
                        .where(
                            excluded.project_id == project_id,
                            excluded.kind == "image_quarantines",
                            excluded.id == StudioEntityRow.payload["sha256"].as_string(),
                        )
                        .exists(),
                    )
                )
            )

    def actor_has_pending_image_delivery(
        self, project_id: str, actor_id: str, *, exclude_records_only: bool = False
    ) -> bool:
        """Read indexed actor/task metadata without hydrating a whole project."""
        with self.database.session() as session:
            payload = session.scalar(
                select(StudioEntityRow.payload).where(
                    StudioEntityRow.project_id == project_id,
                    StudioEntityRow.kind == "actors",
                    StudioEntityRow.id == actor_id,
                )
            )
            if not payload or not payload.get("pending_image_deliveries"):
                return False
            return not (
                exclude_records_only and self._records_only_actor(session, project_id, payload)
            )

    @staticmethod
    def _records_only_actor(session: Session, project_id: str, payload: dict[str, Any]) -> bool:
        task_id = payload.get("delegate_task_id")
        if not task_id or payload.get("role") not in {"task_analyst", "material_executor"}:
            return False
        from .delegation import records_only_authorized

        task = session.scalar(
            select(StudioEntityRow.payload).where(
                StudioEntityRow.project_id == project_id,
                StudioEntityRow.kind == "delegate_tasks",
                StudioEntityRow.id == task_id,
            )
        )
        return bool(
            task
            and records_only_authorized(
                task.get("request", {}).get("kind", ""),
                str(task.get("parent_actor_id", "")),
                task.get("version", 1),
                task.get("followups", []),
            )
        )

    def actor_records_only(self, project_id: str, actor_id: str) -> bool:
        """Check an unsafe delegate route against current indexed authorization metadata."""
        with self.database.session() as session:
            payload = session.scalar(
                select(StudioEntityRow.payload).where(
                    StudioEntityRow.project_id == project_id,
                    StudioEntityRow.kind == "actors",
                    StudioEntityRow.id == actor_id,
                )
            )
            return bool(payload and self._records_only_actor(session, project_id, payload))

    def completed_turn_time(self, project_id: str, actor_id: str) -> float | None:
        with self.database.session() as session:
            row = session.scalar(
                select(StudioEventRow)
                .where(
                    StudioEventRow.project_id == project_id,
                    StudioEventRow.payload["actor_id"].as_string() == actor_id,
                    StudioEventRow.payload["kind"].as_string() == "turn_completed",
                )
                .order_by(StudioEventRow.cursor.desc())
                .limit(1)
            )
            return datetime.fromisoformat(row.payload["created_at"]).timestamp() if row else None

    def cursor(self, project_id: str, session: Session) -> int:
        return (
            session.scalar(
                select(func.max(StudioEventRow.cursor)).where(
                    StudioEventRow.project_id == project_id
                )
            )
            or 0
        )

    def events(self, project_id: str, after: int = 0, limit: int = 200) -> EventBatch:
        limit = min(max(limit, 1), 200)
        with self.database.session() as session:
            rows = list(
                session.scalars(
                    select(StudioEventRow)
                    .where(StudioEventRow.project_id == project_id, StudioEventRow.cursor > after)
                    .order_by(StudioEventRow.cursor)
                    .limit(limit + 1)
                )
            )
            events = [
                StudioEvent(cursor=row.cursor, project_id=project_id, **row.payload)
                for row in rows[:limit]
            ]
            return EventBatch(
                events=events,
                cursor=events[-1].cursor if events else after,
                has_more=len(rows) > limit,
            )

    def command_result(self, project: StudioProject) -> CommandResult:
        cursor = getattr(project, "_command_cursor", 0)
        with self.database.session() as session:
            row = session.get(StudioEventRow, cursor)
            events = (
                [StudioEvent(cursor=cursor, project_id=project.id, **row.payload)] if row else []
            )
        return CommandResult(
            project_id=project.id, revision=project.revision, cursor=cursor, events=events
        )

    @staticmethod
    def _operation_summary(event: StudioEventRow | None) -> dict[str, Any]:
        if event is None:
            return {}
        data = event.payload.get("data", {})
        item = data.get("item", {})
        return {
            "project_id": event.project_id,
            "revision": data.get("revision"),
            "cursor": event.cursor,
            "action": data.get("action"),
            "item_id": event.payload.get("item_id") or data.get("item_id"),
            "outcome": item.get("activity", {}),
        }

    def command_receipt(self, project: StudioProject) -> dict[str, Any]:
        """A small MCP outcome, linked to the complete persisted business event."""
        with self.database.session() as session:
            event = session.get(StudioEventRow, getattr(project, "_command_cursor", 0))
            return {"state": "committed", **self._operation_summary(event)}

    def owns_actor_pause(self, project_id: str, actor_id: str, key: str) -> bool:
        """Use the original committed pause and any later explicit lifecycle actions."""
        with self.database.session() as session:
            receipt = session.get(StudioCommandRow, (project_id, "user:" + key))
            if not receipt or not receipt.cursor:
                return False
            origin = session.get(StudioEventRow, receipt.cursor)
            if origin is None or origin.project_id != project_id:
                return False
            data = origin.payload["data"]
            if data.get("action") != "pause" or not (
                data.get("changes", {}).get("actors", {}).get(actor_id, {}).get("paused")
            ):
                return False
            action = StudioEventRow.payload["data"]["action"].as_string()
            for payload in session.scalars(
                select(StudioEventRow.payload).where(
                    StudioEventRow.project_id == project_id,
                    StudioEventRow.cursor > origin.cursor,
                    action.in_(("pause", "resume", "reset_context")),
                )
            ):
                target = payload.get("actor_id")
                if target is not None:
                    if target == actor_id:
                        return False
                    continue
                actors = payload["data"].get("changes", {}).get("actors", {})
                # Before actor-scoped events, an unchanged pause could leave no
                # actor patch. Its target cannot be proven, so preserve the pause.
                if not actors or actor_id in actors:
                    return False
            return True

    def operation_receipt(self, project_id: str, actor_id: str, key: str) -> dict[str, Any]:
        with self.database.session() as session:
            receipt = session.get(StudioCommandRow, (project_id, actor_id + ":" + key))
            if receipt is None:
                return {
                    "state": "not_found",
                    "idempotency_key": key,
                    "registration": "not_committed_at_read",
                    "safe_replay": {
                        "same_actor": True,
                        "same_idempotency_key": key,
                        "same_action_and_payload": True,
                        "refresh_read_token": True,
                    },
                    "changed_payload": "若参考清单等业务参数已更新，先用原键和原参数确认旧命令被"
                    "当前版本校验明确拒绝，核对无已登记任务后再用新键提交当前方案；"
                    "禁止把新参数塞入旧键，也不能据此重发远端状态未知的任务",
                    "next_step": "本地操作未登记。刷新所需上下文后，以原角色、原幂等键、"
                    "相同操作和业务参数重放；原调用若迟到，原子回执仍只登记一次。"
                    "这只重放本地 MCP 命令，不授权直接重发服务商 POST；"
                    "已有 ambiguous 任务仍须 reconcile_job，不能换新键或重建任务",
                }
            event = session.get(StudioEventRow, receipt.cursor)
            return {
                "state": "committed",
                "idempotency_key": key,
                **self._operation_summary(event),
            }

    def transcript_page(
        self, project_id: str, actor_id: str, before: int | None = None, limit: int = 50
    ) -> TranscriptPage:
        limit = min(max(limit, 1), 200)
        with self.database.session() as session:
            session.execute(text("BEGIN"))
            stmt = select(StudioTranscriptRow).where(
                StudioTranscriptRow.project_id == project_id,
                StudioTranscriptRow.actor_id == actor_id,
            )
            if before is not None:
                stmt = stmt.where(StudioTranscriptRow.sequence < before)
            rows = list(
                session.scalars(stmt.order_by(StudioTranscriptRow.sequence.desc()).limit(limit + 1))
            )
            has_more = len(rows) > limit
            rows = rows[:limit]
            items = [
                TranscriptItem.model_validate(
                    {**row.payload, "sequence": row.sequence, "version": row.version}
                )
                for row in reversed(rows)
            ]
            for item in items:
                if item.kind == "message" and item.status == "running":
                    item.body_ref = "live:" + item.id
            return TranscriptPage(
                items=items,
                cursor=self.cursor(project_id, session),
                has_more=has_more,
                next_cursor=rows[-1].sequence if rows and has_more else None,
            )
