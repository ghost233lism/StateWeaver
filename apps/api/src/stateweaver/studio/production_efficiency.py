"""Opt-in task-scoped coordination; preserved quality and provenance boundaries."""

from __future__ import annotations

from typing import Any

from .contracts import Actor, DelegateReport, DelegateTask, StudioProject, VisualFinding, now
from .memory import require
from .repository import select_entities
from .store import StudioStore


def effort(project: StudioProject, actor: Actor) -> str:
    """Classify current work, not the failure count of a whole production scope."""
    task = project.delegate_tasks.get(actor.delegate_task_id or "")
    if task and task.request.kind in {"state_conflict", "repair_diagnosis"}:
        return "max"
    if actor.role in {"screenwriter", "visual_analyst", "task_analyst", "material_executor"}:
        return "high"
    if actor.role == "set_director":
        return "high"  # Review and prompt planning; waiting runs outside the model.
    if not project.catalog_frozen_at or any(
        not s.planning_ready_at for s in select_entities(project.sets)
    ):
        return "high"
    return "medium"


def creative_fingerprint(project: StudioProject, clip_id: str) -> str:
    """Approval progress gates submission, but does not rewrite a creative draft."""
    from .memory import digest
    from .task_context import context_records

    return digest(
        [
            record
            for record in context_records(project, clip_id, "references")
            if record["type"] not in {"proposal", "task"}
        ]
        + [{"type": "proposal_definitions", "value": proposal_definitions(project, clip_id)}]
    )


def proposal_definitions(project: StudioProject, clip_id: str) -> list[dict[str, Any]]:
    """Retain approved creative obligations, not their changing delivery ledger.

    Completed declarations remain present so merely resolving a proposal cannot
    invalidate a prompt. New obligations, changed scope or rejection still can.
    """
    clip = project.clips[clip_id]
    result = []
    for proposal in select_entities(project.proposals, parent_id=clip.set_id):
        if proposal.affected_clip_ids and clip_id not in proposal.affected_clip_ids:
            continue
        result.append(
            {
                "id": proposal.id,
                "text": proposal.text,
                "rejected": proposal.status == "rejected",
                "requirements": [
                    requirement.model_dump(
                        exclude={"status", "evidence_ids", "resolution"}, mode="json"
                    )
                    for requirement in proposal.requirements
                    if not requirement.clip_ids or clip_id in requirement.clip_ids
                ],
                "material_requests": [
                    r.model_dump(mode="json") for r in proposal.material_requests
                ],
            }
        )
    return sorted(result, key=lambda r: r["id"])


def stable_snapshot(project: StudioProject, snapshot: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in snapshot.items() if v.get("type") != "proposal"}


def bind_supply_targets(project: StudioProject, task: DelegateTask) -> None:
    """A task may supply only reservations that already existed at authorization."""
    if task.request.kind != "material_preparation":
        return
    from .repairs import pending_attempt

    for identifier in task.allowed_scope.clip_ids:
        clip = project.clips[identifier]
        attempt = pending_attempt(clip)
        if attempt and attempt.kind == "executive" and not attempt.job_ids:
            task.repair_supply_targets.setdefault(clip.id, attempt.id)


def provenance(project: StudioProject, task: DelegateTask) -> list[dict[str, Any]]:
    result = []
    for identifier in task.output_revision_ids:
        reference = project.references[identifier]
        media = project.media.get(reference.media_id or "")
        result.append(
            {
                "revision_id": identifier,
                "state_id": reference.state_id,
                "view": reference.view,
                "status": reference.status,
                "job_id": reference.job_id,
                "media_id": reference.media_id,
                "sha256": media.sha256 if media else None,
                "bytes": media.byte_size if media else None,
                "width": media.width if media else None,
                "height": media.height if media else None,
                "available_at": project.jobs[reference.job_id].completed_at
                if reference.job_id in project.jobs
                else None,
                "selected_at": reference.selection.created_at if reference.selection else None,
            }
        )
    return result


