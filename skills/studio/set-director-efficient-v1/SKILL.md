---
name: set-director-efficient-v1
description: Efficient Set director input, lookahead promotion, single-candidate review and targeted repair for projects explicitly using efficient-v1; preserve the loaded media and quality rules.
metadata:
  version: 1.0.0
---

This package refines the loaded Set director/reviewer instructions only for a project
using `set_director_policy=efficient-v1`. Existing media limits, access fences, actual
continuation, quality allowances and paused/unknown-request protections still apply.

## Planning

Use `get_clip_work_packet` for a known current Clip. Read every context page, applying
verified deltas; routine returns from review preserve this planning conversation's
baseline. A new target still needs its own complete baseline. After compaction or
native-context replacement, reread full context; do not borrow another session's receipts.

The packet contains an automatic reference draft when valid lookahead and the selected
predecessor's true endpoint/tail are ready. It is NOT submitted or visually approved.
Inspect the actual materials and opening; preserve valid draft work instead of rebuilding
all bindings. Use its current plan ID and normal finalization/generation tools. Do not
overwrite a director-edited/finalized plan, assume a planned state is observed, or carry
stale material versions forward. Disabled/incomplete preparation does not auto-promote.
Use `pending_requirements` only for unresolved stable story/material needs. The runtime
tracks predecessor selection and real endpoint/tail readiness separately; do not leave
a generic "waiting for previous selection" entry after the stable work is ready.

For continuations, finalization needs `continuity_check` with `action`, `space`, `camera`,
`completed_actions`, and `unknowns`: describe incoming actual motion stage/direction/speed,
placement/framing, camera trajectory or intentional cut, actions not to replay, and any
unconfirmed/occluded facts. Compare to the real predecessor, not its planned ending.
Record "none observed" when appropriate, not fabricated certainty. This is your judgment
in this planning conversation, not a new checker conversation or a machine-certified seam.

On rejection use the packet's findings, evidence, preservation guidance and suggested
changes. Decide regenerate versus full-current-candidate edit yourself, request optional
scoped material support when useful, and author the final six-section prompt. Follow the
original repair ledger; suggestions do not authorize extra attempts or accept failed footage.

## Single-candidate review

For the candidate ID already in your native goal, START with
`get_review_work_packet(candidate_id=...)`; this replaces the earlier role package's
`get_project` + `get_required_context` entry sequence. Read `get_project` only if the
assignment is missing or has changed. Follow `next_request` until null and use its
final read_token.
Read `next_media_batch` and all remaining required images in this SAME conversation.
The transport still returns at most six pictures per call; coverage and JPEG95 size stay
unchanged. Inspect the actual candidate evidence and the relevant predecessor seam and
tail in the assigned review conversation.

Repeated same-image reads in this valid native context return `already_in_context`.
Use the existing image and its index. If delivery was failed/uncertain, or you explicitly
need retransmission, request `force_reload=true`; only fixed JPEG95 is resent. Quarantined
images cannot be resent. A new/compacted context must actually receive its own images.
Request focused new evidence only for a concrete unresolved question; preserve full
coverage and honest audio/inter-frame/occlusion limitations.

A rejection must provide one `repair_guidance` item per repair-severity finding:
`finding_index`, `suggested_change`, `preserve`, `material_gap` (null when no extra material
is needed), and `next_check`. Findings already bind exact times and viewed evidence.
Give specific changes and resource gaps, not a complete replacement prompt or a binding
repair-mode decision. Final production decisions remain in the planning conversation.
