"""Read-only project progress and a resource/dependency-aware completion estimate.

Forecast nodes are estimates, never Clip plans. Existing readiness and quality
rules remain authoritative. Reads use display rows and bounded numeric samples;
they do not load transcripts, raw model output or all candidate histories.
"""

from __future__ import annotations

import math
import statistics
import time
from collections import Counter, OrderedDict
from collections.abc import Callable
from datetime import UTC, datetime
from threading import RLock
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from stateweaver.persistence.models import StudioEntityRow, StudioProjectRow, StudioRoutingSampleRow

from .contracts import (
    Clip,
    Job,
    PhaseTiming,
    ProductionActorStatus,
    ProductionBlocker,
    ProductionETA,
    ProductionMaterial,
    ProductionSet,
    ProductionStatus,
    StudioProject,
)
from .playback import report as playback_report
from .production_reads import clip_readiness
from .queries import StudioQueries
from .repository import MODELS, MemoryEntries, MemoryStateIds, select_entities
from .routing import calibration_samples, estimate, profile_key, supports_request, timestamp
from .settings import StudioSettings
from .store import StudioError, StudioStore
from .workflow import can_plan_set

LIVE = ["queued", "submitting", "running", "ambiguous"]
JOB_LABELS = {
    "image": "生成参考图",
    "video": "生成视频",
    "reference": "准备参考素材",
    "analysis": "准备审核证据",
    "assembly": "合成成片",
}


