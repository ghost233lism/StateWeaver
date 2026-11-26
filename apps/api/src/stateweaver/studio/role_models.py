"""Fixed-service role inference and bounded, receipt-backed retries."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import httpx
from openai import AsyncOpenAI
from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelResponse,
    TextPart,
    ToolCallPart,
)
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.openai import OpenAIResponsesModel, OpenAIResponsesModelSettings
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.usage import RequestUsage

from stateweaver.providers.audit import numeric_usage
from stateweaver.providers.base import ProviderContractError
from stateweaver.providers.coordination import ProviderCapacityWait

from .contracts import Actor, StudioProject
from .native_provider import (
    NativeProviderProxy,
    NativeSSEEvents,
    RoleModelRetry,
    RoleProviderFailure,
    RoleResponsePending,
)
from .role_receipts import (
    RoleAttempt,
    RoleReconciliationRequired,
    RoleRunEnded,
    original_role_receipt,
    rejected_role_result,
)
from .settings import RetryPolicy


@dataclass(frozen=True, slots=True)
class RoleLease:
    project_id: str
    actor_id: str
    owner: str
    generation: int
    session_key: str
    context_epoch: int
    thread_id: str

    def owned(self, project: StudioProject) -> Actor:
        actor = project.actors.get(self.actor_id)
        if actor is None:
            raise RoleRunEnded()
        if (
            project.deleted_at
            or actor.lease_owner != self.owner
            or actor.lease_generation != self.generation
            or actor.lease_until <= time.time()
            or actor.session_key != self.session_key
            or actor.context_epoch != self.context_epoch
            or actor.thread_id != self.thread_id
        ):
            raise RoleRunEnded()
        return actor

    def active(self, project: StudioProject) -> Actor:
        actor = self.owned(project)
        parent = project.actors.get(actor.parent_actor_id or "")
        if actor.paused or (parent and parent.paused) or actor.status != "running":
            raise RoleRunEnded()
        return actor


@dataclass(frozen=True, slots=True)
class RoleModelFailure:
    model: str
    error_type: str
    recovery_hint: str | None = None


class RoleModelFailed(RuntimeError):
    def __init__(self, failures: list[RoleModelFailure]) -> None:
        super().__init__("固定角色模型未返回可用结果")
        self.failures = failures


class RoleModelWait(RuntimeError):
    def __init__(self, retry_at: float) -> None:
        super().__init__("等待固定角色模型服务")
        self.retry_at = retry_at


@dataclass(frozen=True, slots=True)
class RoleModelPlan:
    model: str
    retry: RetryPolicy


class RoleInvalidResult(ProviderContractError):
    """An exact completed model response failed local output validation."""


def _reported_usage(value: object) -> dict[str, Any] | None:
    """Map only observed Responses counters, retaining omitted fields as unknown."""
    reported = numeric_usage(value)
    if reported is None:
        return None
    input_details = reported.get("input_tokens_details")
    output_details = reported.get("output_tokens_details")
    input_details = input_details if isinstance(input_details, dict) else {}
    output_details = output_details if isinstance(output_details, dict) else {}
    usage: dict[str, Any] = {
        key: count
        for key, count in {
            "input_tokens": reported.get("input_tokens"),
            "output_tokens": reported.get("output_tokens"),
            "total_tokens": reported.get("total_tokens"),
            "cache_read_tokens": input_details.get("cached_tokens"),
            "cache_write_tokens": input_details.get("cache_write_tokens"),
        }.items()
        if type(count) is int
    }
    if type(reasoning := output_details.get("reasoning_tokens")) is int:
        usage["details"] = {"reasoning_tokens": reasoning}
    return usage or None


type RolePreview = Callable[[int, str, str, bool], Awaitable[None]]


class RoleTextStream:
    def __init__(self, preview: RolePreview, model: str, attempt: int) -> None:
        self.preview, self.model, self.attempt = preview, model, attempt
        self.events = NativeSSEEvents()
        self.text = ""
        self.published_at = 0.0

    async def feed(self, chunk: bytes) -> None:
        updated = False
        for event in self.events.feed(chunk):
            event_type, conflicting = event.role_type()
            if (
                not conflicting
                and event_type == "response.output_text.delta"
                and isinstance(event.value, dict)
            ):
                delta = event.value.get("delta")
                if isinstance(delta, str) and delta:
                    self.text += delta
                    updated = True
        if updated and time.monotonic() - self.published_at >= 0.4:
            self.published_at = time.monotonic()
            await self.preview(self.attempt, self.model, self.text, False)

    async def fail(self) -> None:
        if self.text.strip():
            with suppress(Exception):
                await self.preview(self.attempt, self.model, self.text, True)


class RoleResponsesTransport(httpx.AsyncBaseTransport):
    """Adapt one stateless Responses request to Studio's existing transport."""

    def __init__(
        self,
        proxy: NativeProviderProxy,
        lease: RoleLease,
        model: str,
        effort: str | None,
        group: str,
        attempt: int,
        text_stream: RoleTextStream | None,
        checkpoint: RoleAttempt | None,
        continuation_instructions: str | None,
    ) -> None:
        self.proxy = proxy
        self.lease = lease
        self.model = model
        self.effort = effort
        self.group = group
        self.attempt = attempt
        self.text_stream = text_stream
        self.checkpoint = checkpoint
        self.continuation_instructions = continuation_instructions
        self.failure: Exception | None = None
        self.reported_usage: dict[str, Any] | None = None
        self.completed_response_id: str | None = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        try:
            return await self._forward(request)
        except Exception as error:
            # The model SDK wraps transport errors as generic connection errors.
            # Retain the local typed result so capacity timing and fences survive.
            self.failure = error
            raise

    async def _forward(self, request: httpx.Request) -> httpx.Response:
        if request.method != "POST" or request.url.path != "/responses":
            raise ValueError("V2 role transport supports only Responses submissions")
        project = await self.proxy.run_io(self.proxy.service.store.get, self.lease.project_id)
        actor = self.lease.active(project)
        wire = json.loads(await request.aread())
        # Previous IDs are chosen only for the same model and owned session.
        # The proxy validates the original provider receipt before forwarding.
        # The SDK drops leading system messages when it sends a previous response ID.
        # The compact, checkpointed language instruction still governs the new turn.
        if wire.get("previous_response_id") and self.continuation_instructions:
            wire["instructions"] = self.continuation_instructions
        if self.text_stream is not None:
            wire["stream"] = True
        if not self.model.startswith(("gt-", "gpt-")):
            wire.pop("reasoning", None)
        elif self.effort is not None:
            wire["reasoning"] = {**wire.get("reasoning", {}), "effort": self.effort}
        payload = await self.proxy.role_attempt(
            project,
            actor,
            json.dumps(wire, ensure_ascii=False, separators=(",", ":")).encode(),
            dict(request.headers),
            model=self.model,
            group=self.group,
            attempt=self.attempt,
            on_chunk=self.text_stream.feed if self.text_stream else None,
            checkpoint=self.checkpoint,
        )
        self.reported_usage = _reported_usage(payload.get("usage"))
        self.completed_response_id = payload["id"]
        # Interpret optional usage locally so pricing-library conversions cannot
        # replace real token counts with defaults or reject a usable response.
        return httpx.Response(200, json={**payload, "usage": None}, request=request)


