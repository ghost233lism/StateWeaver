"""Planning and review policy for Studio Sets."""

from __future__ import annotations

from stateweaver.studio_media.latent import CONTINUATION_SECONDS as CONTINUATION_SECONDS

from .contracts import ProductionSet, StudioProject

H3_PROMPT_SKILL = "h3-ref2va-set-v2"
SELF_REVIEW = "director-self-review-v1"


def unit_name(project: StudioProject) -> str:
    return "片场"


def can_plan_set(project: StudioProject, production_set: ProductionSet) -> bool:
    return bool(
        project.screenplay.confirmed_at
        and production_set.planning_ready_at
        and production_set.story_goal.strip()
        and production_set.entry_state.strip()
        and production_set.exit_state.strip()
    )
