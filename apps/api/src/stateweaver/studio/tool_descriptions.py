"""Versioned tool descriptions; Set is also the stored domain vocabulary."""

from __future__ import annotations

ALIASES = {"prepare_review": "analyze_candidate"}
PUBLIC = {v: k for k, v in ALIASES.items()}


DESCRIPTIONS = {
    "prepare_sets": (
        "Create the Set directory once: sets [{id?, title, description, story_time, "
        "depends_on?, story_goal, entry_state, exit_state, asset_ids, story_event_ids}]. "
        "Executive first publishes each continuous-space/time substory's goal and "
        "entry/exit states. Its director can plan immediately, before images or other Sets "
        "finish. Use planning/global read_token."
    ),
    "publish_set_context": (
        "Executive updates one Set's story_goal, entry_state, exit_state, assigned "
        "asset_ids/story_event_ids with a reason, retaining previous handoffs. Use "
        "planning/global read_token. Resolve cross-Set questions in applicable global story "
        "memory; directors cannot read one another's private work."
    ),
    "plan_clips": (
        "Plan this Set's sequential 5–15 second Clips from the executive handoff. Each Clip "
        "includes story_event_ids, starts_at/ends_at, asset_plan, speaking_asset_ids and "
        "continuity. Camera cuts inside a Clip are allowed; time/location jumps need "
        "another Set. Planning may continue with provisional/conflicting state scopes; only "
        "affected generation waits for executive resolution. Use planning/set_id "
        "read_token. mode=append_unplanned_suffix preserves ALL current Clip IDs and only "
        "adds subsequent unsubmitted actions, even while earlier Clips generate, await "
        "materials or undergo quality intervention. Use clips=[] with this mode to mark an "
        "already covered plan complete. Default mode=replace_unselected_suffix replaces "
        "the entire unselected suffix: include every still-needed unselected Clip and wait "
        "for its live video requests/quality intervention to finish. Selected Clips, original "
        "requests, candidates and quality allowances are preserved. complete=true "
        "requires every required Set event to be planned; missing events save a partial plan "
        "and return their exact IDs. Output registration and footage may follow planning."
    ),
    "freeze_production": (
        "Executive freezes the logical asset/Set directory after publishing story handoffs. "
        "Do not wait for every director's plan or every image. Individual Clip generation "
        "validates its own material/state prerequisites."
    ),
    "prepare_media": (
        "Prepare a local video/audio/image excerpt from an allowed candidate. Accepted "
        "predecessor's final 73 real frames (about 3.042 seconds) are prepared automatically "
        "on selection for Video 1; "
        "its real final frame is Picture 1. Other video inputs and failed current-Clip "
        "editing inputs are forbidden. Failed footage may be examined as evidence only. "
        "Read the exact completed job and evidence before reuse."
    ),
    "save_reference_plan": (
        "Save this Clip's exact selected image bindings and audio references before writing "
        "its prompt. From Clip 2 onward Picture 1 is the accepted predecessor's real final "
        "frame and Video 1 its final 73 real frames (about 3.042 seconds), auto-added when "
        "omitted. No other "
        "video inputs. Remaining slots are images/audio, at most 9 images and 12 mixed "
        "files. Use references/clip_id read_token, expected_plan_id when replacing, "
        "explicit purposes/state roles, exact preparation IDs and conflict resolutions."
    ),
    "analyze_candidate": (
        "Prepare local visual evidence for director self-review. Supply candidate_id and "
        "optional focus_intervals "
        "[{start_seconds,end_seconds,fps:2–8}]. Samples full Clip at configured density "
        "(default 1 fps), boundaries at configured density (default 4) "
        "fps, exact final frame and prior-tail/seam evidence. Returns job_id. Wait for "
        "completion, read ALL required_review_media_ids with read_media, then "
        "review_candidate. Supplemental evidence does not spend video corrections."
    ),
    "review_candidate": (
        "Director self-review after viewing ALL required_review_media_ids. Supply "
        "candidate_id, decision accept|reject, reason, evidence_media_ids, findings "
        "[{description,severity:minor|repair,evidence_media_ids,start_seconds,end_seconds?,"
        "category:identity|state|action|space|camera|other}], "
        "final_end_state {identity,action,space,camera,frame_media_id}, end_state_media_ids "
        "and visible_state_ids. Confirm only observed state. Minor nondisruptive findings "
        "may pass; repair findings block ordinary acceptance and rejection needs "
        "repair_plan {defects,preserve,change,source_candidate_id}. Keep audio/sampling "
        "limitations explicit. Use review/candidate_id read_token."
    ),
    "query_memory": (
        "Read own-Set and applicable executive-published global story memory, including its "
        "revisions. Other Sets' private work is inaccessible. Scope defaults global, or own "
        "set ID; * is limited to these two scopes. Follow next_cursor. Ask executive to "
        "coordinate cross-Set state questions."
    ),
    "compare_candidates": (
        "Compare existing local candidates, director conclusions, findings and inspected "
        "visual evidence. Use get_detail to read a candidate's complete record."
    ),
    "resolve_quality": (
        "Executive resolves an exhausted quality episode using inspected footage and "
        "director findings. Existing finite correction/extra-attempt/fallback rules apply. "
        "Supply retry with defects/preserve/change/fallback_id or explicit select with "
        "deviations. Preserve original verdicts. Use review/clip_id or review/state_id read_token."
    ),
}
