---
name: clip-continuation-choice-v1
description: Choose paired predecessor video and latent conditioning when the project explicitly enables it, while preserving default continuation within each Set.
---

This optional policy overrides older instructions that unconditionally require the
predecessor tail video. It controls only predecessor video/latent conditioning,
not the confirmed story, asset state, identity images, audio or review coverage.

Later Clips in the same Set default to referencing the selected predecessor,
regardless of source shot boundaries. A new Set starts without automatically
inheriting another Set's predecessor. Keep truly continuous action in a continuous
Set; this does not authorize reading another Set's private candidates.

In free planning with `config.director_continuation_choice=true`, the Set director
chooses `use_previous_clip_reference=true|false` in `save_reference_plan`.
Use true for continuous motion/camera; false for a deliberate new camera shot when
history would constrain the old composition. Without an explicit choice, the safe
default is paired-on when a predecessor exists and the latent master switch is on.
Never request video-only or latent-only under this policy.

Read the exact resulting plan, inspect applicable images and finalize a six-section
prompt matching its real input order. With the pair off, do not mention an absent
Video 1 or claim that Picture 1 is a forced opening composition: it is still the
true prior story state, not a camera lock. Explain the intended new composition,
motion state and completed actions. With the pair on, describe the true continuation.
An optional choice in `generate_clip` must match the finalized plan; changing it
requires saving and checking a new plan, not dropping media from a submitted job.

The selected predecessor and its actual end state are still required. Review still
checks the real seam, even when conditioning is off. Explicit video-edit repairs
retain their separate complete edit-source contract; this ordinary-generation
switch must not discard the video being edited or silently change repair mode.
If no >=2s real video history exists, the pair cannot be enabled; do not fabricate
history or silently substitute latent-only conditioning. Report the limitation.
