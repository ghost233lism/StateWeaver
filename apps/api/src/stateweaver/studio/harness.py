"""Versioned task policy shared by the supervisor, tools and immutable job inputs."""

from __future__ import annotations

from .contracts import Actor, StudioProject

VIDEO_EXECUTIVE_ADDITIONS = 2
REVIEW_IMAGE_LIMIT = 6
CANDIDATE_REVIEW = "candidate-session-v2"
QUAD_EVIDENCE = "quad-native-v1"


def video_extra_limit(project: StudioProject) -> int:
    value = project.config.video_executive_extra_attempts
    return min(value, VIDEO_EXECUTIVE_ADDITIONS) if value is not None else VIDEO_EXECUTIVE_ADDITIONS


def task_effort(project: StudioProject, actor: Actor) -> str | None:
    """Honor explicit overrides; classify persisted work rather than model prose."""
    if project.config.reasoning_effort is not None:
        return project.config.reasoning_effort
    from .production_efficiency import effort

    return effort(project, actor)
