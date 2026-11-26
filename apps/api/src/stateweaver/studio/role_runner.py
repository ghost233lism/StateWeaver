"""First-party role execution using the same Studio task, tool and lease contracts."""

from __future__ import annotations

import asyncio
import base64
import json
import time
from collections import deque
from dataclasses import asdict, dataclass, field, replace
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from pydantic import TypeAdapter
from pydantic_ai.messages import (
    BinaryContent,
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.tools import ToolDefinition

from stateweaver.storage.cas import ContentAddressedStore

from .contracts import Actor, ActorSession, ProductionLanguage, StudioProject, TranscriptItem, now
from .director_efficiency import invalidate_context
from .harness import task_effort
from .mcp import CanvasMCP
from .mcp_inputs import ACTION_MODELS
from .memory import digest
from .native_provider import NativeProviderProxy, RoleResponsePending
from .review_context_recovery import pending_recovery, recovery_record, review_session
from .role_models import (
    RoleLease,
    RoleModelFailed,
    RoleModelPlan,
    RoleModelWait,
    RolePreview,
    RoleResponse,
    StudioRoleModels,
)
from .role_receipts import (
    RoleAttempt,
    RoleReconciliationRequired,
    RoleRunEnded,
    role_rebuild_requires_reconciliation,
    role_response_id,
)
from .runtime_decisions import read_actor_decision
from .service import add_item
from .store import StudioError
from .supervisor import native_goal_objective, release_actor_run
from .tool_descriptions import ALIASES

if TYPE_CHECKING:
    from .supervisor import ActorSupervisor


@dataclass(slots=True)
class RoleJournal:
    epoch: int
    messages: list[str] = field(default_factory=list)
    last_transcript_sequence: int = 0
    awaiting_model: bool = False
    pending_context: str | None = None
    response_group: str | None = None
    response_attempts: int = 0
    receipt_protocol: int = 1
    attempt_receipts: dict[str, dict[str, Any]] = field(default_factory=dict)
    rebuilt_context: bool = False

    @classmethod
    def from_session(cls, session: ActorSession) -> RoleJournal:
        saved = session.role_journal
        if saved.get("epoch") != session.context_epoch:
            if role_rebuild_requires_reconciliation(saved):
                raise RoleReconciliationRequired("旧模型上下文仍有待核对的原回执")
            return cls(session.context_epoch, rebuilt_context=bool(saved))
        if saved.get("receipt_protocol") != 1 or (
            saved.get("awaiting_model")
            and (
                not saved.get("pending_context")
                or not saved.get("response_group")
                or not saved.get("response_attempts")
            )
        ):
            raise RoleReconciliationRequired("当前角色的模型检查点缺少绑定")
        return cls(
            session.context_epoch,
            list(saved["messages"]),
            saved["last_transcript_sequence"],
            bool(saved.get("awaiting_model")),
            saved.get("pending_context"),
            saved.get("response_group"),
            saved.get("response_attempts", 0),
            1,
            dict(saved.get("attempt_receipts", {})),
            bool(saved.get("rebuilt_context")),
        )


@dataclass(frozen=True, slots=True)
class ModelContext:
    system_prompt: str
    tools: list[ToolDefinition]
    effort: str | None
    model_plan: RoleModelPlan
    continuation_instructions: str | None = None


_MODEL_CONTEXT = TypeAdapter(ModelContext)


def production_language_prompt(language: ProductionLanguage) -> str:
    name = "Simplified Chinese" if language == "zh-CN" else "English"
    return (
        f"Current project production language: {name}. Write all new replies, screenplay, "
        "production plans, media prompt descriptions and review reports in this language. "
        "Use it for new dialogue and on-screen text unless the user or confirmed screenplay "
        "specifies a different language. Keep names, quotations, dialogue and on-screen text "
        "already specified by the user or confirmed screenplay in their original language. "
        "Preserve existing work and use it as context. Keep tool names, JSON field names, "
        "machine-readable codes, H3 section names, task labels and reference tags verbatim."
    )


def tool_calls(response: ModelResponse) -> list[ToolCallPart]:
    return [part for part in response.parts if isinstance(part, ToolCallPart)]


def pending_calls(messages: list[ModelMessage]) -> list[ToolCallPart]:
    returned: set[str] = set()
    for message in reversed(messages):
        if isinstance(message, ModelResponse):
            return [call for call in tool_calls(message) if call.tool_call_id not in returned]
        returned.update(
            part.tool_call_id for part in message.parts if isinstance(part, ToolReturnPart)
        )
    return []


def tool_content(result: dict[str, Any]) -> str | list[str | BinaryContent]:
    parts = result.get("content", [])
    if not any(part.get("type") == "image" for part in parts):
        if "structuredContent" in result:
            return json.dumps(result["structuredContent"], ensure_ascii=False)
        return "\n".join(str(part.get("text", "")) for part in parts)
    content: list[str | BinaryContent] = []
    for part in parts:
        if part.get("type") == "text":
            content.append(str(part["text"]))
        if part.get("type") == "image":
            content.append(
                BinaryContent(
                    data=base64.b64decode(part["data"], validate=True), media_type=part["mimeType"]
                )
            )
    return content


class StudioRoleRunner:
    def __init__(
        self,
        supervisor: ActorSupervisor,
        canvas: CanvasMCP,
        proxy: NativeProviderProxy,
    ) -> None:
        self.supervisor = supervisor
        self.store = supervisor.store
        self.canvas = canvas
        self.models = StudioRoleModels(proxy)
        self.journal_assets = ContentAddressedStore(supervisor.settings.work_root / "role-context")

    async def run(self, project_id: str, actor_id: str) -> None:
        supervisor = self.supervisor
        try:
            project = await asyncio.to_thread(supervisor._claim_actor, project_id, actor_id)
        except StudioError:
            return
        actor = project.actors[actor_id]
        started = time.monotonic()
        wall_time = time.time()
        interrupted = False
        failure: Exception | None = None
        try:
            instructions, skill_hash = await asyncio.to_thread(
                supervisor._instructions, actor, project
            )
            lease, turn, journal = await asyncio.to_thread(
                self._start_turn, project, actor, skill_hash
            )
            if pending_recovery(project, actor):
                lease, journal = await asyncio.to_thread(
                    self._recover_review_context, lease, journal
                )
            await self._take_turn(
                lease,
                turn,
                journal,
                instructions,
                native_goal_objective(actor, project=project),
                repeat_limit=project.config.no_progress_turns,
            )
        except RoleRunEnded:
            interrupted = True
        except asyncio.CancelledError:
            interrupted = True
            raise
        except Exception as exc:
            failure = exc
            await asyncio.to_thread(self._record_failure, project_id, actor, exc)
        finally:
            released = await asyncio.to_thread(
                release_actor_run,
                self.store,
                project_id,
                actor_id,
                supervisor.owner,
                actor.lease_generation,
                wait_reason=supervisor.wait_reason,
            )
            key = (project_id, actor_id)
            if supervisor.actor_generations.get(key) == actor.lease_generation:
                supervisor.actor_generations.pop(key, None)
            session = actor.sessions.get(actor.session_key)
            phase = "review" if session and session.purpose == "candidate_review" else "director"
            await asyncio.to_thread(
                self.store.metrics.record,
                project_id,
                phase,
                actor.role,
                wall_time,
                time.monotonic() - started,
                actor_id=actor_id,
                success=not interrupted
                and failure is None
                and released.actors[actor_id].status != "blocked",
                details={
                    "runtime": "agentvideo-v2",
                    **({"outcome": "interrupted"} if interrupted else {}),
                },
            )

    def _start_turn(
        self, project: StudioProject, actor: Actor, skill_hash: str
    ) -> tuple[RoleLease, str, RoleJournal]:
        turn = "av2-turn-" + uuid4().hex

        def start(current: StudioProject) -> tuple[RoleLease, RoleJournal]:
            state = current.actors[actor.id]
            if (
                state.lease_owner != self.supervisor.owner
                or state.lease_generation != actor.lease_generation
                or state.lease_until <= time.time()
                or state.paused
                or state.status != "running"
            ):
                raise RoleRunEnded()
            session = state.sessions.setdefault(
                state.session_key, ActorSession(key=state.session_key)
            )
            if not session.thread_id or not session.thread_id.startswith("av2-"):
                session.thread_id = "av2-" + uuid4().hex
            state.thread_id = session.thread_id
            state.skill_hash = skill_hash
            state.turn_id = turn
            lease = RoleLease(
                project.id,
                actor.id,
                self.supervisor.owner,
                actor.lease_generation,
                state.session_key,
                state.context_epoch,
                state.thread_id,
            )
            lease.active(current)
            journal = RoleJournal.from_session(session)
            if session.role_journal.get("epoch") != session.context_epoch:
                self._interrupt_group(
                    current,
                    session.role_journal.get("response_group"),
                    session.role_journal.get("response_attempts", 0),
                )
            return lease, journal

        # StudioStore.mutate returns the domain state. Capture the atomic claim
        # result inside the transaction without a second unfenced lookup.
        started: list[tuple[RoleLease, RoleJournal]] = []

        def initialize(current: StudioProject) -> None:
            started.append(start(current))

        self.store.mutate(
            project.id,
            initialize,
            actor_id=actor.id,
            kind="role_turn_started",
            advance_revision=False,
        )
        lease, journal = started[0]
        return lease, turn, journal

    def _recover_review_context(
        self, lease: RoleLease, journal: RoleJournal
    ) -> tuple[RoleLease, RoleJournal]:
        def current(p: StudioProject) -> tuple[Actor, ActorSession, dict[str, Any]] | None:
            actor = lease.active(p)
            session = review_session(p, actor)
            record = recovery_record(session) if session else None
            if (
                not session
                or not record
                or record.get("status") != "pending"
                or record.get("thread_id") != lease.thread_id
                or record.get("session_key") != lease.session_key
                or record.get("candidate_id") != session.candidate_id
            ):
                return None
            if RoleJournal.from_session(session) != journal:
                raise RoleRunEnded()
            return actor, session, record

        if current(self.store.get(lease.project_id)) is None:
            return lease, journal
        if role_rebuild_requires_reconciliation(asdict(journal)):
            raise RoleReconciliationRequired("审核上下文仍有待核对的原模型响应")
        # The overflow was raised at a model boundary after tool returns were
        # checkpointed. Keep the previous journal and all actual business results.
        if pending_calls(self._read_messages(journal)):
            raise StudioError("Review recovery cannot supersede unsettled role tool calls")
        archived = self.journal_assets.put_bytes(json.dumps(asdict(journal)).encode()).sha256
        recovered: tuple[RoleLease, RoleJournal] | None = None

        def recover(p: StudioProject) -> None:
            nonlocal recovered
            context = current(p)
            if context is None:
                raise RoleRunEnded()
            actor, session, record = context
            record["prior_image_receipts"] = dict(session.image_receipts)
            self._interrupt_group(p, journal.response_group, journal.response_attempts)
            invalidate_context(p, actor, reason="v2_review_context_recovery")
            updated = RoleJournal(
                session.context_epoch,
                last_transcript_sequence=journal.last_transcript_sequence,
                rebuilt_context=True,
            )
            session.role_journal = asdict(updated)
            record.update(
                status="completed",
                runtime="agentvideo-v2",
                completed_at=now(),
                prior_role_journal_sha256=archived,
                context_epoch=session.context_epoch,
                required_evidence_must_be_reinspected=True,
            )
            actor.summary = "审核上下文已重新打开，请复查实际证据"
            add_item(
                p,
                actor.id,
                "user",
                "Continue the same candidate review after context overflow. Original tool actions "
                "and business records are retained. Open the current review contract, inspect all "
                "required actual evidence again, including supplements, exact references and the "
                "predecessor boundary where required, then record a verdict. Historical inspection "
                "receipts and earlier summaries cannot replace current visual evidence.",
                {"runtime": "agentvideo-v2", "review_context_recovered": True},
            )
            recovered = replace(lease, context_epoch=actor.context_epoch), updated

        self.store.mutate(
            lease.project_id,
            recover,
            actor_id=lease.actor_id,
            kind="review_context_recovery.completed",
            advance_revision=False,
        )
        assert recovered is not None
        return recovered

    def _read_messages(self, journal: RoleJournal) -> list[ModelMessage]:
        messages: list[ModelMessage] = []
        try:
            for sha256 in journal.messages:
                raw = self.journal_assets.read_bytes(sha256)
                messages.extend(ModelMessagesTypeAdapter.validate_json(raw))
        except (OSError, TypeError, ValueError) as error:
            if journal.awaiting_model:
                raise RoleReconciliationRequired("待恢复的原模型消息文件缺失或损坏") from error
            raise
        return messages

    def _append(
        self,
        lease: RoleLease,
        journal: RoleJournal,
        message: ModelMessage,
        *,
        item: TranscriptItem | None = None,
        transcript_sequence: int | None = None,
        model_output: bool = False,
    ) -> None:
        asset = self.journal_assets.put_bytes(ModelMessagesTypeAdapter.dump_json([message]))
        updated = replace(
            journal,
            messages=[*journal.messages, asset.sha256],
            last_transcript_sequence=journal.last_transcript_sequence
            if transcript_sequence is None
            else transcript_sequence,
            awaiting_model=False if model_output else journal.awaiting_model,
            pending_context=None if model_output else journal.pending_context,
            response_group=None if model_output else journal.response_group,
            response_attempts=0 if model_output else journal.response_attempts,
            attempt_receipts={} if model_output else journal.attempt_receipts,
            rebuilt_context=False if model_output else journal.rebuilt_context,
        )

        self._save_journal(lease, journal, updated, item=item, require_active=model_output)

    def _save_journal(
        self,
        lease: RoleLease,
        journal: RoleJournal,
        updated: RoleJournal,
        *,
        item: TranscriptItem | None = None,
        require_active: bool = False,
    ) -> None:
        if item is not None:
            # Publish immutable bytes before the guarded transcript commit.
            self.store.prepare_body({"text": item.text, "data": item.data})

        def save(p: StudioProject) -> None:
            actor = lease.active(p) if require_active else lease.owned(p)
            session = actor.sessions[lease.session_key]
            if RoleJournal.from_session(session) != journal:
                raise RoleRunEnded()
            session.role_journal = asdict(updated)
            if journal.response_group and (
                not updated.awaiting_model or journal.response_group != updated.response_group
            ):
                self._interrupt_group(
                    p, journal.response_group, journal.response_attempts, item.id if item else None
                )
            if item is not None:
                p.transcript[item.id] = item

        self.store.mutate(
            lease.project_id,
            save,
            actor_id=lease.actor_id,
            kind="role_checkpoint",
            advance_revision=False,
        )
        journal.messages = updated.messages
        journal.last_transcript_sequence = updated.last_transcript_sequence
        journal.awaiting_model = updated.awaiting_model
        journal.pending_context = updated.pending_context
        journal.response_group = updated.response_group
        journal.response_attempts = updated.response_attempts
        journal.receipt_protocol = updated.receipt_protocol
        journal.attempt_receipts = updated.attempt_receipts
        journal.rebuilt_context = updated.rebuilt_context

    @staticmethod
    def _interrupt_preview(project: StudioProject, item: TranscriptItem) -> None:
        if item.status == "running" and item.data.get("runtime") == "agentvideo-v2":
            item.status = "failed"
            item.data["preview_interrupted"] = True
            project.transcript[item.id] = item

    def _interrupt_group(
        self, project: StudioProject, group: str | None, attempts: int, keep: str | None = None
    ) -> None:
        if group:
            for attempt in range(attempts):
                identifier = f"av2-message-{group}-{attempt}"
                if identifier != keep and (item := project.transcript.get(identifier)):
                    self._interrupt_preview(project, item)

    def _new_inputs(self, lease: RoleLease, after: int) -> tuple[int, list[str]]:
        upto, before = after, None
        relevant: list[TranscriptItem] = []
        while True:
            page = self.store.transcript_page(lease.project_id, lease.actor_id, before=before)
            if not page.items:
                break
            upto = max(upto, page.items[-1].sequence)
            relevant.extend(
                item
                for item in reversed(page.items)
                if item.sequence > after and item.kind == "user"
            )
            if not page.has_more or page.items[0].sequence <= after:
                break
            before = page.next_cursor
        inputs = []
        for item in reversed(relevant):
            if item.body_ref and item.text_length and item.text_length > len(item.text):
                offset: int | None = 0
                body = []
                while offset is not None:
                    chunk = self.store.body(lease.project_id, item.body_ref, offset)
                    body.append(chunk.text)
                    offset = chunk.next_offset
                inputs.append(str(json.loads("".join(body))["text"]))
            else:
                inputs.append(item.text)
        return upto, inputs

    def _canvas(self, lease: RoleLease, method: str, params: dict[str, Any]) -> dict[str, Any]:
        lease.active(self.store.get(lease.project_id))
        reply = self.canvas.handle(
            lease.project_id,
            lease.actor_id,
            {"jsonrpc": "2.0", "id": "studio-role-v2", "method": method, "params": params},
            lease_generation=lease.generation,
            lease_owner=lease.owner,
        )
        if not reply or not isinstance(reply.get("result"), dict):
            raise StudioError("角色工具未返回可用结果")
        return dict(reply["result"])

    def _model_context(
        self,
        lease: RoleLease,
        journal: RoleJournal,
        system_prompt: str,
        tools: list[ToolDefinition],
    ) -> ModelContext:
        if journal.pending_context:
            try:
                context = _MODEL_CONTEXT.validate_json(
                    self.journal_assets.read_bytes(journal.pending_context)
                )
            except (OSError, TypeError, ValueError) as error:
                raise RoleReconciliationRequired("待恢复的原模型上下文文件缺失或损坏") from error
            return context
        if role_rebuild_requires_reconciliation(asdict(journal)):
            raise RoleReconciliationRequired("缺少待恢复的原模型上下文")
        project = self.store.get(lease.project_id)
        actor = lease.active(project)
        plan = self.models.plan(project, actor)
        instructions = production_language_prompt(project.config.production_language)
        context = ModelContext(
            system_prompt + "\n\n" + instructions,
            tools,
            task_effort(project, actor),
            plan,
            continuation_instructions=instructions,
        )
        context_ref = self.journal_assets.put_bytes(_MODEL_CONTEXT.dump_json(context)).sha256
        self._save_journal(
            lease,
            journal,
            replace(
                journal,
                awaiting_model=True,
                pending_context=context_ref,
                response_group=uuid4().hex,
                response_attempts=plan.retry.max_retries + 1,
                receipt_protocol=1,
            ),
            require_active=True,
        )
        return context

    async def _take_turn(
        self,
        lease: RoleLease,
        turn: str,
        journal: RoleJournal,
        instructions: str,
        objective: str,
        *,
        repeat_limit: int,
    ) -> None:
        messages = await asyncio.to_thread(self._read_messages, journal)
        progress_start: int | None = None
        wake_start: int | None = None
        decision = None
        tools: dict[str, ToolDefinition] | None = None
        last_input_wake: int | None = None
        fresh_prompt = True
        limit = max(2, repeat_limit)
        recent_tools: deque[str] = deque(maxlen=4 * limit)
        repeated = False
        tool_progress: tuple[int, int] | None = None
        system_prompt = (
            instructions + "\n\n" + objective + "\n"
            "Use only the listed Studio tools. Persisted Studio state determines task "
            "completion. Use the normal role actions for waits and progress. "
            "Disclose missing evidence and never claim to have inspected unseen media."
        )
        while not self.supervisor.stopping:
            current, updated_decision = await asyncio.to_thread(
                read_actor_decision,
                self.store,
                lease.project_id,
                lease.actor_id,
                self.supervisor.wait_reason,
                decision,
            )
            schema_changed = updated_decision is not decision
            decision = updated_decision
            if current.status != "running" or decision.session_key != lease.session_key:
                return
            if progress_start is None:
                progress_start, wake_start = current.progress_version, current.wake_version
            if tools is None or schema_changed:
                canvas = await asyncio.to_thread(self._canvas, lease, "tools/list", {})
                tools = {
                    item["name"]: ToolDefinition(
                        name=item["name"],
                        description=item.get("description"),
                        parameters_json_schema=item["inputSchema"],
                        strict=False,
                    )
                    for item in canvas["tools"]
                }
            progress = current.progress_version, current.wake_version
            if progress != tool_progress:
                recent_tools.clear()
                repeated = False
            if calls := pending_calls(messages):
                # Settle the full returned call batch before giving the scheduler
                # a chance to apply its usual no-progress recovery policy.
                signature = await self._run_tool(lease, journal, messages, calls[0], tools)
                if signature is None:
                    recent_tools.clear()
                    repeated = False
                else:
                    recent_tools.append(signature)
                    repeated |= recent_tools.count(signature) >= limit
                tool_progress = progress
                continue
            if repeated:
                await asyncio.to_thread(
                    self._finish_turn, lease, turn, progress_start, wake_start, stalled=True
                )
                return
            sequence = journal.last_transcript_sequence
            user_inputs: list[str] = []
            resuming_model = journal.awaiting_model
            if not resuming_model and (fresh_prompt or last_input_wake != current.wake_version):
                sequence, user_inputs = await asyncio.to_thread(
                    self._new_inputs, lease, journal.last_transcript_sequence
                )
                last_input_wake = current.wake_version
                wake_start = last_input_wake
            if user_inputs or (fresh_prompt and not resuming_model):
                prompts = user_inputs or [
                    "Read current Studio state and continue the assigned work."
                ]
                request = ModelRequest(parts=[UserPromptPart(prompt) for prompt in prompts])
                await asyncio.to_thread(
                    self._append, lease, journal, request, transcript_sequence=sequence
                )
                messages.append(request)
            fresh_prompt = False
            context = await asyncio.to_thread(
                self._model_context, lease, journal, system_prompt, list(tools.values())
            )
            system = ModelRequest(parts=[SystemPromptPart(context.system_prompt)])

            async def publish(attempt: int, model: str, text: str, failed: bool) -> None:
                await asyncio.to_thread(
                    self._preview,
                    lease,
                    f"av2-message-{journal.response_group}-{attempt}",
                    model,
                    text,
                    failed,
                )

            output = await self._request_model(
                lease, turn, journal, [system, *messages], context, preview=publish
            )
            assert journal.response_group is not None
            text = "\n".join(
                part.content for part in output.message.parts if isinstance(part, TextPart)
            )
            item = (
                TranscriptItem(
                    id=f"av2-message-{journal.response_group}-{output.attempt}",
                    actor_id=lease.actor_id,
                    kind="message" if text.strip() else "status",
                    text=text if text.strip() else "角色模型重试后返回工具操作",
                    data={
                        "runtime": "agentvideo-v2",
                        "model": output.model,
                        "failures": [asdict(attempt) for attempt in output.failures],
                        "usage": (output.message.provider_details or {}).get(
                            "agentvideo_reported_usage"
                        ),
                    },
                )
                if text.strip() or output.failures
                else None
            )
            await asyncio.to_thread(
                self._append, lease, journal, output.message, item=item, model_output=True
            )
            messages.append(output.message)
            if not tool_calls(output.message):
                if resuming_model:
                    _, unread = await asyncio.to_thread(
                        self._new_inputs, lease, journal.last_transcript_sequence
                    )
                    if unread:
                        continue
                await asyncio.to_thread(self._finish_turn, lease, turn, progress_start, wake_start)
                return

    async def _request_model(
        self,
        lease: RoleLease,
        turn: str,
        journal: RoleJournal,
        messages: list[ModelMessage],
        context: ModelContext,
        *,
        preview: RolePreview,
    ) -> RoleResponse:
        """Keep collecting the owned request while pause stops new actions."""

        def checkpoint(attempt: int) -> RoleAttempt:
            key = str(attempt)

            def save(receipt: dict[str, Any]) -> None:
                previous = journal.attempt_receipts.get(key)
                if previous == receipt:
                    return
                updated = replace(
                    journal, attempt_receipts={**journal.attempt_receipts, key: receipt}
                )
                same_admission = (
                    previous is not None
                    and previous["id"] == receipt["id"]
                    and previous["generation"] == receipt["generation"]
                    and receipt["state"] != "reserved"
                )
                try:
                    # Publication can finish while paused once this exact admission
                    # belongs to the turn, even if the result has no provider ID.
                    self._save_journal(
                        lease,
                        journal,
                        updated,
                        require_active=not same_admission and role_response_id(receipt) is None,
                    )
                except (RoleRunEnded, RoleReconciliationRequired):
                    raise
                except Exception as error:
                    raise RoleResponsePending("Role attempt checkpoint is awaiting save") from error

            return RoleAttempt(
                journal.attempt_receipts.get(key),
                save,
                check_previous_context=journal.rebuilt_context,
                reject_result=lambda response_id: self._reject_model_result(
                    lease, journal, key, response_id
                ),
            )

        task = asyncio.create_task(
            self.models.request(
                lease,
                messages,
                context.tools,
                preview=preview,
                effort=context.effort,
                group=journal.response_group,
                checkpoint=checkpoint,
                plan=context.model_plan,
                continuation_instructions=context.continuation_instructions,
            )
        )
        draining = False
        try:
            while True:
                done, _ = await asyncio.wait(
                    {task}, timeout=self.supervisor.settings.actor_state_poll_seconds
                )
                if done:
                    output = task.result()
                    if draining:
                        raise RoleRunEnded()
                    return output
                if self.supervisor.stopping:
                    raise RoleRunEnded()
                current = await asyncio.to_thread(self.store.get, lease.project_id)
                actor = lease.owned(current)
                if actor.turn_id != turn:
                    raise RoleRunEnded()
                parent = current.actors.get(actor.parent_actor_id or "")
                # A quick resume can already have switched the actor to ready
                # without releasing this turn. Let its original wire call settle.
                if actor.paused or (parent and parent.paused) or actor.status == "ready":
                    draining = True
                elif actor.status != "running":
                    raise RoleRunEnded()
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    def _reject_model_result(
        self, lease: RoleLease, journal: RoleJournal, key: str, response_id: str
    ) -> None:
        receipt = journal.attempt_receipts.get(key)
        if (
            not receipt
            or receipt.get("state") != "complete"
            or receipt.get("provider_job_id") != response_id
        ):
            raise RoleResponsePending("Invalid role result has no matching terminal receipt")
        item = TranscriptItem(
            id=f"av2-rejected-result-{receipt['id']}-{receipt['generation']}",
            actor_id=lease.actor_id,
            kind="error",
            text="角色模型已完成，返回结果无法使用",
            data={
                "runtime": "agentvideo-v2",
                "model": receipt["model"],
                "api_receipt_id": receipt["id"],
                "generation": receipt["generation"],
                "provider_response_id": response_id,
            },
        )
        try:
            self._save_journal(
                lease,
                journal,
                replace(
                    journal,
                    attempt_receipts={
                        **journal.attempt_receipts,
                        key: {**receipt, "rejected_result": True},
                    },
                ),
                item=item,
            )
        except (RoleRunEnded, RoleReconciliationRequired):
            raise
        except Exception as error:
            raise RoleResponsePending("Invalid role result checkpoint is awaiting save") from error

    def _preview(
        self, lease: RoleLease, identifier: str, model: str, text: str, failed: bool
    ) -> None:
        def save(p: StudioProject) -> None:
            if failed:
                lease.owned(p)
            else:
                lease.active(p)
            existing = p.transcript.get(identifier)
            item = existing or TranscriptItem(
                id=identifier, actor_id=lease.actor_id, kind="message"
            )
            item.text = text
            item.status = "failed" if failed else "running"
            item.data = {
                "runtime": "agentvideo-v2",
                "model": model,
                "attempt_failed": failed,
            }
            p.transcript[identifier] = item

        try:
            self.store.mutate(
                lease.project_id,
                save,
                actor_id=lease.actor_id,
                kind="role_text_progress",
                advance_revision=False,
            )
        except RoleRunEnded:
            return

    async def _run_tool(
        self,
        lease: RoleLease,
        journal: RoleJournal,
        messages: list[ModelMessage],
        call: ToolCallPart,
        tools: dict[str, ToolDefinition],
    ) -> str | None:
        schema = tools.get(call.tool_name)
        try:
            args = dict(call.args_as_dict(raise_if_invalid=True))
        except (TypeError, ValueError, AssertionError):
            # Older checkpoints may already contain a malformed tool call.
            args = None
        response_index = max(i for i, msg in enumerate(messages) if isinstance(msg, ModelResponse))
        identifier = digest([lease.thread_id, journal.messages[response_index], call.tool_call_id])
        if args is None:
            args = {}
            result = {
                "isError": True,
                "structuredContent": {"error": "工具参数必须是有效的 JSON 对象"},
            }
        elif schema is None:
            result = {"isError": True, "structuredContent": {"error": "工具不属于当前角色和任务"}}
        else:
            action = ALIASES.get(call.tool_name, call.tool_name)
            if (action in ACTION_MODELS or call.tool_name == "act") and (
                "idempotency_key" in schema.parameters_json_schema.get("properties", {})
            ):
                # Checkpointed model outputs recover their exact operator key.
                args.setdefault("idempotency_key", "av2-" + identifier)
            result = await asyncio.to_thread(
                self._canvas, lease, "tools/call", {"name": call.tool_name, "arguments": args}
            )
        content = tool_content(result)
        request = ModelRequest(
            parts=[ToolReturnPart(call.tool_name, content, tool_call_id=call.tool_call_id)]
        )
        item = TranscriptItem(
            id="av2-tool-" + identifier,
            actor_id=lease.actor_id,
            kind="tool",
            text=call.tool_name,
            status="failed" if result.get("isError") else "complete",
            data={
                "type": "mcpToolCall",
                "tool": call.tool_name,
                "runtime": "agentvideo-v2",
                "arguments": args,
                "result": result.get("structuredContent"),
            },
        )
        await asyncio.to_thread(self._append, lease, journal, request, item=item)
        messages.append(request)
        if not isinstance(content, str):
            return None  # Actual media delivery has its own progress and receipt semantics.
        response = result.get("structuredContent", content)
        if isinstance(response, dict):
            response = {
                k: v for k, v in response.items() if k not in {"read_token", "context_token"}
            }
        return digest(
            [call.tool_name, {k: v for k, v in args.items() if k != "idempotency_key"}, response]
        )

    def _finish_turn(
        self,
        lease: RoleLease,
        turn: str,
        previous_progress: int,
        wake_start: int | None,
        *,
        stalled: bool = False,
    ) -> None:
        def settle_writer(p: StudioProject) -> None:
            actor = lease.owned(p)
            if (
                actor.role == "screenwriter"
                and actor.wake_version == wake_start
                and not actor.paused
            ):
                actor.status, actor.wake_requested = "waiting", False

        if not stalled:
            self.store.mutate(
                lease.project_id,
                settle_writer,
                actor_id=lease.actor_id,
                kind="role_turn_settling",
                advance_revision=False,
            )
        self.supervisor.settle_turn(
            lease.project_id,
            lease.actor_id,
            previous_progress,
            turn,
            "av2:" + turn,
            {"id": turn, "status": "completed"},
            lease_generation=lease.generation,
            require_current_turn=True,
        )

    def _record_failure(self, project_id: str, actor: Actor, failure: Exception) -> None:
        def save(p: StudioProject) -> None:
            current = p.actors[actor.id]
            parent = p.actors.get(current.parent_actor_id or "")
            if (
                current.lease_owner != self.supervisor.owner
                or current.lease_generation != actor.lease_generation
                or current.lease_until <= time.time()
                or current.status == "complete"
                or current.paused
                or (parent and parent.paused)
            ):
                return
            if isinstance(failure, RoleResponsePending):
                settings = self.supervisor.settings
                delay = max(1.0, settings.actor_recovery_delay_seconds)
                if failure.query_http_status == 400:
                    delay = max(delay, settings.query_recovery_poll_seconds)
                current.recovery_not_before = time.time() + delay
                current.status = "ready"
                current.summary = "等待回收原模型响应"
                if failure.query_http_status is not None:
                    current.summary += f"（查询 HTTP {failure.query_http_status}）"
                current.request_wake()
                return
            if isinstance(failure, RoleModelWait):
                current.recovery_not_before = failure.retry_at
                current.status = "ready"
                current.summary = "等待同一模型服务重试"
                current.request_wake()
                return
            session = current.sessions.get(current.session_key)
            if isinstance(failure, RoleReconciliationRequired) or (
                session is not None and role_rebuild_requires_reconciliation(session.role_journal)
            ):
                current.status = "blocked"
                current.recovery_not_before = 0
                current.wake_requested = False
                current.last_error = (
                    str(failure)
                    if isinstance(failure, RoleReconciliationRequired)
                    else "原模型请求结果仍不确定"
                )
                current.summary = current.last_error + "。保留原回执，核对后继续。"
                add_item(p, current.id, "error", current.summary, {"runtime": "agentvideo-v2"})
                return
            if isinstance(failure, RoleModelFailed):
                hint = next(
                    (
                        item.recovery_hint
                        for item in reversed(failure.failures)
                        if item.recovery_hint
                    ),
                    None,
                )
                if hint is not None:
                    self.supervisor._recover_actor(p, current, hint)
                else:
                    current.status = "blocked"
                    current.recovery_not_before = 0
                    current.wake_requested = False
                    current.last_error = current.summary = str(failure)
                attempts = [asdict(attempt) for attempt in failure.failures]
            else:
                self.supervisor._recover_actor(p, current, type(failure).__name__)
                attempts = []
            item = TranscriptItem(
                id="av2-error-" + uuid4().hex,
                actor_id=current.id,
                kind="error",
                text="角色执行未完成",
                data={"runtime": "agentvideo-v2", "attempts": attempts},
            )
            p.transcript[item.id] = item

        self.store.mutate(
            project_id, save, actor_id=actor.id, kind="role_execution_error", advance_revision=False
        )
