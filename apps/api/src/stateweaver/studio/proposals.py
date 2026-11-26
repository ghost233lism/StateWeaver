"""Global changes release each Clip when its own linked work is actually ready."""

from __future__ import annotations

from typing import Any, Literal, cast

from stateweaver.studio.contracts import (
    Actor,
    Clip,
    MaterialRequest,
    Proposal,
    ProposalRequirement,
    StudioProject,
    now,
)
from stateweaver.studio.memory import digest, memory_record, require
from stateweaver.studio.repository import select_entities
from stateweaver.studio.store import StudioPrerequisiteError


def requirement_ready(p: StudioProject, item: ProposalRequirement) -> bool:
    if item.status != "done" or not item.resolution.strip() or not item.evidence_ids:
        return False
    for identifier in item.evidence_ids:
        if item.kind in {"memory", "impact"}:
            record = memory_record(p, identifier)
            if not record or record[0] != "global" or record[1].status != "effective":
                return False
            if item.kind == "impact" and record[1].basis not in {"plan", "provisional", "observed"}:
                return False
        elif item.kind == "state":
            if identifier not in p.states:
                return False
        elif item.kind == "asset":
            if identifier not in p.assets:
                return False
        else:
            revision = p.references.get(identifier)
            if not revision or not revision.media_id or revision.status != "selected":
                return False
            state = p.states[revision.state_id]
            if state.selected_revisions.get(revision.view) != identifier:
                return False
    return True


def proposal_request_fingerprint(proposal: Proposal) -> str:
    """Bind administrative acknowledgment to requests and their affected Clip scope."""
    return digest(
        {
            "material_requests": [
                item.model_dump(mode="json") for item in proposal.material_requests
            ],
            "affected_clip_ids": sorted(set(proposal.affected_clip_ids)),
        }
    )


def material_requests_need_acknowledgement(p: StudioProject, proposal: Proposal) -> bool:
    """Only a successful coherent resolution acknowledges the exact request scope.

    The compact hash avoids reading proposal history during scheduling. Adding a
    new request changes the hash; historical records without a receipt stay
    actionable rather than inferring authorization from status or prose.
    """
    if not proposal.material_requests:
        return False
    return proposal.material_requests_acknowledged_hash != proposal_request_fingerprint(proposal)


def proposal_waiting_for_delegate(p: StudioProject, proposal: Proposal) -> bool:
    """Defer acknowledged obligations only with an exact requirement assignment."""
    from .delegation import progressing_tasks

    if proposal.status != "in_progress" or material_requests_need_acknowledgement(p, proposal):
        return False
    outstanding = [r for r in proposal.requirements if not requirement_ready(p, r)]
    if not outstanding or any(r.status != "pending" for r in outstanding):
        return False
    assigned = {
        identifier
        for parent in select_entities(p.actors, subtype="executive")
        for task in progressing_tasks(p, parent.id)
        if proposal.id in task.request.scope.proposal_ids
        for identifier in {*task.requirement_ids, *task.request.requirement_ids}
    }
    return all(r.id in assigned for r in outstanding)


