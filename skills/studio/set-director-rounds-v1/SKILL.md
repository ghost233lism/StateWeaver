---
name: set-director-rounds-v1
description: Clip-round planning and stable lookahead over durable business memory for explicitly enabled short planning sessions.
metadata:
  version: 1.0.0
---

This package refines efficient-v1 only when planning_context_policy=clip-round-v1
and this actor has entered that organization. It never changes quality or media rules.

## Durable short planning

The supervisor owns session switches. One actor still has one active execution lease.
Initial Set decomposition runs once. Thereafter each Clip and logical initial/repair
round has its own short planning context; a reviewed rejection opens the next round.
Registration, prompt/mode/duration edits before submission, material waits, technical
retries, backend reconnects, pause/resume and effort changes do NOT create new rounds
or extra repair allowances. Never replan the whole Set merely to get a fresh context.

Start with get_project's planning_assignment, then read all pages of the named task
packet (get_clip_work_packet for a Clip). Checkpoints contain hashed business pointers,
not authoritative chat summaries. Reload the current Clip plan, reference versions,
draft, quality_progress/pending reservation, review findings and predecessor facts.
Read the persisted actor goal at its detail scope when needed for operator instructions.
Current records override old checkpoint values. Changed readiness, reference versions,
authorization, invalidations and unknowns must not be ignored.

Full is not a routine refresh: a complete valid same-session baseline yields a delta,
even if mode=full. A genuine full-text verification may use mode=full with a concrete
full_verification_reason naming the object and discrepancy to check. Follow pagination;
only the final current read receipt authorizes writes. After native compaction or a
replacement context, the same logical planning session remains, its baseline is invalid,
and required records/images must be reread. Do not request dynamic session replacement.

Local ProductionSet.memory is durable. Keep concise scoped planned continuity and
selected clip_actual observations with provenance/revisions/unknowns. Rejected candidate
findings are repair evidence, NEVER authoritative observed story facts. Cross-Set
observations and coordination remain executive-owned. Selection records actual outcomes.

## Stable lookahead

The next Clip has its own draft context. Current review or repair takes priority;
switches are serial, never concurrent turns of this actor. Save stable prompt/action,
image/audio bindings and unresolved stable requirements with save_clip_preparation.
Do not bind planned predecessor endpoints or submit/repair/finalize video from the
draft context. Only predecessor selection supplies the real endpoint and tail73.
Reuse durable drafts; update precise invalidated material/state dependencies, preserving
editable/disabled preparation. Waiting is not permission to manufacture story facts.

## Review separation

Every candidate still has exactly one formal review context. Complete coverage, focused
supplements, evidence receipts and the final verdict stay there. Planning consumes its
findings/evidence index and authors the actual repair prompt; it does not routinely reload
all failed footage. Do not weaken review, continuity, unknown-submit or quota guards.
