"""Pinned, actor-owned preparation of the exact images a new H3 job receives."""

from __future__ import annotations

from typing import Any

from stateweaver.studio_media.reference_crop import is_reference_board

from .contracts import (
    Actor,
    Job,
    PrepareReferenceInput,
    ReferenceBinding,
    StudioProject,
    VideoReferenceInput,
)
from .store import StudioError, StudioPrerequisiteError


def available_alternatives(
    project: StudioProject, alternatives: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Exclude quarantined optional inputs without invalidating the requested source."""
    from .image_input_recovery import quarantine

    available, excluded = [], []
    for candidate in alternatives:
        blocked = quarantine(project, candidate["media_id"])
        if blocked:
            excluded.append(
                {
                    **candidate,
                    "reason": "image_quarantined",
                    "recovery_id": blocked.recovery_id,
                }
            )
        else:
            available.append(candidate)
    return available, excluded


def reference_snapshot(
    project: StudioProject,
    revision_id: str,
    *,
    approved_crop: bool = False,
    actor_id: str | None = None,
    clip_id: str | None = None,
) -> dict[str, Any]:
    from .external_review import blocked_quarantine

    revision = project.references.get(revision_id)
    if revision is None or revision.status not in (
        {"selected", "candidate", "rejected"} if approved_crop else {"selected", "candidate"}
    ):
        raise StudioError("参考图修订不可用")
    media = project.media.get(str(revision.media_id))
    if media is None or not media.mime_type.startswith("image/"):
        raise StudioError("参考修订缺少可用图片")
    state = project.states[revision.state_id]
    asset = project.assets[state.asset_id]
    blocked = blocked_quarantine(
        project,
        media.id,
        "reference_use",
        actor_id=actor_id,
        clip_id=clip_id,
    )
    if blocked:
        raise StudioPrerequisiteError(
            f"隔离图片不能准备、裁剪或作为普通参考复用："
            f"{asset.name}/{state.label}/{revision.view}"
            f"（revision_id={revision.id}, media_id={media.id}）",
            details={
                "reason": "image_quarantined",
                "asset_id": asset.id,
                "asset_name": asset.name,
                "state_id": state.id,
                "state_label": state.label,
                "revision_id": revision.id,
                "view": revision.view,
                "media_id": media.id,
                "recovery_id": blocked.recovery_id,
            },
            recovery="选择未隔离且实际查看过的同资产、同状态参考；保留原隔离记录，"
            "不要重新读取、裁剪或转送该隔离图片。",
        )
    return {
        "asset_id": asset.id,
        "asset_name": asset.name,
        "state_id": state.id,
        "state_label": state.label,
        "revision_id": revision.id,
        "view": revision.view,
        "media_id": media.id,
        "sha256": media.sha256,
    }


def preparation_job(project: StudioProject, actor: Actor, request: PrepareReferenceInput) -> Job:
    approved = None
    if request.region_approval_id:
        from .memory import require
        from .reference_regions import approved_region

        approved = approved_region(
            project, request.revision_id, request.region_approval_id, request.clip_id
        )
        require(
            request.crop == approved.crop
            and not request.full_board_reason
            and not request.preferred_view,
            "局部复用必须使用精确批准区域，不允许整图或自动换图",
        )
    source = reference_snapshot(
        project,
        request.revision_id,
        approved_crop=approved is not None,
        actor_id=actor.id,
        clip_id=request.clip_id,
    )
    state = project.states[source["state_id"]]
    preferred = [request.preferred_view] if request.preferred_view else []
    views = list(dict.fromkeys([*preferred, "close_up", "full_body", *state.selected_revisions]))
    alternatives = []
    excluded = []
    for view in [] if approved else views:
        if not view or is_reference_board(view):
            continue
        identifier = state.selected_revisions.get(view)
        if not identifier or identifier == request.revision_id:
            continue
        try:
            candidate = reference_snapshot(
                project, identifier, actor_id=actor.id, clip_id=request.clip_id
            )
        except StudioPrerequisiteError as error:
            if error.details.get("reason") != "image_quarantined":
                raise
            excluded.append(error.details)
            continue
        if candidate["asset_id"] != source["asset_id"] or candidate["state_id"] != state.id:
            raise StudioError("单视图回退必须属于同一资产及剧情状态")
        alternatives.append(candidate)
    return Job(
        actor_id=actor.id,
        kind="reference",
        target_id=request.revision_id,
        request={
            **request.model_dump(mode="json"),
            "source": source,
            "alternatives": alternatives,
            "excluded_alternatives": excluded,
            **({"region_approval": approved.model_dump(mode="json")} if approved else {}),
        },
    )


def video_reference(
    project: StudioProject,
    actor: Actor,
    item: VideoReferenceInput,
    *,
    clip_id: str | None = None,
) -> tuple[ReferenceBinding, dict[str, Any]]:
    binding = ReferenceBinding(
        asset_id=item.asset_id, state_id=item.state_id, revision_id=item.revision_id
    )
    job = project.jobs.get(item.prepared_reference_id or "")
    approval_id = job.request.get("region_approval_id") if job else None
    if approval_id:
        from .reference_regions import approved_region

        approved = approved_region(project, binding.revision_id, str(approval_id), clip_id)
        if (
            not job
            or job.request.get("clip_id") != clip_id
            or job.result.get("provenance", {}).get("selection") != "crop"
            or job.result.get("provenance", {}).get("crop", {}).get("normalized")
            != list(approved.crop)
        ):
            raise StudioError("只能绑定本 Clip 批准区域的实际裁剪结果，不能复用整图")
    source = reference_snapshot(
        project,
        binding.revision_id,
        approved_crop=bool(approval_id),
        actor_id=actor.id,
        clip_id=clip_id,
    )
    from .reference_coverage import semantic_record

    source["state_role"] = item.state_role
    source["reference_semantics"] = semantic_record(project, binding.revision_id, item.state_role)
    if source["asset_id"] != binding.asset_id or source["state_id"] != binding.state_id:
        raise StudioError("参考图与绑定的资产或剧情状态不一致")
    if item.prepared_reference_id:
        job = project.jobs.get(item.prepared_reference_id)
        if job is None or job.kind != "reference" or job.status != "complete":
            raise StudioError("请先完成参考素材准备")
        if job.actor_id != actor.id:
            from .prepared_reference_access import authorized_preparation

            if clip_id is None or not authorized_preparation(project, actor, job, clip_id=clip_id):
                raise StudioError("参考素材准备不属于当前片场导演")
        if job.result.get("binding") != binding.model_dump():
            raise StudioError("请使用参考准备实际返回的 binding，不要沿用原始参考板的绑定")
        media = project.media.get(str(job.result.get("media_id")))
        provenance = job.result.get("provenance", {})
        if media is None or media.sha256 != provenance.get("output_sha256"):
            raise StudioError("准备后的参考素材缺失或哈希不一致")
        from .external_review import blocked_quarantine

        if blocked_quarantine(
            project,
            media.id,
            "generation_input",
            actor_id=actor.id,
            clip_id=clip_id,
        ):
            raise StudioError("准备后的图片已隔离，不能重新作为参考复用")
        return binding, {
            **source,
            "media_id": media.id,
            "sha256": media.sha256,
            "purpose": item.purpose or job.request["purpose"],
            "prepared_reference_id": job.id,
            "preparation": provenance,
            **({"region_approval_id": approval_id} if approval_id else {}),
        }
    if is_reference_board(source["view"]) and not (item.full_board_reason or "").strip():
        raise StudioError("参考板请先通过 prepare_reference 选择单视图，或说明 full_board_reason")
    from .external_review import blocked_quarantine

    if blocked_quarantine(
        project,
        source["media_id"],
        "generation_input",
        actor_id=actor.id,
        clip_id=clip_id,
    ):
        raise StudioError("隔离图片未获得当前 Clip 的外部生成输入批准")
    return binding, {
        **source,
        "purpose": item.purpose,
        "selection": "full_board" if is_reference_board(source["view"]) else "single",
        "full_board_reason": item.full_board_reason,
    }
