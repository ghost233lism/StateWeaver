---
name: set-duration-budget-v1
description: Set playback-duration budgets and automatic adjustment requests for new projects.
---

Load only for planning directors with playback_policy=set-budget-v1; historical projects keep their policy. Drafting involves only user and writer; confirmed screenplay and facts remain frozen. story_time/event anchors describe story time, separately from playback duration and production ETA.

The executive assigns each Set duration_budget {target_seconds, minimum_seconds, maximum_seconds, basis} from the confirmed screenplay's natural duration_estimate; bounds enclose the target. Stay near the overall estimate without equal partitioning or stretching brief actions. The film has no fixed tolerance, hard maximum or mandatory sum-of-Set-maxima bound. Explain story-based changes. Use set_duration_budget with expected_budget_revision, budget and reason after a complete planning/global read. Change production organization only, not screenplay, selections or original requests.

Within the Set's interval, its director chooses Clip count, natural action scope and duration. Save complete plans using plan_clips complete=true; incomplete plans may use complete=false but cannot finish the Set. Incomplete plans block only when the known total already exceeds the maximum; a temporarily low total is not a defect. If a complete plan is below the minimum, request a lower interval instead of padding.

Out-of-range plans are saved normally and the system registers a current budget-adjustment request and notifies the executive; do not duplicate it with a global proposal. Waiting affects only this Set's new video submissions. Planning, material preparation, self-review, collecting submitted jobs and other Sets continue. Revise only unsubmitted suffixes or wait for a budget change. Preserve selected prefixes, submissions, continuation and quality allowances. Planning is allowed without an assigned budget, but video submission is not. The executive must handle budget requests rather than wait for a blocked Set to finish.

get_playback_duration returns the shared ledger; Set directors see their Set and the executive sees all. Count each current Clip once, using original job duration after submission/selection and current planned duration otherwise. Failed versions, ordinary repair history, archived plans, predecessor tails and latent history add no playback duration. Actual video/film duration is separate; ordinary generation tail differences do not trigger budget approval. Major duration or audiovisual anomalies still require existing integrity checks and quality repair; budgets do not bypass review.