def clip_pipeline_progressing(p: StudioProject, first: Clip) -> bool:
    """Recognize submitted work, director repairs and explicitly delegated support."""
    from .continuity import waiting_for_voices
    from .delegation import progressing_clip_tasks
    from .quality import ordinary_clip_corrections_complete
    from .runtime_decisions import source_actor_can_progress
    from .set_handoffs import dependency_report

    if not source_actor_can_progress(p, p.sets[first.set_id].actor_id):
        return False
    if dependency_report(p, first.set_id, first).blockers:
        return False
    delegated = progressing_clip_tasks(p, first.id)
    if first.status == "escalated" and not delegated:
        return False
    progressing = False
    for candidate in select_entities(p.candidates, parent_id=first.id):
        if candidate.status not in {"generating", "review"}:
            continue
        job = p.jobs.get(candidate.job_id)
        if not job or job.status in {"ambiguous", "failed"} or job.query_outage_since:
            return False
        analyses = select_entities(
            p.jobs,
            parent_id=candidate.id,
            subtype="analysis",
            statuses=["queued", "submitting", "running", "ambiguous"],
        )
        if any(j.status == "ambiguous" or j.query_outage_since for j in analyses):
            return False
        if (
            candidate.status == "generating"
            and job.kind == "video"
            and job.status in {"queued", "submitting", "running"}
        ) or (candidate.status == "review" and candidate.media_id and job.status == "complete"):
            progressing = True
    if progressing or delegated:
        return True
    if first.status == "escalated":
        return False
    rejected = any(
        c.status == "rejected" and (job := p.jobs.get(c.job_id)) and job.status == "complete"
        for c in select_entities(p.candidates, parent_id=first.id)
    )
    pending = next(
        (
            r
            for r in first.repair_attempts
            if r.id == first.pending_repair_id and not r.cancelled_at
        ),
        None,
    )
    repair_allowed = not ordinary_clip_corrections_complete(p, first) or bool(
        pending
        and not pending.job_ids
        and (pending.kind == "ordinary" or pending.reinforcement_revision_ids)
    )
    return bool(
        rejected
        and first.status in {"planned", "repair"}
        and repair_allowed
        and not clip_pending_proposals(p, first)
        and not waiting_for_voices(p, first)
    )


def proposal_waiting_for_predecessor(p: StudioProject, proposal: Proposal) -> bool:
    """Prepared later-Clip materials wait on the actual earliest unfinished Clip.

    Only acknowledged reference obligations qualify. A missing view, invalid done
    evidence, new request, current-Clip obligation or failed source stays actionable.
    No prose is interpreted and no material or observed-state gate is completed.
    """
    from .image_input_recovery import quarantine

    if proposal.status != "in_progress" or material_requests_need_acknowledgement(p, proposal):
        return False
    outstanding = [r for r in proposal.requirements if not requirement_ready(p, r)]
    if not outstanding or any(
        r.status != "pending" or r.kind != "reference" or not r.clip_ids for r in outstanding
    ):
        return False
    production_set = p.sets.get(proposal.set_id)
    if not production_set:
        return False
    first = min(
        (
            c
            for c in select_entities(p.clips, ids=production_set.clip_ids)
            if not c.archived_at and not c.accepted_candidate_id
        ),
        key=lambda c: c.order,
        default=None,
    )
    if not first:
        return False
    for requirement in outstanding:
        for identifier in requirement.clip_ids:
            clip = p.clips.get(identifier)
            if (
                not clip
                or clip.set_id != production_set.id
                or clip.archived_at
                or clip.accepted_candidate_id
                or clip.order <= first.order
                or not clip.asset_plan
            ):
                return False
            for plan in clip.asset_plan:
                state = p.states.get(plan.start_state_id)
                if not state or state.asset_id != plan.asset_id or not plan.required_views:
                    return False
                for view in plan.required_views:
                    revision = p.references.get(state.selected_revisions.get(view, ""))
                    if (
                        not revision
                        or revision.status != "selected"
                        or not revision.media_id
                        or revision.media_id not in p.media
                        or quarantine(p, revision.media_id)
                    ):
                        return False
    return clip_pipeline_progressing(p, first)


