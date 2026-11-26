"""Candidate task packets, with exact submitted inputs and scoped story facts."""

from __future__ import annotations

from typing import Any

from .adjudication import actual_end_state
from .contracts import Candidate, ClipAssetPlan, StudioProject
from .memory import effective_entries
from .repository import select_entities


def review_context_records(p: StudioProject, candidate: Candidate) -> list[dict[str, Any]]:
    from .image_input_recovery import available_evidence, review_gaps

    clip = p.clips[candidate.clip_id]
    production_set = p.sets[clip.set_id]
    request = p.jobs[candidate.job_id].request
    text_only = request.get("video_input_policy") == "text-only"
    plan = clip.model_dump(
        include={
            "id",
            "set_id",
            "order",
            "title",
            "description",
            "duration",
            "asset_plan",
            "story_time",
            "story_end_time",
            "story_event_ids",
            "starts_at",
            "ends_at",
            "speaking_asset_ids",
            "continuity",
            "state_notes",
            "state_readiness",
            "plan_sources",
        },
        mode="json",
    )
    # Later reference selections/preparation edits must not replace this take's
    # plan, prompt or actual upload list in its review task packet.
    plan.update({key: request[key] for key in plan if key in request})
    asset_plan = [ClipAssetPlan.model_validate(value) for value in plan["asset_plan"]]
    asset_ids = {item.asset_id for item in asset_plan} | {b.asset_id for b in candidate.bindings}
    state_ids = {
        identifier
        for item in asset_plan
        for identifier in (item.start_state_id, item.end_state_id)
        if identifier
    } | {b.state_id for b in candidate.bindings}
    event_ids = set(plan["story_event_ids"])
    dimensions = {
        "asset_ids": sorted(asset_ids),
        "set_ids": [production_set.id],
        "clip_ids": [clip.id],
        "event_ids": sorted(event_ids),
    }
    manuscript = p.screenplay.manuscript
    records: list[dict[str, Any]] = [
        {
            "type": "task",
            "target_id": candidate.id,
            "phase": "review",
            "packet_version": 1,
        },
        {
            "type": "review_coverage",
            "available_media_ids": available_evidence(p, candidate),
            "coverage_gaps": [gap.model_dump(mode="json") for gap in review_gaps(p, candidate)],
        },
        {
            "type": "global_constraints",
            "value": {
                "user_brief": p.brief,
                "screenplay_hash": p.screenplay.confirmed_hash,
                "facts": [fact.model_dump(mode="json") for fact in p.screenplay.facts],
                "style": manuscript.style if manuscript else None,
                "screenplay_detail_scope": "screenplay",
            },
        },
        {
            "type": "set",
            "value": production_set.model_dump(
                include={
                    "id",
                    "title",
                    "description",
                    "story_goal",
                    "entry_state",
                    "exit_state",
                    "story_event_ids",
                },
                mode="json",
            ),
        },
        {"type": "clip_plan", "value": plan},
        {
            "type": "submitted_inputs",
            "value": {
                "id": candidate.job_id,
                "prompt": request.get("provider_prompt", request.get("prompt", candidate.prompt)),
                "reference_inputs": [] if text_only else request.get("reference_inputs", []),
                "video_inputs": [] if text_only else request.get("video_inputs", []),
                "audio_inputs": [] if text_only else request.get("audio_inputs", []),
                "latent_continuation": request.get("latent_continuation", {"mode": "off"}),
                "bindings": []
                if text_only
                else [binding.model_dump(mode="json") for binding in candidate.bindings],
                **(
                    {"video_input_policy": request["video_input_policy"]}
                    if "video_input_policy" in request
                    else {}
                ),
                "repair_plan": request.get("repair_plan"),
            },
        },
    ]
    if request.get("set_director_policy") == "efficient-v1":
        # The verdict must inspect the check pinned to this submission, not a
        # later draft's description or the project's current plan.
        records[-1]["value"]["continuity_check"] = request.get("continuity_check", {})
    if request.get("continuation_decision"):
        records[-1]["value"]["continuation_decision"] = request["continuation_decision"]
        records.append(
            {
                "type": "continuation_review_policy",
                "value": request["continuation_decision"],
                "instruction": "Check the actual seam and story/action state. When predecessor "
                "conditioning is off, an intentional camera cut is permitted: do not require "
                "the opening composition to match the preceding end frame.",
            }
        )
    if text_only:
        records.append(
            {
                "type": "local_reference_evidence",
                "value": {
                    "reference_inputs": request.get("reference_inputs", []),
                    "audio_inputs": request.get("audio_inputs", []),
                },
                "instruction": "These references are local evidence. The video API received "
                "only the submitted text.",
            }
        )
    for scope in ("global", production_set.id):
        records.extend(
            {"type": "memory", "scope": scope, "value": entry.model_dump(mode="json")}
            for entry in effective_entries(p, scope, context=dimensions, clip=clip)
        )
    if candidate.previous_candidate_id:
        previous = p.candidates[candidate.previous_candidate_id]
        end_state = actual_end_state(previous)
        records.append(
            {
                "type": "previous_selected_actual",
                "clip_id": previous.clip_id,
                "value": previous.model_dump(
                    include={
                        "id",
                        "media_id",
                        "confirmed_end_state",
                        "observed_asset_states",
                    },
                    mode="json",
                ),
                "continuation_end_state": request.get("previous_end_state")
                or (end_state.model_dump(mode="json") if end_state else None),
            }
        )
    for asset in select_entities(p.assets, ids=sorted(asset_ids)):
        records.append(
            {
                "type": "asset",
                "value": asset.model_dump(include={"id", "name", "kind", "description"}),
            }
        )
    for state in select_entities(p.states, ids=sorted(state_ids)):
        records.append(
            {
                "type": "state",
                "value": state.model_dump(
                    exclude={"revision_ids", "repair_episodes", "selected_revisions"},
                    mode="json",
                ),
            }
        )
        event_ids.update(anchor.event_id for anchor in (state.starts_at, state.ends_at) if anchor)
    for event in select_entities(p.story_events, ids=sorted(event_ids)):
        records.append(
            {
                "type": "story_event",
                "value": event.model_dump(exclude={"history"}, mode="json"),
            }
        )
    for binding in candidate.bindings:
        reference = p.references.get(binding.revision_id)
        if reference:
            records.append(
                {
                    "type": "review_reference",
                    "value": reference.model_dump(
                        include={
                            "id",
                            "state_id",
                            "view",
                            "media_id",
                            "source_observation_ids",
                        },
                        mode="json",
                    ),
                }
            )
    records.append(
        {
            "type": "review_candidate",
            "value": candidate.model_dump(
                include={
                    "id",
                    "clip_id",
                    "media_id",
                    "previous_candidate_id",
                    "analysis",
                    "end_state",
                    "confirmed_end_state",
                    "status",
                },
                mode="json",
            ),
        }
    )
    from .context_projection import compact_records

    return compact_records(records)
