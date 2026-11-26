"""Bounded read projections and keyset history, independent of domain transactions."""

from __future__ import annotations

import base64
import json
from typing import Any

from sqlalchemy import and_, case, func, or_, select, text, tuple_
from sqlalchemy.orm import Session

from stateweaver.persistence.models import (
    StudioBindingRow,
    StudioDisplayRow,
    StudioEntityRow,
    StudioProjectRow,
    StudioTranscriptRow,
)
from stateweaver.studio.assembly import current_final_media_id
from stateweaver.studio.asset_covers import asset_cover_states
from stateweaver.studio.contracts import (
    EntityLabelBatch,
    EntityPage,
    Media,
    StudioProject,
    StudioSnapshot,
)
from stateweaver.studio.entity_labels import read_entity_labels
from stateweaver.studio.memory_reads import effective_memory_condition
from stateweaver.studio.repository import COLLECTIONS, public_actor_payload
from stateweaver.studio.state_references import asset_state_tree
from stateweaver.studio.store import StudioError, StudioStore

_PRIVATE_PROVIDER_FIELDS = frozenset({"base_url", "credential_ref", "shared_media_root"})


def _public_job_payload(value: Any) -> Any:
    """Leave provider identities and receipts visible while withholding deployment bindings."""
    if isinstance(value, dict):
        return {
            key: _public_job_payload(item)
            for key, item in value.items()
            if key not in _PRIVATE_PROVIDER_FIELDS
        }
    if isinstance(value, list):
        return [_public_job_payload(item) for item in value]
    return value


def _analysis_media_ids(analysis: dict[str, Any]) -> set[str]:
    """Keep all named evidence pages in one scoped report, including older formats."""
    identifiers = {
        analysis[name]
        for name in ("sampled_frames_media_id", "seam_media_id", "boundary_comparison_media_id")
        if isinstance(analysis.get(name), str) and analysis[name]
    }
    for name in (
        "required_review_media_ids",
        "sampled_frames_media_ids",
        "previous_tail_media_ids",
        "repair_source_media_ids",
    ):
        values = analysis.get(name)
        if isinstance(values, list):
            identifiers.update(value for value in values if isinstance(value, str) and value)
    return identifiers


def page_cursor(row: StudioEntityRow, ordinal: int) -> str:
    return base64.urlsafe_b64encode(json.dumps([row.sort_key, row.id, ordinal]).encode()).decode()


def cursor_key(value: str) -> tuple[str, str, int]:
    try:
        parts = json.loads(base64.urlsafe_b64decode(value))
        if (
            not isinstance(parts, list)
            or len(parts) != 3
            or not all(isinstance(v, str) for v in parts[:2])
            or not isinstance(parts[2], int)
            or parts[2] < 0
        ):
            raise ValueError
        return parts[0], parts[1], parts[2]
    except (ValueError, TypeError) as exc:
        raise StudioError("分页游标无效", 422) from exc