def proposal_waiting_for_source(p: StudioProject, proposal: Proposal) -> bool:
    """Defer acknowledged obligations while their actual source is progressing.

    Explicit later-Clip sources inside a Set do not require cross-Set handoffs.
    Proposal ownership alone cannot tell whether the executive has a decision
    to make. Never infer dependencies from prose; new requests stay actionable.
    """
    from .runtime_decisions import source_actor_can_progress
    from .set_handoffs import (
        dependency_report,
        source_clip,
        state_source_context,
        validate_state_preparation,
    )
    from .store import StudioError

    if proposal.status != "in_progress" or material_requests_need_acknowledgement(p, proposal):
        return False
    from .identity_bootstrap import proposal_waiting as bootstrap_source_waiting

    if bootstrap_source_waiting(p, proposal):
        return True
    outstanding = [r for r in proposal.requirements if not requirement_ready(p, r)]
    if not outstanding or any(
        r.status != "pending" or not r.clip_ids or (r.kind != "impact" and not r.source_waits)
        for r in outstanding
    ):
        return False
    target_ids = {identifier for r in outstanding for identifier in r.clip_ids}
    declared_sources = {
        (w.source_set_id, w.source_event_id) for r in outstanding for w in r.source_waits
    }
    sources: set[tuple[str, str]] = set()
    same_set_waits = all(
        r.source_waits and all(w.source_set_id == proposal.set_id for w in r.source_waits)
        for r in outstanding
    )
    if same_set_waits:
        # Later Clips can explicitly depend on this Set's selected predecessor.
        # This is scheduling metadata, not a cross-Set asset handoff or evidence
        # that the pending material/preparation requirement has been fulfilled.
        for requirement in outstanding:
            for wait in requirement.source_waits:
                source = source_clip(p, wait.source_set_id, wait.source_event_id)
                if not source:
                    return False
                for identifier in requirement.clip_ids:
                    clip = p.clips.get(identifier)
                    if (
                        not clip
                        or clip.set_id != proposal.set_id
                        or clip.archived_at
                        or clip.accepted_candidate_id
                        or clip.order <= source.order
                    ):
                        return False
        sources.update(declared_sources)
    else:
        for identifier in target_ids:
            clip = p.clips.get(identifier)
            if not clip or clip.set_id != proposal.set_id or clip.archived_at:
                return False
            report = dependency_report(p, clip.set_id, clip)
            handoffs = [item for item in report.blockers if item.kind == "asset_handoff"]
            if not handoffs or any(
                item.details.get("mode") != "observed"
                or item.details.get("status") != "waiting_source"
                for item in handoffs
            ):
                return False
            clip_sources = {
                (str(item.details["source_set_id"]), str(item.details["source_event_id"]))
                for item in handoffs
            }
            for item in report.blockers:
                if item.kind == "asset_handoff":
                    continue
                if item.kind != "state_preparation":
                    return False
                state = p.states.get(str(item.details.get("expected_state_id")))
                if not state or not state.preparation:
                    return False
                try:
                    validate_state_preparation(p, state, state.preparation)
                except StudioError:
                    return False
                if state.preparation.mode == "transition_target":
                    matching = [
                        h
                        for h in p.sets[clip.set_id].asset_handoffs
                        if h.asset_id == state.asset_id
                        and state.id in h.state_ids
                        and (str(h.source_set_id), str(h.source_event_id))
                        in clip_sources.intersection(declared_sources)
                    ]
                    views = {
                        view
                        for plan in clip.asset_plan
                        if plan.start_state_id == state.id
                        for view in plan.required_views
                    }
                    if (
                        not matching
                        or not state.selected_revisions
                        or not all(
                            view in state.selected_revisions
                            and state.selected_revisions[view] in p.references
                            and p.references[state.selected_revisions[view]].status == "selected"
                            and p.references[state.selected_revisions[view]].media_id
                            for view in views
                        )
                    ):
                        return False
                    continue  # Prepared design still needs the explicitly named real source.
                if state.preparation.mode != "observed":
                    return False
                if (
                    state_source_context(p, state).get("status") != "waiting_source"
                    or (
                        str(state.preparation.source_set_id),
                        str(state.preparation.source_event_id),
                    )
                    not in clip_sources
                ):
                    return False
            sources.update(clip_sources)
    for requirement in outstanding:
        for wait in requirement.source_waits:
            pair = (wait.source_set_id, wait.source_event_id)
            if (
                wait.source_set_id not in p.sets
                or wait.source_event_id not in p.story_events
                or wait.source_event_id not in p.sets[wait.source_set_id].story_event_ids
            ):
                return False
            if pair in sources:
                continue
            # A named voice bootstrap can also precede the observed exit. Do
            # not infer this obligation from prose or unrelated asset IDs.
            bootstrap = source_clip(p, *pair)
            if not bootstrap or not any(
                asset.voice_bootstrap_clip_id == bootstrap.id
                and not asset.selected_voice_id
                and any(asset.id in p.clips[c].speaking_asset_ids for c in requirement.clip_ids)
                for asset in p.assets.values()
            ):
                return False
            sources.add(pair)
    for set_id, event_id in sources:
        final = source_clip(p, set_id, event_id)
        if not final or final.accepted_candidate_id:
            return False
        source_set = p.sets[set_id]
        if not source_actor_can_progress(p, source_set.actor_id):
            return False
        # Earlier Clips must finish first; work after the source event is irrelevant.
        first = min(
            (
                c
                for c in select_entities(p.clips, ids=source_set.clip_ids)
                if not c.archived_at and not c.accepted_candidate_id and c.order <= final.order
            ),
            key=lambda c: c.order,
        )
        if not clip_pipeline_progressing(p, first):
            return False
    return bool(sources)


