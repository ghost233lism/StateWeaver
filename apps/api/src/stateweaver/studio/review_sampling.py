"""Freeze review density when queuing, and retain prior density during focused follow-up."""

from __future__ import annotations

from typing import Any

from .contracts import Candidate, ReviewSampling, StudioProject


def review_sampling(project: StudioProject, candidate: Candidate | None = None) -> dict[str, Any]:
    rates = project.config.review_sampling or ReviewSampling()
    if candidate and candidate.analysis.get("sampled_frame_indices"):
        saved = candidate.analysis.get("sampling", {})
        # Historical prepared evidence predates configurable density.
        rates = ReviewSampling(fps=saved.get("fps", 2), boundary_fps=saved.get("boundary_fps", 8))
    return {"version": "dense-v1", **rates.model_dump()}
