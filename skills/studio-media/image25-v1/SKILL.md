---
name: image25-v1
description: Build coherent Image 2.5 reference views and same-state revisions.
---

## Scoped memory and image provenance

Before a new image generation/selection, read all required `references` context pages for its state and pass the returned `context_token`; review/quality intervention uses `review` context. Keep story event anchors, state transitions and same-state revisions distinct. New states may use older-state identity images as explicit edit sources; this does not permit H3 to borrow an unrelated visual state. Follow the project runtime policy for production-time asset additions; saved projects retain their original catalog rules.

An adopted video screenshot must have an executive `approve_asset_observation` entry that names exact target `state_ids`, correct local `attributes`, `purpose`, source candidate, screenshot time/media and reason. `source_observation_ids` may use only the approved state and attributes; other contents of a rejected source remain unapproved. After an image selection changes, affected unsubmitted H3 reference drafts need director refinalization, while old jobs/video bindings remain untouched. Put defect/preserve/change details in revision/task history, and only concise scoped story changes in effective memory.


Use the image service and model registered for this project. Create an initial reference from explicit asset identity and chronological state. Derive subsequent views or corrections using the actual reference image through the edit tool, preserving identity, materials, markings, costume and state. Independent text-only prompts for each required view can drift; always pin the selected reference revision.

People require a face close-up, a full-body image, an aligned front/side/back three-view sheet, and a 3×3 expression sheet. Full-body means head to feet, not a scenic wide shot. Objects require a three-view sheet. The executive director chooses useful views for scenes and other catalogued assets during preparation. Prepare needed identity views before their first use; the runtime gate determines when production can open. Plan derived states in advance, but render only their imminent required views and prefetch upcoming needs; do not generate every view of every future wet/injured/damaged state before shooting.

A changed story-time condition is a separate state. A correction to an image of that same condition is a new revision with a parent. Do not overwrite prior states or revisions. Only adaptive-v2 permits executive-published scoped new assets and auxiliary pictures during production. Older projects retain the frozen logical directory. Auxiliary action/layout/target-frame pictures are guidance, not observed story states.

For a composite asset such as a photograph depicting an existing character, use `parent_id` for the photograph's same-state edit target and `source_revision_ids` for exact character, prop and scene references. Cross-asset sources are supported and preserve depicted identities; they are not revision ancestors of the target. Choose the story states actually depicted (a photograph may show another time), inspect the sources and specify what each contributes. Preserve anatomical left/right markings, costume, object geometry and the intended composition. When a text-only correction repeatedly fails on identity, condition the edit on the actual known identity reference. Review the result before selecting it.

Keep each reference legible, consistently lit, and free of accidental extra subjects. A sheet uses the same subject and scale across panels. Describe which visual attributes remain fixed and the exact requested change. Provide the exact reference revisions through the durable edit action. The configured adapter owns the provider request format.

## Defect-focused review and observed state sources

Images are inspected by the executive or, in adaptive-v2, an authorized visual-analysis child, then the executive calls `review_reference`. Rejection records a concrete `repair_plan` with defects, preserve, change and source_revision_id. Use the best usable target as the same-state edit parent, change only the named incorrect attributes/region and recheck the correct parts for regressions. Preserve all candidates and the original failures. Initial generation plus three ordinary corrections is the default; then `resolve_quality` allocates from a finite executive allowance with a named final fallback or explicitly selects an existing displayable compromise. Exhaustion must not lead to indefinite rewriting or a quality-only production block. Record remaining deviations rather than claim a compromised image passed inspection.

A correct local detail from an actual Clip can guide an existing asset's state or revision. The executive first inspects a real `prepare_media(kind="image")` screenshot and approves the precise detail with `approve_asset_observation`. Pass the returned entry ID in `source_observation_ids`. The backend pins source bytes, original candidate, interval and approved scope. A rejected source remains rejected: transfer only the approved wet sleeve, injury location or damaged prop detail, not every surrounding error. Planned state transitions and observed screenshot evidence stay distinct and are located at Clip story time.

## Writing a reference request

Use the durable `generate_reference` action. Its `prompt` describes the visible result; optional `preserve` (a list), `change`, `composition`, and `reference_roles` (revision ID to role description) make important edit instructions explicit. These are creative instructions, not claims that the model perfectly obeys them. Inspect the actual result before selection, using the existing review standards and repair controls.

Identify the requested operation and its constraints:

| Operation | Change | Preserve |
| --- | --- | --- |
| Initial reference | Establish the asset, material/appearance and target story state | Confirmed identities and screenplay facts |
| Derived view | Camera/view, framing or expression needed for this view | Identity, proportions, costume, materials, markings and target state |
| Same-state correction | The named defect or region | Unaffected identity, geometry, pose, expression, costume, camera, layout, lighting and surrounding objects |
| New story state | The explicitly changed injury, costume condition or other story-time fact | Stable identity and unaffected traits; old-state sources are identity references only |
| Composite asset | The named people/objects within the target photograph or scene | The target's format and composition, each depicted asset's exact intended state |

For each request state the purpose/view, visible subject and state, reference responsibilities, exact requested change, invariants, and framing/lighting. Short labelled clauses are sufficient; do not inflate a simple edit. Repeat critical invariants in each new revision because the image endpoint does not inherit the previous conversation. Describe the complete intended image, not just the defective pixel.