def known_evidence_scope(p: StudioProject, identifier: str) -> dict[str, Any]:
    """Only identify records found in this project; never echo an unknown argument."""
    for collection, kind in (
        (p.assets, "asset"),
        (p.states, "state"),
        (p.references, "reference"),
        (p.jobs, "job"),
        (p.media, "media"),
        (p.candidates, "candidate"),
    ):
        if (record := collection.get(identifier)) is not None:
            return {
                "actual_kind": kind,
                "detail_scope": f"{kind}:{record.id}",
                "status": getattr(record, "status", None),
            }
    memory = memory_record(p, identifier)
    if memory:
        return {
            "actual_kind": "global_memory" if memory[0] == "global" else "set_memory",
            "detail_scope": f"memory-entry:{memory[1].id}",
            "status": memory[1].status,
            "basis": memory[1].basis,
        }
    return {"actual_kind": "not_found_in_project"}


def requirement_diagnostic(
    p: StudioProject, item: ProposalRequirement, index: int, existing_ids: set[str]
) -> dict[str, Any]:
    expected = {
        "memory": "effective_global_memory",
        "impact": "effective_global_impact_memory",
        "state": "existing_state",
        "reference": "current_selected_reference",
        "asset": "published_asset",
        "view": "current_selected_reference",
        "auxiliary": "current_selected_reference",
    }[item.kind]
    problems: list[dict[str, Any]] = []
    for field, invalid in (
        ("status", item.status != "done"),
        ("resolution", not item.resolution.strip()),
        ("evidence_ids", not item.evidence_ids),
    ):
        if invalid:
            problems.append({"path": f"requirements.{index}.{field}", "reason": "incomplete"})
    for evidence_index, identifier in enumerate(item.evidence_ids):
        # Reuse the exact release predicate on a read-only diagnostic copy. The
        # original requirement, creative choice and proposed IDs remain untouched.
        single = item.model_copy(
            update={"status": "done", "resolution": "diagnostic", "evidence_ids": [identifier]}
        )
        if not requirement_ready(p, single):
            problems.append(
                {
                    "path": f"requirements.{index}.evidence_ids.{evidence_index}",
                    "reason": "evidence_not_ready",
                    "expected_kind": expected,
                    **known_evidence_scope(p, identifier),
                }
            )
    return {
        "requirement_index": index,
        **({"requirement_id": item.id} if item.id in existing_ids else {}),
        "kind": item.kind,
        "expected_kind": expected,
        "problems": problems,
    }


def clip_pending_proposals(p: StudioProject, clip: Clip) -> list[Proposal]:
    from .identity_bootstrap import deferred as bootstrap_deferred

    result = []
    for proposal in select_entities(p.proposals, statuses=["pending", "accepted", "in_progress"]):
        applies = (
            clip.id in proposal.affected_clip_ids
            if proposal.affected_clip_ids
            else proposal.set_id == clip.set_id
        )
        if not applies:
            continue
        requirements = [r for r in proposal.requirements if not r.clip_ids or clip.id in r.clip_ids]
        if (
            proposal.status == "pending"
            or not requirements
            or not all(
                requirement_ready(p, r) or bootstrap_deferred(p, clip, proposal, r)
                for r in requirements
            )
        ):
            result.append(proposal)
    return result


