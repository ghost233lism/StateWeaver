"""Opt-in predecessor conditioning, independent from story state and review continuity."""

from __future__ import annotations

from typing import Any

from .contracts import Clip, ReferencePlan, StudioProject
from .memory import require
from .store import StudioError


def decision(
    project: StudioProject, clip: Clip, choice: bool | None = None
) -> dict[str, Any] | None:
    """Return no policy at all for legacy requests; never rewrite saved recipes."""

    require(choice is None or type(choice) is bool, "接续选择必须为布尔值")
    editing = bool(clip.repair_plan and clip.repair_plan.mode == "edit")
    # Explicit edit-source video is not a predecessor continuation reference.
    if editing:
        require(choice is None, "编辑源沿用原编辑规则，不使用普通生成的前段接续开关")
        return None
    source = "director-paired-v2"
    available = clip.order > 0
    latent = project.config.latent_continuation_mode
    use = available and latent != "off"
    if choice is not None:
        require(not choice or available, "首条 Clip 没有同片场前段可参考")
        require(not choice or latent != "off", "latent 总开关已关闭，不能只开启尾视频参考")
        use = choice
    return {
        "policy": source,
        "enabled": use,
        "tail_video": use and not editing,
        "latent": use,
        "reason": "paired_on" if use else "paired_off",
    }


def plan_decision(project: StudioProject, clip: Clip, plan: ReferencePlan) -> dict[str, Any] | None:
    resolved = decision(project, clip, plan.use_previous_clip_reference)
    if plan.continuation_decision:
        require(
            resolved == plan.continuation_decision, "接续策略或原分镜映射已变化，需重新保存参考清单"
        )
    return resolved


def needs_tail(project: StudioProject, clip: Clip, plan: ReferencePlan | None = None) -> bool:
    if clip.repair_plan and clip.repair_plan.mode == "edit":
        return False
    resolved = decision(project, clip, plan.use_previous_clip_reference if plan else None)
    return clip.order > 0 and (resolved is None or resolved["tail_video"])


def task_packet(project: StudioProject, clip: Clip) -> dict[str, Any] | None:

    from .policy import project_video_http

    video = project_video_http(project)
    # Reference-context fingerprints must not depend on a draft made from that
    # context, otherwise choosing paired-off invalidates the draft immediately.
    # The selected decision belongs to the exact ReferencePlan/submitted recipe.
    try:
        resolved = decision(project, clip)
    except StudioError as error:
        resolved = {"blocked": str(error)}
    return {
        "decision": resolved,
        "decision_scope": "fixed_policy_or_default; actual choice is pinned in reference_plan",
        "director_choice_enabled": video is None,
        "choice_field": "save_reference_plan.use_previous_clip_reference",
        "scope": "predecessor_tail_and_latent_only",
        "preserved": ["identity_images", "actual_end_state", "audio", "seam_review"],
    }
