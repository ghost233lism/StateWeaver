"""Typed role tool arguments; domain services remain the write authority."""

from __future__ import annotations

import json
from types import GenericAlias
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, create_model, field_validator

from stateweaver.studio.contracts import (
    AssetHandoffPlan,
    CandidateSelectionAssessment,
    ClipAssetPlan,
    DelegateApproval,
    DelegateReport,
    DelegateRequest,
    EndState,
    EventExecution,
    EventObservation,
    MaterialRequest,
    MemoryScope,
    MemorySource,
    ProposalRequirement,
    ReferenceConflict,
    ReferenceCoverageSource,
    RepairGuidance,
    RepairPlan,
    ReviewFindingAssessment,
    ScreenplayDraft,
    SetDurationBudget,
    SourceStateBoundary,
    StateEvolutionInput,
    StatePreparationPlan,
    StateReferencePurpose,
    StoryAnchor,
    StoryAttribute,
    TimedMediaBinding,
    VideoReferenceInput,
    VisualFinding,
)
from stateweaver.studio.identity_bootstrap import IdentityBootstrapInput

Text = Annotated[str, Field(min_length=1)]


class NestedArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Arguments(NestedArguments):
    model_config = ConfigDict(extra="forbid")

    @field_validator(
        "report", "selection_assessment", "final_end_state", mode="before", check_fields=False
    )
    @classmethod
    def decode_object_argument(cls, value: Any) -> Any:
        """Accept JSON object envelopes without relaxing the typed business contract."""
        if isinstance(value, str):
            try:
                decoded = json.loads(value)
            except ValueError:
                return value
            if isinstance(decoded, dict):
                return decoded
        return value

    idempotency_key: str = Field(min_length=1, max_length=128)
    read_token: Text = Field(
        description=(
            "Use the current read_token or the required phase's complete context_token. "
            "Re-read on a relevant conflict."
        )
    )


def model(name: str, fields: dict[str, Any], *, command: bool = False) -> type[BaseModel]:
    return create_model(name, __base__=Arguments if command else NestedArguments, **fields)