def takeover_references(project: StudioProject, task: DelegateTask) -> list[str]:
    """Use an explicit parent's real supply without attributing it to the child."""
    from .delegation import stable_target
    from .image_input_recovery import quarantine

    state_id = task.request.state_id
    if (
        task.request.kind != "material_preparation"
        or not state_id
        or state_id not in task.allowed_scope.state_ids
        or state_id not in project.states
        or (
            state_id in task.target_fingerprints
            and stable_target(project, state_id) != task.target_fingerprints[state_id]
        )
    ):
        return []
    takeover = next(
        (f for f in reversed(task.followups) if f.get("kind") == "superseded_by_parent_takeover"),
        {},
    )
    supplied = set(takeover.get("supplied_media_ids", []))
    state = project.states[state_id]
    result = []
    for view in task.request.views:
        reference = project.references.get(state.selected_revisions.get(view, ""))
        if (
            reference
            and reference.state_id == state_id
            and reference.view == view
            and reference.status == "selected"
            and reference.selection
            and reference.media_id in supplied
            and reference.media_id in project.media
            and not quarantine(project, str(reference.media_id))
        ):
            result.append(reference.id)
    return result


def deliver_requirements(project: StudioProject, task: DelegateTask) -> None:
    """Only explicitly assigned, preapproved image obligations may auto-complete.

    No prose is interpreted as an authorization, no observed-state obligation is
    completed, and all tasks assigned to the same obligation must have delivered.
    """
    from .delegation import stale_reasons
    from .proposals import requirement_ready

    declared = set(task.requirement_ids) | set(task.request.requirement_ids)
    for proposal_id in task.allowed_scope.proposal_ids:
        proposal = project.proposals[proposal_id]
        if proposal.status not in {"accepted", "in_progress"}:
            continue
        for requirement in proposal.requirements:
            if (
                requirement.id not in declared
                or requirement.kind not in {"reference", "view", "auxiliary"}
                or requirement.source_waits
                or not requirement.clip_ids
                or not set(requirement.clip_ids) <= set(task.allowed_scope.clip_ids)
                or requirement_ready(project, requirement)
            ):
                continue
            peers = [
                t
                for t in select_entities(project.delegate_tasks, parent_id=task.parent_actor_id)
                if proposal_id in t.allowed_scope.proposal_ids
                and requirement.id in {*t.requirement_ids, *t.request.requirement_ids}
            ]
            revisions: list[str] = []
            parent_supplied = False
            for peer in peers:
                takeover = takeover_references(project, peer)
                if (
                    peer.request.kind != "material_preparation"
                    or peer.status not in {"queued", "running", "complete"}
                    or (
                        not takeover
                        and (
                            stale_reasons(project, peer)
                            or select_entities(
                                project.jobs,
                                actor_id=peer.actor_id,
                                statuses=["queued", "submitting", "running", "ambiguous"],
                                limit=1,
                            )
                        )
                    )
                ):
                    break
                selected = [
                    i
                    for i in dict.fromkeys([*peer.delivered_revision_ids, *takeover])
                    if project.references[i].status == "selected"
                    and project.states[project.references[i].state_id].selected_revisions.get(
                        project.references[i].view
                    )
                    == i
                ]
                if not set(peer.request.views) <= {project.references[i].view for i in selected}:
                    break
                revisions.extend(selected)
                parent_supplied = parent_supplied or bool(takeover)
            else:
                if revisions:
                    proposal.history.append(
                        {
                            "kind": "preauthorized_material_delivery",
                            "task_id": task.id,
                            "requirement": requirement.model_dump(mode="json"),
                            "created_at": now(),
                        }
                    )
                    requirement.evidence_ids = list(dict.fromkeys(revisions))
                    requirement.resolution = (
                        "原授权素材已实际选用并交付；父导演接管供给保留原来源。"
                        if parent_supplied
                        else "原授权素材任务已实际查看、合格选用并逐项交付。"
                    )
                    requirement.status = "done"


