---
name: h3-ref2va-set-v2
description: Write official six-section H3 Ref2VA prompts with explicit real-frame and three-second-tail continuation for continuous Sets, allowing planned camera cuts inside Clips.
---

## Six sections and actual media

This new-project package follows MiniMax-AI/MiniMax-H3's full-reference guide at
commit d21241f0a4b3acbb34c97dae47fa417b7065e438. The fixed tail, role
restrictions and continuity checklist are StateWeaver production policy.

Provide exactly these six canonical headers, in order, with nonempty bodies, no
JSON wrapper, Markdown fences, preamble or seventh section:
`subject_definitions`, `summary`, `retention_analysis`, `detailed_description`,
`overall_soundscape`, `non_diegetic_music`.

Write descriptive prose in the current project production language. Keep the required section headers, task types, markers and slot labels in their specified form. Preserve spoken language in `<d>[Chinese] ...</d>`,
`<d>[English] ...</d>` etc.; quote visible text in its original language. Subtitles
are visible text, not additional dialogue. Keep requested style/language and sound.

Number `<Picture N>`, `<Video N>`, `<Audio N>` independently in exact saved input
order. Define reusable people/props/places as `<Subject N>`; group a person's
identity/body images under one Subject and say which Picture supplies which feature.
A standalone Picture definition means a concrete composition/first/key/end-frame
anchor. An identity sheet is not automatically the first frame. Every Subject used
must be defined; every video/audio needs its own definition; every supplied media
label must be cited, with no missing, duplicated or nonexistent index.

Every separately defined Subject/Picture/Video/Audio has one matching line in
retention_analysis. Visual relationship markers are `fully_preserved -`,
`partially_preserved -`, `attribute_transfer -`, `weak_reference -`; audio uses
`reference -` or `weak_reference -` for timbre/ambience, not copied words. The
same label means the same thing in all six sections.

summary starts with the bracketed actual task types. A first Clip normally uses
`[reference generation]`, optionally keyframe completion for a supplied composition
anchor. A continuing Clip uses
`[video continuation + keyframe completion + reference generation]`, adding
` + audio reference` inside the brackets when audio is supplied. Explicit full-current-candidate repairs use [video editing + reference generation], adding keyframe completion when the predecessor endpoint is supplied. Never label the edit source as a preceding tail. Reference conditioning is not FL2V's explicit first-frame
input. The local endpoint-latent extension below adds a generated-latent constraint;
neither path guarantees pixel-identical decoded output.

## Local endpoint-latent operation

For a new same-Set continuation the adapter defaults to `endpoint-anchor-v1` when
the project enables it. It re-encodes the accepted predecessor's real Picture 1 at
the output canvas, pins the first generated latent frame at H3's clean timestep,
and restores that frame after each sampling update. This is a local FAST/SLOW
extension, not an official Ref2VA prompt keyword. Write the same six sections and
retain Picture 1 and all exact image/audio bindings. Regeneration retains the final 73 real frames as Video 1. Editing instead uses the COMPLETE current candidate as Video 1 and retains the independently bound predecessor endpoint latent.
Latent anchoring constrains the opening state; the tail and action/camera clauses
still supply movement. Inspect the seam and motion normally.

`generate_clip` optionally accepts `latent_continuation_mode="off"` for a deliberate
reference-only attempt, such as a repair of an observed anchoring regression. Record
the reason in the repair plan; do not turn it off to bypass an unavailable provider.
Omitting this argument follows project policy. The first Clip is always off. Submitted
jobs pin the actual mode and source; never retrofit them or use a rejected candidate
as the predecessor endpoint anchor. An explicit edit source is separate from that anchor. A waiting anchored job needs a service advertising support, not another
generation request. The system preserves requested mode and actual application receipt.

## Continuation is the next instant

From Clip 2 in a Set, **Picture 1 is the accepted preceding Clip's actual final
frame**. **Video 1 is that same accepted Clip's final 73 real frames at 24 fps (about 3.042 seconds)**. They are
complementary: the frame provides the opening composition/state; the video provides
the incoming motion trend. Output begins at the next instant after the tail ends.
Do not replay the supplied tail, interpolate from the start of the tail, stage a
new introduction or describe the same completed action as if it has not happened.

Use the actual inspected final frame and tail, not the originally planned endpoint.
Describe concrete visible boundary facts before desired action: who is where on
screen, body orientation, which foot/hand is planted or moving, object contact and
ownership, gaze, direction/speed trend, camera height/distance/angle and pan/dolly
direction. Do not invent numerical velocities, camera settings or hidden geometry.
Keep image identity references subordinate to this real boundary composition.

Write each new Clip's detailed_description specifically for its next action. Keep
identity, wardrobe, environment/lighting, spatial axis, screen direction and camera
trend stable. Change the action clause to advance the story, not repeat the prior
whole prompt. State already completed actions in retention_analysis as facts and
the new action positively in detailed_description. Use a small explicit no-replay
constraint only when it resolves a real ambiguity; avoid long generic negative lists.

For example: "At the opening her right palm is already flat on the door, left foot
planted near the threshold, body leaning forward. The door is already half-open.
Continue its outward swing as she shifts her weight through; she does not reach for
or push the door a second time." This is more grounded than "same character walks
to the door and opens it" after a Clip which already did that. For a fighting beat,
carry the ongoing parry/contact into its consequence; do not reset both fighters to
guard before every attack. Preserve the established left/right relationship.