EventInput = model(
    "EventInput",
    {
        "id": (str | None, None),
        "title": (Text, ...),
        "description": (str, ""),
        "order": (float, ...),
        "story_clock": (str | None, None),
        "source_fact_ids": (list[str], []),
        "requirement_ids": (list[str], []),
        "preconditions": (dict[str, str], {}),
        "results": (dict[str, str], {}),
        "narrative_mode": (Literal["forward", "flashback", "replay", "time_jump"], "forward"),
        "replay_of_event_id": (str | None, None),
        "completion_required": (bool, True),
    },
)
SetInput = model(
    "ProductionSetInput",
    {
        "duration_budget": (SetDurationBudget | None, None),
        "id": (str | None, None),
        "title": (Text, ...),
        "description": (Text, ...),
        "story_time": (float, ...),
        "depends_on": (list[str], []),
        "asset_handoffs": (list[AssetHandoffPlan], []),
        "story_goal": (str, ""),
        "entry_state": (str, ""),
        "exit_state": (str, ""),
        "asset_ids": (list[str], []),
        "story_event_ids": (list[str], []),
    },
)
ClipInput = model(
    "ClipInput",
    {
        "title": (Text, ...),
        "description": (Text, ...),
        "duration": (Annotated[float, Field(ge=5, le=15.083333333333334)], ...),
        "story_time": (float | None, None),
        "story_end_time": (float | None, None),
        "story_event_ids": (list[str], ...),
        "requirement_ids": (list[str], []),
        "event_execution": (list[EventExecution], []),
        "starts_at": (StoryAnchor, ...),
        "ends_at": (StoryAnchor | None, None),
        "asset_plan": (list[ClipAssetPlan], ...),
        "speaking_asset_ids": (list[str], []),
        "continuity": (dict[str, str], {}),
        "plan_sources": (list[MemorySource], []),
        "replaces_clip_ids": (list[str], []),
        "state_notes": (list[str], []),
    },
)
MemoryInput = model(
    "MemoryInput",
    {
        "text": (Text, ...),
        "kind": (Literal["plan", "observation", "constraint"], ...),
        "section": (
            Literal[
                "events",
                "directives",
                "set_shared",
                "clip_plan",
                "clip_actual",
                "clip_continuation",
            ],
            ...,
        ),
        "basis": (Literal["fact", "plan", "observed", "unknown", "provisional"], ...),
        "scope": (MemoryScope, ...),
        "attributes": (list[StoryAttribute], []),
        "sources": (list[MemorySource], []),
        "reason": (Text, ...),
        "source_candidate_id": (str | None, None),
        "supersedes_ids": (list[str], []),
        "record_key": (str, ""),
        "state_boundary": (SourceStateBoundary | None, None),
    },
)
FocusInterval = model(
    "FocusInterval",
    {
        "start_seconds": (Annotated[float, Field(ge=0)], ...),
        "end_seconds": (Annotated[float, Field(gt=0)], ...),
        "fps": (Annotated[float, Field(ge=2, le=8)], 8),
    },
)
VisualTaskInput = model(
    "VisualTaskInput",
    {
        "question": (Text, ...),
        "candidate_ids": (list[str], []),
        "revision_ids": (list[str], []),
        "media_ids": (list[str], []),
        "source_task_id": (str | None, None),
    },
)
F: dict[str, dict[str, Any]] = {
    "delegate_tasks": {"tasks": (list[DelegateRequest], ...)},
    "complete_delegate_task": {"task_id": (Text, ...), "report": (DelegateReport, ...)},
    "request_delegate_followup": {
        "task_id": (Text, ...),
        "expected_task_version": (int, ...),
        "question": (Text, ...),
        "evidence_policy": (Literal["current", "records-only"] | None, None),
        "refresh_authorization": (
            Annotated[
                bool,
                Field(
                    description=(
                        "Only for material_preparation: refresh a changed target/source "
                        "within the original authorized scope and views. State the current "
                        "requirement in question. Existing job/receipt guards still apply; "
                        "task/actor IDs, conversation, outputs and allowances stay unchanged."
                    )
                ),
            ],
            False,
        ),
    },
    "approve_delegate_results": {"approvals": (list[DelegateApproval], ...)},
    "acknowledge_executive_inbox": {"item_ids": (list[str], ...)},
    "save_executive_document": {
        "key": (Text, ...),
        "expected_revision": (int, ...),
        "title": (Text, ...),
        "text": (str, ...),
        "source_refs": (list[str], []),
    },
    "finish_initial_planning": {"reason": (Text, ...)},
    "adopt_prepared_reference": {
        "job_id": (Text, ...),
        "state_id": (Text, ...),
        "view": (Text, ...),
        "reason": (Text, ...),
    },
    "delegate_visual_analysis": {"tasks": (GenericAlias(list, VisualTaskInput), ...)},
    "complete_visual_analysis": {
        "task_id": (Text, ...),
        "conclusion": (Text, ...),
        "findings": (list[VisualFinding], []),
        "limitations": (list[str], []),
    },
    "write_memory": {
        "scope": (Literal["global", "set"], ...),
        "memory_revision": (int, ...),
        "entry": (MemoryInput, ...),
        "asset_state_ids": (list[str], []),
    },
    "correct_memory_scope": {
        "entry_id": (Text, ...),
        "scope": (MemoryScope, ...),
        "memory_revision": (int, ...),
        "reason": (Text, ...),
    },
    "plan_story_events": {"events": (GenericAlias(list, EventInput), ...), "reason": (str, "")},
    "prepare_sets": {"sets": (GenericAlias(list, SetInput), ...)},
    "set_duration_budget": {
        "set_id": (Text, ...),
        "expected_budget_revision": (Annotated[int, Field(ge=0)], ...),
        "budget": (SetDurationBudget, ...),
        "reason": (Text, ...),
    },
    "configure_set_dependencies": {
        "set_id": (Text, ...),
        "expected_dependency_revision": (int, ...),
        "depends_on": (list[str], ...),
        "asset_handoffs": (list[AssetHandoffPlan], ...),
        "reason": (Text, ...),
    },
    "configure_state_preparation": {
        "state_id": (Text, ...),
        "expected_preparation_revision": (int, ...),
        "preparation": (StatePreparationPlan, ...),
    },
    "confirm_set_handoff": {
        "set_id": (Text, ...),
        "expected_dependency_revision": (int, ...),
        "handoff_id": (Text, ...),
        "observation_ids": (list[str], ...),
        "reference_ids": (list[str], []),
        "reason": (Text, ...),
    },
    "adopt_observed_reference": {
        "state_id": (Text, ...),
        "observation_id": (Text, ...),
        "media_id": (Text, ...),
        "view": (Text, ...),
        "reason": (Text, ...),
    },
    "publish_set_context": {
        "set_id": (Text, ...),
        "story_goal": (Text, ...),
        "entry_state": (Text, ...),
        "exit_state": (Text, ...),
        "asset_ids": (list[str], ...),
        "story_event_ids": (list[str], ...),
        "reason": (Text, ...),
    },
    "plan_clips": {
        "clips": (GenericAlias(list, ClipInput), ...),
        "mode": (
            Literal["replace_unselected_suffix", "append_unplanned_suffix"],
            "replace_unselected_suffix",
        ),
        "complete": (bool, True),
        "reason": (str, ""),
    },
    "update_clip_context": {},
    "create_asset": {
        "name": (Text, ...),
        "kind": (Text, ...),
        "description": (Text, ...),
        "required_views": (list[str], []),
        "purpose": (Literal["story", "action", "layout", "target_frame", "auxiliary"], "story"),
        "published_scope": (MemoryScope | None, None),
        "reason": (str, ""),
    },
    "add_asset_views": {"asset_id": (Text, ...), "views": (list[str], ...), "reason": (Text, ...)},
    "supply_repair_references": {
        "clip_id": (Text, ...),
        "repair_attempt_id": (Text, ...),
        "revision_ids": (list[str], ...),
        "reason": (Text, ...),
    },
    "create_state": {
        "asset_id": (Text, ...),
        "label": (Text, ...),
        "description": (Text, ...),
        "story_time": (float, 0),
        "is_base": (bool, False),
        "preparation": (StatePreparationPlan | None, None),
        "starts_at": (StoryAnchor | None, None),
        "ends_at": (StoryAnchor | None, None),
        "attributes": (list[StoryAttribute], []),
        "evolution": (StateEvolutionInput | None, None),
        "reference_purpose": (StateReferencePurpose | None, None),
        "excluded_event_ids": (list[Text], []),
    },
    "record_state_evolution": {
        "state_id": (Text, ...),
        "expected_evolution_revision": (int, ...),
        "evolution": (StateEvolutionInput, ...),
    },
    "correct_state_scope": {
        "state_id": (Text, ...),
        "starts_at": (StoryAnchor | None, ...),
        "ends_at": (StoryAnchor | None, ...),
        "excluded_event_ids": (list[Text] | None, None),
        "reason": (Text, ...),
    },
    "generate_reference": {
        "state_id": (Text, ...),
        "view": (Text, ...),
        "prompt": (Text, ...),
        "source_policy": (Literal["auto", "explicit"], "auto"),
        "parent_id": (str | None, None),
        "source_revision_ids": (list[str], []),
        "source_observation_ids": (list[str], []),
        "reason": (str, ""),
        "change": (str, ""),
        "preserve": (list[str], []),
        "composition": (str, ""),
        "reference_roles": (dict[str, str], {}),
        "orientation": (Literal["portrait", "landscape", "square"] | None, None),
        "repair_plan": (RepairPlan | None, None),
    },
    "select_reference": {"revision_id": (Text, ...), "reason": (Text, ...)},
    "review_reference": {
        "revision_id": (Text, ...),
        "reason": (Text, ...),
        "decision": (Literal["accept", "reject"], ...),
        "repair_plan": (RepairPlan | None, None),
    },
    "prepare_reference": {
        "revision_id": (Text, ...),
        "purpose": (Text, ...),
        "clip_id": (str | None, None),
        "preferred_view": (str | None, None),
        "crop": (tuple[float, float, float, float] | None, None),
        "full_board_reason": (str | None, None),
        "region_approval_id": (str | None, None),
    },
    "approve_reference_region": {
        "revision_id": (Text, ...),
        "clip_id": (Text, ...),
        "crop": (tuple[float, float, float, float], ...),
        "purpose": (Text, ...),
        "reason": (Text, ...),
    },
    "approve_reference_coverage": {
        "revision_id": (Text, ...),
        "clip_id": (Text, ...),
        "source_bindings": (list[ReferenceCoverageSource], ...),
        "evidence_media_ids": (list[str], ...),
        "purpose": (Text, ...),
        "reason": (Text, ...),
        "unknowns": (list[str], []),
    },
    "prepare_media": {
        "source_candidate_id": (Text, ...),
        "kind": (Literal["video", "audio", "image"], ...),
        "start_seconds": (float, ...),
        "end_seconds": (float | None, None),
        "purpose": (Text, ...),
    },
    "save_reference_plan": {
        "clip_id": (Text, ...),
        "use_previous_clip_reference": (bool | None, None),
        "expected_plan_id": (str | None, None),
        "preparation_id": (str | None, None),
        "reason": (str, ""),
        "bindings": (list[VideoReferenceInput], ...),
        "video_references": (list[TimedMediaBinding], []),
        "audio_references": (list[TimedMediaBinding], []),
        "conflicts": (list[ReferenceConflict], []),
    },
    "finalize_reference_plan": {
        "reference_plan_id": (Text, ...),
        "prompt": (Text, ...),
        "prompt_reconciliation": (Text, ...),
        "conflict_check": (Text, ...),
        "continuity_check": (dict[str, str], {}),
    },
    "generate_clip": {
        "clip_id": (Text, ...),
        "reference_plan_id": (Text, ...),
        "latent_continuation_mode": (Literal["off", "endpoint-anchor-v1"] | None, None),
        "use_previous_clip_reference": (bool | None, None),
    },
    "analyze_candidate": {
        "candidate_id": (Text, ...),
        "instructions": (str, ""),
        "focus_intervals": (GenericAlias(list, FocusInterval), []),
    },
    "review_candidate": {
        "candidate_id": (Text, ...),
        "decision": (Literal["accept", "reject"], ...),
        "reason": (Text, ...),
        "issues": (list[str], []),
        "repair_plan": (RepairPlan | None, None),
        "observed_asset_states": (dict[str, str], {}),
        "selection_assessment": (CandidateSelectionAssessment | None, None),
        "event_observations": (list[EventObservation], []),
    },
    "repair_clip": {
        "clip_id": (Text, ...),
        "reason": (Text, ...),
        "duration": (float | None, None),
        "title": (str | None, None),
        "description": (str | None, None),
        "repair_plan": (RepairPlan | None, None),
    },
    "resolve_quality": {
        "clip_id": (str | None, None),
        "state_id": (str | None, None),
        "view": (str | None, None),
        "decision": (Literal["retry", "select"], ...),
        "reason": (Text, ...),
        "extra_attempts": (int | None, None),
        "fallback_id": (str | None, None),
        "candidate_id": (str | None, None),
        "revision_id": (str | None, None),
        "deviations": (list[str], []),
        "observed_asset_states": (dict[str, str], {}),
        "repair_plan": (RepairPlan | None, None),
    },
    "plan_voice": {"asset_id": (Text, ...), "clip_id": (Text, ...), "reason": (Text, ...)},
    "establish_voice": {
        "asset_id": (Text, ...),
        "candidate_id": (Text, ...),
        "start_seconds": (float, ...),
        "end_seconds": (float, ...),
        "reason": (Text, ...),
    },
    "select_voice": {
        "asset_id": (Text, ...),
        "voice_reference_id": (Text, ...),
        "reason": (Text, ...),
    },
    "approve_asset_observation": {
        "asset_id": (Text, ...),
        "preparation_job_id": (Text, ...),
        "state_ids": (list[str], ...),
        "attributes": (list[str], ...),
        "purpose": (Text, ...),
        "text": (Text, ...),
        "reason": (Text, ...),
        "scope": (MemoryScope | None, None),
    },
    "propose_global": {
        "text": (Text, ...),
        "affected_clip_ids": (list[str], []),
        "existing_proposal_id": (str | None, None),
        "issue_key": (str | None, None),
        "material_requests": (list[MaterialRequest], []),
    },
    "resolve_proposal": {
        "proposal_id": (Text, ...),
        "resolution": (Text, ...),
        "status": (Literal["accepted", "in_progress", "applied", "rejected"], ...),
        "affected_clip_ids": (list[str] | None, None),
        "requirements": (list[ProposalRequirement] | None, None),
        "evidence_ids": (list[str], []),
    },
    "assess_revision": {
        "issue": (Text, ...),
        "reason": (Text, ...),
        "assessments": (dict[str, str], ...),
    },
    "reconcile_job": {
        "job_id": (Text, ...),
        "provider_job_id": (str | None, None),
        "resolution": (Literal["resume", "failed", "abandon_image_result"], "resume"),
        "acknowledge_unretrievable_result": (bool, False),
        "evidence": (Text, ...),
    },
    "set_status": {
        "status": (Literal["waiting", "blocked"], ...),
        "summary": (Text, ...),
        "category": (Literal["technical"] | None, None),
        "job_id": (str | None, None),
    },
    "freeze_production": {},
    "finish_set": {},
    "assemble": {},
    "finish_project": {},
}
F["update_clip_context"] = {
    "identity_bootstrap": (IdentityBootstrapInput | None, None),
    "requirement_ids": (list[str] | None, None),
    "event_execution": (list[EventExecution] | None, None),
    "clip_id": (Text, ...),
    "reason": (Text, ...),
    "duration": (float | None, None),
    **{
        key: (field.annotation, None)
        for key, field in ClipInput.model_fields.items()
        if key
        not in {
            "title",
            "description",
            "duration",
            "speaking_asset_ids",
        }
    },
    "state_readiness": (Literal["ready", "needs_verification"] | None, None),
}
F["patch_reference_plan"] = {
    "reference_plan_id": (Text, ...),
    "reason": (Text, ...),
    "use_previous_clip_reference": (bool | None, None),
    "bindings": (list[VideoReferenceInput] | None, None),
    "video_references": (list[TimedMediaBinding] | None, None),
    "audio_references": (list[TimedMediaBinding] | None, None),
    "conflicts": (list[ReferenceConflict] | None, None),
    "prompt": (str | None, None),
}
STATE_CONFIRMATION_FIELDS: dict[str, Any] = {
    "final_end_state": (EndState | None, None),
    "end_state_reason": (str, ""),
    "end_state_media_ids": (list[str], []),
    "visible_state_ids": (list[str], []),
}
for action in ("review_candidate", "resolve_quality"):
    F[action].update(STATE_CONFIRMATION_FIELDS)
