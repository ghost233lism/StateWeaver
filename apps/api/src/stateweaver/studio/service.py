"""Business operations used identically by HTTP and actor-scoped MCP tools."""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from typing import Any, Literal

from pydantic import ValidationError

from stateweaver.providers.registry import APIKind, APIRegistry
from stateweaver.providers.video_http_config import HTTP_VIDEO_PROTOCOL
from stateweaver.studio.adjudication import (
    confirm_candidate_state,
    confirm_end_state,
)
from stateweaver.studio.continuity import (
    clip_story_time,
    excerpt_job,
    waiting_for_voices,
)
from stateweaver.studio.contracts import (
    Actor,
    AssetHandoff,
    AssetHandoffPlan,
    AssetState,
    Candidate,
    CandidateReview,
    Clip,
    ClipAssetPlan,
    ClipPreparation,
    ClipPreparationUpdate,
    ContextRead,
    CreateProject,
    EndState,
    EndStateConfirmation,
    EventExecution,
    ExternalCandidateReviewRequest,
    ExternalMediaReview,
    ExternalOperatorTakeoverRequest,
    ExternalReviewRequest,
    ImageInputBatch,
    Job,
    LogicalAsset,
    MemoryScope,
    MemorySource,
    PrepareMediaInput,
    PrepareReferenceInput,
    ProductionSet,
    ReferenceBinding,
    ReferenceImageInstructions,
    ReferenceRevision,
    RepairPlan,
    ScreenplayDraft,
    SelectionRecord,
    StateEvolutionInput,
    StoryAnchor,
    StudioProject,
    ToolCommand,
    TranscriptItem,
    UserAction,
    new_id,
    now,
)
from stateweaver.studio.memory import (
    anchor_key,
    digest,
    plan_events,
    seed_screenplay_memory,
    validate_clip_plan,
    validate_sources,
)
from stateweaver.studio.memory import (
    write_memory as write_story_memory,
)
from stateweaver.studio.proposals import (
    clip_pending_proposals,
    propose_global,
    resolve_proposal,
)
from stateweaver.studio.quality import (
    clip_episode,
    escalate_clip,
    escalate_image,
    image_episode,
    ordinary_clip_corrections_complete,
    ordinary_image_corrections_complete,
    record_observed_states,
    resolve_quality,
)
from stateweaver.studio.reference_inputs import preparation_job
from stateweaver.studio.reference_plans import (
    finalize_reference_plan,
    save_reference_plan,
    submission_plan,
)
from stateweaver.studio.repository import select_entities
from stateweaver.studio.screenplay import render_screenplay
from stateweaver.studio.store import StudioError, StudioStore
from stateweaver.studio.task_context import require_context
from stateweaver.studio.voice import (
    VOICE_ASSET_KINDS,
    approve_asset_observation,
    establish_voice,
    plan_voice,
    select_voice,
    validate_voice_dependencies,
)
from stateweaver.studio.workflow import (
    CONTINUATION_SECONDS,
    H3_PROMPT_SKILL,
    SELF_REVIEW,
    can_plan_set,
    unit_name,
)
from stateweaver.studio_media.assets import image_recipe_snapshot
from stateweaver.studio_media.evidence import SamplingInterval
from stateweaver.studio_media.h3_prompt import SET_FORMAT
from stateweaver.studio_media.latent import CONTINUATION_FPS, CONTINUATION_FRAMES
from stateweaver.studio_media.skills import load_media_skill
from stateweaver.studio_media.video_http import text_only_prompt


def require(condition: object, message: str) -> None:
    if not condition:
        raise StudioError(message)


def get_actor(project: StudioProject, actor_id: str) -> Actor:
    require(actor_id in project.actors, "角色不存在")
    return project.actors[actor_id]


def role(actor: Actor, *allowed: str) -> None:
    require(actor.role in allowed, "该角色没有此操作权限")


def set_for(project: StudioProject, actor: Actor) -> ProductionSet:
    role(actor, "set_director")
    require(actor.set_id in project.sets, f"{unit_name(project)}不存在")
    return project.sets[str(actor.set_id)]


def add_item(
    project: StudioProject,
    actor_id: str,
    kind: Literal["message", "tool", "memory_change", "asset_change", "status", "error", "user"],
    text: str,
    data: dict[str, Any] | None = None,
) -> str:
    item = TranscriptItem(
        id=new_id(),
        actor_id=actor_id,
        kind=kind,
        text=text,
        data=data or {},
    )
    project.transcript[item.id] = item
    return item.id


def wake_executive(project: StudioProject, summary: str, *, dependency_event: bool = False) -> None:
    if project.deleted_at:
        return
    for actor in select_entities(project.actors, subtype="executive"):
        if (
            dependency_event
            and not actor.paused
            and actor.native_session_blocked
            and actor.recovery_history
            and actor.recovery_history[-1].get("reason_category") == "no_progress"
        ):
            # A genuinely new, durable delegate result makes a stalled decision
            # actionable again. Never unblock protocol/auth/image refusals, infer
            # old error categories from prose, or reset quality/recovery history.
            actor.no_progress_count = 0
            actor.recovery_not_before = 0
            actor.last_error = None
        if (
            actor.role == "executive"
            and actor.status != "complete"
            and not actor.native_session_blocked
        ):
            actor.request_wake()
            actor.summary = summary
            if not actor.paused and actor.status != "running":
                actor.status = "ready"


def chronological_state(project: StudioProject, asset_id: str, story_time: float) -> AssetState:
    require(asset_id in project.assets, "资产不存在")
    states = select_entities(project.states, parent_id=asset_id, story_time_max=story_time, limit=1)
    require(states, "资产在此故事时间尚无状态")
    return max(states, key=lambda state: state.story_time)


