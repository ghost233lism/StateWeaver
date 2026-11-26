"""Scoped reuse of inspected, nonquarantined regions without accepting a rejected image."""

from __future__ import annotations

import math
from typing import Any

from .contracts import Actor, ReferenceRegionApproval, StudioProject
from .image_input_recovery import inspected_evidence, quarantine
from .memory import require
from .store import StudioStore


def approve(
    project: StudioProject, actor: Actor, data: dict[str, Any], store: StudioStore
) -> dict[str, Any]:
    require((actor.role == "executive"), "只有新版总导演可批准局部素材")
    revision = project.references.get(str(data.get("revision_id")))
    clip = project.clips.get(str(data.get("clip_id")))
    require(
        revision and revision.media_id and revision.status == "rejected", "局部复用用于已拒绝图片"
    )
    require(clip and not clip.archived_at, "必须指定当前活动 Clip 的适用范围")
    assert revision is not None and revision.media_id and clip is not None
    media = project.media[revision.media_id]
    require(not quarantine(project, media.id), "隔离图片不能裁剪、复用或重新查看")
    require(
        media.id in inspected_evidence(project, actor, [media.id], store),
        "总导演或其看图委托需实际查看原图，再批准明确的局部内容",
    )
    approval = ReferenceRegionApproval(
        actor_id=actor.id,
        clip_id=clip.id,
        media_id=media.id,
        sha256=media.sha256,
        crop=data["crop"],
        purpose=data["purpose"],
        reason=data["reason"],
    )
    left, top, right, bottom = approval.crop
    require(
        all(math.isfinite(v) and 0 <= v <= 1 for v in approval.crop)
        and left < right
        and top < bottom,
        "局部批准需要有效的归一化裁剪区域",
    )
    revision.region_approvals.append(approval)
    return {
        "revision_id": revision.id,
        "region_approval_id": approval.id,
        "clip_id": clip.id,
        "crop": list(approval.crop),
        "summary": approval.reason,
        "next_step": "查看原图后按批准区域 prepare_reference，再查看实际裁剪结果；整图仍拒绝",
    }


def approved_region(
    project: StudioProject, revision_id: str, approval_id: str, clip_id: str | None
) -> ReferenceRegionApproval:
    revision = project.references[revision_id]
    approval = next((x for x in revision.region_approvals if x.id == approval_id), None)
    require(approval and approval.clip_id == clip_id, "局部批准不属于当前 Clip")
    assert approval is not None
    media = project.media.get(str(revision.media_id))
    require(
        media and media.id == approval.media_id and media.sha256 == approval.sha256,
        "局部批准的原图已变化，请重新核对",
    )
    require(not quarantine(project, approval.media_id), "隔离图片不能裁剪或复用")
    return approval
