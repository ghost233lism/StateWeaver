"""Read-only settlement gates: completed creative work is not a reservation count."""

from __future__ import annotations

from .candidate_lifecycle import (
    clip_pending_work,
    fallback_blocking_work,
    fallback_selection_plan,
    selectable_candidates,
)
from .contracts import Clip, StudioProject
from .harness import video_extra_limit
from .quality_progress import quality_progress


def quality_budget_exhausted(project: StudioProject, clip: Clip) -> bool:
    """All configured quality opportunities produced reviewed results.

    A terminal technical failure has a separate termination path. It cannot be
    counted as a completed quality review just to trigger this gate.
    """
    if clip_pending_work(project, clip):
        return False
    progress = quality_progress(project, clip, readiness_items=[])
    extra_limit = video_extra_limit(project)
    episode = clip.quality_episode
    return bool(
        progress.initial.generations >= 1
        and progress.initial.reviews >= 1
        and progress.ordinary.generations >= project.config.repairs_per_clip
        and progress.ordinary.reviews >= project.config.repairs_per_clip
        and progress.executive.generations >= extra_limit
        and progress.executive.reviews >= extra_limit
        and (
            extra_limit == 0
            or (
                episode is not None
                and episode.extra_granted >= extra_limit
                and episode.extra_used >= extra_limit
            )
        )
    )


def preapproved_clip_settlement_ready(project: StudioProject, clip: Clip) -> bool:
    """A recorded fallback is a safe stop; extra allowances are maxima, not obligations."""
    episode = clip.quality_episode
    if (
        episode is None
        or episode.status not in {"escalated", "intervening"}
        or not episode.fallback_id
        or fallback_blocking_work(project, clip)
        or episode.fallback_id not in {c.id for c in selectable_candidates(project, clip)}
    ):
        return False
    progress = quality_progress(project, clip, readiness_items=[])
    return bool(
        progress.initial.generations >= 1
        and progress.initial.reviews >= 1
        and fallback_selection_plan(project, clip)
    )


def fallback_settlement_ready(project: StudioProject, clip: Clip) -> bool:
    """Return whether bounded recovery may stop at an existing reviewed fallback."""
    episode = clip.quality_episode
    if fallback_blocking_work(project, clip):
        return False
    progress = quality_progress(project, clip, readiness_items=[])
    return bool(
        progress.initial.generations >= 1
        and progress.initial.reviews >= 1
        and (bool(episode and episode.fallback_id) or quality_budget_exhausted(project, clip))
        and fallback_selection_plan(project, clip)
    )
