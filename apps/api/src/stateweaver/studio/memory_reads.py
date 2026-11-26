"""SQL authority predicate shared by paged projections and scoped repositories."""

from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import aliased

from stateweaver.persistence.models import StudioEntityRow


def effective_memory_condition() -> Any:
    """Same authority rule as memory.selected_source, applied before pagination."""
    row = StudioEntityRow
    candidate, clip, actor = (aliased(StudioEntityRow) for _ in range(3))
    selected = (
        select(candidate.id)
        .join(
            clip,
            and_(
                clip.project_id == candidate.project_id,
                clip.kind == "clips",
                clip.id == candidate.payload["clip_id"].as_string(),
            ),
        )
        .where(
            candidate.project_id == row.project_id,
            candidate.kind == "candidates",
            candidate.id == row.payload["source_candidate_id"].as_string(),
            clip.payload["archived_at"].as_string().is_(None),
            clip.payload["accepted_candidate_id"].as_string() == candidate.id,
        )
        .correlate(row)
        .exists()
    )
    approved = and_(
        select(actor.id)
        .where(
            actor.project_id == row.project_id,
            actor.kind == "actors",
            actor.id == row.payload["approved_by"].as_string(),
            actor.payload["role"].as_string() == "executive",
        )
        .correlate(row)
        .exists(),
        func.json_array_length(row.payload, "$.approved_state_ids") > 0,
        func.json_array_length(row.payload, "$.approved_attributes") > 0,
        row.payload["source_job_id"].as_string().is_not(None),
    )
    ordinary = and_(
        row.payload["kind"].as_string() != "observation",
        func.coalesce(row.payload["basis"].as_string(), "plan") != "observed",
    )
    return and_(
        func.coalesce(row.status, "effective") == "effective",
        func.coalesce(row.payload["section"].as_string(), "legacy") != "log",
        or_(ordinary, approved, and_(row.payload["approved_by"].as_string().is_(None), selected)),
    )
