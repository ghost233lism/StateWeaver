"""Executive-owned voice baselines and scoped adoption of observed visual evidence."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .continuity import excerpt_job, waiting_for_voices
from .contracts import (
    Actor,
    Job,
    MemoryEntry,
    MemoryScope,
    MemorySource,
    PrepareMediaInput,
    StudioProject,
    VoiceReference,
)
from .memory import validate_scope
from .quality import ensure

if TYPE_CHECKING:
    from .service import StudioService


VOICE_ASSET_KINDS = frozenset({"person", "character", "人物", "角色", "voice", "narrator", "旁白"})


def validate_voice_dependencies(project: StudioProject) -> None:
    clips = {
        c: project.clips[c]
        for production_set in project.sets.values()
        for c in production_set.clip_ids
    }
    graph: dict[str, set[str]] = {}
    for clip in clips.values():
        if clip.accepted_candidate_id:
            continue
        production_set = project.sets[clip.set_id]
        edges = (
            set(production_set.clip_ids[clip.order - 1 : clip.order])
            if clip.order
            else {
                project.sets[d].clip_ids[-1]
                for d in production_set.depends_on
                if project.sets[d].clip_ids
            }
        )
        for identifier in clip.speaking_asset_ids:
            asset = project.assets[identifier]
            if not asset.selected_voice_id and asset.voice_bootstrap_clip_id not in {None, clip.id}:
                edges.add(str(asset.voice_bootstrap_clip_id))
        graph[clip.id] = edges
    visited: set[str] = set()
    pending: set[str] = set()

    def visit(identifier: str) -> None:
        ensure(identifier not in pending, "音色基准与片场顺序形成循环，请选择更早或共同的正式 Clip")
        if identifier in visited:
            return
        pending.add(identifier)
        for dependency in graph.get(identifier, set()):
            visit(dependency)
        pending.remove(identifier)
        visited.add(identifier)

    for identifier in graph:
        visit(identifier)


def wake_voice_users(project: StudioProject, asset_id: str) -> None:
    for production_set in project.sets.values():
        unfinished = next(
            (
                project.clips[c]
                for c in production_set.clip_ids
                if not project.clips[c].accepted_candidate_id
            ),
            None,
        )
        if (
            unfinished
            and asset_id in unfinished.speaking_asset_ids
            and not waiting_for_voices(project, unfinished)
        ):
            actor = project.actors[production_set.actor_id]
            if actor.native_session_blocked:
                continue
            actor.request_wake()
            actor.summary = "人物音色已准备好，可以继续生成"
            if not actor.paused and actor.status not in {"running", "complete"}:
                actor.status = "ready"


def plan_voice(project: StudioProject, actor: Actor, data: dict[str, Any]) -> dict[str, Any]:
    ensure(actor.role == "executive", "只有总导演能够指定人物音色基准")
    asset = project.assets.get(str(data.get("asset_id")))
    clip = project.clips.get(str(data.get("clip_id")))
    ensure(
        asset and clip and asset.id in clip.speaking_asset_ids, "基准 Clip 需要明确包含该人物发声"
    )
    assert asset is not None and clip is not None
    ensure(asset.kind in VOICE_ASSET_KINDS, "固定音色基准只能属于人物资产或旁白声音资产")
    ensure(
        not clip.archived_at and clip.id in project.sets[clip.set_id].clip_ids,
        "音色基准必须选择当前活动计划中的 Clip，不能等待已归档的计划",
    )
    ensure(not asset.selected_voice_id, "此人物已有固定音色，不需要重新启动基准任务")
    ensure(data.get("reason"), "需要记录选择基准 Clip 的理由")
    asset.voice_bootstrap_clip_id = clip.id
    validate_voice_dependencies(project)
    project.global_memory.revision += 1
    wake_voice_users(project, asset.id)
    return {"asset_id": asset.id, "clip_id": clip.id, "summary": data["reason"]}


def establish_voice(project: StudioProject, actor: Actor, data: dict[str, Any]) -> dict[str, Any]:
    ensure(actor.role == "executive", "只有总导演能够建立人物音色基准")
    asset = project.assets.get(str(data.get("asset_id")))
    candidate = project.candidates.get(str(data.get("candidate_id")))
    ensure(asset and candidate and candidate.status == "accepted", "音色必须来自已选用的正式 Clip")
    assert asset is not None and candidate is not None
    ensure(asset.kind in VOICE_ASSET_KINDS, "固定音色基准只能属于人物资产或旁白声音资产")
    clip = project.clips[candidate.clip_id]
    ensure(
        asset.id in clip.speaking_asset_ids
        or any(b.asset_id == asset.id for b in candidate.bindings),
        "来源 Clip 没有该人物的发声计划或身份绑定",
    )
    ensure(
        not any(v.status == "preparing" for v in asset.voice_references),
        "人物音色正在准备，请先回收结果",
    )
    request = PrepareMediaInput(
        source_candidate_id=candidate.id,
        kind="audio",
        start_seconds=data["start_seconds"],
        end_seconds=data["end_seconds"],
        purpose=str(data["reason"]),
    )
    job = excerpt_job(project, actor, request)
    voice = VoiceReference(
        source_candidate_id=candidate.id,
        start_seconds=request.start_seconds,
        end_seconds=float(request.end_seconds or 0),
        reason=data["reason"],
        job_id=job.id,
    )
    job.target_id = asset.id
    job.request.update(voice_asset_id=asset.id, voice_reference_id=voice.id)
    project.jobs[job.id] = job
    asset.voice_references.append(voice)
    project.global_memory.revision += 1
    return {
        "asset_id": asset.id,
        "voice_reference_id": voice.id,
        "job_id": job.id,
        "candidate_id": candidate.id,
    }


def select_voice(project: StudioProject, actor: Actor, data: dict[str, Any]) -> dict[str, Any]:
    ensure(actor.role == "executive", "只有总导演能够选用固定音色")
    asset = project.assets.get(str(data.get("asset_id")))
    ensure(asset, "人物资产不存在")
    assert asset is not None
    ensure(asset.kind in VOICE_ASSET_KINDS, "固定音色基准只能属于人物资产或旁白声音资产")
    voice = next(
        (v for v in asset.voice_references if v.id == data.get("voice_reference_id")), None
    )
    ensure(voice and voice.media_id and data.get("reason"), "音色素材尚未准备好或缺少选用理由")
    assert voice is not None
    ensure(project.media[str(voice.media_id)].mime_type.startswith("audio/"), "音色素材不是音频")
    before = asset.selected_voice_id
    for reference in asset.voice_references:
        if reference.status == "selected":
            reference.status = "historical"
    voice.status = "selected"
    asset.selected_voice_id = voice.id
    project.global_memory.revision += 1
    wake_voice_users(project, asset.id)
    return {
        "asset_id": asset.id,
        "voice_reference_id": voice.id,
        "before_voice_reference_id": before,
        "media_id": voice.media_id,
        "summary": data["reason"],
    }


def apply_voice_result(project: StudioProject, job: Job) -> None:
    asset = project.assets[str(job.request["voice_asset_id"])]
    voice = next(v for v in asset.voice_references if v.id == job.request["voice_reference_id"])
    voice.media_id = str(job.result["media_id"])
    # The executive already selected this precise source and interval in establish_voice.
    select_voice(
        project,
        project.actors[job.actor_id],
        {
            "asset_id": asset.id,
            "voice_reference_id": voice.id,
            "reason": voice.reason,
        },
    )


def approve_asset_observation(
    service: StudioService,
    project: StudioProject,
    actor: Actor,
    data: dict[str, Any],
) -> dict[str, Any]:
    ensure(actor.role == "executive", "只有总导演可以确认全局资产的局部观察")
    asset_id = str(data.get("asset_id"))
    ensure(
        asset_id in project.assets and data.get("text") and data.get("reason"),
        "需要明确资产、已确认的局部内容和采纳理由",
    )
    job = project.jobs.get(str(data.get("preparation_job_id")))
    ensure(
        job
        and job.status == "complete"
        and job.request.get("operation") == "media_excerpt"
        and job.request.get("kind") == "image",
        "请先从实际候选截取截图",
    )
    assert job is not None
    state_ids = list(data.get("state_ids", []))
    attributes = list(data.get("attributes", []))
    ensure(
        state_ids
        and all(i in project.states and project.states[i].asset_id == asset_id for i in state_ids),
        "请明确一个或多个属于该资产的认可状态，不能只批准整个资产",
    )
    ensure(
        attributes
        and all(isinstance(v, str) and v.strip() for v in attributes)
        and data.get("purpose"),
        "请明确截图中正确的局部属性及允许用途",
    )
    media_id = str(job.result["media_id"])
    from .image_input_recovery import inspected_evidence

    inspected = inspected_evidence(project, actor, [media_id], service.store)
    ensure(
        media_id in inspected,
        "总导演需要先查看实际截图，再单独确认其中正确的局部",
    )
    scope = MemoryScope.model_validate(data.get("scope") or {})
    ensure(
        not scope.asset_ids or scope.asset_ids == [asset_id],
        "局部观察的适用对象只能是本次批准的资产",
    )
    scope.asset_ids = [asset_id]
    validate_scope(project, scope)
    entry = MemoryEntry(
        kind="observation",
        text=str(data["text"]),
        source_candidate_id=job.request["source_candidate_id"],
        evidence_media_ids=[media_id],
        approved_asset_id=asset_id,
        approved_by=actor.id,
        approval_reason=data["reason"],
        source_job_id=job.id,
        section="events",
        basis="observed",
        actor_id=actor.id,
        # A state's starting event is a boundary, not its entire applicability.
        # Approved state IDs constrain reads throughout that state's lifetime;
        # narrower Set/Clip/event use must be an explicit executive decision.
        scope=scope,
        sources=[
            MemorySource(kind="candidate", id=job.request["source_candidate_id"]),
            MemorySource(kind="job", id=job.id),
        ],
        approved_state_ids=state_ids,
        approved_attributes=attributes,
        approval_purpose=str(data["purpose"]),
        source_seconds=float(job.request["start_seconds"]),
        reason=str(data["reason"]),
    )
    project.global_memory.story.append(entry)
    project.global_memory.revision += 1
    return {
        "scope": "global",
        "asset_id": asset_id,
        "entry_id": entry.id,
        "media_id": media_id,
        "approved_scope": scope.model_dump(mode="json"),
        "summary": entry.text,
        "reason": data["reason"],
    }