class ProductionMonitor:
    def __init__(
        self,
        store: StudioStore,
        settings: StudioSettings,
        routes: Callable[[], tuple[dict[str, Any], dict[str, int]]] | None = None,
    ) -> None:
        self.store, self.settings = store, settings
        self.routes = routes or (lambda: ({}, {}))
        self.queries = StudioQueries(store)
        self.lock = RLock()
        self.compute_locks = [RLock() for _ in range(16)]
        self.cache: OrderedDict[str, tuple[float, ProductionStatus]] = OrderedDict()

    def get(self, project_id: str, *, details: bool = False) -> ProductionStatus:
        with self.compute_locks[hash(project_id) % len(self.compute_locks)]:
            with self.store.database.session() as session:
                revision = session.scalar(
                    select(StudioProjectRow.revision).where(StudioProjectRow.id == project_id)
                )
            with self.lock:
                cached = self.cache.get(project_id)
            if cached and cached[1].revision == revision and time.monotonic() - cached[0] < 5:
                result = cached[1]
            else:
                result = self._compute(project_id, details=details)
                with self.lock:
                    self.cache[project_id] = time.monotonic(), result
                    self.cache.move_to_end(project_id)
                    while len(self.cache) > 32:
                        self.cache.popitem(last=False)
            if details:
                return result
            return result.model_copy(
                update={
                    "materials": [],
                    "timings": [],
                }
            )

    def _compute(self, project_id: str, *, details: bool = False) -> ProductionStatus:
        with self.store.database.session() as session:
            session.execute(text("BEGIN"))
            row = self.queries._core(session, project_id)
            p = self.store._project(row, session)
            # Only current fields are needed for planning gates. Historical
            # candidates/references remain exact, lazy indexed repositories.
            for kind in ("actors", "sets", "clips"):
                entries = self.queries._rows(session, project_id, kind)
                object.__setattr__(
                    p,
                    kind,
                    {
                        entry.id: MODELS[kind].model_validate(
                            self.store.display_entity(kind, entry.payload)
                        )
                        for entry in entries
                    },
                )
            repositories = p.__dict__["_repositories"]
            for production_set in p.sets.values():
                object.__setattr__(
                    production_set.memory,
                    "story",
                    MemoryEntries(repositories["memory_entries"], production_set.id),
                )
                object.__setattr__(
                    production_set.memory,
                    "asset_state_ids",
                    MemoryStateIds(repositories["memory_states"], production_set.id),
                )
            sets = sorted(p.sets.values(), key=lambda item: item.order)
            clips = [
                p.clips[c]
                for production_set in sets
                for c in production_set.clip_ids
                if c in p.clips and not p.clips[c].archived_at
            ]
            jobs = select_entities(p.jobs, statuses=LIVE)
            executive = next((a for a in p.actors.values() if a.role == "executive"), None)
            blockers: list[ProductionBlocker] = []
            actors: list[ProductionActorStatus] = []
            for production_set in sets:
                actor = p.actors[production_set.actor_id]
                remaining = [
                    c
                    for c in clips
                    if c.set_id == production_set.id and not c.accepted_candidate_id
                ]
                clip = remaining[0] if remaining else None
                owned = [
                    j
                    for j in jobs
                    if j.actor_id == actor.id
                    and (
                        not clip
                        or j.target_id == clip.id
                        or (
                            j.target_id in p.candidates
                            and p.candidates[j.target_id].clip_id == clip.id
                        )
                    )
                ]
                label = "片场已完成" if production_set.status == "complete" else "等待片场收尾"
                ready = None
                before = len(blockers)
                if clip:
                    try:
                        ready = clip_readiness(p, clip.id)
                        blockers.extend(
                            ProductionBlocker(
                                **item.model_dump(exclude={"proposal_ids"}),
                                set_id=production_set.id,
                                clip_id=clip.id,
                                unknown_wait=item.kind in {"technical", "quality"},
                            )
                            for item in ready.items
                            if clip.status != "review"
                            or item.kind in {"technical", "quality", "job"}
                        )
                        label = (
                            "审核当前候选"
                            if clip.status == "review"
                            else "可以提交生成"
                            if ready.can_generate
                            else "准备当前片段"
                        )
                    except StudioError as error:
                        blockers.append(
                            ProductionBlocker(
                                key=f"readiness:{clip.id}",
                                kind="state_conflict",
                                description=str(error),
                                owner_id=executive.id if executive else None,
                                owner_role="executive",
                                entity_id=clip.id,
                                set_id=production_set.id,
                                clip_id=clip.id,
                            )
                        )
                        label = "等待状态核对"
                elif production_set.status != "complete" and not production_set.clip_ids:
                    label = "可以拆分 Clip" if can_plan_set(p, production_set) else "等待片场交付"
                if owned:
                    job = sorted(owned, key=lambda j: j.created_at)[0]
                    label = "提交结果待核实" if job.status == "ambiguous" else JOB_LABELS[job.kind]
                    if job.status == "ambiguous" or job.query_outage_since:
                        blockers.append(
                            ProductionBlocker(
                                key=f"submission:{job.id}",
                                kind="submission_unknown",
                                description=(
                                    "原任务提交结果待核实，禁止重新提交"
                                    if job.status == "ambiguous"
                                    else "原任务查询故障，等待服务恢复后回收"
                                ),
                                owner_role="technical",
                                entity_id=job.id,
                                owner_id=actor.id,
                                set_id=production_set.id,
                                clip_id=clip.id if clip else None,
                                unknown_wait=True,
                            )
                        )
                if actor.paused and production_set.status != "complete":
                    label = "已暂停"
                    blockers.append(
                        ProductionBlocker(
                            key=f"paused:{actor.id}",
                            kind="paused",
                            description="片场导演已暂停",
                            owner_role="user",
                            owner_id=actor.id,
                            set_id=production_set.id,
                            unknown_wait=True,
                        )
                    )
                from .production_runtime import actor_blocks_critical_path

                if actor_blocks_critical_path(p, actor):
                    label = "等待故障处理"
                    blockers.append(
                        ProductionBlocker(
                            key=f"actor:{actor.id}",
                            kind="technical",
                            description="导演会话遇到技术故障，需核对原错误",
                            owner_role="technical",
                            owner_id=actor.id,
                            set_id=production_set.id,
                            unknown_wait=True,
                        )
                    )
                actors.append(
                    ProductionActorStatus(
                        actor_id=actor.id,
                        set_id=production_set.id,
                        clip_id=clip.id if clip else None,
                        label=label,
                        can_plan=can_plan_set(p, production_set) and not actor.paused,
                        can_generate=bool(ready and ready.can_generate and not actor.paused),
                        allowed_operations=list(ready.quality_progress.allowed_operations)
                        if ready and ready.quality_progress
                        else [],
                        blocker_keys=[b.key for b in blockers[before:]],
                    )
                )
            if not p.screenplay.confirmed_at:
                blockers.append(
                    ProductionBlocker(
                        key="screenplay",
                        kind="confirmation",
                        description="等待完成并确认剧本",
                        owner_role="user",
                        unknown_wait=True,
                    )
                )
            if executive and p.status != "complete":
                if executive.paused or executive.status == "blocked":
                    blockers.append(
                        ProductionBlocker(
                            key=f"executive:{executive.id}",
                            kind="paused" if executive.paused else "technical",
                            description="总导演已暂停" if executive.paused else "总导演会话待恢复",
                            owner_id=executive.id,
                            owner_role="user" if executive.paused else "technical",
                            unknown_wait=True,
                        )
                    )
                owned = [j for j in jobs if j.actor_id == executive.id]
                actors.append(
                    ProductionActorStatus(
                        actor_id=executive.id,
                        label=JOB_LABELS[owned[0].kind] if owned else "协调素材、交接与选片",
                        blocker_keys=[b.key for b in blockers if b.owner_id == executive.id],
                    )
                )
            materials = self._materials(
                p, clips, jobs, executive.id if executive else None, session
            )
            for material in materials:
                if material.status == "blocked":
                    blockers.append(
                        ProductionBlocker(
                            key=f"material:{material.state_id}",
                            kind="submission_unknown",
                            description=f"「{material.label}」素材提交结果待核实",
                            owner_id=material.owner_id,
                            owner_role="technical",
                            entity_id=material.state_id,
                            unknown_wait=True,
                        )
                    )
            for state in actors:
                state.blocker_keys = list(
                    dict.fromkeys(
                        [
                            *state.blocker_keys,
                            *(b.key for b in blockers if b.owner_id == state.actor_id),
                        ]
                    )
                )
            timings = self.store.metrics.summary(project_id)
            routes, capacities = self.routes()
            samples: list[dict[str, Any]] = calibration_samples(
                self.settings.routing_calibration_path
            )
            for route in routes.values():
                for sample in session.scalars(
                    select(StudioRoutingSampleRow)
                    .where(StudioRoutingSampleRow.profile_key == profile_key(route))
                    .order_by(StudioRoutingSampleRow.completed_at.desc())
                    .limit(64)
                ):
                    samples.append(
                        {
                            key: getattr(sample, key)
                            for key in (
                                "profile_key",
                                "workload_key",
                                "workload",
                                "duration",
                                "generation_seconds",
                                "collection_seconds",
                                "completed_at",
                            )
                        }
                    )
            # One small grouped query; no candidate analysis/prompt bodies.
            completed: dict[str | None, int] = {
                r[0]: r[1]
                for r in session.execute(
                    select(StudioEntityRow.parent_id, func.count())
                    .where(
                        StudioEntityRow.project_id == project_id,
                        StudioEntityRow.kind == "candidates",
                        StudioEntityRow.status.in_(["accepted", "rejected", "review"]),
                        StudioEntityRow.payload["media_id"].as_string().is_not(None),
                    )
                    .group_by(StudioEntityRow.parent_id)
                ).all()
            }
            selected_counts = [completed.get(c.id, 1) for c in clips if c.accepted_candidate_id]
            rework = (
                statistics.mean(max(0, n - 1) for n in selected_counts) if selected_counts else 0.6
            )
            external_jobs = [
                Job.model_validate(
                    {**payload, "lease_owner": lease_owner, "lease_until": lease_until}
                )
                for payload, lease_owner, lease_until in session.execute(
                    select(
                        StudioEntityRow.payload,
                        StudioEntityRow.lease_owner,
                        StudioEntityRow.lease_until,
                    )
                    .where(
                        StudioEntityRow.kind == "jobs",
                        StudioEntityRow.project_id != project_id,
                        StudioEntityRow.subtype.in_(["video", "image"]),
                        StudioEntityRow.status.in_(LIVE),
                        StudioEntityRow.paused.is_(False),
                    )
                    .order_by(StudioEntityRow.sort_key)
                    .limit(513)
                )
            ]
            external_overflow = len(external_jobs) > 512
            eta = project_estimate(
                p,
                sets,
                clips,
                jobs,
                materials,
                routes,
                capacities,
                samples,
                timings,
                min(rework, p.config.repairs_per_clip + 2),
                blockers,
                external_jobs=external_jobs[:512],
                external_overflow=external_overflow,
            )
            if p.status == "complete" and p.final_media_id:
                blockers = []
            unique = {b.key: b for b in blockers}
            from .production_diagnostics import summary as lifecycle_summary

            lifecycle = lifecycle_summary(p, session)
            return ProductionStatus(
                project_id=p.id,
                revision=p.revision,
                status=p.status,
                selected_clips=sum(bool(c.accepted_candidate_id) for c in clips),
                planned_clips=len(clips),
                planned_sets=sum(bool(s.clip_ids) for s in sets),
                total_sets=len(sets),
                eta=eta,
                playback=playback_report(p),
                blockers=sorted(unique.values(), key=lambda b: (not b.unknown_wait, b.key)),
                actors=actors,
                materials=materials,
                material_counts=dict(Counter(m.status for m in materials)),
                timings=[PhaseTiming.model_validate(t) for t in timings],
                lifecycle=lifecycle,
                elapsed_seconds=max(
                    0,
                    (
                        (timestamp(p.updated_at) or time.time())
                        if p.status == "complete"
                        else time.time()
                    )
                    - (timestamp(p.created_at) or time.time()),
                ),
                notes=[
                    "耗时统计自本版上线开始积累；没有记录的历史阶段不补造。",
                    "累计任务时间包含并发；导演运行包含 MCP，不能直接相加作为项目经过时间。",
                    "分位数取每阶段最近 512 个样本；不足 20 个样本不显示 p95。",
                ],
            )

    @staticmethod
    def _materials(
        p: StudioProject,
        clips: list[Clip],
        jobs: list[Job],
        executive_id: str | None,
        session: Session,
    ) -> list[ProductionMaterial]:
        required: dict[str, list[str]] = {}
        views: dict[str, set[str]] = {}
        covered: set[str] = set()
        needs_image: set[str] = set()
        for clip in clips:
            if clip.accepted_candidate_id:
                continue
            plan = p.reference_plans.get(str(clip.reference_plan_id))
            video_states = (
                {s for video in plan.video_inputs for s in video.get("visual_state_ids", [])}
                if plan
                else set()
            )
            regional_states = (
                {
                    str(r["state_id"])
                    for r in plan.reference_inputs
                    if r.get("state_id") and r.get("region_approval_id")
                }
                if plan
                else set()
            )
            covered.update(video_states | regional_states)
            for item in clip.asset_plan:
                required.setdefault(item.start_state_id, []).append(clip.id)
                views.setdefault(item.start_state_id, set()).update(item.required_views)
                if item.start_state_id not in video_states | regional_states:
                    needs_image.add(item.start_state_id)
            if plan:
                for binding in plan.bindings:
                    required.setdefault(binding.state_id, []).append(clip.id)
                    needs_image.add(binding.state_id)
                    revision = p.references.get(binding.revision_id)
                    if revision:
                        views.setdefault(binding.state_id, set()).add(revision.view)
        result = []
        state_fields = (
            "asset_id",
            "label",
            "selected_revisions",
            "reference_purpose",
            "preparation",
            "is_base",
        )
        entity = StudioEntityRow
        state_rows = session.execute(
            select(
                entity.id, *[entity.payload[field].label(field) for field in state_fields]
            ).where(entity.project_id == p.id, entity.kind == "states")
        ).mappings()
        selected_rows = session.execute(
            select(
                entity.id, entity.parent_id, entity.payload["view"], entity.payload["media_id"]
            ).where(
                entity.project_id == p.id, entity.kind == "references", entity.status == "selected"
            )
        ).all()
        selected_media = {r[3] for r in selected_rows if r[3]}
        media_ids = (
            set(
                session.scalars(
                    select(entity.id).where(
                        entity.project_id == p.id,
                        entity.kind == "media",
                        entity.id.in_(selected_media),
                    )
                )
            )
            if selected_media
            else set()
        )
        available = {r[0] for r in selected_rows if r[3] in media_ids}
        image_jobs: dict[str, list[Job]] = {}
        for job in jobs:
            if job.kind == "image" and job.target_id in p.references:
                image_jobs.setdefault(p.references[job.target_id].state_id, []).append(job)
        pending_views = set(
            session.scalars(
                select(entity.parent_id).where(
                    entity.project_id == p.id,
                    entity.kind == "references",
                    entity.status == "candidate",
                )
            )
        )
        base_views = {
            row[0]: row[1] or []
            for row in session.execute(
                select(entity.id, entity.payload["required_views"]).where(
                    entity.project_id == p.id, entity.kind == "assets"
                )
            )
        }
        active_clip_ids = {c.id for c in clips if not c.accepted_candidate_id}
        planned_set_ids = {c.set_id for c in clips}
        active_set_ids = {c.set_id for c in clips if not c.accepted_candidate_id} | {
            s.id for s in p.sets.values() if s.status != "complete" and s.id not in planned_set_ids
        }
        for row in state_rows:
            state = dict(row)
            identifier = row["id"]
            purpose = state.get("reference_purpose") or {}
            targets = required.get(identifier, []) + [
                s for s in purpose.get("set_ids", []) if s in active_set_ids
            ]
            targets += [c for c in purpose.get("clip_ids", []) if c in active_clip_ids]
            if state.get("is_base") and not p.catalog_frozen_at and p.status != "complete":
                targets.append("global")
                views.setdefault(identifier, set()).update(base_views.get(state["asset_id"], []))
            selected = {
                view: identifier
                for view, identifier in (state.get("selected_revisions") or {}).items()
                if identifier in available
            }
            missing = sorted(views.get(identifier, set()) - selected.keys())
            related = image_jobs.get(identifier, [])
            preparation = state.get("preparation") or {}
            source_id = preparation.get("source_set_id")
            waiting = (
                preparation.get("mode") == "observed"
                and source_id in p.sets
                and p.sets[source_id].status != "complete"
            )
            if waiting and preparation.get("source_event_id"):
                from .set_handoffs import selected_source

                _, actual = selected_source(p, source_id, preparation["source_event_id"])
                waiting = actual is None
            status: Any = (
                "ready"
                if (selected or identifier in covered - needs_image) and not missing
                else "not_required"
                if not targets
                else (
                    "blocked"
                    if any(j.status == "ambiguous" for j in related)
                    else "preparing"
                    if related
                    else "review"
                    if identifier in pending_views
                    else "waiting_source"
                    if waiting
                    else "needed"
                )
            )
            result.append(
                ProductionMaterial(
                    state_id=identifier,
                    asset_id=state["asset_id"],
                    label=state["label"],
                    status=status,
                    required_by=list(dict.fromkeys(targets)),
                    missing_views=missing,
                    source_set_id=source_id,
                    source_event_id=preparation.get("source_event_id"),
                    active_job_ids=[j.id for j in related],
                    owner_id=executive_id,
                )
            )
        return result


