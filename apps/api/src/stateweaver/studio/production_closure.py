"""Apply bounded recovery outcomes while independent production continues."""

from __future__ import annotations

from typing import Any

from .candidate_lifecycle import set_pending_work, terminal_recovery
from .contracts import StudioProject
from .production_decisions import apply_selection
from .production_recovery import decisions_paused, reconcile_terminal_failures
from .repository import select_entities


def reconcile(project: StudioProject) -> list[dict[str, Any]]:
    if project.deleted_at or decisions_paused(project) or project.status in {"complete", "failed"}:
        return []
    outcomes = reconcile_terminal_failures(project)
    executive = next((a for a in project.actors.values() if a.role == "executive"), None)
    for outcome in outcomes:
        clip = project.clips[outcome["clip_id"]]
        director = project.actors[project.sets[clip.set_id].actor_id]
        if outcome["action"] == "settle":
            outcome.update(
                apply_selection(
                    project, executive or director, clip, outcome["plan"], kind="system_fallback"
                )
            )
        for actor in [director, *([executive] if executive else [])]:
            if not actor.paused and not actor.native_session_blocked:
                actor.request_wake()
                if actor.status not in {"running", "complete"}:
                    actor.status = "ready"
    for production_set in project.sets.values():
        clips = [
            c
            for c in select_entities(project.clips, ids=production_set.clip_ids)
            if not c.archived_at
        ]
        if (
            production_set.status not in {"complete", "failed"}
            and production_set.clip_plan_complete
            and clips
            and any(c.terminal_failure for c in clips)
            and all(c.accepted_candidate_id or c.terminal_failure for c in clips)
            and not set_pending_work(project, production_set)
        ):
            production_set.status = "failed"
            actor = project.actors[production_set.actor_id]
            actor.status, actor.wake_requested = "blocked", False
            actor.last_error = "production_failed"
            actor.summary = "Production ended with partial outputs; required Clips failed."
            outcomes.append({"action": "set_failed", "set_id": production_set.id})
    if (
        project.sets
        and any(s.status == "failed" for s in project.sets.values())
        and all(s.status in {"complete", "failed"} for s in project.sets.values())
        and not any(
            not terminal_recovery(j)
            for j in select_entities(
                project.jobs, statuses=["queued", "submitting", "running", "ambiguous"]
            )
        )
    ):
        project.status = "failed"
        if executive:
            executive.status, executive.wake_requested = "blocked", False
            executive.last_error = "production_failed"
            executive.summary = "Independent work finished; partial outputs and failures retained."
        outcomes.append({"action": "project_failed", "project_id": project.id})
    return outcomes