def material_requests(
    p: StudioProject, actor: Actor, affected: list[str], data: dict[str, Any]
) -> list[MaterialRequest]:
    requests = [MaterialRequest.model_validate(item) for item in data.get("material_requests", [])]
    require(not requests or True, "结构化补充素材申请仅适用于新流程")
    if not requests:
        return []
    from .materials import applies

    production_set = p.sets[str(actor.set_id)]
    unique: dict[str, MaterialRequest] = {}
    for item in requests:
        require(item.description.strip(), "素材申请需说明具体用途")
        if item.kind in {"state", "view"}:
            require(item.asset_id, "补充状态或视角需指定已有资产")
        if item.kind == "view":
            require(item.state_id and item.views, "补充视角需指定资产状态和所需视角")
        require(all(view.strip() for view in item.views), "申请的视角名称不能为空")
        if item.asset_id:
            asset = p.assets.get(item.asset_id)
            require(
                asset and asset.id in production_set.asset_ids, "申请的资产不在本片场已发布范围内"
            )
            assert asset is not None
            require(
                not affected
                or any(applies(p, p.clips[identifier], asset) for identifier in affected),
                "申请的资产不适用于受影响 Clip；请先向总导演说明所需范围",
            )
        if item.state_id:
            state = p.states.get(item.state_id)
            require(
                state and state.asset_id == item.asset_id,
                "申请的状态不属于指定资产，请核对 asset_id 与 state_id",
            )
        unique[digest(item.model_dump(mode="json"))] = item
    return list(unique.values())


def propose_global(p: StudioProject, actor: Actor, data: dict[str, Any]) -> dict[str, Any]:
    require(actor.role == "set_director" and actor.set_id in p.sets, "只有片场导演可提交全局建议")
    require(str(data.get("text", "")).strip(), "全局建议不能为空")
    production_set = p.sets[str(actor.set_id)]
    affected = list(data.get("affected_clip_ids", []))
    if not affected:
        affected = [i for i in production_set.clip_ids if not p.clips[i].accepted_candidate_id][:1]
    require(
        (affected or not production_set.clip_ids)
        and all(i in p.clips and not p.clips[i].archived_at for i in affected),
        "请明确建议影响的活跃 Clip",
    )
    require(
        set(affected) <= set(production_set.clip_ids),
        "只能申请本片场的变更，跨片场由总导演协调",
    )
    requests = material_requests(p, actor, affected, data)
    issue_key = data.get("issue_key") or digest(
        [" ".join(data["text"].split()), sorted(affected) or ["set:" + production_set.id]]
    )
    identifier = data.get("existing_proposal_id")
    existing = (
        p.proposals.get(str(identifier))
        if identifier
        else next(
            (
                x
                for x in select_entities(
                    p.proposals, statuses=["pending", "accepted", "in_progress"]
                )
                if x.issue_key == issue_key
            ),
            None,
        )
    )
    if identifier:
        require(
            existing and existing.status in {"pending", "accepted", "in_progress"},
            "关联提案不存在或已处理，请核对就绪检查",
        )
    if existing:
        additions = sorted(set(affected) - set(existing.affected_clip_ids))
        existing_materials = {
            digest(item.model_dump(mode="json")) for item in existing.material_requests
        }
        new_materials = [
            item
            for item in requests
            if digest(item.model_dump(mode="json")) not in existing_materials
        ]
        if additions or new_materials:
            existing.history.append(
                {
                    "kind": "handoff",
                    "actor_id": actor.id,
                    "text": data["text"],
                    "clip_ids": additions,
                    "material_requests": [item.model_dump(mode="json") for item in new_materials],
                    "created_at": now(),
                }
            )
            existing.affected_clip_ids.extend(additions)
            existing.material_requests.extend(new_materials)
            existing.status = "pending"
        return {
            "proposal_id": existing.id,
            "reused": True,
            "no_progress": not (additions or new_materials),
            "summary": "已关联现有全局提案",
            "affected_clip_ids": existing.affected_clip_ids,
            "added_material_requests": [item.model_dump(mode="json") for item in new_materials],
        }
    proposal = Proposal(
        actor_id=actor.id,
        set_id=production_set.id,
        text=data["text"],
        affected_clip_ids=affected,
        issue_key=issue_key,
        material_requests=requests,
    )
    p.proposals[proposal.id] = proposal
    return {"proposal_id": proposal.id, "set_id": production_set.id, "summary": proposal.text[:500]}


