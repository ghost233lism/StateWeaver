"""Coarse indexed dependency lookup followed by exact in-transaction validation."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from stateweaver.persistence.models import StudioEntityRow, StudioPlanDependencyRow

LIVE_STATUSES = ("draft", "finalized")


def bucket(*values: str) -> str:
    return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode()).hexdigest()


def dependency_buckets(dependencies: list[dict[str, Any]]) -> set[str]:
    keys = {bucket("indexed")}
    if not dependencies:
        keys.add(bucket("all"))
    for dependency in dependencies:
        kind, filters = dependency["kind"], dependency["filters"]
        if "ids" in filters:
            keys.update(bucket("id", kind, str(i)) for i in filters["ids"])
        elif kind == "memory_entries" and "memory_context" in filters:
            scope = str(filters.get("parent_id", "global"))
            keys.add(bucket("parent", kind, scope))  # Unscoped entries affect this whole memory.
            for dimension, identifiers in filters["memory_context"].items():
                keys.update(bucket("memory", scope, dimension, str(i)) for i in identifiers)
        elif kind == "memory_entries" and "parent_id" in filters:
            keys.add(bucket("memory-any", str(filters["parent_id"])))
        elif "parent_id" in filters or "parent_ids" in filters:
            parents = filters.get("parent_ids", [filters.get("parent_id")])
            keys.update(bucket("parent", kind, str(i)) for i in parents)
        else:
            keys.add(bucket("kind", kind))
    return keys


def replace_index(session: Session, project_id: str, plan_id: str, payload: dict[str, Any]) -> None:
    session.execute(
        delete(StudioPlanDependencyRow).where(
            StudioPlanDependencyRow.project_id == project_id,
            StudioPlanDependencyRow.plan_id == plan_id,
        )
    )
    if payload.get("status") not in LIVE_STATUSES:
        return
    session.add_all(
        StudioPlanDependencyRow(project_id=project_id, plan_id=plan_id, bucket=key)
        for key in dependency_buckets(payload.get("context_dependencies", []))
    )


def affected_ids(session: Session, project_id: str, keys: set[str]) -> list[str]:
    indexed_ids = select(StudioPlanDependencyRow.plan_id).where(
        StudioPlanDependencyRow.project_id == project_id,
        StudioPlanDependencyRow.bucket.in_(keys | {bucket("all")}),
    )
    indexed = (
        select(StudioPlanDependencyRow.plan_id)
        .where(
            StudioPlanDependencyRow.project_id == project_id,
            StudioPlanDependencyRow.plan_id == StudioEntityRow.id,
        )
        .exists()
    )
    # Old live plans have no index until changed. Include only their IDs; do not
    # scan or deserialize completed history. New plans always write the index atomically.
    return list(
        session.scalars(
            select(StudioEntityRow.id).where(
                StudioEntityRow.project_id == project_id,
                StudioEntityRow.kind == "reference_plans",
                StudioEntityRow.status.in_(LIVE_STATUSES),
                or_(StudioEntityRow.id.in_(indexed_ids), ~indexed),
            )
        )
    )