class StudioQueries:
    def __init__(self, store: StudioStore) -> None:
        self.store = store

    def media(self, project_id: str, media_id: str) -> Media:
        with self.store.database.session() as session:
            payload = session.scalar(
                select(StudioEntityRow.payload).where(
                    StudioEntityRow.project_id == project_id,
                    StudioEntityRow.kind == "media",
                    StudioEntityRow.id == media_id,
                )
            )
            if payload is None:
                raise StudioError("素材不存在", 404)
            return Media.model_validate(payload)

    def entity_labels(self, project_id: str, ids: list[str]) -> EntityLabelBatch:
        if not 1 <= len(ids) <= 100 or any(
            not identifier or len(identifier) > 256 for identifier in ids
        ):
            raise StudioError("每次可查询 1–100 条记录的名称", 422)
        with self.store.database.session() as session:
            session.execute(text("BEGIN"))
            row = self._core(session, project_id)
            return read_entity_labels(session, project_id, ids, row.payload)

    def _core(self, session: Session, project_id: str) -> StudioProjectRow:
        row = session.get(StudioProjectRow, project_id)
        if row is None:
            raise StudioError("项目不存在", 404)
        return row

    @staticmethod
    def _rows(
        session: Session,
        project_id: str,
        kind: str,
        *,
        ids: list[str] | None = None,
        parent_id: str | None = None,
        limit: int | None = None,
        status: str | None = None,
        roles: list[str] | None = None,
        full: bool = False,
    ) -> list[StudioEntityRow]:
        stmt = select(StudioEntityRow).where(
            StudioEntityRow.project_id == project_id, StudioEntityRow.kind == kind
        )
        if ids is not None:
            if not ids:
                return []
            stmt = stmt.where(StudioEntityRow.id.in_(ids))
        if parent_id is not None:
            stmt = stmt.where(StudioEntityRow.parent_id == parent_id)
        if roles is not None:
            stmt = stmt.where(
                or_(
                    StudioEntityRow.subtype.in_(roles),
                    and_(
                        StudioEntityRow.subtype.is_(None),
                        StudioEntityRow.payload["role"].as_string().in_(roles),
                    ),
                )
            )
        if status is not None:
            stmt = stmt.where(
                effective_memory_condition()
                if kind == "memory_entries" and status == "effective"
                else StudioEntityRow.status == status
            )
        stmt = stmt.order_by(StudioEntityRow.sort_key.desc(), StudioEntityRow.id.desc())
        if limit is not None:
            stmt = stmt.limit(limit)
        if not full and kind in {"actors", "sets", "clips"}:
            # Select the small view in SQL, before the ORM can parse the history.
            # An absent/stale view safely falls back to the authoritative record.
            read_stmt = stmt.outerjoin(
                StudioDisplayRow,
                and_(
                    StudioDisplayRow.project_id == StudioEntityRow.project_id,
                    StudioDisplayRow.kind == StudioEntityRow.kind,
                    StudioDisplayRow.id == StudioEntityRow.id,
                    StudioDisplayRow.version == StudioEntityRow.version,
                ),
            ).with_only_columns(
                StudioEntityRow.project_id,
                StudioEntityRow.kind,
                StudioEntityRow.id,
                StudioEntityRow.parent_id,
                StudioEntityRow.actor_id,
                StudioEntityRow.status,
                StudioEntityRow.sort_key,
                StudioEntityRow.story_time,
                StudioEntityRow.version,
                case(
                    (StudioDisplayRow.id.is_not(None), StudioDisplayRow.payload),
                    else_=StudioEntityRow.payload,
                ).label("payload"),
            )
            # Detached read rows cannot pollute the ORM identity map with a
            # partial payload that a later full detail query expects to be exact.
            return [StudioEntityRow(**dict(row._mapping)) for row in session.execute(read_stmt)]
        return list(session.scalars(stmt))

    def snapshot(
        self, project_id: str, scope: str = "overview", *, compact: bool = False
    ) -> StudioSnapshot:
        requested_scope = scope
        with self.store.database.session() as session:
            session.execute(text("BEGIN"))
            row = self._core(session, project_id)
            payload: dict[str, Any] = {**row.payload, **{name: {} for name in COLLECTIONS}}
            payload.pop("api_config_snapshot", None)
            payload["screenplay"] = {
                **row.payload.get("screenplay", {}),
                "text": "",
                "facts": [],
                "manuscript": None,
            }
            payload["global_memory"] = {
                **row.payload.get("global_memory", {}),
                "story": [],
                "asset_state_ids": [],
            }
            payload["export_manifest"] = {}
            if (
                payload.get("final_media_id")
                and payload.get("status") != "complete"
                and "segments" in row.payload.get("export_manifest", {})
            ):
                timeline_project = self.store._project(row, session)
                payload["final_media_id"] = current_final_media_id(timeline_project)
            versions: dict[str, int] = {}
            tree_projection = None

            def add(kind: str, rows: list[StudioEntityRow], full: bool = False) -> None:
                for entity in rows:
                    item = (
                        dict(entity.payload)
                        if full
                        else self.store.display_entity(kind, entity.payload)
                    )
                    if kind == "actors":
                        item = public_actor_payload(item)
                    elif kind == "jobs":
                        item = _public_job_payload(item)
                    payload[kind][entity.id] = item
                    versions[f"{kind}:{entity.id}"] = entity.version
                    if kind == "actors" and not payload[kind][entity.id].get("last_progress_at"):
                        latest = session.scalar(
                            select(StudioTranscriptRow)
                            .where(
                                StudioTranscriptRow.project_id == project_id,
                                StudioTranscriptRow.actor_id == entity.id,
                                StudioTranscriptRow.kind.in_(
                                    ("message", "memory_change", "asset_change", "status")
                                ),
                            )
                            .order_by(StudioTranscriptRow.sequence.desc())
                            .limit(1)
                        )
                        if latest:
                            payload[kind][entity.id]["last_progress_at"] = latest.payload.get(
                                "created_at"
                            )

            if scope == f"entity:{project_id}":
                scope = "memory:global"
            elif scope.startswith("entity:"):
                # Prose links may refer to history absent from the canvas. Resolve
                # one ID on demand with the composite key, never load the aggregate.
                identifier = scope.removeprefix("entity:")
                if not identifier or len(identifier) > 256:
                    raise StudioError("关联记录编号无效", 422)
                categories = {
                    "actors": "actor",
                    "sets": "set",
                    "clips": "clip",
                    "candidates": "candidate",
                    "assets": "asset",
                    "states": "state",
                    "references": "reference",
                    "media": "media",
                    "jobs": "job",
                    "memory_entries": "memory-entry",
                    "proposals": "proposal",
                    "reference_plans": "reference-plan",
                    "story_events": "story-event",
                    "visual_tasks": "visual-task",
                    "delegate_tasks": "delegate-task",
                    "executive_documents": "executive-document",
                    "executive_document_versions": "executive-document-version",
                }
                matches = list(
                    session.scalars(
                        select(StudioEntityRow)
                        .where(
                            StudioEntityRow.project_id == project_id,
                            StudioEntityRow.kind.in_(categories),
                            StudioEntityRow.id == identifier,
                        )
                        .limit(2)
                    )
                )
                transcript = session.scalar(
                    select(StudioTranscriptRow).where(
                        StudioTranscriptRow.project_id == project_id,
                        StudioTranscriptRow.item_id == identifier,
                    )
                )
                if not matches and transcript is None:
                    raise StudioError("未找到这条关联记录，可能已不在当前项目中", 404)
                if len(matches) + int(transcript is not None) != 1:
                    raise StudioError("这条编号对应多个对象，请从具体成果卡片打开", 409)
                if transcript is not None:
                    payload["transcript"][identifier] = {
                        **transcript.payload,
                        "sequence": transcript.sequence,
                        "version": transcript.version,
                    }
                    versions[f"transcript:{identifier}"] = transcript.version
                    scope = f"actor:{transcript.actor_id}"
                else:
                    matched = matches[0]
                    scope = f"{categories[matched.kind]}:{identifier}"
                    if matched.kind == "jobs":
                        add("jobs", matches, full=matched.payload["kind"] == "reference")
                        job = matched.payload
                        if job["kind"] == "reference" and job.get("result", {}).get("media_id"):
                            scope = f"media:{job['result']['media_id']}"
                        elif job["kind"] in {"image", "video", "analysis"}:
                            target = "reference" if job["kind"] == "image" else "candidate"
                            scope = f"{target}:{job['target_id']}"
                        else:
                            scope = f"actor:{job['actor_id']}"

            # Overview is proportional to the current canvas/catalog, not history.
            if scope == "overview":
                for kind in ("actors", "sets", "assets"):
                    add(
                        kind,
                        self._rows(
                            session,
                            project_id,
                            kind,
                            roles=["screenwriter", "executive", "set_director"]
                            if kind == "actors"
                            else None,
                        ),
                    )
                active = [
                    clip_id
                    for production_set in payload["sets"].values()
                    for clip_id in production_set["clip_ids"]
                ]
                add("clips", self._rows(session, project_id, "clips", ids=active))
                pending_jobs = list(
                    session.scalars(
                        select(StudioEntityRow)
                        .where(
                            StudioEntityRow.project_id == project_id,
                            StudioEntityRow.kind == "jobs",
                            StudioEntityRow.status.in_(
                                ("queued", "submitting", "running", "ambiguous")
                            ),
                        )
                        .order_by(StudioEntityRow.sort_key.desc())
                        .limit(100)
                    )
                )
                add("jobs", pending_jobs)
                add(
                    "candidates",
                    self._rows(
                        session,
                        project_id,
                        "candidates",
                        ids=[j.payload["target_id"] for j in pending_jobs],
                    ),
                )
                if row.payload.get("status") == "screenwriting":
                    payload["screenplay"] = row.payload["screenplay"]
            elif scope in {"screenplay", "final"}:
                payload["screenplay"] = row.payload["screenplay"]
                add(
                    "actors",
                    self._rows(
                        session,
                        project_id,
                        "actors",
                        roles=["screenwriter", "executive", "set_director"],
                    ),
                )
                if scope == "final" and payload.get("final_media_id"):
                    payload["export_manifest"] = row.payload.get("export_manifest", {})
                    add(
                        "media",
                        self._rows(session, project_id, "media", ids=[payload["final_media_id"]]),
                    )
            elif ":" in scope:
                category, entity_id = scope.split(":", 1)
                if category == "set":
                    sets = self._rows(session, project_id, "sets", ids=[entity_id])
                    if not sets:
                        raise StudioError("片场不存在", 404)
                    add("sets", sets)
                    add(
                        "actors",
                        self._rows(
                            session, project_id, "actors", ids=[sets[0].payload["actor_id"]]
                        ),
                    )
                    add(
                        "clips",
                        self._rows(session, project_id, "clips", ids=sets[0].payload["clip_ids"]),
                    )
                    candidate_ids = [
                        c["accepted_candidate_id"]
                        for c in payload["clips"].values()
                        if c.get("accepted_candidate_id")
                    ]
                    # Only one latest candidate for a still-unaccepted Clip.
                    for clip in payload["clips"].values():
                        if not clip.get("accepted_candidate_id"):
                            latest = self._rows(
                                session, project_id, "candidates", parent_id=clip["id"], limit=1
                            )
                            candidate_ids.extend(c.id for c in latest)
                    add(
                        "candidates",
                        self._rows(session, project_id, "candidates", ids=candidate_ids),
                    )
                elif category == "memory":
                    add("assets", self._rows(session, project_id, "assets"))
                    add("story_events", self._rows(session, project_id, "story_events"))
                    if entity_id == "global":
                        memory = {**row.payload["global_memory"], "story": []}
                        payload["global_memory"] = memory
                    else:
                        sets = self._rows(session, project_id, "sets", ids=[entity_id])
                        if not sets:
                            raise StudioError("片场记忆不存在", 404)
                        add("sets", sets)
                        add(
                            "clips",
                            self._rows(
                                session, project_id, "clips", ids=sets[0].payload["clip_ids"]
                            ),
                        )
                        memory = payload["sets"][entity_id]["memory"]
                    memory["story"] = [
                        e.payload
                        for e in reversed(
                            self._rows(
                                session,
                                project_id,
                                "memory_entries",
                                parent_id=entity_id,
                                limit=50,
                                status="effective",
                            )
                        )
                    ]
                    # Open only recent state cards; preserve memory order and duplicates.
                    memberships = self._rows(
                        session, project_id, "memory_states", parent_id=entity_id, limit=50
                    )
                    state_ids = [e.payload["state_id"] for e in reversed(memberships)]
                    states = self._rows(session, project_id, "states", ids=state_ids)
                    if entity_id == "global":
                        loaded_ids = {state.id for state in states}
                        states.extend(
                            self._rows(
                                session,
                                project_id,
                                "states",
                                ids=[
                                    identifier
                                    for identifier in asset_cover_states(session, project_id)
                                    if identifier not in loaded_ids
                                ],
                            )
                        )
                    add("states", states)
                    memory["asset_state_ids"] = state_ids
                    for state in states:
                        asset = payload["assets"].get(state.payload["asset_id"])
                        if asset is not None:
                            asset.setdefault("state_ids", []).append(state.id)
                    selected = [
                        v
                        for state in states
                        for v in state.payload.get("selected_revisions", {}).values()
                    ]
                    add("references", self._rows(session, project_id, "references", ids=selected))
                elif category == "preparation-history":
                    clips = self._rows(session, project_id, "clips", ids=[entity_id], full=True)
                    if not clips:
                        raise StudioError("片段不存在", 404)
                    add("clips", clips)
                    payload["clips"][entity_id]["preparation_history"] = [
                        {
                            key: value
                            for key, value in item.items()
                            if key
                            not in {
                                "stable_snapshot",
                                "continuity_snapshot",
                                "context_dependencies",
                            }
                        }
                        for item in clips[0].payload.get("preparation_history", [])
                    ]
                elif category == "asset-tree":
                    assets = self._rows(session, project_id, "assets", ids=[entity_id])
                    if not assets:
                        raise StudioError("资产不存在", 404)
                    add("assets", assets)
                    # Keep original ancestry, including process nodes, in the read.
                    # A separate projection determines the visible reference tree.
                    states = self._rows(session, project_id, "states", parent_id=entity_id)
                    add("states", states)
                    tree_projection = asset_state_tree(
                        session,
                        project_id,
                        entity_id,
                        states,
                        completed=row.payload.get("status") == "complete",
                    )
                    add(
                        "media",
                        self._rows(
                            session,
                            project_id,
                            "media",
                            ids=list(
                                {
                                    media_id
                                    for use in tree_projection.states.values()
                                    for media_id in use.video_media_ids
                                }
                            ),
                        ),
                    )
                    payload["assets"][entity_id]["state_ids"] = [state.id for state in states]
                    selected = [
                        identifier
                        for state in states
                        for identifier in state.payload.get("selected_revisions", {}).values()
                    ]
                    add("references", self._rows(session, project_id, "references", ids=selected))
                elif category == "memory-entry":
                    records = self._rows(session, project_id, "memory_entries", ids=[entity_id])
                    if not records:
                        raise StudioError("这条记忆记录不存在", 404)
                    entry = records[0]
                    versions[f"memory_entries:{entity_id}"] = entry.version
                    if entry.parent_id == "global":
                        payload["global_memory"] = {
                            **row.payload["global_memory"],
                            "story": [entry.payload],
                        }
                    else:
                        sets = self._rows(session, project_id, "sets", ids=[entry.parent_id or ""])
                        if not sets:
                            raise StudioError("记忆所属片场不存在", 404)
                        add("sets", sets)
                        payload["sets"][sets[0].id]["memory"]["story"] = [entry.payload]
                elif category == "proposal":
                    records = self._rows(session, project_id, "proposals", ids=[entity_id])
                    if not records:
                        raise StudioError("这条修改建议不存在", 404)
                    add("proposals", records)
                    sets = self._rows(
                        session, project_id, "sets", ids=[records[0].payload["set_id"]]
                    )
                    add("sets", sets)
                    add(
                        "actors",
                        self._rows(
                            session,
                            project_id,
                            "actors",
                            ids=[production_set.payload["actor_id"] for production_set in sets],
                        ),
                    )
                elif category in {
                    "reference-plan",
                    "story-event",
                    "visual-task",
                    "delegate-task",
                    "executive-document",
                    "executive-document-version",
                }:
                    collection = {
                        "reference-plan": "reference_plans",
                        "story-event": "story_events",
                        "visual-task": "visual_tasks",
                        "delegate-task": "delegate_tasks",
                        "executive-document": "executive_documents",
                        "executive-document-version": "executive_document_versions",
                    }[category]
                    records = self._rows(session, project_id, collection, ids=[entity_id])
                    if not records:
                        raise StudioError("记录不存在", 404)
                    add(collection, records, full=True)
                    if category == "visual-task":
                        task = records[0].payload
                        add(
                            "actors",
                            self._rows(
                                session,
                                project_id,
                                "actors",
                                ids=[task["actor_id"], task["parent_actor_id"]],
                            ),
                        )
                        for kind, field in (
                            ("candidates", "candidate_ids"),
                            ("references", "revision_ids"),
                        ):
                            add(
                                kind, self._rows(session, project_id, kind, ids=task.get(field, []))
                            )
                    if category == "reference-plan":
                        plan = records[0].payload
                        if plan.get("job_id"):
                            jobs = self._rows(session, project_id, "jobs", ids=[plan["job_id"]])
                            add("jobs", jobs)
                            add(
                                "candidates",
                                self._rows(
                                    session,
                                    project_id,
                                    "candidates",
                                    ids=[job.payload["target_id"] for job in jobs],
                                ),
                            )
                        add(
                            "clips", self._rows(session, project_id, "clips", ids=[plan["clip_id"]])
                        )
                        refs = [x["revision_id"] for x in plan.get("bindings", [])]
                        add("references", self._rows(session, project_id, "references", ids=refs))
                        add(
                            "states",
                            self._rows(
                                session,
                                project_id,
                                "states",
                                ids=[x["state_id"] for x in plan.get("bindings", [])],
                            ),
                        )
                        add(
                            "assets",
                            self._rows(
                                session,
                                project_id,
                                "assets",
                                ids=[x["asset_id"] for x in plan.get("bindings", [])],
                            ),
                        )
                        ids = [
                            x["media_id"]
                            for key in ("reference_inputs", "video_inputs", "audio_inputs")
                            for x in plan.get(key, [])
                            if x.get("media_id")
                        ]
                        if plan.get("previous_end_state"):
                            ids.append(plan["previous_end_state"]["frame_media_id"])
                        add("media", self._rows(session, project_id, "media", ids=ids))
                elif category in {
                    "clip",
                    "candidate",
                    "reference",
                    "state",
                    "media",
                    "asset",
                    "actor",
                    "job",
                }:
                    name = {
                        "clip": "clips",
                        "candidate": "candidates",
                        "reference": "references",
                        "state": "states",
                        "media": "media",
                        "asset": "assets",
                        "actor": "actors",
                        "job": "jobs",
                    }[category]
                    records = self._rows(
                        session, project_id, name, ids=[entity_id], full=not compact
                    )
                    if not records:
                        raise StudioError("制作记录不存在", 404)
                    add(name, records, full=True)
                    if category == "actor" and records[0].payload.get("set_id"):
                        add(
                            "sets",
                            self._rows(
                                session, project_id, "sets", ids=[records[0].payload["set_id"]]
                            ),
                        )
                    elif category == "clip":
                        clip = records[0].payload
                        candidate_id = clip.get("accepted_candidate_id")
                        candidates = (
                            self._rows(session, project_id, "candidates", ids=[candidate_id])
                            if candidate_id
                            else self._rows(
                                session, project_id, "candidates", parent_id=entity_id, limit=1
                            )
                        )
                        add("candidates", candidates, full=True)
                        add("sets", self._rows(session, project_id, "sets", ids=[clip["set_id"]]))
                    elif category == "candidate":
                        add(
                            "clips",
                            self._rows(
                                session, project_id, "clips", ids=[records[0].parent_id or ""]
                            ),
                        )
                    elif category == "reference":
                        add(
                            "states",
                            self._rows(
                                session, project_id, "states", ids=[records[0].parent_id or ""]
                            ),
                        )
                    elif category == "state":
                        state_payload = records[0].payload
                        add(
                            "assets",
                            self._rows(
                                session, project_id, "assets", ids=[state_payload["asset_id"]]
                            ),
                        )
                        add(
                            "references",
                            self._rows(
                                session,
                                project_id,
                                "references",
                                ids=list(state_payload.get("selected_revisions", {}).values()),
                            ),
                        )
                else:
                    raise StudioError("详情范围无效", 422)
            else:
                raise StudioError("详情范围无效", 422)
            if scope.startswith(("candidate:", "clip:")):
                bindings = [
                    b for c in payload["candidates"].values() for b in c.get("bindings", [])
                ]
                for c in payload["candidates"].values():
                    add(
                        "jobs",
                        self._rows(session, project_id, "jobs", ids=[c["job_id"]]),
                        full=True,
                    )
                plans = [
                    plan
                    for job in payload["jobs"].values()
                    for plan in job.get("request", {}).get("asset_plan", [])
                ]
                # A planned ending state may not supply an input image. Keep the exact
                # submitted plan readable without substituting a later Clip plan.
                bound_state_ids = {b["state_id"] for b in bindings}
                bound_state_ids.update(
                    plan[key]
                    for plan in plans
                    for key in ("start_state_id", "end_state_id")
                    if plan.get(key)
                )
                asset_ids = {b["asset_id"] for b in bindings}
                asset_ids.update(plan["asset_id"] for plan in plans if plan.get("asset_id"))
                asset_ids.update(
                    identifier
                    for c in payload["candidates"].values()
                    for identifier in c.get("observed_asset_states", {})
                )
                add(
                    "states",
                    self._rows(session, project_id, "states", ids=list(bound_state_ids)),
                )
                add(
                    "references",
                    self._rows(
                        session,
                        project_id,
                        "references",
                        ids=list({b["revision_id"] for b in bindings}),
                    ),
                )
                add(
                    "assets",
                    self._rows(session, project_id, "assets", ids=list(asset_ids)),
                )
            if scope.startswith("reference:"):
                add(
                    "assets",
                    self._rows(
                        session,
                        project_id,
                        "assets",
                        ids=[s["asset_id"] for s in payload["states"].values()],
                    ),
                )
            media_ids = {
                v.get("media_id")
                for kind in ("candidates", "references")
                for v in payload[kind].values()
                if v.get("media_id")
            }
            media_ids.update(
                job["result"]["media_id"]
                for job in payload["jobs"].values()
                if job.get("result", {}).get("media_id")
            )
            media_ids.update(
                voice["media_id"]
                for asset in payload["assets"].values()
                for voice in asset.get("voice_references", [])
                if voice.get("media_id")
                and (scope.startswith("asset:") or voice["id"] == asset.get("selected_voice_id"))
            )
            media_ids.update(
                identifier
                for reference in payload["references"].values()
                for identifier in reference.get("source_media_ids", [])
            )
            for job in payload["jobs"].values():
                request = job.get("request", {})
                provenance = job.get("result", {}).get("provenance", {})
                media_ids.update(
                    item["media_id"]
                    for name in ("video_inputs", "audio_inputs", "reference_inputs")
                    for item in (request.get(name, provenance.get(name, [])) or [])
                    if item.get("media_id")
                )
                # This is the submitted boundary, which may differ from the preceding
                # candidate's later observed/selected state. Never look it up by selection.
                previous_end = request.get(
                    "previous_end_state", provenance.get("previous_end_state")
                )
                if previous_end and previous_end.get("frame_media_id"):
                    media_ids.add(previous_end["frame_media_id"])
                if job.get("kind") == "analysis":
                    result = job.get("result", {})
                    media_ids.update(_analysis_media_ids(result.get("analysis", {})))
                    end_state = result.get("end_state") or {}
                    if end_state.get("frame_media_id"):
                        media_ids.add(end_state["frame_media_id"])
            for c in payload["candidates"].values():
                analysis = c.get("analysis", {})
                media_ids.update(_analysis_media_ids(analysis))
                if c.get("end_state"):
                    media_ids.add(c["end_state"]["frame_media_id"])
            add("media", self._rows(session, project_id, "media", ids=list(media_ids)))
            counts = {
                "sets": row.summary.get("set_count", 0),
                "clips": row.summary.get("clip_count", 0),
                "accepted_clips": row.summary.get("accepted_clip_count", 0),
            }
            counts["assets"] = (
                session.scalar(
                    select(func.count())
                    .select_from(StudioEntityRow)
                    .where(
                        StudioEntityRow.project_id == project_id, StudioEntityRow.kind == "assets"
                    )
                )
                or 0
            )
            if scope.startswith(("clip:", "set:", "candidate:")):
                from .quality_progress import quality_progress

                current = self.store._project(row, session)
                for identifier, clip_value in payload["clips"].items():
                    clip_value["quality_progress"] = quality_progress(
                        current, current.clips[identifier]
                    ).model_dump(mode="json")
            return StudioSnapshot(
                project=StudioProject.model_validate(payload),
                cursor=self.store.cursor(project_id, session),
                scope=requested_scope,
                entity_versions=versions,
                counts=counts,
                state_tree=tree_projection,
            )

    def memory_page(
        self,
        project_id: str,
        *,
        scope: str = "global",
        status: str = "effective",
        asset_id: str | None = None,
        clip_id: str | None = None,
        event_id: str | None = None,
        query: str = "",
        after: str | None = None,
        limit: int = 50,
    ) -> EntityPage:
        if status not in {"effective", "all", "history", "superseded"}:
            raise StudioError("记忆状态无效", 422)
        limit = max(1, min(200, limit))
        with self.store.database.session() as session:
            session.execute(text("BEGIN"))
            self._core(session, project_id)
            if scope not in {"global", "*"} and not session.get(
                StudioEntityRow, (project_id, "sets", scope)
            ):
                raise StudioError("片场记忆不存在", 404)
            stmt = select(StudioEntityRow).where(
                StudioEntityRow.project_id == project_id, StudioEntityRow.kind == "memory_entries"
            )
            if scope != "*":
                stmt = stmt.where(StudioEntityRow.parent_id == scope)
            if status != "all":
                stmt = stmt.where(
                    effective_memory_condition()
                    if status == "effective"
                    else StudioEntityRow.status == status
                )
            for field, identifier in (
                ("asset_ids", asset_id),
                ("clip_ids", clip_id),
                ("event_ids", event_id),
            ):
                if identifier:
                    members = func.json_each(
                        StudioEntityRow.payload, f"$.scope.{field}"
                    ).table_valued("value")
                    stmt = stmt.where(
                        select(members.c.value).where(members.c.value == identifier).exists()
                    )
            if query:
                stmt = stmt.where(
                    func.instr(
                        func.lower(func.json_extract(StudioEntityRow.payload, "$.text")),
                        query.lower(),
                    )
                    > 0
                )
            ordinal = 0
            if after:
                stamp, identifier, ordinal = cursor_key(after)
                stmt = stmt.where(
                    tuple_(StudioEntityRow.sort_key, StudioEntityRow.id) > (stamp, identifier)
                )
            rows = list(
                session.scalars(
                    stmt.order_by(StudioEntityRow.sort_key, StudioEntityRow.id).limit(limit + 1)
                )
            )
            more, rows = len(rows) > limit, rows[:limit]
            return EntityPage(
                items=[{**r.payload, "memory_scope": r.parent_id} for r in rows],
                next_cursor=page_cursor(rows[-1], ordinal + len(rows)) if more else None,
                has_more=more,
                cursor=self.store.cursor(project_id, session),
                entity_versions={f"memory_entries:{r.id}": r.version for r in rows},
            )

    def history(
        self, project_id: str, kind: str, parent_id: str, after: str | None = None, limit: int = 50
    ) -> EntityPage:
        if kind not in {
            "references",
            "states",
            "candidates",
            "jobs",
            "usage",
            "memory_entries",
            "reference_plans",
            "visual_tasks",
            "delegate_tasks",
            "executive_inbox",
            "executive_documents",
            "executive_document_versions",
        }:
            raise StudioError("历史类型无效", 422)
        limit = min(max(limit, 1), 200)
        with self.store.database.session() as session:
            session.execute(text("BEGIN"))
            self._core(session, project_id)
            collection = "candidates" if kind == "usage" else kind
            condition = [
                StudioEntityRow.project_id == project_id,
                StudioEntityRow.kind == collection,
            ]
            if kind == "usage":
                ids = select(StudioBindingRow.candidate_id).where(
                    StudioBindingRow.project_id == project_id,
                    StudioBindingRow.revision_id == parent_id,
                )
                condition.append(StudioEntityRow.id.in_(ids))
            else:
                condition.append(StudioEntityRow.parent_id == parent_id)
            stmt = select(StudioEntityRow).where(*condition)
            ordinal = 0
            newest_first = kind == "visual_tasks"
            if after:
                stamp, item_id, ordinal = cursor_key(after)
                position = tuple_(StudioEntityRow.sort_key, StudioEntityRow.id)
                stmt = stmt.where(
                    position < (stamp, item_id) if newest_first else position > (stamp, item_id)
                )
            ordering = (
                (StudioEntityRow.sort_key.desc(), StudioEntityRow.id.desc())
                if newest_first
                else (StudioEntityRow.sort_key, StudioEntityRow.id)
            )
            rows = list(session.scalars(stmt.order_by(*ordering).limit(limit + 1)))
            more, rows = len(rows) > limit, rows[:limit]
            related: dict[str, dict[str, Any]] = {
                "media": {},
                "clips": {},
                "states": {},
                "references": {},
            }
            versions = {f"{collection}:{r.id}": r.version for r in rows}
            ancestors: list[str] = []
            for row in rows:
                links = {
                    "media": row.payload.get("media_id"),
                    "clips": row.payload.get("clip_id"),
                    "states": row.payload.get("state_id"),
                }
                for name, value in links.items():
                    if value and value not in related[name]:
                        target = session.get(StudioEntityRow, (project_id, name, value))
                        if target is not None:
                            related[name][value] = self.store.display_entity(name, target.payload)
                            versions[f"{name}:{value}"] = target.version
                next_id = row.payload.get("parent_id") if collection == "references" else None
                while next_id and len(ancestors) < 32 and next_id not in related["references"]:
                    target = session.get(StudioEntityRow, (project_id, "references", next_id))
                    if target is None:
                        break
                    related["references"][next_id] = self.store.display_entity(
                        "references", target.payload
                    )
                    versions[f"references:{next_id}"] = target.version
                    ancestors.append(next_id)
                    next_id = target.payload.get("parent_id")
                # A paged state tree needs real ancestors, not a chronology-based
                # guess. Keep the indexed asset boundary and bound malformed chains.
                evolution = row.payload.get("evolution") or {}
                next_state = evolution.get("parent_state_id") if collection == "states" else None
                seen = {row.id}
                while next_state and next_state not in seen and len(ancestors) < 200:
                    seen.add(next_state)
                    target = session.get(StudioEntityRow, (project_id, "states", next_state))
                    if target is None or target.parent_id != parent_id:
                        break
                    related["states"][next_state] = self.store.display_entity(
                        "states", target.payload
                    )
                    versions[f"states:{next_state}"] = target.version
                    if next_state not in ancestors:
                        ancestors.append(next_state)
                    next_state = (target.payload.get("evolution") or {}).get("parent_state_id")
            selected_refs = {
                r
                for state in [*[row.payload for row in rows], *related["states"].values()]
                if collection == "states"
                for r in state.get("selected_revisions", {}).values()
            }
            for target in self._rows(session, project_id, "references", ids=list(selected_refs)):
                related["references"][target.id] = self.store.display_entity(
                    "references", target.payload
                )
                versions[f"references:{target.id}"] = target.version
            linked_media = {
                r.get("media_id") for r in related["references"].values() if r.get("media_id")
            }
            for target in self._rows(session, project_id, "media", ids=list(linked_media)):
                related["media"][target.id] = target.payload
                versions[f"media:{target.id}"] = target.version
            return EntityPage(
                items=[
                    {
                        **(
                            _public_job_payload(self.store.display_entity(collection, row.payload))
                            if collection == "jobs"
                            else self.store.display_entity(collection, row.payload)
                        ),
                        "history_ordinal": ordinal + i + 1,
                    }
                    for i, row in enumerate(rows)
                ],
                related=related,
                entity_versions=versions,
                ancestor_ids=ancestors,
                next_cursor=page_cursor(rows[-1], ordinal + len(rows)) if more and rows else None,
                has_more=more,
                cursor=self.store.cursor(project_id, session),
            )
