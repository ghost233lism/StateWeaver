"""Prioritize references on the immediate production path without starving others."""

from __future__ import annotations

from .contracts import Job, StudioProject
from .preparations import frontier
from .repository import select_entities
from .routing import timestamp


def ordered_images(
    projects: list[StudioProject], jobs: dict[str, list[Job]], stamp: float
) -> list[tuple[StudioProject, Job]]:
    queued = [
        (project, job)
        for project in projects
        for job in jobs[project.id]
        if job.kind == "image" and job.status == "queued"
    ]
    # No prioritization is needed with fewer than two submissions. In particular,
    # an empty queue must not hydrate every Clip's preparation history each tick.
    if len(queued) < 2:
        return queued
    queued_project_ids = {project.id for project, _ in queued}
    priorities: dict[tuple[str, str, str], int] = {}
    for project in projects:
        if project.id not in queued_project_ids or project.deleted_at:
            continue
        for production_set in select_entities(project.sets):
            if project.actors[production_set.actor_id].paused:
                continue
            for index, clip in enumerate(frontier(project, production_set.id)):
                if index and not project.config.prepare_next_clip:
                    continue
                for plan in clip.asset_plan:
                    views = plan.required_views or project.assets[plan.asset_id].required_views
                    for state_id in {plan.start_state_id, plan.end_state_id} - {None}:
                        for view in views:
                            key = (project.id, str(state_id), view)
                            priorities[key] = min(priorities.get(key, 2), index)

    def order(pair: tuple[StudioProject, Job]) -> tuple[int, str, str, str]:
        project, job = pair
        revision = project.references.get(job.target_id)
        rank = priorities.get((project.id, revision.state_id, revision.view), 2) if revision else 2
        # Each five minutes of waiting promotes one class. FIFO breaks equal ranks.
        age = max(0, stamp - (timestamp(job.created_at) or stamp))
        return max(0, rank - int(age // 300)), job.created_at, project.id, job.id

    return sorted(queued, key=order)