class StudioService:
    def __init__(self, store: StudioStore, api_registry: APIRegistry | None = None) -> None:
        self.store = store
        self.api_registry = api_registry
        self.provider_receipt_reader: Callable[[str], dict[str, Any]] | None = None
        self.video_settings: dict[str, int | str] = {"short_edge": 768, "aspect_ratio": "16:9"}

    def create(self, request: CreateProject) -> StudioProject:
        project_id = new_id()
        creation_fingerprint = None
        if request.idempotency_key is not None:
            from .command_receipts import creation_project_id

            project_id = creation_project_id(request.idempotency_key)
            creation_fingerprint = digest(
                request.model_dump(mode="json", exclude_unset=True, exclude={"idempotency_key"})
            )
            if existing := self.store.replay_creation(project_id, creation_fingerprint):
                return existing
        custom_video = False
        # Project model choices must belong to the fixed service for their media kind.
        if self.api_registry is not None:
            model_fields: tuple[tuple[APIKind, tuple[str, ...]], ...] = (
                ("text", ("text_model", "director_model")),
                ("image", ("image_model",)),
                ("video", ("video_model",)),
            )
            models: dict[str, str] = {}
            for kind, fields in model_fields:
                binding = self.api_registry.fixed_service(kind)
                if binding is None:
                    if any(field in request.config.model_fields_set for field in fields):
                        raise StudioError(f"未配置 {kind} API 服务", 422)
                    continue
                if kind == "video" and binding.protocol == HTTP_VIDEO_PROTOCOL:
                    custom_video = True
                for field in fields:
                    model = getattr(request.config, field)
                    if field not in request.config.model_fields_set:
                        model = binding.models[0]
                        models[field] = model
                    if model is not None and model not in binding.models:
                        raise StudioError(f"指定 {field} 没有匹配的已注册 API 模型", 422)
            if models:
                request = request.model_copy(
                    update={"config": request.config.model_copy(update=models)}
                )
        writer = Actor(
            role="screenwriter",
            goal=(
                "Develop the story screenplay with the user. Save the film "
                "overview, characters, story, key facts and natural overall "
                "duration estimate. Do not plan Sets, Clips or segment "
                "timecodes. After confirmation the executive organizes Sets and"
                " Set directors plan Clips."
            ),
        )
        executive = Actor(
            role="executive",
            status="idle",
            wake_requested=False,
            goal=(
                "From the confirmed screenplay, assign each Set its story goals"
                " and entry/exit states, prepare materials and coordinate "
                "production through the completed film."
            ),
        )
        project = StudioProject(
            id=project_id,
            title=request.title,
            brief=request.brief,
            config=request.config.model_copy(
                update={
                    "repairs_per_clip": min(request.config.repairs_per_clip, 1),
                    "video_executive_extra_attempts": min(
                        request.config.video_executive_extra_attempts
                        if request.config.video_executive_extra_attempts is not None
                        else 2,
                        2,
                    ),
                    "continuation_seconds": CONTINUATION_SECONDS,
                    **({"latent_continuation_mode": "off"} if custom_video else {}),
                }
            ),
            director_mode=request.director_mode,
            actors={writer.id: writer, executive.id: executive},
            api_config_snapshot=self.api_registry.snapshot() if self.api_registry else {},
        )
        add_item(project, writer.id, "user", request.brief)
        return self.store.create(project, creation_fingerprint=creation_fingerprint)

    def delete_project(self, project_id: str, request: UserAction) -> StudioProject:
        """Tombstone the project; preserve submitted work and its collection handles."""

        def apply(project: StudioProject) -> dict[str, Any]:
            if project.deleted_at:
                raise StudioError("项目已删除", 410)
            project.deleted_at = now()
            for actor in project.actors.values():
                actor.paused = True
                actor.wake_requested = False
                if actor.status != "complete":
                    actor.status = "paused"
                    actor.summary = "项目已删除，停止后续制作；已提交的任务仍会回收"
                # Keep the lease until the running turn observes the pause and drains.
            return {"action": "delete"}

        return self.store.mutate(
            project_id,
            apply,
            expected_revision=request.expected_revision,
            key="user:" + request.idempotency_key,
            fingerprint_data=["delete"],
            kind="project_deleted",
        )

    def user_action(
        self,
        project_id: str,
        action: str,
        request: UserAction,
        actor_id: str | None = None,
    ) -> StudioProject:
        recovery_seeds: dict[str, ImageInputBatch | None] = {}
        if action in {"recover_image_input", "recover_project_image_inputs"}:
            from .image_input_recovery import historical_batch, is_image_refusal

            def seeds(p: StudioProject) -> dict[str, ImageInputBatch | None]:
                actors = (
                    [get_actor(p, str(actor_id))]
                    if action == "recover_image_input"
                    else [
                        a
                        for a in select_entities(p.actors, statuses=["blocked"])
                        if a.role != "screenwriter"
                        and not a.paused
                        and is_image_refusal(a.last_error or "")
                    ]
                )
                return {a.id: historical_batch(p, a) for a in actors}

            # Read archived native bodies before taking the shared writer lock.
            recovery_seeds = self.store.read(project_id, seeds)

        def apply(project: StudioProject) -> dict[str, Any]:
            if project.deleted_at:
                raise StudioError("项目已删除", 410)
            if action in {"message", "confirm"}:
                require(project.status == "screenwriting", "剧本已确认，制作期间不可修改")
                writer = next(a for a in project.actors.values() if a.role == "screenwriter")
                if action == "confirm" and request.director_mode is not None:
                    project.director_mode = request.director_mode
                if action == "message":
                    require(request.text.strip(), "请输入创作要求")
                    require(not writer.paused, "请先继续编剧")
                    add_item(project, writer.id, "user", request.text)
                    writer.request_wake()
                    writer.status = "ready" if writer.status != "running" else "running"
                    writer.no_progress_count = 0
                else:
                    require(project.screenplay.text.strip(), "编剧尚未保存剧本")
                    require(
                        project.screenplay.manuscript is not None,
                        "请先由编剧保存完整故事与整体预计时长，再确认剧本",
                    )
                    require(
                        writer.status == "waiting" and not writer.wake_requested,
                        "请等待编剧完成当前修改后确认",
                    )
                    project.screenplay.confirmed_at = now()
                    project.screenplay.frozen_text_sha256 = hashlib.sha256(
                        project.screenplay.text.encode()
                    ).hexdigest()
                    project.screenplay.confirmed_hash = hashlib.sha256(
                        project.screenplay.model_dump_json().encode()
                    ).hexdigest()
                    project.status = "preparing"
                    writer.status = "complete"
                    writer.summary = "Screenplay confirmed; story facts are frozen."
                    writer.wake_requested = False
                    seed_screenplay_memory(project, writer.id)
                    wake_executive(project, f"剧本已确认，准备资产目录和{unit_name(project)}")
            elif action in {"recover_image_input", "recover_project_image_inputs"}:
                from .image_input_recovery import is_image_refusal, recover_image_input

                require(recovery_seeds, "没有因图片拒绝而阻塞且未被用户暂停的导演")
                require(request.text.strip(), "请记录本次恢复原因")
                for identifier, seed in recovery_seeds.items():
                    actor = get_actor(project, identifier)
                    require(
                        actor.role != "screenwriter"
                        and actor.status == "blocked"
                        and not actor.paused,
                        "只恢复未被用户暂停、因图片拒绝而阻塞的导演",
                    )
                    require(not actor.lease_owner and not actor.turn_id, "请等待角色回合与租约释放")
                    require(is_image_refusal(actor.last_error or ""), "当前错误不是图片策略拒绝")
                    if seed:
                        require(
                            seed.thread_id == actor.thread_id
                            and seed.session_key == actor.session_key,
                            "会话或审核上下文在准备恢复期间已改变，请重新读取",
                        )
                        actor.last_image_input = seed
                # All exclusions and native detachments commit together, so a director
                # cannot resume before a second blocked director's batch is excluded.
                for identifier in recovery_seeds:
                    actor = project.actors[identifier]
                    add_item(project, actor.id, "user", request.text)
                    recover_image_input(project, actor, str(actor.last_error))
            elif action == "reset_context":
                actor = get_actor(project, str(actor_id))
                require(actor.status in {"paused", "blocked"}, "请先暂停角色再重建模型上下文")
                require(
                    not actor.lease_owner and actor.lease_until <= time.time(),
                    "请等待当前角色回合停止",
                )
                from .role_receipts import role_rebuild_requires_reconciliation

                session = actor.sessions.get(actor.session_key)
                require(
                    not session or not role_rebuild_requires_reconciliation(session.role_journal),
                    "请先核对原模型回执，再重建模型上下文",
                )
                # A released blocked role can retain the last failed turn ID. It
                # names archived history, not a live execution lease; an explicit
                # operator reset must not wait forever for an exited turn.
                require(actor.thread_id is not None, "角色尚无模型会话")
                require(request.text.strip(), "请记录重建模型上下文的原因")
                previous_thread_id = actor.thread_id
                previous_turn_id = actor.turn_id
                actor.turn_id = None
                actor.thread_id = None
                actor.history_turn_id = None
                from .director_efficiency import invalidate_context

                invalidate_context(project, actor, reason="operator_context_reset")
                if session:
                    # activate_session must not restore the detached native thread.
                    session.thread_id = None
                    session.history_turn_id = None
                    session.image_receipts.clear()
                    session.viewed_media_ids.clear()
                    session.image_count = 0
                actor.no_progress_count = 0
                actor.recovery_attempts = 0
                actor.recovery_not_before = 0
                actor.paused = True
                actor.status = "paused"
                actor.wake_requested = False
                actor.last_error = None
                actor.summary = "模型上下文已重建；继续后将读取已有剧本、记忆和任务"
                add_item(
                    project,
                    actor.id,
                    "tool",
                    "重建模型上下文",
                    {
                        "previous_thread_id": previous_thread_id,
                        "previous_turn_id": previous_turn_id,
                        "reason": request.text,
                    },
                )
            else:
                actor = get_actor(project, str(actor_id))
                require(actor.status != "complete", "角色任务已经完成")
                actor.paused = action == "pause"
                actor.status = "paused" if actor.paused else "ready"
                if actor.paused:
                    actor.wake_requested = False
                else:
                    actor.request_wake()
                actor.summary = (
                    "已暂停；已提交的生成任务仍会回收" if actor.paused else "继续处理已保存的进度"
                )
                actor.last_error = None
                if action in {"pause", "resume"}:
                    return {
                        "action": action,
                        "user_operation_key": request.idempotency_key,
                    }
            return {"action": action}

        fingerprint = [action, actor_id, request.text]

        return self.store.mutate(
            project_id,
            apply,
            expected_revision=request.expected_revision,
            key="user:" + request.idempotency_key,
            fingerprint_data=[*fingerprint, request.director_mode]
            if action == "confirm"
            else fingerprint,
            actor_id=actor_id if action in {"pause", "resume"} else None,
        )

    def submit_external_review(
        self, project_id: str, request: ExternalReviewRequest
    ) -> StudioProject:
        """Record an operator's media decision without forging a native inspection.

        The receipt is immutable evidence scoped to the exact current media hash and
        declared purpose.  Provider refusals and image quarantines are deliberately
        left untouched; downstream gates may consume this separate provenance only
        for an explicitly supported purpose.
        """

        def apply(project: StudioProject) -> dict[str, Any]:
            require(not project.deleted_at, "项目已删除")
            scope = request.scope
            if scope.actor_id is not None:
                require(scope.actor_id in project.actors, "外部审核范围中的角色不存在")
            if scope.clip_id is not None:
                require(scope.clip_id in project.clips, "外部审核范围中的 Clip 不存在")
            if scope.candidate_id is not None:
                require(scope.candidate_id in project.candidates, "外部审核范围中的候选不存在")
                candidate = project.candidates[scope.candidate_id]
                if scope.clip_id is not None:
                    require(candidate.clip_id == scope.clip_id, "候选不属于外部审核指定的 Clip")
            require(scope.purposes, "外部审核必须声明至少一个用途")
            require(request.items, "外部审核必须包含至少一个素材裁决")
            media_ids = [item.media_id for item in request.items]
            require(len(media_ids) == len(set(media_ids)), "同一外部审核不能重复提交素材")
            for item in request.items:
                media = project.media.get(item.media_id)
                require(media is not None, "外部审核素材不存在")
                assert media is not None
                require(
                    media.sha256 == item.source_sha256,
                    "素材内容已变化，外部审核哈希失效；请重新查看当前素材",
                )
                quarantine_ids = {
                    quarantine.id
                    for quarantine in project.image_quarantines.values()
                    if quarantine.media_id == item.media_id or quarantine.id == item.source_sha256
                }
                from .image_input_recovery import quarantine

                current_quarantine = quarantine(project, item.media_id)
                if current_quarantine is not None:
                    quarantine_ids.add(current_quarantine.id)
                acknowledged = set(item.acknowledged_quarantine_ids)
                require(
                    acknowledged <= quarantine_ids,
                    "外部审核引用了不属于当前素材的隔离记录",
                )
                if item.verdict == "approved" and quarantine_ids:
                    require(
                        quarantine_ids <= acknowledged,
                        "批准被隔离素材前必须逐项确认原始隔离记录",
                    )
            receipt = ExternalMediaReview(
                project_id=project.id,
                reviewer_id=request.reviewer_id,
                authorization_id=request.authorization_id,
                reason=request.reason,
                scope=scope,
                items=request.items,
            )
            from .external_review import validate_external_media_review

            try:
                validate_external_media_review(project, receipt)
            except ValueError as error:
                raise StudioError(str(error)) from error
            project.external_reviews[receipt.id] = receipt
            audit_actor = scope.actor_id or next(
                (actor.id for actor in project.actors.values() if actor.role == "executive"),
                next(iter(project.actors)),
            )
            add_item(
                project,
                audit_actor,
                "tool",
                "外部人工审核已记录；原始拒绝与隔离记录保持不变",
                {
                    "source": "external_operator_review",
                    "external_review_id": receipt.id,
                    "reviewer_id": receipt.reviewer_id,
                    "authorization_id": receipt.authorization_id,
                    "scope": receipt.scope.model_dump(mode="json"),
                    "items": [item.model_dump(mode="json") for item in receipt.items],
                },
            )
            return {"external_review_id": receipt.id}

        return self.store.mutate(
            project_id,
            apply,
            expected_revision=request.expected_revision,
            key=f"external-review:{request.reviewer_id}:{request.idempotency_key}",
            fingerprint_data=[
                "external_review",
                request.model_dump(exclude={"expected_revision", "idempotency_key"}, mode="json"),
            ],
            kind="external_review_recorded",
        )

    def operator_takeover(
        self,
        project_id: str,
        actor_id: str,
        request: ExternalOperatorTakeoverRequest,
    ) -> StudioProject:
        """Resume or detach a stopped native context under an audited operator identity."""

        def apply(project: StudioProject) -> dict[str, Any]:
            require(not project.deleted_at, "项目已删除")
            actor = get_actor(project, actor_id)
            require(actor.role != "screenwriter", "外部生产接管不适用于编剧确认流程")
            require(
                actor.status in {"blocked", "paused", "waiting"},
                "只能接管已阻塞、已暂停或等待外部裁决的生产角色",
            )
            require(
                not actor.lease_owner or actor.lease_until <= time.time(),
                "请等待当前角色回合与租约释放",
            )
            pending_jobs = [
                job.id
                for job in select_entities(
                    project.jobs,
                    actor_id=actor.id,
                    statuses=["queued", "submitting", "running", "ambiguous"],
                )
            ]
            require(not pending_jobs, "角色仍有提交中、运行中或结果不明的上游任务，不能接管")
            previous = {
                "status": actor.status,
                "paused": actor.paused,
                "thread_id": actor.thread_id,
                "turn_id": actor.turn_id,
                "history_turn_id": actor.history_turn_id,
                "context_epoch": actor.context_epoch,
                "last_error": actor.last_error,
                "no_progress_count": actor.no_progress_count,
                "recovery_attempts": actor.recovery_attempts,
            }
            if actor.lease_owner and actor.lease_until <= time.time():
                actor.lease_owner = None
                actor.lease_until = 0
            if request.action == "reset_context":
                require(actor.thread_id is not None, "角色尚无模型会话，无需重建上下文")
                actor.turn_id = None
                actor.thread_id = None
                actor.history_turn_id = None
                from .director_efficiency import invalidate_context

                invalidate_context(project, actor, reason="external_operator_takeover")
                session = actor.sessions.get(actor.session_key)
                if session:
                    session.thread_id = None
                    session.history_turn_id = None
                    session.image_receipts.clear()
                    session.viewed_media_ids.clear()
                    session.image_count = 0
            else:
                # A failed provider turn may remain as historical metadata after its
                # lease is released.  It must be detached before scheduling a new turn.
                actor.turn_id = None
            actor.paused = False
            actor.status = "ready"
            actor.request_wake()
            actor.recovery_not_before = 0
            actor.last_error = None
            actor.summary = "外部操作员已接管并恢复生产；继续读取持久化任务与失败历史"
            add_item(
                project,
                actor.id,
                "tool",
                "外部操作员接管生产角色",
                {
                    "source": "external_operator_takeover",
                    "acting_as_actor_id": actor.id,
                    "reviewer_id": request.reviewer_id,
                    "authorization_id": request.authorization_id,
                    "reason": request.reason,
                    "action": request.action,
                    "previous": previous,
                    "preserved_job_ids": [
                        job.id for job in select_entities(project.jobs, actor_id=actor.id)
                    ],
                },
            )
            return {"actor_id": actor.id, "action": request.action}

        return self.store.mutate(
            project_id,
            apply,
            expected_revision=request.expected_revision,
            key=f"external-takeover:{request.reviewer_id}:{request.idempotency_key}",
            fingerprint_data=[
                "external_operator_takeover",
                actor_id,
                request.model_dump(exclude={"expected_revision", "idempotency_key"}, mode="json"),
            ],
            actor_id=actor_id,
            kind="external_operator_takeover",
        )

    def review_candidate_external(
        self,
        project_id: str,
        candidate_id: str,
        request: ExternalCandidateReviewRequest,
    ) -> StudioProject:
        """Apply a quality decision backed only by exact external review receipts."""

        def apply(project: StudioProject) -> dict[str, Any]:
            require(not project.deleted_at, "项目已删除")
            candidate = project.candidates.get(candidate_id)
            require(candidate is not None, "候选不存在")
            assert candidate is not None
            require(candidate.status == "review" and candidate.analysis, "先完成候选视频分析")
            clip = project.clips[candidate.clip_id]
            production_set = project.sets[clip.set_id]
            actor = project.actors[production_set.actor_id]
            required_media = list(
                dict.fromkeys(candidate.analysis.get("required_review_media_ids", []))
            )
            end_frame = candidate.analysis.get("end_frame_media_id")
            if end_frame and end_frame not in required_media:
                required_media.append(end_frame)
            require(required_media, "候选没有可供外部审核的实际画面证据")
            require(request.external_review_ids, "外部候选审核必须引用独立素材审核收据")
            for review_id in request.external_review_ids:
                receipt = project.external_reviews.get(review_id)
                require(receipt is not None, "外部素材审核收据不存在")
                assert receipt is not None
                require(not receipt.revoked_at, "外部素材审核收据已撤销")
                require(
                    receipt.reviewer_id == request.reviewer_id
                    and receipt.authorization_id == request.authorization_id,
                    "外部候选审核与素材收据的审核人或授权不一致",
                )
                require(
                    receipt.scope.candidate_id == candidate.id
                    and "candidate_evidence" in receipt.scope.purposes,
                    "外部素材审核收据不属于当前候选质量审核范围",
                )
                for item in receipt.items:
                    media = project.media.get(item.media_id)
                    require(
                        media is not None and media.sha256 == item.source_sha256,
                        "外部素材审核收据对应的内容已变化",
                    )
            from .external_review import media_is_external_approved

            approved = {
                media_id
                for media_id in required_media
                if media_is_external_approved(
                    project,
                    media_id,
                    "candidate_evidence",
                    actor_id=actor.id,
                    clip_id=clip.id,
                    candidate_id=candidate.id,
                    receipt_ids=request.external_review_ids,
                )
            }
            require(
                set(required_media) <= approved,
                "外部候选审核必须逐张覆盖当前候选的全部必需画面",
            )
            payload = request.model_dump(mode="json")
            data = dict(payload.pop("review_data", {}) or {})
            for name in (
                "decision",
                "findings",
                "evidence_media_ids",
                "limitations",
                "batch_assessments",
                "coverage_gaps",
                "selection_assessment",
                "event_observations",
            ):
                if name in payload:
                    data[name] = payload[name]
            data.update(
                {
                    "candidate_id": candidate.id,
                    "reason": request.reason,
                    "_external_operator": {
                        "external_review_ids": list(request.external_review_ids),
                        "reviewer_id": request.reviewer_id,
                        "authorization_id": request.authorization_id,
                        "inspected_media_ids": required_media,
                    },
                }
            )
            self._review_candidate(project, actor, data)
            add_item(
                project,
                actor.id,
                "tool",
                "外部操作员已完成候选质量裁决",
                {
                    "source": "external_operator_candidate_review",
                    "candidate_id": candidate.id,
                    "decision": request.decision,
                    "external_review_ids": request.external_review_ids,
                    "reviewer_id": request.reviewer_id,
                    "authorization_id": request.authorization_id,
                },
            )
            return {"candidate_id": candidate.id, "decision": request.decision}

        return self.store.mutate(
            project_id,
            apply,
            expected_revision=request.expected_revision,
            key=f"external-candidate-review:{request.reviewer_id}:{request.idempotency_key}",
            fingerprint_data=[
                "external_candidate_review",
                candidate_id,
                request.model_dump(exclude={"expected_revision", "idempotency_key"}, mode="json"),
            ],
            kind="external_candidate_reviewed",
        )

    def configure_preparation(
        self, project_id: str, clip_id: str, request: ClipPreparationUpdate
    ) -> StudioProject:
        def apply(project: StudioProject) -> dict[str, Any]:
            from .preparations import frontier

            require((not project.deleted_at), "项目不可配置预准备")
            clip = project.clips.get(clip_id)
            require(
                clip
                and clip.status in {"planned", "repair", "escalated", "blocked"}
                and not clip.archived_at,
                "已提交或已归档的生成记录不可修改",
            )
            assert clip is not None
            require(
                clip.id in {c.id for c in frontier(project, clip.set_id)},
                "只配置当前或紧邻的下一条 Clip",
            )
            old = clip.preparation
            require(
                request.expected_preparation_id == (old.id if old else None),
                "预准备版本已变化，请刷新后修改",
            )
            if request.prepare_next_clip is not None:
                project.config.prepare_next_clip = request.prepare_next_clip
            if old:
                clip.preparation_history.append(old.model_copy(deep=True))
            clip.preparation = ClipPreparation(
                actor_id=project.sets[clip.set_id].actor_id,
                parent_id=old.id if old else None,
                revision=old.revision + 1 if old else 1,
                enabled=request.enabled,
                user_disabled=not request.enabled,
                status="stale" if request.enabled else "disabled",
                prompt_draft=request.prompt_draft,
                action_notes=request.action_notes,
                bindings=old.bindings if old else [],
                audio_references=old.audio_references if old else [],
                pending_requirements=old.pending_requirements if old else [],
                reason="用户调整预准备配置；需片场导演按最新状态核对",
                invalidation_reasons=["用户调整配置，待导演核对"] if request.enabled else [],
            )
            director = project.actors[clip.preparation.actor_id]
            director.request_wake()
            if not director.paused and director.status in {"idle", "waiting"}:
                director.status = "ready"
            add_item(
                project,
                director.id,
                "user",
                clip.preparation.reason,
                {"clip_id": clip.id, "preparation_id": clip.preparation.id},
            )
            return {"clip_id": clip.id, "preparation_id": clip.preparation.id}

        return self.store.mutate(
            project_id,
            apply,
            expected_revision=request.expected_revision,
            key="user:" + request.idempotency_key,
            fingerprint_data=[
                "configure_preparation",
                clip_id,
                request.model_dump(exclude={"expected_revision", "idempotency_key"}),
            ],
            kind="preparation_configured",
        )

    def execute(
        self,
        project_id: str,
        actor_id: str,
        command: ToolCommand,
        *,
        lease_generation: int | None = None,
        lease_owner: str | None = None,
        native_transport: bool = False,
    ) -> StudioProject:
        delivery_timings: list[dict[str, Any]] = []

        if not native_transport and self.store.actor_has_pending_image_delivery(
            project_id, actor_id, exclude_records_only=True
        ):
            from .image_input_recovery import acknowledge_pending_deliveries

            def acknowledge(current: StudioProject) -> None:
                from .delegation import DELEGATE_ROLES, records_only, task_for

                actor = get_actor(current, actor_id)
                if actor.role not in DELEGATE_ROLES or not records_only(task_for(current, actor)):
                    acknowledge_pending_deliveries(current, actor)

            self.store.mutate(
                project_id,
                acknowledge,
                actor_id=actor_id,
                kind="image_delivery_acknowledged",
                advance_revision=False,
            )

        def apply(project: StudioProject) -> dict[str, Any]:
            if project.deleted_at:
                raise StudioError("项目已删除", 410)
            actor = get_actor(project, actor_id)
            from .delegation import DELEGATE_ROLES, records_only, task_for
            from .delegation import guard as delegate_guard
            from .mcp_inputs import ROLE_ACTIONS

            executive_only = command.action in ROLE_ACTIONS["executive"] and not any(
                command.action in actions
                for owner, actions in ROLE_ACTIONS.items()
                if owner != "executive"
            )
            require(
                command.action in ROLE_ACTIONS[actor.role],
                "该角色没有此操作权限，只有总导演可以执行"
                if executive_only
                else "该角色没有此操作权限",
            )
            if (
                not native_transport
                and actor.pending_image_deliveries
                and not (actor.role in DELEGATE_ROLES and records_only(task_for(project, actor)))
            ):
                from .image_input_recovery import acknowledge_pending_deliveries

                acknowledge_pending_deliveries(project, actor)
            if actor.role in DELEGATE_ROLES:
                delegate_guard(project, actor, command.action, command.payload)
            if actor.role == "visual_analyst":
                require(
                    command.action in {"complete_visual_analysis", "set_status"},
                    "看图子对话只能分析与交回证据，不能选片、生成或修改资产",
                )
                if command.action == "set_status":
                    require(
                        command.payload.get("status") != "complete",
                        "请用 complete_visual_analysis 提交有证据的结论",
                    )
            from .delegation import authorization_payload
            from .visibility import Visibility

            Visibility(project, actor_id).sources(
                authorization_payload(project, actor, command.action, command.payload)
            )
            read_receipt = None
            if command.read_token:
                from stateweaver.studio.context_cache import (
                    validate_job_wait_token,
                    validate_read_token,
                )

                waiting_job = (
                    project.jobs.get(str(command.payload.get("job_id")))
                    if command.action == "set_status"
                    and command.payload.get("status") == "waiting"
                    and command.payload.get("job_id")
                    else None
                )
                if waiting_job and waiting_job.actor_id == actor.id:
                    read_receipt = validate_job_wait_token(
                        project, actor, command.read_token, waiting_job.id
                    )
                else:
                    read_receipt = validate_read_token(project, actor, command.read_token)
            else:
                require(command.expected_revision is not None, "请提供读取凭据或项目版本")
            if lease_generation is not None:
                require(
                    actor.status == "running"
                    and actor.lease_generation == lease_generation
                    and actor.lease_owner == lease_owner
                    and lease_owner is not None
                    and actor.lease_until > time.time(),
                    "MCP 执行租约已失效",
                )
                parent = (
                    project.actors.get(actor.parent_actor_id) if actor.parent_actor_id else None
                )
                require(not (parent and parent.paused), "父角色已暂停，禁止发起新操作")
            require(not actor.paused, "角色已暂停，禁止发起新操作")
            require(actor.status != "complete", "角色任务已经完成")
            from .review_sessions import guard_action

            guard_action(project, actor, command.action, command.payload)
            if command.action in {
                "generate_reference",
                "select_reference",
                "review_reference",
                "resolve_quality",
            }:
                role(actor, "executive", "material_executor")
                if command.action == "resolve_quality":
                    role(actor, "executive")
                target = command.payload.get("clip_id") or command.payload.get("state_id")
                if not target and command.payload.get("revision_id") in project.references:
                    target = project.references[command.payload["revision_id"]].state_id
                if target:
                    require_context(
                        project,
                        actor,
                        command.payload,
                        "review"
                        if command.action in {"review_reference", "resolve_quality"}
                        else "references",
                        str(target),
                    )
            handler = getattr(self, "_" + command.action)
            outcome = (
                self._set_status(project, actor, command.payload, read_receipt=read_receipt)
                if command.action == "set_status"
                else handler(project, actor, command.payload)
            )
            from .delegation import record_outputs

            if command.action not in {
                "set_status",
                "write_memory",
                "save_executive_document",
                "acknowledge_executive_inbox",
            }:
                actor.production_blocker = None
            record_outputs(project, actor, outcome)
            if command.action in {"review_reference", "select_reference"}:
                from .production_efficiency import deliver_material

                delivery_timings.extend(deliver_material(project, actor, outcome, self.store))
            if command.action in {
                "prepare_sets",
                "plan_clips",
                "update_clip_context",
                "repair_clip",
                "set_duration_budget",
                "review_candidate",
                "resolve_quality",
            }:
                from .playback import sync_adjustment

                affected_set_ids = (
                    [str(actor.set_id)]
                    if actor.set_id
                    else [str(command.payload["set_id"])]
                    if command.payload.get("set_id")
                    else [project.clips[str(command.payload["clip_id"])].set_id]
                    if command.payload.get("clip_id")
                    else list(project.sets)
                )
                for identifier in affected_set_ids:
                    sync_adjustment(project, project.sets[identifier], actor)
            bookkeeping = {
                "set_status",
                "write_memory",
                "save_reference_plan",
                "patch_reference_plan",
                "save_executive_document",
                "acknowledge_executive_inbox",
            }
            if command.action not in bookkeeping and not (
                isinstance(outcome, dict) and outcome.get("no_progress")
            ):
                meaningful = {
                    k: v
                    for k, v in command.payload.items()
                    if k not in {"context_token", "reason", "expected_plan_id", "memory_revision"}
                }
                marker = digest([command.action, meaningful])
                # An actual new generation or analysis is progress; repeating a
                # status/plan/review description is not an endless-loop escape.
                if (
                    command.action in {"generate_clip", "generate_reference", "analyze_candidate"}
                    or marker not in actor.progress_markers
                ):
                    actor.progress_version += 1
                    if marker not in actor.progress_markers:
                        actor.progress_markers.append(marker)
            kind: Literal["asset_change", "memory_change", "tool"] = (
                "asset_change"
                if command.action
                in {
                    "create_asset",
                    "create_state",
                    "correct_state_scope",
                    "record_state_evolution",
                    "generate_reference",
                    "select_reference",
                    "review_reference",
                    "establish_voice",
                    "select_voice",
                    "plan_voice",
                    "approve_asset_observation",
                    "assess_revision",
                }
                else "memory_change"
                if command.action
                in {"write_memory", "correct_memory_scope", "resolve_proposal", "propose_global"}
                else "tool"
            )
            item_id = add_item(project, actor_id, kind, command.action, command.payload)
            project.transcript[item_id].activity = {
                "action": command.action,
                **(outcome if isinstance(outcome, dict) else {}),
            }
            return {
                "action": command.action,
                "item_id": item_id,
                "item": project.transcript[item_id].model_dump(mode="json"),
            }

        # Revision is intentionally excluded from receipt fingerprint so an identical
        # retried mutation can return its committed outcome after refreshing context.
        result = self.store.mutate(
            project_id,
            apply,
            expected_revision=None if command.read_token else command.expected_revision,
            key=actor_id + ":" + command.idempotency_key,
            fingerprint_data=[
                command.action,
                {k: v for k, v in command.payload.items() if k != "context_token"},
            ],
            actor_id=actor_id,
            kind="transcript.upsert",
        )
        for timing in delivery_timings:
            self.store.metrics.record(project_id, **timing)
        return result

    def _save_screenplay(self, p: StudioProject, a: Actor, data: dict[str, Any]) -> None:
        role(a, "screenwriter")
        require(p.status == "screenwriting", "已确认剧本不可修改")
        draft = ScreenplayDraft.model_validate(data)
        text = render_screenplay(draft)
        p.screenplay.manuscript = draft.manuscript
        p.screenplay.text = text
        p.screenplay.facts = draft.facts
        a.summary = "Screenplay saved; waiting for confirmation or revision requests."

    def _write_memory(self, p: StudioProject, a: Actor, data: dict[str, Any]) -> dict[str, Any]:
        return write_story_memory(p, a, data)

    def _correct_memory_scope(
        self, p: StudioProject, a: Actor, data: dict[str, Any]
    ) -> dict[str, Any]:
        from .memory import correct_memory_scope

        role(a, "executive")
        require_context(p, a, data, "planning", "global")
        return correct_memory_scope(p, a, data)

    def _plan_story_events(
        self, p: StudioProject, a: Actor, data: dict[str, Any]
    ) -> dict[str, Any]:
        role(a, "executive")
        require_context(p, a, data, "planning", "global")
        return plan_events(p, a, data)

    def _save_reference_plan(
        self, p: StudioProject, a: Actor, data: dict[str, Any]
    ) -> dict[str, Any]:
        return save_reference_plan(p, a, data)

    def _finalize_reference_plan(
        self, p: StudioProject, a: Actor, data: dict[str, Any]
    ) -> dict[str, Any]:
        return finalize_reference_plan(p, a, data, self.store)

    def _patch_reference_plan(
        self, p: StudioProject, a: Actor, data: dict[str, Any]
    ) -> dict[str, Any]:
        from .reference_plans import patch_reference_plan

        return patch_reference_plan(p, a, data)

    def _update_clip_context(
        self, p: StudioProject, a: Actor, data: dict[str, Any]
    ) -> dict[str, Any]:
        role(a, "executive", "set_director")
        clip = p.clips.get(str(data.get("clip_id")))
        require(clip and not clip.archived_at, "Clip 不存在或已归档")
        assert clip is not None
        require(
            a.role == "executive" or clip.set_id == a.set_id, f"不能修改其他{unit_name(p)}的计划"
        )
        require_context(p, a, data, "planning", clip.id)
        require(data.get("reason"), "调整 Clip 状态和事件计划需记录依据")
        from .identity_bootstrap import approve as approve_identity_bootstrap
        from .identity_bootstrap import guard_continuity

        guard_continuity(clip, data)
        if clip.state_readiness == "needs_verification" and data.get("state_readiness") == "ready":
            require(
                a.role == "executive" and data.get("plan_sources"),
                "待核对状态由总导演基于证据或临时制作决定落实",
            )
        require(
            not clip.accepted_candidate_id,
            "已选 Clip 的历史计划保持原样，偏差请记录作用于后续 Clip 的决定",
        )
        require(
            not select_entities(p.candidates, parent_id=clip.id, statuses=["generating"]),
            "已创建任务保留原计划，请等待回收结果",
        )
        if data.get("duration") is not None:
            from .quality_progress import lineage

            require(
                not any(parent.candidate_ids for parent in lineage(p, clip)),
                "已有生成历史的 Clip 请通过 repair_clip 调整时长，保留修正计数",
            )
            duration = float(data["duration"])
            require(5 <= duration <= 15, "Clip 播放时长须为 5–15 秒")
            if duration != clip.duration:
                clip.plan_change_history.append(
                    {
                        "field": "duration",
                        "before": clip.duration,
                        "after": duration,
                        "reason": str(data["reason"]),
                        "actor_id": a.id,
                        "at": now(),
                    }
                )
                clip.duration = duration
        for field in (
            "story_time",
            "story_end_time",
            "story_event_ids",
            "requirement_ids",
            "state_notes",
            "state_readiness",
            "continuity",
        ):
            if field in data:
                setattr(clip, field, data[field])
        if "event_execution" in data:
            clip.event_execution = [
                EventExecution.model_validate(x) for x in data["event_execution"]
            ]
        for field in ("starts_at", "ends_at"):
            if field in data:
                setattr(
                    clip, field, StoryAnchor.model_validate(data[field]) if data[field] else None
                )
        if "asset_plan" in data:
            clip.asset_plan = [ClipAssetPlan.model_validate(x) for x in data["asset_plan"]]
        if "plan_sources" in data:
            clip.plan_sources = [MemorySource.model_validate(x) for x in data["plan_sources"]]
            validate_sources(p, clip.plan_sources)
        validate_clip_plan(p, clip, allow_unknown=True)
        if data.get("identity_bootstrap") is not None:
            approve_identity_bootstrap(p, a, clip, data["identity_bootstrap"])
        return {"clip_id": clip.id, "summary": str(data["reason"])[:500]}

    def _create_asset(self, p: StudioProject, a: Actor, data: dict[str, Any]) -> dict[str, str]:
        role(a, "executive")

        producing = p.status in {"producing", "blocked"}
        require(
            p.status == "preparing" and p.catalog_frozen_at is None or producing, "资产目录已冻结"
        )
        require(
            not any(asset.name == data.get("name") for asset in p.assets.values()), "资产名称重复"
        )
        scope = MemoryScope.model_validate(data.get("published_scope", {}))
        from .memory import validate_scope

        validate_scope(p, scope)
        purpose = data.get("purpose", "story")
        require(
            (True),
            "辅助资产与制作中发布仅适用于新流程",
        )
        if producing:
            require(
                data.get("reason") and (scope.set_ids or scope.clip_ids),
                "制作中补充资产需说明原因和适用的片场/Clip，不能默认为全局既成事实",
            )
        require(
            all(i in p.sets for i in scope.set_ids)
            and all(i in p.clips and not p.clips[i].archived_at for i in scope.clip_ids),
            "资产发布范围不存在或已归档",
        )
        asset = LogicalAsset(
            name=data["name"],
            kind=data["kind"],
            description=data["description"],
            purpose=purpose,
            published_scope=scope,
            publication_reason=str(data.get("reason", "")),
        )
        asset.required_views = (
            list(data.get("required_views") or ["guidance"])
            if asset.purpose != "story"
            else ["close_up", "full_body", "three_view", "expression_grid"]
            if asset.kind in {"person", "character", "人物", "角色"}
            else ["three_view"]
            if asset.kind in {"object", "prop", "物体", "道具"}
            else list(data.get("required_views", ["establishing"]))
        )
        require(asset.required_views, "资产至少需要一个参考视图")
        p.assets[asset.id] = asset
        for set_id in set(scope.set_ids) | {p.clips[i].set_id for i in scope.clip_ids}:
            production_set = p.sets[set_id]
            if asset.id not in production_set.asset_ids:
                production_set.asset_ids.append(asset.id)
            director = p.actors[production_set.actor_id]
            if not director.native_session_blocked:
                director.request_wake()
        return {"asset_id": asset.id, "summary": asset.description[:500]}

    def _add_asset_views(self, p: StudioProject, a: Actor, data: dict[str, Any]) -> dict[str, Any]:

        role(a, "executive")
        asset = p.assets.get(str(data.get("asset_id")))
        require(asset and data.get("reason"), "需指定资产并说明补充视角的用途")
        assert asset is not None
        views = list(dict.fromkeys(data.get("views", [])))
        require(views and all(isinstance(v, str) and v.strip() for v in views), "视角名称不能为空")
        added = [v for v in views if v not in asset.required_views]
        asset.required_views.extend(added)
        return {
            "asset_id": asset.id,
            "added_views": added,
            "no_progress": not added,
            "summary": data["reason"],
        }

    def _create_state(self, p: StudioProject, a: Actor, data: dict[str, Any]) -> dict[str, Any]:
        from .state_references import validate_reference_purpose

        role(a, "executive")
        require(p.status in {"preparing", "producing", "blocked"}, "尚未确认剧本")
        fields = dict(data)
        evolution = fields.pop("evolution", None)
        state = AssetState.model_validate(fields)
        require(not state.evolution_history, "新状态不能预填演变历史")
        require(
            not state.preparation_revision and not state.preparation_history,
            "新状态不能预填准备历史",
        )
        if state.preparation:
            from .set_handoffs import validate_state_preparation

            validate_state_preparation(p, state, state.preparation)
            state.preparation_revision = 1
        for anchor in (state.starts_at, state.ends_at):
            if anchor:
                anchor_key(p, anchor)
        if state.starts_at and state.ends_at:
            require(
                anchor_key(p, state.starts_at) <= anchor_key(p, state.ends_at),
                "状态结束事件不能早于开始事件",
            )
        require(state.asset_id in p.assets, "资产不存在")
        from .state_applicability import validate_excluded_events

        validate_excluded_events(p, state)
        validate_reference_purpose(p, a, state)
        require(state.id not in p.states, "资产状态 ID 已存在，不能覆盖历史")
        asset = p.assets[state.asset_id]
        require(
            not any(
                s.starts_at == state.starts_at
                for s in select_entities(p.states, parent_id=asset.id, story_time=state.story_time)
            )
            if state.starts_at
            else not select_entities(
                p.states, parent_id=asset.id, story_time=state.story_time, limit=1
            ),
            "此故事时间已存在资产状态；修正图片应创建该状态的修订",
        )
        require(
            not state.revision_ids and not state.selected_revisions and not state.repair_episodes,
            "新状态不能预填图片修订或修复额度",
        )
        p.states[state.id] = state
        asset.state_ids.append(state.id)
        p.global_memory.asset_state_ids.append(state.id)
        p.global_memory.revision += 1
        if evolution is not None:
            from .state_evolution import record_state_evolution

            record_state_evolution(p, a, state.id, StateEvolutionInput.model_validate(evolution), 0)
        return {
            "asset_id": asset.id,
            "state_id": state.id,
            "summary": state.description[:500],
            "evolution_recorded": state.evolution is not None,
        }

    def _record_state_evolution(
        self, p: StudioProject, a: Actor, data: dict[str, Any]
    ) -> dict[str, Any]:
        from .state_evolution import record_state_evolution

        role(a, "executive")
        state_id = str(data["state_id"])
        require_context(p, a, data, "planning", state_id)
        return record_state_evolution(
            p,
            a,
            state_id,
            StateEvolutionInput.model_validate(data["evolution"]),
            data["expected_evolution_revision"],
        )

    def _correct_state_scope(
        self, p: StudioProject, a: Actor, data: dict[str, Any]
    ) -> dict[str, Any]:
        role(a, "executive")
        state = p.states.get(str(data.get("state_id")))
        require(state, "资产状态不存在")
        assert state is not None
        require_context(p, a, data, "planning", state.id)
        require(str(data.get("reason", "")).strip(), "纠正状态事件边界需要具体依据和理由")
        start = StoryAnchor.model_validate(data["starts_at"]) if data.get("starts_at") else None
        end = StoryAnchor.model_validate(data["ends_at"]) if data.get("ends_at") else None
        for anchor in (start, end):
            if anchor:
                anchor_key(p, anchor)
        require(
            not (start and end) or anchor_key(p, start) <= anchor_key(p, end),
            "状态结束事件不能早于开始事件",
        )
        intended = state.model_copy(deep=False)
        intended.starts_at, intended.ends_at = start, end
        if data.get("excluded_event_ids") is not None:
            intended.excluded_event_ids = data["excluded_event_ids"]
        from .state_applicability import protect_submitted_boundaries, validate_excluded_events

        validate_excluded_events(p, intended)
        protect_submitted_boundaries(p, state, intended)
        require(
            (start, end, set(intended.excluded_event_ids))
            != (state.starts_at, state.ends_at, set(state.excluded_event_ids)),
            "状态事件边界未改变，无需修订",
        )
        require(
            not start
            or not any(
                other.id != state.id and other.starts_at == start
                for other in select_entities(p.states, parent_id=state.asset_id)
            ),
            "同一资产已有相同起始事件的状态，请核对状态划分",
        )

        def boundary(
            first: StoryAnchor | None, last: StoryAnchor | None, excluded: list[str]
        ) -> dict[str, Any]:
            result: dict[str, Any] = {
                "starts_at": first.model_dump(mode="json") if first else None,
                "ends_at": last.model_dump(mode="json") if last else None,
            }
            if excluded:
                result["excluded_event_ids"] = list(excluded)
            return result

        def label(anchor: StoryAnchor | None, missing: str) -> str:
            if not anchor:
                return missing
            return p.story_events[anchor.event_id].title + (
                "前" if anchor.position == "before" else "后"
            )

        previous = boundary(state.starts_at, state.ends_at, state.excluded_event_ids)
        previous_label = (
            label(state.starts_at, "未限定开始") + " → " + label(state.ends_at, "未限定结束")
        )
        state.starts_at, state.ends_at = start, end
        state.excluded_event_ids = list(intended.excluded_event_ids)
        before = p.global_memory.revision
        p.global_memory.revision += 1
        return {
            "scope": "global",
            "asset_id": state.asset_id,
            "state_id": state.id,
            "before_scope": previous,
            "after_scope": boundary(start, end, state.excluded_event_ids),
            "memory_revision_before": before,
            "memory_revision_after": p.global_memory.revision,
            "summary": (
                f"{state.label}：{previous_label}；"
                f"纠正为{label(start, '未限定开始')} → {label(end, '未限定结束')}。"
                + (
                    "排除完整事件："
                    + "、".join(p.story_events[i].title for i in state.excluded_event_ids)
                    + "。"
                    if state.excluded_event_ids
                    else ""
                )
                + str(data["reason"])
            ),
            "reason": data["reason"],
        }

    def _generate_reference(
        self, p: StudioProject, a: Actor, data: dict[str, Any]
    ) -> dict[str, Any]:
        from .delegation import explicit_observation_source, material_authority

        material_authority(p, a, "generate_reference", data)
        state_id = str(data["state_id"])
        require(state_id in p.states, "资产状态不存在")
        source_policy = data.get("source_policy", "auto")
        require(source_policy in {"auto", "explicit"}, "图片参考来源策略无效")
        if source_policy == "explicit":
            require(str(data.get("reason", "")).strip(), "显式参考来源需要说明选择理由")
        parent = data.get("parent_id")
        if parent:
            require(
                parent in p.references and p.references[parent].state_id == state_id,
                "修订父节点必须属于相同剧情状态",
            )
            require(p.references[parent].media_id, "父修订没有可用参考图")
        sources = list(data.get("source_revision_ids", []))
        state = p.states[state_id]
        from .set_handoffs import require_state_source

        require_state_source(p, state, list(data.get("source_observation_ids", [])))
        if source_policy == "auto" and not sources and not parent:
            previous_states = [
                s
                for s in select_entities(p.states, parent_id=state.asset_id)
                if s.asset_id == state.asset_id
                and s.story_time < state.story_time
                and s.selected_revisions
            ]
            if previous_states:
                previous = max(previous_states, key=lambda s: s.story_time)
                sources = [
                    previous.selected_revisions.get("close_up")
                    or next(iter(previous.selected_revisions.values()))
                ]
        for source in sources:
            require(
                source in p.references
                and p.references[source].media_id
                and p.references[source].status in {"candidate", "selected"}
                and p.references[source].media_id in p.media
                and p.media[str(p.references[source].media_id)].mime_type.startswith("image/"),
                "参考来源必须是本项目中已生成的可用图片修订",
            )
        revision = ReferenceRevision(
            source_revision_ids=sources,
            state_id=state_id,
            parent_id=parent,
            view=data["view"],
            prompt=data["prompt"],
            reason=data.get("reason", ""),
        )
        require(
            revision.view in p.assets[p.states[state_id].asset_id].required_views,
            "此视图不属于已定义的资产视图",
        )
        instructions = ReferenceImageInstructions.model_validate(
            {key: data[key] for key in ReferenceImageInstructions.model_fields if key in data}
        )
        asset = p.assets[state.asset_id]
        sources_media: list[str] = []
        source_media_inputs: list[dict[str, Any]] = []
        observation_ids = list(data.get("source_observation_ids", []))
        repositories = p.__dict__.get("_repositories")
        observations = (
            repositories["memory_entries"].select(ids=observation_ids, parent_id="global")
            if repositories and observation_ids
            else [e for e in p.global_memory.story if e.id in observation_ids]
            if observation_ids
            else []
        )
        require(len(observations) == len(set(observation_ids)), "引用的局部观察不存在")
        for observation in observations:
            require(
                observation.approved_by
                and observation.status == "effective"
                and observation.approved_attributes
                and (
                    (
                        observation.approved_asset_id == asset.id
                        and state_id in observation.approved_state_ids
                    )
                    or explicit_observation_source(p, a, observation, data)
                ),
                "截图观察必须已由总导演确认适用于当前资产或明确授权的来源资产、状态和局部属性",
            )
            sources_media.extend(observation.evidence_media_ids)
            for identifier in observation.evidence_media_ids:
                media = p.media[identifier]
                source_media_inputs.append(
                    {
                        "media_id": media.id,
                        "sha256": media.sha256,
                        "observation_id": observation.id,
                        "approved_scope": observation.text,
                        "source_candidate_id": observation.source_candidate_id,
                        "approved_asset_id": observation.approved_asset_id,
                        "approved_state_ids": observation.approved_state_ids,
                        "approved_attributes": observation.approved_attributes,
                        "source_seconds": observation.source_seconds,
                        "purpose": observation.approval_purpose,
                    }
                )
        revision.source_media_ids = list(dict.fromkeys(sources_media))
        revision.source_observation_ids = observation_ids
        episode = image_episode(p, state_id, revision.view)
        same_view = [
            r for r in select_entities(p.references, parent_id=state_id) if r.view == revision.view
        ]
        require(
            not any(
                r.status == "pending"
                or (
                    r.job_id
                    and (old_job := p.jobs.get(r.job_id))
                    and old_job.status in {"queued", "submitting", "running", "ambiguous"}
                )
                for r in same_view
            ),
            "同一状态视图仍在生成或提交状态不明，请先回收或核对原请求，不能重复提交",
        )
        correcting = any(r.media_id for r in same_view)
        last_reference = max(same_view, key=lambda r: (r.created_at, r.id)) if same_view else None
        technical_retry = bool(
            last_reference
            and last_reference.status == "failed"
            and last_reference.job_id
            and p.jobs[last_reference.job_id].status == "failed"
        )
        plan = (
            RepairPlan.model_validate(data.get("repair_plan", {}))
            if data.get("repair_plan")
            else (episode.plan.model_copy(deep=True) if episode.plan else RepairPlan())
        )
        plan.change = instructions.change or plan.change or str(data.get("reason", ""))
        plan.preserve = instructions.preserve or plan.preserve
        plan.source_revision_id = str(parent) if parent else plan.source_revision_id
        if correcting:
            require(
                plan.defects and plan.preserve and plan.change,
                "定向图片修正需要列出 defects、preserve 和 change，保留已确认的正确内容",
            )
            if not plan.source_revision_id:
                plan.source_revision_id = (
                    state.selected_revisions.get(revision.view)
                    or max(
                        (r for r in same_view if r.media_id), key=lambda r: (r.created_at, r.id)
                    ).id
                )
            if plan.source_revision_id:
                source = p.references.get(plan.source_revision_id)
                require(
                    source and source.state_id == state.id and source.view == revision.view,
                    "图片修正来源必须属于相同状态和视图",
                )
                if not parent:
                    parent = plan.source_revision_id
                    revision.parent_id = parent
            if technical_retry:
                attempt_kind = "technical"
            elif episode.extra_used < episode.extra_granted:
                episode.extra_used += 1
                episode.status = "intervening"
                attempt_kind = "executive"
            elif (
                episode.status not in {"escalated", "intervening"}
                and episode.ordinary_attempts < p.config.repairs_per_reference
            ):
                episode.ordinary_attempts += 1
                attempt_kind = "ordinary"
            else:
                changed = escalate_image(
                    p, a, state.id, revision.view, plan.change or "参考图修正次数已用尽"
                )
                return {
                    "state_id": state.id,
                    "view": revision.view,
                    "escalated": True,
                    "no_progress": not changed,
                }
            revision.repair_plan = plan
            instructions.change = plan.change
            instructions.preserve = plan.preserve
        else:
            attempt_kind = "initial"
        job_sources = [*sources_media]
        anchor_id = parent
        if source_policy == "auto" and not anchor_id:
            for status in ("selected", "candidate", "pending"):
                candidates = select_entities(
                    p.references, parent_id=state.id, statuses=[status], limit=1
                )
                if candidates:
                    anchor_id = candidates[0].id
                    break
        job = Job(
            actor_id=a.id,
            kind="image",
            target_id=revision.id,
            request={
                "prompt": revision.prompt,
                "quality_attempt_kind": attempt_kind,
                "parent_id": parent,
                "view": revision.view,
                "source_policy": source_policy,
                "source_revision_ids": sources,
                "source_media_ids": job_sources,
                "source_media_inputs": source_media_inputs,
                "source_observation_ids": observation_ids,
                "repair_plan": revision.repair_plan.model_dump(mode="json")
                if revision.repair_plan
                else None,
                "anchor_revision_id": anchor_id,
                "image_recipe": image_recipe_snapshot(
                    revision.view, asset_kind=asset.kind, orientation=instructions.orientation
                ),
                "image_instructions": instructions.model_dump(mode="json"),
                "state_preparation": state.preparation.model_dump(mode="json")
                if state.preparation
                else None,
                "state_preparation_revision": state.preparation_revision,
                "asset_description": (
                    f"{asset.name}. {asset.description}. "
                    f"State: {state.label}. {state.description}. "
                ),
            },
        )
        if technical_retry and last_reference and last_reference.job_id:
            previous_job = p.jobs[last_reference.job_id]
            job.request["retry_of_job_id"] = previous_job.id
            authorization = previous_job.image_recovery_authorization
            if authorization:
                require(
                    not authorization.get("replacement_job_id")
                    and authorization.get("max_attempts") == 1,
                    "本次明确授权的旧图片新尝试已登记，请回收该新任务，不能再次消耗授权",
                )
                authorization["replacement_job_id"] = job.id
                authorization["replacement_reference_id"] = revision.id
                authorization["used_at"] = now()
                job.request["unknown_submission_recovery"] = {
                    "original_job_id": previous_job.id,
                    "remote_execution": "unknown",
                    "authorization_id": authorization.get("id"),
                }
        revision.job_id = job.id
        p.references[revision.id] = revision
        p.states[state_id].revision_ids.append(revision.id)
        p.jobs[job.id] = job
        return {
            "reference_id": revision.id,
            "state_id": state_id,
            "asset_id": state.asset_id,
            "parent_id": parent,
            "view": revision.view,
        }

    def _select_reference(self, p: StudioProject, a: Actor, data: dict[str, Any]) -> dict[str, Any]:
        from .delegation import material_authority
        from .image_input_recovery import inspected_evidence
        from .visual_analysis import delegation_receipts

        material_authority(p, a, "select_reference", data)
        require(data.get("revision_id") in p.references, "图片修订不存在")
        revision = p.references[data["revision_id"]]
        require(
            revision.media_id and revision.status in {"candidate", "selected"}, "参考图尚不可用"
        )
        job = p.jobs.get(revision.job_id) if revision.job_id else None
        if job and "quality_attempt_kind" in job.request:
            require(
                revision.media_id in inspected_evidence(p, a, [str(revision.media_id)], self.store),
                "总导演或授权看图子对话需要先用 read_media 查看实际参考图",
            )
            require(
                data.get("reason") or revision.selection,
                "请记录参考图选用依据，或使用 review_reference 完成验收",
            )
        state = p.states[revision.state_id]
        old_id = state.selected_revisions.get(revision.view)
        if old_id and old_id != revision.id:
            p.references[old_id].status = "candidate"
            impacted = [c.id for c in select_entities(p.candidates, binding_revision=old_id)]
            if impacted:
                p.issues.append(f"revision_assessment:{old_id}:{revision.id}")
                wake_executive(p, "参考图已修正，请检查旧修订的实际使用记录并按需修复")
        state.selected_revisions[revision.view] = revision.id
        revision.status = "selected"
        if revision.selection is None:
            revision.selection = SelectionRecord(
                kind="quality_passed",
                actor_id=a.id,
                reason=str(data.get("reason") or revision.reason or "总导演查看后选用"),
                delegated_task_ids=delegation_receipts(p, a, [str(revision.media_id)], self.store),
            )
        episode = state.repair_episodes.get(revision.view)
        if episode:
            episode.status = "resolved"
        issue = f"quality:reference:{state.id}:{revision.view}"
        if issue in p.issues:
            p.issues.remove(issue)
        return {
            "before_reference_id": old_id,
            "reference_id": revision.id,
            "state_id": state.id,
            "asset_id": state.asset_id,
            "view": revision.view,
        }

    def _review_reference(self, p: StudioProject, a: Actor, data: dict[str, Any]) -> dict[str, Any]:
        from .delegation import material_authority
        from .image_input_recovery import inspected_evidence
        from .visual_analysis import delegation_receipts

        material_authority(p, a, "review_reference", data)
        reference = p.references.get(str(data.get("revision_id")))
        require(reference and reference.media_id, "参考图尚未生成")
        assert reference is not None
        require(
            data.get("decision") in {"accept", "reject"} and data.get("reason"),
            "图片检查需要结论及具体理由",
        )
        require(
            reference.media_id in inspected_evidence(p, a, [str(reference.media_id)], self.store),
            "总导演或授权看图子对话需要先用 read_media 查看实际参考图",
        )
        plan = RepairPlan.model_validate(data.get("repair_plan", {}))
        reference.review_history.append(
            {
                "actor_id": a.id,
                "decision": data["decision"],
                "reason": data["reason"],
                "repair_plan": plan.model_dump(),
                "created_at": now(),
                "delegated_task_ids": delegation_receipts(
                    p, a, [str(reference.media_id)], self.store
                ),
            }
        )
        episode = image_episode(p, reference.state_id, reference.view)
        if data["decision"] == "accept":
            if reference.status == "rejected":
                from .image_input_recovery import quarantine

                assert reference.media_id is not None
                require(
                    not quarantine(p, reference.media_id),
                    "已隔离的图片不能通过重新验收选用",
                )
                # A fresh inspected verdict may supersede an earlier rejection.
                # Keep its review history, provenance and spent attempts; the
                # command transaction rolls this transition back if selection fails.
                reference.status = "candidate"
            return self._select_reference(
                p, a, {"revision_id": reference.id, "reason": data["reason"]}
            )
        require(
            plan.defects and plan.preserve and plan.change,
            "图片修正需要列出 defects、preserve 和 change，保留已经正确的内容",
        )
        plan.source_revision_id = reference.id
        reference.status = "rejected"
        reference.repair_plan = plan
        episode.plan = plan
        if (
            ordinary_image_corrections_complete(p, reference.state_id, reference.view)
            and episode.extra_used >= episode.extra_granted
        ):
            escalate_image(p, a, reference.state_id, reference.view, str(data["reason"]))
        return {
            "reference_id": reference.id,
            "summary": data["reason"],
            "defects": plan.defects,
            "preserve": plan.preserve,
        }

    def _resolve_quality(self, p: StudioProject, a: Actor, data: dict[str, Any]) -> dict[str, Any]:
        return resolve_quality(self, p, a, data)

    def _confirm_candidate_state(
        self, p: StudioProject, a: Actor, data: dict[str, Any]
    ) -> dict[str, Any]:
        require_context(p, a, data, "review", str(data.get("candidate_id")))
        return confirm_candidate_state(p, a, data, self.store)

    def _plan_voice(self, p: StudioProject, a: Actor, data: dict[str, Any]) -> dict[str, Any]:
        return plan_voice(p, a, data)

    def _establish_voice(self, p: StudioProject, a: Actor, data: dict[str, Any]) -> dict[str, Any]:
        return establish_voice(p, a, data)

    def _select_voice(self, p: StudioProject, a: Actor, data: dict[str, Any]) -> dict[str, Any]:
        return select_voice(p, a, data)

    def _approve_asset_observation(
        self, p: StudioProject, a: Actor, data: dict[str, Any]
    ) -> dict[str, Any]:
        return approve_asset_observation(self, p, a, data)

    def _prepare_sets(self, p: StudioProject, a: Actor, data: dict[str, Any]) -> None:
        role(a, "executive")
        require_context(p, a, data, "planning", "global")
        require(
            p.status == "preparing" and p.catalog_frozen_at is None, f"{unit_name(p)}边界已冻结"
        )
        require(not p.sets, f"{unit_name(p)}已经建立，不能重复建立目录")
        raw_sets = data["sets"]
        require(raw_sets, f"至少需要一个{unit_name(p)}")
        for index, raw in enumerate(raw_sets):
            set_id = str(raw.get("id") or new_id())
            require(set_id not in p.sets, f"{unit_name(p)} ID 重复")
            actor = Actor(
                role="set_director",
                set_id=set_id,
                status="idle",
                wake_requested=False,
                goal=f"Produce and review all consecutive Clips in Set {raw['title']}.",
            )
            from .contracts import SetDurationBudget

            budget = (
                SetDurationBudget.model_validate(raw["duration_budget"])
                if raw.get("duration_budget")
                else None
            )
            production_set = ProductionSet(
                duration_budget=budget,
                duration_budget_revision=1 if budget else 0,
                duration_history=[
                    {
                        "budget": budget.model_dump(mode="json"),
                        "actor_id": a.id,
                        "at": now(),
                        "reason": budget.basis,
                    }
                ]
                if budget
                else [],
                id=set_id,
                title=raw["title"],
                description=raw["description"],
                order=index,
                story_time=raw["story_time"],
                depends_on=raw.get("depends_on", []),
                actor_id=actor.id,
                story_goal=raw.get("story_goal", ""),
                entry_state=raw.get("entry_state", ""),
                exit_state=raw.get("exit_state", ""),
                asset_ids=list(raw.get("asset_ids", [])),
                story_event_ids=list(raw.get("story_event_ids", [])),
                asset_handoffs=[
                    AssetHandoff(**AssetHandoffPlan.model_validate(item).model_dump())
                    for item in raw.get("asset_handoffs", [])
                ],
                dependency_revision=1 if raw.get("asset_handoffs") else 0,
            )
            require(
                production_set.story_goal.strip()
                and production_set.entry_state.strip()
                and production_set.exit_state.strip(),
                "请先交付片场的 story_goal、entry_state 和 exit_state，再开始分片规划",
            )
            require(all(i in p.assets for i in production_set.asset_ids), "片场发布的资产不存在")
            require(
                all(i in p.story_events for i in production_set.story_event_ids),
                "片场剧情事件不存在",
            )
            production_set.planning_ready_at = now()
            actor.status = "ready"
            actor.request_wake()
            actor.goal = (
                "Complete planning, generation and self-review for Set "
                f"{production_set.title}; "
                "preserve time/space continuity; Clips may contain cuts. "
                "Refer cross-state changes to the executive."
            )
            p.actors[actor.id] = actor
            p.sets[production_set.id] = production_set
        visited: set[str] = set()
        visiting: set[str] = set()

        def visit(set_id: str) -> None:
            require(set_id in p.sets, f"{unit_name(p)}依赖不存在")
            require(set_id not in visiting, f"{unit_name(p)}依赖有环")
            if set_id in visited:
                return
            visiting.add(set_id)
            for dependency in p.sets[set_id].depends_on:
                visit(dependency)
            visiting.remove(set_id)
            visited.add(set_id)

        for set_id in p.sets:
            visit(set_id)
        from .set_handoffs import validate_graph

        validate_graph(p)

    def _set_duration_budget(
        self, p: StudioProject, a: Actor, data: dict[str, Any]
    ) -> dict[str, Any]:
        from .contracts import SetDurationBudget
        from .playback import accounted_duration

        role(a, "executive")
        require_context(p, a, data, "planning", "global")
        production_set = p.sets.get(str(data.get("set_id")))
        require(
            production_set is not None and production_set.status != "complete", "片场不存在或已完成"
        )
        assert production_set is not None
        require(
            data.get("expected_budget_revision") == production_set.duration_budget_revision,
            "Set 时长预算版本已变化，请重新读取",
        )
        reason = str(data.get("reason", "")).strip()
        require(reason, "总导演调整时长预算需说明剧情和自然节奏依据")
        budget = SetDurationBudget.model_validate(data["budget"])
        # Already submitted or selected footage cannot be silently reduced by a budget edit.
        committed = 0.0
        for clip in select_entities(p.clips, ids=production_set.clip_ids):
            if clip.archived_at:
                continue
            candidates = select_entities(
                p.candidates, parent_id=clip.id, statuses=["generating", "review"]
            )
            if clip.accepted_candidate_id or candidates:
                committed += accounted_duration(p, clip)
        require(
            budget.maximum_seconds + 0.001 >= committed,
            f"已提交或已选片已占 {committed:g} 秒；预算调整不能缩短原始提交或已选片",
        )
        previous = production_set.duration_budget
        if previous == budget:
            return {
                "set_id": production_set.id,
                "budget_revision": production_set.duration_budget_revision,
                "no_progress": True,
            }
        production_set.duration_history.append(
            {
                "before": previous.model_dump(mode="json") if previous else None,
                "after": budget.model_dump(mode="json"),
                "actor_id": a.id,
                "reason": reason,
                "at": now(),
                "revision": production_set.duration_budget_revision + 1,
                "screenplay_hash": p.screenplay.confirmed_hash,
            }
        )
        production_set.duration_budget = budget
        production_set.duration_budget_revision += 1
        director = p.actors[production_set.actor_id]
        director.request_wake()
        if not director.paused and director.status in {"idle", "waiting"}:
            director.status = "ready"
        add_item(
            p,
            director.id,
            "status",
            (
                f"总导演已调整本 Set 时长：目标 {budget.target_seconds:g} 秒，"
                f"区间 {budget.minimum_seconds:g}–{budget.maximum_seconds:g} 秒。{reason}"
            ),
            {
                "set_id": production_set.id,
                "budget_revision": production_set.duration_budget_revision,
            },
        )
        return {
            "set_id": production_set.id,
            "budget_revision": production_set.duration_budget_revision,
        }

    def _plan_clips(self, p: StudioProject, a: Actor, data: dict[str, Any]) -> dict[str, Any]:
        from .repairs import (
            inherit_replan_attempt,
            replan_repair_count,
            replan_reserves_correction,
            transfer_replanned_reservations,
        )

        production_set = set_for(p, a)
        mode = data.get("mode", "replace_unselected_suffix")
        require(
            mode in {"replace_unselected_suffix", "append_unplanned_suffix"}, "未知 Clip 规划方式"
        )
        appending = mode == "append_unplanned_suffix"
        require(
            (can_plan_set(p, production_set)),
            ("请等待总导演交付片场剧情目标和起终状态"),
        )
        require_context(p, a, data, "planning", production_set.id)
        require(
            appending
            or not any(
                j.kind == "video"
                and (c := p.candidates.get(j.target_id)) is not None
                and c.clip_id in production_set.clip_ids
                for j in select_entities(
                    p.jobs, statuses=["queued", "submitting", "running", "ambiguous"]
                )
            ),
            "仍有任务进行，暂不能调整 Clip 计划",
        )
        accepted = [p.clips[c] for c in production_set.clip_ids if p.clips[c].accepted_candidate_id]
        require(
            [c.order for c in accepted] == list(range(len(accepted))), "已验收片段必须为连续前缀"
        )
        suffix = (
            []
            if appending
            else [
                p.clips[c] for c in production_set.clip_ids if not p.clips[c].accepted_candidate_id
            ]
        )
        preserved = list(production_set.clip_ids) if appending else [c.id for c in accepted]
        require(
            not any(c.quality_episode and c.quality_episode.status != "resolved" for c in suffix),
            "质量接管期间请修正原 Clip，重排不能重置追加次数",
        )
        has_history = any(clip.candidate_ids for clip in suffix)
        inherited_repairs = max(
            production_set.replan_repair_count, max((c.repair_count for c in suffix), default=0)
        ) + int(replan_reserves_correction(p, suffix))
        parents = [] if appending else [c.id for c in suffix] or production_set.replan_parent_ids
        if not appending:
            production_set.replan_repair_count = inherited_repairs
            production_set.replan_parent_ids = parents
        if has_history:
            require(data.get("reason"), "修改已生成候选的 Clip 计划必须说明原因并保留历史")
            require(
                (True),
                "重排仍受原 Clip 修复次数上限约束",
            )
        retained_parents = {
            identifier
            for raw in data["clips"]
            for identifier in (raw.get("replaces_clip_ids") or parents)
        }
        require(
            all(c.id in retained_parents for c in suffix if c.candidate_ids or c.repair_count),
            "重排不能丢弃已有生成或修正历史的 Clip；请明确 replaces_clip_ids",
        )
        for clip in suffix:
            clip.archived_at = now()
        for asset in p.assets.values():
            if asset.voice_bootstrap_clip_id in {c.id for c in suffix}:
                require(
                    not any(v.status == "preparing" for v in asset.voice_references),
                    "原音色基准正在提取，暂不能重排其 Clip",
                )
                asset.voice_bootstrap_clip_id = None
        production_set.clip_ids = list(preserved)
        replacements: list[Clip] = []
        for raw in data["clips"]:
            explicit_parents = list(raw.get("replaces_clip_ids", []))
            require(
                set(explicit_parents) <= {c.id for c in suffix},
                "replaces_clip_ids 必须属于本次替换的未选用 Clip",
            )
            clip_parents = explicit_parents or parents
            own_repairs = (
                replan_repair_count(p, clip_parents)
                if (clip_parents)
                else 0
                if appending
                else inherited_repairs
            )
            require(
                (own_repairs <= p.config.repairs_per_clip),
                "重排仍受原 Clip 修复次数上限约束，请先完成质量接管",
            )
            clip = Clip(
                set_id=production_set.id,
                order=len(production_set.clip_ids),
                title=raw["title"],
                description=raw["description"],
                duration=raw.get("duration", 5),
                plan_parent_ids=clip_parents,
                repair_count=own_repairs,
                repair_reason=data.get("reason"),
                story_time=raw.get("story_time", production_set.story_time),
                story_end_time=raw.get("story_end_time"),
                asset_plan=[ClipAssetPlan.model_validate(x) for x in raw.get("asset_plan", [])],
                speaking_asset_ids=list(raw.get("speaking_asset_ids", [])),
                continuity=dict(raw.get("continuity", {})),
                story_event_ids=list(raw.get("story_event_ids", [])),
                starts_at=raw.get("starts_at"),
                requirement_ids=list(raw.get("requirement_ids", [])),
                event_execution=[
                    EventExecution.model_validate(x) for x in raw.get("event_execution", [])
                ],
                ends_at=raw.get("ends_at"),
                plan_sources=raw.get("plan_sources", []),
                state_notes=raw.get("state_notes", []),
                state_readiness=raw.get("state_readiness", "ready"),
            )
            require(
                clip.story_end_time is None or clip.story_end_time >= clip_story_time(p, clip),
                "同一连续 Clip 的故事结束时间不能早于开始时间",
            )
            for plan in clip.asset_plan:
                for state_id in (plan.start_state_id, plan.end_state_id):
                    require(
                        state_id is None
                        or (state_id in p.states and p.states[state_id].asset_id == plan.asset_id),
                        "Clip 状态计划引用了错误资产",
                    )
                require(plan.asset_id in p.assets, "Clip 状态计划中的资产不存在")
                require(
                    (True),
                    "Clip 起始状态必须符合故事实际发生时间，倒叙不能引用未来状态",
                )
                if plan.end_state_id and plan.end_state_id != plan.start_state_id:
                    require(
                        (True),
                        "状态变化需指定 story_end_time，并匹配该时刻的结束状态",
                    )
                require(
                    all(v in p.assets[plan.asset_id].required_views for v in plan.required_views),
                    "Clip 所需视图不属于此资产",
                )
            require(
                all(
                    identifier in p.assets and p.assets[identifier].kind in VOICE_ASSET_KINDS
                    for identifier in clip.speaking_asset_ids
                ),
                "发声资产必须是已冻结目录中的人物资产或旁白声音资产",
            )
            require(
                len({plan.asset_id for plan in clip.asset_plan}) == len(clip.asset_plan)
                and len(set(clip.speaking_asset_ids)) == len(clip.speaking_asset_ids),
                "Clip 的资产与发声人物计划不能重复",
            )
            p.clips[clip.id] = clip
            inherit_replan_attempt(p, a, clip, data)
            validate_clip_plan(p, clip, allow_unknown=True)
            validate_sources(p, clip.plan_sources)
            clip.plan_sources.extend(
                MemorySource(kind="decision", id=i, note="explicit_replan_parent")
                for i in explicit_parents
            )
            production_set.clip_ids.append(clip.id)
            replacements.append(clip)
        require(production_set.clip_ids, "至少需要一个 Clip")
        if not appending:
            transfer_replanned_reservations(p, suffix, replacements, str(data.get("reason", "")))
        from .production_contracts import missing_planned_events

        missing_events = missing_planned_events(p, production_set)
        production_set.clip_plan_complete = bool(data.get("complete", True)) and not missing_events
        from .memory_lineage import inherit_memory_scopes

        inheritance = inherit_memory_scopes(p, a, suffix, replacements)
        if inheritance["needs_review"]:
            wake_executive(p, "Clip 重排后部分记忆范围缺少明确对应，请核对已有提案后继续相关 Clip")
        validate_voice_dependencies(p)
        if any(p.clips[c].speaking_asset_ids for c in production_set.clip_ids):
            wake_executive(p, f"{unit_name(p)}已规划发声人物，请安排首个正式 Clip 建立固定音色")
        elif any(p.clips[c].asset_plan for c in production_set.clip_ids):
            wake_executive(
                p, f"{unit_name(p)}已提交 Clip 状态计划，请准备即将使用的视图并预取后续素材"
            )

        if data.get("complete", True) and missing_events:
            wake_executive(
                p,
                "Clip 计划已保存为部分规划；本片场仍缺事件：" + ", ".join(missing_events),
            )
        return {
            "set_id": production_set.id,
            "clip_ids": list(production_set.clip_ids),
            "clip_plan_complete": production_set.clip_plan_complete,
            "missing_story_event_ids": missing_events,
            "plan_semantics": mode,
            "preserved_selected_clip_ids": [clip.id for clip in accepted],
            "preserved_clip_ids": preserved,
            "appended_clip_ids": [clip.id for clip in replacements] if appending else [],
        }

    def _publish_set_context(
        self, p: StudioProject, a: Actor, data: dict[str, Any]
    ) -> dict[str, Any]:
        role(a, "executive")
        require_context(p, a, data, "planning", "global")
        production_set = p.sets.get(str(data.get("set_id")))
        require(
            production_set is not None and production_set.status != "complete",
            "片场不存在或已经完成",
        )
        assert production_set is not None
        require(str(data.get("reason", "")).strip(), "交付或修订片场状态需要说明依据")
        fields = ("story_goal", "entry_state", "exit_state", "asset_ids", "story_event_ids")
        for key in fields[:3]:
            require(str(data.get(key, "")).strip(), f"片场交付缺少 {key}")
        require(all(i in p.assets for i in data["asset_ids"]), "片场发布的资产不存在")
        require(all(i in p.story_events for i in data["story_event_ids"]), "片场发布的事件不存在")
        previous = {key: getattr(production_set, key) for key in fields}
        production_set.handoff_history.append(
            {"before": previous, "reason": data["reason"], "at": now()}
        )
        for key in fields:
            setattr(production_set, key, data[key])
        production_set.planning_ready_at = now()
        director = p.actors[production_set.actor_id]
        director.request_wake()
        if (
            not director.paused
            and not director.native_session_blocked
            and director.status != "running"
        ):
            director.status = "ready"
        return {"set_id": production_set.id, "can_plan": True, "summary": data["reason"]}

    def _freeze_production(self, p: StudioProject, a: Actor, data: dict[str, Any]) -> None:
        role(a, "executive")
        require(p.status == "preparing" and p.sets, f"必须先准备{unit_name(p)}目录")
        require(p.assets, "必须先准备视觉资产目录")
        # Set catalogs publish identities and scope before on-demand reference
        # states. Only the current Clip's actual needs gate video submission.
        p.catalog_frozen_at = now()
        p.status = "producing"
        for actor in p.actors.values():
            if actor.role == "set_director" and not actor.native_session_blocked:
                if actor.status != "running" and not actor.paused:
                    actor.status = "ready"
                actor.request_wake()
        a.summary = f"制作进行中，持续协调所有{unit_name(p)}、资产修订与全局建议"
        if p.director_mode == "delegated-v1" and not a.production_context_started_at:
            a.production_context_started_at = now()
            a.request_wake()

    def _configure_set_dependencies(
        self, p: StudioProject, a: Actor, data: dict[str, Any]
    ) -> dict[str, Any]:
        from .handoff_actions import configure_dependencies

        return configure_dependencies(p, a, data)

    def _configure_state_preparation(
        self, p: StudioProject, a: Actor, data: dict[str, Any]
    ) -> dict[str, Any]:
        from .handoff_actions import configure_state

        return configure_state(p, a, data)

    def _confirm_set_handoff(
        self, p: StudioProject, a: Actor, data: dict[str, Any]
    ) -> dict[str, Any]:
        from .handoff_actions import confirm_handoff

        return confirm_handoff(self, p, a, data)

    def _adopt_observed_reference(
        self, p: StudioProject, a: Actor, data: dict[str, Any]
    ) -> dict[str, Any]:
        from .handoff_actions import adopt_reference

        return adopt_reference(self, p, a, data)

    def _propose_global(self, p: StudioProject, a: Actor, data: dict[str, Any]) -> dict[str, Any]:
        result = propose_global(p, a, data)
        wake_executive(p, "片场导演提交了全局修改建议")
        return result

    def _resolve_proposal(self, p: StudioProject, a: Actor, data: dict[str, Any]) -> dict[str, Any]:
        return resolve_proposal(p, a, data)

    def _approve_reference_region(
        self, p: StudioProject, a: Actor, data: dict[str, Any]
    ) -> dict[str, Any]:
        from .reference_regions import approve

        require_context(p, a, data, "references", str(data.get("clip_id")))
        return approve(p, a, data, self.store)

    def _approve_reference_coverage(
        self, p: StudioProject, a: Actor, data: dict[str, Any]
    ) -> dict[str, Any]:
        from .reference_coverage import approve

        role(a, "executive")
        require_context(p, a, data, "references", str(data.get("clip_id")))
        return approve(p, a, data, self.store)

    def _prepare_reference(
        self, p: StudioProject, a: Actor, data: dict[str, Any]
    ) -> dict[str, str]:
        if a.role == "material_executor":
            from .delegation import guard, task_for

            guard(p, a, "prepare_reference", data)
            request = PrepareReferenceInput.model_validate(data)
            job = preparation_job(p, a, request)
            task = task_for(p, a)
            require(
                request.revision_id in task.request.source_revision_ids
                or request.revision_id in task.output_revision_ids,
                "裁剪来源不在授权修订中",
            )
            require(
                job.request["source"]["media_id"]
                in self.store.inspected(p.id, a.id, [job.request["source"]["media_id"]]),
                "先实际查看原图，再裁剪",
            )
            job.request["alternatives"] = [
                item
                for item in job.request.get("alternatives", [])
                if item["revision_id"] in task.allowed_scope.revision_ids
            ]
            p.jobs[job.id] = job
            return {"job_id": job.id, "reference_id": request.revision_id}
        production_set = set_for(p, a)
        request = PrepareReferenceInput.model_validate(data)
        job = preparation_job(p, a, request)
        source = job.request["source"]
        auxiliary = p.assets[source["asset_id"]].purpose != "story"
        if auxiliary:
            from .materials import auxiliary_states

            require(
                request.clip_id in production_set.clip_ids
                and source["state_id"] in auxiliary_states(p, p.clips[str(request.clip_id)]),
                "辅助素材准备需指定适用的本片场 Clip，不能作为剧情状态跨范围使用",
            )
        story_time = production_set.story_time
        if request.clip_id:
            require(
                request.clip_id in production_set.clip_ids,
                f"参考准备的 Clip 不属于当前{unit_name(p)}",
            )
            clip = p.clips[request.clip_id]
            story_time = clip_story_time(p, clip)
            plan = next((x for x in clip.asset_plan if x.asset_id == source["asset_id"]), None)
            allowed_states = {plan.start_state_id, plan.end_state_id} if plan else set()
        else:
            allowed_states = set()
        if auxiliary:
            pass  # Scoped guidance describes an intended picture, not chronological state.
        elif allowed_states:
            require(
                source["state_id"] in allowed_states, "参考状态不属于 Clip 计划内的起始或目标状态"
            )
        else:
            state = chronological_state(p, source["asset_id"], story_time)
            require(state.id == source["state_id"], "资产状态不符合 Clip 的剧情时间或起止状态计划")
        if request.crop:
            require(
                source["media_id"] in self.store.inspected(p.id, a.id, [source["media_id"]]),
                "请先 read_media 查看原图，再指定单视图裁剪区域",
            )
        p.jobs[job.id] = job
        return {"job_id": job.id, "reference_id": request.revision_id, "set_id": production_set.id}

    def _prepare_media(self, p: StudioProject, a: Actor, data: dict[str, Any]) -> dict[str, str]:
        role(a, "executive", "set_director", "task_analyst", "material_executor")
        if a.role in {"task_analyst", "material_executor"}:
            from .delegation import guard

            guard(p, a, "prepare_media", data)
        job = excerpt_job(p, a, PrepareMediaInput.model_validate(data))
        candidate = p.candidates[job.target_id]
        source = p.media[str(candidate.media_id)]
        if job.request["kind"] == "image":
            from .image_input_recovery import review_gaps

            require(
                not review_gaps(p, candidate),
                "来源候选已有隔离画面，不能重新截帧替代；请使用剩余已有证据并记录未审范围",
            )
        fixed_tail = (
            a.role == "set_director"
            and p.clips[candidate.clip_id].accepted_candidate_id == candidate.id
            and (job.request["kind"] == "video")
            and (source.duration is not None)
            and (
                abs(float(job.request["start_seconds"]) - (source.duration - CONTINUATION_SECONDS))
                < 0.001
            )
            and (job.request["end_seconds"] is not None)
            and (abs(float(job.request["end_seconds"]) - source.duration) < 0.001)
        )
        if fixed_tail:
            existing = p.jobs.get(str(candidate.continuation_reference_job_id))
            if (
                existing is not None
                and existing.status != "failed"
                and existing.request.get("tail_frame_count") == CONTINUATION_FRAMES
            ):
                return {"job_id": existing.id, "candidate_id": candidate.id}
            # An explicit retry replaces only the local preparation pointer. The
            # original failure and selected generation remain in their history.
            candidate.continuation_reference_job_id = job.id
            job.request["automatic_continuation"] = True
            job.request["tail_frame_count"] = CONTINUATION_FRAMES
            job.request["tail_fps"] = CONTINUATION_FPS
            if existing:
                lineage = (
                    "retry_of_job_id" if existing.status == "failed" else "previous_tail_job_id"
                )
                job.request[lineage] = existing.id
        p.jobs[job.id] = job
        return {"job_id": job.id, "candidate_id": job.target_id}

    def _generate_clip(self, p: StudioProject, a: Actor, data: dict[str, Any]) -> dict[str, str]:
        from .set_handoffs import require_handoffs

        production_set = set_for(p, a)
        require(p.catalog_frozen_at and p.status in {"producing", "blocked"}, "尚未开始制作")
        require(data.get("clip_id") in production_set.clip_ids, f"Clip 不属于当前{unit_name(p)}")
        clip = p.clips[data["clip_id"]]
        from .playback import require_submission

        require_submission(p, clip)
        require_handoffs(p, clip)
        require(clip.status == "planned", "Clip 尚未就绪或已有任务")
        require(not waiting_for_voices(p, clip), "此 Clip 的发声人物尚未建立固定音色，请等待总导演")
        previous: Candidate | None = None
        if clip.order:
            predecessor = p.clips[production_set.clip_ids[clip.order - 1]]
            require(predecessor.accepted_candidate_id, "必须先验收前一个 Clip")
            previous = p.candidates[str(predecessor.accepted_candidate_id)]
            require(previous.end_state is not None, "前一 Clip 缺少实际末帧及动作空间状态")
        require(
            not clip_pending_proposals(p, clip), "当前 Clip 的全局修改要求尚未落实，请等待总导演"
        )
        plan = submission_plan(p, a, data, clip)
        if data.get("use_previous_clip_reference") is not None:
            require(
                data["use_previous_clip_reference"] == plan.use_previous_clip_reference,
                "提交的接续选择与定稿清单不一致；请先重新保存清单并核对提示词",
            )
        from .repairs import validate_submission

        repair_attempt = validate_submission(p, clip, plan)
        from .latent import bind_continuation
        from .policy import project_video_http

        video_http = project_video_http(p)
        if video_http is not None:
            require(
                data.get("latent_continuation_mode") in (None, "off"),
                "通用视频 API 模式不支持 H3 latent 接续",
            )
            require(
                not plan.audio_inputs
                or video_http.reference_policy == "text-only"
                or "audio" in video_http.inputs,
                "当前视频 API 未配置音频参考输入。请检查服务配置和本次参考清单。",
            )
        latent = bind_continuation(p, clip, plan, data.get("latent_continuation_mode"))
        bindings = [
            ReferenceBinding(asset_id=b.asset_id, state_id=b.state_id, revision_id=b.revision_id)
            for b in plan.bindings
        ]
        reference_inputs = [dict(x) for x in plan.reference_inputs]
        video_inputs = [dict(x) for x in plan.video_inputs]
        audio_inputs = [dict(x) for x in plan.audio_inputs]
        video_continuation = any(x["role"] == "continuation" for x in video_inputs)
        prompt = plan.prompt
        video_transport = {}
        if video_http is not None:
            text_only = video_http.reference_policy == "text-only"
            video_transport = {
                "video_input_policy": video_http.reference_policy,
                "provider_prompt": text_only_prompt(prompt, language=p.config.production_language)
                if text_only
                else prompt,
            }
        job = Job(
            actor_id=a.id,
            kind="video",
            target_id="",
            request={
                "prompt": prompt,
                **video_transport,
                "reference_plan_id": plan.id,
                "reference_plan_revision": plan.revision,
                "reference_plan_fingerprint": plan.dependency_fingerprint,
                "context_fingerprint": plan.context_fingerprint,
                "story_event_ids": clip.story_event_ids,
                "starts_at": clip.starts_at.model_dump() if clip.starts_at else None,
                "ends_at": clip.ends_at.model_dump() if clip.ends_at else None,
                "prompt_format": (SET_FORMAT),
                "workflow_version": p.workflow_version,
                "harness_version": p.harness_version,
                "h3_skill_sha256": load_media_skill(H3_PROMPT_SKILL).sha256,
                "duration": clip.duration,
                "video_settings": dict(self.video_settings),
                "generation_attempt": len(clip.candidate_ids) + 1,
                "quality_attempt_kind": clip.pending_attempt_kind,
                "repair_count": clip.repair_count,
                "executive_extra_used": clip.quality_episode.extra_used
                if clip.quality_episode
                else 0,
                "bindings": [b.model_dump() for b in bindings],
                "reference_inputs": reference_inputs,
                "video_inputs": video_inputs,
                "audio_inputs": audio_inputs,
                "video_continuation": video_continuation,
                "latent_continuation": latent,
                "asset_plan": [x.model_dump(mode="json") for x in clip.asset_plan],
                "story_time": clip_story_time(p, clip),
                "story_end_time": clip.story_end_time,
                "continuity": clip.continuity,
                "repair_plan": clip.repair_plan.model_dump(mode="json")
                if clip.repair_plan
                else None,
                "previous_end_state": plan.previous_end_state.model_dump()
                if plan.previous_end_state
                else None,
            },
        )
        # H3 defaults to a fixed seed. A repair needs a fresh sample, while

        job.request["continuity_check"] = plan.continuity_check
        job.request["set_director_policy"] = p.set_director_policy
        if plan.continuation_decision:
            job.request["continuation_decision"] = dict(plan.continuation_decision)
        # H3 defaults to a fixed seed. A repair needs a fresh sample, while
        # replay/recovery of this persisted job must retain the same request.
        if plan.inspection_gaps:
            job.request["reference_inspection_gaps"] = [
                gap.model_dump(mode="json") for gap in plan.inspection_gaps
            ]
        job.request["seed"] = int(hashlib.sha256(job.id.encode()).hexdigest()[:8], 16) & 0x7FFFFFFF
        candidate = Candidate(
            clip_id=clip.id,
            job_id=job.id,
            prompt=prompt,
            bindings=bindings,
            previous_candidate_id=previous.id if previous else None,
            repair_plan=clip.repair_plan.model_copy(deep=True) if clip.repair_plan else None,
        )
        if repair_attempt:
            if repair_attempt.kind == "executive" and not repair_attempt.job_ids:
                assert clip.quality_episode is not None
                clip.quality_episode.extra_used += 1
            repair_attempt.job_ids.append(job.id)
            job.request["repair_attempt_id"] = repair_attempt.id
            job.request["quality_attempt_kind"] = repair_attempt.kind
            job.request["executive_extra_used"] = (
                clip.quality_episode.extra_used if clip.quality_episode else 0
            )
            job.request["reinforcement_revision_ids"] = repair_attempt.reinforcement_revision_ids
            job.request["reinforcement_reason"] = repair_attempt.reinforcement_reason
        job.target_id = candidate.id
        p.jobs[job.id] = job
        p.candidates[candidate.id] = candidate
        clip.candidate_ids.append(candidate.id)
        plan.status, plan.job_id = "submitted", job.id
        clip.status = "generating"
        production_set.status = "producing"
        return {
            "job_id": job.id,
            "candidate_id": candidate.id,
            "clip_id": clip.id,
            "set_id": production_set.id,
        }

    def _save_clip_preparation(
        self, p: StudioProject, a: Actor, data: dict[str, Any]
    ) -> dict[str, Any]:
        from .preparations import save_preparation

        return save_preparation(p, a, data)

    def _finish_review_batch(
        self, p: StudioProject, a: Actor, data: dict[str, Any]
    ) -> dict[str, Any]:
        from .review_sessions import finish_batch

        require_context(p, a, data, "review", str(data.get("candidate_id")))
        return finish_batch(p, a, data, self.store)

    def _request_review_batch(
        self, p: StudioProject, a: Actor, data: dict[str, Any]
    ) -> dict[str, Any]:
        from .review_sessions import request_batch

        require_context(p, a, data, "review", str(data.get("candidate_id")))
        return request_batch(p, a, data)

    def _analyze_candidate(
        self, p: StudioProject, a: Actor, data: dict[str, Any]
    ) -> dict[str, str]:
        production_set = set_for(p, a)
        require(data.get("candidate_id") in p.candidates, "候选不存在")
        candidate = p.candidates[data["candidate_id"]]
        require(candidate.clip_id in production_set.clip_ids, f"候选不属于当前{unit_name(p)}")
        require(candidate.media_id and candidate.status == "review", "候选视频尚不可用")
        from .image_input_recovery import review_gaps

        require(
            not review_gaps(p, candidate),
            "候选已有隔离画面，不能重新抽帧替代；请使用剩余已有证据完成有限审核",
        )
        existing = [j for j in select_entities(p.jobs, parent_id=candidate.id, subtype="analysis")]

        if not data.get("focus_intervals"):
            automatic = next(
                (j for j in existing if j.request.get("automatic") and j.status != "failed"), None
            )
            if automatic:
                return {
                    "job_id": automatic.id,
                    "candidate_id": candidate.id,
                    "clip_id": candidate.clip_id,
                    "set_id": production_set.id,
                }
        require(
            not any(j.status in {"queued", "submitting", "running", "ambiguous"} for j in existing),
            "候选正在分析，请等待结果",
        )
        try:
            intervals = [
                SamplingInterval.model_validate(i) for i in data.get("focus_intervals", [])
            ]
        except (ValidationError, TypeError):
            raise StudioError("复查区间需要有效起止秒数及 2–8 fps") from None
        completed = [j for j in existing if j.status == "complete"]
        require(
            not completed or (intervals and str(data.get("instructions", "")).strip()),
            "候选已分析；复查需指定时间段和待核实的问题",
        )
        duration = p.media[candidate.media_id].duration if candidate.media_id in p.media else None
        require(
            not duration
            or all(i.start_seconds < duration and i.end_seconds <= duration for i in intervals),
            "复查时间段超出视频时长",
        )
        prior_intervals = [
            i for j in completed for i in j.request.get("sampling", {}).get("focus_intervals", [])
        ]
        requested = [i.model_dump() for i in intervals]
        require(
            not completed or any(i not in prior_intervals for i in requested),
            "该时间段已完成相同密度的复查，请依据已有证据判断",
        )
        from .review_sampling import review_sampling

        merged_intervals = []
        for interval in [*prior_intervals, *requested]:
            if interval not in merged_intervals:
                merged_intervals.append(interval)
        job = Job(
            actor_id=a.id,
            kind="analysis",
            target_id=candidate.id,
            request={
                "review_mode": "director",
                "sampling": {
                    **review_sampling(p, candidate),
                    "focus_intervals": merged_intervals,
                },
                "prior_analysis_job_ids": [j.id for j in completed],
                "instructions": data.get(
                    "instructions",
                    ("Check identity, action, space, camera continuity and screenplay compliance."),
                ),
            },
        )
        from .review_sessions import evidence_policy

        job.request.update(evidence_policy(p, candidate))
        p.jobs[job.id] = job
        return {
            "job_id": job.id,
            "candidate_id": candidate.id,
            "clip_id": candidate.clip_id,
            "set_id": production_set.id,
        }

    def _confirm_external_end_state(
        self,
        p: StudioProject,
        actor: Actor,
        candidate: Candidate,
        data: dict[str, Any],
        external: dict[str, Any],
    ) -> None:
        """Confirm the actual end frame from externally approved, hash-bound evidence."""

        supplied = data.get("final_end_state")
        if supplied is None:
            require(
                candidate.confirmed_end_state is not None,
                "外部审核需提交 final_end_state，或沿用已确认的实际末态",
            )
            return
        frame_id = candidate.analysis.get("end_frame_media_id")
        require(frame_id and candidate.media_id, "候选缺少可播放视频与实际末帧")
        state = EndState.model_validate(supplied)
        require(state.frame_media_id == frame_id, "外部末态确认必须保留该候选的实际末帧")
        require(
            all(
                value.strip() for value in (state.action, state.space, state.camera, state.identity)
            ),
            "实际末态的动作、空间、机位和人物描述不能为空",
        )
        reason = str(data.get("end_state_reason") or data.get("reason") or "").strip()
        require(reason, "外部末态确认需要记录具体画面依据")
        evidence = list(dict.fromkeys(data.get("end_state_media_ids") or [frame_id]))
        inspected = set(external["inspected_media_ids"])
        require(
            frame_id in evidence and set(evidence) <= inspected,
            "外部末态依据必须包含实际末帧，且每张图均有当前哈希的外部审核收据",
        )
        visual_ids = list(dict.fromkeys(data.get("visible_state_ids", [])))
        clip = p.clips[candidate.clip_id]
        allowed_states = {binding.state_id for binding in candidate.bindings}
        source_job = p.jobs.get(candidate.job_id)
        plans = source_job.request.get("asset_plan", []) if source_job else []
        for plan in plans:
            allowed_states.update(
                value for value in (plan.get("start_state_id"), plan.get("end_state_id")) if value
            )
        allowed_states.update(
            value
            for plan in clip.asset_plan
            for value in (plan.start_state_id, plan.end_state_id)
            if value
        )
        allowed_states = {
            identifier
            for identifier in allowed_states
            if identifier in p.states and p.assets[p.states[identifier].asset_id].purpose == "story"
        }
        require(set(visual_ids) <= allowed_states, "只能确认本候选绑定或计划内实际可见的状态")
        assets = [p.states[identifier].asset_id for identifier in visual_ids]
        require(len(assets) == len(set(assets)), "实际末态中每项资产只能确认一个状态")
        from .image_input_recovery import review_gaps

        confirmation = EndStateConfirmation(
            end_state=state,
            actor_id=actor.id,
            reason="外部人工审核：" + reason,
            evidence_media_ids=evidence,
            visual_state_ids=visual_ids,
            analysis_request_id=candidate.analysis.get("request_id"),
            coverage_gaps=review_gaps(p, candidate),
        )
        candidate.confirmed_end_state = confirmation
        candidate.end_state_history.append(confirmation)
        candidate.end_state = state

    def _review_candidate(self, p: StudioProject, a: Actor, data: dict[str, Any]) -> None:
        require(data.get("candidate_id") in p.candidates, "候选不存在")
        candidate = p.candidates[data["candidate_id"]]
        external = data.get("_external_operator")
        context_target = candidate.id
        if external:
            production_set = p.sets[p.clips[candidate.clip_id].set_id]
            require(a.id == production_set.actor_id, "外部候选审核必须归档到负责片场角色")
        elif a.role == "task_analyst":
            from .delegation import guard, task_for

            guard(p, a, "review_candidate", data)
            # The scoped delegate packet includes this candidate's review
            # evidence; its mandatory receipt is owned by the original task.
            context_target = task_for(p, a).id
            production_set = p.sets[p.clips[candidate.clip_id].set_id]
        else:
            production_set = set_for(p, a)
        require(candidate.clip_id in production_set.clip_ids, f"候选不属于当前{unit_name(p)}")
        require(candidate.status == "review" and candidate.analysis, "先完成候选视频分析")
        require(
            candidate.analysis.get("check_version") == SELF_REVIEW,
            "请先准备本候选的自审画面证据",
        )
        if not external:
            require_context(p, a, data, "review", context_target)
        from .review_sessions import require_complete

        require_complete(
            p,
            a,
            candidate,
            data,
            externally_inspected=set(external["inspected_media_ids"]) if external else None,
        )
        require(
            not any(
                j.kind == "analysis"
                and j.target_id == candidate.id
                and j.status in {"queued", "submitting", "running", "ambiguous"}
                for j in select_entities(
                    p.jobs,
                    parent_id=candidate.id,
                    subtype="analysis",
                    statuses=["queued", "submitting", "running", "ambiguous"],
                )
            ),
            "请等待补充分析完成后再验收",
        )
        if external:
            self._confirm_external_end_state(p, a, candidate, data, external)
        else:
            confirm_end_state(p, a, candidate, data, self.store, required=True)
        require(candidate.end_state, "缺少实际末帧和动作空间状态")
        from .image_input_recovery import (
            available_evidence,
            coverage_limitations,
            current_evidence,
            inspected_evidence,
            review_gaps,
        )

        candidate.review_gaps = review_gaps(p, candidate)
        if external:
            required_media = list(dict.fromkeys(external["inspected_media_ids"]))
            inspected = set(required_media)
            pages = {
                page.get("media_id"): page
                for page in candidate.analysis.get("evidence_pages", [])
                if isinstance(page, dict)
            }
            noncurrent = set(candidate.analysis.get("review_reference_media_ids") or []) | set(
                candidate.analysis.get("previous_tail_media_ids") or []
            )
            current_review_evidence = {
                media_id
                for media_id in required_media
                if media_id not in noncurrent
                and pages.get(media_id, {}).get("kind") not in {"reference", "previous_tail"}
            }
        else:
            required_media = available_evidence(p, candidate)
            require(
                not candidate.review_gaps or current_evidence(p, candidate),
                "本候选的检查图片均不可用，不能只凭参考素材或前段画面生成审核结论",
            )
            inspected = inspected_evidence(p, a, required_media, self.store)
            require(
                all(media_id in inspected for media_id in required_media),
                "片场导演需要通过 read_media 查看实际末帧和接缝对照图后再验收",
            )
            current_review_evidence = current_evidence(p, candidate)
        clip = p.clips[candidate.clip_id]
        candidate.observed_asset_states = record_observed_states(p, clip, data)
        require(data.get("decision") in {"accept", "reject"}, "需要接受或拒绝候选")
        require(data.get("reason"), "需要记录验收依据")
        review = CandidateReview(
            actor_id=a.id,
            decision=data["decision"],
            reason=data["reason"],
            mode="external_operator" if external else "director",
            external_review_ids=list(external["external_review_ids"]) if external else [],
            external_reviewer_id=external["reviewer_id"] if external else None,
            external_authorization_id=external["authorization_id"] if external else None,
            findings=(data.get("findings", [])),
            evidence_media_ids=(list(dict.fromkeys(data.get("evidence_media_ids", [])))),
            limitations=(
                list(
                    dict.fromkeys(
                        [
                            *candidate.analysis.get("limitations", []),
                            *(
                                limitation
                                for batch in candidate.review_batches
                                for limitation in batch.limitations
                            ),
                            *data.get("limitations", []),
                            *(
                                [
                                    "原生模型存在图片隔离缺口；本结论由外部操作员按当前内容哈希逐张复核。"
                                ]
                                if external and candidate.review_gaps
                                else coverage_limitations(candidate.review_gaps)
                            ),
                        ]
                    )
                )
            ),
            batch_assessments=data.get("batch_assessments", []),
            coverage_gaps=candidate.review_gaps,
            selection_assessment=data.get("selection_assessment"),
            event_observations=data.get("event_observations", []),
        )

        comparison_assessment = review.selection_assessment
        require(
            comparison_assessment is not None, "Record the evidence-based candidate comparison."
        )
        assert comparison_assessment is not None
        require(
            set(comparison_assessment.evidence_media_ids) <= inspected
            and set(comparison_assessment.evidence_media_ids) <= set(required_media)
            and bool(set(comparison_assessment.evidence_media_ids) & current_review_evidence),
            "Candidate comparison must cite inspected evidence from this candidate.",
        )
        require(
            set(review.evidence_media_ids) & current_review_evidence
            and set(review.evidence_media_ids) <= set(required_media),
            "自审结论必须引用当前候选的实际检查画面",
        )
        duration = p.media[str(candidate.media_id)].duration or 0
        for finding in review.findings:
            require(
                set(finding.evidence_media_ids) <= set(required_media)
                and set(finding.evidence_media_ids) <= inspected,
                "每项问题必须引用已查看的当前检查画面",
            )
            require(
                0 <= finding.start_seconds <= duration
                and (
                    finding.end_seconds is None
                    or finding.start_seconds <= finding.end_seconds <= duration
                ),
                "问题时间范围超出当前候选的视频时长",
            )
        require(
            data["decision"] == "accept" or any(f.severity == "repair" for f in review.findings),
            "拒绝候选需要指出实质缺陷、发生时间及画面证据",
        )
        candidate.issues = list(data.get("issues", []))
        candidate.issues = list(
            dict.fromkeys(
                [
                    *candidate.issues,
                    *(f.description for f in review.findings if f.severity == "repair"),
                ]
            )
        )
        if data["decision"] == "accept":
            if clip.order:
                predecessor = p.clips[production_set.clip_ids[clip.order - 1]]
                require(
                    predecessor.accepted_candidate_id
                    and predecessor.accepted_candidate_id == candidate.previous_candidate_id,
                    "前驱 Clip 已更新，当前候选的接续来源失效，需要明确修复后重新生成",
                )
            require(not candidate.issues, "仍有未解决连续性问题，不能验收")
            candidate.status = "accepted"
        else:
            candidate.status = "rejected"
            if not clip.accepted_candidate_id:
                clip.status = "repair"
                clip.repair_reason = str(data["reason"])
            plan = RepairPlan.model_validate(data.get("repair_plan", {}))
            plan.defects = plan.defects or list(candidate.issues) or [str(data["reason"])]
            plan.change = plan.change or str(data["reason"])
            plan.source_candidate_id = candidate.id
            require(
                plan.preserve,
                "修正需要明确 preserve，列出本次必须保持的正确人物、动作或空间细节",
            )
            clip.repair_plan = plan

            guidance = data.get("repair_guidance", [])
            indices = [item["finding_index"] for item in guidance]
            required_indices = {
                i for i, finding in enumerate(review.findings) if finding.severity == "repair"
            }
            require(
                len(indices) == len(set(indices))
                and set(indices) == required_indices
                and all(
                    item["suggested_change"].strip()
                    and item["next_check"].strip()
                    and all(text.strip() for text in item["preserve"])
                    for item in guidance
                ),
                "每个实质缺陷需提供对应的具体改法、保留项、素材缺口和下版核验项",
            )
            candidate.analysis["repair_guidance"] = guidance
            # The ledger counts actual recorded reviews. Commit the judgment in
            # this transaction before checking the ordinary-repair boundary.
            candidate.review = review
            episode = clip.quality_episode
            if ordinary_clip_corrections_complete(p, clip) and (
                episode is None or episode.extra_used >= episode.extra_granted
            ):
                escalate_clip(p, a, clip, str(data["reason"]))
        candidate.review_history.append(review)
        candidate.review = review
        from .candidate_lifecycle import clip_pending_work, reviewed_selection_plan
        from .production_decisions import apply_selection

        selection_plan = reviewed_selection_plan(p, clip)
        if selection_plan and not clip.accepted_candidate_id:
            apply_selection(p, a, clip, selection_plan, kind="quality_passed")
            wake_executive(
                p, "Candidate review and final selection completed; continue production."
            )
        elif clip_pending_work(p, clip) and not clip.accepted_candidate_id:
            clip.status = "review"
        elif clip.accepted_candidate_id:
            clip.status = "accepted"
        from .production_closure import reconcile

        reconcile(p)
        if not external:
            from .image_input_recovery import complete_image_recovery

            complete_image_recovery(a)

    def _repair_clip(self, p: StudioProject, a: Actor, data: dict[str, Any]) -> dict[str, Any]:
        from .policy import project_video_http
        from .repairs import pending_attempt, register_attempt

        role(a, "executive", "set_director")
        require(data.get("clip_id") in p.clips, "Clip 不存在")
        clip = p.clips[data["clip_id"]]
        require(clip.id in p.sets[clip.set_id].clip_ids, "Clip 已归档，请修改当前活动计划")
        if a.role == "set_director":
            require(clip.set_id == a.set_id, f"Clip 不属于当前{unit_name(p)}")
        existing_attempt = pending_attempt(clip)
        reactivating_reservation = bool(
            clip.status == "escalated"
            and existing_attempt
            and (existing_attempt.kind == "executive")
            and (not existing_attempt.job_ids)
            and clip.quality_episode
            and (clip.quality_episode.extra_used < clip.quality_episode.extra_granted)
        )
        if reactivating_reservation:
            require(a.role == "executive", "只有总导演可恢复已授权但尚未提交的质量接管登记")
        editing_reservation = bool(
            (clip.status == "planned" or reactivating_reservation)
            and existing_attempt
            and (not existing_attempt.job_ids)
        )
        reactivation_only = bool(
            reactivating_reservation
            and existing_attempt
            and clip.repair_plan == existing_attempt.plan
            and not any(
                field in data for field in ("repair_plan", "duration", "description", "title")
            )
        )
        require(
            clip.status in {"accepted", "blocked", "repair", "escalated"} or editing_reservation,
            "Clip 尚不可修复",
        )
        require(
            not any(
                p.jobs[p.candidates[c].job_id].status
                in {"queued", "submitting", "running", "ambiguous"}
                for c in clip.candidate_ids
            ),
            "原任务仍在执行或提交状态不明，禁止重复生成",
        )
        require(data.get("reason"), "必须记录修复原因")
        production_set = p.sets[clip.set_id]
        # Repairing an accepted prefix has downstream continuity consequences: require
        # explicit recorded repair of its successors, never silently relabel them.
        require(
            not any(
                p.clips[c].accepted_candidate_id or p.clips[c].status in {"generating", "review"}
                for c in production_set.clip_ids[clip.order + 1 :]
            ),
            "后续已验收 Clip 依赖此结果；先从末尾逐个记录修复",
        )
        last = p.candidates[clip.candidate_ids[-1]] if clip.candidate_ids else None
        technical = bool(last and not last.media_id and p.jobs[last.job_id].status == "failed")
        if not technical:
            require(
                all(
                    not p.candidates[i].media_id or p.candidates[i].review
                    for i in clip.candidate_ids
                ),
                "先完成已有候选的审核，再根据已看缺陷登记或调整修复",
            )
        previous_repair_count = clip.repair_count
        if editing_reservation:
            pass
        elif technical:
            clip.pending_attempt_kind = "technical"
        elif ordinary_clip_corrections_complete(p, clip):
            episode = clip_episode(p, clip)
            if episode.extra_used >= episode.extra_granted:
                changed = escalate_clip(p, a, clip, str(data["reason"]))
                return {"clip_id": clip.id, "escalated": True, "no_progress": not changed}
            episode.status = "intervening"
            clip.pending_attempt_kind = "executive"
        else:
            # Replanning may have carried an allocated but unrendered correction.
            # Complete that reservation without erasing its historical counter or
            # handing off before the required reviewed results actually exist.
            from .quality_progress import quality_progress

            progress = quality_progress(p, clip)
            require(
                progress.ordinary.generations < p.config.repairs_per_clip,
                "普通修正的实际生成已达上限，但首版或审核账本不一致；"
                "请总导演核对 quality_progress 和原始任务，不能再登记普通修正",
            )
            if clip.repair_count < p.config.repairs_per_clip:
                clip.repair_count += 1
            clip.pending_attempt_kind = "ordinary"
            if clip.quality_episode:
                clip.quality_episode.status = "ordinary"
                issue = "quality:clip:" + clip.id
                if issue in p.issues:
                    p.issues.remove(issue)
        if data.get("repair_plan"):
            clip.repair_plan = RepairPlan.model_validate(data["repair_plan"])
        if clip.repair_plan is None:
            clip.repair_plan = RepairPlan(change=str(data["reason"]), defects=[str(data["reason"])])
        video_http = project_video_http(p)
        require(
            clip.repair_plan.mode != "edit"
            or video_http is None
            or video_http.edit_submit_path is not None,
            "当前视频 API 未配置视频编辑，请选择按缺陷重新生成",
        )
        if clip.repair_plan.source_candidate_id:
            source = p.candidates.get(clip.repair_plan.source_candidate_id)
            require(source and source.clip_id == clip.id, "修正来源必须属于同一个 Clip")
        register_attempt(p, a, clip, reuse=technical or editing_reservation)
        if reactivating_reservation and clip.quality_episode:
            clip.quality_episode.status = "intervening"
        if clip.reference_plan_id and (not reactivation_only):
            old_plan = p.reference_plans.get(clip.reference_plan_id)
            if old_plan and old_plan.status not in {"submitted", "superseded"}:
                old_plan.status = "needs_review"
                old_plan.invalidation_reasons = ["修复方案已更新，需重新核对当次模式、参考与提示词"]
        clip.repair_reason = str(data["reason"])
        if "duration" in data:
            clip.duration = data["duration"]
        if "description" in data:
            clip.description = str(data["description"])
        if "title" in data:
            clip.title = str(data["title"])
        clip.accepted_candidate_id = None
        clip.status = "planned"
        production_set.status = "producing"
        director = p.actors[production_set.actor_id]
        if not director.paused and not director.native_session_blocked:
            director.status = "ready" if director.status != "running" else director.status
            director.request_wake()
        p.final_media_id = None
        if p.status == "complete":
            p.status = "producing"
        return {
            "clip_id": clip.id,
            "summary": data["reason"],
            "attempt_kind": clip.pending_attempt_kind,
            "ordinary_correction_spent": clip.repair_count > previous_repair_count,
            "repair_count": clip.repair_count,
        }

    def _supply_repair_references(
        self, p: StudioProject, a: Actor, data: dict[str, Any]
    ) -> dict[str, Any]:
        from .repairs import supply_references

        require_context(p, a, data, "references", str(data.get("clip_id")))
        return supply_references(p, a, data, self.store)

    def _delegate_visual_analysis(
        self, p: StudioProject, a: Actor, data: dict[str, Any]
    ) -> dict[str, Any]:
        from .visual_analysis import delegate

        return delegate(p, a, data)

    def _delegate_tasks(self, p: StudioProject, a: Actor, data: dict[str, Any]) -> dict[str, Any]:
        from .delegation import delegate

        return delegate(p, a, data)

    def _complete_delegate_task(
        self, p: StudioProject, a: Actor, data: dict[str, Any]
    ) -> dict[str, Any]:
        from .delegation import complete

        require_context(p, a, data, "review", str(data.get("task_id")))
        return complete(p, a, data, self.store)

    def _request_delegate_followup(
        self, p: StudioProject, a: Actor, data: dict[str, Any]
    ) -> dict[str, Any]:
        from .delegation import followup

        return followup(p, a, data, read_receipt=self.provider_receipt_reader)

    def _approve_delegate_results(
        self, p: StudioProject, a: Actor, data: dict[str, Any]
    ) -> dict[str, Any]:
        from .delegation import approve

        return approve(self, p, a, data)

    def _acknowledge_executive_inbox(
        self, p: StudioProject, a: Actor, data: dict[str, Any]
    ) -> dict[str, Any]:
        from .delegation import acknowledge

        return acknowledge(p, a, data)

    def _save_executive_document(
        self, p: StudioProject, a: Actor, data: dict[str, Any]
    ) -> dict[str, Any]:
        from .delegation import save_document

        return save_document(p, a, data)

    def _finish_initial_planning(
        self, p: StudioProject, a: Actor, data: dict[str, Any]
    ) -> dict[str, Any]:
        role(a, "executive")
        from .delegation import enabled

        require(enabled(p), "现有模式保持原会话流程")
        require(str(data.get("reason", "")).strip(), "需记录初始规划发布依据")
        require(
            p.sets and all(s.planning_ready_at for s in p.sets.values()),
            "先发布片场目标、起终状态、事件与资产目录；不必等待图片或 Clip 规划",
        )
        previously_started = a.production_context_started_at is not None
        if not p.catalog_frozen_at:
            self._freeze_production(p, a, data)
        if previously_started:
            return {"phase": "production", "no_progress": True}
        a.production_context_started_at = a.production_context_started_at or now()
        a.request_wake()
        return {"phase": "production", "planning_archived": True}

    def _adopt_prepared_reference(
        self, p: StudioProject, a: Actor, data: dict[str, Any]
    ) -> dict[str, Any]:
        from .delegation import guard, task_for

        guard(p, a, "adopt_prepared_reference", data)
        task = task_for(p, a)
        job = p.jobs.get(str(data.get("job_id")))
        require(
            job
            and job.id in task.output_job_ids
            and job.actor_id == a.id
            and job.kind == "reference"
            and job.status == "complete"
            and job.result.get("media_id")
            and job.request.get("crop"),
            "只可采用本任务已完成并有准确来源的裁剪",
        )
        assert job is not None
        state_id, view = str(data["state_id"]), str(data["view"])
        require(
            state_id == task.request.state_id and view in task.request.views, "裁剪采用目标越界"
        )
        source = p.references[str(job.request["revision_id"])]
        require(source.state_id == state_id, "裁剪不能虚构新剧情状态")
        media_id = str(job.result["media_id"])
        require(
            media_id in self.store.inspected(p.id, a.id, [media_id]), "实际查看裁剪结果后才能采用"
        )
        ref = ReferenceRevision(
            state_id=state_id,
            view=view,
            media_id=media_id,
            status="candidate",
            prompt="本地裁剪采用；未请求图像生成。",
            reason=data["reason"],
            job_id=job.id,
            source_revision_ids=[source.id],
            source_media_ids=[media_id],
        )
        p.references[ref.id] = ref
        p.states[state_id].revision_ids.append(ref.id)
        task.output_revision_ids.append(ref.id)
        return self._select_reference(p, a, {"revision_id": ref.id, "reason": data["reason"]})

    def _complete_visual_analysis(
        self, p: StudioProject, a: Actor, data: dict[str, Any]
    ) -> dict[str, Any]:
        from .visual_analysis import complete

        require_context(p, a, data, "review", str(data.get("task_id")))
        return complete(p, a, data, self.store)

    def _assess_revision(self, p: StudioProject, a: Actor, data: dict[str, Any]) -> None:
        role(a, "executive")
        issue = str(data["issue"])
        require(issue in p.issues and issue.startswith("revision_assessment:"), "修订检查不存在")
        require(data.get("reason"), "需要逐一检查实际使用并记录依据")
        affected = [
            c.id for c in select_entities(p.candidates, binding_revision=issue.split(":")[1])
        ]
        assessments = data.get("assessments", {})
        require(all(c in assessments for c in affected), "需要记录每个已绑定候选的影响判断")
        p.issues.remove(issue)

    def _reconcile_job(self, p: StudioProject, a: Actor, data: dict[str, Any]) -> dict[str, Any]:
        role(a, "executive")
        require(data.get("job_id") in p.jobs, "任务不存在")
        job = p.jobs[data["job_id"]]
        original_status = job.status
        abandon = data.get("resolution") == "abandon_image_result"
        require(
            job.status == "ambiguous" or (abandon and job.status == "failed"),
            "仅提交不明的任务或明确放弃无恢复线索的旧图片失败结果需要对账",
        )
        require(data.get("evidence"), "需要提供原任务核查依据，不能猜测任务 ID")
        if data.get("resolution") in {"failed", "abandon_image_result"}:
            from stateweaver.studio_media.submission_errors import (
                image_rejection_status,
                recorded_image_post_rejection,
            )

            status = (
                image_rejection_status(job.error or "")
                if abandon
                else recorded_image_post_rejection(job.error or "")
            )
            authorization = job.image_recovery_authorization or {}
            scoped_unknown = bool(
                abandon
                and authorization.get("job_id") == job.id
                and authorization.get("authorized_by") == "user"
                and authorization.get("authorized_at")
                and authorization.get("reason")
                and authorization.get("max_attempts") == 1
                and not authorization.get("replacement_job_id")
            )
            require(
                job.kind == "image"
                and not job.provider_job_id
                and (status is not None or scoped_unknown),
                (
                    "确认提交失败需要明确 HTTP 拒绝且已知发生在图片 "
                    "POST；旧记录若无法区分提交和下载阶段，须明确放弃不可找回的结果，不能推断未执行；超时或视频提交仍需原任务对账"
                ),
            )
            require(
                not job.lease_owner and not p.references[job.target_id].media_id,
                "任务仍在处理或已有图片，应先回收已有结果",
            )
            if abandon:
                require(
                    data.get("acknowledge_unretrievable_result") is True and not job.result,
                    "须确认原结果没有可用文件或恢复地址，且远端是否执行仍未知；"
                    "已有结果线索时应先回收，不能再次生成",
                )
            job.status = "failed"
            job.result["submission_failure"] = {
                "kind": "result_unavailable" if abandon else "http_rejected",
                "http_status": status,
                "source": "user_scoped_recovery"
                if scoped_unknown
                else "legacy_http_error"
                if abandon
                else "recorded_post_response",
                "request_phase": "unknown" if abandon else "submission",
                "remote_execution": "unknown" if abandon else "rejected",
                "original_status": original_status,
                "reconciled_by": a.id,
                "evidence": str(data["evidence"]),
                "reconciled_at": now(),
            }
            p.references[job.target_id].status = "failed"
            return {
                "job_id": job.id,
                "summary": (
                    "已明确放弃无法找回的图片结果；远端执行情况仍未知。"
                    if abandon
                    else f"已核对图片 POST 的 HTTP {status} 拒绝应答。"
                )
                + "保留原失败任务，由导演另行决定后续技术请求，不自动重发。",
            }
        require(job.kind == "video" and data.get("provider_job_id"), "需要原始视频服务任务 ID")
        job.provider_job_id = str(data["provider_job_id"])
        job.status = "running"
        job.error = None
        return {"job_id": job.id, "summary": "已关联原视频服务任务，继续回收结果"}

    def _set_status(
        self,
        p: StudioProject,
        a: Actor,
        data: dict[str, Any],
        *,
        read_receipt: ContextRead | None = None,
    ) -> dict[str, Any] | None:
        from stateweaver.studio.context_cache import overview_wake_observed, wake_fingerprint
        from stateweaver.studio.production_runtime import enabled, infer_wait_job_id, register_wait

        require(
            data.get("status") in {"waiting", "blocked"}, "只能主动等待或报告阻塞；完成需通过验收"
        )
        coherent = enabled(p)
        wait_job_id = data.get("job_id")
        if coherent and data["status"] == "waiting":
            require(not a.native_session_blocked, "模型会话技术恢复已耗尽，请通过恢复入口继续")
            wait_job_id = wait_job_id or infer_wait_job_id(p, a)
        unread_wake = a.wake_requested and (
            read_receipt is None
            or read_receipt.target_id != "overview"
            or read_receipt.wake_fingerprint != wake_fingerprint(a)
        )
        if coherent and data["status"] == "waiting" and unread_wake:
            unread_wake = not overview_wake_observed(p, a)
        if coherent and data["status"] == "waiting" and unread_wake and not wait_job_id:
            a.runtime_wait = None
            a.status = "running" if a.status == "running" else "ready"
            a.summary = "New progress is available; read the current project before waiting."
            return {
                "waiting": False,
                "waiting_reason": "unread_notifications",
                "next_action": "get_project",
                "summary": a.summary,
            }
        waiting_job = None
        if data["status"] == "waiting" and wait_job_id:
            waiting_job = p.jobs.get(str(wait_job_id))
            require(waiting_job, "等待需要引用当前项目中已有的任务")
            assert waiting_job is not None
            if coherent:
                require(waiting_job.actor_id == a.id, "A provider wait must name this actor's job.")
            require(not a.native_session_blocked, "模型会话技术恢复已耗尽，请通过恢复入口继续")
            terminal = waiting_job.status in {"complete", "failed", "ambiguous"}
            if terminal or unread_wake:
                # A result or another notification may arrive after the model read
                # its token. Never overwrite that wake-up with an outdated wait.
                a.status = "running" if a.status == "running" else "ready"
                # Preserve the signal; this reconciliation is not a new event.
                a.wake_requested = True
                if coherent:
                    a.runtime_wait = None
                a.summary = (
                    "原任务已有结果，请读取并继续处理"
                    if terminal
                    else "等待期间已有新进展，请读取后继续处理"
                )
                return {
                    "job_id": waiting_job.id,
                    "job_status": waiting_job.status,
                    "detail_scope": f"job:{waiting_job.id}",
                    "waiting": False,
                    "waiting_reason": "job_returned" if terminal else "unread_notifications",
                    "next_action": "get_detail" if terminal else "get_project",
                    "summary": a.summary,
                }
        if (
            data["status"] == "blocked"
            and a.role == "set_director"
            and data.get("category") != "technical"
        ):
            production_set = set_for(p, a)
            unfinished = [
                p.clips[c] for c in production_set.clip_ids if p.clips[c].status != "accepted"
            ]
            if unfinished and any(c.candidate_ids for c in unfinished):
                if coherent:
                    a.runtime_wait = None
                    from .quality import notify_executive, ordinary_clip_corrections_complete

                    clip = unfinished[0]
                    if not ordinary_clip_corrections_complete(p, clip):
                        # Reporting a prerequisite problem must not become a new
                        # validation failure merely because normal repairs remain.
                        # Keep the actual candidate/attempt history and ask the
                        # executive to review/select existing work or supply inputs.
                        notify_executive(p, str(data["summary"]))
                        a.status = "running" if a.status == "running" else "ready"
                        a.request_wake()
                        a.summary = "候选尚需审核或常规修正；总导演已收到接管请求。"
                        return {
                            "waiting": False,
                            "waiting_reason": "ordinary_quality_work_pending",
                            "clip_id": clip.id,
                            "next_action": "review_candidate_or_repair_clip",
                            "executive_notified": True,
                            "summary": a.summary,
                        }
                escalate_clip(p, a, unfinished[0], str(data["summary"]))
                return None
        if data["status"] == "blocked" and data.get("category") == "technical":
            job = p.jobs.get(str(data.get("job_id")))
            native_error = str(a.last_error or "").strip()
            reported_native_error = native_error and native_error in str(data.get("summary", ""))
            require(
                job
                and (job.error or job.status in {"failed", "ambiguous"})
                or reported_native_error,
                "技术阻塞需要引用实际失败或提交不明的任务，或原样引用当前模型会话故障",
            )
        if (
            data["status"] == "blocked"
            and a.role == "executive"
            and data.get("category") != "technical"
            and any(issue.startswith("quality:") for issue in p.issues)
        ):
            raise StudioError("质量接管不能阻塞项目，请有限追加或明确妥协选用可用候选")
        if coherent and data["status"] == "waiting":
            from .production_readiness import has_ready_work
            from .runtime_store import RuntimeStore

            ready: dict[str, Any] = {}
            if has_ready_work(
                p, a, RuntimeStore(self.store), include_lookahead=False, details=ready
            ):
                a.runtime_wait = None
                a.status = "running" if a.status == "running" else "ready"
                a.summary = "Executable work remains; handle it before registering a wait."
                return {
                    "waiting": False,
                    "waiting_reason": "ready_work",
                    "next_action": "get_project",
                    "ready_work": ready,
                    "summary": a.summary,
                }
        a.status = data["status"]
        a.summary = str(data["summary"])
        a.wake_requested = False
        registered = register_wait(p, a, job_id=wait_job_id, reason=a.summary)
        if (
            registered
            and registered.get("waiting")
            and a.production_blocker
            and a.production_blocker.get("action") == "set_status"
        ):
            a.production_blocker = None
        if a.status == "blocked" and a.role in {"task_analyst", "material_executor"}:
            from .delegation import fail

            fail(p, a, a.summary)
        if a.role == "set_director" and a.status == "blocked":
            p.sets[str(a.set_id)].status = "blocked"
            wake_executive(p, a.summary)
        if waiting_job:
            return {
                "job_id": waiting_job.id,
                "job_status": waiting_job.status,
                "detail_scope": f"job:{waiting_job.id}",
                "waiting": registered.get("waiting", True) if registered else True,
                **({"waiting_reason": registered["waiting_reason"]} if registered else {}),
                "summary": a.summary,
            }
        if registered is not None:
            return registered
        return None

    def _finish_set(self, p: StudioProject, a: Actor, data: dict[str, Any]) -> None:
        production_set = set_for(p, a)
        from .candidate_lifecycle import require_set_settled

        require_set_settled(p, production_set)
        from .playback import require_submission

        require(
            production_set.clip_plan_complete,
            "请先完成本 Set 的 Clip 规划，不能把部分规划标记为片场完成",
        )
        if production_set.clip_ids:
            require_submission(p, p.clips[production_set.clip_ids[0]])
        require(
            production_set.clip_ids
            and all(p.clips[c].status == "accepted" for c in production_set.clip_ids),
            f"{unit_name(p)}仍有未验收 Clip",
        )
        production_set.status = "complete"
        a.status = "complete"
        a.wake_requested = False
        a.summary = "全部 Clip 已选用，检查结论和妥协记录已保留"
        wake_executive(p, f"一个{unit_name(p)}已完成，请继续协调制作和成片")
        for downstream in p.sets.values():
            if downstream.status == "planned" and all(
                p.sets[d].status == "complete" for d in downstream.depends_on
            ):
                p.actors[downstream.actor_id].request_wake()
        from .production_closure import reconcile

        reconcile(p)

    def _assemble(self, p: StudioProject, a: Actor, data: dict[str, Any]) -> None:
        role(a, "executive")
        from .candidate_lifecycle import require_project_settled

        require_project_settled(p)
        from .set_handoffs import dependency_report

        for production_set in p.sets.values():
            blocks = dependency_report(p, production_set.id).blockers
            require(not blocks, blocks[0].description if blocks else "")
        require(
            p.sets and all(s.status == "complete" for s in p.sets.values()),
            f"仍有{unit_name(p)}未完成",
        )
        require(
            not p.issues
            and all(
                r.status not in {"pending", "accepted", "in_progress"}
                for r in select_entities(
                    p.proposals, statuses=["pending", "accepted", "in_progress"]
                )
            ),
            "仍有未处理事项",
        )
        require(
            not any(
                j.kind == "assembly" and j.status in {"queued", "submitting", "running"}
                for j in select_entities(
                    p.jobs, statuses=["queued", "submitting", "running", "ambiguous"]
                )
            ),
            "已经有合成任务",
        )
        from .playback import require_submission

        for production_set in p.sets.values():
            require(production_set.clip_plan_complete, "仍有片场规划未完成")
            require_submission(p, p.clips[production_set.clip_ids[0]])
        job = Job(actor_id=a.id, kind="assembly", target_id=p.id)
        p.jobs[job.id] = job

    def _finish_project(self, p: StudioProject, a: Actor, data: dict[str, Any]) -> None:
        role(a, "executive")
        from .assembly import current_final_media_id
        from .candidate_lifecycle import require_project_settled

        require_project_settled(p)
        require(
            p.final_media_id and all(s.status == "complete" for s in p.sets.values()), "成片未完成"
        )
        require(current_final_media_id(p), "当前选片已变化，请重新合成。")
        require(
            not p.issues
            and all(
                r.status not in {"pending", "accepted", "in_progress"}
                for r in select_entities(
                    p.proposals, statuses=["pending", "accepted", "in_progress"]
                )
            ),
            "仍有未处理问题",
        )
        p.status = "complete"
        a.status = "complete"
        a.wake_requested = False
        a.summary = "成片与来源记录已完成"
