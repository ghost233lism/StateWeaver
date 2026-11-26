---
name: set-executive-quality-packets-v1
description: Complete current quality verdict and indexed historical defects for executive decisions without routine bulk candidate rereads.
metadata:
  version: 1.0.0
---

For an adaptive-v2 Clip decision, read its review/Clip task context (or the approved
candidate-review/repair-diagnosis delegate's decision packet) and follow every page.
The packet contains unified quality_progress, the current COMPLETE review report,
evidence IDs, coverage limitations, necessary endpoint facts and prior defects.
Current report is not necessarily the newest generated candidate: check
latest_candidate_id and the ledger's pending reviews/submissions before deciding.

Prior versions are indexed defect summaries, not all old prompts/analysis/images.
Use candidate:<id> detail/history or a scoped visual delegate for a concrete question.
Do not routinely load every failed candidate's entire submission and frame set.
Delegates' actual evidence/adoption and limitations remain traceable; final selection,
compromise, cross-Set facts and extra allowances remain your responsibility.
Preserve original rejected verdicts, prior_verdicts, fallback IDs and selection kinds.
A confirmed diagnostic endpoint on rejected footage is NOT an authoritative selected
story fact. Unseen details or missing coverage cannot be certified by a compact report.

Ordinary refreshes use a complete valid same-session delta baseline, even if mode=full.
Only a specific full_verification_reason with mode=full requests a full-text verification.
New/compacted/replaced contexts reread required records; do not reuse foreign baselines.
