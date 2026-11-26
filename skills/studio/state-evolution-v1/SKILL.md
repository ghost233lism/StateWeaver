---
name: state-evolution-v1
description: Record and read the executive-confirmed primary ancestry of an asset's story states, separately from image revisions and reference inputs.
---

The asset detail tree represents reference states: dry clothes → wet clothes
→ wet clothes and injury, when those appearances are actually needed as generation
references. A purely narrative process inside a Clip belongs in scoped story memory,
the Clip description and its prompt, not a new asset node. Do not generate a picture
or adopt a decorative frame merely to fill such a node.

For Set projects, every new state needs `reference_purpose` with `purpose`, at least
one existing `set_ids` or active `clip_ids` target, concrete `required_materials`,
and `prepare_when` (`now`, `before_generation`, or `after_source_selected`). Create
assets and publish the Set directory (goals, entry/exit descriptions and assigned
asset IDs) first; handoffs can add exact state IDs after registration. Set-level
targets need neither Clip IDs nor finished images. A genuinely needed state may wait for its source footage: retain
its reference purpose and an `observed` preparation with source Set/event and required
attributes. A preparation source alone is not a target reference purpose. Existing
legacy Set creation rules and historical records remain compatible.

Each reference state has one primary parent from the same asset;
several children may share a parent. Image upload sources, alternate viewpoints,
reference revisions and chronological order alone do not establish this ancestry.

The executive records `evolution` when creating a state, or reads the complete
`planning` context for its `state_id` and calls `record_state_evolution` with the
current `expected_evolution_revision` (zero if unrecorded). Record:

- `root`: a supported initial/independent state; no parent.
- `derived`: one `parent_state_id` supported by the story and state descriptions.
- `unresolved`: insufficient or contradictory evidence; no guessed parent.

Include a concise `reason`. Confirmed roots and derivations require exact quoted
`evidence` from a same-asset `state`, a `story_event` (`kind="event"`), global
story `memory`, or the confirmed `screenplay`. Evidence uses `{kind,id,quote}`;
`state/self` is available only for the state being created. Explain uncertainty
and prefer `unresolved` when different primary parents remain plausible.
An unchanged identity can have multiple independent states; do not invent an
off-screen transformation just to make one connected tree.

Set directors may read applicable ancestry and request executive corrections.
They cannot register parents or read another Set's private work. Additional
reference sources remain separately traceable in image details.

Ancestry is descriptive metadata. Recording or correcting it does not change
event applicability, handoff requirements, selected references, Clip plans or
production allowances. Use their existing explicit tools for those changes.
Historical review must preserve original IDs, media, submitted requests,
selections and actor status, and must not restart production.
The browser can omit historical process-only nodes and draw a dashed indirect
connection along their actual ancestry. This display never changes the recorded
parent or turns an indirect relation into a directly confirmed derivation.
