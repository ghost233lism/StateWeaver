"""Compact actor inputs. Original records and validation fingerprints stay durable."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .contracts import Actor, Clip, StudioProject
from .memory import digest
from .repository import select_entities


def writer_packet(project: StudioProject, actor: Actor) -> dict[str, Any]:
    screenplay = project.screenplay.model_dump(mode="json")
    if screenplay.get("manuscript"):
        screenplay.pop("text", None)  # The rendered text duplicates the structured manuscript.
    result = {
        "id": project.id,
        "revision": project.revision,
        "status": project.status,
        "title": project.title,
        "brief": project.brief,
        "screenplay": screenplay,
        "you": actor.model_dump(include={"id", "role", "status", "goal"}),
        "user_messages": [
            {"id": item.id, "text": item.text, "created_at": item.created_at}
            for item in select_entities(project.transcript, actor_id=actor.id, subtype="user")
            if item.text != project.brief
        ],
        "workflow_version": project.workflow_version,
        "harness_version": project.harness_version,
    }
    return result


HISTORY_FIELDS = {
    "history",
    "review_history",
    "end_state_history",
    "handoff_history",
    "duration_history",
    "quality_accounting_history",
    "plan_change_history",
}
MACHINE_FIELDS = {
    "context_dependencies",
    "context_fingerprint",
    "dependency_fingerprint",
    "source_fingerprint",
    "progress_markers",
    "recovery_history",
    "role_journal",
    "stable_snapshot",
    "continuity_snapshot",
}


def clip_preparation(clip: Clip) -> dict[str, Any] | None:
    """Expose the exact editable draft without repeating its dependency snapshots."""
    preparation = clip.preparation
    if preparation is None:
        return None
    return preparation.model_dump(
        mode="json",
        include={
            "id",
            "revision",
            "parent_id",
            "enabled",
            "user_disabled",
            "status",
            "prompt_draft",
            "action_notes",
            "bindings",
            "audio_references",
            "pending_requirements",
            "invalidation_reasons",
            "reason",
            "actor_id",
            "stable_status",
            "continuity_status",
            "created_at",
        },
    )


def compact_records(
    records: list[dict[str, Any]], *, role_read: bool = False
) -> list[dict[str, Any]]:
    """Remove exact duplicate/current-history copies, never truncate creative prose."""
    history_fields = HISTORY_FIELDS
    if role_read:
        history_fields = history_fields | {"preparation_history", "dependency_history"}

    def project(value: Any) -> Any:
        if isinstance(value, list):
            return [project(item) for item in value]
        if not isinstance(value, dict):
            return value
        result = {
            # Summary values are counts, not the original history arrays.
            key: deepcopy(item) if key == "history_available" else project(item)
            for key, item in value.items()
            if key not in history_fields | MACHINE_FIELDS
        }
        removed = {key: len(value[key]) for key in history_fields if value.get(key)}
        if removed:
            existing = result.get("history_available")
            result["history_available"] = {
                **(existing if isinstance(existing, dict) else {}),
                **removed,
            }
        if result.get("manuscript") and "text" in result:
            result.pop("text")
        # The confirmation is authoritative; keep its actor/evidence/limitations,
        # with only one copy of the observed endpoint in this record.
        confirmation = result.get("confirmed_end_state")
        if confirmation and confirmation.get("end_state") == result.get("end_state"):
            result.pop("end_state")
        return result

    compact = [project(record) for record in records]
    return list({digest(record): record for record in compact}.values())