F["review_candidate"].update(
    {
        "repair_guidance": (list[RepairGuidance], []),
        "findings": (list[VisualFinding], []),
        "evidence_media_ids": (list[str], []),
        "limitations": (list[str], []),
        "batch_assessments": (list[ReviewFindingAssessment], []),
    }
)
F["finish_review_batch"] = {
    **STATE_CONFIRMATION_FIELDS,
    "candidate_id": (Text, ...),
    "batch_id": (Text, ...),
    "reason": (Text, ...),
    "findings": (list[VisualFinding], []),
    "limitations": (list[str], []),
}
F["request_review_batch"] = {
    "candidate_id": (Text, ...),
    "media_ids": (list[str], []),
    "question": (Text, ...),
    "restart_batch_id": (str | None, None),
}
F["save_clip_preparation"] = {
    "clip_id": (Text, ...),
    "expected_preparation_id": (str | None, None),
    "enabled": (bool | None, None),
    "prompt_draft": (str | None, None),
    "action_notes": (str | None, None),
    "bindings": (list[VideoReferenceInput] | None, None),
    "audio_references": (list[TimedMediaBinding] | None, None),
    "pending_requirements": (list[str] | None, None),
    "reason": (Text, ...),
}
F["confirm_candidate_state"] = {
    **STATE_CONFIRMATION_FIELDS,
    "candidate_id": (Text, ...),
    "final_end_state": (EndState, ...),
    "reason": (Text, ...),
}
ACTION_MODELS = {
    name: model("".join(part.title() for part in name.split("_")) + "Input", fields, command=True)
    for name, fields in F.items()
}