@dataclass(frozen=True, slots=True)
class RoleResponse:
    message: ModelResponse
    model: str
    failures: tuple[RoleModelFailure, ...]
    attempt: int


class StudioRoleModels:
    def __init__(self, proxy: NativeProviderProxy) -> None:
        self.proxy = proxy

    def plan(self, project: StudioProject, actor: Actor) -> RoleModelPlan:
        return RoleModelPlan(
            project.config.model_for_role(actor.role), self.proxy.settings.text_retry.model_copy()
        )

    async def verify_attempt(self, lease: RoleLease, receipt: dict[str, object]) -> None:
        try:
            await self.proxy.run_io(
                original_role_receipt,
                self.proxy.coordinator,
                receipt,
                str(self.proxy.settings.work_root.resolve()),
                lease.project_id,
                lease.actor_id,
            )
        except Exception as error:
            raise RoleResponsePending("Original role receipt requires reconciliation") from error

    async def request(
        self,
        lease: RoleLease,
        messages: list[ModelMessage],
        tools: list[ToolDefinition],
        preview: RolePreview | None = None,
        *,
        effort: str | None = None,
        group: str | None = None,
        checkpoint: Callable[[int], RoleAttempt] | None = None,
        plan: RoleModelPlan | None = None,
        continuation_instructions: str | None = None,
    ) -> RoleResponse:
        project = await self.proxy.run_io(self.proxy.service.store.get, lease.project_id)
        actor = lease.active(project)
        plan = plan or self.plan(project, actor)
        group = group or uuid4().hex
        failures: list[RoleModelFailure] = []
        model = plan.model
        for attempt in range(plan.retry.max_retries + 1):
            fresh = await self.proxy.run_io(self.proxy.service.store.get, lease.project_id)
            lease.active(fresh)
            tracked = checkpoint(attempt) if checkpoint else None
            if tracked and tracked.original and rejected_role_result(tracked.original):
                await self.verify_attempt(lease, tracked.original)
                failures.append(RoleModelFailure(model, RoleInvalidResult.__name__))
                raise RoleModelFailed(failures)
            stream = RoleTextStream(preview, model, attempt) if preview else None
            try:
                message = await self._attempt(
                    lease,
                    messages,
                    tools,
                    model,
                    effort,
                    group,
                    attempt,
                    stream,
                    tracked,
                    continuation_instructions=continuation_instructions,
                )
            except (
                asyncio.CancelledError,
                RoleRunEnded,
                RoleResponsePending,
                RoleReconciliationRequired,
            ):
                if stream is not None:
                    await stream.fail()
                raise
            except ProviderCapacityWait as error:
                if stream is not None:
                    await stream.fail()
                raise RoleModelWait(
                    max(error.retry_at, time.time() + plan.retry.base_delay_seconds)
                ) from error
            except RoleModelRetry as error:
                if stream is not None:
                    await stream.fail()
                failures.append(RoleModelFailure(model, type(error).__name__))
                if attempt == plan.retry.max_retries:
                    raise RoleModelFailed(failures) from error
                retry_at = max(error.retry_at, error.failed_at + plan.retry.delay(attempt))
                delay = max(0, retry_at - time.time())
                if delay:
                    if checkpoint:
                        # Yield the role lease during backoff. The durable
                        # failed-at timestamp enforces the same wait on restart.
                        raise RoleModelWait(retry_at) from error
                    await asyncio.sleep(delay)
            except Exception as error:
                if stream is not None:
                    await stream.fail()
                hint = error.recovery_hint if isinstance(error, RoleProviderFailure) else None
                failures.append(RoleModelFailure(model, type(error).__name__, hint))
                raise RoleModelFailed(failures) from error
            else:
                return RoleResponse(message, model, tuple(failures), attempt)
        raise RoleModelFailed(failures)

    @staticmethod
    def validate_response(message: ModelResponse) -> None:
        actions = [part for part in message.parts if isinstance(part, ToolCallPart)]
        if any(
            not isinstance(action.tool_call_id, str) or not action.tool_call_id.strip()
            for action in actions
        ) or len({action.tool_call_id for action in actions}) != len(actions):
            raise ProviderContractError("Role model returned invalid tool call identities")
        for action in actions:
            if not isinstance(action.tool_name, str) or not action.tool_name.strip():
                raise ProviderContractError("Role model returned an invalid tool name")
            action.args_as_dict(raise_if_invalid=True)
        # The SDK constructs response dataclasses without checking every wire field.
        # Reject any response that cannot be written and reopened by the role journal.
        ModelMessagesTypeAdapter.validate_json(
            ModelMessagesTypeAdapter.dump_json([message], warnings="error")
        )
        if not actions and not any(
            isinstance(part, TextPart) and part.content.strip() for part in message.parts
        ):
            raise ProviderContractError("Role model produced neither text nor a tool action")

    async def _attempt(
        self,
        lease: RoleLease,
        messages: list[ModelMessage],
        tools: list[ToolDefinition],
        name: str,
        effort: str | None,
        group: str,
        attempt: int,
        text_stream: RoleTextStream | None,
        checkpoint: RoleAttempt | None,
        *,
        continuation_instructions: str | None,
    ) -> ModelResponse:
        transport = RoleResponsesTransport(
            self.proxy,
            lease,
            name,
            effort,
            group,
            attempt,
            text_stream,
            checkpoint,
            continuation_instructions,
        )
        async with httpx.AsyncClient(transport=transport) as http:
            # This client speaks wire format only. Real credentials stay in the
            # Studio registry and the existing proxy signs the upstream request.
            client = AsyncOpenAI(
                base_url="http://agentvideo-role.internal/",
                api_key="studio-role-transport",
                http_client=http,
                max_retries=0,
            )
            model = OpenAIResponsesModel(name, provider=OpenAIProvider(openai_client=client))
            settings: OpenAIResponsesModelSettings = {
                "openai_store": True,
                "openai_send_reasoning_ids": False,
            }
            prior = next((m for m in reversed(messages) if isinstance(m, ModelResponse)), None)
            if (
                prior
                and prior.provider_response_id
                and ((prior.provider_details or {}).get("agentvideo_runtime_model") == name)
            ):
                settings["openai_previous_response_id"] = "auto"
            response: ModelResponse | None = None
            try:
                response = await model.request(
                    messages, settings, ModelRequestParameters(function_tools=tools)
                )
                reported = transport.reported_usage
                if reported is not None:
                    response.usage = RequestUsage(
                        **{key: count for key, count in reported.items() if key != "total_tokens"}
                    )
                response.provider_details = {
                    **(response.provider_details or {}),
                    "agentvideo_runtime_model": name,
                    "agentvideo_usage_reported": reported is not None,
                    "agentvideo_reported_usage": reported,
                }
                self.validate_response(response)
            except Exception as error:
                if transport.failure is not None:
                    raise transport.failure from None
                response_id = transport.completed_response_id
                # The SDK can fail to decode the body before returning a ModelResponse.
                # The proxy's exact terminal ID allows the journal to verify that receipt.
                if (
                    isinstance(
                        error,
                        (
                            ProviderContractError,
                            ValueError,
                            AssertionError,
                            TypeError,
                            AttributeError,
                        ),
                    )
                    and checkpoint
                    and checkpoint.reject_result
                    and response_id
                    and (response is None or response.provider_response_id == response_id)
                ):
                    await self.proxy.run_io(checkpoint.reject_result, response_id)
                    raise RoleInvalidResult("Completed role response is unusable") from None
                raise
            return response