def deliver_material(
    project: StudioProject, actor: Actor, outcome: Any, store: StudioStore
) -> list[dict[str, Any]]:
    """Deliver each inspected selection without waiting for a narrated batch report.

    Called inside the original authorized selection transaction. A crop, observed
    adoption or old source cannot count as newly generated repair reinforcement.
    """
    if actor.role != "material_executor" or not isinstance(outcome, dict):
        return []
    from .delegation import complete, stale_reasons, task_for
    from .image_input_recovery import inspected_evidence, quarantine
    from .repairs import pending_attempt, supply_references

    task = task_for(project, actor)
    identifier = outcome.get("reference_id")
    if not identifier or task.status not in {"queued", "running"}:
        return []
    reference = project.references[identifier]
    if reference.status != "selected" or identifier not in task.output_revision_ids:
        return []
    if not reference.selection or reference.selection.kind != "quality_passed":
        return []  # A recorded executive compromise is never a passing worker review.
    require(not stale_reasons(project, task), "素材授权已变化，不能自动交付")
    require(
        identifier in task.output_revision_ids
        and reference.state_id == task.request.state_id
        and reference.view in task.request.views
        and reference.media_id
        and not quarantine(project, reference.media_id)
        and reference.media_id in inspected_evidence(project, actor, [reference.media_id], store),
        "自动交付需本任务获准视角、准确选用版本及实际查看证据",
    )
    if identifier in task.delivered_revision_ids:
        return []
    task.delivered_revision_ids.append(identifier)
    delivered_at = now()
    task.approvals.append(
        {
            "action": "material_item_delivered",
            "task_version": task.version,
            "revision_id": identifier,
            "at": delivered_at,
            "authorized_by": task.parent_actor_id,
        }
    )
    for clip_id, reservation_id in task.repair_supply_targets.items():
        clip = project.clips[clip_id]
        attempt = pending_attempt(clip)
        if not attempt or attempt.id != reservation_id or attempt.job_ids:
            continue  # Never transfer a preauthorization to a later repair.
        job = project.jobs.get(reference.job_id or "")
        if (
            not job
            or job.kind != "image"
            or job.status != "complete"
            or reference.created_at < attempt.registered_at
        ):
            continue
        supply_references(
            project,
            actor,
            {
                "clip_id": clip_id,
                "repair_attempt_id": reservation_id,
                "revision_ids": list(
                    dict.fromkeys([*attempt.reinforcement_revision_ids, identifier])
                ),
                "reason": "预授权内已实际查看并选用的补强素材：" + task.request.question,
            },
            store,
            authorized_task_id=task.id,
        )
    deliver_requirements(project, task)
    for clip_id in task.allowed_scope.clip_ids:
        clip = project.clips[clip_id]
        if clip.archived_at or clip.accepted_candidate_id:
            continue
        director = project.actors[project.sets[clip.set_id].actor_id]
        if not director.native_session_blocked:
            director.request_wake()
            if not director.paused and director.status in {"waiting", "idle"}:
                director.status = "ready"
    timings = []
    job = project.jobs.get(reference.job_id or "")
    if job and job.completed_at:
        from datetime import datetime

        start = datetime.fromisoformat(job.completed_at).timestamp()
        end = datetime.fromisoformat(delivered_at).timestamp()
        timings.append(
            {
                "phase": "material_delivery",
                "operation": "result_to_selected_delivery",
                "started": start,
                "seconds": max(0, end - start),
                "actor_id": actor.id,
                "details": {
                    "task_id": task.id,
                    "revision_id": identifier,
                    "inclusive_of": ["material_review", "mcp"],
                },
            }
        )
    # The task's remaining views still execute; no premature whole-task completion.
    selected = [
        i
        for i in task.delivered_revision_ids
        if project.references[i].status == "selected"
        and project.states[reference.state_id].selected_revisions.get(project.references[i].view)
        == i
    ]
    views = {project.references[i].view for i in selected}
    pending = select_entities(
        project.jobs,
        actor_id=actor.id,
        statuses=["queued", "submitting", "running", "ambiguous"],
        limit=1,
    )
    if not set(task.request.views) <= views or pending:
        return timings
    findings = []
    for selected_id in selected:
        selected_reference = project.references[selected_id]
        selection = selected_reference.selection
        if selection:
            findings.append(
                VisualFinding(
                    start_seconds=0,
                    description=selection.reason,
                    severity="minor",
                    evidence_media_ids=[str(selected_reference.media_id)],
                )
            )
    report = DelegateReport(
        conclusion="获准视角已逐项实际审查、选用并交付；视频效果仍需片场导演审核。",
        findings=findings,
        selected_revision_ids=selected,
        limitations=["图片仅为设计参考，不认证实拍状态、运动或声音。"],
        provenance=provenance(project, task),
    )
    complete(project, actor, {"task_id": task.id, "report": report.model_dump()}, store)
    task.approvals.append(
        {
            "action": "preauthorized_delivery",
            "actor_id": task.parent_actor_id,
            "task_version": task.version,
            "revision_ids": selected,
            "at": now(),
        }
    )
    return timings