def project_estimate(
    p: StudioProject,
    sets: list[ProductionSet],
    clips: list[Clip],
    jobs: list[Job],
    materials: list[ProductionMaterial],
    routes: dict[str, Any],
    capacities: dict[str, int],
    samples: list[dict[str, Any]],
    timings: list[dict[str, Any]],
    rework: float,
    blockers: list[ProductionBlocker],
    *,
    external_jobs: list[Job] | None = None,
    external_overflow: bool = False,
) -> ProductionETA:
    if p.status == "complete" and p.final_media_id:
        return ProductionETA(
            status="complete", minimum_seconds=0, maximum_seconds=0, confidence="medium"
        )
    if not p.screenplay.confirmed_at:
        return ProductionETA(status="unavailable", confidence="none", uncovered=["剧本尚未确认"])
    unplanned = [s for s in sets if not s.clip_ids and s.status != "complete"]
    typical = statistics.median(c.duration for c in clips) if clips else 8.0
    planned_seconds = sum(c.duration for c in clips)
    duration = p.screenplay.manuscript.duration_estimate if p.screenplay.manuscript else None
    rough_total = (
        (duration.minimum_seconds + duration.maximum_seconds) / 2
        if duration
        else 30 * max(1, len(sets))
    )
    missing_count = len(unplanned) or int(not sets)
    unknown_clips = (
        max(
            missing_count,
            math.ceil(max(rough_total - planned_seconds, typical * missing_count) / typical),
        )
        if missing_count
        else 0
    )
    per_unknown, extra_unknown = divmod(unknown_clips, max(1, missing_count))
    per_unknown = max(1, per_unknown)
    routes = {key: route for key, route in routes.items() if capacities.get(key, 0) > 0}
    fallback = not routes
    if fallback:
        routes = {
            "unverified": {"id": "unverified", "label": "SLOW", "prior_seconds_per_second": 180}
        }
        capacities = {"unverified": 1}
    durations = {
        t["phase"]: max(1.0, t["successful_p50_seconds"])
        for t in timings
        if t.get("successful_p50_seconds") is not None
    }
    review = durations.get("review", 180.0)
    evidence = durations.get("evidence", 20.0)
    image = durations.get("image", 120.0)
    prepare = durations.get("reference", 20.0)
    assembly = durations.get("assembly", 60.0)
    # Nodes represent selection milestones, including ordinary expected rework.
    nodes: dict[str, dict[str, Any]] = {}
    first: dict[str, str] = {}
    last: dict[str, str] = {}
    for production_set in sets:
        previous = None
        group = [c for c in clips if c.set_id == production_set.id]
        for c in group:
            nodes[c.id] = {
                "set_id": production_set.id,
                "duration": c.duration,
                "deps": {previous} if previous else set(),
                "selected": bool(c.accepted_candidate_id),
                "reviewing": c.status == "review",
                "clip": c,
                "planning": 0.0,
            }
            first.setdefault(production_set.id, c.id)
            previous = c.id
        if production_set in unplanned:
            for i in range(per_unknown + int(unplanned.index(production_set) < extra_unknown)):
                key = f"estimate:{production_set.id}:{i}"
                nodes[key] = {
                    "set_id": production_set.id,
                    "duration": typical,
                    "deps": {previous} if previous else set(),
                    "selected": False,
                    "reviewing": False,
                    "clip": None,
                    "planning": 180.0 if i == 0 else 0.0,
                }
                first.setdefault(production_set.id, key)
                previous = key
        if previous:
            last[production_set.id] = previous
    if not sets:
        for i in range(unknown_clips):
            key = f"estimate:unplanned:{i}"
            nodes[key] = {
                "set_id": "unplanned",
                "duration": typical,
                "deps": {f"estimate:unplanned:{i - 1}"} if i else set(),
                "selected": False,
                "reviewing": False,
                "clip": None,
                "planning": 300.0 if not i else 0.0,
            }
    for production_set in sets:
        target = first.get(production_set.id)
        if not target:
            continue
        nodes[target]["deps"].update(last[d] for d in production_set.depends_on if d in last)
        for handoff in production_set.asset_handoffs:
            if handoff.mode != "observed" or handoff.confirmation:
                continue
            source = [
                c
                for c in clips
                if c.set_id == handoff.source_set_id
                and handoff.source_event_id in c.story_event_ids
            ]
            targets = [
                c
                for c in clips
                if c.set_id == production_set.id
                and (
                    not handoff.target_event_ids
                    or set(handoff.target_event_ids) & set(c.story_event_ids)
                )
            ]
            dependency = (
                max(source, key=lambda c: c.order).id
                if source
                else last.get(str(handoff.source_set_id))
            )
            if dependency:
                for identifier in [c.id for c in targets] or [target]:
                    nodes[identifier]["deps"].add(dependency)
    unresolved_material_sources = False
    for material in materials:
        if material.status != "waiting_source":
            continue
        sources = [
            c.id
            for c in clips
            if c.set_id == material.source_set_id
            and (not material.source_event_id or material.source_event_id in c.story_event_ids)
        ]
        if not sources and material.source_set_id in last:
            sources = [last[material.source_set_id]]
        if not sources:
            unresolved_material_sources = True
        for key, node in nodes.items():
            if key in material.required_by or node["set_id"] in material.required_by:
                # A derived image cannot be prepared before its actual source
                # selection, even when a broad Set dependency was not declared.
                node["deps"].update(s for s in sources if s != key)
    used_samples: list[int] = []
    unsupported_requests = False
    needs_generation = any(
        not node["selected"] and not node["reviewing"] for node in nodes.values()
    )
    unknown_wait = (
        any(b.unknown_wait for b in blockers)
        or (fallback and needs_generation)
        or unresolved_material_sources
        or external_overflow
    )
    computed = []
    for factor, repairs in ((0.75, rework * 0.5), (1.5, max(0.75, rework * 1.5))):
        slots = [
            (route, 0.0)
            for key, route in routes.items()
            for _ in range(max(0, capacities.get(key, 0)))
        ]

        def reserve(
            index: int, finish: float, slots: list[tuple[dict[str, Any], float]] = slots
        ) -> None:
            route, free = slots[index]
            slots[index] = route, max(free, finish)
            group = route.get("resource_group") or route["id"]
            # Same physical resource with alternate topology is not extra GPUs.
            for other, (alias, available) in enumerate(slots):
                if (
                    alias["id"] != route["id"]
                    and (alias.get("resource_group") or alias["id"]) == group
                ):
                    slots[other] = alias, max(available, finish)

        finished: dict[str, float] = {}
        remaining = dict(nodes)
        # Each Set owns one director; different Sets no longer share an artificial pool.
        reviewer_slots = {node["set_id"]: 0.0 for node in nodes.values()}
        for other in external_jobs or []:
            if other.kind == "image":
                if other.status == "ambiguous":
                    unknown_wait = True
                continue
            elapsed = max(0, time.time() - (timestamp(other.submitted_at) or time.time()))
            pinned = other.request.get("provider_route")
            possibilities = []
            for i, (route, free) in enumerate(slots):
                if pinned and other.status != "queued" and route["id"] != pinned.get("id"):
                    continue
                if route["id"] != "unverified" and not supports_request(route, other.request):
                    continue
                cost = estimate(route, other.request, samples)
                left = max(
                    30, factor * (cost["generation_seconds"] + cost["collection_seconds"]) - elapsed
                )
                possibilities.append((free + left, i))
            if possibilities:
                occupied_until, i = min(possibilities)
                reserve(i, occupied_until)
                if other.status == "ambiguous" or other.query_outage_since:
                    unknown_wait = True
        material_finish: dict[str, float] = {}
        while remaining:
            ready = [
                (key, node) for key, node in remaining.items() if node["deps"] <= finished.keys()
            ]
            if not ready:
                return ProductionETA(
                    status="unavailable",
                    confidence="none",
                    uncovered=["规划依赖有环或来源未能解析"],
                )
            key, node = min(
                ready, key=lambda pair: max((finished[d] for d in pair[1]["deps"]), default=0)
            )
            start = max((finished[d] for d in node["deps"]), default=0)
            if node["selected"]:
                finished[key] = 0.0
                del remaining[key]
                continue
            for material in materials:
                if (
                    key not in material.required_by
                    and node["set_id"] not in material.required_by
                    and "global" not in material.required_by
                ) or material.status in {"ready", "not_required"}:
                    continue
                if material.state_id not in material_finish:
                    source_ready = 0.0
                    if material.status == "waiting_source":
                        sources = [
                            c.id
                            for c in clips
                            if c.set_id == material.source_set_id
                            and (
                                not material.source_event_id
                                or material.source_event_id in c.story_event_ids
                            )
                        ]
                        source_ready = max((finished.get(c, start) for c in sources), default=start)
                    work = image * max(1, len(material.missing_views))
                    if material.status == "review":
                        work = 0
                    active_images = [
                        j for j in jobs if j.id in material.active_job_ids and j.submitted_at
                    ]
                    elapsed = max(
                        (
                            time.time() - (timestamp(j.submitted_at) or time.time())
                            for j in active_images
                        ),
                        default=0,
                    )
                    material_finish[material.state_id] = (
                        source_ready + max(0, factor * work - elapsed) + 30 * factor
                    )
                start = max(start, material_finish[material.state_id])
            if node["reviewing"]:
                reviewer = node["set_id"]
                reviewer_slots[reviewer] = max(start, reviewer_slots[reviewer]) + review * factor
                start = reviewer_slots[reviewer]
            clip = node["clip"]
            request = {
                "duration": node["duration"],
                "video_settings": {"short_edge": 768, "aspect_ratio": "16:9"},
            }
            submitted = next(
                (
                    j
                    for j in jobs
                    if j.kind == "video"
                    and clip
                    and j.target_id in p.candidates
                    and p.candidates[j.target_id].clip_id == clip.id
                ),
                None,
            )
            if clip and clip.reference_plan_id and clip.reference_plan_id in p.reference_plans:
                plan = p.reference_plans[clip.reference_plan_id]
                request.update(
                    reference_inputs=plan.reference_inputs,
                    video_inputs=plan.video_inputs,
                    audio_inputs=plan.audio_inputs,
                    previous_end_state=bool(plan.previous_candidate_id),
                )
            elif clip:
                request.update(
                    reference_inputs=[{} for _ in clip.asset_plan],
                    previous_end_state=bool(clip.order),
                    video_inputs=[{"duration": p.config.continuation_seconds}]
                    if clip.order
                    else [],
                )
            if clip and clip.order:
                request["latent_continuation"] = {
                    "mode": p.config.latent_continuation_mode,
                    "tail_frame_count": 73,
                }
                if clip.repair_plan and clip.repair_plan.mode == "edit":
                    request["latent_continuation"]["binding"] = "endpoint-only-v1"
            if clip:
                from .continuation_controls import task_packet as conditioning_packet

                conditioning = conditioning_packet(p, clip)
                control = conditioning.get("decision") if conditioning else None
                if control and control.get("enabled") is False:
                    request["latent_continuation"] = {"mode": "off"}
                    request["video_inputs"] = [
                        video
                        for video in request.get("video_inputs", [])
                        if video.get("role") != "continuation" and video.get("full_source")
                    ]
            if submitted:
                request = submitted.request
            choices = []
            for i, (route, free) in enumerate(slots):
                if route["id"] != "unverified" and not supports_request(route, request):
                    continue
                pinned = request.get("provider_route")
                if (
                    pinned
                    and submitted
                    and submitted.status != "queued"
                    and pinned.get("id") != route["id"]
                ):
                    continue
                cost = estimate(route, request, samples)
                used_samples.append(cost["sample_count"])
                generation = cost["generation_seconds"] + cost["collection_seconds"]
                first_generation = generation
                if (
                    submitted
                    and submitted.submitted_at
                    and submitted.status in {"submitting", "running"}
                ):
                    first_generation = (
                        max(
                            30.0,
                            generation * factor
                            - max(
                                0, time.time() - (timestamp(submitted.submitted_at) or time.time())
                            ),
                        )
                        / factor
                    )
                work = generation * repairs + (0 if node["reviewing"] else first_generation)
                choices.append(
                    (
                        (max(start + node["planning"] * factor, free) + work * factor)
                        if work
                        else start,
                        i,
                    )
                )
            if not choices:
                unknown_wait = True
                unsupported_requests = True
                choices = [(start + node["duration"] * 180 * factor * (1 + repairs), 0)]
            video_end, slot = min(choices)
            reserve(slot, video_end)
            reviewer = node["set_id"]
            reviewer_slots[reviewer] = max(video_end, reviewer_slots[reviewer]) + factor * (
                prepare + (evidence + review) * (repairs if node["reviewing"] else 1 + repairs)
            )
            finished[key] = reviewer_slots[reviewer]
            del remaining[key]
        unresolved_handoffs = sum(
            h.mode == "observed" and not h.confirmation
            for s in sets
            if s.status != "complete"
            for h in s.asset_handoffs
        )
        # Coordination is model work; unlike unknown human/external waits it has
        # an explicit coarse allowance until dedicated samples are available.
        assembly_job = next((j for j in jobs if j.kind == "assembly" and j.submitted_at), None)
        assembly_elapsed = (
            max(0, time.time() - (timestamp(assembly_job.submitted_at) or time.time()))
            if assembly_job
            else 0
        )
        computed.append(
            max(finished.values(), default=0)
            + max(5, assembly * factor - assembly_elapsed)
            + 90 * unresolved_handoffs * factor
        )
    low, high = sorted(computed)
    assumptions = [
        "按已知 Clip 顺序、片场依赖和实拍来源事件估算；包含素材、生成、审核、交接与合成。",
        f"计入当前其他项目的 {len(external_jobs or [])} 个图片／视频任务；不预知之后新增的任务。",
        f"参考本项目已选片统计，预计每条追加约 {rework:.2f} 次生成；"
        "未有样本时使用 0.6 次初始假设。",
        f"阶段假设：每次审核 {review:.0f} 秒、每视图素材 {image:.0f} 秒、"
        f"参考处理 {prepare:.0f} 秒、证据准备 {evidence:.0f} 秒；未规划片场先计 180 秒规划。",
        "素材选用每状态暂计 30 秒，未确认的实拍交接每项暂计 90 秒；已准备素材不重复计生成。",
        "时间区间包含每次阶段时长 0.75–1.5 倍变化和返工波动，并非统计置信区间。",
    ]
    uncovered = []
    if missing_count:
        uncovered.append(
            f"{missing_count} 个片场尚未完成 Clip 规划，暂按约 {unknown_clips} 条估算；"
            "分段与依赖仍不确定"
        )
    if fallback and needs_generation:
        uncovered.append("生成服务容量暂未验证；按恢复后单路保守速度粗估")
    if unsupported_requests:
        uncovered.append("部分请求缺少可用且兼容的生成路线；解除服务阻塞后按保守速度粗估")
    if unresolved_material_sources:
        uncovered.append("部分待派生素材尚未能定位来源 Clip，实际等待时间未确定")
    if external_overflow:
        uncovered.append("其他项目的活动任务超过 512 个，队列未完整覆盖，需队列缩小后重新估算")
    if needs_generation and not any(used_samples):
        uncovered.append("没有匹配当前部署的视频速度样本，使用路由初始假设")
    if not timings:
        uncovered.append("历史 Harness 阶段缺少计时，未纳入虚构的历史耗时")
    needed_phases = {"assembly"}
    if any(not node["selected"] for node in nodes.values()):
        needed_phases.update({"review", "evidence", "reference"})
    if any(m.status not in {"ready", "not_required", "review"} for m in materials):
        needed_phases.add("image")
    missing_phases = needed_phases - durations.keys()
    if missing_phases:
        labels = {
            "assembly": "合成",
            "review": "自审",
            "evidence": "证据准备",
            "reference": "参考处理",
            "image": "图片生成",
        }
        uncovered.append(
            "尚缺成功耗时样本，采用初始假设："
            + "、".join(labels[k] for k in sorted(missing_phases))
        )
    if unresolved_handoffs:
        uncovered.append("实拍交接复核尚无独立速度样本，采用明确的协调耗时假设")
    return ProductionETA(
        status="conditional" if unknown_wait else "estimated",
        minimum_seconds=round(low),
        maximum_seconds=round(high),
        confidence="low" if uncovered else "medium",
        earliest_at=None
        if unknown_wait
        else datetime.fromtimestamp(time.time() + low, UTC).isoformat(),
        latest_at=None
        if unknown_wait
        else datetime.fromtimestamp(time.time() + high, UTC).isoformat(),
        assumptions=assumptions,
        uncovered=uncovered,
        unplanned_sets=missing_count,
        estimated_unplanned_clips=unknown_clips,
        rework_per_clip=rework,
        sample_counts={
            "video": max(used_samples, default=0),
            **{t["phase"]: t["count"] for t in timings},
        },
    )
