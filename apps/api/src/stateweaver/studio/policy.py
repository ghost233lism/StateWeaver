"""Runtime policy values injected into versioned role instructions and tools."""

from __future__ import annotations

from typing import Any

from stateweaver.providers.video_http_config import HTTP_VIDEO_PROTOCOL, VideoHTTPConfig

from .contracts import StudioProject
from .harness import video_extra_limit

NEW_ACTIONS = {
    "delegate_visual_analysis",
    "complete_visual_analysis",
    "supply_repair_references",
    "add_asset_views",
    "approve_reference_region",
}


def project_video_http(project: StudioProject) -> VideoHTTPConfig | None:
    for service in project.api_config_snapshot.get("services", []):
        if service.get("protocol") == HTTP_VIDEO_PROTOCOL and service.get("enabled"):
            return VideoHTTPConfig.model_validate(service["capabilities"]["video_http"])
    return None


def runtime_policy(project: StudioProject) -> dict[str, Any]:
    video = project_video_http(project)
    policy: dict[str, Any] = {
        "version": project.harness_version,
        "handoff_policy": project.handoff_policy,
        "observed_handoffs_require_selected_source": True,
        "asset_states_require_reference_purpose": True,
        "video_ordinary_repairs": project.config.repairs_per_clip,
        "video_executive_additions": video_extra_limit(project),
        "image_ordinary_repairs": project.config.repairs_per_reference,
        "image_executive_additions": project.config.executive_extra_attempts,
        "editing_allowed": video is None or video.edit_submit_path is not None,
        "production_asset_additions": True,
        "each_extra_needs_new_reference": True,
        "review": {
            "sessions_per_candidate": 1,
            "frames_per_sheet": 4,
            "jpeg_quality": 95,
            "images_per_call": 6,
            "session_image_limit": None,
        },
    }

    policy["production_lifecycle"] = {
        "version": "coherent-v1",
        "candidate_owned_reviews": True,
        "final_selection_requires_all_candidate_work_terminal": True,
        "runtime_waits_outside_creative_checkpoints": True,
        "quality_attempts": [
            1,
            project.config.repairs_per_clip,
            video_extra_limit(project),
        ],
        "selection_assessment_required": [
            "story_coverage",
            "identity_consistency",
            "visual_quality",
        ],
        "assessment_categories": [
            "met",
            "minor_deviation",
            "major_deviation",
            "unknown",
        ],
        "assessment_requires_inspected_candidate_evidence": True,
        "system_fallback_after_decision_recovery_exhausted": True,
        "quality_attempt_counts_are_maxima": True,
        "fallback_on_no_progress_or_prerequisite_failure": True,
        "unsubmitted_repair_reservations_cancel_on_fallback": True,
        "technical_failure_preserves_partial_results": True,
        "reference_coverage_approval_tool": "approve_reference_coverage",
    }
    policy["predecessor_conditioning"] = {
        "policy": "director-paired-v1",
        "video_and_latent_paired": True,
        "choice_field": "use_previous_clip_reference",
        "preserve_actual_state_and_seam_review": True,
        "off_means_no_opening_composition_lock": True,
    }
    if video is not None:
        policy["video_backend"] = {
            "protocol": HTTP_VIDEO_PROTOCOL,
            "reference_policy": video.reference_policy,
            "sends_audio_references": "audio" in video.inputs,
            "h3_latent_available": False,
        }
    policy["set_director_policy"] = project.set_director_policy
    policy["planning_context_policy"] = project.planning_context_policy
    policy["automatic_reference_draft"] = True
    policy["continuity_check_fields"] = [
        "action",
        "space",
        "camera",
        "completed_actions",
        "unknowns",
    ]
    policy["review"]["work_packet"] = "get_review_work_packet"
    policy["review"]["same_context_image_reuse"] = True
    return policy


DESCRIPTIONS = {
    "delegate_visual_analysis": (
        "Queue independent visual analysis tasks: ONE candidate version per child. Multiple "
        "candidate_ids fan out into separate tasks sharing group_id. A predecessor listed "
        "alongside successors contributes only their existing tail/seam evidence, never its "
        "whole-video evidence. Name a question and relevant revision_ids/media_ids; do not "
        "combine other candidates' frames. Over 50 images is rejected before starting. "
        "source_task_id may link a failed task; preserve it instead of restarting oversized work. "
        "Children inspect only their assignment; the executive "
        "may use their traceable viewed evidence and retains final authority. System "
        "concurrency is separate from video generation; excess tasks queue."
    ),
    "complete_visual_analysis": (
        "Return this task's conclusion, findings with actual viewed image IDs and timestamps, "
        "and all unconfirmed coverage/limitations. Cannot select candidates or mutate assets. "
        "Read review/task_id first."
    ),
    "supply_repair_references": (
        "For the current pending executive repair_attempt_id, provide newly generated, "
        "inspected, selected revision_ids and defect-specific guidance. Each addition needs "
        "its own new material generated after registration, actually used in the submitted "
        "reference plan. Read references/clip_id first."
    ),
    "add_asset_views": (
        "Executive appends new view names to an existing asset with a reason; keep earlier "
        "views and submitted inputs. Generate and inspect the needed state/view, then publish "
        "through current selection."
    ),
    "create_asset": (
        "Executive creates a logical or auxiliary asset. During production supply reason and "
        "published_scope with set_ids/clip_ids. purpose: story, action, layout, target_frame "
        "or auxiliary. Auxiliary images are planned guidance, never observations. The "
        "confirmed screenplay stays frozen."
    ),
    "repair_clip": (
        "Register or revise an unsubmitted repair without double-spending. Read "
        "quality_progress. repair_plan contains defects, preserve, change, "
        "source_candidate_id and mode regenerate|edit. Editing uses the complete reviewed "
        "CURRENT candidate as Video 1, drops the predecessor tail video, and retains the "
        "predecessor endpoint image/latent anchor. Optional duration/title/description adjust "
        "only an unsubmitted plan. Technical retries reuse the reservation."
    ),
    "save_reference_plan": (
        "Save exact selected image/audio inputs for this Clip. Regeneration retains the "
        "predecessor true endpoint as Picture 1 and real tail 73 frames as Video 1. Explicit "
        "editing uses the full current candidate as Video 1 instead of that tail; preserve "
        "Picture 1 and endpoint latent anchoring. Image state_role guidance is for published "
        "auxiliary assets; target identifies a planned ending, not an observed fact or hard "
        "ending lock. Use references/clip_id read_token and expected_plan_id."
    ),
    "prepare_media": (
        "Prepare an allowed local excerpt as evidence or a reference. Regeneration "
        "automatically uses the predecessor's real tail 73 frames. Full editing sources bind "
        "source_candidate_id directly in video_references, role edit_source; do not crop or "
        "shorten them."
    ),
    "resolve_quality": (
        "For coherent-v1 Clips, executive may select an existing reviewed playable compromise "
        "before ordinary repairs or takeover escalation are exhausted. Additional generation "
        "allowances still require completed ordinary generations AND reviews. "
        "Use quality_progress for limits and pending "
        "reservations. Each extra generation must use a new inspected reference generated "
        "after its registration. Supply defects/preserve/change/fallback and concrete "
        "guidance; delegated actual image evidence is permitted. Original rejections remain "
        "unchanged."
    ),
    "freeze_production": (
        "Open production after Set handoffs. Do not wait for all Clip plans or materials. "
        "Each Clip checks its own prerequisites. New scoped assets/states/views may "
        "subsequently be added by the executive; the screenplay stays frozen."
    ),
}
