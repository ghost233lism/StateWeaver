---
name: image-input-recovery-v1
description: Continue director work after a recorded image-input refusal with quarantined evidence, explicit coverage limits, and one bounded recovery.
---

# Image input recovery

The runtime may return `status=image_quarantined`, `inspected=false` for an image.
This is an unavailable input, not a failed video or proof that the picture was
inspected. The original picture and generation recipe remain archived. A batch
is only suspected: the upstream error did not identify an individual picture.

Read the remaining permitted evidence and task context. Do not rerequest excluded
images, extract replacements of the excluded evidence, transform them, or switch
providers to get those same inputs processed. Do not ask another director to read
the batch; quarantine applies to all directors in this project.

For this recorded exception, partial coverage replaces the normal all-images
review gate. Read `review_coverage.available_media_ids` (or the current review
assignment), then inspect those permitted images in the replacement context.
For video review, the user permits partial coverage. Decide from the remaining
actual evidence, state what remains unknown, and record limitations. Do not infer
an unseen action or end state from the prompt. Cite only images actually available
and inspected. `coverage_gaps` and their time ranges are retained automatically;
explain their impact in your reason/limitations. An otherwise usable candidate
may be selected with a limited review. Visible substantive defects still follow
the original repair and executive-compromise rules.

If the actual endpoint image is quarantined, retain its exact ID in
`final_end_state.frame_media_id`, but describe the ending as unverified. Base any
tentative statements on remaining current-candidate evidence, include that
evidence in `end_state_media_ids`, and leave `visible_state_ids` empty. Do not
invent an ending from the screenplay or certify asset states. The backend records
this as a limited end-state confirmation. Evidence of only the predecessor or
reference assets cannot justify selecting a wholly unseen candidate.

Image quarantine does not approve an unseen asset, resolve a conflicting story
state, or change required reference bindings. Continue independent planning from
the permitted material; keep uncertain observations explicit.

For the next Clip, an already-selected predecessor's genuine endpoint remains a
mandatory pinned continuity input. If its end-state confirmation explicitly
records that endpoint as uninspected and certifies no asset states, the reference
plan records `inspection_gaps` and permits this boundary-only exception. Keep the
real endpoint and tail; do not request excluded imagery again or try to obtain an
inspection from another model. Explain the uncertainty in `prompt_reconciliation`
and `conflict_check`; do not invent a pose or position from the screenplay. All
ordinary asset references and correction evidence still require inspection.

The runtime can archive and replace native context once for this refusal, within
the same logical conversation. Read the durable canvas again and re-inspect the
remaining needed images; prior transport receipts are not a substitute for this
new context. Existing selections, provider jobs and quality allowances survive.
Do not repeat completed generation jobs. Another refusal in that candidate's
recovery stops automatic recovery and requires human handling; do not reset it.