def administrative_evidence_ready(
    p: StudioProject, proposal: Proposal, evidence_ids: list[str]
) -> bool:
    """Explicit closure cites real source records; missing material stays a gate."""
    from .image_input_recovery import quarantine

    if not evidence_ids:
        return False
    production_set = p.sets[proposal.set_id]
    clip_ids = set(proposal.affected_clip_ids or production_set.clip_ids)
    asset_ids = set(production_set.asset_ids)
    event_ids = set(production_set.story_event_ids)
    for clip_id in clip_ids:
        clip = p.clips.get(clip_id)
        if clip:
            asset_ids.update(item.asset_id for item in clip.asset_plan)
            event_ids.update(clip.story_event_ids)
    asset_ids.update(request.asset_id for request in proposal.material_requests if request.asset_id)
    asset_ids.update(
        p.states[request.state_id].asset_id
        for request in proposal.material_requests
        if request.state_id in p.states
    )
    selected: set[str] = set()
    for identifier in evidence_ids:
        reference = p.references.get(identifier)
        if reference:
            if (
                reference.status != "selected"
                or p.states[reference.state_id].asset_id not in asset_ids
                or not reference.media_id
                or reference.media_id not in p.media
                or p.states[reference.state_id].selected_revisions.get(reference.view) != identifier
                or quarantine(p, reference.media_id)
            ):
                return False
            selected.add(identifier)
            continue
        candidate = p.candidates.get(identifier)
        if candidate:
            if (
                candidate.clip_id not in clip_ids
                or not candidate.media_id
                or candidate.media_id not in p.media
                or p.clips[candidate.clip_id].accepted_candidate_id != identifier
            ):
                return False
            continue
        record = memory_record(p, identifier)
        if record:
            if record[0] != "global" or record[1].status != "effective":
                return False
            continue
        if identifier in p.states and p.states[identifier].asset_id in asset_ids:
            continue
        if identifier in asset_ids or identifier in event_ids:
            continue
        clip = p.clips.get(identifier)
        if identifier in clip_ids and clip and not clip.archived_at:
            continue
        return False
    for request in proposal.material_requests:
        state = p.states.get(request.state_id or "")
        if (
            not state
            or not request.views
            or (request.asset_id and state.asset_id != request.asset_id)
            or any(state.selected_revisions.get(view) not in selected for view in request.views)
        ):
            return False
    return True