Start with a style sentence before `[Shot 1]`. At each opening use the incoming
camera motion already in progress, not "the camera starts moving". Avoid a freeze,
fade, new establishing frame, reset zoom or posed final beat at every Clip boundary.
Keep the first continuation beat on the same viewpoint; any intended internal cut
comes after that beat, with an explicit time and reason. Subsequent camera shots
use consecutive `[Shot 2]`, `[Shot 3]` in the same place/time and preserve the action
axis and subject position. Camera cuts are allowed inside a Clip; time/location
jumps need another Set. End with a legible ongoing action when another Clip follows.

Make action phases fit the exact requested duration; keep crucial contacts,
handoffs and dialogue inside one Clip when practical. Avoid overloading a boundary
with a turn, occlusion, new attacker and camera cut simultaneously. Official guidance
for an English detailed_description suggests roughly 350–500 words. Aim for
comparable visual, temporal, camera and sound detail in other languages. Keep
the description consistent with the story and the requested duration.

## Actual saved reference sheet

Save exact materials first, inspect the resulting images/boundary/tail evidence,
then finalize the prompt against that version. A continuation's bindings start at
Picture 2 because Picture 1 is automatic. No other video input is allowed. Use
remaining slots for necessary selected state/identity pictures and audio. Limits:
9 total images, 3 audio, 12 mixed files; each audio 2–15 seconds, combined audio
<=15 seconds. Newly prepared tails contain exactly 73 real frames at 24 fps (73/24 seconds), with one extra frame from before the old three-second window. Never duplicate the endpoint to fill this window. Preserve historical pinned 72-frame recipes. Never silently discard references.

Choose useful single views, inspect boards and prepare a conflict-free panel when
needed. The result must have image width/height in [1/4,4]. A board is documentation,
not an instruction to draw repeated figures/panels/labels in the video. State this
role concretely. Never crop or swap the predecessor's actual final-frame anchor.
Remove conflicting costumes/props/states via reference preparation or executive
revision; asking the model to ignore a contradiction is not a state fix.

For selected voices define Audio N as the correct speaker's timbre/delivery, with
stable speaker ID, e.g. `<Subject 1> (S1)`. Generate current dialogue anew. A silent
tail contributes no sound reference automatically. overall_soundscape covers actual
ambience/movement/contact; non_diegetic_music is `N/A` when none is intended.

For a failed candidate inspect the defect, retain correct parts in the preserve
list, and change the relevant action/identity/camera clause. The failed video is
**never** an edit source. Regenerate with the required accepted predecessor inputs
and selected pictures/audio, preserving all original attempts and verdicts.

## Short format example (expand action detail to fit the actual request)

subject_definitions:
<Subject 1> is the woman in <Picture 2>, which supplies her identity and blue work jacket.
<Picture 1> is the exact final frame of the accepted previous Clip and the opening-frame anchor of [Shot 1].
<Video 1> is the final 73 real frames (about 3.042 seconds) of that accepted Clip, showing the incoming door swing, her forward weight shift and slow camera track.

summary:
[video continuation + keyframe completion + reference generation] Continue directly beyond the end of <Video 1> from <Picture 1>; <Subject 1> crosses the threshold through the already moving door.

retention_analysis:
<Subject 1>: fully_preserved - identity, blue jacket and continuous left-to-right movement; <Picture 2> supplies identity, not opening pose.
<Picture 1>: fully_preserved - opening composition, palm contact, planted foot, door angle and lighting. The door has already been pushed; do not repeat that completed action.
<Video 1>: partially_preserved - continue its final motion and camera trend beyond the endpoint, without replaying the supplied interval.

detailed_description:
Naturalistic documentary imagery with the existing soft daylight and steady eye-level tracking perspective.
[Shot 1] Begin at <Picture 1>, immediately after <Video 1> ends. <Subject 1>'s right palm is already in contact with the half-open door, her left foot already planted at the threshold. Continue the outward door swing and her existing forward weight shift at the established pace. The camera continues its slow parallel track with her, retaining the same subject scale and side of the action axis. As the doorway clears, she steps through and lets the moving door pass beneath her palm. The following action is the crossing itself, not a second reach or push. Preserve the incoming shot until the crossing is readable. Continue her stride toward the room without a posed stop or concluding fade.

overall_soundscape:
Continuous room ambience, a soft hinge creak and footsteps. No copied source dialogue.

non_diegetic_music:
N/A

## Explicit editing and auxiliary references

When repair_plan.mode=edit, define <Video 1> as the complete current reviewed candidate. State the exact defects to correct and correct features/timeline to preserve. Do not request continuation after the source video. Remove the predecessor tail video; its actual endpoint Picture 1 remains the opening authority. Do not import the source's wrong state as a verified fact. If actual source duration exceeds 15 seconds, use regeneration without truncating the source. Auxiliary action/layout/target-frame pictures must state their assigned role; a desired ending picture is a soft Ref2VA target, not an observed result or ending latent lock.

## Runtime recipe

```json
{
  "format": "h3-ref2va-six-section-set-v1",
  "continuation_prefix": "The opening frame is <Picture 1>, the actual final frame of the preceding accepted Clip. <Video 1> is that same Clip's final 73 real frames (about 3.042 seconds) and supplies only the incoming motion context. Begin the next instant AFTER its endpoint: preserve the visible pose, contacts, object state, screen positions, motion direction and speed trend, and continue the existing camera trajectory. Do not replay the supplied tail or restart an already completed action. Identity references do not replace this opening composition; perform any planned internal camera cut only after the continuous opening beat.",
  "editing_continuation_prefix": "<Picture 1> is the actual final frame of the accepted preceding clip and defines this clip's opening. <Video 1> is the complete CURRENT candidate to edit, not preceding history to append. Correct only the stated defects and preserve the intended timeline. Maintain the opening pose, contacts, screen positions, motion direction, speed trend and camera trajectory from <Picture 1>. Generate the corrected current clip from its opening without replaying earlier action."
}
```
