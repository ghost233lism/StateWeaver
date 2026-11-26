---
name: video-self-review-v1
description: Prepare local dense visual evidence and record evidence-backed director findings.
---

Use `prepare_review` after video collection. Sampling covers the full Clip at configured density (default 1 fps), first/last second at configured density (default 4 fps), the actual final frame and the accepted predecessor tail/seam. View every required page and exact endpoint via `read_media`; timestamps are relative to the labelled source, not an invented continuous timeline. Additional focus_intervals at 2–8 fps help resolve uncertainty without generating another video.

Describe what is visible before comparing it with intended story/identity/action/space/camera. Check the seam first, then the current Clip's action progression and final state. A correct still does not prove the whole motion is smooth. Do not claim to have listened to audio from stills. Evidence preparation supplies no independent verdict.

Review with a conclusion and inspected media IDs. Findings specify current-video seconds, category, minor|repair severity, visible symptom and frame evidence. Minor nondisruptive defects may pass. Repair-severity violations require a targeted preserve/change plan and another allowed attempt. Only director-confirmed actual end states become continuation facts. Record uncertainties and keep previous analyses, raw images, limitations and rejected results.

```json
{
  "format": "director-self-review-v1",
  "evidence_instruction": "Inspect every required evidence page, the actual final frame and the previous-tail seam. Record the conclusion, timestamped findings, visible end state and inspection limitations."
}
```