def resolve_proposal(p: StudioProject, actor: Actor, data: dict[str, Any]) -> dict[str, Any]:
    require(actor.role == "executive", "只有总导演可以落实全局建议")
    proposal = p.proposals.get(str(data.get("proposal_id")))
    require(proposal, "建议不存在")
    assert proposal is not None
    require(proposal.status not in {"applied", "rejected"}, "建议已经处理")
    status = data.get("status")
    require(
        status in {"accepted", "in_progress", "applied", "rejected"},
        "请选择接受、处理中、已落实或拒绝",
    )
    require(str(data.get("resolution", "")).strip(), "需要记录处理依据")
    before = {
        "status": proposal.status,
        "resolution": proposal.resolution,
        "affected_clip_ids": list(proposal.affected_clip_ids),
        "requirements": [x.model_dump(mode="json") for x in proposal.requirements],
        "created_at": now(),
    }
    affected = list(data.get("affected_clip_ids", proposal.affected_clip_ids))
    require(set(proposal.affected_clip_ids) <= set(affected), "不能删除尚未处理的受影响 Clip")
    requirements = (
        [ProposalRequirement.model_validate(x) for x in data["requirements"]]
        if "requirements" in data
        else proposal.requirements
    )
    old_ids = {x.id for x in proposal.requirements}
    require(old_ids <= {x.id for x in requirements}, "不能移除尚未落实的工作，请更新其结果")
    if status == "rejected":
        # Replanning archives the old Clips without rewriting their proposals. An
        # executive must still be able to retire such a proposal, including its
        # now-stale completion evidence. Rejection preserves those obligations as
        # history; it must not rewrite them into apparently completed work.
        require(
            affected == proposal.affected_clip_ids and requirements == proposal.requirements,
            "拒绝提案时请保留原 Clip 范围和要求，只提交 status、resolution；新计划另行明确落实",
        )
        require(all(i in p.clips for i in affected), "受影响 Clip 不存在")
    else:
        require(
            all(i in p.clips and not p.clips[i].archived_at for i in affected),
            "受影响 Clip 不存在或已归档；过时提案可保留原范围和要求，由总导演拒绝并记录理由",
        )
    administrative_evidence = list(dict.fromkeys(str(i) for i in data.get("evidence_ids", [])))
    administrative = status == "applied" and not requirements
    if status != "rejected":
        require(
            requirements
            or (
                administrative
                and administrative_evidence_ready(p, proposal, administrative_evidence)
            ),
            "实际制作要求需逐项落实；纯行政提案可 applied 并关联真实 source/选用证据，不能只改状态",
        )
        require(
            administrative
            or all(
                any(not r.clip_ids or identifier in r.clip_ids for r in requirements)
                for identifier in affected
            ),
            "每个受影响 Clip 都需要具体落实要求",
        )
    invalid = []
    for index, item in enumerate(requirements):
        require(set(item.clip_ids) <= set(affected), "落实要求超出了提案的 Clip 范围")
        if item.status == "done" and status != "rejected" and not requirement_ready(p, item):
            invalid.append(requirement_diagnostic(p, item, index, old_ids))
    if invalid:
        raise StudioPrerequisiteError(
            "已落实要求需关联有效全局记忆、现有状态、当前选用图片或影响决定；"
            "请核对第 " + "、".join(str(i["requirement_index"] + 1) for i in invalid) + " 项",
            details={
                "reason": "proposal_evidence_not_ready",
                "proposal_id": proposal.id,
                "owner_role": "executive",
                "requirements": invalid,
            },
            recovery="按 requirements 中的 path 定位具体证据项，读取已知记录的 detail_scope，"
            "按 expected_kind 提交有效证据；不要重复原请求或把未落实的工作标为完成",
        )
    if status == "applied":
        pending = [
            requirement_diagnostic(p, item, index, old_ids)
            for index, item in enumerate(requirements)
            if not requirement_ready(p, item)
        ]
        if pending:
            raise StudioPrerequisiteError(
                "仍有要求未落实，不能标记全部完成",
                details={
                    "reason": "proposal_requirements_pending",
                    "proposal_id": proposal.id,
                    "owner_role": "executive",
                    "requirements": pending,
                },
                recovery="按 requirements 完成尚缺的工作及证据；部分就绪时保持 in_progress，"
                "只让其所需事项已落实的 Clip 继续",
            )
    if administrative:
        before["administrative_evidence_ids"] = administrative_evidence
    proposal.history.append(before)
    proposal.affected_clip_ids, proposal.requirements = affected, requirements
    proposal.status = cast(Literal["accepted", "in_progress", "applied", "rejected"], status)
    proposal.resolution = str(data["resolution"])
    if status != "rejected":
        proposal.material_requests_acknowledged_hash = proposal_request_fingerprint(proposal)
    ready_ids = [
        i
        for i in affected
        if not p.clips[i].archived_at and not clip_pending_proposals(p, p.clips[i])
    ]
    for identifier in ready_ids:
        director = p.actors[p.sets[p.clips[identifier].set_id].actor_id]
        if director.native_session_blocked:
            continue
        director.request_wake()
        if not director.paused and director.status not in {"running", "complete"}:
            director.status = "ready"
    return {
        "proposal_id": proposal.id,
        "ready_clip_ids": ready_ids,
        "summary": proposal.resolution[:500],
    }
