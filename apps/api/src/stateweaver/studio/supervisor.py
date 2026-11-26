"""Durable actor leases for StateWeaver roles and asynchronous media jobs.

Models choose business operations through MCP. This supervisor only owns
continuation, scheduling, recovery and business completion boundaries.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from uuid import uuid4

from stateweaver.providers.base import (
    ProviderContractError,
    ProviderJobError,
    ProviderQueryUnavailable,
    safe_provider_text,
)
from stateweaver.providers.coordination import ProviderCapacityWait, ProviderCoordinator
from stateweaver.providers.coordinator_client import CoordinatorUnavailable
from stateweaver.providers.registry import APIRegistry, route_physical_identity
from stateweaver.providers.studio_runtime import ProviderRuntime
from stateweaver.storage.cas import CASIntegrityError, ContentAddressedStore
from stateweaver.studio.continuity import waiting_for_voices
from stateweaver.studio.contracts import (
    Actor,
    Job,
    StudioProject,
    now,
)
from stateweaver.studio.job_executor import (
    JobExecutor,
    JobOutcome,
    ProviderResultPublicationError,
    require_same_video_outcome,
    require_video_outcome,
)
from stateweaver.studio.job_receipts import (
    LOCAL_VIDEO_RECOVERY_GENERATION,
    JobReceipts,
    detached_call,
    request_hash,
)
from stateweaver.studio.proposals import (
    clip_pending_proposals,
)
from stateweaver.studio.quality import (
    escalate_clip,
    ordinary_clip_corrections_complete,
    ordinary_image_corrections_complete,
    resolve_quality,
)
from stateweaver.studio.repository import select_entities
from stateweaver.studio.review_context_recovery import (
    is_context_overflow,
    queue_recovery,
    recovery_record,
    review_session,
)
from stateweaver.studio.review_sessions import (
    activate_session,
    assignment,
    handoff_completed_lookahead,
    prepare_batches,
    queue_evidence,
)
from stateweaver.studio.runtime_store import RuntimeStore
from stateweaver.studio.service import StudioService, add_item, require, wake_executive
from stateweaver.studio.settings import StudioSettings
from stateweaver.studio.store import StudioError, StudioStore
from stateweaver.studio.voice import apply_voice_result
from stateweaver.studio_media import load_media_config
from stateweaver.studio_media.evidence import MediaDecoderTimeout, MediaDecoderUnavailable

logger = logging.getLogger(__name__)


def native_goal_objective(
    actor: Actor, reminder: str = "", project: StudioProject | None = None
) -> str:
    """Keep native goal metadata bounded; the complete durable goal stays in MCP."""
    from .task_context import full_context_required

    strict_context = bool(project and full_context_required(project))
    context_instruction = (
        "This project uses strict full-context reading: complete every page of the "
        "current required task packet before production decisions, then use its final "
        "context_token as read_token. "
        if strict_context
        else "Full task-text reading is optional, not a prerequisite for every action. "
        "This current optional policy supersedes generic full-text prerequisites in "
        "historical Skills, actor goals and operator boilerplate. Specific story "
        "requirements and targeted original-text checks still apply. "
        "Use a fresh scoped read_token from get_project. "
        "Read relevant details or get_required_context when information is missing or "
        "uncertain; do not mistake an overview or incomplete packet for full text. "
        "Preserve actor/session/version/dependency fences and every required image "
        "inspection, quarantine, quality, source and state check. "
    )
    if actor.role == "screenwriter":
        context_instruction = ""
    session = actor.sessions.get(actor.session_key)
    review_instructions = (
        "Read actor_instructions in get_project or the first review work packet. "
        "Its exact persisted goal includes the latest operator instructions. "
        "If detail_request is present, call get_detail with those arguments and read "
        f"the complete project.actors[{actor.id}].goal, including its final instructions. "
    )
    if session and session.purpose == "planning" and session.planning_kind:
        target = session.planning_clip_id or actor.set_id
        task = (
            "Prepare only editable stable next-Clip draft; do not bind an unselected "
            "predecessor or register/finalize/submit video."
            if session.planning_kind == "lookahead"
            else "Finish this Set from selected Clip records; do not repeat decomposition."
            if session.key.endswith(":finish")
            else "Confirm the already covered Set plan with "
            "plan_clips(mode=append_unplanned_suffix, clips=[], complete=true). "
            "Read current event coverage and playback budget first. Preserve ALL existing "
            "Clips, submitted and unknown requests, selected footage and quality allowances. "
            "Do not replan, invent events or submit video in this confirmation round. "
            "Missing images or earlier filming waits do not block this planning confirmation."
            if session.planning_round == "confirmation"
            else "Reconcile the covered Set plan with its approved playback interval. "
            "Plan genuine remaining source actions at natural pacing when more time is needed; "
            "otherwise ask the executive to resolve the exact budget or unsubmitted suffix. "
            "Do not pad, replay actions, invent events or use an empty confirmation while "
            "duration is outside the approved interval. Preserve ALL existing Clips, "
            "submitted and unknown requests, selected footage and quality allowances."
            if session.planning_round == "duration"
            else "Complete the unplanned suffix under the executive handoff with "
            "plan_clips(mode=append_unplanned_suffix). Retain ALL existing Clips, "
            "original events, submitted receipts, actual predecessor endpoint and "
            "recorded total duration; do not replay the selected opening or replace "
            "any existing Clip. Missing "
            "images, earlier generation and quality intervention block only the "
            "affected filming step, not this structure plan. Bind actual continuity "
            "when its selected predecessor is available."
            if session.planning_round == "suffix"
            else "Decompose this Set once into Clips under the executive handoff."
            if session.planning_kind == "set"
            else "Plan only this Clip's current logical generation/repair round. "
            "Reuse its repair reservation and submitted receipts; technical retries, "
            "prompt edits and effort changes do not create another round."
        )
        return (
            f"Planning target {target}; round {session.planning_round or 'set'}. {task} "
            "First call the advertised canvas get_project tool directly with {}. "
            "Do not use resources/list or resources/templates/list to discover tools. "
            f'Next call canvas get_detail(scope="actor:{actor.id}") and read the '
            f"complete project.actors[{actor.id}].goal, including its final operator "
            "instructions. Then read the durable planning assignment/checkpoint. "
            + context_instruction
            + "Reload authoritative business "
            "records, not transcript summaries. Preserve scoped memory, material "
            "versions, actual predecessor endpoint, unknowns and quality allowances. "
            "Review remains in the candidate's separate single review conversation. " + reminder
        )
    if session and session.purpose == "candidate_review":
        reading = (
            "Read get_review_work_packet for this candidate's assignment and evidence index. "
            if project is not None
            else "Read get_project for this candidate's assignment and evidence index. "
        ) + context_instruction
        return (
            f"Review only candidate {session.candidate_id} in this one conversation. "
            + reading
            + review_instructions
            + "Inspect every required "
            "image, including actual references and the accepted predecessor's tail/seam, "
            "using read_media or batch_read_media as instructed, without resending images "
            "already inspected in this context. "
            "Follow the assigned image budget; oversized evidence moves to separate review "
            "batches. Keep focused rechecks and the final review_candidate decision in the "
            "assigned context. Confirm the actual end state; do not plan or generate Clips. "
            + reminder
        )
    if session and session.purpose == "review_batch":
        return (
            f"Review only candidate {session.candidate_id}, batch {session.batch_id}. "
            + review_instructions
            + "Read get_project for the current assignment. "
            + context_instruction
            + "Inspect every assigned image, then finish_review_batch with grounded "
            "findings and the actual end state when its end frame is assigned. "
            "The supervisor switches image contexts; do not generate media or plan other Clips. "
            + reminder
        )
    if session and session.purpose == "review_summary":
        return (
            f"Adjudicate candidate {session.candidate_id} from all completed review batches. "
            + review_instructions
            + context_instruction
            + "Read the relevant review records and preserve findings/limitations and actual "
            "end-state evidence. Use review_candidate, or request_review_batch for a precise "
            "unresolved visual question. This summary context does not load images. " + reminder
        )
    objective = (
        actor.goal
        + ("\n\n" + context_instruction if project and actor.role != "screenwriter" else "")
        + reminder
    )
    if len(objective) <= 4000:
        return objective
    return (
        f"Continue your {actor.role} task for actor {actor.id}. "
        f'First call canvas get_detail(scope="actor:{actor.id}") and read the '
        f"complete project.actors[{actor.id}].goal, including its final instructions. "
        "That persisted goal is authoritative and has not been truncated. "
        + context_instruction
        + "Read current canvas state before acting; preserve "
        "submitted work, history, counters and explicit pauses. Continue until the "
        "business task is complete or an actual dependency requires waiting."
    )


def release_actor_run(
    store: StudioStore,
    project_id: str,
    actor_id: str,
    expected_owner: str,
    expected_generation: int,
    *,
    wait_reason: Callable[[StudioProject, Actor], bool],
) -> StudioProject:
    """Release one finished native run under its original ownership fence.

    Callers establish that the run has exited. This does not complete a native
    turn, reset context, resolve business blockers or replay provider requests.
    """

    def release(project: StudioProject) -> None:
        state = project.actors[actor_id]
        if state.lease_owner == expected_owner and state.lease_generation == expected_generation:
            state.lease_owner = None
            state.lease_until = 0
            state.turn_id = None
            if state.paused:
                state.status = "paused"
            elif state.status == "running" or (
                state.status == "waiting" and not state.paused and not wait_reason(project, state)
            ):
                state.status = "ready"
                state.request_wake()

    return store.mutate(
        project_id,
        release,
        actor_id=actor_id,
        kind="actor_released",
        advance_revision=False,
    )


class ActorSupervisor:
    def __init__(
        self,
        service: StudioService,
        cas: ContentAddressedStore,
        settings: StudioSettings,
        secret: bytes,
        *,
        job_executor: Any = None,
        api_registry: APIRegistry | None = None,
        api_coordinator: ProviderCoordinator | None = None,
        provider_config: dict[str, str] | None = None,
    ) -> None:
        self.service = service
        self.store = service.store
        self.runtime = RuntimeStore(self.store)
        self.cas = cas
        self.settings = settings
        self.secret = secret
        self.role_runner: Callable[[str, str], Awaitable[None]] | None = None
        self.provider_config = load_media_config() if provider_config is None else provider_config
        self.api_registry = (
            api_registry
            or service.api_registry
            or APIRegistry.load(self.provider_config, settings.provider_registry_path)
        )
        service.api_registry = self.api_registry
        self.service.video_settings = {
            "short_edge": int(self.provider_config.get("H3_SHORT_EDGE", "768")),
            "aspect_ratio": self.provider_config.get("H3_ASPECT_RATIO", "16:9"),
        }
        if job_executor is None:
            job_executor = JobExecutor(
                cas,
                settings.work_root,
                self.provider_config,
                media_scratch_root=settings.media_scratch_root,
                registry=self.api_registry,
                provider_runtime=ProviderRuntime(
                    self.api_registry,
                    api_coordinator or ProviderCoordinator(settings.provider_ledger_path),
                    str(settings.work_root.resolve()),
                ),
            )
        self.executor = job_executor
        if isinstance(job_executor, JobExecutor):
            job_executor.on_video_remote_completion = self.runtime.record_video_remote_completion
        self.provider_runtime: ProviderRuntime | None = getattr(
            job_executor, "provider_runtime", None
        )
        if self.provider_runtime:
            service.provider_receipt_reader = self.provider_runtime.coordinator.get
        self.owner = uuid4().hex
        self.actor_tasks: dict[tuple[str, str], asyncio.Task[None]] = {}
        self.job_tasks: dict[tuple[str, str], asyncio.Task[None]] = {}
        self.loop_task: asyncio.Task[None] | None = None
        self.heartbeat_task: asyncio.Task[None] | None = None
        self._heartbeat_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="studio-lease"
        )
        self.stopping = False
        self.receipts = JobReceipts(settings.work_root)
        self.active_job_runs: dict[tuple[str, str], asyncio.Task[Any]] = {}
        self.shutdown_pending = 0
        self.shutdown_started_at: float | None = None
        self.last_heartbeat = 0.0
        self.last_recovery_check = 0.0
        self._startup_recovery_pending = False
        from .routing import default_h3_readiness

        self.video_capacity = 0
        self.video_capacities: dict[str, int] = {}
        self.video_readiness = default_h3_readiness(None)
        self.capacity_error: str | None = None
        self.capacity_task: asyncio.Task[None] | None = None
        self.actor_generations: dict[tuple[str, str], int] = {}
        self.last_scheduled_project = ""
        self.last_loop_success_at: float = 0
        self.last_loop_error: str | None = None

    def _recover_startup(self) -> None:
        self.recover_expired()
        self.adopt_provider_inflight()
        self._startup_recovery_pending = False

    async def start(self) -> None:
        self._startup_recovery_pending = True
        try:
            await asyncio.to_thread(self._recover_startup)
        except CoordinatorUnavailable:
            # Keep this source alive and retry original recovery before scheduling.
            # A congested owner is not a failed actor turn or a new POST grant.
            self.last_loop_error = "CoordinatorUnavailable"
            logger.warning("Studio startup recovery waiting for private coordinator")
        self.heartbeat_task = asyncio.create_task(self.renew_leases(), name="studio-heartbeat")
        self.loop_task = asyncio.create_task(self.run(), name="studio-supervisor")
        self.capacity_task = asyncio.create_task(self.refresh_capacity(), name="studio-capacity")

    def request_shutdown(self) -> None:
        if self.shutdown_started_at is None:
            self.shutdown_started_at = time.monotonic()
        self.stopping = True

    async def close(self) -> None:
        self.request_shutdown()
        controls = [*self.actor_tasks.values()]
        controls += [
            task for task in (self.loop_task, self.capacity_task, self.heartbeat_task) if task
        ]
        for task in controls:
            task.cancel()
        await asyncio.gather(*controls, return_exceptions=True)
        pending = set(self.active_job_runs.values())
        unstarted = set(self.job_tasks.values()) - pending
        for task in unstarted:
            task.cancel()
        await asyncio.gather(*unstarted, return_exceptions=True)
        pending = {task for task in pending if not task.done()}
        assert self.shutdown_started_at is not None
        deadline = self.shutdown_started_at + self.settings.shutdown_drain_seconds
        while pending and time.monotonic() < deadline:
            self.shutdown_pending = len(pending)
            await self._heartbeat_async()
            _, pending = await asyncio.wait(
                pending, timeout=min(10, max(0, deadline - time.monotonic()))
            )
        if pending:
            await asyncio.to_thread(self._expire_shutdown_jobs)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        self.shutdown_pending = 0
        self._heartbeat_executor.shutdown(wait=False, cancel_futures=True)

    async def _heartbeat_async(self) -> None:
        await asyncio.get_running_loop().run_in_executor(self._heartbeat_executor, self.heartbeat)

    async def renew_leases(self) -> None:
        """Renew live work independently of expensive scheduling and provider I/O."""
        while not self.stopping:
            try:
                await self._heartbeat_async()
            except Exception as error:
                logger.error("Studio heartbeat failed: %s", type(error).__name__)
            await asyncio.sleep(min(10.0, self.settings.lease_seconds / 3))

    def _expire_shutdown_jobs(self) -> None:
        keys = set(self.job_tasks) | set(self.active_job_runs)
        for project_id in {key[0] for key in keys}:
            job_ids = [key[1] for key in keys if key[0] == project_id]

            def expire(project: StudioProject, job_ids: list[str] = job_ids) -> None:
                for job in select_entities(project.jobs, ids=job_ids):
                    if job.lease_owner != self.owner:
                        continue
                    job.lease_owner, job.lease_until = None, 0
                    if job.status == "submitting":
                        if job.provider_job_id:
                            job.status = "running"
                        elif job.kind in {"image", "video"}:
                            job.status = "ambiguous"
                            job.error = "停机等待超时，提交结果未知；保留原请求，禁止自动重复提交"
                            job.result["shutdown_submission"] = {
                                "generation": job.lease_generation,
                                "timed_out_at": now(),
                                "drain_seconds": self.settings.shutdown_drain_seconds,
                            }
                            self._notify_result(project, job)
                        else:
                            job.status = "queued"

            self.store.mutate(project_id, expire, kind="shutdown_submission_timeout")

    def recover_receipts(self) -> None:
        for path in sorted(self.receipts.root.glob("*.json")):
            try:
                payload = json.loads(path.read_text())
                project_id, job_id = payload["project_id"], payload["job_id"]
                project = self.store.get(project_id)
                job = project.jobs[job_id]
                if (project_id, job_id) in self.active_job_runs or (
                    job.lease_owner and job.lease_until > time.time()
                ):
                    continue
                outcome = self.receipts.outcome(payload)
                if (
                    job.lease_generation != payload["generation"]
                    or request_hash(job) != payload["request_hash"]
                    or job.status not in {"submitting", "running", "ambiguous"}
                    or (
                        job.provider_job_id
                        and outcome.provider_job_id
                        and job.provider_job_id != outcome.provider_job_id
                    )
                ):
                    # Retain unmatched late results as evidence, never overwrite a
                    # newer lease, a reconciled decision or a different request.
                    path.rename(path.with_suffix(".archived"))
                    continue
                shared = (
                    self.provider_runtime.matching_receipt(project, job)
                    if self.provider_runtime and self.provider_runtime.receipt_id(job)
                    else None
                )
                shared_video = None
                if (
                    self.provider_runtime
                    and job.kind == "video"
                    and outcome.status in {"complete", "failed"}
                    and shared is not None
                    and shared["state"] in {"complete", "failed"}
                ):
                    shared_video = self.provider_runtime.recovered(shared)
                    require_video_outcome(project_id, job, shared_video)
                if job.kind == "video":
                    try:
                        require_video_outcome(project_id, job, outcome)
                        if shared_video is not None:
                            require_same_video_outcome(outcome, shared_video)
                        self._verify_receipt_media(project_id, outcome)
                    except ProviderQueryUnavailable:
                        path.rename(path.with_suffix(".archived"))
                        raise
                    except (
                        FileNotFoundError,
                        CASIntegrityError,
                        MediaDecoderTimeout,
                        MediaDecoderUnavailable,
                        ProviderContractError,
                    ):
                        if (
                            shared_video is None
                            and outcome.status in {"complete", "failed"}
                            and job.provider_job_id
                            and job.provider_job_id == outcome.provider_job_id
                        ):
                            self._pin_local_video_recovery(project_id, job)
                        raise
                if self.provider_runtime and shared is not None and shared_video is None:
                    self.provider_runtime.outcome(job, outcome)
                self._complete_job(project_id, job, outcome, recovery=True)
                path.unlink(missing_ok=True)
            except Exception as error:
                logger.error(
                    "Job receipt recovery failed file=%s type=%s", path.name, type(error).__name__
                )

    def _verify_receipt_media(self, project_id: str, outcome: JobOutcome) -> None:
        for media in outcome.media:
            require(
                media.url == f"/api/v1/studio/projects/{project_id}/media/{media.id}",
                "回执媒体归属不一致",
            )
            with self.cas.open(media.sha256) as source:
                if source.seek(0, 2) != media.byte_size:
                    raise CASIntegrityError("Receipt media size does not match its content")

    def _pin_local_video_recovery(self, project_id: str, job: Job) -> None:
        """Hand byte repair to the next leased worker without changing the terminal proof."""
        if job.result.get(LOCAL_VIDEO_RECOVERY_GENERATION) == job.lease_generation:
            return

        def remember(project: StudioProject) -> None:
            current = project.jobs[job.id]
            require(
                current.lease_generation == job.lease_generation
                and current.provider_job_id == job.provider_job_id
                and request_hash(current) == request_hash(job)
                and current.status in {"submitting", "running", "ambiguous"}
                and (not current.lease_owner or current.lease_until <= time.time()),
                "原视频回执归属已变化",
            )
            current.result.setdefault(LOCAL_VIDEO_RECOVERY_GENERATION, job.lease_generation)

        self.store.mutate(
            project_id, remember, kind="video_delivery_repair_wait", advance_revision=False
        )

    def recover_shared_receipts(self) -> None:
        """Recover IDs/results before lease expiry can misclassify a saved result.

        No provider POST or actor resume/selection occurs here. Unmatched or
        unpublished material stays blocked with its original receipt intact.
        """
        if not self.provider_runtime:
            return
        for project_id, job_ids in self.runtime.provider_inflight().items():
            project = self.store.get(project_id)
            for job in select_entities(project.jobs, ids=job_ids):
                if not self.provider_runtime.receipt_id(job) or (
                    (project_id, job.id) in self.active_job_runs
                    or (job.lease_owner and job.lease_until > time.time())
                ):
                    continue
                try:
                    receipt = self.provider_runtime.matching_receipt(project, job)
                    if (
                        receipt["state"] in {"reserved", "cancelled"}
                        and not receipt["provider_job_id"]
                    ):
                        # Only this journal state proves no POST intent began.
                        def requeue(
                            current: StudioProject,
                            original: Job = job,
                            receipt_id: str = receipt["id"],
                        ) -> None:
                            candidate = current.jobs[original.id]
                            require(
                                candidate.lease_generation == original.lease_generation,
                                "任务租约已转移",
                            )
                            require(
                                request_hash(candidate) == request_hash(original),
                                "原任务请求已变化",
                            )
                            require(
                                not candidate.lease_owner or candidate.lease_until <= time.time(),
                                "任务仍有有效租约",
                            )
                            require(candidate.status in {"submitting", "ambiguous"}, "任务已核对")
                            candidate.status = "queued"
                            candidate.lease_owner, candidate.lease_until = None, 0
                            candidate.error = None
                            candidate.result["reservation_recovery"] = {
                                "receipt_id": receipt_id,
                                "recovered_at": now(),
                            }

                        if job.status in {"submitting", "ambiguous"}:
                            self.store.mutate(
                                project_id,
                                requeue,
                                kind="api_unsubmitted_recovered",
                                advance_revision=False,
                            )
                        continue
                    if (
                        receipt["state"] not in {"complete", "failed"}
                        and not receipt["provider_job_id"]
                    ):
                        continue  # Durable intent without acceptance is still unknown.
                    if job.status == "running" and receipt["state"] not in {"complete", "failed"}:
                        if (
                            self.provider_runtime.video_completion(receipt)
                            and job.provider_job_id == receipt["provider_job_id"]
                            and job.result.get("video_remote_completion_id") != job.provider_job_id
                        ):
                            self.runtime.record_video_remote_completion(project_id, job)
                        continue  # Already recoverable by the original GET/poll path.
                    if receipt["state"] == "failed" and not receipt["result"].get("outcome"):
                        failure = receipt["result"].get("error")
                        if (
                            not isinstance(failure, dict)
                            or failure.get("phase") != "submission"
                            or failure.get("definitely_not_accepted") is not True
                            or receipt["provider_job_id"]
                        ):
                            continue
                        # A matched, explicitly rejected POST has no material outcome.
                        # Recover that failure without another POST or a model change.
                        outcome = JobOutcome(
                            "failed",
                            result={
                                **job.result,
                                "api_receipt_id": receipt["id"],
                                "api_receipt_generation": receipt["generation"],
                                "error": (
                                    f"Submission rejected: HTTP {failure.get('http_status')}; "
                                    f"category={failure.get('category')}"
                                ),
                                "shared_failure_reconciliation": {
                                    "receipt_id": receipt["id"],
                                    "receipt_generation": receipt["generation"],
                                    "failure": failure,
                                    "original_status": job.status,
                                    "original_error": job.error,
                                    "reconciled_at": now(),
                                    "provider_posts": 0,
                                },
                            },
                        )
                    else:
                        outcome = self.provider_runtime.recovered(receipt)
                    if job.kind == "video":
                        require_video_outcome(project_id, job, outcome)
                    self._verify_receipt_media(project_id, outcome)
                    self._complete_job(project_id, job, outcome, recovery=True)
                except Exception as error:
                    logger.error(
                        "Shared API reconciliation blocked job=%s type=%s",
                        job.id,
                        type(error).__name__,
                    )

    def adopt_provider_inflight(self) -> None:
        if not self.provider_runtime:
            return
        for project_id, inflight_ids in self.runtime.provider_inflight().items():
            project = self.store.get(project_id)
            jobs = select_entities(project.jobs, ids=inflight_ids)
            ids = [
                j.id
                for j in jobs
                if j.kind in {"image", "video"} and not self.provider_runtime.receipt_id(j)
            ]
            if not ids:
                continue

            def adopt(current: StudioProject, ids: list[str] = ids) -> None:
                assert self.provider_runtime is not None
                for job in select_entities(current.jobs, ids=ids):
                    self.provider_runtime.adopt(current, job)

            self.store.mutate(
                project.id, adopt, kind="api_inflight_accounted", advance_revision=False
            )

    async def refresh_capacity(self) -> None:
        from .routing import default_h3_readiness

        while not self.stopping:
            try:
                pool_checker = getattr(self.executor, "verified_video_capacities", None)
                if pool_checker:
                    self.video_capacities = await asyncio.to_thread(pool_checker)
                    self.video_capacity = sum(self.video_capacities.values())
                    if self.provider_runtime:
                        await asyncio.to_thread(self._observe_video_capacity)
                else:
                    checker = getattr(self.executor, "verified_video_capacity", None)
                    if checker is None:
                        raise ProviderContractError("video capacity verification is unavailable")
                    self.video_capacity = await asyncio.to_thread(checker)
                route = next(
                    (
                        profile
                        for identifier, profile in getattr(
                            self.executor, "video_capacity_routes", {}
                        ).items()
                        if self.video_capacities.get(identifier, 0) > 0
                    ),
                    None,
                )
                self.video_readiness = default_h3_readiness(route)
                self.capacity_error = next(
                    (
                        observation.error
                        for observation in getattr(
                            self.executor, "video_capacity_observations", {}
                        ).values()
                        if observation.error is not None
                    ),
                    None,
                )
            except Exception as error:
                self.video_capacity = 0
                self.video_capacities = {}
                self.video_readiness = default_h3_readiness(None)
                self.capacity_error = type(error).__name__
            await asyncio.sleep(30)

    def _shared_video_admission(self) -> bool:
        return self.provider_runtime is not None

    def _video_capacities_for(self, routes: dict[str, dict[str, Any]]) -> dict[str, int]:
        lookup: Callable[[dict[str, dict[str, Any]], dict[str, int]], dict[str, int]] | None = (
            getattr(self.executor, "video_capacities_for", None)
        )
        return lookup(routes, self.video_capacities) if lookup else self.video_capacities

    def _observe_video_capacity(self) -> None:
        assert self.provider_runtime is not None
        services = self.provider_runtime.registry.services
        observations = getattr(self.executor, "video_capacity_observations", None)
        if observations is not None:
            from stateweaver.providers.registry import wire_identity

            bindings = getattr(self.executor, "video_capacity_services", None) or {
                key: services.get(str(route.get("service_id", key)))
                for key, route in self.executor.video_routes().items()
            }
            grouped: dict[tuple[str, str], tuple[Any, list[Any]]] = {}
            for identifier, observation in observations.items():
                service = bindings.get(identifier)
                if service:
                    key = (
                        wire_identity(
                            kind=service.kind, protocol=service.protocol, base_url=service.base_url
                        ),
                        service.version,
                    )
                    grouped.setdefault(key, (service, []))[1].append(observation)
            for service, values in grouped.values():
                if not any(value.fresh for value in values):
                    continue
                healthy = [
                    value for value in values if value.capacity > 0 and value.observed_at > 0
                ]
                if healthy:
                    capacity = max(value.capacity for value in healthy)
                    observed_at = min(value.observed_at for value in healthy)
                else:
                    capacity = 0
                    observed_at = max(value.observed_at for value in values)
                self.provider_runtime.coordinator.observe_capacity(
                    service, capacity, observed_at=observed_at
                )
            return
        grouped_capacities: dict[str, list[int]] = {}
        for key, route in self.executor.video_routes().items():
            grouped_capacities.setdefault(str(route.get("service_id", key)), []).append(
                self.video_capacities.get(key, 0)
            )
        for identifier, values in grouped_capacities.items():
            if identifier in services:
                self.provider_runtime.coordinator.observe_capacity(
                    services[identifier], min(values)
                )

    def recover_expired(self) -> None:
        self.recover_receipts()
        self.recover_shared_receipts()
        stamp = time.time()
        for project_id, expired in self.runtime.expired(stamp).items():

            def recover(p: StudioProject, ids: dict[str, list[str]] = expired) -> None:
                for actor in select_entities(p.actors, ids=ids["actors"]):
                    if actor.lease_owner and actor.lease_until <= stamp:
                        actor.lease_owner = None
                        actor.lease_until = 0
                        if actor.status == "running":
                            actor.status = "paused" if actor.paused else "ready"
                            if actor.paused:
                                actor.wake_requested = False
                            else:
                                actor.request_wake()
                for job in select_entities(p.jobs, ids=ids["jobs"]):
                    if job.lease_owner and job.lease_until <= stamp:
                        job.lease_owner = None
                        job.lease_until = 0
                        if job.status == "submitting":
                            if job.provider_job_id:
                                job.status = "running"
                            elif job.kind in {"analysis", "assembly", "reference"}:
                                # Analysis has no generative side effects; assembly/crops are local.
                                job.status = "queued"
                            else:
                                job.status = "ambiguous"
                                job.error = "提交过程中服务重启，缺少原任务 ID；禁止自动重复提交"
                                if p.config.production_lifecycle_policy == "coherent-v1":
                                    from .production_recovery import initialize_policy

                                    initialize_policy(job, self.settings)
                                    job.result.setdefault("submission_unknown_since", now())
                                self._notify_result(p, job)

            self.store.mutate(project_id, recover, kind="recovery")

    async def run(self) -> None:
        while not self.stopping:
            try:
                stamp = time.time()
                if self._startup_recovery_pending:
                    await asyncio.to_thread(self._recover_startup)
                    self.last_recovery_check = time.time()
                if stamp - self.last_recovery_check > self.settings.lease_seconds / 3:
                    await asyncio.to_thread(self.recover_expired)
                    self.last_recovery_check = time.time()
                scheduled = await asyncio.to_thread(self.runtime.scheduled, stamp)
                self._reap_tasks()
                starts = await asyncio.to_thread(
                    self._plan_tick,
                    None,
                    scheduled,
                    set(self.actor_tasks),
                    set(self.job_tasks),
                )
                self._dispatch_tick(starts)
                self.last_loop_success_at = stamp
                self.last_loop_error = None
            except Exception as error:
                self.last_loop_error = type(error).__name__
                logger.error("Studio scheduler failed: %s", self.last_loop_error)
            await asyncio.sleep(self.settings.tick_seconds)

    def tick(
        self,
        projects: list[StudioProject] | None = None,
        *,
        scheduled: dict[str, dict[str, list[str]]] | None = None,
    ) -> None:
        """Synchronous scheduling entrypoint for explicit local callers."""
        self._reap_tasks()
        starts = self._plan_tick(projects, scheduled, set(self.actor_tasks), set(self.job_tasks))
        self._dispatch_tick(starts)

    def _reap_tasks(self) -> None:
        for tasks in (self.actor_tasks, self.job_tasks):
            for key, task in list(tasks.items()):
                if task.done():
                    try:
                        task.result()
                    except asyncio.CancelledError:
                        pass
                    except Exception as error:
                        self.last_loop_error = type(error).__name__
                        logger.error("Studio background task failed: %s", self.last_loop_error)
                    del tasks[key]

    def _dispatch_tick(self, starts: list[tuple[str, str, str]]) -> None:
        # A canceled worker-thread read may finish after close(). It must never
        # launch new work. Actual actor/job claims still recheck current leases
        # and pause state inside their existing write transactions.
        if self.stopping:
            return
        for kind, project_id, identifier in starts:
            key = (project_id, identifier)
            tasks = self.actor_tasks if kind == "actor" else self.job_tasks
            if key in tasks:
                continue
            run = self.run_actor if kind == "actor" else self.run_job
            tasks[key] = asyncio.create_task(run(*key))
            if kind != "image":
                self.last_scheduled_project = project_id

    def _plan_tick(
        self,
        projects: list[StudioProject] | None,
        scheduled: dict[str, dict[str, list[str]]] | None,
        actor_tasks: set[tuple[str, str]],
        job_tasks: set[tuple[str, str]],
    ) -> list[tuple[str, str, str]]:
        """Read scheduling inputs off the event loop, returning identities only.

        Pending-task sets are private snapshots. Capacity reservations and
        fairness use the same order as dispatch, without touching live tasks
        or creating an asyncio task in a worker thread.
        """
        if self._startup_recovery_pending:
            return []
        starts: list[tuple[str, str, str]] = []
        stamp = time.time()
        if projects is None and scheduled is None:
            if stamp - self.last_heartbeat > self.settings.lease_seconds / 3:
                self.heartbeat()
                self.recover_expired()
                self.last_recovery_check = time.time()
            scheduled = self.runtime.scheduled(stamp)
        if projects is None:
            assert scheduled is not None
            project_ids = sorted(scheduled)
            project_ids = [p for p in project_ids if p > self.last_scheduled_project] + [
                p for p in project_ids if p <= self.last_scheduled_project
            ]
            projects = [self.store.get(pid) for pid in project_ids]
        elif scheduled is None:
            scheduled = {
                p.id: {
                    "actors": [
                        a.id
                        for a in select_entities(p.actors, statuses=["ready", "running", "waiting"])
                        if not a.paused
                    ],
                    "jobs": [j.id for j in select_entities(p.jobs, statuses=["queued", "running"])],
                }
                for p in projects
            }
        from .routing import route_occupancy, supports_request

        scoped = self._shared_video_admission() and bool(self.video_capacities)
        image_cooldown_until = self.runtime.image_cooldown_until()
        jobs_by_project = {
            p.id: list(select_entities(p.jobs, ids=scheduled[p.id]["jobs"])) for p in projects
        }
        # Actor activity and polling update the project rotation independently
        # of video submissions. Reserve new video slots by durable queue time
        # so newer work cannot repeatedly overtake a quiet waiting project.
        # Dispatches already pending in this process retain their advisory slots.
        queued_videos = sorted(
            (
                (p, job)
                for p in projects
                for job in jobs_by_project[p.id]
                if job.kind == "video" and job.status == "queued"
            ),
            key=lambda pair: (
                scoped and (pair[0].id, pair[1].id) not in job_tasks,
                pair[1].created_at,
                pair[0].id,
                pair[1].id,
            ),
        )
        active_videos = self.runtime.video_reservations(stamp) if scoped and queued_videos else []
        persisted_video_keys = {(item["project_id"], item["id"]) for item in active_videos}
        project_routes: dict[str, tuple[dict[str, dict[str, Any]], dict[str, int]]] = {}
        video_inflight = (
            0 if scoped or not queued_videos else self.runtime.video_inflight(job_tasks)
        )
        for project, job in queued_videos:
            from .playback import set_status as playback_set_status

            candidate = project.candidates.get(job.target_id)
            if (
                candidate
                and not playback_set_status(
                    project, project.sets[project.clips[candidate.clip_id].set_id]
                ).can_submit
            ):
                continue
            if self.video_capacity <= 0 or (not scoped and video_inflight >= self.video_capacity):
                break
            key = (project.id, job.id)
            if (
                key in persisted_video_keys
                or (not scoped and key in job_tasks)
                or job.next_poll_at > stamp
                or (job.lease_owner and job.lease_until > stamp)
                or project.deleted_at
                or project.actors[job.actor_id].paused
            ):
                continue
            if scoped:
                if project.id not in project_routes:
                    routes = self.executor.video_routes(project)
                    project_routes[project.id] = (routes, self._video_capacities_for(routes))
                routes, capacities = project_routes[project.id]
                if not routes:
                    continue
                if len(routes) != 1:
                    raise ProviderContractError("Configure one fixed video service")
                identifier, route = next(iter(routes.items()))
                limit = capacities.get(identifier, 0)
                occupied, competing = route_occupancy(route, active_videos)
                if limit <= 0 or not supports_request(route, job.request):
                    continue
                if competing or occupied >= limit:
                    continue
                active_videos.append({"project_id": project.id, "id": job.id, "route": route})
            if key in job_tasks:
                continue
            job_tasks.add(key)
            starts.append(("job", *key))
            if not scoped:
                video_inflight += 1
        from .scheduling import ordered_images

        for project, job in ordered_images(projects, jobs_by_project, stamp):
            if stamp < image_cooldown_until:
                break
            key = (project.id, job.id)
            if (
                key in job_tasks
                or job.next_poll_at > stamp
                or (job.lease_owner and job.lease_until > stamp)
                or project.deleted_at
                or project.actors[job.actor_id].paused
            ):
                continue
            job_tasks.add(key)
            starts.append(("image", *key))
        for project in projects:
            actors = select_entities(project.actors, ids=scheduled[project.id]["actors"])
            queue_observations = []
            from .production_performance import queue_epoch

            for actor in sorted(
                actors,
                key=lambda a: (
                    -project.delegate_tasks[a.delegate_task_id].request.priority
                    if a.delegate_task_id
                    else 0,
                    a.id,
                ),
            ):
                key = (project.id, actor.id)
                if key in actor_tasks:
                    continue
                actor_eligible = self.eligible(project, actor, stamp)
                queue_observations.append(
                    (actor.id, actor.role, queue_epoch(actor), actor_eligible)
                )
                if not actor_eligible:
                    continue
                actor_tasks.add(key)
                starts.append(("actor", *key))
            self.store.metrics.performance.observe_queue(project.id, queue_observations, stamp)
            for job in jobs_by_project[project.id]:
                key = (project.id, job.id)
                if key in job_tasks:
                    continue
                if job.status not in {"queued", "running"} or job.next_poll_at > stamp:
                    continue
                if job.lease_owner and job.lease_until > stamp:
                    continue
                if job.kind in {"video", "image"} and job.status == "queued":
                    # All new generation reservations were considered above.
                    continue
                # Paused roles still collect already submitted tasks. Queued work has
                # not crossed the provider boundary and waits until resume.
                if job.status == "queued" and (
                    project.deleted_at or project.actors[job.actor_id].paused
                ):
                    continue
                job_tasks.add(key)
                starts.append(("job", *key))
        return starts

    def eligible(self, project: StudioProject, actor: Actor, stamp: float) -> bool:
        if project.config.production_lifecycle_policy == "coherent-v1" and project.status in {
            "complete",
            "failed",
        }:
            return False
        if project.deleted_at or actor.paused or actor.status in {"complete", "idle", "blocked"}:
            return False
        if actor.role in {"task_analyst", "material_executor"}:
            from .delegation import task_for as delegate_task_for

            task = delegate_task_for(project, actor)
            parent = project.actors.get(task.parent_actor_id)
            if not parent or parent.paused or task.status not in {"queued", "running"}:
                return False
            from .delegation import execution_blockers

            if execution_blockers(project, actor):
                return False
        if actor.role == "visual_analyst":
            from .visual_analysis import task_for

            parent = project.actors.get(actor.parent_actor_id or "")
            if (
                not parent
                or parent.paused
                or task_for(project, actor).status not in {"queued", "running"}
            ):
                return False
        if actor.recovery_not_before > stamp:
            return False
        if actor.lease_owner and actor.lease_until > stamp:
            return False

        if actor.status == "waiting" and self.wait_reason(project, actor):
            return False  # Unrelated wakes must not start a native Goal just to wait again.
        if (
            not actor.wake_requested
            and actor.status not in {"ready", "running"}
            and (actor.status != "waiting" or self.wait_reason(project, actor))
        ):
            return False
        if actor.role == "set_director":
            from .workflow import can_plan_set

            production_set = project.sets[str(actor.set_id)]
            return can_plan_set(project, production_set)
        return True

    def progress_eligible(self, project: StudioProject, actor: Actor, stamp: float) -> bool:
        """Include this supervisor's live, fenced startup without admitting another turn."""
        if self.eligible(project, actor, stamp):
            return True
        key = (project.id, actor.id)
        task = self.actor_tasks.get(key)
        owned = bool(
            not project.deleted_at
            and project.status not in {"complete", "failed"}
            and not actor.paused
            and not actor.native_session_blocked
            and actor.status in {"running", "ready"}
            and actor.lease_owner == self.owner
            and actor.lease_until > stamp
            and self.actor_generations.get(key) == actor.lease_generation
            and task is not None
            and not task.done()
        )
        if not owned or actor.status == "running":
            return owned
        if actor.role == "set_director":
            production_set = project.sets.get(str(actor.set_id))
            if not production_set or production_set.status in {"complete", "failed"}:
                return False
        from .production_runtime import native_retry_scheduled

        # Recovery can commit ready before this owned task finishes pausing and
        # releasing its lease. Observe its exact scheduled retry without changing
        # the real actor or admitting another native turn while the lease is held.
        unleased = actor.model_copy(update={"lease_owner": None, "lease_until": 0.0})
        return native_retry_scheduled(project, unleased, actor_eligible=self.eligible, stamp=stamp)

    def heartbeat(self) -> None:
        # A stalled scheduler may not reap completed tasks promptly. Never let
        # the independent heartbeat keep their abandoned leases alive.
        actor_ids = tuple(key for key, task in tuple(self.actor_tasks.items()) if not task.done())
        job_ids = {
            key
            for tasks in (self.job_tasks, self.active_job_runs)
            for key, task in tuple(tasks.items())
            if not task.done()
        }
        self.runtime.heartbeat(
            self.owner,
            actor_ids,
            tuple(job_ids),
            time.time() + self.settings.lease_seconds,
            lease_seconds=self.settings.lease_seconds,
        )
        self.last_heartbeat = time.time()

    def _claim_actor(self, project_id: str, actor_id: str) -> StudioProject:
        from .production_performance import queue_epoch

        queue_identity = []

        def claim(p: StudioProject) -> None:
            actor = p.actors[actor_id]
            require(self.eligible(p, actor, time.time()), "角色已有有效租约或尚未就绪")
            queue_identity.append(queue_epoch(actor))
            if actor.role == "visual_analyst":
                p.visual_tasks[str(actor.visual_task_id)].status = "running"
            if actor.role in {"task_analyst", "material_executor"}:
                p.delegate_tasks[str(actor.delegate_task_id)].status = "running"
            actor.lease_generation += 1
            actor.lease_owner = self.owner
            actor.lease_until = time.time() + self.settings.lease_seconds
            actor.status = "running"
            actor.wake_requested = False
            actor.last_error = None
            activate_session(p, actor)

        project = self.store.mutate(
            project_id, claim, actor_id=actor_id, kind="actor_started", advance_revision=False
        )
        self.actor_generations[(project_id, actor_id)] = project.actors[actor_id].lease_generation
        self.store.metrics.performance.claimed(project_id, actor_id, queue_identity[0], time.time())
        return project

    def _instructions(self, actor: Actor, project: StudioProject) -> tuple[str, str]:
        from .planning_sessions import enabled as short_planning

        session = actor.sessions.get(actor.session_key)
        reviewing = session is not None and session.purpose not in {
            "planning",
            "executive_production",
        }
        folder = {
            "screenwriter": "screenwriter-story-v1",
            "executive": "set-executive-fast-v2",
            "set_director": "set-director-fast-v2",
            "visual_analyst": "executive-visual-analyst-v1",
            "task_analyst": "executive-task-analyst-v1",
            "material_executor": "material-executor-v1",
        }[actor.role]
        directs = actor.role in {"executive", "set_director"}
        if directs and reviewing:
            folder = "set-reviewer-v3"
        if project.director_mode == "delegated-v1" and actor.role == "executive":
            folder = "set-executive-delegated-v1"
        paths = [self.settings.roles_skill_root / folder / "SKILL.md"]
        if directs and not reviewing:
            paths.extend(
                self.settings.roles_skill_root / name / "SKILL.md"
                for name in ("set-duration-budget-v1", "state-evolution-v1", "story-pacing-v2")
            )
        if directs:
            paths.append(self.settings.roles_skill_root / "set-handoffs-v1" / "SKILL.md")
        media_package = {
            "executive": "image25-v1",
            "material_executor": "image25-delegated-v1",
        }.get(actor.role)
        if actor.role == "set_director" and not reviewing:
            media_package = "h3-ref2va-set-v2"
        if media_package is not None:
            paths.append(
                self.settings.roles_skill_root.parent / "studio-media" / media_package / "SKILL.md"
            )
        if actor.role != "screenwriter":
            paths.append(self.settings.roles_skill_root / "image-input-recovery-v1" / "SKILL.md")
        if actor.role == "set_director":
            paths.append(self.settings.roles_skill_root / "set-director-efficient-v1" / "SKILL.md")
            if short_planning(project, actor):
                paths.append(self.settings.roles_skill_root / "set-director-rounds-v1" / "SKILL.md")
        if actor.role == "executive":
            paths.append(
                self.settings.roles_skill_root / "set-executive-quality-packets-v1" / "SKILL.md"
            )
        if directs:
            paths.append(
                self.settings.roles_skill_root / "clip-continuation-choice-v1" / "SKILL.md"
            )
        if actor.role != "screenwriter":
            paths.extend(
                self.settings.roles_skill_root / name / "SKILL.md"
                for name in ("production-tasks-v1", "production-lifecycle-v1")
            )
        contents = []
        for path in paths:
            if not path.is_file():
                raise StudioError(f"角色所需 Skill 缺失：{path.parent.name}")
            content = path.read_text()
            if not content.strip() or not content.startswith("---"):
                raise StudioError(f"角色所需 Skill 无效：{path.parent.name}")
            contents.append(content)
        instructions = "\n\n".join(contents)
        from .policy import project_video_http, runtime_policy

        video = project_video_http(project)
        if video is not None and directs:
            instructions += (
                "\n\nThis project uses a generic video API. Keep the six-section Studio recipe "
                "and all local evidence and review requirements. Do not request paired tail "
                "video and latent anchoring. Use a fresh generation for repairs when "
                "editing_allowed is false. The actual predecessor end state remains evidence."
            )
            if video.reference_policy == "text-only":
                instructions += (
                    " The upstream receives prose only. Describe concrete character appearance, "
                    "action, spatial layout, opening composition and voice in the prompt. "
                    "Numbered references remain local planning and review evidence; never say "
                    "the provider used a reference file or enforced an anchor."
                )
        instructions += "\n\nAuthoritative project runtime policy:\n" + json.dumps(
            runtime_policy(project), ensure_ascii=False
        )
        if actor.role != "screenwriter":
            from .task_context import full_context_required

            context_policy = (
                "full_context_policy=strict. Complete every page of the current required "
                "task packet before context-gated production actions, then use its final "
                "context_token as read_token."
                if full_context_required(project)
                else "full_context_policy=optional. Full task-text reading is optional. "
                "This current optional policy supersedes generic full-text prerequisites in "
                "historical Skills, actor goals and operator boilerplate, including blanket "
                "requirements to reread all task text before every production action. "
                "Read the current assignment and relevant entities, then use a fresh scoped "
                "read_token from get_project. Read relevant "
                "details or get_required_context when information is missing or uncertain. "
                "Specific story requirements, targeted original-text checks and other "
                "substantive task/operator instructions still apply; generic full-text "
                "boilerplate does not reinstate the waived prerequisite. An overview or partial "
                "packet is not complete text; an optional read may stop early but must never "
                "be recorded as fully read."
            )
            instructions += (
                "\n\nAuthoritative task-text reading policy (full-context-policy-v1):\n"
                + context_policy
                + " Preserve actor/session/version/dependency fences and every required "
                "image inspection, quarantine, quality, source and state check. Explicit "
                "full-text reading still follows next_request; only a complete read creates "
                "a complete baseline, and compaction or context changes invalidate it."
            )
        from .image_input_recovery import recovery_context

        context = recovery_context(actor)
        if context:
            instructions += "\n\nImage input recovery:\n" + json.dumps(context, ensure_ascii=False)
        if reviewing and project is not None:
            instructions += "\n\nCurrent review assignment:\n" + json.dumps(
                assignment(project, actor), ensure_ascii=False
            )
        if project and actor.role in {"task_analyst", "material_executor"}:
            from .delegation import actions_for, records_only, task_for

            task = task_for(project, actor)
            capability: dict[str, Any] = {
                "kind": task.request.kind,
                "allowed_actions": task.allowed_actions,
                "allowed_scope": task.allowed_scope.model_dump(),
                "task_id": task.id,
                "version": task.version,
            }
            if records_only(task):
                allowed = actions_for(project, actor)
                capability["allowed_actions"] = [
                    action for action in task.allowed_actions if action in allowed
                ]
                capability["evidence_policy"] = "records-only"
            instructions += "\n\nAuthoritative task capability contract:\n" + json.dumps(
                capability, ensure_ascii=False
            )
        return instructions, hashlib.sha256(instructions.encode()).hexdigest()

    async def run_actor(self, project_id: str, actor_id: str) -> None:
        if self.role_runner is None:
            raise RuntimeError("The StateWeaver role runner has not been installed")
        await self.role_runner(project_id, actor_id)

    def _recover_actor(
        self,
        p: StudioProject,
        actor: Actor,
        detail: str,
        *,
        rebuild: bool = False,
        reason_category: str = "native",
    ) -> bool:
        """Bounded technical recovery; durable jobs/receipts remain the authority."""
        production_set = p.sets.get(actor.set_id or "")
        if actor.role == "set_director" and production_set and production_set.status == "complete":
            # A late native failure cannot reopen a committed business completion.
            return False
        from .image_input_recovery import is_image_refusal, recover_image_input

        blocker = actor.production_blocker
        if (
            reason_category == "no_progress"
            and not actor.paused
            and blocker
            and blocker.get("progress_version") == actor.progress_version
            and blocker.get("occurrences", 0) >= 2
        ):
            actor.status, actor.wake_requested = "blocked", False
            actor.last_error = None  # A field correction is not a broken native session.
            actor.recovery_not_before = 0
            actor.summary = str(blocker["description"]) + " " + str(blocker["next_action"])
            if actor.role == "set_director":
                p.sets[str(actor.set_id)].status = "blocked"
                wake_executive(p, actor.summary)
            elif actor.role in {"task_analyst", "material_executor"}:
                from .delegation import fail as fail_delegate

                fail_delegate(p, actor, actor.summary)
            add_item(
                p,
                actor.id,
                "error",
                actor.summary,
                {"technical_recovery": False, "reason": "domain_conflict", "rebuild": False},
            )
            return False
        if not actor.paused and "maximum number of images" in detail.lower():
            from .image_capacity_recovery import recover_image_capacity

            if recover_image_capacity(p, actor, detail):
                return True
            actor.last_error = detail
            actor.status, actor.wake_requested = "blocked", False
            actor.summary = "图片数量超过服务端容量；相同任务重试无效，请按每候选一委托重建范围"
            if actor.role == "visual_analyst":
                from .visual_analysis import fail

                fail(
                    p,
                    actor,
                    actor.summary,
                    self.store,
                    unconfirmed_media_ids=actor.last_image_input.media_ids
                    if actor.last_image_input
                    else [],
                )
            elif actor.role in {"task_analyst", "material_executor"}:
                from .delegation import fail as fail_delegate

                fail_delegate(p, actor, actor.summary)
            elif actor.role == "set_director":
                p.sets[str(actor.set_id)].status = "blocked"
                wake_executive(p, actor.summary)
            add_item(
                p,
                actor.id,
                "error",
                actor.summary,
                {"technical_recovery": False, "reason": "image_capacity", "rebuild": False},
            )
            return False
        if is_image_refusal(detail) and actor.role != "screenwriter":
            permitted = recover_image_input(p, actor, detail)
            if not permitted and actor.role == "visual_analyst" and not actor.paused:
                from .visual_analysis import fail

                fail(p, actor, "图片拒绝恢复未完成：" + actor.summary, self.store)
            if (
                not permitted
                and actor.role in {"task_analyst", "material_executor"}
                and not actor.paused
            ):
                from .delegation import fail as fail_delegate

                fail_delegate(p, actor, "图片拒绝恢复未完成：" + actor.summary)
            return permitted
        actor.last_error = detail
        if actor.paused:
            actor.status, actor.wake_requested = "paused", False
            return False
        overflow_session = review_session(p, actor) if is_context_overflow(detail) else None
        prior_compaction = recovery_record(overflow_session) if overflow_session else None
        if prior_compaction and prior_compaction.get("status") != "pending":
            actor.status, actor.wake_requested = "blocked", False
            actor.summary = (
                "Original review native compaction already attempted; history retained. "
                "Reconcile its outcome before further recovery."
            )
            return False
        actor.recovery_attempts += 1
        permitted = actor.recovery_attempts <= self.settings.actor_recovery_limit
        session = actor.sessions.get(actor.session_key)
        single_review = bool(session and session.purpose == "candidate_review")
        image_recovery = any(
            recovery.status == "retrying" for recovery in actor.image_input_recoveries
        )
        missing_context = (
            "no rollout found for thread id" in detail.lower()
            or detail.lower() == "original remote native thread is missing; cannot resume"
        )
        if missing_context and not single_review and not image_recovery:
            # A confirmed missing ordinary context gets one actual replacement,
            # independently of earlier generic recoveries. Its history still counts;
            # it cannot consume this replacement before one has actually completed.
            permitted = not actor.paused and not any(
                h.get("reason_category") == "native_context_missing" and h.get("rebuild") is True
                for h in actor.recovery_history
            )
            reason_category = "native_context_missing"
            rebuild = True
        # A candidate review may reconnect to its existing native thread, but
        # technical recovery must not silently replace its inspected image context.
        rebuild = (
            permitted
            and not single_review
            and not image_recovery
            and (rebuild or actor.recovery_attempts >= 2)
        )
        actor.recovery_history.append(
            {
                "attempt": actor.recovery_attempts,
                "reason": detail,
                "reason_category": reason_category,
                "created_at": now(),
                "previous_thread_id": actor.thread_id,
                "checkpoint_turn_id": actor.history_turn_id,
                "rebuild": rebuild,
                "progress_version": actor.progress_version,
                "inflight_job_ids": [
                    j.id
                    for j in select_entities(
                        p.jobs, statuses=["queued", "submitting", "running", "ambiguous"]
                    )
                    if j.actor_id == actor.id
                ],
            }
        )
        if permitted:
            if overflow_session:
                queue_recovery(p, actor)
            if rebuild:
                actor.thread_id = None
                actor.history_turn_id = None
                from .director_efficiency import invalidate_context

                invalidate_context(p, actor, reason="native_context_replacement")
            actor.status = "ready"
            actor.request_wake()
            actor.recovery_not_before = (
                time.time() + self.settings.actor_recovery_delay_seconds * actor.recovery_attempts
            )
            actor.summary = (
                "已确认原生会话丢失；从持久化操作和任务重建一次普通上下文，继续原制作进度"
                if reason_category == "native_context_missing"
                else (
                    f"会话自动恢复 {actor.recovery_attempts}/{self.settings.actor_recovery_limit}；"
                    f"已核对持久化操作和任务，继续原制作进度：{detail}"
                )
            )
            if actor.role == "set_director":
                p.sets[str(actor.set_id)].status = "producing"
        else:
            if self._settle_exhausted_quality(p, actor):
                if actor.role not in {"task_analyst", "material_executor"}:
                    actor.status, actor.wake_requested = "ready", True
                    actor.last_error = None
                actor.no_progress_count = 0
                actor.summary = (
                    "Decision recovery exhausted; recorded system fallback and continued."
                )
                return True
            actor.status, actor.wake_requested = "blocked", False
            actor.summary = "会话技术恢复次数已用尽；任务、结果和修复计数保留：" + detail
            if actor.role == "set_director":
                p.sets[str(actor.set_id)].status = "blocked"
                wake_executive(p, actor.summary)
            if actor.role == "visual_analyst":
                from .visual_analysis import fail

                fail(p, actor, actor.summary, self.store)
            if actor.role in {"task_analyst", "material_executor"}:
                from .delegation import fail as fail_delegate

                fail_delegate(p, actor, actor.summary)
        add_item(
            p,
            actor.id,
            "error",
            actor.summary,
            {
                "technical_recovery": permitted,
                "attempt": actor.recovery_attempts,
                "rebuild": rebuild,
            },
        )
        return permitted

    def _require_actor_lease(self, actor: Actor, generation: int | None) -> None:
        require(
            actor.lease_owner == self.owner
            and (generation is None or actor.lease_generation == generation),
            "角色租约已转移",
        )

    def settle_turn(
        self,
        project_id: str,
        actor_id: str,
        previous_progress: int,
        turn_id: str | None,
        event_id: str,
        turn: dict[str, Any],
        *,
        lease_generation: int | None = None,
        fingerprint_data: dict[str, Any] | None = None,
        require_current_turn: bool = False,
    ) -> None:
        generation = (
            lease_generation
            if lease_generation is not None
            else self.actor_generations.get((project_id, actor_id))
        )

        def finish(p: StudioProject) -> None:
            actor = p.actors[actor_id]
            self._require_actor_lease(actor, generation)
            if require_current_turn:
                require(
                    actor.lease_until > time.time() and actor.turn_id == turn_id,
                    "角色当前回合已变化",
                )
            accepted_wait_this_turn = bool(
                p.config.production_lifecycle_policy == "coherent-v1"
                and actor.runtime_wait
                and actor.turn_id
                and actor.runtime_wait.registered_turn_id == actor.turn_id == turn_id
            )
            from .task_context import consume_context_turn_progress

            context_progress_this_turn = consume_context_turn_progress(actor, turn_id)
            actor.turn_id = None
            actor.history_turn_id = turn_id
            production_set = p.sets.get(actor.set_id or "")
            if (
                actor.role == "set_director"
                and production_set
                and production_set.status == "complete"
            ):
                # The native turn may finish after finish_set already committed.
                return
            from .image_input_recovery import complete_image_recovery

            complete_image_recovery(actor)
            if handoff_completed_lookahead(p, actor):
                # A saved draft-to-review handoff is normal business scheduling,
                # not a stalled turn or a technical context recovery.
                return
            if actor.role in {"task_analyst", "material_executor"}:
                task = p.delegate_tasks.get(actor.delegate_task_id or "")
                if task and task.status in {"complete", "blocked", "failed"}:
                    return  # A reported terminal task is not a stalled model turn.
            legitimate_wait = accepted_wait_this_turn or (
                actor.status == "waiting" and self.wait_reason(p, actor)
            )
            if (
                not legitimate_wait
                and actor.role == "executive"
                and actor.status in {"running", "ready", "waiting"}
                and actor.progress_version <= previous_progress
            ):
                # A coordinator that has no decision to make while a submitted
                # job, director or delegate is progressing must wait for that
                # durable dependency instead of consuming recovery attempts by
                # polling the same state repeatedly.
                from .production_runtime import independent_work_ready, register_wait

                if independent_work_ready(p):
                    # register_wait deliberately accepts only an explicit
                    # waiting transition.  Make that transition in this same
                    # transaction; when no durable target exists it restores
                    # ready+wake rather than manufacturing an idle wait.
                    actor.status = "waiting"
                    actor.wake_requested = False
                    wait = register_wait(
                        p,
                        actor,
                        reason="等待已提交制作、片场审核或委托任务产生新的持久化结果",
                    )
                    if wait and wait.get("waiting"):
                        actor.status = "waiting"
                        legitimate_wait = True
            stalled = not legitimate_wait and not actor.paused and actor.status != "complete"
            if actor.progress_version > previous_progress:
                actor.no_progress_count = 0
                actor.recovery_attempts = 0
                actor.recovery_not_before = 0
            elif context_progress_this_turn:
                # Required-page advancement is not a creative command and must
                # not reset the native recovery budget or its original history.
                actor.no_progress_count = 0
            elif (
                legitimate_wait
                and actor.role == "executive"
                and p.director_mode == "delegated-v1"
                and p.config.production_lifecycle_policy != "coherent-v1"
            ):
                from .delegation import progressing_tasks

                if progressing_tasks(p, actor.id):
                    # An actual delegation breaks this coordination stall. Keep
                    # legacy job-wait accounting and native/quality history.
                    actor.no_progress_count = 0
            elif stalled:
                actor.no_progress_count += 1
            if stalled and actor.no_progress_count >= p.config.no_progress_turns:
                unfinished = [
                    p.clips[c]
                    for s in p.sets.values()
                    if actor.role == "executive" or s.id == actor.set_id
                    for c in s.clip_ids
                    if not p.clips[c].accepted_candidate_id
                ]
                quality = next(
                    (
                        c
                        for c in unfinished
                        if c.candidate_ids and c.status in {"repair", "escalated", "review"}
                    ),
                    None,
                )
                if (
                    quality is not None
                    and actor.role == "set_director"
                    and ordinary_clip_corrections_complete(p, quality)
                ):
                    escalate_clip(
                        p, actor, quality, "常规修正已完成且多轮未有进展，交由总导演选择方案"
                    )
                    actor.no_progress_count = 0
                elif actor.role == "executive" and any(
                    issue.startswith("quality:") for issue in p.issues
                ):
                    settled = self._settle_preapproved_quality(p, actor)
                    if settled:
                        actor.status = "ready"
                        actor.request_wake()
                        actor.no_progress_count = 0
                        actor.summary = "已执行预先指定的兜底选择，继续制作"
                    else:
                        self._recover_actor(
                            p,
                            actor,
                            "质量接管尚未形成新决定；重建决策上下文，立即查看候选并定向修正或妥协选用，不能反复重置次数",
                            rebuild=True,
                            reason_category="no_progress",
                        )
                elif actor.role in {"task_analyst", "material_executor"} and actor.last_image_input:
                    prior_redeliveries = sum(
                        item.get("reason_category") == "image_delivery"
                        for item in actor.recovery_history
                    )
                    if prior_redeliveries:
                        actor.recovery_attempts = max(
                            actor.recovery_attempts, self.settings.actor_recovery_limit
                        )
                    self._recover_actor(
                        p,
                        actor,
                        (
                            "图片返回后仍未形成可执行进展；已完成一次精确重送，现在交由父角色接管"
                            if prior_redeliveries
                            else "图片返回未形成进展；清除当前会话图片收据并精确重送一次"
                        ),
                        rebuild=True,
                        reason_category="image_delivery",
                    )
                else:
                    self._recover_actor(
                        p,
                        actor,
                        "连续多轮没有可执行进展；核对工具故障和当前任务",
                        rebuild=True,
                        reason_category="no_progress",
                    )
            elif actor.status == "waiting" and not legitimate_wait:
                actor.status = "running"

        self.store.mutate(
            project_id,
            finish,
            actor_id=actor_id,
            kind="turn_completed",
            key="event:" + event_id,
            fingerprint_data=fingerprint_data if fingerprint_data is not None else {"turn": turn},
            advance_revision=False,
        )

    def _settle_exhausted_quality(self, p: StudioProject, actor: Actor) -> bool:
        from .candidate_lifecycle import fallback_selection_plan
        from .production_decisions import apply_selection
        from .quality_settlement import fallback_settlement_ready

        if (
            p.deleted_at
            or p.status in {"complete", "failed"}
            or actor.paused
            or actor.role not in {"executive", "set_director", "task_analyst", "material_executor"}
        ):
            return False
        decision_actor = actor
        scoped_clip_ids: set[str] | None = None
        if actor.role in {"task_analyst", "material_executor"}:
            from .delegation import task_for

            task = task_for(p, actor)
            decision_actor = p.actors.get(task.parent_actor_id) or actor
            if decision_actor.role != "executive" or decision_actor.paused:
                return False
            scoped_clip_ids = set(task.request.scope.clip_ids)
            scoped_clip_ids.update(
                p.candidates[identifier].clip_id
                for identifier in task.request.scope.candidate_ids
                if identifier in p.candidates
            )
        settled = False
        for production_set in p.sets.values():
            if production_set.status in {"complete", "failed"}:
                continue
            if actor.role == "set_director" and production_set.id != actor.set_id:
                continue
            for clip in select_entities(p.clips, ids=production_set.clip_ids):
                if (
                    clip.archived_at
                    or clip.accepted_candidate_id
                    or clip.terminal_failure
                    or scoped_clip_ids is not None
                    and clip.id not in scoped_clip_ids
                    or not fallback_settlement_ready(p, clip)
                ):
                    continue
                plan = fallback_selection_plan(p, clip)
                if not plan:
                    continue
                outcome = apply_selection(p, decision_actor, clip, plan, kind="system_fallback")
                add_item(p, decision_actor.id, "tool", "system_fallback", outcome)
                if actor.role in {"task_analyst", "material_executor"}:
                    from .delegation import supersede

                    supersede(
                        p,
                        actor,
                        "父角色已用保存的可播放候选完成接管；原专项失败保留为历史，不再阻塞关键路径。",
                    )
                director = p.actors[production_set.actor_id]
                if not director.paused and not director.native_session_blocked:
                    director.request_wake()
                    if director.status != "running":
                        director.status = "ready"
                settled = True
        return settled

    def _settle_preapproved_quality(self, p: StudioProject, actor: Actor) -> bool:
        """Only execute a fallback the executive already explicitly designated."""
        for issue in list(p.issues):
            if issue.startswith("quality:clip:"):
                clip = p.clips[issue.split(":", 2)[2]]
                episode = clip.quality_episode
                if not episode or not episode.fallback_id:
                    continue

                from .candidate_lifecycle import fallback_selection_plan
                from .production_decisions import apply_selection
                from .quality_settlement import preapproved_clip_settlement_ready

                if not preapproved_clip_settlement_ready(p, clip):
                    continue
                plan = fallback_selection_plan(p, clip)
                if not plan:
                    continue
                outcome = apply_selection(p, actor, clip, plan, kind="system_fallback")
                add_item(p, actor.id, "tool", "system_fallback", outcome)
                return True
                if not ordinary_clip_corrections_complete(p, clip):
                    continue
                candidate = p.candidates[episode.fallback_id]
                data: dict[str, Any] = {
                    "clip_id": clip.id,
                    "candidate_id": candidate.id,
                    "deviations": candidate.issues
                    or [candidate.review.reason if candidate.review else "保留原检查所记录的偏差"],
                }
            elif issue.startswith("quality:reference:"):
                _, _, state_id, view = issue.split(":", 3)
                from .delegation import reference_has_worker

                if reference_has_worker(p, state_id, view):
                    continue
                episode = p.states[state_id].repair_episodes.get(view)
                if not episode or not episode.fallback_id:
                    continue
                if not ordinary_image_corrections_complete(p, state_id, view):
                    continue
                data = {
                    "state_id": state_id,
                    "view": view,
                    "revision_id": episode.fallback_id,
                    "deviations": episode.plan.defects
                    if episode.plan and episode.plan.defects
                    else ["保留原图片检查所记录的偏差"],
                }
            else:
                continue
            if episode.extra_used < episode.extra_granted:
                continue
            try:
                outcome = resolve_quality(
                    self.service,
                    p,
                    actor,
                    {
                        **data,
                        "decision": "select",
                        "reason": (
                            "执行总导演在追加尝试前已指定的兜底选择；保留原始失败检查并继续制作"
                        ),
                    },
                )
            except StudioError:
                continue
            add_item(p, actor.id, "tool", "resolve_quality", outcome)
            return True
        return False

    def has_ready_work(
        self, p: StudioProject, actor: Actor, *, include_lookahead: bool = True
    ) -> bool:
        from .production_readiness import has_ready_work

        return has_ready_work(p, actor, self.runtime, include_lookahead=include_lookahead)

    def wait_reason(self, p: StudioProject, actor: Actor) -> bool:
        if actor.role == "screenwriter" and p.status == "screenwriting":
            return True
        from .production_runtime import registered_wait

        if registered_wait(p, actor):
            return not self.has_ready_work(p, actor, include_lookahead=False)
        if self.has_ready_work(p, actor):
            return False

        from .candidate_lifecycle import terminal_recovery

        if any(
            not terminal_recovery(job)
            for job in select_entities(
                p.jobs, actor_id=actor.id, statuses=["queued", "submitting", "running"]
            )
        ):
            return True
        if actor.role == "executive":
            from .delegation import progressing_tasks

            if progressing_tasks(p, actor.id):
                return True
            if select_entities(p.visual_tasks, statuses=["queued", "running"], limit=1):
                return True
            if any(issue.startswith("quality:") for issue in p.issues):
                return True
            return bool(p.catalog_frozen_at) and bool(
                select_entities(p.sets, statuses=["planned", "producing"], limit=1)
            )
        if actor.role == "set_director":
            production_set = p.sets[str(actor.set_id)]
            from .playback import set_status as playback_set_status

            if (
                p.playback_policy == "set-budget-v1"
                and not playback_set_status(p, production_set).can_submit
            ):
                return True
            first = next(
                (
                    p.clips[c]
                    for c in production_set.clip_ids
                    if not p.clips[c].accepted_candidate_id
                ),
                None,
            )
            if first:
                from .quality_progress import quality_progress
                from .set_handoffs import dependency_report

                if dependency_report(p, production_set.id, first).blockers:
                    return True

                if any(
                    b.code == "new_reference_required" for b in quality_progress(p, first).blockers
                ):
                    return True
            if first and (
                first.status == "escalated"
                or waiting_for_voices(p, first)
                or clip_pending_proposals(p, first)
                or first.state_readiness == "needs_verification"
            ):
                return True
            return any(p.sets[d].status != "complete" for d in production_set.depends_on)
        return False

    def _claim_job(self, project_id: str, job_id: str) -> StudioProject:
        def claim(p: StudioProject) -> None:
            job = p.jobs[job_id]
            capacity = None
            require(not job.lease_owner or job.lease_until <= time.time(), "任务已有租约")
            require(job.status in {"queued", "running"}, "任务尚未就绪")
            if job.status == "queued":
                require(job.next_poll_at <= time.time(), "任务正在退避，保留原任务等待到期")
                if p.config.production_lifecycle_policy == "coherent-v1":
                    from .candidate_lifecycle import terminal_recovery

                    require(p.status not in {"complete", "failed"}, "Production is closed.")
                    require(
                        not terminal_recovery(job),
                        "Local recovery is already terminal.",
                    )
                require(not p.deleted_at, "项目已删除")
                require(not p.actors[job.actor_id].paused, "角色已暂停")
                if p.actors[job.actor_id].delegate_task_id:
                    from .delegation import execution_blockers, task_for

                    actor = p.actors[job.actor_id]
                    require(
                        task_for(p, actor).status in {"queued", "running"}
                        and not execution_blockers(p, actor),
                        "专项素材任务已阻塞、过期或其总导演暂停",
                    )
                if job.kind == "image":
                    require(
                        self.runtime.image_cooldown_until() <= time.time(),
                        "图片服务正在限流冷却，保留队列等待",
                    )
                    require(
                        job.next_poll_at <= time.time(),
                        "图片任务正在指数退避，保留原任务等待",
                    )
                    from .image_delivery import initialize as initialize_image_delivery

                    initialize_image_delivery(job, p.config.image_model, self.settings.image_retry)
                if job.kind == "video":
                    from .playback import require_submission

                    require_submission(p, p.clips[p.candidates[job.target_id].clip_id])
                    if self.provider_runtime:
                        routes = self.executor.video_routes(p)
                        require(len(routes) == 1, "请使用项目固定的视频服务配置")
                        identifier, route = next(iter(routes.items()))
                        capacity = self._video_capacities_for(routes).get(identifier, 0)
                        from .routing import supports_request

                        require(
                            capacity > 0 and supports_request(route, job.request),
                            "当前视频服务未就绪或缺少本次任务所需能力",
                        )
                        if saved := job.request.get("provider_route"):
                            require(
                                route_physical_identity(saved) == route_physical_identity(route)
                                and saved.get("id") == identifier
                                and saved.get("base_url") == route.get("base_url"),
                                "原请求绑定的视频服务已变化",
                            )
                        else:
                            job.request["provider_route"] = route
                        if not job.request.get("api_receipt_id"):
                            from .routing import active_for_routes, route_occupancy

                            active = self.runtime.video_reservations(
                                time.time(), require_registered_actor=True
                            )
                            active = self.provider_runtime.shared_active(active, routes)
                            occupied, competing = route_occupancy(
                                route, active_for_routes(active, routes)
                            )
                            require(not competing and occupied < capacity, "视频服务可用并发已满")
                    else:
                        require(
                            self.runtime.video_inflight(()) < self.video_capacity,
                            "视频服务并发已满或尚未验证",
                        )
                if self.provider_runtime and job.kind in {"image", "video"}:
                    try:
                        receipt = self.provider_runtime.bind(
                            p, job, owner=self.owner, capacity=capacity
                        )
                    except ProviderCapacityWait as exc:
                        raise StudioError("共享 API 资源正在占用或冷却，尚未发送请求") from exc
                    if receipt.get("provider_job_id"):
                        job.provider_job_id, job.status = (
                            receipt["provider_job_id"],
                            "running",
                        )
            elif self.provider_runtime and job.kind in {"image", "video"}:
                self.provider_runtime.adopt(p, job)
            job.lease_generation += 1
            job.lease_owner = self.owner
            job.lease_until = time.time() + self.settings.lease_seconds
            if self.provider_runtime and job.kind in {"image", "video"} and job.provider_job_id:
                self.provider_runtime.claim_collection(job)

        return self.store.mutate(project_id, claim, kind="job_claimed", advance_revision=False)

    def _begin_submission(self, project_id: str, job_id: str) -> StudioProject | None:
        """Linearize submission start against pause without holding a lock over I/O."""
        allowed = False

        def begin(p: StudioProject) -> None:
            nonlocal allowed
            job = p.jobs[job_id]
            require(
                job.lease_owner == self.owner and job.lease_until > time.time(),
                "任务租约已转移",
            )
            require(job.status in {"queued", "running"}, "任务尚未就绪")
            if job.status == "queued":
                from .playback import set_status as playback_set_status
                from .routing import supports_request

                duration_blocked = False
                route_capacity_blocked = False
                shared_admitted = False
                if (
                    job.kind == "video"
                    and self.provider_runtime
                    and (identifier := self.provider_runtime.receipt_id(job))
                ):
                    from stateweaver.providers.base import provider_config_hash

                    receipt = self.provider_runtime.coordinator.get(identifier)
                    shared_admitted = (
                        receipt["state"] == "reserved"
                        and receipt["owner"] == self.owner
                        and receipt["lease_until"] > time.time()
                        and receipt["generation"] == job.result.get("api_receipt_generation")
                        and receipt["request_hash"]
                        == provider_config_hash(self.provider_runtime.semantic_request(job))
                    )
                if job.kind == "video":
                    candidate = p.candidates[job.target_id]
                    duration_blocked = not playback_set_status(
                        p, p.sets[p.clips[candidate.clip_id].set_id]
                    ).can_submit
                if job.kind == "video" and self.provider_runtime:
                    route = job.request.get("provider_route") or {}
                    routes = self.executor.video_routes(p)
                    actual = routes.get(str(route.get("id")))
                    capacities = self._video_capacities_for(routes)
                    route_capacity_blocked = (
                        not shared_admitted
                        or not actual
                        or route_physical_identity(actual) != route_physical_identity(route)
                        or capacities.get(str(actual["id"]), 0) <= 0
                        or not supports_request(actual, job.request)
                    )
                if (
                    duration_blocked
                    or job.next_poll_at > time.time()
                    or self.stopping
                    or p.deleted_at
                    or p.actors[job.actor_id].paused
                    or (
                        p.actors[job.actor_id].delegate_task_id
                        and self._delegate_job_blocked(p, job)
                    )
                    or (job.kind == "image" and self.runtime.image_cooldown_until() > time.time())
                    or (
                        job.kind == "video"
                        and not self.provider_runtime
                        and self.runtime.video_inflight(()) > self.video_capacity
                    )
                    or route_capacity_blocked
                ):
                    if provider_runtime := getattr(self, "provider_runtime", None):
                        provider_runtime.cancel_unsubmitted(job)
                    job.lease_owner, job.lease_until = None, 0
                    return
                job.status = "submitting"
                job.submitted_at = now()
            allowed = True

        project = self.store.mutate(project_id, begin, kind="job_submission_started")
        return project if allowed else None

    @staticmethod
    def _delegate_job_blocked(project: StudioProject, job: Job) -> bool:
        from .delegation import execution_blockers, task_for

        actor = project.actors[job.actor_id]
        return task_for(project, actor).status not in {"queued", "running"} or bool(
            execution_blockers(project, actor)
        )

    def _execute_claimed_job(self, project_id: str, job_id: str) -> Any:
        project = self._begin_submission(project_id, job_id)
        if project is None:
            return None
        job = project.jobs[job_id]
        started, monotonic = time.time(), time.monotonic()
        success = False
        try:
            try:
                local_outcome = (
                    self.receipts.local_video_outcome(project_id, job)
                    if job.kind == "video" and isinstance(self.executor, JobExecutor)
                    else None
                )
                if local_outcome is not None:
                    outcome = self.executor.execute(project, job, local_video_outcome=local_outcome)
                else:
                    outcome = self.executor.execute(project, job)
            except ProviderResultPublicationError as error:
                self.receipts.save(project_id, job, error.outcome)
                raise
            success = outcome.status != "failed"
            # Publish the returned ID/result before optional telemetry or the
            # event-loop waiter can block. A failed local commit can replay this.
            self.receipts.save(project_id, job, outcome)
        finally:
            phase = {
                "image": "image",
                "reference": "reference",
                "analysis": "evidence",
                "assembly": "assembly",
                "video": "video_io",
            }[job.kind]
            try:
                self.store.metrics.record(
                    project_id,
                    phase,
                    job.kind,
                    started,
                    time.monotonic() - monotonic,
                    actor_id=job.actor_id,
                    success=success,
                )
            except Exception as error:
                logger.error("Job telemetry deferred job=%s type=%s", job.id, type(error).__name__)
        return outcome, job

    async def run_job(self, project_id: str, job_id: str) -> None:
        task = asyncio.current_task()
        assert task is not None
        self.active_job_runs[project_id, job_id] = task
        retry_delay = 0.25
        try:
            while not self.stopping:
                try:
                    retry = await self._run_job(project_id, job_id)
                except ProviderCapacityWait as error:
                    # Collection admission can fail before a local lease is claimed.
                    # Keep one cancelable waiter for the original Job/receipt.
                    if error.reason != "ledger_writer_busy":
                        raise
                    retry = True
                if not retry or self.stopping:
                    return
                await asyncio.sleep(retry_delay)
                retry_delay = min(1.0, retry_delay * 2)
        finally:
            self.active_job_runs.pop((project_id, job_id), None)

    async def _run_job(self, project_id: str, job_id: str) -> bool:
        if self.stopping:
            return False
        try:
            project = await asyncio.to_thread(self._claim_job, project_id, job_id)
        except StudioError as error:
            cause = error.__cause__
            return isinstance(cause, ProviderCapacityWait) and cause.reason == "ledger_writer_busy"
        job = project.jobs[job_id]
        if job.lease_owner != self.owner:
            return False
        returned = False
        try:
            execution = await detached_call(self._execute_claimed_job, project_id, job_id)
            if execution is None:
                return False
            outcome, job = execution
            returned = True

            await asyncio.to_thread(self._complete_job, project_id, job, outcome)
            self.receipts.path(project_id, job).unlink(missing_ok=True)
        except asyncio.CancelledError:
            # Keep the leased intent: after expiry recovery distinguishes known IDs
            # from ambiguous submissions. Never pretend cancellation undid a POST.
            raise
        except Exception as error:
            error_name = type(error).__name__
            safe_detail = safe_provider_text(
                str(error),
                secrets=tuple(
                    value
                    for key, value in self.provider_config.items()
                    if isinstance(value, str)
                    and any(part in key.upper() for part in ("KEY", "TOKEN", "SECRET", "PASSWORD"))
                ),
            )
            definite_failure = not returned and isinstance(
                error, (ProviderContractError, ProviderJobError, ValueError)
            )
            definite_failure = definite_failure or (
                not returned
                and not job.provider_job_id
                and getattr(error, "definitely_not_accepted", False) is True
            )
            from stateweaver.studio_media.submission_errors import ImageSubmissionRejected

            rejected_http_status = (
                error.http_status
                if job.kind == "image" and isinstance(error, ImageSubmissionRejected)
                else None
            )
            rejected_details = (
                dict(error.details) if isinstance(error, ImageSubmissionRejected) else {}
            )
            definite_failure = definite_failure or rejected_http_status is not None
            query_failure = isinstance(error, ProviderQueryUnavailable)
            api_retry = (
                not returned
                and job.kind == "video"
                and self.provider_runtime is not None
                and self.provider_runtime.receipt_id(job) is not None
                and getattr(error, "definitely_not_accepted", False) is True
                and getattr(error, "http_status", None) in {429, 503}
                and not job.provider_job_id
            )
            api_retry_status = getattr(error, "http_status", None)
            api_retry_delay = getattr(error, "retry_after_seconds", None)
            shared_unknown = False
            if self.provider_runtime and self.provider_runtime.receipt_id(job):
                try:
                    shared_state = self.provider_runtime.coordinator.get(
                        str(self.provider_runtime.receipt_id(job))
                    )["state"]
                    shared_unknown = shared_state == "unknown" or (
                        not job.provider_job_id
                        and shared_state in {"submitting", "running", "collecting"}
                    )
                except Exception:
                    shared_unknown = True  # Journal loss never proves a POST was rejected.
            # Technical reposting needs its durable shared receipt. Legacy direct
            # image refusals retain their original failure/cooldown workflow, and
            # an unpublished rejection cannot authorize another send.
            api_retry = api_retry and not shared_unknown
            definite_failure = definite_failure and not shared_unknown
            from stateweaver.providers.coordination import classify_error

            image_error = classify_error(
                error, phase="submission", known_job=bool(job.provider_job_id)
            )
            image_retry = (
                not returned
                and job.kind == "image"
                and self.provider_runtime is not None
                and self.provider_runtime.receipt_id(job) is not None
                and not shared_unknown
                and image_error["retryable"]
                and not job.provider_job_id
                and getattr(error, "request_method", "POST") == "POST"
            )
            image_outcome_known = getattr(error, "outcome_known", False) is True
            capacity_wait = (
                not returned
                and isinstance(error, ProviderCapacityWait)
                and not job.provider_job_id
                and not shared_unknown
            )

            capacity_reason = error.reason if isinstance(error, ProviderCapacityWait) else ""
            capacity_retry_at = error.retry_at if isinstance(error, ProviderCapacityWait) else 0.0

            def failure(p: StudioProject) -> None:

                from .production_recovery import (
                    initialize_policy,
                    mark_terminal_job,
                    schedule_technical_retry,
                )

                current = p.jobs[job_id]
                if (
                    current.lease_owner != self.owner
                    or current.lease_generation != job.lease_generation
                ):
                    return
                current.error = f"{error_name}: {safe_detail}" if safe_detail else error_name
                if rejected_http_status is not None:
                    current.result["submission_failure"] = {
                        "kind": "http_rejected",
                        "http_status": rejected_http_status,
                        "source": "received_http_response",
                        "request_phase": "submission",
                        "failed_at": now(),
                    }
                    if rejected_details:
                        current.result["submission_failure"]["provider_error"] = rejected_details
                    current.result["submission_failure"]["category"] = image_error["category"]
                    current.result["submission_failure"]["effective_http_status"] = image_error[
                        "effective_http_status"
                    ]
                    if rejected_http_status in {429, 503}:
                        current.next_poll_at = (
                            time.time() + self.settings.image_retry.base_delay_seconds
                        )
                        current.result["submission_failure"]["cooldown_until"] = (
                            current.next_poll_at
                        )
                current.lease_owner = None
                current.lease_until = 0
                if capacity_wait:
                    # This is admission wait, not a failed generation. Keep the
                    # registration, model and all repair/fallback allowances.
                    current.result.setdefault("capacity_wait_history", []).append(
                        {
                            "reason": capacity_reason,
                            "observed_at": now(),
                            "retry_after": capacity_retry_at,
                            "local_submission_started_at": current.submitted_at,
                            "same_registration": True,
                            "definitely_not_accepted": True,
                        }
                    )
                    current.status = "queued"
                    current.error = None
                    current.submitted_at = None
                    current.next_poll_at = (
                        time.time() + 0.25
                        if capacity_reason == "ledger_writer_busy"
                        else max(time.time() + 0.5, capacity_retry_at)
                    )
                elif image_retry:
                    from .image_delivery import schedule_capacity_retry

                    if not schedule_capacity_retry(
                        current,
                        primary_model=p.config.image_model,
                        settings=self.settings.image_retry,
                        category=image_error["category"],
                        http_status=rejected_http_status or api_retry_status,
                        retry_after_seconds=api_retry_delay,
                        stamp=time.time(),
                        definitely_not_accepted=image_error["definitely_not_accepted"],
                        outcome_known=image_outcome_known,
                    ):
                        current.status = "failed"
                        self._notify_result(p, current)
                elif api_retry:
                    if not schedule_technical_retry(
                        p,
                        current,
                        self.settings,
                        category="submission_capacity",
                        reason=current.error,
                        stamp=time.time(),
                        definitely_not_accepted=True,
                        retry_after_seconds=api_retry_delay,
                    ):
                        current.status = "failed"
                        self._notify_result(p, current)
                elif current.provider_job_id and (query_failure or not definite_failure):
                    # Keep reserving this task's slot. After the threshold it is
                    # an explicit outage wait, still reconciled on the same ID.
                    current.status = "running"
                    current.poll_attempts += 1
                    current.query_error_count += 1
                    if current.query_error_count >= self.settings.query_error_limit:
                        current.query_outage_since = current.query_outage_since or now()
                        current.next_poll_at = (
                            time.time() + self.settings.query_recovery_poll_seconds
                        )
                    else:
                        current.next_poll_at = time.time() + min(
                            30, 5 * 2**current.query_error_count
                        )
                    # A failed status query is not a failed generation. Keep the
                    # original ID occupied and collect it without an age deadline.
                elif definite_failure:
                    current.status = "failed"
                    if job.kind == "image" and image_error["category"] == "policy_rejection":
                        current.result.setdefault("image_delivery", {})["state"] = "policy_blocked"
                    self._notify_result(p, current)
                elif current.kind in {"image", "video"}:
                    current.status = "ambiguous"
                    initialize_policy(current, self.settings)
                    current.result.setdefault("submission_unknown_since", now())
                    self._notify_result(p, current)
                else:
                    current.status = "failed"
                    self._notify_result(p, current)
                if current.status == "failed":
                    mark_terminal_job(
                        current,
                        category="provider_failure",
                        reason=current.error or "Provider recovery ended without output.",
                    )
                self._reconcile_production(p)

            await asyncio.to_thread(
                self.store.mutate, project_id, failure, actor_id=job.actor_id, kind="job_error"
            )
            # Only explicit local admission failure can re-enter execution. An
            # unknown POST or a returned outcome still follows durable recovery.
            return capacity_wait and capacity_reason == "ledger_writer_busy"
        return False

    def _complete_job(
        self, project_id: str, job: Job, outcome: JobOutcome, *, recovery: bool = False
    ) -> None:
        job_id = job.id

        def complete(p: StudioProject) -> None:
            current = p.jobs[job_id]

            from .candidate_lifecycle import terminal_recovery

            if current.status in {"complete", "failed"}:
                require(
                    request_hash(current) == request_hash(job)
                    and current.lease_generation == job.lease_generation
                    and outcome.status == current.status
                    and current.provider_job_id == (outcome.provider_job_id or job.provider_job_id),
                    "Completed receipt does not match the original task.",
                )
                return
            require(current.lease_generation == job.lease_generation, "任务租约已转移")
            require(current.status in {"submitting", "running", "ambiguous"}, "任务已核对或完成")
            require(recovery or current.lease_owner == self.owner, "任务租约已转移")
            require(
                not recovery or not current.lease_owner or current.lease_until <= time.time(),
                "任务仍有有效租约，等待原执行者回收",
            )
            require(request_hash(current) == request_hash(job), "原任务请求已变化")
            require(
                not current.provider_job_id
                or not outcome.provider_job_id
                or current.provider_job_id == outcome.provider_job_id,
                "远端任务编号与原提交不一致",
            )
            if terminal_recovery(current):
                # Reconciliation may establish the real remote outcome after local
                # closure. Preserve its artifacts without reopening a frozen cut.
                current.result.setdefault("late_outcomes", []).append(
                    {
                        "status": outcome.status,
                        "provider_job_id": outcome.provider_job_id,
                        "result": outcome.result,
                        "media_ids": [media.id for media in outcome.media],
                        "received_at": now(),
                    }
                )
                for media in outcome.media:
                    p.media[media.id] = media
                current.status = outcome.status
                current.provider_job_id = outcome.provider_job_id or current.provider_job_id
                current.lease_owner, current.lease_until = None, 0
                if outcome.status in {"complete", "failed"}:
                    current.completed_at = now()
                else:
                    current.next_poll_at = time.time() + self.settings.query_recovery_poll_seconds
                return
            current.status = outcome.status
            current.provider_job_id = outcome.provider_job_id or current.provider_job_id
            routing_decision = current.result.get("routing_decision")
            shutdown_submission = current.result.get("shutdown_submission")
            original_receipt_fields = {
                key: current.result[key]
                for key in ("api_receipt_id", "provider_route", "video_remote_completion_id")
                if key in current.result
            }
            api_generation = current.result.get("api_receipt_generation")
            technical_retries = current.result.get("technical_retry_history")
            image_delivery = current.result.get("image_delivery")
            production_audit = {
                key: value for key, value in current.result.items() if key.startswith("production_")
            }
            current.result = dict(outcome.result)
            current.result.update(production_audit)
            if image_delivery is not None:
                current.result["image_delivery"] = image_delivery
                if outcome.status == "complete":
                    from .image_delivery import record_success

                    provenance = outcome.result.get("provenance", {})
                    effective_model_name = (
                        provenance.get("effective_model")
                        or current.request.get("api_binding", {}).get("model")
                        or p.config.image_model
                    )
                    actual_model = provenance.get("actual_model") or effective_model_name
                    if actual_model and effective_model_name:
                        record_success(
                            current,
                            effective_model_name=effective_model_name,
                            actual_model=actual_model,
                            actual_model_source=provenance.get("actual_model_source")
                            or "effective_binding",
                            receipt_id=current.request.get("api_receipt_id"),
                        )
                    else:
                        image_delivery["state"] = "complete"
            for key, value in original_receipt_fields.items():
                current.result.setdefault(key, value)
            if api_generation is not None:
                current.result["api_receipt_generation"] = api_generation
            if technical_retries:
                current.result["technical_retry_history"] = technical_retries
            if shutdown_submission:
                current.result["shutdown_submission"] = {
                    **shutdown_submission,
                    "recovered_at": now(),
                }
            if routing_decision:
                current.result["routing_decision"] = routing_decision
            current.error = None
            if job.provider_job_id:
                if current.query_error_count:
                    current.query_recoveries.append(
                        {
                            "outage_since": current.query_outage_since,
                            "error_count": current.query_error_count,
                            "recovered_at": now(),
                            "provider_job_id": current.provider_job_id,
                        }
                    )
                current.query_error_count = 0
                current.query_outage_since = None
                current.last_query_success_at = now()
            current.poll_attempts += 1
            current.next_poll_at = time.time() + min(30, 2 + current.poll_attempts * 2)
            for media in outcome.media:
                p.media[media.id] = media
            if current.status == "complete":
                current.completed_at = now()
                self._apply_result(p, current)
                self._notify_result(p, current)
            elif current.status == "failed":
                current.completed_at = now()
                current.error = str(outcome.result.get("error", "生成服务返回失败"))
                from .production_recovery import mark_terminal_job

                mark_terminal_job(
                    current, category="provider_terminal_failure", reason=current.error
                )
                self._notify_result(p, current)
            elif current.status == "queued":
                current.submitted_at = None
            current.lease_owner = None
            current.lease_until = 0
            self._reconcile_production(p)

        self.store.mutate(
            project_id,
            complete,
            actor_id=job.actor_id,
            kind="job_receipt_recovered" if recovery else "job_updated",
        )

    @staticmethod
    def _reconcile_production(p: StudioProject) -> None:
        from .production_closure import reconcile

        for outcome in reconcile(p):
            actor_id = (
                p.sets[p.clips[outcome["clip_id"]].set_id].actor_id
                if outcome.get("clip_id")
                else next((a.id for a in p.actors.values() if a.role == "executive"), "")
            )
            if actor_id:
                add_item(p, actor_id, "tool", "production_recovery", outcome)

    @staticmethod
    def _apply_result(p: StudioProject, job: Job) -> None:
        if job.kind == "image":
            revision = p.references[job.target_id]
            revision.media_id = str(job.result["media_id"])
            revision.status = "candidate"
            if "source_revision_ids" in job.result:
                revision.source_revision_ids = list(job.result["source_revision_ids"])
        elif job.kind == "video":
            candidate = p.candidates[job.target_id]
            candidate.media_id = str(job.result["media_id"])
            if candidate.disposition and candidate.disposition.kind == "superseded":
                candidate.status = "rejected"
                candidate.analysis["late_result_disposition"] = {
                    "kind": "superseded",
                    "reason": candidate.disposition.reason,
                    "job_id": job.id,
                    "collected_at": now(),
                }
                return
            candidate.status = "review"
            p.clips[candidate.clip_id].status = "review"
            queue_evidence(p, candidate)
        elif job.kind == "analysis":
            candidate = p.candidates[job.target_id]
            candidate.analysis = dict(job.result["analysis"])
            prepare_batches(p, candidate)
            from .image_input_recovery import review_gaps

            candidate.review_gaps = review_gaps(p, candidate)
        elif job.kind == "assembly":
            from .assembly import matches_selected_timeline

            manifest = dict(job.result["manifest"])
            if not matches_selected_timeline(p, manifest):
                # Keep the actual result and original receipt. A concurrent repair
                # or replan may already have changed the cut that Studio serves.
                job.result["assembly_superseded"] = True
                return
            p.final_media_id = str(job.result["media_id"])
            p.export_manifest = manifest
        elif job.kind == "reference" and job.request.get("voice_reference_id"):
            apply_voice_result(p, job)

    @staticmethod
    def _notify_result(p: StudioProject, job: Job) -> None:
        actor = p.actors[job.actor_id]
        if not p.deleted_at and not actor.native_session_blocked:
            actor.request_wake()
            if not actor.paused and actor.status not in {"running", "complete"}:
                actor.status = "ready"
        if (
            not p.deleted_at
            and job.status == "complete"
            and actor.role == "set_director"
            and (
                p.issues
                or bool(
                    select_entities(p.proposals, statuses=["pending", "accepted", "in_progress"])
                )
            )
        ):
            wake_executive(p, "片场结果已返回，请检查待处理的全局提案及修订影响")
        if job.status in {"failed", "ambiguous"}:
            if job.kind == "video":
                candidate = p.candidates[job.target_id]
                if job.status == "failed":
                    candidate.status = "failed"
                p.clips[candidate.clip_id].status = "blocked"
            elif job.kind == "image":
                p.references[job.target_id].status = "failed"
            elif job.kind == "reference" and job.request.get("voice_reference_id"):
                asset = p.assets[str(job.request["voice_asset_id"])]
                for voice in asset.voice_references:
                    if voice.id == job.request["voice_reference_id"]:
                        voice.status = "failed"
            wake_executive(p, "有生成任务失败或提交状态不明，请检查任务记录")
        add_item(
            p,
            job.actor_id,
            "status",
            "参考素材已准备好"
            if job.kind == "reference" and job.status == "complete"
            else "合成返回时选片已变化，旧结果保留。请按当前选片重新合成。"
            if job.kind == "assembly" and job.result.get("assembly_superseded")
            else f"{job.kind} 任务 {job.status}",
            {
                "job_id": job.id,
                "target_id": job.target_id,
                "error": job.error,
                **(
                    {
                        "action": "establish_voice"
                        if job.request.get("voice_reference_id")
                        else "prepare_media"
                        if job.request.get("operation") == "media_excerpt"
                        else "prepare_reference",
                        "asset_id": job.request.get("voice_asset_id"),
                        "voice_reference_id": job.request.get("voice_reference_id"),
                        "media_id": job.result.get("media_id"),
                        "reason": job.result.get("provenance", {}).get("fallback_reason", ""),
                    }
                    if job.kind == "reference"
                    else {}
                ),
            },
        )
