"""Scoped reference/state/voice decisions shared by MCP commands and scheduling."""

from __future__ import annotations

from typing import Any

from stateweaver.studio_media.latent import CONTINUATION_FPS, CONTINUATION_FRAMES

from .contracts import (
    Actor,
    Candidate,
    Clip,
    Job,
    PrepareMediaInput,
    StudioProject,
    TimedMediaBinding,
)
from .store import StudioError
from .workflow import CONTINUATION_SECONDS


def clip_story_time(project: StudioProject, clip: Clip) -> float:
    return clip.story_time if clip.story_time is not None else project.sets[clip.set_id].story_time


def waiting_for_voices(project: StudioProject, clip: Clip) -> list[str]:
    return [
        identifier
        for identifier in clip.speaking_asset_ids
        if not project.assets[identifier].selected_voice_id
        and project.assets[identifier].voice_bootstrap_clip_id != clip.id
    ]


def excerpt_job(project: StudioProject, actor: Actor, request: PrepareMediaInput) -> Job:
    candidate = project.candidates.get(request.source_candidate_id)
    if candidate is None or not candidate.media_id:
        raise StudioError("来源候选没有可用视频")
    source = project.media.get(candidate.media_id)
    if source is None or not source.mime_type.startswith("video/") or not source.duration:
        raise StudioError("来源候选缺少已解码的视频或时长")
    if request.start_seconds >= source.duration or (
        request.end_seconds is not None and request.end_seconds > source.duration + 0.001
    ):
        raise StudioError("截取区间超出实际视频时长")
    return Job(
        actor_id=actor.id,
        kind="reference",
        target_id=candidate.id,
        request={
            "operation": "media_excerpt",
            **request.model_dump(mode="json"),
            "source": {
                "candidate_id": candidate.id,
                "clip_id": candidate.clip_id,
                "media_id": source.id,
                "sha256": source.sha256,
                "selection_at_preparation": candidate.status,
            },
            **({"continuation_policy": "real-tail73-v1"} if (request.kind == "video") else {}),
        },
    )


def ensure_continuation_tail(project: StudioProject, candidate: Candidate) -> Job | None:
    """Prepare once on selection; a retry never re-encodes the same accepted tail."""
    clip = project.clips[candidate.clip_id]
    if clip.accepted_candidate_id != candidate.id or not candidate.media_id:
        raise StudioError("固定接续素材必须来自已选用的真实候选")
    existing = project.jobs.get(str(candidate.continuation_reference_job_id))
    if existing is not None:
        return existing
    source = project.media[candidate.media_id]
    if source.duration is None or source.duration < CONTINUATION_SECONDS:
        raise StudioError("前段实际视频不足 73 帧（约 3.042 秒），无法准备固定接续素材")
    actor = project.actors[project.sets[clip.set_id].actor_id]
    job = excerpt_job(
        project,
        actor,
        PrepareMediaInput(
            source_candidate_id=candidate.id,
            kind="video",
            start_seconds=source.duration - CONTINUATION_SECONDS,
            end_seconds=source.duration,
            purpose="保留前段最后 73 个真实帧（约 3.042 秒）的动作、构图和运镜趋势",
        ),
    )
    job.request["automatic_continuation"] = True
    job.request["tail_frame_count"] = CONTINUATION_FRAMES
    job.request["tail_fps"] = CONTINUATION_FPS
    project.jobs[job.id] = job
    candidate.continuation_reference_job_id = job.id
    return job


def timed_media_reference(
    project: StudioProject,
    actor: Actor,
    clip: Clip,
    item: TimedMediaBinding,
    kind: str,
) -> dict[str, Any]:
    if item.voice_reference_id:
        if kind != "audio" or item.role != "voice" or not item.asset_id:
            raise StudioError("固定音色必须绑定人物、音色版本及 voice 用途")
        asset = project.assets.get(item.asset_id)
        if asset is None:
            raise StudioError("音色所属人物不存在")
        voice = next((v for v in asset.voice_references if v.id == item.voice_reference_id), None)
        if voice is None or not voice.media_id or asset.selected_voice_id != voice.id:
            raise StudioError("新任务需要使用人物当前选定的音色版本")
        media = project.media.get(voice.media_id)
        source_id = voice.source_candidate_id
        provenance: dict[str, Any] = {
            "asset_id": asset.id,
            "voice_reference_id": voice.id,
            "start_seconds": voice.start_seconds,
            "end_seconds": voice.end_seconds,
        }
    else:
        job = project.jobs.get(str(item.preparation_job_id))
        if job is None or job.kind != "reference" or job.status != "complete":
            raise StudioError("请先完成视频或音频截取任务")
        if job.actor_id != actor.id or job.request.get("operation") != "media_excerpt":
            raise StudioError("参考截取不属于当前导演")
        media = project.media.get(str(job.result.get("media_id")))
        source_id = str(job.request["source_candidate_id"])
        provenance = dict(job.result.get("provenance", {}))
        provenance["preparation_job_id"] = job.id
    if media is None or not media.mime_type.startswith(kind + "/") or not media.duration:
        raise StudioError("参考素材类型或时长不符")
    source = project.candidates[source_id]
    if kind == "video" and (
        item.role != "continuation"
        or project.clips[source.clip_id].accepted_candidate_id != source.id
        or source.clip_id == clip.id
    ):
        raise StudioError(
            "片场视频参考只允许前段已验收的尾段；失败候选仅用于诊断，不能作为编辑输入"
        )
    if (
        project.clips[source.clip_id].accepted_candidate_id != source.id
        and not item.voice_reference_id
        and not (kind == "video" and item.role == "edit_source" and source.clip_id == clip.id)
    ):
        raise StudioError("未选候选只能作为自身 Clip 的显式修正来源")
    if kind == "video" and item.role not in {"continuation", "edit_source", "motion", "identity"}:
        raise StudioError("视频参考用途无效")
    if kind == "audio" and item.role not in {"voice", "ambience", "sound"}:
        raise StudioError("音频参考用途无效")
    return {
        **provenance,
        "media_id": media.id,
        "sha256": media.sha256,
        "duration": media.duration,
        "source_candidate_id": source_id,
        "role": item.role,
        "purpose": item.purpose,
    }
