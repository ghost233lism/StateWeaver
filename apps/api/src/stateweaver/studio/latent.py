"""Bind new continuation jobs to the selected predecessor before submission."""

from __future__ import annotations

from typing import Any

from stateweaver.studio_media.latent import CONTINUATION_FPS, ENDPOINT_ANCHOR, EndpointAnchor

from .contracts import Clip, ReferencePlan, StudioProject
from .memory import require


def bind_continuation(
    project: StudioProject, clip: Clip, plan: ReferencePlan, override: str | None
) -> dict[str, Any]:
    require(override in (None, "off", ENDPOINT_ANCHOR), "未知 latent 接续模式")
    eligible = clip.order > 0
    from .continuation_controls import plan_decision

    control = plan_decision(project, clip, plan)
    if control:
        required = ENDPOINT_ANCHOR if control["latent"] else "off"
        require(
            override is None or override == required,
            "视频与 latent 必须按定稿接续选择成对开启或关闭",
        )
        override = required
    mode = override or (project.config.latent_continuation_mode if eligible else "off")
    if mode == "off":
        return {"mode": "off"}
    require(eligible, "latent 接续只用于同片场后续 Clip，首段或跨片场不能绑定尾帧")
    previous_clip = project.clips[project.sets[clip.set_id].clip_ids[clip.order - 1]]
    require(
        plan.previous_candidate_id
        and plan.previous_candidate_id == previous_clip.accepted_candidate_id
        and plan.previous_end_state,
        "latent 接续必须绑定前一 Clip 的已选候选及真实末态",
    )
    assert plan.previous_end_state is not None and plan.previous_candidate_id is not None
    endpoint = project.media[plan.previous_end_state.frame_media_id]
    editing = clip.repair_plan and clip.repair_plan.mode == "edit"
    if editing:
        require(
            len(plan.video_inputs) == 1 and plan.video_inputs[0].get("role") == "edit_source",
            "编辑模式需完整当前候选；前段真实末帧独立锚定",
        )
        require(endpoint.mime_type.startswith("image/"), "latent 端点必须是真实末帧图片")
        anchor = EndpointAnchor(plan.previous_candidate_id, endpoint.sha256, "", endpoint_only=True)
        return {
            **anchor.payload(),
            "source_clip_id": previous_clip.id,
            "endpoint_media_id": endpoint.id,
        }
    frames = plan.video_inputs[0].get("frame_count", 72) if plan.video_inputs else 72
    require(
        plan.video_inputs
        and plan.video_inputs[0].get("role") == "continuation"
        and plan.video_inputs[0].get("source_candidate_id") == plan.previous_candidate_id
        and type(frames) is int
        and frames in (72, 73)
        and abs(float(plan.video_inputs[0].get("duration", 0)) - frames / CONTINUATION_FPS) < 0.001,
        "latent 接续需要同一已选前段的真实尾 73 帧作为 Video 1；历史 72 帧记录保留",
    )
    tail = project.media[str(plan.video_inputs[0]["media_id"])]
    require(endpoint.mime_type.startswith("image/"), "latent 端点必须是真实末帧图片")
    require(tail.sha256 == plan.video_inputs[0].get("sha256"), "接续尾视频已偏离定稿记录")
    anchor = EndpointAnchor(plan.previous_candidate_id, endpoint.sha256, tail.sha256, frames)
    return {
        **anchor.payload(),
        "source_clip_id": previous_clip.id,
        "endpoint_media_id": endpoint.id,
        "tail_media_id": tail.id,
    }
