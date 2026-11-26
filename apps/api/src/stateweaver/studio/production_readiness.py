"""One indexed readiness predicate for waiting feedback, scheduling and turn closure."""

from __future__ import annotations

from typing import Any

from .assembly import current_final_media_id
from .contracts import Actor, StudioProject
from .proposals import (
    material_requests_need_acknowledgement,
    proposal_waiting_for_delegate,
    proposal_waiting_for_predecessor,
    proposal_waiting_for_source,
)
from .quality import quality_waiting
from .repository import select_entities
from .runtime_store import RuntimeStore


def has_ready_work(
    p: StudioProject,
    actor: Actor,
    runtime: RuntimeStore,
    *,
    include_lookahead: bool = True,
    details: dict[str, Any] | None = None,
) -> bool:
    """Query unconsumed work by owner/status; archived results are never scanned."""

    def ready(kind: str, entity_id: str) -> bool:
        if details is not None:
            details.update(kind=kind, entity_id=entity_id, next_action="get_project")
            if kind == "proposal":
                details["detail_scope"] = f"proposal:{entity_id}"
        return True

    pending = ["queued", "submitting", "running", "ambiguous"]
    from .candidate_lifecycle import (
        candidate_disposition_valid,
        pending_review_candidates,
        terminal_recovery,
    )

    coherent = True

    def live_jobs(actor_id: str) -> bool:
        if not coherent:
            return bool(select_entities(p.jobs, actor_id=actor_id, statuses=pending, limit=1))
        return any(
            not terminal_recovery(job)
            for job in select_entities(p.jobs, actor_id=actor_id, statuses=pending)
        )

    if coherent:
        review_candidates = pending_review_candidates(p, actor)
        if review_candidates:
            return ready("candidate_review", review_candidates[0].id)
    if actor.role in {"task_analyst", "material_executor"}:
        from .delegation import task_for

        task = task_for(p, actor)
        if task.status not in {"queued", "running"}:
            return False
        return not live_jobs(actor.id) and ready("delegate_task", task.id)
    if actor.role == "visual_analyst":
        return p.visual_tasks[str(actor.visual_task_id)].status in {"queued", "running"} and ready(
            "visual_task", str(actor.visual_task_id)
        )
    if actor.role == "set_director":
        production_set = p.sets[str(actor.set_id)]
        if coherent and production_set.status in {"complete", "failed"}:
            return False
        from .planning_sessions import covered_plan_work, unplanned_suffix_ready
        from .playback import set_status as playback_set_status
        from .preparations import needs_lookahead
        from .workflow import can_plan_set

        if unplanned_suffix_ready(p, actor):
            return ready("set_suffix_planning", production_set.id)
        completion = covered_plan_work(p, actor)
        if completion:
            return ready(
                "set_plan_confirmation"
                if completion == "confirmation"
                else "set_duration_planning",
                production_set.id,
            )

        duration_wait = bool(production_set.clip_ids) and (
            not playback_set_status(p, production_set).can_submit
        )
        # Optional lookahead must not turn a declared budget wait into a
        # no-progress loop. Already returned candidates are still actionable.
        if include_lookahead and not duration_wait and needs_lookahead(p, actor):
            return ready("clip_preparation", production_set.id)

        if not production_set.clip_ids and can_plan_set(p, production_set):
            return ready("set_planning", production_set.id)
        for clip in select_entities(p.clips, ids=production_set.clip_ids):
            if (
                clip.archived_at
                or clip.accepted_candidate_id
                or (coherent and clip.terminal_failure)
            ):
                continue
            for candidate in select_entities(p.candidates, parent_id=clip.id, statuses=["review"]):
                if coherent and candidate_disposition_valid(p, candidate):
                    continue
                if not candidate.media_id:
                    continue
                if runtime.has_analysis(p, candidate.id, pending):
                    continue
                if (
                    candidate.analysis
                    and (candidate.end_state or candidate.analysis.get("review_mode") == "director")
                ) or not runtime.has_analysis(p, candidate.id):
                    return ready("candidate_review", candidate.id)
        return False
    if actor.role != "executive":
        return False
    from .delegation import progressing_clip_tasks, reference_has_worker

    inbox = select_entities(p.executive_inbox, parent_id=actor.id, statuses=["pending"], limit=1)
    if inbox:
        return ready("executive_inbox", inbox[0].id)
    from .playback import set_status as playback_set_status

    for production_set in p.sets.values():
        if (
            production_set.clip_ids
            and not (coherent and production_set.status in {"complete", "failed"})
            and not playback_set_status(p, production_set).can_submit
        ):
            return ready("set_duration_budget", production_set.id)
    from .set_handoffs import dependency_report

    for production_set in p.sets.values():
        report = dependency_report(p, production_set.id)
        if report.blockers and (
            any(item.kind == "handoff_unclassified" for item in report.blockers)
            or any(
                item.get("status") in {"needs_confirmation", "stale", "invalid"}
                for item in report.items
            )
        ):
            return ready("set_handoff", production_set.id)
    for production_set in p.sets.values():
        for clip in select_entities(p.clips, ids=production_set.clip_ids):
            if (
                not clip.archived_at
                and not clip.accepted_candidate_id
                and not (coherent and clip.terminal_failure)
                and clip.state_readiness == "needs_verification"
            ):
                return ready("clip_state_verification", clip.id)
    for issue in p.issues:
        if issue.startswith("quality:clip:"):
            clip = p.clips[issue.split(":", 2)[2]]
            if coherent and clip.terminal_failure:
                continue
            if progressing_clip_tasks(p, clip.id):
                continue
            if (
                clip.quality_episode
                and clip.quality_episode.status == "escalated"
                and not quality_waiting(p, clip)
            ):
                return ready("quality_decision", clip.id)
            if clip.quality_episode and clip.quality_episode.status == "intervening":
                from .quality_progress import quality_progress

                if any(
                    b.code == "new_reference_required" for b in quality_progress(p, clip).blockers
                ):
                    return ready("quality_reference_supply", clip.id)
        elif issue.startswith("quality:reference:"):
            _, _, state_id, view = issue.split(":", 3)
            if reference_has_worker(p, state_id, view):
                continue
            if not any(
                r.view == view and r.status == "pending"
                for r in select_entities(p.references, parent_id=state_id)
            ):
                return ready("reference_quality", state_id)
    for asset in p.assets.values():
        if asset.selected_voice_id or any(v.status == "preparing" for v in asset.voice_references):
            continue
        if asset.voice_bootstrap_clip_id:
            if p.clips[asset.voice_bootstrap_clip_id].accepted_candidate_id:
                return ready("voice_reference", asset.id)
        elif any(
            asset.id in p.clips[c].speaking_asset_ids for s in p.sets.values() for c in s.clip_ids
        ):
            return ready("voice_reference", asset.id)
    proposals = select_entities(p.proposals, statuses=["pending", "accepted", "in_progress"])
    unfinished_sets = select_entities(p.sets, statuses=["planned", "producing", "blocked"])
    if p.final_media_id and not unfinished_sets and not p.issues and not proposals:
        if current_final_media_id(p):
            return ready("project_completion", p.id)
        if not live_jobs(actor.id) and not select_entities(p.sets, statuses=["failed"], limit=1):
            ready("assembly", p.id)
            if details is not None:
                details["next_action"] = "assemble"
            return True
    for reference in select_entities(p.references, statuses=["candidate"]):
        state = p.states[reference.state_id]
        if (
            reference.media_id
            and reference.view in p.assets[state.asset_id].required_views
            and reference.view not in state.selected_revisions
            and not reference_has_worker(p, state.id, reference.view)
        ):
            return ready("reference_selection", reference.id)
    if coherent:
        for proposal in proposals:
            if proposal.status == "pending" or material_requests_need_acknowledgement(p, proposal):
                return ready("proposal", proposal.id)
    assessments = [issue for issue in p.issues if issue.startswith("revision_assessment:")]
    for issue in assessments:
        affected = runtime.affected_candidates(p, issue.split(":")[1])
        if all(c.status == "failed" or (c.analysis and c.end_state) for c in affected):
            return ready("revision_assessment", issue.split(":")[1])
    if assessments and not coherent:
        return False
    for proposal in proposals:
        if (
            proposal_waiting_for_delegate(p, proposal)
            or proposal_waiting_for_predecessor(p, proposal)
            or proposal_waiting_for_source(p, proposal)
        ):
            continue
        if coherent:
            # Only the exact assigned/material/source predicates above suppress
            # a proposal. Unrelated jobs owned by the same actor are not evidence
            # that an administrative decision is being handled elsewhere.
            return ready("proposal", proposal.id)
        if any(live_jobs(aid) for aid in {actor.id, proposal.actor_id}):
            continue
        if any(
            not candidate.analysis
            for clip in select_entities(p.clips, ids=p.sets[proposal.set_id].clip_ids)
            for candidate in select_entities(p.candidates, parent_id=clip.id, statuses=["review"])
        ):
            continue
        return ready("proposal", proposal.id)
    return False
