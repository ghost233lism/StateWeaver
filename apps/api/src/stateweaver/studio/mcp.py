"""Stateless Streamable HTTP MCP tools, bound to one project and actor."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import json
import logging
import time
import traceback
from collections import OrderedDict
from pathlib import Path
from threading import RLock
from typing import Any, cast
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy.exc import OperationalError

from stateweaver.storage.cas import ContentAddressedStore
from stateweaver.studio.context_cache import guarded_read
from stateweaver.studio.contracts import (
    ActorSession,
    ContextPhase,
    PendingImageDelivery,
    StudioProject,
    ToolCommand,
)
from stateweaver.studio.delegation import actions_for, read_tools_for, records_only, task_for
from stateweaver.studio.mcp_inputs import ACTION_MODELS, ROLE_ACTIONS, action_model
from stateweaver.studio.mcp_results import error_result, native_result
from stateweaver.studio.mcp_results import result as tool_result
from stateweaver.studio.media_view import image_view
from stateweaver.studio.memory import effective_entries
from stateweaver.studio.production_reads import (
    clip_readiness,
    compare_candidates,
    reference_options,
)
from stateweaver.studio.quality_progress import quality_progress
from stateweaver.studio.queries import StudioQueries
from stateweaver.studio.repository import core_payload, select_entities
from stateweaver.studio.review_sessions import (
    REVIEW_TOOLS,
    actor_instruction_detail,
    actor_instruction_packet,
    assignment,
    guard_read,
    overview,
    reserve_image,
)
from stateweaver.studio.service import StudioService, add_item, get_actor, require
from stateweaver.studio.store import StudioError
from stateweaver.studio.task_context import full_context_required, read_required_context
from stateweaver.studio.tool_descriptions import (
    ALIASES,
    DESCRIPTIONS,
    PUBLIC,
)
from stateweaver.studio.visibility import Visibility
from stateweaver.studio_media.image_views import review_image_view, review_view_policy

# These are business arguments, not provider payload templates. Provider prompt
# knowledge stays in the versioned studio-media skills.
ACTIONS: dict[str, str] = {
    "delegate_tasks": (
        "Executive authorizes scoped production tasks. Types: candidate_review, "
        "repair_diagnosis, material_preparation, handoff_verification, revision_impact, "
        "state_conflict. Material tasks require one published state, approved views and "
        "exact sources. Repair diagnosis without a returned candidate may target one "
        "explicit Clip for planning advice only; it certifies no generated footage. "
        "Compatible exact goals deduplicate; no count ceiling."
    ),
    "complete_delegate_task": (
        "Submit this task's traceable report, viewed evidence and explicit gaps. "
        "Repair diagnosis maps every defect to change and material purpose; paraphrased "
        "defect descriptions are allowed. Use blocker only when this scoped task cannot "
        "finish. Pending executive decisions or downstream material/video work belong in "
        "limitations and suggested_actions, not a completed diagnosis blocker. Handoffs "
        "have separate target conclusions. Revision impact checks the current use index; "
        "metadata-only impact reports need no images and certify no footage. Revalidate "
        "changed usage in the same scoped task; downstream draft changes are not failures. "
        "No selection or global certification."
    ),
    "request_delegate_followup": (
        "Return an incomplete report to its SAME task/actor/native conversation. Use "
        "expected_task_version. For a changed material target/source use explicit "
        "refresh_authorization=true plus the current requirement in question, after all "
        "original jobs return. Keeps the exact scope, IDs, outputs and allowances."
    ),
    "approve_delegate_results": (
        "Executive approves complete reports using each task's review context_token, "
        "task version and report fingerprint. Independent items may succeed separately; "
        "a shared group_id is atomic. Final business permissions/evidence still apply."
    ),
    "acknowledge_executive_inbox": (
        "Acknowledge only explicit durable notification IDs already processed. Later "
        "notifications remain pending. This is not production progress."
    ),
    "save_executive_document": (
        "Save an indexed executive-owned operational note as needed. expected_revision "
        "is the DOCUMENT revision, not project revision. Keep evidence/business links and "
        "separate intended from executed decisions. Notes are not authoritative story "
        "memory or business progress."
    ),
    "finish_initial_planning": (
        "Publish/freeze initial catalog and Set handoffs, then archive planning into ONE "
        "lightweight production context. Do not wait for all images or Clip plans; "
        "no repeated production context rotation."
    ),
    "adopt_prepared_reference": (
        "Adopt this authorized material task's completed and actually inspected "
        "same-state crop as a declared view. Not newly generated reinforcement."
    ),
    "set_duration_budget": (
        "Executive updates one Set playback target and interval with budget revision "
        "and reason; requires planning/global context."
    ),
    "configure_set_dependencies": (
        "Executive replaces one Set's dependency declarations with expected_dependency_revision. "
        "Classify each asset as independent, shared fixed design, or observed inheritance. "
        "Observed plans need source_set_id/source_event_id, state_ids and required_attributes. "
        "depends_on means whole-Set completion; do not use it for event-level handoffs. "
        "Read complete planning/global for replacements. Only appending one new independent "
        "auxiliary or action-purpose handoff with one state and the sole event of one active Clip "
        "may use that "
        "Clip's complete planning read_token. Supply every original handoff's plan fields "
        "in the same array order, then the one addition. Omit confirmation keys; the server "
        "retains the original confirmations. Keep depends_on unchanged. Asset/state must "
        "already be published to that exact "
        "Set/Clip/event and included in its mandatory context. Keep expected_dependency_revision. "
        "Task or Set tokens do not qualify; all other changes still require global."
    ),
    "configure_state_preparation": (
        "Executive declares whether a state image is independent, waits for an observed "
        "source event, or is a transition_target design for the producing event. Supply "
        "expected_preparation_revision and preparation; read planning/state_id. "
        "An observed state requires selected actual footage and approved observation inputs."
    ),
    "confirm_set_handoff": (
        "After inspecting selected source-event footage, publish scoped approve_asset_observation "
        "records, inspect the exact selected reference_ids and confirm compatibility here. "
        "Supply set_id/handoff_id, expected_dependency_revision, observation_ids, reference_ids "
        "and reason after reading planning/global. No new image is needed if existing pictures "
        "match; source or reference changes invalidate this confirmation."
    ),
    "adopt_observed_reference": (
        "Reuse an inspected, approved real image as an exact selected asset/state/view without "
        "generating another image. Supply observation_id/media_id/state_id/view/reason and "
        "read references/state_id. Keep source timestamp, media hash and original versions."
    ),
    "save_clip_preparation": (
        "Save or revise an editable next-Clip preparation while its predecessor generates. "
        "Supply references/Clip context, expected_preparation_id and changed draft fields. "
        "No finalized inputs or speculative predecessor bindings; "
        "state changes invalidate the draft."
    ),
    "finish_review_batch": (
        "Complete the assigned isolated batch after viewing every assigned image. "
        "Provide findings and limitations; an actual end-frame batch also records final_end_state. "
        "The supervisor switches contexts after this receipt."
    ),
    "request_review_batch": (
        "Request a separate short recheck of 1–6 existing evidence images with a precise question. "
        "To recover an incomplete batch's exhausted image context, "
        "supply restart_batch_id and reason "
        "in question instead of media_ids. Never load images into the summary/planning context."
    ),
    "correct_memory_scope": (
        "{entry_id, scope: MemoryScope, memory_revision, reason, context_token}. "
        "Executive only, after reading planning/global context. Correct the applicable "
        "events, subjects or Sets/Clips of one effective non-screenplay memory. "
        "Creates a new revision with identical original content and preserved old scope; "
        "reason must explain the evidence and mapping. Scope dimensions combine with AND."
    ),
    "save_screenplay": (
        "{text: string, facts: [{text, kind: identity|event|ending|dialogue|detail}]}"
    ),
    "write_memory": (
        "{scope: global|set, memory_revision: int, entry: {text, kind: "
        "plan|observation|constraint, source_candidate_id?: string}, asset_state_ids?: "
        "string[]}"
    ),
    "create_asset": (
        "{name, kind: person|object|scene|other descriptive kind, description, "
        "required_views?: string[]}. Only during preparation; people automatically "
        "require close_up,full_body,three_view,expression_grid."
    ),
    "create_state": (
        "{asset_id, label, story_time: number, description}. Chronological story time; "
        "image corrections use generate_reference with parent_id instead."
    ),
    "generate_reference": (
        "{state_id, view, prompt, parent_id?: revision_id, "
        "source_policy?: auto|explicit, source_revision_ids?: revision_id[], "
        "reason?: string, change?: string, "
        "preserve?: string[], composition?: string, reference_roles?: {revision_id: purpose}, "
        "orientation?: portrait|landscape|square}. Returns "
        "durable image job. parent_id is the edited target's same-state revision. "
        "source_revision_ids may additionally reference other existing project assets "
        "(e.g. a character, prop or scene depicted inside a photograph); choose exact "
        "states/views and explain their intended roles in prompt. Cross-asset sources "
        "do not become revision parents or change the target asset's identity. "
        "source_policy=explicit requires a reason and disables automatic previous-state "
        "sources and same-state anchors: a NEW view uses only the explicitly supplied "
        "sources/approved observations, or text only when both are empty. An existing-view "
        "correction still includes its required same-state repair parent and preserves "
        "all correction counts; explicit is not a way to reset or omit that parent. "
        "The default auto policy retains the current automatic anchor behavior. "
        "Optional source_observation_ids use executive-approved screenshot details, preserving "
        "their exact sources. For a correction include repair_plan: {defects: string[], preserve: "
        "string[], change, source_revision_id?}. Ordinary corrections are finite; use "
        "resolve_quality when escalated. All new requests use high quality. Standard "
        "view aspect ratios are fixed by purpose; "
        "orientation controls custom views. Recipes/settings and actual ordered sources are saved."
    ),
    "prepare_reference": (
        "{revision_id, clip_id?: string, purpose, preferred_view?: string, crop?: "
        "[left,top,right,bottom], region_approval_id?: string, "
        "full_board_reason?: string}. Set director only. Creates a local preparation job. "
        "View the original with read_media before requesting normalized 0..1 crop coordinates. "
        "Prefer the requested selected single view; otherwise crop the needed complete panel. "
        "Invalid/unusable crop geometry falls back to a selected same-asset/same-state single "
        "view, then the original board with a recorded reason. full_board_reason explicitly "
        "requests the board for multi-angle needs. Does not generate an image or new revision. "
        "Read get_detail(scope='job:JOB_ID') and the actual media_id before generate_clip; "
        "use its resolved "
        "binding plus prepared_reference_id, and match the final prompt to that actual image. "
        "For a rejected but nonquarantined image, request executive approve_reference_region; "
        "supply region_approval_id, exact crop and clip_id. No whole-image fallback is allowed."
    ),
    "approve_reference_coverage": (
        "Executive only, coherent-v1 projects: approve explicitly inspected multi-asset coverage "
        "for an exact selected reference revision and Clip. Supply source_bindings with "
        "state/revision/media IDs and hashes, inspected evidence_media_ids, purpose, reason "
        "and unknowns. Provenance alone never proves coverage. Read references/clip_id first."
    ),
    "approve_reference_region": (
        "Approve only an actually inspected, nonquarantined region of a rejected reference "
        "for one clip_id. Provide revision_id, normalized crop, purpose and reason. Read "
        "references/clip_id first. Whole-image rejection remains; director must inspect the "
        "source and resulting crop before binding prepared_reference_id. Cannot reuse quarantine."
    ),
    "select_reference": (
        "{revision_id, reason}. View the actual image first; prefer review_reference for "
        "an explicit review. Replacing a selected revision "
        "requires assess_revision for existing uses."
    ),
    "prepare_media": (
        "{source_candidate_id, kind: video|audio|image, start_seconds, end_seconds?: "
        "number, purpose}. "
        "Creates a durable local excerpt job. Video/audio intervals must be 2–15 seconds within "
        "the actual source; image extracts one real timestamped frame. Use the preceding selected "
        "Clip's final 2 seconds for continuation. Video excerpts exclude sound; use a separate "
        "audio reference for timbre/ambience. Rejected own candidates may supply repair/evidence "
        "excerpts; they do not become accepted observations. Read the completed job before use."
    ),
    "review_reference": (
        "{revision_id, decision: accept|reject, reason, repair_plan?: {defects: string[], "
        "preserve: string[], change, source_revision_id?}}. Executive views the actual image "
        "with read_media. Rejection requires explicit defects, preserved correct content and "
        "a targeted change."
    ),
    "resolve_quality": (
        "Executive only: {clip_id OR state_id+view, decision: retry|select, reason, "
        "extra_attempts?: int, fallback_id?: string, repair_plan?: {defects, preserve, change, "
        "source_candidate_id?, source_revision_id?}, candidate_id?: string, revision_id?: string, "
        "deviations?: string[]}. For coherent-v1 Clips, select may choose an existing reviewed "
        "playable candidate without exhausting ordinary repairs or entering takeover first; "
        "record deviations and retain original reviews and actual endpoint. "
        "retry still requires the completed ordinary correction phase. Legacy and image "
        "takeover retain their saved rules. Compare candidate evidence. "
        "retry grants a finite additional allowance and requires a preselected "
        "fallback_id; its episode ceiling cannot be reset by replanning or repeated takeover. "
        "For Clips retry also prepares the next repair; for images call generate_reference next. "
        "At the ceiling select a displayable/playable candidate, recording deviations even if "
        "they concern plot. Original screenplay, rejections remain unchanged. "
        "A preselected fallback may be applied if the director subsequently makes no progress."
    ),
    "plan_voice": (
        "{asset_id, clip_id, reason}. Executive designates a formal Clip containing this "
        "character's speech to establish the first voice baseline. Other speaking Clips for "
        "the character wait; unrelated Sets proceed. Avoid cycles with Clip/Set dependencies."
    ),
    "establish_voice": (
        "{asset_id, candidate_id, start_seconds, end_seconds, reason}. Executive selects a "
        "2–15 second voice interval from a selected formal Clip, preferably clear single-person "
        "speech. A local job extracts and selects the baseline on that character's asset card. "
        "It is playable in the frontend; no automatic audio assessment is claimed."
    ),
    "select_voice": (
        "{asset_id, voice_reference_id, reason}. Executive selects an existing voice baseline "
        "for future jobs. Historical voice versions and already submitted exact "
        "bindings stay intact."
    ),
    "approve_asset_observation": (
        "{asset_id, preparation_job_id, text, reason}. Executive must first read_media the "
        "actual prepared screenshot. Confirms ONLY the stated correct detail, including one "
        "from a rejected candidate; preserves that candidate's failure. Returns entry_id for "
        "generate_reference.source_observation_ids. Does not rewrite screenplay or "
        "auto-select video."
    ),
    "prepare_sets": (
        "{sets: [{id?: string, title, description, story_time: number, depends_on?: "
        "set_id[]}]}. Create the complete Set directory once, before freeze; IDs "
        "supplied when dependencies exist."
    ),
    "plan_clips": (
        "{clips: [{title, description, duration: seconds, story_time?: number, "
        "story_end_time?: number, "
        "asset_plan?: [{asset_id,start_state_id,end_state_id?,required_views?: string[]}], "
        "speaking_asset_ids?: string[], continuity?: {action_direction, speed_trend, "
        "camera_motion, "
        "opening_action, ending_action}}], reason?: string}. A Set is one continuous camera set, "
        "never internal cuts. Plan varied 5–15 second Clips around complete actions and dialogue. "
        "Current Set; "
        "revises the unaccepted suffix, archiving prior plans and retaining candidates; "
        "a reason is required for generated candidates and repair counts carry forward. "
        "Use mode=append_unplanned_suffix to preserve every existing Clip and add only "
        "later actions while earlier production is pending. Default mode=replace_unselected_suffix "
        "retains the replacement semantics. Replanning definite failed API tasks "
        "with no media adds no quality correction; "
        "prior real quality history still counts."
    ),
    "freeze_production": (
        "{}. Executive only, after asset entries, story-state plans, required BASE views and "
        "Set directory are ready. Derived-state images may remain unrendered until needed. "
        "Directory and Set boundaries become immutable; render imminent views and prefetch "
        "later ones while production runs."
    ),
    "propose_global": (
        "{text}. Set director sends executive a required state/reference/global-memory"
        " change; explain affected story-time and your own Clip. Before the first Clip, "
        "use affected_clip_ids=[] and describe your own Set; do not guess another Set's IDs."
    ),
    "resolve_proposal": (
        "{proposal_id, status: accepted|rejected, resolution}. Executive applies any "
        "needed changes before resolving and explains them."
    ),
    "generate_clip": (
        "{clip_id, prompt, bindings: [{asset_id,state_id,revision_id, "
        "prepared_reference_id?: job_id, purpose?: string, full_board_reason?: string}], "
        "video_references?: [{preparation_job_id, role: "
        "continuation|edit_source|motion|identity, purpose}], "
        "audio_references?: [{preparation_job_id?, voice_reference_id?, asset_id?, "
        "role: voice|ambience|sound, purpose}]}. "
        "Sequential Clip; exact chronological references plus actual prior end frame/state are "
        "enforced automatically. prompt must follow the H3 Skill's six-section format. "
        "Picture labels follow bindings order starting at 1, or at 2 when the actual "
        "preceding end frame occupies Picture 1. At most 9 total image inputs. "
        "Use the actual binding returned by prepare_reference and read its resulting media first. "
        "Video/Audio labels are numbered independently in supplied order; continuation tail is "
        "Video 1 and must come from the preceding selected Clip. Use the selected fixed voice "
        "for every speaking character after bootstrap. Max 9 images, 3 videos, 3 audio, 12 "
        "combined files; video and audio each have <=15 seconds total. Explicitly prioritize "
        "required continuity/identity/voices when limits conflict, never silently drop inputs. "
        "Direct use of a complete reference board requires full_board_reason."
    ),
    "analyze_candidate": (
        "Prepare local review evidence for the exact candidate. Supply candidate_id and, "
        "for a focused check, a new interval with start_seconds, end_seconds, fps (2–8, "
        "default 8), and the question to inspect. Read job:ID to track that work. "
        "A failed extraction may be retried without another video generation."
    ),
    "confirm_candidate_state": (
        "Confirm or correct the actual end state from inspected candidate media. "
        "Use review/candidate_id context. Supply final_end_state, end_state_media_ids "
        "(including actual end frame), reason, and visible_state_ids only for states "
        "actually visible at the end. Keeps original observations and submitted jobs. "
        "Set directors confirm their own Set; executive can resolve disputed results."
    ),
    "review_candidate": (
        "Record the director's accept or reject decision for inspected candidate evidence. "
        "Read the required frames, cite actual evidence, explain substantive defects, "
        "and record the actual end state. On rejection, supply a targeted repair plan "
        "listing defects, features to preserve, and the proposed change."
    ),
    "repair_clip": (
        "{clip_id, reason, duration?: seconds, title?: string, description?: string, "
        "repair_plan?: {defects,preserve,change,source_candidate_id?}}. "
        "Prepares a new attempt, preserving old candidates/references. If the latest "
        "video job definitively failed with no media, this is a technical retry and "
        "DOES NOT spend a quality correction. Call this before saving a new reference "
        "plan for a technically blocked Clip; use its current expected_plan_id. "
        "Other repairs use durable ordinary/executive allowances. Returns attempt_kind, "
        "ordinary_correction_spent and repair_count. Repair dependent accepted Clips "
        "from the end backwards first."
    ),
    "assess_revision": (
        "{issue: revision_assessment:OLD:NEW, reason, assessments: {candidate_id: "
        "explanation}}. All old uses need an assessment; apply explicit repair_clip "
        "only when necessary."
    ),
    "reconcile_job": (
        "{job_id, evidence, resolution?:resume|failed|abandon_image_result, provider_job_id?, "
        "acknowledge_unretrievable_result?:bool}. H3 resume requires its original verified "
        "task ID. Image failed requires recorded POST rejection. An already-failed "
        "POST rejection needs no reconciliation. A legacy failed or ambiguous image HTTP "
        "400/401/402/403/422/425/429/503 without a known request phase, result or recovery handle "
        "can use abandon_image_result only after inspection and explicit "
        "acknowledge_unretrievable_result=true. Preserve remote execution as UNKNOWN; "
        "no automatic retry. Network loss/timeouts without a recovery handle require an "
        "existing exact-job image_recovery_authorization from the user, max_attempts=1, "
        "with no replacement already consumed; tools cannot create this authorization. "
        "After the authorized abandonment, generate_reference records one linked attempt "
        "and keeps remote_execution=unknown. Other network/5xx failures remain blocked."
    ),
    "set_status": (
        "{status: waiting|blocked, summary, category?: technical, job_id?: string}. Quality "
        "problems escalate to the executive; technical blocks must name an actually failed "
        "or ambiguous job. Wait for external jobs, voice bootstrap, user "
        "screenplay confirmation or executive proposals; do not finish merely because a"
        " turn ended. When waiting for your own existing job, supply job_id: the backend "
        "reconciles its current status atomically without requiring another read for every "
        "poll update. If it already returned or new work arrived, waiting=false keeps you "
        "awake; read the indicated job and new context. Other actions and general waits "
        "still require current dependencies."
    ),
    "finish_set": (
        "{}. Every Clip needs a selected candidate through review, explicit user selection "
        "or recorded executive compromise; executive is automatically notified."
    ),
    "assemble": (
        "{}. All Sets complete, proposals and revision assessments resolved. Durable "
        "export retains native audio and sources."
    ),
    "finish_project": (
        "{}. Executive only, after successful assembly and all unresolved issues handled."
    ),
}


ACTIONS.update(
    {
        "write_memory": (
            "{scope: global|set, memory_revision, entry: {text, section: events|dire"
            "ctives|set_shared|clip_plan|clip_actual|clip_continuation, basis: fact|"
            "plan|observed|unknown|provisional, kind: plan|observation|constraint, sc"
            "ope: {asset_ids?, set_ids?, clip_ids?, event_ids?, starts_at?: {event_i"
            "d,position: before|after}, ends_at?}, attributes?: [{subject_id,property"
            ": appearance|position|ownership|relationship|knowledge|other,value}], so"
            "urces?: [{kind: screenplay_fact|memory|candidate|job|media|user|decision"
            ",id,note?}], source_candidate_id?, supersedes_ids?: string[], reason, re"
            "cord_key?}}. Executive alone writes global; local Clip sections specify "
            "one Clip. Replacement preserves history and requires identical scope/sec"
            "tion plus reason. Unknowns/provisional decisions are never observations."
            " Provisional global decisions need evidence and scope. Confirmed screenp"
            "lay facts cannot be rewritten. Do not copy execution logs or full review"
            "s into story memory."
        ),
        "plan_story_events": (
            "{context_token, events: [{id?,title,description,order:number,story_clock"
            "?,source_fact_ids?}], reason?}. Executive plans chronological story even"
            "ts independently of film order/media seconds. Existing event updates pre"
            "serve history and invalidate affected unsubmitted work. Read planning/gl"
            "obal context first."
        ),
        "update_clip_context": (
            "{clip_id,context_token,reason,story_event_ids?,starts_at?:{event_id,posi"
            "tion},ends_at?,asset_plan?:[{asset_id,start_state_id,end_state_id?,requi"
            "red_views?}],plan_sources?:[{kind,id,note?}],state_notes?,state_readines"
            "s?:ready|needs_verification,continuity?,duration?:5-15}. Duration may change "
            "in place only before any generation in this Clip's lineage; after generation "
            "use repair_clip. Update an unselected Clip with"
            "out replacing its identity, attempts or candidates; submitted requests r"
            "etain their snapshot. Own Set or executive. Read planning/clip_id conte"
            "xt. Only executive with sources can resolve uncertain migrated state. "
            "Executive identity_bootstrap?: {asset_id,state_id,reason,unknowns:string[],"
            "proposal_id,requirement_ids:string[]} records a textual identity exception "
            "only for the first Clip/base state before any candidate, without a required "
            "external identity image. It defers exact pending reference obligations until "
            "real first-Clip footage is reviewed; no image/observation is supplied. Keep "
            "source story, timing, other true references and identity risks. Later Clips "
            "require genuine inspected footage references. Never write this authorization "
            "directly into continuity or renew it after submission."
        ),
        "save_reference_plan": (
            "{clip_id,context_token,expected_plan_id?:current_reference_plan_id,reaso"
            "n?,bindings:[{asset_id,state_id,revision_id,prepared_reference_id?,purpo"
            "se,state_role:start|target,full_board_reason?}],video_references?:[{prep"
            "aration_job_id,role:continuation|edit_source|motion|identity,purpose,vis"
            "ual_state_ids?}],audio_references?:[{preparation_job_id?,voice_reference"
            "_id?,asset_id?,role:voice|ambience|sound,purpose}],conflicts?:[{id?,desc"
            "ription,conflicting_media_ids,status:open|resolved|not_applicable,"
            "resolution_method?:crop|replace|revise,replacement_media_ids?,reason?,"
            "applicability_reason?,current_evidence_media_ids?}]}. Read references/cli"
            "p_id context. Save exact materials BEFORE prompt authoring. Images must "
            "be current selected state/view versions or exact inspected crops with a "
            "scoped approve_reference_region approval. Visual video states must be wit"
            "hin this Clip's start/target plan, no executive exemption. Fixed voice a"
            "nd actual selected predecessor remain supported. Old conflicts cannot be"
            " silently dropped. Use not_applicable only for inherited conflicts whose "
            "original conflicting image is absent from current inputs; preserve old "
            "evidence and supply inspected current input evidence plus applicability_reason. "
            "Never reread quarantined evidence. Revisions and submitted inputs remain immutable."
        ),
        "finalize_reference_plan": (
            "{reference_plan_id,context_token,prompt,prompt_reconciliation,conflict_c"
            "heck}. Use references/clip_id context. First read_media every exact inpu"
            "t image and prior end frame, resolve all known conflicts by crop/replace"
            "ment/revision, then author the official H3 six-section prompt in exact P"
            "icture/Video/Audio order. Record reconciliation and inspection results. "
            "Changed/stale drafts require save_reference_plan again. Returns a finali"
            "zed plan for generation."
        ),
        "generate_clip": (
            "{clip_id,reference_plan_id,context_token}. Read generation/clip_id conte"
            "xt after finalizing the plan. Creates a sequential Clip job with pinned "
            "exact images/crops/video intervals/audio versions and reconciled six-sec"
            "tion prompt. No inline material/prompt changes. Queue/submission never s"
            "waps newer references. Clip-specific proposal requirements, current sele"
            "cted reference versions and relevant context are checked atomically."
        ),
        "propose_global": (
            "{text,affected_clip_ids?: string[]}. Describe evidence, required change "
            "and affected Clips; defaults to the first unfinished Clip. Independent C"
            "lips continue."
        ),
        "resolve_proposal": (
            "{proposal_id,status:accepted|in_progress|applied|rejected,resolution,aff"
            "ected_clip_ids?,requirements?:[{id?,kind:memory|state|reference|impact,d"
            "escription,clip_ids?,status:pending|done,evidence_ids?:string[],resoluti"
            "on?}]}. Executive links actual global memory/state/current reference/imp"
            "act decisions. Applied requires every item complete; individual Clips pr"
            "oceed when their items are complete, even while other Clips wait. Accept"
            "ing alone does not imply application. To retire an obsolete proposal "
            "after Clip replanning, use rejected with a reason and omit affected_clip_ids "
            "and requirements: its archived scope and unfinished work remain history. "
            "Resolve the replacement plan's obligations explicitly. For completed "
            "administrative registration that originally had no requirements, use "
            "status:applied, requirements:[], evidence_ids:[actual existing IDs] and "
            "explain the completed work. Existing requirements cannot be erased this way; "
            "real material requests still require their current selected state/views."
        ),
        "approve_asset_observation": (
            "{asset_id,state_ids:string[],attributes:string[],purpose,preparation_job"
            "_id,text,reason,scope?}. Executive first inspects the exact screenshot. Approva"
            "l pins source candidate/time/media, correct local attributes and explici"
            "tly allowed target states. Reuse outside those states requires new appro"
            "val. Rejected source candidate stays rejected. Supply scope with target Set/Clip/"
            "event IDs or anchors for a limited purpose. Without narrower scope it applies to "
            "the approved target states throughout their lifetime, not only their starting event. "
            "Source screenshot time is provenance, never an inferred target scope."
        ),
    }
)
ACTIONS["create_state"] += (
    " Prefer starts_at/ends_at: {event_id,position:before|after}; story_time "
    "remains a separate optional clock coordinate. attributes [{subject_id,pr"
    "operty,value}] capture relevant appearance, ownership, position, relatio"
    "ns or knowledge. Supply evolution with kind root|derived|unresolved, one same-asset "
    "parent_state_id for derived states, reason and exact quoted evidence "
    "[{kind:state|event|memory|screenplay,id,quote}]. Use state:self for the new state's "
    "own description. Image input provenance and story_time alone do not prove ancestry. "
    "Set projects require reference_purpose: {purpose, set_ids?: string[], clip_ids?: string[], "
    "required_materials: string[], prepare_when: now|before_generation|after_source_selected}. "
    "Name at least one existing target Set or active Clip, concrete materials/views, and when "
    "they are prepared. Establish assets and the Set directory before creating reference "
    "states; Set-only targets do not wait for Clip planning or images. Waiting on actual footage "
    "also needs observed preparation with its source Set/event and required attributes. "
    "Pure within-Clip process or ending descriptions with no reference use belong in story "
    "memory and prompts. Use a new asset only for a distinct story entity."
)
ACTIONS["record_state_evolution"] = (
    "Executive only, after planning/state_id context. Record or correct story-state ancestry "
    "with expected_evolution_revision (0 if absent) and evolution {kind:root|derived|unresolved, "
    "parent_state_id?,reason,evidence:[{kind:state|event|memory|screenplay,id,quote}]}. "
    "Each derived state has one primary parent in the same asset; branches are allowed, "
    "cycles are rejected. Cite exact saved text; screenplay id confirmed means the frozen "
    "script. Mark insufficient evidence unresolved with a reason, never guess from image "
    "references or chronology. Keeps prior lineage revisions, applicability and footage."
)
ACTIONS["prepare_sets"] += " Requires context_token from planning/global."
ACTIONS["correct_state_scope"] = (
    "Executive only, after complete planning/state_id context. Correct an existing state's "
    "story-event boundaries using state_id, starts_at, ends_at and a specific reason. Supply "
    "both intended bounds; null explicitly means unbounded. Retains state identity, description, "
    "selected images, repair counters and all submitted recipes. Stores before/after boundaries "
    "in the operation history and invalidates affected unsubmitted reference plans. Use this "
    "for a mistaken range or event/Clip repartition, not for a different visual story state. "
    "Optional excluded_event_ids excludes whole events (both before and after) within these "
    "bounds, for a state which returns unchanged after an intervening event. Each event must "
    "belong to a published Set using this asset. Omit/null to preserve existing exclusions; "
    "[] explicitly clears them. Cannot remove a boundary already bound by submitted/selected "
    "footage. Does not establish observed state or authorize reuse across state identities."
)
ACTIONS["plan_clips"] += (
    " Requires context_token from planning/set_id. Each Clip includes story_"
    "event_ids, starts_at/ends_at event anchors, asset_plan and optional plan"
    "_sources/state_notes; changing duration does not change event order."
)
ACTIONS["review_candidate"] += (
    " Requires context_token from review/candidate_id, after analysis is comp"
    "lete. Selected actual end-state is recorded separately from the full rev"
    "iew."
)
for _action in ("generate_reference", "select_reference"):
    ACTIONS[_action] += " Requires context_token from references/state_id before acting."
for _action in ("review_reference", "resolve_quality"):
    ACTIONS[_action] += (
        " Requires context_token from review/state_id (image) or review/clip_id (video)."
    )


def actor_token(secret: bytes, project_id: str, actor_id: str, generation: int = 0) -> str:
    return hmac.new(
        secret, f"{project_id}:{actor_id}:{generation}".encode(), hashlib.sha256
    ).hexdigest()


def token_secret(path: Path) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as stream:
            import os

            os.chmod(path, 0o600)
            stream.write(os.urandom(32))
    except FileExistsError:
        pass
    return path.read_bytes()


def context(project: StudioProject, actor_id: str, store: Any = None) -> dict[str, Any]:
    """Current role context assembled by IDs/status; historical evidence is explicit."""
    actor = get_actor(project, actor_id)
    from .delegation import DELEGATE_ROLES

    if actor.role in DELEGATE_ROLES:
        from .delegation import task_for as delegate_task_for

        task = delegate_task_for(project, actor)
        projection = task.model_dump(exclude={"approvals", "followups"})
        if records_only(task):
            from .delegation import RECORD_REPORT_LIMITATION

            projection["allowed_actions"] = sorted(actions_for(project, actor))
            projection["request"]["evidence_policy"] = "records-only"
            projection["evidence_basis"] = "records-only"
            projection["limitations"] = [RECORD_REPORT_LIMITATION]
        return {
            "id": project.id,
            "revision": project.revision,
            "you": actor.model_dump(include={"id", "role", "goal", "status"}),
            "delegate_task": projection,
            "next_read": {"phase": "review", "target_id": task.id},
        }
    if actor.role == "executive" and actor.production_context_started_at:
        from .delegation import document_index, inbox

        return {
            "id": project.id,
            "revision": project.revision,
            "status": project.status,
            "you": actor.model_dump(
                include={"id", "role", "goal", "status", "session_key", "context_epoch"}
            ),
            "director_mode": project.director_mode,
            "screenplay_hash": project.screenplay.confirmed_hash,
            "inbox": inbox(project, actor, limit=20),
            "operational_documents": document_index(project, actor),
            "sets": {
                s.id: {
                    "id": s.id,
                    "title": s.title,
                    "status": s.status,
                    "actor_id": s.actor_id,
                    "detail_scope": "set:" + s.id,
                }
                for s in project.sets.values()
            },
            "issues": project.issues,
            "overview_only": True,
            "instruction": (
                "Read indexed notes and short reports; delegate heavy inspection. "
                "Current business records override notes. Use scoped details as needed."
            ),
        }
    if actor.role == "screenwriter":
        from .context_projection import writer_packet

        return writer_packet(project, actor)
    if actor.role == "visual_analyst":
        from .visual_analysis import task_for

        return {
            "id": project.id,
            "revision": project.revision,
            "you": actor.model_dump(include={"id", "role", "goal", "status"}),
            "visual_task": task_for(project, actor).model_dump(mode="json"),
            "next_read": {"phase": "review", "target_id": actor.visual_task_id},
        }
    from .planning_sessions import assignment as planning_assignment

    planning = planning_assignment(project, actor)
    if planning:
        return {
            "id": project.id,
            "revision": project.revision,
            "status": project.status,
            "you": actor.model_dump(
                include={"id", "role", "set_id", "status", "session_key", "goal"}
            ),
            "planning_assignment": planning,
            "goal_detail_scope": f"actor:{actor.id}",
            "next_read": planning["next_read"],
            "overview_only": True,
        }
    visibility = Visibility(project, actor_id)
    result = core_payload(project)
    result["you"] = actor.model_dump(
        mode="json",
        include={
            "id",
            "role",
            "set_id",
            "status",
            "summary",
            "goal",
            "last_error",
            "production_blocker",
            "session_key",
            "context_epoch",
        },
    )
    result["actors"] = {
        a.id: {
            **a.model_dump(mode="json", exclude={"goal", "recovery_history", "progress_markers"}),
            "preview_only": True,
            "detail_scope": f"actor:{a.id}",
        }
        for a in (
            [actor]
            if visibility.isolated
            else [
                item
                for role in ("screenwriter", "executive", "set_director")
                for item in select_entities(project.actors, subtype=role)
            ]
        )
    }
    sets = [project.sets[str(actor.set_id)]] if visibility.isolated else list(project.sets.values())
    relevant = [
        s
        for s in sets
        if not actor.set_id or s.id == actor.set_id or s.id in project.sets[actor.set_id].depends_on
    ]
    clips = [project.clips[i] for production_set in relevant for i in production_set.clip_ids]
    current_candidates = []
    for clip in clips:
        if clip.accepted_candidate_id:
            current_candidates.append(project.candidates[clip.accepted_candidate_id])
        if actor.set_id == clip.set_id:
            current_candidates.extend(
                select_entities(
                    project.candidates, parent_id=clip.id, statuses=["generating", "review"]
                )
            )
        if (
            clip.quality_episode
            and clip.quality_episode.status in {"escalated", "intervening"}
            and (actor.role == "executive" or actor.set_id == clip.set_id)
        ):
            current_candidates.extend(
                select_entities(
                    project.candidates,
                    parent_id=clip.id,
                    statuses=["review", "rejected", "accepted"],
                )
            )
    result["clips"] = {}
    for clip in clips:
        record = clip.model_dump(
            mode="json",
            exclude={
                "candidate_ids",
                "repair_reason",
                "repair_plan",
                "quality_episode",
                "continuity",
                "plan_sources",
                "state_notes",
            },
        )
        episode = clip.quality_episode
        record.update(
            quality_progress=quality_progress(project, clip).model_dump(mode="json"),
            quality_episode=(
                {
                    **episode.model_dump(mode="json", exclude={"decisions", "plan"}),
                    "decision_count": len(episode.decisions),
                    "plan_available": episode.plan is not None,
                    "preview_only": True,
                }
                if episode
                else None
            ),
            repair_plan_available=clip.repair_plan is not None,
            continuity_details_available=bool(clip.continuity or clip.state_notes),
            preview_only=True,
            detail_scope=f"clip:{clip.id}",
        )
        result["clips"][clip.id] = record
    # Full reports and their histories belong to scoped detail/review reads. In
    # mature projects the same report used to appear here and again in its job,
    # multiplying each overview by megabytes without adding current status.
    result["candidates"] = {
        c.id: {
            **c.model_dump(
                mode="json",
                exclude={
                    "analysis",
                    "prompt",
                    "review_history",
                    "end_state_history",
                    "repair_plan",
                    "review",
                    "selection",
                    "confirmed_end_state",
                },
            ),
            "review": (
                {
                    **c.review.model_dump(
                        mode="json", include={"actor_id", "decision", "created_at"}
                    ),
                    "reason_preview": c.review.reason[:240],
                    "preview_only": True,
                }
                if c.review
                else None
            ),
            "selection": (
                {
                    **c.selection.model_dump(
                        mode="json", include={"kind", "actor_id", "created_at"}
                    ),
                    "reason_preview": c.selection.reason[:240],
                    "deviation_count": len(c.selection.deviations),
                    "preview_only": True,
                }
                if c.selection
                else None
            ),
            "confirmed_end_state": (
                {
                    **c.confirmed_end_state.model_dump(mode="json", exclude={"reason"}),
                    "preview_only": True,
                }
                if c.confirmed_end_state
                else None
            ),
            "repair_plan_available": c.repair_plan is not None,
            "analysis_available": bool(c.analysis),
            "analysis_check_version": c.analysis.get("check_version"),
            "preview_only": True,
            "detail_scope": f"candidate:{c.id}",
        }
        for c in current_candidates
    }

    def memory(value: Any, scope: str) -> dict[str, Any]:
        dimensions = (
            {
                "asset_ids": sorted(visibility.asset_ids),
                "set_ids": [str(actor.set_id)],
                "clip_ids": sorted(visibility.clip_ids),
                "event_ids": sorted(visibility.event_ids),
            }
            if visibility.isolated
            else None
        )
        entries = effective_entries(project, scope, context=dimensions, limit=41)
        return {
            "revision": value.revision,
            "asset_state_ids": value.asset_state_ids[-40:],
            "story": [
                {
                    **entry.model_dump(
                        mode="json",
                        include={
                            "id",
                            "kind",
                            "section",
                            "basis",
                            "status",
                            "scope",
                            "actor_id",
                            "version",
                            "record_key",
                            "source_candidate_id",
                            "source_job_id",
                            "evidence_media_ids",
                            "approved_asset_id",
                            "approved_state_ids",
                        },
                    ),
                    "text": entry.text[:280],
                    "text_truncated": len(entry.text) > 280,
                    "preview_only": True,
                    "detail_scope": f"memory:{scope}",
                }
                for entry in entries[:40]
            ],
            "preview_only": True,
            "has_more": len(entries) > 40,
            "required_context": (
                "This project requires complete task-context pagination before production "
                "decisions; call get_required_context and read every page."
                if full_context_required(project)
                else "Full task text is optional, not a gate for every action. This preview "
                "may omit relevant details: use get_detail or a work packet for missing "
                "facts, and retain a current scoped read_token before writes."
            ),
            "history": {
                "tool": "query_memory",
                "scope": scope,
                "note": (
                    "Query current or historical entries anywhere in this project, by asset/C"
                    "lip/event or text. get_history preserves full originals."
                ),
            },
        }

    result["global_memory"] = memory(project.global_memory, "global")
    from .playback import report as playback_report

    result["playback"] = playback_report(
        project, set_id=str(actor.set_id) if visibility.isolated else None
    ).model_dump(mode="json", exclude={"sets"})
    result["sets"] = {
        production_set.id: {
            **production_set.model_dump(mode="json", exclude={"memory", "duration_history"}),
            "memory": memory(production_set.memory, production_set.id)
            if production_set in relevant
            else {"revision": production_set.memory.revision},
        }
        for production_set in sets
    }
    assets = (
        select_entities(project.assets, ids=sorted(visibility.asset_ids))
        if visibility.isolated
        else list(project.assets.values())
    )
    result["assets"] = {a.id: a.model_dump(mode="json", exclude={"state_ids"}) for a in assets}
    state_ids = {b.state_id for c in current_candidates for b in c.bindings}
    times: set[float | None] = {s.story_time for s in relevant}
    times.update(c.story_time for c in clips if c.story_time is not None)
    # None explicitly means no story-time ceiling. Infinity is serialized to
    # null by durable read receipts, which used to break later SQL validation.
    times = times or {None}
    state_ids.update(plan.start_state_id for clip in clips for plan in clip.asset_plan)
    state_ids.update(
        plan.end_state_id for clip in clips for plan in clip.asset_plan if plan.end_state_id
    )
    for asset in assets:
        for stamp in times:
            state_ids.update(
                s.id
                for s in select_entities(
                    project.states, parent_id=asset.id, story_time_max=stamp, limit=1
                )
            )
    states = [project.states[i] for i in state_ids]
    result["states"] = {s.id: s.model_dump(mode="json", exclude={"revision_ids"}) for s in states}
    refs = {r for state in states for r in state.selected_revisions.values()}
    refs.update(b.revision_id for c in current_candidates for b in c.bindings)
    # Pending preparation results must remain discoverable without past rejected revisions.
    if actor.role == "executive":
        refs.update(
            r.id
            for r in select_entities(
                project.references, statuses=["pending", "candidate"], limit=50, descending=True
            )
        )
        for issue in project.issues:
            if issue.startswith("quality:reference:"):
                _, _, state_id, view = issue.split(":", 3)
                refs.update(
                    r.id
                    for r in select_entities(project.references, parent_id=state_id)
                    if r.view == view and r.media_id
                )
                state = project.states[state_id]
                result["states"][state.id] = state.model_dump(mode="json", exclude={"revision_ids"})
    result["references"] = {
        i: {
            **project.references[i].model_dump(mode="json", exclude={"prompt", "review_history"}),
            "preview_only": True,
            "detail_scope": f"reference:{i}",
        }
        for i in refs
    }
    filters: dict[str, Any] = {"statuses": ["queued", "submitting", "running", "ambiguous"]}
    if actor.role != "executive":
        filters["actor_id"] = actor.id
    jobs = select_entities(project.jobs, **filters)
    # Completed results are fetched only for currently visible candidates.
    for candidate in current_candidates:
        generation = project.jobs.get(candidate.job_id)
        if generation:
            jobs.append(generation)
        jobs.extend(
            select_entities(
                project.jobs,
                parent_id=candidate.id,
                subtype="analysis",
                statuses=["complete"],
                descending=True,
                limit=1,
            )
        )
    result["jobs"] = {}
    for job in jobs:
        record = job.model_dump(mode="json", exclude={"request"})
        if job.kind in {"analysis", "video"}:
            record["result"] = (
                {
                    key: value
                    for key, value in job.result.items()
                    if key not in {"analysis", "end_state", "provenance"}
                }
                if job.target_id in result["candidates"]
                else {}
            )
            record["result_details_available"] = bool(job.result)
            record["preview_only"] = True
            record["detail_scope"] = f"job:{job.id}"
        result["jobs"][job.id] = record
    result["proposals"] = {
        p.id: p.model_dump(mode="json")
        for p in select_entities(
            project.proposals,
            statuses=["pending", "accepted", "in_progress"],
            **({"parent_id": actor.set_id} if visibility.isolated else {}),
        )
    }
    if actor.role == "executive":
        result["visual_tasks"] = {
            task.id: {
                **task.model_dump(
                    include={"id", "actor_id", "question", "status", "completed_at"}, mode="json"
                ),
                "detail_scope": f"visual-task:{task.id}",
            }
            for task in select_entities(
                project.visual_tasks, parent_id=actor.id, descending=True, limit=16
            )
        }
        result["visual_tasks_history"] = {
            "tool": "get_history",
            "kind": "visual_tasks",
            "parent_id": actor.id,
        }
    media_ids = {
        v.get("media_id")
        for name in ("candidates", "references")
        for v in result[name].values()
        if v.get("media_id")
    }
    media_ids.update(
        v.media_id
        for asset in assets
        for v in asset.voice_references
        if v.media_id and v.id == asset.selected_voice_id
    )
    for candidate in current_candidates:
        media_ids.update(candidate.analysis.get("required_review_media_ids", []))
        if candidate.end_state:
            media_ids.add(candidate.end_state.frame_media_id)
    result["media"] = {
        i: project.media[i].model_dump(mode="json") for i in media_ids if i in project.media
    }
    if store:
        page = store.transcript_page(project.id, actor.id, limit=60)
        messages = [i for i in page.items if i.kind in {"user", "message", "error"}]
    else:
        messages = [
            i
            for i in project.transcript.values()
            if i.actor_id == actor.id and i.kind in {"user", "message", "error"}
        ][-60:]
    result["transcript"] = {i.id: i.model_dump(mode="json") for i in messages}
    result["story_events"] = {
        e.id: e.model_dump(exclude={"history"}, mode="json")
        for e in (
            select_entities(project.story_events, ids=sorted(visibility.event_ids))
            if visibility.isolated
            else project.story_events.values()
        )
    }
    result["reference_plans"] = {
        c.reference_plan_id: project.reference_plans[c.reference_plan_id].model_dump(
            include={"id", "clip_id", "revision", "status", "job_id", "invalidation_reasons"},
            mode="json",
        )
        for c in clips
        if c.reference_plan_id
    }
    result["detail_tools"] = {
        "get_detail": (
            "scope=actor:ID|set:ID|memory:global|memory:SET_ID|clip:ID|candidate:ID|"
            "reference:ID|state:ID|proposal:ID|job:ID|"
            "screenplay|final"
        ),
        "get_history": (
            "kind=states|references|candidates|usage|memory_entries; parent_id; after; limit"
        ),
        "get_body": "body_ref + offset, for complete visible tool/message bodies",
    }
    result["overview_only"] = True
    result["full_context_policy"] = project.config.full_context_policy
    result["overview_instruction"] = (
        "Overview records marked preview_only omit full reports, repair/decision details, "
        "prompts and past history. Memory text is a preview (see text_truncated); "
        "selection deviations and review assessments require the exact candidate detail. "
        "analysis_available reports whether analysis exists, not its verdict. Use each "
        "detail_scope with get_detail for exact content. "
        "Use compare_candidates for review findings. "
        + (
            "Complete get_required_context before decisions. "
            if full_context_required(project)
            else "Read relevant full text when the task needs more information. "
            "A fresh scoped read_token remains required; optional full reading never "
            "waives image inspection, quarantine, quality or source/state checks. "
        )
        + "No stored evidence or goal was shortened."
    )
    return visibility.payload(result)


class CanvasMCP:
    # Legacy adapters can construct a Canvas without its normal initializer.
    compact_role_reads: bool = False

    def __init__(
        self,
        service: StudioService,
        cas: ContentAddressedStore,
        *,
        compact_role_reads: bool = False,
    ) -> None:
        self.service = service
        self.cas = cas
        self.compact_role_reads = compact_role_reads
        self._previews: OrderedDict[str, tuple[bytes, str, bool]] = OrderedDict()
        self._preview_lock = RLock()

    def _ack_pending_image_deliveries(self, project: StudioProject, actor_id: str) -> StudioProject:
        """Only explicitly legacy stages use the historical later-tool acknowledgement."""
        actor = project.actors[actor_id]
        if not any(
            (item := project.transcript.get(delivery.item_id))
            and item.data.get("native_transport") is False
            for delivery in actor.pending_image_deliveries
        ) or not self.service.store.actor_has_pending_image_delivery(
            project.id, actor_id, exclude_records_only=True
        ):
            return project

        def acknowledge(current: StudioProject) -> None:
            from .delegation import DELEGATE_ROLES, records_only, task_for
            from .image_input_recovery import acknowledge_pending_deliveries

            active = get_actor(current, actor_id)
            if active.role not in DELEGATE_ROLES or not records_only(task_for(current, active)):
                acknowledge_pending_deliveries(current, active)

        return self.service.store.mutate(
            project.id,
            acknowledge,
            actor_id=actor_id,
            kind="image_delivery_acknowledged",
            advance_revision=False,
        )

    def tools(
        self,
        role: str | None = None,
        *,
        review_session: bool = False,
        candidate_review: bool = False,
        delegated: bool = False,
        task_actions: list[str] | None = None,
        task_read_tools: list[str] | None = None,
        strict_context: bool = False,
    ) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = [
            {
                "name": "get_required_context",
                "description": (
                    "Read mandatory task context before planning, reference selection, genera"
                    "tion or review. target_id is global, Set, Clip, state or candidate ID. "
                    "Start page=0; concatenate content pages in order (JSON may span pages), "
                    "follow next_page with the returned fingerprint until null. Only the fina"
                    "l page returns context_token. It is actor/task/phase bound, revalidated "
                    "before mutation, and excludes unrelated Set activity. This is a physica"
                    "l page size, never a last-N semantic cutoff. With mode=auto, omitting "
                    "page repeats the last served page of a still-current incomplete read; "
                    "follow its exact next_request. Explicit page=0 starts a new read."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "phase": {"enum": ["planning", "references", "generation", "review"]},
                        "target_id": {"type": "string"},
                        "page": {"type": "integer", "minimum": 0},
                        "fingerprint": {"type": "string"},
                        "mode": {"enum": ["auto", "full"], "default": "auto"},
                        "full_verification_reason": {
                            "type": "string",
                            "minLength": 12,
                            "description": (
                                "Only with mode=full: concrete object and reason for a "
                                "full-text verification. Ordinary full requests still use "
                                "a complete same-session delta baseline. Archived for audit."
                            ),
                        },
                        "baseline_token": {"type": "string"},
                        "snapshot_id": {"type": "string"},
                    },
                    "required": ["phase", "target_id"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "query_memory",
                "description": (
                    "Proactively query any same-project global/Set memory, including other S"
                    "hots and history. Defaults to effective entries; status=all preserves su"
                    "perseded/legacy records. Filter by asset, Clip, event or text and follow"
                    " next_cursor. Reading does not expand write permissions or make rejected"
                    " evidence authoritative."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "scope": {"type": "string", "default": "global"},
                        "status": {"enum": ["effective", "all", "history", "superseded"]},
                        "asset_id": {"type": "string"},
                        "clip_id": {"type": "string"},
                        "event_id": {"type": "string"},
                        "text": {"type": "string"},
                        "after": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                    },
                    "additionalProperties": False,
                },
            },
            {
                "name": "get_project",
                "description": (
                    "Read a bounded current project overview, effective memory previews, assets, "
                    "active jobs and your role. Older entries and full bodies are explicitly "
                    "paged with query_memory/get_history/get_body. This preview does not replace "
                    "get_required_context before decisions. Always refresh "
                    "revision before mutation."
                ),
                "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            },
            {
                "name": "act",
                "description": (
                    "Mutate the canvas through business operations. Returns a command receipt "
                    "with revision and changed entities; use get_project/get_detail when more "
                    "context is needed. Payloads:\n"
                )
                + "\n".join(f"{name}: {shape}" for name, shape in ACTIONS.items()),
                "inputSchema": ToolCommand.model_json_schema(),
            },
            {
                "name": "read_media",
                "description": (
                    "See an actual image/reference/end frame by project media_id. Returns image "
                    "content, not a textual claim of seeing it. Large images use a labelled "
                    "compressed preview to keep conversation requests small. Request original "
                    "only when a fine detail cannot be judged from the preview."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "media_id": {"type": "string"},
                        "original": {"type": "boolean", "default": False},
                    },
                    "required": ["media_id"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "get_history",
                "description": (
                    "Inspect explicit revision/candidate audit including failed evidence; it is not"
                    " accepted memory. kind=jobs with parent_id=candidate_id or reference_id "
                    "lists that target's exact tasks, including failed analysis. Follow "
                    "next_cursor, then get_detail(job:ID) for complete request/result."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "asset_id": {"type": "string"},
                        "clip_id": {"type": "string"},
                        "kind": {
                            "type": "string",
                            "enum": [
                                "states",
                                "references",
                                "candidates",
                                "jobs",
                                "usage",
                                "memory_entries",
                                "reference_plans",
                                "visual_tasks",
                            ],
                        },
                        "parent_id": {"type": "string"},
                        "after": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                    },
                    "additionalProperties": False,
                },
            },
            {
                "name": "get_detail",
                "description": (
                    "Read complete current scoped details. scope: actor:ID, set:ID, "
                    "memory:global, "
                    "memory:SET_ID, clip:ID, candidate:ID, reference:ID, reference-plan:ID, "
                    "story-event:ID, visual-task:ID, job:ID, screenplay, "
                    "and event "
                    "mapping in two sections, independent of context deltas), "
                    "final. job:ID returns the exact request/result, including completed "
                    "reference preparation binding, media_id and provenance. "
                    "Memory history is paged with get_history. actor:ID returns the complete "
                    "durable goal; review conversations may read only their own actor. "
                    "reference-plan:ID returns a current-version body_ref first; use get_body "
                    "to read its complete original JSON when the detail output is too large."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {"scope": {"type": "string"}},
                    "required": ["scope"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "get_body",
                "description": (
                    "Read a complete visible message/tool body or the original reference-plan "
                    "JSON by body_ref in character chunks. Reference-plan refs require the "
                    "same scope as get_detail and its current version; reread detail after a "
                    "version change. Follow next_offset until null when full content is needed. "
                    "Body reads do not acknowledge mandatory task-context pages."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "body_ref": {"type": "string"},
                        "offset": {"type": "integer", "minimum": 0},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 65536},
                    },
                    "required": ["body_ref"],
                    "additionalProperties": False,
                },
            },
        ]
        tools[0]["description"] = (
            (
                "This project uses strict full-context reading: read ALL pages of task-relevant "
                "context before planning/references/generation/review. "
                if strict_context
                else "Optionally read the complete task-relevant text when needed; this is "
                "not an unconditional prerequisite for planning/references/generation/review. "
                "Use fresh scoped read_token values from get_project "
                "for writes, and request relevant full text when current information is "
                "insufficient. "
            )
            + "A complete same-session baseline yields a verified delta for auto or ordinary full. "
            "Apply upserts/removals by record_key; preserve record_order. "
            "For explicit full-text verification use mode=full with a concrete "
            "full_verification_reason. Missing/incomplete baselines read full automatically. "
            "When collecting full text, follow each returned next_request verbatim. "
            + (
                "Do not interleave writes before all pages are read. "
                if strict_context
                else "An optional read may stop early without claiming completeness; "
                "obtain a fresh scoped read_token before any subsequent write. "
            )
            + "Check isError and "
            "structuredContent.ok/error; stop the batch at the first error. If restart_request "
            "is returned, discard old pages/cursors and restart with those arguments. "
            "After two conflicting restarts return control to the director loop. "
            "A fully read packet returns context_token, also usable as read_token. "
            "An incomplete packet has no completion token. Session reconstruction/"
            "compaction invalidates full-text baselines; a later explicit full read starts "
            "from a valid baseline. No last-N cutoff. Image inspections, quarantine, quality "
            "and source/state checks remain mandatory under both context policies."
        )
        for tool in tools:
            if tool["name"] == "get_project":
                tool["description"] = (
                    "Read the bounded overview and a fresh scoped read_token. "
                    + (
                        "Before production decisions, read the complete task context."
                        if strict_context
                        else "Full task-text pagination is optional. Read exact details or "
                        "relevant full text when the current information is insufficient; "
                        "this token does not waive required images or business checks."
                    )
                )

        def read_tool(
            name: str, description: str, properties: dict[str, Any], required: list[str]
        ) -> dict[str, Any]:
            return {
                "name": name,
                "description": description,
                "inputSchema": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                    "additionalProperties": False,
                },
                "annotations": {"readOnlyHint": True},
            }

        paging = {
            "after": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200},
        }
        if role == "set_director" and not review_session:
            context_schema = tools[0]["inputSchema"]["properties"]
            tools.append(
                read_tool(
                    "get_clip_work_packet",
                    "Read one Clip's context, readiness, reference options and editable "
                    "preparation together. "
                    + (
                        "Strict context policy requires next_request until null before writes. "
                        if strict_context
                        else "Follow next_request when complete task text is needed; a current "
                        "scoped read_token from get_project suffices for "
                        "optional-policy writes. "
                    )
                    + "Incomplete pages are never recorded as a completed full read. Exact "
                    "versions and actor "
                    "isolation still apply. Reference options keep their own cursor; "
                    "this does not finalize or submit anything.",
                    {
                        **{
                            key: value
                            for key, value in context_schema.items()
                            if key != "target_id"
                        },
                        "clip_id": {"type": "string"},
                    },
                    ["clip_id"],
                )
            )
        tools.extend(
            [
                read_tool(
                    "batch_get_details",
                    "Read up to 12 exact scopes; keep successes and retry only failed scopes.",
                    {
                        "scopes": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 1,
                            "maxItems": 12,
                        }
                    },
                    ["scopes"],
                ),
                read_tool(
                    "batch_read_media",
                    "View up to 6 actual images. Per-image success/inspection; "
                    "failures remain unread. Retry failures only.",
                    {
                        "media_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 1,
                            "maxItems": 6,
                        },
                        "original": {"type": "boolean", "default": False},
                    },
                    ["media_ids"],
                ),
                read_tool(
                    "get_clip_readiness",
                    "Read prerequisites, owners, waiting objects and existing proposals. "
                    "Does not submit proposals or authorize generation.",
                    {"clip_id": {"type": "string"}},
                    ["clip_id"],
                ),
                read_tool(
                    "get_reference_candidates",
                    "List compliant sources by purpose, with inspection/crop/excerpt steps. "
                    "No recommended combination; director assigns slots and final inputs.",
                    {"clip_id": {"type": "string"}, **paging},
                    ["clip_id"],
                ),
                read_tool(
                    "compare_candidates",
                    "Compare existing candidate review conclusions, defects, evidence "
                    "and selection status.",
                    {"clip_id": {"type": "string"}, **paging},
                    ["clip_id"],
                ),
                read_tool(
                    "get_operation_receipt",
                    "Reconcile a potentially committed operation by its ORIGINAL idempotency key. "
                    "If not_found, replay only the SAME local action/payload/key with fresh "
                    "context. Registration and receipt are atomic, including a late original. "
                    "If business parameters were superseded, follow changed_payload before "
                    "submitting a new plan under a new key. "
                    "Never directly repeat a provider POST; ambiguous jobs require reconciliation.",
                    {"idempotency_key": {"type": "string", "minLength": 1, "maxLength": 128}},
                    ["idempotency_key"],
                ),
            ]
        )
        tools.append(
            read_tool(
                "get_playback_duration",
                "Read playback accounting, separate from production ETA. One current Clip recipe "
                "counts once; actual output tails are display-only. Executive reads "
                "whole-film/Set allocation; Set director reads only own Set. Obtain "
                "planning/global context before changing budgets.",
                {"set_id": {"type": "string"}},
                [],
            )
        )
        if role == "executive" and delegated:
            tools.extend(
                [
                    read_tool(
                        "get_executive_inbox",
                        "Read durable prioritized task notices, concise reports and evidence "
                        "counts, not duplicated full reports or media lists. Use item_id for one "
                        "exact immutable notification snapshot. Excerpts are marked; approvals "
                        "still require current scoped context under the configured reading policy. "
                        "Reading does not "
                        "acknowledge.",
                        {
                            "after": {"type": "string"},
                            "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                            "item_id": {"type": "string", "minLength": 1},
                        },
                        [],
                    ),
                    read_tool(
                        "get_executive_documents",
                        "Read executive-owned note index or exact key. Current business data "
                        "override stale notes; no automatic summaries or forced rewrites.",
                        {"key": {"type": "string"}, "revision": {"type": "integer", "minimum": 1}},
                        [],
                    ),
                ]
            )
        if role is not None:
            tools = [t for t in tools if t["name"] != "act"]
            for name in sorted(ROLE_ACTIONS[role]):
                if (
                    name
                    in {
                        "delegate_tasks",
                        "request_delegate_followup",
                        "approve_delegate_results",
                        "acknowledge_executive_inbox",
                        "save_executive_document",
                        "finish_initial_planning",
                    }
                    and not delegated
                ):
                    continue
                if task_actions is not None and name not in task_actions:
                    continue
                description = ACTIONS.get(
                    name,
                    "Patch changed reference fields into a new draft; "
                    "reconcile the retained draft prompt before generation.",
                ).replace("context_token", "read_token")
                if name == "save_screenplay":
                    description = (
                        "Save the complete story manuscript and fixed facts before confirmation. "
                        "Provide title, logline, style, whole-story duration_estimate, characters, "
                        "narrative paragraphs with action/dialogue/sound beats. "
                        "No Set/Clip plans, counts or per-paragraph timecodes. "
                        "The application renders 影片概览、人物、剧情正文、关键事实."
                    )
                tools.append(
                    {
                        "name": name,
                        "description": description,
                        "inputSchema": action_model(name).model_json_schema(),
                    }
                )
        if review_session:
            if candidate_review:
                schema = tools[0]["inputSchema"]["properties"]
                tools.append(
                    read_tool(
                        "get_review_work_packet",
                        "Read this candidate's paged review packet and next unseen image batch "
                        "together. "
                        + (
                            "Strict context policy requires next_request until null before writes. "
                            if strict_context
                            else "Read the full text when needed; optional-policy writes require "
                            "a fresh scoped read_token from get_project, "
                            "not completion of every text page. "
                        )
                        + "Inspect all required images in this same "
                        "conversation. Byte-identical images are reused; larger reviews "
                        "automatically use isolated six-image batches.",
                        {
                            **{k: v for k, v in schema.items() if k not in {"phase", "target_id"}},
                            "candidate_id": {"type": "string"},
                        },
                        ["candidate_id"],
                    )
                )
            tools = [tool for tool in tools if tool["name"] in REVIEW_TOOLS]
        if candidate_review:
            tools = [
                t for t in tools if t["name"] not in {"finish_review_batch", "request_review_batch"}
            ]
            for tool in tools:
                if tool["name"] in {"read_media", "batch_read_media"}:
                    tool["inputSchema"]["properties"].pop("original", None)
                    tool["description"] = (
                        "Inspect actual candidate evidence as JPEG 95 at the fixed review size. "
                        "Use read_media for one image or batch_read_media for up to six images "
                        "per call. All calls stay in this candidate's one review conversation; "
                        "up to 45 distinct images fit in one review; larger evidence sets use "
                        "isolated six-image batches automatically. Inspect every required image; "
                        "retry only failed deliveries. PNG/original reads are unavailable."
                    )
        if not review_session:
            tools.append(
                read_tool(
                    "get_set_dependencies",
                    "Read one Set's film order, asset handoffs, missing classifications, "
                    "source status and owner. Planning and submission readiness are separate; "
                    "Set directors can read only their own published handoff summary.",
                    {"set_id": {"type": "string"}},
                    ["set_id"],
                )
            )
        if role in {"set_director", "visual_analyst", "task_analyst", "material_executor"}:
            for tool in tools:
                if tool["name"] in {"read_media", "batch_read_media"}:
                    tool["inputSchema"]["properties"]["force_reload"] = {
                        "type": "boolean",
                        "default": False,
                        "description": "Explicitly resend an image already in THIS context.",
                    }
        if role == "screenwriter":
            tools = [
                t
                for t in tools
                if t["name"]
                in {
                    "get_project",
                    "get_detail",
                    "get_body",
                    "get_operation_receipt",
                    "save_screenplay",
                    "set_status",
                }
            ]
        if role == "visual_analyst":
            tools = [
                t
                for t in tools
                if t["name"]
                in {
                    "get_project",
                    "get_required_context",
                    "read_media",
                    "batch_read_media",
                    "complete_visual_analysis",
                    "get_operation_receipt",
                    "set_status",
                }
            ]
            for tool in tools:
                if tool["name"] in {"read_media", "batch_read_media"}:
                    tool["inputSchema"]["properties"].pop("original", None)
        from .delegation import DELEGATE_ROLES, READ_TOOLS

        if role in DELEGATE_ROLES:
            permitted = frozenset(task_read_tools) if task_read_tools is not None else READ_TOOLS
            permitted |= frozenset(task_actions if task_actions is not None else ROLE_ACTIONS[role])
            tools = [t for t in tools if t["name"] in permitted]
            for tool in tools:
                if tool["name"] in {"read_media", "batch_read_media"} and role == "task_analyst":
                    tool["inputSchema"]["properties"].pop("original", None)
        for tool in tools:
            tool.setdefault(
                "annotations",
                {"readOnlyHint": tool["name"] not in ACTION_MODELS and tool["name"] != "act"},
            )
        for tool in tools:
            name = tool["name"]
            tool["name"] = PUBLIC.get(name, name)
            tool["description"] = DESCRIPTIONS.get(name, tool["description"])
            if name == "create_state":
                tool["inputSchema"]["properties"]["reference_purpose"] = {
                    "$ref": "#/$defs/StateReferencePurpose"
                }
                tool["inputSchema"].setdefault("required", []).append("reference_purpose")
            from .policy import DESCRIPTIONS as V2_DESCRIPTIONS

            tool["description"] = V2_DESCRIPTIONS.get(name, tool["description"])

        for tool in tools:
            if tool["name"] == "prepare_sets":
                tool["description"] += (
                    " Allocate duration_budget "
                    "{target_seconds,minimum_seconds,maximum_seconds,basis} per Set from "
                    "natural screenplay rhythm. Whole-film estimate guides the executive; "
                    "there is no fixed global cap. Missing budgets allow planning but not "
                    "video submission."
                )
            elif tool["name"] == "plan_clips":
                tool["description"] += (
                    " Set Clip duration sum must fit own approved duration_budget. complete "
                    "defaults true; false saves a partial plan and only a definite upper "
                    "overrun blocks. Out-of-range plans persist and automatically notify "
                    "executive; only this Set's new video submissions wait. Do not stretch "
                    "actions to meet minimum."
                )
            elif tool["name"] == "generate_clip":
                tool["description"] += (
                    " New-project submission also checks own Set duration interval; reference"
                    " history and ordinary decoder tails do not consume playback budget."
                )
            elif tool["name"] == "set_duration_budget":
                tool["description"] = (
                    "Executive sets/adjusts one Set target and allowed playback interval with"
                    " expected_budget_revision and a nonempty reason. Read planning/global "
                    "first. No global hard cap; keep close to screenplay natural estimate and"
                    " explain necessary adjustments. Preserve submitted recipes, selected "
                    "footage and quality counts. Out-of-range current plans remain waiting; "
                    "fitting plans release only this Set."
                )
        for tool in tools:
            if tool["name"] == "review_candidate":
                tool["inputSchema"].setdefault("required", []).append("selection_assessment")
                tool["description"] += (
                    " Record an evidence-backed selection_assessment of story_coverage, "
                    "identity_consistency and visual_quality. Each candidate is reviewed "
                    "independently. A passing review only finalizes the Clip after every "
                    "registered generation, result, necessary review and repair reservation "
                    "has settled; never close a Set with unreviewed candidates."
                )
        if self.compact_role_reads:
            for tool in tools:
                if tool["name"] in {"get_detail", "batch_get_details"}:
                    tool["inputSchema"]["properties"]["full"] = {
                        "type": "boolean",
                        "default": False,
                        "description": "Include the exact scoped history.",
                    }
                    tool["description"] += (
                        " Clips, candidates and Sets default to compact details with history "
                        "counts. Use full=true for original scoped history. Job recipes and "
                        "pinned review contracts are always exact."
                    )
        return tools

    def handle(
        self,
        project_id: str,
        actor_id: str,
        request: dict[str, Any],
        *,
        lease_generation: int | None = None,
        lease_owner: str | None = None,
    ) -> dict[str, Any] | None:
        from .runtime_metrics import DATABASE_TIME

        started, monotonic = time.time(), time.monotonic()
        counter = {"seconds": 0.0, "calls": 0.0}
        token = DATABASE_TIME.set(counter)
        success = False
        try:
            response = self._handle(
                project_id,
                actor_id,
                request,
                lease_generation=lease_generation,
                lease_owner=lease_owner,
            )
            success = (
                response is None
                or not response.get("error")
                and not response.get("result", {}).get("isError")
            )
            return response
        finally:
            DATABASE_TIME.reset(token)
            if request.get("method") == "tools/call":
                self.service.store.metrics.record(
                    project_id,
                    "mcp",
                    str(request.get("params", {}).get("name", "unknown")),
                    started,
                    time.monotonic() - monotonic,
                    actor_id=actor_id,
                    success=success,
                    details={
                        "database_seconds": counter["seconds"],
                        "database_calls": counter["calls"],
                    },
                )
                raw_arguments = request.get("params", {}).get("arguments", {})
                arguments = raw_arguments if isinstance(raw_arguments, dict) else {}
                tool_name = str(request.get("params", {}).get("name", "unknown"))
                action = (
                    str(arguments.get("action", ""))
                    if tool_name == "act"
                    else ALIASES.get(tool_name, tool_name)
                )
                if success and action in {"review_candidate", "finish_set", "finish_project"}:
                    payload_arguments = arguments.get("payload", arguments)
                    target_arguments = (
                        payload_arguments if isinstance(payload_arguments, dict) else arguments
                    )
                    entity_id = (
                        str(target_arguments.get("candidate_id", ""))
                        if action == "review_candidate"
                        else actor_id
                        if action == "finish_set"
                        else project_id
                    )
                    if entity_id:
                        self.service.store.metrics.performance.completed(
                            project_id, actor_id, action, entity_id, time.time()
                        )

    def _handle(
        self,
        project_id: str,
        actor_id: str,
        request: dict[str, Any],
        *,
        lease_generation: int | None = None,
        lease_owner: str | None = None,
    ) -> dict[str, Any] | None:
        request_id = request.get("id")
        method = request.get("method")
        if request_id is None:
            return None
        try:
            project = self.service.store.get(project_id)
            actor = get_actor(project, actor_id)
            if lease_generation is not None and not (
                actor.lease_generation == lease_generation
                and actor.lease_owner == lease_owner
                and lease_owner
                and actor.lease_until > time.time()
            ):
                raise StudioError("MCP 执行租约已失效")
            result: dict[str, Any]
            if method == "initialize":
                result = {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "agentvideo-canvas", "version": "2.0.0"},
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {
                    "tools": self.tools(
                        actor.role,
                        strict_context=full_context_required(project),
                        delegated=project.director_mode == "delegated-v1",
                        task_actions=list(actions_for(project, actor))
                        if actor.role in {"task_analyst", "material_executor"}
                        else None,
                        task_read_tools=list(read_tools_for(project, actor))
                        if actor.role in {"task_analyst", "material_executor"}
                        else None,
                        review_session=assignment(project, actor) is not None,
                        candidate_review=bool(
                            actor.sessions.get(actor.session_key)
                            and actor.sessions[actor.session_key].purpose == "candidate_review"
                        ),
                    )
                }
            elif method == "tools/call":
                params = request.get("params", {})
                tool_name = str(params.get("name"))
                for attempt in range(3):
                    try:
                        result = self.call(
                            project,
                            actor_id,
                            tool_name,
                            params.get("arguments", {}),
                            lease_generation=lease_generation,
                            lease_owner=lease_owner,
                            native_transport=True,
                        )
                        break
                    except (OperationalError, TimeoutError) as exc:
                        if ALIASES.get(tool_name, tool_name) in ACTION_MODELS or tool_name == "act":
                            key = params.get("arguments", {}).get("idempotency_key")
                            if key:
                                receipt = self.service.store.operation_receipt(
                                    project_id, actor_id, str(key)
                                )
                                if receipt["state"] == "committed":
                                    result = tool_result({"reconciled": True, **receipt})
                                    break
                            raise
                        if attempt == 2:
                            raise
                        if isinstance(exc, OperationalError) and not any(
                            k in str(exc).lower() for k in ("locked", "busy")
                        ):
                            raise
                        time.sleep(0.05 * (attempt + 1))
                        project = self.service.store.get(project_id)
            else:
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32601, "message": "Unknown method"},
                }
            return {"jsonrpc": "2.0", "id": request_id, "result": native_result(result)}
        except (
            StudioError,
            ValidationError,
            KeyError,
            ValueError,
            OSError,
            OperationalError,
        ) as exc:
            name = request.get("params", {}).get("name")
            failed_result = error_result(
                exc,
                write=name == "act" or ALIASES.get(name, name) in ACTION_MODELS,
                tool_name=name,
            )
            error = failed_result["structuredContent"]["error"]
            if error["code"] == "domain_conflict" and (
                name == "act" or ALIASES.get(name, name) in ACTION_MODELS
            ):
                from .production_runtime import record_domain_blocker

                arguments = request.get("params", {}).get("arguments") or {}
                if isinstance(arguments, dict):
                    payload = arguments.get("payload", arguments)
                    action = arguments.get("action") if name == "act" else ALIASES.get(name, name)

                    def retain(current: StudioProject) -> None:
                        state = current.actors.get(actor_id)
                        if state is None:
                            return
                        if lease_generation is not None and (
                            state.lease_generation != lease_generation
                            or state.lease_owner != lease_owner
                        ):
                            return
                        record_domain_blocker(
                            current,
                            state,
                            str(action),
                            error,
                            payload if isinstance(payload, dict) else {},
                        )

                    # Diagnostics cannot hide the definite rejection.
                    with contextlib.suppress(StudioError, OperationalError):
                        self.service.store.mutate(
                            project_id,
                            retain,
                            actor_id=actor_id,
                            kind="production_validation_rejected",
                            advance_revision=False,
                        )
            return {"jsonrpc": "2.0", "id": request_id, "result": failed_result}
        except Exception as exc:
            name = request.get("params", {}).get("name")
            # Keep transport responses recoverable even for implementation defects.
            # Log code locations, never arguments, credentials or exception payloads.
            locations = [
                f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}"
                for frame in traceback.extract_tb(exc.__traceback__)
            ]
            logging.getLogger(__name__).error(
                "Unexpected MCP failure tool=%s type=%s locations=%s",
                name,
                type(exc).__name__,
                locations,
            )
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": error_result(
                    exc,
                    write=name == "act" or ALIASES.get(name, name) in ACTION_MODELS,
                    tool_name=name,
                ),
            }

    def call(
        self,
        project: StudioProject,
        actor_id: str,
        name: str,
        args: dict[str, Any],
        *,
        lease_generation: int | None = None,
        lease_owner: str | None = None,
        image_batch_id: str | None = None,
        native_transport: bool = False,
    ) -> dict[str, Any]:
        value: Any
        actor = project.actors[actor_id]
        from .delegation import DELEGATE_ROLES

        if actor.role in DELEGATE_ROLES:
            from .delegation import RECORD_READ_TOOLS

            if name not in RECORD_READ_TOOLS | {"complete_delegate_task", "set_status"}:
                require(
                    not self.service.store.actor_records_only(project.id, actor_id),
                    "专项子对话只能使用当前任务授权的工具",
                )
            require(
                name in read_tools_for(project, actor) | actions_for(project, actor),
                "专项子对话只能使用任务授权的工具",
            )
        # Native input needs its successful response binding. Explicit legacy
        # transport retains its historical later-tool acknowledgement.
        if not native_transport:
            project = self._ack_pending_image_deliveries(project, actor_id)
            actor = project.actors[actor_id]
        visibility = Visibility(project, actor_id)
        if project.actors[actor_id].role == "visual_analyst":
            require(
                name
                in {
                    "get_project",
                    "get_required_context",
                    "read_media",
                    "batch_read_media",
                    "complete_visual_analysis",
                    "get_operation_receipt",
                    "set_status",
                },
                "看图子对话只能访问分配的任务与图片",
            )
        name = ALIASES.get(name, name)
        guard_read(project, project.actors[actor_id], name, args)
        if name == "get_project":

            def read_overview(current: StudioProject) -> dict[str, Any]:
                actor = current.actors[actor_id]
                records = (
                    overview(current, actor)
                    if assignment(current, actor) is not None
                    else context(current, actor_id, self.service.store)
                )
                if self.compact_role_reads:
                    from .context_projection import compact_records

                    return compact_records([records], role_read=True)[0]
                return records

            value = guarded_read(
                self.service.store, project.id, actor_id, "overview", read_overview
            )
        elif name in {"get_required_context", "get_clip_work_packet", "get_review_work_packet"}:
            packet = name == "get_clip_work_packet"
            review_packet = name == "get_review_work_packet"
            if review_packet:
                current_assignment = assignment(project, project.actors[actor_id])
                require(
                    (
                        current_assignment is not None
                        and current_assignment["purpose"] == "candidate_review"
                    ),
                    "审核工作包仅用于新流程的当前独立审核会话",
                )
            if packet:
                visibility.require("clips", str(args.get("clip_id")))
            target_id = str(
                args.get("clip_id")
                if packet
                else args.get("candidate_id")
                if review_packet
                else args.get("target_id")
            )
            phase = cast(
                ContextPhase, "review" if review_packet else args.get("phase", "references")
            )
            value = read_required_context(
                self.service.store,
                project.id,
                actor_id,
                phase,
                target_id,
                int(args["page"]) if "page" in args else None,
                args.get("fingerprint"),
                mode=str(args.get("mode", "auto")),
                baseline_token=args.get("baseline_token"),
                snapshot_id=args.get("snapshot_id"),
                full_verification_reason=args.get("full_verification_reason"),
            ).model_dump(mode="json")
            value["read_token"] = value.get("context_token")
            value["next_request"] = (
                {
                    "phase": value["phase"],
                    "target_id": value["target_id"],
                    "page": value["next_page"],
                    "fingerprint": value["fingerprint"],
                    "snapshot_id": value["snapshot_id"],
                    "mode": args.get("mode", "auto"),
                }
                if value["next_page"] is not None
                else None
            )
            if packet:
                if value["next_request"]:
                    value["next_request"]["clip_id"] = value["next_request"].pop("target_id")

                def read_packet(current: StudioProject) -> dict[str, Any]:
                    from .store import StudioContextRestartRequired
                    from .task_context import context_fingerprint

                    Visibility(current, actor_id).require("clips", target_id)
                    if (
                        context_fingerprint(current, target_id, phase, actor_id)
                        != value["fingerprint"]
                    ):
                        raise StudioContextRestartRequired(
                            "资料在合并读取时更新，请重新读取", phase=phase, target_id=target_id
                        )
                    clip = current.clips[target_id]
                    extra: dict[str, Any] = {
                        "readiness": clip_readiness(current, target_id).model_dump(mode="json"),
                        "reference_candidates": reference_options(
                            current, actor_id, target_id, limit=100
                        ).model_dump(mode="json"),
                        "preparation": clip.preparation.model_dump(mode="json")
                        if clip.preparation
                        else None,
                        "prepare_next_clip": current.config.prepare_next_clip,
                    }
                    from .planning_sessions import assignment as planning_assignment

                    extra["planning_assignment"] = planning_assignment(
                        current, current.actors[actor_id]
                    )
                    from .director_efficiency import (
                        compact_records,
                        continuity_packet,
                        repair_packet,
                    )
                    from .quality_progress import quality_progress

                    extra["quality_progress"] = quality_progress(current, clip).model_dump(
                        mode="json"
                    )
                    extra = compact_records([extra])[0]
                    if extra["preparation"]:
                        for key in (
                            "stable_snapshot",
                            "continuity_snapshot",
                            "context_dependencies",
                            "context_fingerprint",
                            "source_fingerprint",
                        ):
                            extra["preparation"].pop(key, None)
                    plan = current.reference_plans.get(clip.reference_plan_id or "")
                    extra["reference_plan"] = (
                        plan.model_dump(
                            include={
                                "id",
                                "revision",
                                "status",
                                "bindings",
                                "audio_references",
                                "video_references",
                                "prompt",
                                "automatic_draft",
                                "changes",
                                "invalidation_reasons",
                                "preparation_id",
                                "preparation_revision",
                            },
                            mode="json",
                        )
                        if plan
                        else None
                    )
                    extra["repair_packet"] = repair_packet(current, target_id)
                    extra["continuity_preflight"] = continuity_packet(current, target_id)
                    return extra

                if not args.get("page"):
                    value.update(self.service.store.read(project.id, read_packet))
            if review_packet:
                if value["next_request"]:
                    value["next_request"].pop("phase", None)
                    value["next_request"]["candidate_id"] = value["next_request"].pop("target_id")

                def review_index(current: StudioProject) -> dict[str, Any]:
                    from .context_cache import dependencies_current, session_key
                    from .store import StudioContextRestartRequired
                    from .task_context import context_fingerprint

                    receipt = current.context_reads.get(value["context_id"])
                    if not (
                        receipt
                        and receipt.snapshot_id == value["snapshot_id"]
                        and receipt.fingerprint == value["fingerprint"]
                        and receipt.session_key == session_key(current.actors[actor_id], current)
                        and (
                            dependencies_current(
                                current, receipt.dependencies, receipt.core_fingerprint
                            )
                            or context_fingerprint(current, target_id, "review", actor_id)
                            == value["fingerprint"]
                        )
                    ):
                        raise StudioContextRestartRequired(
                            "审核资料在合并读取时变化，请重新读取",
                            phase="review",
                            target_id=target_id,
                        )
                    current_assignment = assignment(current, current.actors[actor_id])
                    require(
                        current_assignment and current_assignment["candidate_id"] == target_id,
                        "审核会话已变化",
                    )
                    assert current_assignment is not None
                    remaining = current_assignment["remaining_media_ids"]
                    return {
                        "review_assignment": {
                            key: current_assignment[key]
                            for key in ("purpose", "candidate_id", "clip_id")
                        },
                        **(
                            {
                                "actor_instructions": actor_instruction_packet(
                                    current.actors[actor_id]
                                )
                            }
                            if not args.get("page")
                            else {}
                        ),
                        "unseen_image_count": len(remaining),
                        "next_media_batch": remaining[:6] if value["next_page"] is None else [],
                    }

                value.update(self.service.store.read(project.id, review_index))
        elif name == "query_memory":
            scope = str(args.get("scope", "global"))
            if scope != "*":
                visibility.scope("memory:" + scope)
            for key in ("asset_id", "clip_id", "event_id"):
                if args.get(key):
                    visibility.target(str(args[key]))
            value = (
                StudioQueries(self.service.store)
                .memory_page(
                    project.id,
                    scope=str(args.get("scope", "global")),
                    status=str(args.get("status", "effective")),
                    asset_id=args.get("asset_id"),
                    clip_id=args.get("clip_id"),
                    event_id=args.get("event_id"),
                    query=str(args.get("text", "")),
                    after=args.get("after"),
                    limit=int(args.get("limit", 50)),
                )
                .model_dump(mode="json")
            )
            if visibility.isolated:
                value["items"] = [
                    item
                    for item in value["items"]
                    if visibility.allowed("memory_entries", item["id"])
                ]
                value["entity_versions"] = {
                    k: v
                    for k, v in value["entity_versions"].items()
                    if visibility.allowed("memory_entries", k.split(":", 1)[1])
                }
        elif name == "get_executive_inbox":
            from .delegation import inbox

            value = guarded_read(
                self.service.store,
                project.id,
                actor_id,
                "inbox",
                lambda p: inbox(
                    p,
                    p.actors[actor_id],
                    after=args.get("after"),
                    limit=int(args.get("limit", 50)),
                    item_id=args.get("item_id"),
                ),
            )
        elif name == "get_executive_documents":
            from .delegation import document_index

            def read_documents(p: StudioProject) -> dict[str, Any]:
                actor = p.actors[actor_id]
                index = document_index(p, actor)
                if args.get("key"):
                    identifier = hashlib.sha256(
                        json.dumps(
                            [actor.id, args["key"]],
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode()
                    ).hexdigest()
                    doc = p.executive_documents.get(identifier)
                    require(doc and doc.actor_id == actor.id, "运行小结不存在")
                    assert doc is not None
                    if args.get("revision") and args["revision"] != doc.revision:
                        from .memory import digest

                        version = p.executive_document_versions.get(
                            digest([identifier, args["revision"]])
                        )
                        require(version and version.actor_id == actor.id, "该历史小结修订不存在")
                        assert version is not None
                        return {"document": version.model_dump(mode="json"), "historical": True}
                    return {"document": doc.model_dump(mode="json")}
                return {"index": index}

            value = guarded_read(
                self.service.store, project.id, actor_id, "documents", read_documents
            )
        elif name in ACTION_MODELS:
            require(
                name in ROLE_ACTIONS[project.actors[actor_id].role], "当前角色无此工具的写入权限"
            )
            data = (
                action_model(name)
                .model_validate(args)
                .model_dump(mode="json", exclude_unset=True, exclude_none=True)
            )
            key, read_token = data.pop("idempotency_key"), data.pop("read_token")
            if name in {
                "generate_reference",
                "select_reference",
                "review_reference",
                "resolve_quality",
                "correct_memory_scope",
                "correct_state_scope",
                "record_state_evolution",
                "plan_story_events",
                "prepare_sets",
                "set_duration_budget",
                "publish_set_context",
                "configure_set_dependencies",
                "configure_state_preparation",
                "confirm_set_handoff",
                "adopt_observed_reference",
                "plan_clips",
                "update_clip_context",
                "save_reference_plan",
                "patch_reference_plan",
                "finalize_reference_plan",
                "generate_clip",
                "review_candidate",
                "confirm_candidate_state",
                "save_clip_preparation",
                "finish_review_batch",
                "request_review_batch",
                "complete_visual_analysis",
                "supply_repair_references",
                "approve_reference_region",
                "approve_reference_coverage",
                "complete_delegate_task",
                "adopt_prepared_reference",
            }:
                data["context_token"] = read_token
            updated = self.service.execute(
                project.id,
                actor_id,
                ToolCommand.model_validate(
                    {
                        "action": name,
                        "read_token": read_token,
                        "idempotency_key": key,
                        "payload": data,
                    }
                ),
                lease_generation=lease_generation,
                lease_owner=lease_owner,
                native_transport=native_transport,
            )
            value = self.service.store.command_receipt(updated)
        elif name == "act":
            updated = self.service.execute(
                project.id,
                actor_id,
                ToolCommand.model_validate(args),
                lease_generation=lease_generation,
                lease_owner=lease_owner,
                native_transport=native_transport,
            )
            value = self.service.store.command_result(updated).model_dump(mode="json")
        elif name == "read_media":
            from .image_input_recovery import quarantine, record_transport

            visibility.require("media", str(args.get("media_id")))
            media = project.media.get(str(args.get("media_id")))
            if media is None:
                raise StudioError("素材不存在", 404)
            if not media.mime_type.startswith("image/"):
                raise StudioError("请通过 analyze_candidate 分析视频，再查看实际末帧")
            blocked = quarantine(project, media.id)
            if blocked:
                # A human/operator receipt is deliberately not registered as a
                # native image inspection.  It lets the actor consume the
                # operator's written observation while the original provider
                # refusal and quarantine remain visible and immutable.
                from .external_review import external_review_item

                actor = project.actors[actor_id]
                session = actor.sessions.get(actor.session_key)
                candidate_id = session.candidate_id if session else None
                clip_id = session.planning_clip_id if session else None
                if candidate_id and candidate_id in project.candidates:
                    clip_id = project.candidates[candidate_id].clip_id
                external = external_review_item(
                    project,
                    media.id,
                    "reference_inspection",
                    actor_id=actor_id,
                    clip_id=clip_id,
                    candidate_id=candidate_id,
                )
                if external and external[1].verdict == "approved":
                    review, item = external
                    return tool_result(
                        {
                            "media_id": media.id,
                            "source_sha256": media.sha256,
                            "status": "external_operator_review",
                            "inspected": False,
                            "native_model_inspection": False,
                            "original_quarantine": {
                                "id": blocked.id,
                                "recovery_id": blocked.recovery_id,
                                "reason": blocked.reason,
                            },
                            "receipts": [
                                {
                                    "external_review_id": review.id,
                                    "reviewer_id": review.reviewer_id,
                                    "authorization_id": review.authorization_id,
                                    "scope": review.scope.model_dump(mode="json"),
                                    "observation": item.observation,
                                    "decision": item.verdict,
                                }
                            ],
                            "summary": (
                                "图片仍保留原隔离记录；原生模型未查看像素。"
                                "可在收据记录的精确角色/Clip/用途范围内采用外部人工观察。"
                            ),
                            "retryable": False,
                        }
                    )
                response = {
                    "media_id": media.id,
                    "status": "image_quarantined",
                    "inspected": False,
                    "recovery_id": blocked.recovery_id,
                    "reason": blocked.reason,
                    "retryable": False,
                }
                if external:
                    review, item = external
                    response["external_operator_review"] = {
                        "external_review_id": review.id,
                        "reviewer_id": review.reviewer_id,
                        "authorization_id": review.authorization_id,
                        "scope": review.scope.model_dump(mode="json"),
                        "observation": item.observation,
                        "decision": item.verdict,
                    }
                return tool_result(response)
            delivery_id = image_batch_id or uuid4().hex
            original = args.get("original", False)
            require(isinstance(original, bool), "original 必须为布尔值")
            force_reload = args.get("force_reload", False)
            require(isinstance(force_reload, bool), "force_reload 必须为布尔值")
            from .director_efficiency import image_key

            require(
                not force_reload
                or (
                    (project.actors[actor_id].role == "set_director")
                    or project.actors[actor_id].role
                    in {"visual_analyst", "task_analyst", "material_executor"}
                ),
                "明确重送只适用于新流程导演或图片任务角色",
            )
            actor = project.actors[actor_id]
            session = actor.sessions.get(actor.session_key)
            detailed_review = actor.role in {"visual_analyst", "task_analyst"} or bool(
                session and session.purpose == "candidate_review"
            )
            require(not (detailed_review and original), "审核图片统一使用 JPEG 95，不提供 PNG 回读")
            if (
                not force_reload
                and actor.role in {"visual_analyst", "task_analyst", "material_executor"}
                and session
                and media.id in session.viewed_media_ids
            ):
                return tool_result(
                    {
                        "media_id": media.id,
                        "status": "already_in_context",
                        "summary": "这张图片已送入当前委托会话，沿用已看画面；不重复增加图片输入",
                    }
                )
            policy = review_view_policy() if detailed_review else None
            preview_key = (json.dumps(policy, sort_keys=True) if policy else "") + media.sha256
            with self._preview_lock:
                cached = self._previews.get(preview_key) if not original else None
                if cached:
                    self._previews.move_to_end(preview_key)
            if cached:
                viewed, mime_type, preview = cached
            else:
                with self.cas.open(media.sha256) as stream:
                    source = stream.read()
                viewed, mime_type = (
                    review_image_view(source, media.mime_type)
                    if detailed_review
                    else image_view(source, media.mime_type, original=original)
                )
                preview = viewed != source
                if not original and (detailed_review or len(viewed) <= 256 * 1024):
                    with self._preview_lock:
                        self._previews[preview_key] = (viewed, mime_type, preview)
                        while (
                            len(self._previews) > 64
                            or sum(len(value[0]) for value in self._previews.values())
                            > 64 * 1024 * 1024
                        ):
                            self._previews.popitem(last=False)
            view_record = {
                "media_id": media.id,
                "source": "mcp.read_media",
                "source_sha256": media.sha256,
                "view_sha256": hashlib.sha256(viewed).hexdigest(),
                "preview": preview,
                "view_bytes": len(viewed),
                "view_profile": policy["version"] if policy else "preview-v1",
                **({"view_parameters": policy} if policy else {}),
            }
            inspection_receipt: dict[str, str] = {}
            excluded_receipt: dict[str, Any] = {}
            # Persist the exact inspection body outside the shared writer lock.
            # The guarded transaction below must still validate the current lease
            # before registering either the transcript or an inspection receipt.
            self.service.store.prepare_body({"text": "read_media", "data": view_record})

            def stage(p: StudioProject) -> dict[str, Any]:
                actor = get_actor(p, actor_id)
                require(
                    actor.role not in DELEGATE_ROLES or not records_only(task_for(p, actor)),
                    "仅记录任务不能读取像素或登记图像检查",
                )
                Visibility(p, actor_id).require("media", media.id)
                if lease_generation is not None:
                    require(
                        actor.lease_generation == lease_generation
                        and actor.lease_owner == lease_owner
                        and actor.lease_until > time.time(),
                        "MCP 执行租约已失效",
                    )
                blocked = quarantine(p, media.id)
                if blocked:
                    excluded_receipt.update(
                        {
                            "media_id": media.id,
                            "status": "image_quarantined",
                            "inspected": False,
                            "recovery_id": blocked.recovery_id,
                            "reason": blocked.reason,
                            "retryable": False,
                        }
                    )
                    return excluded_receipt
                session = actor.sessions.get(actor.session_key)
                if session:
                    key = image_key(p, actor, media.sha256, policy or {"original": original})
                    prior_media_id = session.image_receipts.get(key)
                    # Explicit reloading is a recovery escape hatch for
                    # a caller that did not receive a prior tool image. Normal
                    # review reads still deduplicate the current context.
                    reload_allowed = force_reload and (
                        session.purpose in {"planning", "candidate_review", "review_batch"}
                        or actor.role in {"visual_analyst", "task_analyst", "material_executor"}
                    )
                    if not reload_allowed and prior_media_id:
                        unseen_alias = media.id not in session.viewed_media_ids
                        reserve_image(p, actor, media.id, reused=True)
                        if prior_media_id != media.id and unseen_alias:
                            # A second ID for the same bytes is inspected by the
                            # existing image. Preserve its own citation receipt.
                            add_item(
                                p,
                                actor_id,
                                "tool",
                                "read_media",
                                {
                                    **view_record,
                                    "reused_from_media_id": prior_media_id,
                                },
                            )
                        excluded_receipt.update(
                            {
                                "media_id": media.id,
                                "status": "already_in_context",
                                "reused_from_media_id": prior_media_id,
                                "summary": (
                                    "相同图片已在当前有效上下文中，"
                                    "复用已看画面与证据收据，不重复送图"
                                ),
                            }
                        )
                        return {"action": "read_media", **excluded_receipt}
                if (
                    not force_reload
                    and actor.role in {"visual_analyst", "task_analyst", "material_executor"}
                    and session
                    and media.id in session.viewed_media_ids
                ):
                    excluded_receipt.update(
                        {
                            "media_id": media.id,
                            "status": "already_in_context",
                            "summary": "图片已在当前委托会话中，不重复送入",
                        }
                    )
                    return {"action": "read_media", **excluded_receipt}
                session = actor.sessions.setdefault(
                    actor.session_key, ActorSession(key=actor.session_key)
                )
                reserve_image(p, actor, media.id, commit=False)
                record_transport(p, actor, media.id, delivery_id)
                deferred = (
                    native_transport
                    or actor.role
                    in {
                        "visual_analyst",
                        "task_analyst",
                        "material_executor",
                    }
                    or (
                        actor.role == "set_director"
                        and session.purpose in {"candidate_review", "review_batch"}
                    )
                )
                item_id = add_item(
                    p,
                    actor_id,
                    "tool",
                    "read_media",
                    {
                        **view_record,
                        "source": "mcp.read_media.pending" if deferred else "mcp.read_media",
                        "delivery_confirmation": (
                            "awaiting_native_input" if deferred else "returned_in_current_call"
                        ),
                        "native_transport": native_transport,
                    },
                )
                receipt_key = image_key(p, actor, media.sha256, policy or {"original": original})
                if deferred:
                    actor.pending_image_deliveries.append(
                        PendingImageDelivery(
                            media_id=media.id,
                            session_key=actor.session_key,
                            actor_context_epoch=actor.context_epoch,
                            session_context_epoch=session.context_epoch,
                            thread_id=actor.thread_id,
                            delivery_id=delivery_id,
                            receipt_key=receipt_key,
                            item_id=item_id,
                        )
                    )
                else:
                    from .image_input_recovery import confirm_delivery

                    reserve_image(p, actor, media.id)
                    session.image_receipts[receipt_key] = media.id
                    confirm_delivery(p, actor, media.id)
                inspection_receipt["item_id"] = item_id
                return {"item_id": item_id, "item": p.transcript[item_id].model_dump(mode="json")}

            self.service.store.mutate(
                project.id,
                stage,
                actor_id=actor_id,
                kind="transcript.upsert",
                advance_revision=False,
            )
            if excluded_receipt:
                return tool_result(excluded_receipt)
            content: list[dict[str, Any]] = [
                {"type": "image", "data": base64.b64encode(viewed).decode(), "mimeType": mime_type}
            ]
            if preview:
                content.append(
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                **view_record,
                                "note": (
                                    "JPEG 95 evidence at the fixed review size; inspect this view "
                                    "and use focused sampling in the same session for uncertainty."
                                    if detailed_review
                                    else "Compressed view of the actual media; original CAS image "
                                    "is unchanged. Use original=true if fine detail is unclear."
                                ),
                            }
                        ),
                    }
                )
            return tool_result({**view_record, **inspection_receipt}, content=content)
        elif name in {"batch_get_details", "batch_read_media"}:
            is_media = name == "batch_read_media"
            ids = args.get("media_ids" if is_media else "scopes")
            require(
                isinstance(ids, list)
                and 1 <= len(ids) <= (6 if is_media else 12)
                and all(isinstance(i, str) and i for i in ids),
                "批量读取参数或数量无效",
            )
            items, content = [], []
            batch_delivery_id = uuid4().hex if is_media else None
            assert isinstance(ids, list)
            for identifier in dict.fromkeys(ids):
                try:
                    for attempt in range(3):
                        try:
                            single = self.call(
                                project,
                                actor_id,
                                "read_media" if is_media else "get_detail",
                                {
                                    "media_id": identifier,
                                    "original": args.get("original", False),
                                    "force_reload": args.get("force_reload", False),
                                }
                                if is_media
                                else {
                                    "scope": identifier,
                                    **({"full": True} if args.get("full") else {}),
                                },
                                lease_generation=lease_generation,
                                lease_owner=lease_owner,
                                image_batch_id=batch_delivery_id,
                                native_transport=native_transport,
                            )
                            break
                        except (OperationalError, TimeoutError) as exc:
                            if (
                                attempt == 2
                                or isinstance(exc, OperationalError)
                                and not any(k in str(exc).lower() for k in ("locked", "busy"))
                            ):
                                raise
                            time.sleep(0.05 * (attempt + 1))
                    items.append(
                        {"id": identifier, "ok": True, "result": single["structuredContent"]}
                    )
                    if is_media:
                        content.append({"type": "text", "text": "media_id=" + identifier})
                        content.extend(single["content"])
                except (StudioError, ValueError, KeyError, OSError, OperationalError) as exc:
                    items.append(
                        {"id": identifier, "ok": False, **error_result(exc)["structuredContent"]}
                    )
            value = {
                "items": items,
                "partial": any(not i["ok"] for i in items),
                "retry_ids": [i["id"] for i in items if not i["ok"]],
            }
            if is_media:
                content.append({"type": "text", "text": json.dumps(value, ensure_ascii=False)})
                return tool_result(value, content=content)
        elif name == "get_set_dependencies":
            from .set_handoffs import dependency_report

            visibility.require("sets", str(args.get("set_id")))
            value = self.service.store.read(
                project.id,
                lambda p: dependency_report(p, str(args["set_id"])).model_dump(mode="json"),
            )
        elif name == "get_playback_duration":
            require(
                project.actors[actor_id].role in {"executive", "set_director"},
                "仅导演可读取制作片长预算",
            )
            from .playback import report as playback_report

            target = (
                str(args["set_id"])
                if args.get("set_id")
                else visibility.set_id
                if visibility.isolated
                else None
            )
            if target:
                visibility.require("sets", target)
            value = self.service.store.read(
                project.id, lambda p: playback_report(p, set_id=target).model_dump(mode="json")
            )
        elif name == "get_clip_readiness":
            visibility.require("clips", str(args.get("clip_id")))
            value = self.service.store.read(
                project.id,
                lambda p: clip_readiness(p, str(args.get("clip_id"))).model_dump(mode="json"),
            )
        elif name == "get_reference_candidates":
            visibility.require("clips", str(args.get("clip_id")))
            value = self.service.store.read(
                project.id,
                lambda p: reference_options(
                    p,
                    actor_id,
                    str(args.get("clip_id")),
                    after=args.get("after"),
                    limit=int(args.get("limit", 100)),
                ).model_dump(mode="json"),
            )
        elif name == "compare_candidates":
            visibility.require("clips", str(args.get("clip_id")))
            value = self.service.store.read(
                project.id,
                lambda p: compare_candidates(
                    p,
                    str(args.get("clip_id")),
                    after=args.get("after"),
                    limit=int(args.get("limit", 100)),
                ).model_dump(mode="json"),
            )
        elif name == "get_operation_receipt":
            value = self.service.store.operation_receipt(
                project.id, actor_id, str(args.get("idempotency_key"))
            )
        elif name == "get_history":
            kind = args.get("kind") or ("candidates" if args.get("clip_id") else "states")
            parent_id = args.get("parent_id") or args.get("clip_id") or args.get("asset_id")
            require(parent_id, "需要历史所属实体 ID")
            visibility.target(str(parent_id))
            value = (
                StudioQueries(self.service.store)
                .history(
                    project.id,
                    str(kind),
                    str(parent_id),
                    args.get("after"),
                    int(args.get("limit", 50)),
                )
                .model_dump(mode="json")
            )
            if visibility.isolated:
                collection = "candidates" if kind == "usage" else str(kind)
                value["items"] = [
                    v for v in value["items"] if visibility.allowed(collection, v["id"])
                ]
                value["related"] = visibility.payload(value["related"])
                value["entity_versions"] = {
                    k: v
                    for k, v in value["entity_versions"].items()
                    if visibility.allowed(*k.split(":", 1))
                }
                value["ancestor_ids"] = [
                    i for i in value.get("ancestor_ids", []) if visibility.allowed("references", i)
                ]
        elif name == "get_detail":
            scope = str(args.get("scope"))
            visibility.scope(scope)
            current_assignment = assignment(project, project.actors[actor_id])
            if scope == f"actor:{actor_id}":

                def read_actor_instructions(current: StudioProject) -> dict[str, Any]:
                    current_actor = current.actors[actor_id]
                    guard_read(current, current_actor, name, args)
                    if (
                        current_assignment is not None
                        and current_assignment["purpose"] == "candidate_review"
                    ):
                        current_review = assignment(current, current_actor)
                        require(
                            current_review is not None
                            and current_review["purpose"] == "candidate_review",
                            "当前审核会话已变化，请重新读取任务",
                        )
                    return actor_instruction_detail(current, current_actor)

                value = self.service.store.read(project.id, read_actor_instructions)
            else:
                value = (
                    StudioQueries(self.service.store)
                    .snapshot(project.id, scope)
                    .model_dump(mode="json")
                )
                if visibility.isolated:
                    value["project"] = visibility.payload(value["project"])
                    value["entity_versions"] = {
                        k: v
                        for k, v in value["entity_versions"].items()
                        if visibility.allowed(*k.split(":", 1))
                    }
                    value["counts"] = {
                        "sets": 1,
                        "clips": len(value["project"].get("clips", {})),
                        "assets": len(value["project"].get("assets", {})),
                    }
            if (
                self.compact_role_reads
                and not args.get("full")
                and scope.partition(":")[0] in {"clip", "candidate", "set"}
            ):
                from .context_projection import compact_records

                value = {
                    **compact_records([value], role_read=True)[0],
                    "detail_projection": "compact",
                }
            if scope.startswith("reference-plan:"):
                from .reference_plan_bodies import detail_metadata

                identifier = scope.removeprefix("reference-plan:")
                version = value["entity_versions"][f"reference_plans:{identifier}"]
                metadata = self.service.store.read(
                    project.id,
                    lambda current: detail_metadata(current, actor_id, identifier, version),
                )
                value = {**metadata, **value}
        elif name == "get_body":
            body_ref = str(args.get("body_ref"))
            if body_ref.startswith("reference-plan:"):
                from .reference_plan_bodies import read_body as read_reference_plan_body

                value = self.service.store.read(
                    project.id,
                    lambda current: read_reference_plan_body(
                        current,
                        actor_id,
                        body_ref,
                        int(args.get("offset", 0)),
                        int(args.get("limit", 65536)),
                    ),
                )
            else:
                visibility.body(self.service.store, body_ref)
                value = self.service.store.body(
                    project.id,
                    body_ref,
                    int(args.get("offset", 0)),
                    int(args.get("limit", 65536)),
                ).model_dump(mode="json")

        else:
            raise StudioError("工具不存在", 404)
        if name in {
            "get_project",
            "get_detail",
            "get_required_context",
            "get_clip_work_packet",
            "get_review_work_packet",
        }:
            value.setdefault("full_context_policy", project.config.full_context_policy)
        return tool_result(value)