class StoryScreenplayInput(Arguments, ScreenplayDraft):
    """New writers submit a story, with headings rendered by the application."""


ACTION_MODELS["save_screenplay"] = StoryScreenplayInput


def action_model(name: str) -> type[BaseModel]:
    return ACTION_MODELS[name]


WRITER = {"save_screenplay", "set_status"}
SET = {
    "write_memory",
    "update_clip_context",
    "plan_clips",
    "prepare_reference",
    "prepare_media",
    "save_reference_plan",
    "patch_reference_plan",
    "finalize_reference_plan",
    "save_clip_preparation",
    "finish_review_batch",
    "request_review_batch",
    "generate_clip",
    "analyze_candidate",
    "review_candidate",
    "confirm_candidate_state",
    "repair_clip",
    "propose_global",
    "set_status",
    "finish_set",
}
EXECUTIVE = (
    set(F)
    - (
        SET
        - {
            "write_memory",
            "update_clip_context",
            "prepare_media",
            "repair_clip",
            "confirm_candidate_state",
            "set_status",
        }
    )
    - {"save_screenplay"}
)
EXECUTIVE.discard("complete_visual_analysis")
EXECUTIVE.discard("complete_delegate_task")
EXECUTIVE.discard("adopt_prepared_reference")
from .delegation import ANALYST_ACTIONS, MATERIAL_ACTIONS  # noqa: E402

ROLE_ACTIONS = {
    "screenwriter": WRITER,
    "executive": EXECUTIVE,
    "set_director": SET,
    "visual_analyst": {"complete_visual_analysis", "set_status"},
    # The service-level delegation guard exposes review_candidate only for an
    # active candidate_review assignment scoped to exactly one candidate.
    "task_analyst": set(ANALYST_ACTIONS) | {"review_candidate"},
    "material_executor": set(MATERIAL_ACTIONS)
    | {"prepare_reference", "adopt_prepared_reference", "adopt_observed_reference"},
}
