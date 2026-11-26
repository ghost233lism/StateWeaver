---
name: set-handoffs-v1
description: Coordinate event-level observed asset inheritance and state-image timing for observed-v1 projects, while preserving isolated directors and existing footage.
---

Apply only when runtime policy says `handoff_policy=observed-v1`. These explicit
handoff rules supplement the role Skill; missing fields never mean independence.

Film `order`, story event positions, and generation dependencies are different.
Never serialize every Set by film order. Never assume independent generation just
because the directory contains `depends_on: []`.

For each Set, the executive classifies every assigned asset in `asset_handoffs`:
- `independent`: first appearance or independently designed state, with a concrete reason.
- `shared`: same fixed identity/design as `source_set_id`, without inheriting an
  unpredictable actual result. Explain which invariants are sufficient.
- `observed`: inherit actual damage, wetness, possession, layout, landing/arrival
  position or other visual results. Name `source_set_id`, `source_event_id`, applicable
  `state_ids`, optional `target_event_ids`, `required_attributes`, and the story reason.

Use `prepare_sets` for initial declarations and `configure_set_dependencies` with
the current `expected_dependency_revision` for later revisions. Read
`get_set_dependencies` and planning/global context first. `depends_on` is reserved
for a genuine whole-Set completion prerequisite; an observed handoff waits only
for the last planned Clip covering its named source event. Never wait for the whole
proposal, all state images, or all directors to finish planning.

The executive still publishes story goals and entry/exit states first. Set
directors may immediately plan Clips and stable prompt/material drafts. They read
only their own work and applicable executive-published memory/materials. A source
Set/event ID is a dependency identifier, not permission to read that director's
private plans, candidates, prompts or conversation. Ask the executive to coordinate.
Readiness and `quality_progress` list generation blockers and responsible owner.

For each non-base asset state, explicitly configure `preparation`:
- `independent`: stable base identity or independently specified state; images may
  be generated before filming.
- `observed`: derived state must wait for the selected source event. Prepare its
  text, views and prompt beforehand; only `generate_reference` waits for evidence.
- `transition_target`: a design image used by the Clip that PRODUCES the change.
  Name that producing Set/event. It may be generated early as target guidance, but
  is never proof that the change already happened. Do not make a transition wait
  for its own final result. Later start-state use requires real prior evidence.

States may be declared before the Set directory without a preparation policy.
After Set/event IDs exist, configure those states before requesting their images;
do not invent a source ID or wait for the image before creating the directory.

After source selection, the executive prepares and inspects the precise actual
image with `prepare_media`; approves only observed attributes through
`approve_asset_observation`, scoped to the consuming Set/events and state IDs.
Unselected candidates, prior selection versions, planned images, unseen attributes,
quarantined evidence and unrelated timestamps cannot certify actual inheritance.
When footage already depicts the needed state/view, `adopt_observed_reference`
reuses its exact approved image. Otherwise generate only the needed supplementary
views, explicitly including `source_observation_ids`. Do not regenerate a complete
asset catalog by default.

Inspect the actual source and each proposed reference (delegated inspected evidence
is permitted where the tool supports it), then `confirm_set_handoff` with the exact
observation IDs, reference IDs and current dependency revision. Existing design
images may remain if visually verified to match; explain the compatibility. The
confirmation records source selection, image hashes, timestamps and versions.
The director uses those references and published facts; editing them requires a
new confirmation. A source selection, state boundary, actual end state or reference
version change invalidates only relevant unsubmitted drafts. Refresh affected
fields; submitted recipes, selected footage and repair counters stay intact.

During an explicit opt-in of an in-progress project, preserve every existing Clip,
candidate, selection, review and repair allowance. Inspect already produced
downstream footage against the newly available upstream facts. Record real
differences and coordinate a scoped decision; never blanket-rerender or relabel a
historical submission as having used a source that did not yet exist.
If a selected source still has an unconfirmed upstream handoff, its footage stays
selected while further inheritance waits. Resolve that source's handoff first;
do not bypass the chain merely because a downstream video already exists.

Same-Set continuity remains unchanged: selected predecessor endpoint Picture 1,
tail 73 real frames Video 1 and current endpoint latent policy; explicit repair
editing keeps its existing exception. Cross-Set handoffs do not automatically copy
another Set's tail video or force a continuous camera cut across a time/place jump.