The backend supplies the actual numbered image mapping for EVERY edit, including same-asset references and automatic anchors. Use those roles consistently. A face reference supplies identity; a body reference supplies proportions/costume; a target image supplies the layout being edited; a reference board supplies appearances rather than its panel layout. For conflicts, the requested story state governs changeable condition, the chosen identity reference governs stable likeness, and the target governs composition unless the requested edit changes composition. Do not silently mix states. Use anatomical left/right for bodily markings and screen-left/screen-right for image placement; state both only when the visible pose supports that relationship.

All new images and edits use `quality=high`. Full-body views use portrait output, three-view sheets landscape, and close-ups/expression grids square. For custom scene/other views `orientation` may be `portrait`, `landscape` or `square`; choose it for the reference's intended use. The backend owns concrete gateway parameters and records requested settings and decoded output dimensions. Do not add unsupported API parameters or reinterpret the configured model alias as a different model.

## Examples to adapt

- **Initial full-body:** `prompt`: "Production identity reference of the single adult courier in the confirmed brief, dry olive jacket, dark trousers and brown boots; calm standing pose, head and feet fully visible, soft neutral light, simple background." `composition`: "Centered full body with breathing room above the hair and below both shoes." Specify established facial/physical traits from memory rather than inventing them.
- **Derived three-view:** select the exact existing full-body/face references. `change`: "Show front, left profile and back in three distinct columns, equal scale and baseline." `preserve`: identity, body proportions, outfit, footwear, markings and story state. `reference_roles`: identify the face authority and body/costume authority. Use clean gutters, no decorative grid, writing or additional people; all three depictions are the same person.
- **Same-state correction:** use the image being corrected as `parent_id`. `change`: "Correct the specified eyebrow scar position using the chosen identity image." `preserve`: face shape, hair, expression, pose, clothing, framing, background and lighting. Name anatomical side from confirmed memory; do not rewrite several unrelated details at once.
- **New story state:** target the existing asset's new chronological state, with the previous selected face/body in `source_revision_ids`, not as a same-state parent. `change`: "The jacket is now rain-soaked and darker at the shoulders." `preserve`: facial identity, body proportions, jacket design, trousers and boots. Explicitly remove any old-state condition that conflicts with the new state.
- **Photograph containing a character:** use the photograph revision as `parent_id` and the exact depicted-time character references as sources. `change`: "Correct the depicted person's likeness within the existing photograph." `preserve`: photograph border, camera perspective, subject placement, lighting and all other depicted objects. The character sheet contributes identity and costume only; do not insert its multiple panels into the photograph.

For an expression grid, the nine cells depict the same face with stable framing, hair, lighting and story state, with only the requested expression changing. Keep the head complete in each cell and use clean gutters so a needed cell can later be cropped. For scenes, establish usable spatial layout, camera viewpoint and lighting; do not apply a portrait background recipe to a scene.

These templates follow the [OpenAI image prompting Cookbook](https://github.com/openai/openai-cookbook/blob/main/examples/multimodal/image-gen-models-prompting-guide.ipynb), particularly invariant-preserving edits, indexed multi-image roles and character anchors.


## Runtime recipe

The adapter consumes this versioned recipe. Update the manifest with any edit.
Previously queued jobs without a saved recipe use the immutable [v2 runtime archive](references/runtime-v2.json); it is retained only for recovery, not new requests.

```json
{
  "views": {
    "close_up": "Face close-up with head and shoulders, neutral expression, visible identity details.",
    "full_body": "Full body from head to feet, neutral standing pose, all clothing and footwear visible.",
    "three_view": "One reference sheet with aligned front, side and back views at identical scale; the same subject and state in every panel. Clean separation between three panels.",
    "expression_grid": "A 3 by 3 expression reference sheet of the same face: neutral, joyful, sad, angry, afraid, surprised, disgusted, thoughtful, determined. Same identity, hair and story-time condition in all nine panels."
  },
  "quality": "high",
  "output_profiles": {"square": "1024x1024", "portrait": "1024x1536", "landscape": "1536x1024"},
  "view_profiles": {"close_up": "square", "full_body": "portrait", "three_view": "landscape", "expression_grid": "square"},
  "edit_template": "Operation: {operation}\nRequested change: {change}\nPreserve: {preserve}\nComposition: {composition}\nChange only the named attributes within the requested view/state. Keep unaffected appearance, geometry, materials, pose, lighting and surroundings consistent. References supply only their assigned attributes; do not copy a reference board's panel layout into a single-view output.",
  "prompt_template": "Create a production reference for this exact asset and story state: {description}\nRequested view: {instruction}\nPreserve the target asset's identity, materials, colors, distinguishing marks and intended story-time state. Follow the requested correction and presentation. For composite assets, include only the explicitly requested depicted subjects and props, using their assigned references. Use clear lighting and composition; do not invent unrelated subjects or props.",
  "composition_suffix": "This is a multi-reference edit of the named target asset. The ordered reference mapping assigns each image a role. target_asset images show the asset being edited; depicted_asset images supply ONLY the identity, appearance and geometry of the named subjects or objects depicted within the target. Keep the requested target format and composition; do not replace a photograph with a portrait sheet or combine all reference layouts. Apply the explicitly requested depicted states and anatomical left/right details. Other assets' reference states are not state transitions of the target itself.",
  "state_transition_suffix": "References from earlier story states provide stable identity only. Apply the explicitly requested target state and its changes; do not preserve outdated injuries, costume conditions, or other superseded state details."
}
```
