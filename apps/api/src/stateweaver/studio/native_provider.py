"""Fixed-service Responses transport with durable role receipts."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sqlite3
import tempfile
import time
from collections.abc import Awaitable, Callable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote
from urllib.request import Request

import httpx

from stateweaver.providers.audit import atomic_id, numeric_usage, record_event
from stateweaver.providers.base import (
    ProviderContractError,
    ProviderExternalError,
    ProviderHttpStatusError,
    explicitly_not_accepted,
    parse_retry_after,
    provider_config_hash,
)
from stateweaver.providers.coordination import (
    ProviderCapacityWait,
    ProviderCoordinator,
    ProviderSubmissionUnknown,
    classify_error,
)
from stateweaver.providers.registry import APIRegistry, APIService

from .contracts import Actor, StudioProject
from .image_input_recovery import REFUSAL, is_image_refusal
from .native_image_delivery import NativeImageInput, confirm_image_input, match_image_input
from .native_receipts import NativeReceipts
from .review_context_recovery import is_context_overflow
from .role_receipts import (
    RoleAttempt,
    RoleReconciliationRequired,
    RoleRunEnded,
    original_role_receipt,
    role_response_id,
    scoped_role_receipt,
)
from .service import StudioService
from .settings import StudioSettings

logger = logging.getLogger(__name__)

NATIVE_INPUT_COMPATIBILITY_POLICY = "nonportable-input-items-v3"
_TRANSIENT_HTTP = {429, 502, 503, 504}


def _portable_role_input(
    payload: dict[str, Any], model: str, body: bytes
) -> tuple[bytes, dict[str, Any] | None]:
    """Keep the historical wire contract for foreign optional history IDs."""
    history = payload.get("input")
    if not model.startswith(("gt-", "gpt-")) or not isinstance(history, list):
        return body, None
    portable = []
    changed = False
    for item in history:
        identifier = atomic_id(item.get("id")) if isinstance(item, dict) else None
        if (
            isinstance(item, dict)
            and not item.get("encrypted_content")
            and identifier
            and identifier.startswith("msg_")
        ):
            if item.get("type") == "reasoning":
                changed = True
                continue
            if item.get("type") in {
                "function_call",
                "function_call_output",
                "custom_tool_call",
                "custom_tool_call_output",
            }:
                item = {key: value for key, value in item.items() if key != "id"}
                changed = True
        portable.append(item)
    if not changed:
        return body, None
    payload["input"] = portable
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(), {
        "policy": NATIVE_INPUT_COMPATIBILITY_POLICY,
        "source_request_hash": hashlib.sha256(body).hexdigest(),
        "source_request_bytes": len(body),
    }


def _rate_limited(error: Mapping[str, Any]) -> bool:
    """Read only bounded error envelopes, never arbitrary prompt/output strings."""
    values: list[str] = []

    def visit(value: Any, depth: int = 0) -> None:
        if depth > 6 or len(values) >= 64:
            return
        if isinstance(value, Mapping):
            for key in ("code", "type", "status", "message", "error", "cause", "details"):
                if key in value:
                    visit(value[key], depth + 1)
        elif isinstance(value, (str, int)) and not isinstance(value, bool):
            rendered = str(value)[:8192]
            values.append(rendered.lower())
            if isinstance(value, str) and rendered.lstrip().startswith(("{", "[", '"')):
                try:
                    decoded = json.loads(rendered)
                except (ValueError, RecursionError):
                    return
                if decoded != value:
                    visit(decoded, depth + 1)

    visit(error)
    if any(
        marker in value
        for value in values
        for marker in (
            "content_filter",
            "moderation_blocked",
            "policy_rejection",
            "safety_rejection",
        )
    ):
        return False
    return any(
        value.strip() == "429"
        or value.strip() in {"rate_limit", "rate_limit_exceeded", "too_many_requests"}
        or "engineoverloaded" in value
        for value in values
    )


def _acceptance_indicated(value: Any, depth: int = 0) -> bool:
    if not isinstance(value, dict) or depth > 4:
        return False
    if value.get("accepted") is True or any(
        value.get(key) for key in ("id", "job_id", "task_id", "output")
    ):
        return True
    return any(
        _acceptance_indicated(value.get(key), depth + 1)
        for key in ("error", "response", "result", "cause", "details")
    )


def _role_retry(receipt: Mapping[str, Any]) -> RoleModelRetry | None:
    error = receipt.get("result", {}).get("error") or {}
    if (
        receipt.get("state") == "failed"
        and role_response_id(receipt) is None
        and error.get("phase") == "submission"
        and error.get("definitely_not_accepted") is True
        and error.get("category") != "policy_rejection"
        and (
            error.get("http_status") in _TRANSIENT_HTTP
            or error.get("http_status") is None
            and error.get("category") == "rate_limit"
        )
    ):
        failed_at = float(receipt["updated"])
        return RoleModelRetry(failed_at, failed_at + float(error.get("retry_after_seconds") or 0))
    return None


@dataclass
class NativeRequest:
    request: Request
    binding: APIService
    receipt: dict[str, Any] | None
    model: str
    operation: str
    wire_hash: str
    body_size: int
    input_compatibility: dict[str, Any] | None = None
    image_input: NativeImageInput | None = None
    saved_role_result: dict[str, Any] | None = None
    role_attempt: RoleAttempt | None = None


@dataclass
class BufferedAttempt:
    status: int
    headers: dict[str, str]
    chunks: list[bytes]
    role_result: dict[str, Any] | None


_ROLE_RECOVERY_HINTS = {
    "role_image_input_refusal": REFUSAL,
    "role_image_capacity": "maximum number of images",
    "context_length_exceeded": "context_length_exceeded",
}


def role_failure_code(detail: str) -> str | None:
    text = detail.lower()
    if text in _ROLE_RECOVERY_HINTS:
        return text
    if is_image_refusal(text):
        return "role_image_input_refusal"
    if "maximum number of images" in text:
        return "role_image_capacity"
    if is_context_overflow(text):
        return "context_length_exceeded"
    return None


class RoleProviderFailure(ProviderExternalError):
    """A failed model response with only the known Studio recovery hint retained."""

    def __init__(self, detail: str, body: bytes) -> None:
        super().__init__(detail)
        self.recovery_code = role_failure_code(body.decode(errors="replace"))
        self.recovery_hint = _ROLE_RECOVERY_HINTS.get(self.recovery_code or "")


class RoleResponsePending(ProviderExternalError):
    """The already submitted role response is not ready to recover."""

    def __init__(self, detail: str, *, query_http_status: int | None = None) -> None:
        super().__init__(detail)
        self.query_http_status = query_http_status


class RoleModelRetry(ProviderExternalError):
    """The original provider response proves temporary non-admission."""

    def __init__(self, failed_at: float, retry_at: float) -> None:
        super().__init__("Fixed model service temporarily rejected the request")
        self.failed_at, self.retry_at = failed_at, retry_at


def _query_request(binding: APIService, response_id: str, authorization: str) -> Request:
    if response_id in {".", ".."}:
        raise RoleResponsePending("Original role response ID cannot be queried safely")
    identifier = quote(response_id, safe="")
    return Request(
        f"{binding.base_url}/responses/{identifier}",
        headers={"Authorization": authorization, "Accept": "application/json"},
        method="GET",
    )


@dataclass(frozen=True, slots=True)
class NativeSSEEvent:
    name: str | None
    value: Any

    def role_type(self) -> tuple[Any, bool]:
        declared = self.value.get("type") if isinstance(self.value, dict) else None
        if self.name in {None, "message"}:
            return declared, False
        return self.name, declared is not None and declared != self.name


class NativeSSEEvents:
    """Frame the original SSE bytes without changing the forwarded body."""

    def __init__(self) -> None:
        self.buffer = b""
        self._previous_chunk_ended_in_cr = False
        self._first_frame = True

    def feed(self, data: bytes) -> Iterator[NativeSSEEvent]:
        if not data:
            return
        # CR terminates a line immediately. An LF in the next chunk may already
        # belong to that CRLF, rather than terminating a second line.
        if self._previous_chunk_ended_in_cr and data.startswith(b"\n"):
            data = data[1:]
        self._previous_chunk_ended_in_cr = data.endswith(b"\r")
        normalized = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        pieces = (self.buffer + normalized).split(b"\n\n")
        self.buffer = pieces.pop()
        for piece in pieces:
            if self._first_frame:
                piece = piece.removeprefix(b"\xef\xbb\xbf")
                self._first_frame = False
            name = None
            data_lines = []
            for line in piece.split(b"\n"):
                field, _, value = line.partition(b":")
                value = value.removeprefix(b" ")
                if field == b"data":
                    data_lines.append(value)
                elif field == b"event":
                    name = value.decode("utf-8", errors="replace") or None
            payload = b"\n".join(data_lines)
            if not payload or payload.strip() == b"[DONE]":
                continue
            try:
                yield NativeSSEEvent(name, json.loads(payload))
            except (ValueError, UnicodeDecodeError):
                continue


_ROLE_TERMINAL_EVENTS = {
    "response.completed": "completed",
    "response.failed": "failed",
    "response.incomplete": "incomplete",
    "response.cancelled": "cancelled",
}
_ROLE_FAILURE_STATES = {"failed", "incomplete", "cancelled"}


class NativeMetadata:
    """Parse only safe metadata; retained state never contains model text."""

    def __init__(self, requested_model: str, *, pin_response_identity: bool = False) -> None:
        self.requested_model = requested_model
        self.pin_response_identity = pin_response_identity
        self.actual_model: str | None = None
        self.response_id: str | None = None
        self.usage: dict[str, Any] | None = None
        self.terminal = False
        self.failed = False
        self.refused = False
        self.rate_limited = False
        self.has_output = False
        self.acceptance_indicated = False
        self.rejection_indicated = False
        self.error_code: str | None = None
        self.role_recovery_code: str | None = None
        self.events = NativeSSEEvents()
        self._terminal_hash: str | None = None
        self.validated_role_response_id: str | None = None

    @staticmethod
    def _terminal_fingerprint(response: dict[str, Any], failed: bool) -> str:
        payload = json.dumps([failed, response], sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()

    def event(self, frame: NativeSSEEvent) -> dict[str, Any] | None:
        return self.object(frame.value, frame=frame)

    def object(self, value: Any, *, frame: NativeSSEEvent | None = None) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return None
        self.acceptance_indicated |= _acceptance_indicated(value)
        error_marker = value.get("error")
        self.rejection_indicated |= value.get("accepted") is False or (
            isinstance(error_marker, dict) and error_marker.get("accepted") is False
        )
        event_type = value.get("type")
        label_conflicts = False
        if self.pin_response_identity and frame is not None:
            event_type, label_conflicts = frame.role_type()
        elif self.pin_response_identity and event_type == value.get("object") == "response":
            event_type = None  # A response object's type is not an SSE event name.
        nested = value.get("response")
        response = None
        if isinstance(nested, dict) and (
            self.pin_response_identity or event_type is None or isinstance(event_type, str)
        ):
            response = nested
        elif (
            event_type is None
            or isinstance(event_type, str)
            and (event_type == "error" or value.get("object") == "response")
        ):
            response = value  # Output-item IDs are not remote response IDs.
        identifier = atomic_id(response.get("id")) if response is not None else None
        declared_state = response.get("status") if response is not None else None
        state = declared_state if isinstance(declared_state, str) else None
        failed_events = ("response.failed", "response.incomplete", "error")
        failed = event_type in failed_events or state in _ROLE_FAILURE_STATES
        if self.pin_response_identity and event_type == "response.cancelled":
            failed = True
        terminal = failed or event_type == "response.completed" or state == "completed"
        model = response.get("model") if response is not None else None
        terminal_hash = None
        if self.pin_response_identity:
            if self.response_id and (
                identifier not in {None, self.response_id} or identifier is None and terminal
            ):
                raise ProviderContractError(
                    "Native role stream did not match its acknowledged response"
                )
            if isinstance(model, str) and (identifier or self._terminal_hash is None):
                self._require_model(model)
                if identifier:
                    self.validated_role_response_id = identifier
            event_state = (
                _ROLE_TERMINAL_EVENTS.get(event_type) if isinstance(event_type, str) else None
            )
            if (
                label_conflicts
                or event_type is not None
                and not isinstance(event_type, str)
                or declared_state is not None
                and state is None
                or event_state is not None
                and state is not None
                and event_state != state
                or terminal
                and event_type is not None
                and event_state is None
                and event_type != "error"
                or event_type == "error"
                and state is not None
                and state not in _ROLE_FAILURE_STATES
            ):
                raise ProviderContractError("Native role event conflicts with its response status")
            if response is not None and event_state is not None and state is None:
                response = {**response, "status": event_state}
            if response is not None and terminal:
                terminal_hash = self._terminal_fingerprint(response, failed)
            if self._terminal_hash is not None:
                if terminal_hash is not None and terminal_hash != self._terminal_hash:
                    raise ProviderContractError(
                        "Native role stream returned conflicting terminal results"
                    )
                # Repeated terminal frames and late progress cannot change a finished result.
                return None
        refusal_types = {
            "refusal",
            "response.refusal.done",
            "content_filter",
            "content_filter_guidance",
            "moderation_blocked",
            "policy_rejection",
            "safety_rejection",
        }
        stack = [value]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                item_type = event_type if item is value else item.get("type")
                item_code = item.get("code")
                item_reason = item.get("reason")
                if any(
                    isinstance(marker, str) and marker in refusal_types
                    for marker in (item_type, item_code, item_reason)
                ):
                    self.refused = True
                    self.error_code = atomic_id(item_code or item_reason or item_type)
                stack.extend(item.values())
            elif isinstance(item, list):
                stack.extend(item)
        if isinstance(event_type, str) and event_type.startswith(
            ("response.output", "response.function_call", "response.custom_tool")
        ):
            self.has_output = True
        if response is None:
            return None
        if identifier:
            self.response_id = identifier
        if isinstance(model, str):
            self.actual_model = model
            if not self.pin_response_identity:
                self._require_model(model)
        successful_result = None
        if self.pin_response_identity and terminal and not failed and not self.refused:
            if not identifier or not isinstance(model, str):
                raise ProviderContractError("Native role success lacks an exact response identity")
            successful_result = response
        if "usage" in response:
            self.usage = numeric_usage(response["usage"])
        error = response.get("error")
        if isinstance(error, dict):
            self.error_code = atomic_id(error.get("code") or error.get("type"))
            self.role_recovery_code = (
                role_failure_code(str(error.get("message") or ""))
                or role_failure_code(self.error_code or "")
                or self.role_recovery_code
            )
            self.rate_limited |= _rate_limited(error)
        elif event_type == "error":
            self.rate_limited |= _rate_limited(value)
        if response.get("output"):
            self.has_output = True
        if terminal:
            self.terminal = True
            self.failed = failed
            self._terminal_hash = terminal_hash
        return successful_result

    def _require_model(self, model: str) -> None:
        # Exact JDCloud alias/canonical pairs observed in isolated Responses probes.
        canonical = {
            "gt-6-as-a": "gpt-6-astra",
            "gt-6.1-sol-a": "gpt-6.1-sol",
            "gt-6.1-sol-b": "gpt-6.1-sol",
            "gt-6.1-sol-zl": "gpt-6.1-sol",
            "qwen-3.8-max": "qwen3.8-max",
        }.get(self.requested_model)
        if model != self.requested_model and model != canonical:
            raise ProviderContractError("Native API returned a different model")

    def feed(self, data: bytes) -> None:
        for event in self.events.feed(data):
            self.event(event)


class NativeProviderProxy:
    def __init__(
        self,
        service: StudioService,
        registry: APIRegistry,
        coordinator: ProviderCoordinator,
        settings: StudioSettings,
        *,
        async_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.service, self.registry, self.coordinator = service, registry, coordinator
        self.settings = settings
        self.receipts = NativeReceipts(
            coordinator, root=settings.work_root / "native-provider-receipts"
        )
        self._io_executor = ThreadPoolExecutor(thread_name_prefix="native-journal")
        self._async_transport = async_transport
        self._client: httpx.AsyncClient | None = None
        self._closed = False

    async def run_io[T](self, function: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        return await asyncio.get_running_loop().run_in_executor(
            self._io_executor, partial(function, *args, **kwargs)
        )

    async def aclose(self) -> None:
        self._closed = True
        try:
            if self._client is not None:
                await self._client.aclose()
        finally:
            self._io_executor.shutdown(wait=False)

    @staticmethod
    def _active(project: StudioProject, expected: Actor) -> Actor:
        actor = project.actors.get(expected.id)
        parent = project.actors.get(actor.parent_actor_id or "") if actor else None
        if (
            project.deleted_at
            or actor is None
            or actor.status != "running"
            or actor.paused
            or (parent and parent.paused)
            or not actor.lease_owner
            or actor.lease_owner != expected.lease_owner
            or actor.lease_generation != expected.lease_generation
            or actor.lease_until <= time.time()
            or actor.session_key != expected.session_key
            or actor.context_epoch != expected.context_epoch
            or actor.thread_id != expected.thread_id
        ):
            raise RoleRunEnded()
        return actor

    def _registry(self, project: StudioProject) -> APIRegistry:
        return (
            self.registry.pinned(project.api_config_snapshot)
            if project.api_config_snapshot
            else self.registry
        )

    @staticmethod
    def _original_binding(registry: APIRegistry, receipt: dict[str, Any]) -> APIService:
        try:
            binding = registry.fixed_service("text", required=True)
            assert binding is not None
            expected = binding.binding(receipt["model"])
            if binding.id != receipt["service_id"] or receipt["context"][
                "binding_hash"
            ] != provider_config_hash(expected):
                raise ProviderContractError("Original model endpoint changed")
        except (KeyError, ValueError, ProviderContractError) as error:
            raise RoleResponsePending("Original role model endpoint changed") from error
        return binding

    def _fixed_binding(
        self, project: StudioProject, actor: Actor, registry: APIRegistry, model: str
    ) -> APIService:
        binding = registry.fixed_service("text", required=True)
        assert binding is not None
        expected = binding.binding(model)
        session = actor.sessions[actor.session_key]
        if session.provider_binding:
            if session.provider_binding != expected:
                raise ProviderContractError("Saved role model/service binding has changed")
            return binding

        def pin(current: StudioProject) -> None:
            active = self._active(current, actor)
            saved = active.sessions[actor.session_key].provider_binding
            if current.config.model_for_role(active.role) != model or (saved and saved != expected):
                raise RoleRunEnded()
            active.sessions[actor.session_key].provider_binding = expected

        self.service.store.mutate(
            project.id,
            pin,
            actor_id=actor.id,
            kind="native_provider_pinned",
            advance_revision=False,
        )
        return binding

    def prepare(
        self,
        project: StudioProject,
        actor: Actor,
        body: bytes,
        headers: Mapping[str, str],
        *,
        model: str,
        group: str,
        attempt: int,
        role_attempt: RoleAttempt | None,
    ) -> NativeRequest:
        self._active(project, actor)
        self.receipts.recover(project_id=project.id, actor_id=actor.id)
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError) as error:
            raise ProviderContractError("Invalid role model request JSON") from error
        if not isinstance(payload, dict) or payload.get("model") != model:
            raise ProviderContractError("原生文本请求必须使用角色固定模型")
        body, input_compatibility = _portable_role_input(payload, model, body)
        wire_hash = hashlib.sha256(body).hexdigest()
        workspace = str(self.settings.work_root.resolve())
        original = None
        if role_attempt and role_attempt.original is not None:
            try:
                original = original_role_receipt(
                    self.coordinator, role_attempt.original, workspace, project.id, actor.id
                )
            except (KeyError, TypeError, ProviderContractError) as error:
                raise RoleResponsePending(
                    "Original role receipt requires reconciliation"
                ) from error
            if role_attempt.original.get("historical"):
                role_attempt.record(original, historical=True)
                raise RoleReconciliationRequired("旧上下文的相同模型请求仍待核对")
        elif role_attempt and role_attempt.check_previous_context:
            matches = self._previous_context_role_receipts(
                project.id, actor.id, model, wire_hash, attempt, before_epoch=actor.context_epoch
            )
            if len(matches) > 1:
                raise RoleReconciliationRequired("旧上下文中的原模型回执不唯一")
            if matches:
                role_attempt.record(matches[0], historical=True)
                raise RoleReconciliationRequired("旧上下文的相同模型请求仍待核对")

        registry = self._registry(project)
        if original is not None:
            if original["model"] != model or original["request_hash"] != wire_hash:
                raise RoleResponsePending("Original role request changed before recovery")
            if role_attempt and not role_attempt.can_reserve_again(original):
                role_attempt.record(original)
                if original["state"] == "failed":
                    if (retry := _role_retry(original)) is not None:
                        raise retry
                    code = original.get("result", {}).get("error", {}).get("code", "")
                    raise RoleProviderFailure(
                        "Saved role model response failed", str(code).encode()
                    )
                response_id = role_response_id(original)
                saved_result = self._read_role_result(original)
                if response_id is None and saved_result is not None:
                    response_id = atomic_id(saved_result.get("id"))
                if response_id is None:
                    if original["state"] == "submitting":
                        raise RoleResponsePending("Original role submission awaits acknowledgment")
                    raise RoleReconciliationRequired("原模型请求未返回响应编号，需先核对原回执")
                binding = self._original_binding(registry, original)
                try:
                    authorization = "" if saved_result else "Bearer " + registry.credential(binding)
                except ProviderExternalError as error:
                    raise RoleResponsePending(
                        "Original role response credentials are unavailable"
                    ) from error
                return NativeRequest(
                    _query_request(binding, response_id, authorization),
                    binding,
                    original,
                    model,
                    "query",
                    wire_hash,
                    len(body),
                    input_compatibility,
                    image_input=match_image_input(project, actor, payload),
                    saved_role_result=saved_result,
                    role_attempt=role_attempt,
                )

        if model != project.config.model_for_role(actor.role):
            raise ProviderContractError("原生文本请求必须使用角色固定模型")
        binding = self._fixed_binding(project, actor, registry, model)
        binding_hash = provider_config_hash(binding.binding(model))
        predecessor = payload.get("previous_response_id")
        if predecessor is not None:
            if not isinstance(predecessor, str):
                raise ProviderContractError("原生响应编号无效")
            scope = {"workspace": workspace, "project_id": project.id, "actor_id": actor.id}
            try:
                prior = self.coordinator.by_provider_id(predecessor, **scope)
            except ProviderContractError as error:
                pending = self.receipts.pending_completion(
                    predecessor,
                    scope={
                        **scope,
                        "context_epoch": actor.context_epoch,
                        "binding_hash": binding_hash,
                    },
                    service_id=binding.id,
                    model=model,
                )
                if (
                    pending is not None
                    and (saved := self._read_role_result(pending)) is not None
                    and saved["id"] == predecessor
                ):
                    raise RoleResponsePending(
                        "Original role predecessor is awaiting shared receipt publication"
                    ) from error
                # Recovery can publish and remove the sidecar after the first read.
                prior = self.coordinator.by_provider_id(predecessor, **scope)
            if (
                prior["service_id"] != binding.id
                or prior["model"] != model
                or prior["context"].get("binding_hash") != binding_hash
            ):
                raise ProviderContractError("Native response belongs to another pinned endpoint")
        credential = registry.credential(binding)
        context = {
            "workspace": workspace,
            "project_id": project.id,
            "actor_id": actor.id,
            "task_id": actor.delegate_task_id or actor.visual_task_id,
            "operation": "native_responses",
            "context_epoch": actor.context_epoch,
            "job_id": "av2-role-" + group,
            "recipe_hash": wire_hash,
            "binding_hash": binding_hash,
            **({"attempt": attempt} if attempt else {}),
        }
        if original and original["context"].get("binding_hash") != binding_hash:
            raise RoleResponsePending("Original role model endpoint changed")
        receipt = self.coordinator.reserve(
            binding,
            model,
            original["context"] if original else context,
            wire_hash,
            original["config_hash"] if original else registry.snapshot()["sha256"],
            owner=actor.lease_owner or "",
            ttl=self.settings.lease_seconds,
            writer_timeout_seconds=0.25,
        )
        if original and receipt["id"] != original["id"]:
            raise RoleResponsePending("Original role receipt disappeared before recovery")
        try:
            if role_attempt:
                role_attempt.record(receipt)
            # The last active fence precedes submission intent. A pause that
            # wins before it can still release the untouched reservation.
            current = self.service.store.get(project.id)
            active = self._active(current, actor)
            if current.config.model_for_role(active.role) != model or active.sessions[
                actor.session_key
            ].provider_binding != binding.binding(model):
                raise RoleRunEnded()
        except Exception:
            if receipt["state"] == "reserved":
                self.coordinator.update(
                    receipt["id"],
                    "cancelled",
                    owner=receipt["owner"],
                    generation=receipt["generation"],
                    writer_timeout_seconds=0.25,
                )
            raise
        try:
            started = self.coordinator.begin(
                receipt["id"],
                owner=actor.lease_owner or "",
                generation=receipt["generation"],
                execution_generation=actor.lease_generation,
                local_request_hash=wire_hash,
                writer_timeout_seconds=0.25,
            )
        except ProviderSubmissionUnknown as error:
            raise RoleReconciliationRequired("原模型请求未返回响应编号，需先核对原回执") from error
        if role_attempt:
            role_attempt.record(started)
        # A completed known operation never authorizes another POST. A worker
        # whose checkpoint publication raced will recover the original next time.
        if not started.get("may_submit"):
            raise RoleResponsePending("Original role submission must be recovered")
        safe_headers = {
            k: v
            for k, v in headers.items()
            if k.lower() in {"content-type", "accept", "openai-beta", "x-client-request-id"}
        }
        safe_headers["Authorization"] = "Bearer " + credential
        request = Request(
            f"{binding.base_url}/responses", data=body, headers=safe_headers, method="POST"
        )
        return NativeRequest(
            request,
            binding,
            started,
            model,
            "submission",
            wire_hash,
            len(body),
            input_compatibility,
            image_input=match_image_input(project, actor, payload),
            role_attempt=role_attempt,
        )

    def unavailable(self, prepared: NativeRequest) -> None:
        if prepared.receipt and prepared.operation == "submission":
            self.receipts.publish(
                prepared.receipt,
                "unknown",
                result={"error": {"category": "submission_unknown", "phase": "submission"}},
            )

    def received(
        self, prepared: NativeRequest, status: int, headers: Mapping[str, str]
    ) -> dict[str, str]:
        response_headers = {
            k: v
            for k, v in headers.items()
            if k.lower() in {"content-type", "retry-after", "x-request-id"}
        }
        if prepared.receipt:
            if prepared.input_compatibility:
                record_event(
                    self.coordinator,
                    prepared.receipt["id"],
                    "input_compatibility",
                    "applied",
                    {
                        "event_type": prepared.input_compatibility["policy"],
                        "wire_request_hash": prepared.input_compatibility["source_request_hash"],
                        "request_bytes": prepared.input_compatibility["source_request_bytes"],
                        "bytes": prepared.body_size,
                    },
                )
            record_event(
                self.coordinator,
                prepared.receipt["id"],
                prepared.operation,
                "received",
                {
                    "http_status": status,
                    "wire_request_hash": prepared.wire_hash,
                    "request_bytes": prepared.body_size,
                    "method": prepared.request.get_method(),
                },
            )
        return response_headers

    async def _async_send(self, prepared: NativeRequest, method: str) -> httpx.Response:
        if self._client is None:
            self._client = httpx.AsyncClient(
                transport=self._async_transport,
                follow_redirects=False,
                limits=httpx.Limits(max_connections=None, max_keepalive_connections=20),
            )
        request = self._client.build_request(
            method,
            prepared.request.full_url,
            content=cast(bytes, prepared.request.data) if method == "POST" else None,
            headers={**dict(prepared.request.header_items()), "Accept-Encoding": "identity"},
            timeout=prepared.binding.timeout_seconds,
        )
        try:
            return await self._client.send(request, stream=True)
        except BaseException as error:
            await asyncio.shield(self.run_io(self.unavailable, prepared))
            if isinstance(error, httpx.TransportError):
                if prepared.operation == "submission":
                    raise RoleReconciliationRequired(
                        "原模型请求未返回响应编号，需先核对原回执"
                    ) from None
                raise RoleResponsePending("Original role response query is unavailable") from None
            raise

    async def role_attempt(
        self,
        project: StudioProject,
        actor: Actor,
        body: bytes,
        headers: Mapping[str, str],
        *,
        model: str,
        group: str,
        attempt: int,
        on_chunk: Callable[[bytes], Awaitable[None]] | None,
        checkpoint: RoleAttempt | None,
    ) -> dict[str, Any]:
        if self._closed:
            raise RoleRunEnded()
        try:
            prepared = await self.run_io(
                self.prepare,
                project,
                actor,
                body,
                headers,
                model=model,
                group=group,
                attempt=attempt,
                role_attempt=checkpoint,
            )
        except sqlite3.OperationalError as error:
            code = getattr(error, "sqlite_errorcode", None)
            if isinstance(code, int) and code & 0xFF in {
                sqlite3.SQLITE_BUSY,
                sqlite3.SQLITE_LOCKED,
            }:
                raise ProviderCapacityWait(time.time() + 1, "provider_ledger_busy") from error
            raise
        try:
            return await self._role_outcome(prepared, on_chunk=on_chunk)
        finally:
            if checkpoint and prepared.receipt:
                await asyncio.shield(self.run_io(checkpoint.record, prepared.receipt))

    def _previous_context_role_receipts(
        self,
        project_id: str,
        actor_id: str,
        model: str,
        wire_hash: str,
        attempt: int,
        *,
        before_epoch: int,
    ) -> list[dict[str, Any]]:
        workspace = str(self.settings.work_root.resolve())
        matching = []
        try:
            for identifier in self.coordinator.native_receipt_ids(project_id, actor_id):
                receipt = self.coordinator.get(identifier)
                if (
                    scoped_role_receipt(receipt, workspace, project_id, actor_id)
                    and receipt["model"] == model
                    and receipt["request_hash"] == wire_hash
                    and receipt["context"].get("attempt", 0) == attempt
                    and isinstance(epoch := receipt["context"].get("context_epoch"), int)
                    and epoch < before_epoch
                    and receipt["state"] in {"unknown", "submitting", "running", "collecting"}
                ):
                    matching.append(receipt)
        except Exception as error:
            raise RoleResponsePending("Original role receipt requires reconciliation") from error
        return matching

    async def _role_outcome(
        self,
        prepared: NativeRequest,
        *,
        on_chunk: Callable[[bytes], Awaitable[None]] | None,
    ) -> dict[str, Any]:
        if prepared.operation == "query":
            return await self._resume_role_response(prepared)
        try:
            response = await self._async_send(prepared, "POST")
            outcome = await self._read_role_response(prepared, response, on_chunk=on_chunk)
        except (RoleRunEnded, RoleResponsePending, RoleReconciliationRequired):
            raise
        except Exception:
            if recovery := self._accepted_role_query(prepared):
                return await self._resume_role_response(recovery)
            raise
        if recovery := self._accepted_role_query(prepared):
            return await self._resume_role_response(recovery)
        receipt = prepared.receipt
        assert receipt is not None
        if (retry := _role_retry(receipt)) is not None:
            raise retry
        if receipt["state"] == "unknown":
            raise RoleReconciliationRequired("原模型请求未返回响应编号，需先核对原回执")
        raw = b"".join(outcome.chunks)
        if outcome.status >= 400:
            raise RoleProviderFailure(f"Role model returned HTTP {outcome.status}", raw)
        return outcome.role_result or self._completed_role_result(
            prepared.model, raw, outcome.headers
        )

    @staticmethod
    def _accepted_role_query(prepared: NativeRequest) -> NativeRequest | None:
        receipt = prepared.receipt
        if not receipt or receipt["state"] not in {
            "submitting",
            "running",
            "collecting",
            "unknown",
        }:
            return None
        identifier = role_response_id(receipt)
        if not identifier:
            return None
        authorization = prepared.request.get_header("Authorization")
        if not authorization:
            raise RoleResponsePending("Original role response credentials are unavailable")
        return replace(
            prepared,
            request=_query_request(prepared.binding, identifier, authorization),
            operation="query",
        )

    @staticmethod
    def _completed_role_result(
        model: str, raw: bytes, headers: Mapping[str, str]
    ) -> dict[str, Any]:
        metadata = NativeMetadata(model, pin_response_identity=True)
        if any(
            "text/event-stream" in value
            for key, value in headers.items()
            if key.lower() == "content-type"
        ):
            candidate = None
            for event in metadata.events.feed(raw):
                result = metadata.event(event)
                if candidate is None and result is not None:
                    candidate = result
        else:
            candidate = metadata.object(json.loads(raw))
        if (
            not isinstance(candidate, dict)
            or candidate.get("id") != metadata.response_id
            or not metadata.terminal
            or metadata.failed
            or metadata.refused
            or not metadata.response_id
            or not metadata.actual_model
        ):
            raise RoleProviderFailure("Role model did not return a successful response", raw)
        return candidate

    def _role_result_path(self, receipt: dict[str, Any]) -> Path:
        path = self.receipts.path(receipt)
        return path.parent / "role-results" / path.name

    def _read_role_result(self, receipt: dict[str, Any]) -> dict[str, Any] | None:
        try:
            saved = json.loads(self._role_result_path(receipt).read_bytes())
            candidate = saved["response"]
            if (
                saved["request_hash"] != receipt["request_hash"]
                or saved["model"] != receipt["model"]
                or receipt["state"] in {"reserved", "failed", "cancelled"}
                or role_response_id(receipt) not in {None, candidate["id"]}
            ):
                raise ValueError("saved response belongs to a different request")
            return self._completed_role_result(receipt["model"], json.dumps(candidate).encode(), {})
        except FileNotFoundError:
            return None
        except (
            KeyError,
            TypeError,
            ValueError,
            OSError,
            ProviderContractError,
            RoleProviderFailure,
        ) as error:
            logger.warning("Saved role result could not be recovered (%s)", type(error).__name__)
            return None

    def _save_role_result(
        self, prepared: NativeRequest, headers: Mapping[str, str], chunks: list[bytes]
    ) -> dict[str, Any] | None:
        receipt = prepared.receipt
        if not receipt:
            return None
        try:
            candidate = self._completed_role_result(prepared.model, b"".join(chunks), headers)
        except (ValueError, ProviderContractError, RoleProviderFailure):
            return None
        try:
            self._write_role_result(receipt, candidate)
        except OSError as error:
            # A non-streamed successful reply had no separate SSE acknowledgment.
            # Preserve its validated ID even when the full result cannot be saved.
            if role_response_id(receipt) is None:
                receipt["observed_response_id"] = candidate["id"]
                if prepared.role_attempt:
                    prepared.role_attempt.record(receipt)
            raise RoleResponsePending("Original role response result is awaiting save") from error
        return candidate

    def _write_role_result(self, receipt: dict[str, Any], candidate: dict[str, Any]) -> None:
        target = self._role_result_path(receipt)
        temporary = None
        try:
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", dir=target.parent, delete=False) as file:
                temporary = Path(file.name)
                json.dump(
                    {
                        "request_hash": receipt["request_hash"],
                        "model": receipt["model"],
                        "response": candidate,
                    },
                    file,
                    ensure_ascii=False,
                )
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, target)
            directory = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if temporary:
                with suppress(OSError):
                    temporary.unlink(missing_ok=True)

    async def _resume_role_response(self, prepared: NativeRequest) -> dict[str, Any]:
        """Collect the exact prior response without another submission."""
        receipt = prepared.receipt
        assert receipt is not None
        value = prepared.saved_role_result
        identifier = role_response_id(receipt) or (value or {}).get("id")
        try:
            if value is None:
                response = await self._async_send(prepared, "GET")
                try:
                    if response.status_code >= 400:
                        raise RoleResponsePending(
                            "Original role response query is unavailable",
                            query_http_status=response.status_code,
                        )
                    raw = await response.aread()
                finally:
                    await asyncio.shield(response.aclose())
                value = json.loads(raw)
            else:
                raw = json.dumps(value).encode()
            metadata = NativeMetadata(prepared.model, pin_response_identity=True)
            result = metadata.object(value)
        except (RoleRunEnded, RoleResponsePending, RoleReconciliationRequired):
            raise
        except Exception as error:
            raise RoleResponsePending("Original role response query is unavailable") from error
        if (
            not isinstance(value, dict)
            or not identifier
            or value.get("id") != identifier
            or metadata.response_id != identifier
            or not metadata.actual_model
            or not metadata.terminal
        ):
            raise RoleResponsePending("Original role response is awaiting exact completion")
        failure = (
            RoleProviderFailure("Original role model returned a terminal failure", raw)
            if metadata.failed or metadata.refused
            else None
        )
        if receipt["state"] == "complete" and failure:
            raise RoleResponsePending("Original role response differs from its completion receipt")
        if failure is None and result is None:
            raise RoleResponsePending("Original role response has no exact completed result")
        if result is not None and prepared.saved_role_result is None:
            # Preserve the exact GET result before a terminal ledger state can
            # outlive the upstream body or the actor's model checkpoint.
            try:
                await self.run_io(self._write_role_result, receipt, result)
            except OSError as error:
                raise RoleResponsePending(
                    "Original role response result is awaiting save"
                ) from error
        if receipt["state"] != "complete":
            try:
                await self.run_io(
                    self.receipts.publish,
                    receipt,
                    "failed" if failure else "complete",
                    provider_job_id=identifier,
                    result={
                        "actual_model": metadata.actual_model,
                        "usage": metadata.usage,
                        "provider_request_id": identifier,
                        **(
                            {
                                "error": {
                                    "category": "policy_rejection"
                                    if metadata.refused
                                    else "native_response_failed",
                                    "phase": "query",
                                    "known_job": True,
                                    "code": failure.recovery_code,
                                }
                            }
                            if failure
                            else {}
                        ),
                    },
                )
            except Exception as error:
                raise RoleResponsePending(
                    "Original role response receipt is awaiting save"
                ) from error
        if failure:
            raise failure
        if prepared.image_input:
            await self.run_io(
                self.confirm_model_image, prepared.image_input, receipt["id"], identifier
            )
        assert result is not None
        return result

    def confirm_model_image(
        self, binding: NativeImageInput, receipt_id: str, response_id: str
    ) -> None:
        def confirm(project: StudioProject) -> None:
            actor = project.actors.get(binding.actor_id)
            if actor is None or actor.lease_until <= time.time():
                return
            confirm_image_input(project, binding, receipt_id, response_id)

        try:
            self.service.store.mutate(
                binding.project_id,
                confirm,
                actor_id=binding.actor_id,
                kind="native_image_input_confirmed",
                advance_revision=False,
            )
        except Exception as error:
            # A verified response remains available while the receipt is pending.
            logger.warning("Native image confirmation deferred (%s)", type(error).__name__)

    async def _read_role_response(
        self,
        prepared: NativeRequest,
        response: httpx.Response,
        *,
        on_chunk: Callable[[bytes], Awaitable[None]] | None,
    ) -> BufferedAttempt:
        observation = NativeObservation(
            self,
            prepared.receipt,
            prepared.model,
            prepared.operation,
            response.status_code,
            response.headers,
            image_input=prepared.image_input,
            role_attempt=prepared.role_attempt,
        )
        chunks: list[bytes] = []
        response_headers = None

        async def finish(headers: Mapping[str, str]) -> BufferedAttempt:
            role_result = (
                await self.run_io(self._save_role_result, prepared, headers, chunks)
                if response.status_code < 400
                else None
            )
            await self.run_io(observation.finish, durable_role_result=role_result is not None)
            return BufferedAttempt(response.status_code, dict(headers), chunks, role_result)

        try:
            response_headers = await self.run_io(
                self.received, prepared, response.status_code, response.headers
            )
            async for chunk in response.aiter_raw():
                chunks.append(chunk)
                for _ in observation.role_acknowledgments(chunk):
                    await self.run_io(observation.running)
                if on_chunk is not None and observation.event_stream and response.status_code < 400:
                    try:
                        await on_chunk(chunk)
                    except Exception as error:
                        logger.warning("Role preview deferred (%s)", type(error).__name__)
            return await finish(response_headers)
        except httpx.TransportError as error:
            # A validated terminal frame settles the request even if its
            # network trailer disconnects.
            if (
                response_headers is not None
                and observation.metadata.terminal
                and (
                    observation.metadata.response_id
                    or observation.metadata.failed
                    or observation.metadata.refused
                )
            ):
                return await finish(response_headers)
            await asyncio.shield(self.run_io(observation.fail, error))
            raise
        except asyncio.CancelledError as error:
            try:
                if response_headers is not None and response.status_code < 400:
                    with suppress(RoleResponsePending):
                        await asyncio.shield(
                            self.run_io(self._save_role_result, prepared, response_headers, chunks)
                        )
            finally:
                await asyncio.shield(self.run_io(observation.fail, error))
            raise
        except BaseException as error:
            await asyncio.shield(self.run_io(observation.fail, error))
            raise
        finally:
            await asyncio.shield(response.aclose())
            observation.clear()


class NativeObservation:
    """Observe metadata for streamed and buffered native responses."""

    def __init__(
        self,
        proxy: NativeProviderProxy,
        receipt: dict[str, Any] | None,
        model: str,
        operation: str,
        status: int,
        headers: Mapping[str, str],
        *,
        image_input: NativeImageInput | None = None,
        role_attempt: RoleAttempt | None = None,
    ) -> None:
        self.image_input = image_input
        self.role_attempt = role_attempt
        self.proxy, self.receipt = proxy, receipt
        self.operation, self.status = operation, status
        self.metadata = NativeMetadata(model, pin_response_identity=True)
        self.event_stream = any(
            k.lower() == "content-type" and "text/event-stream" in v for k, v in headers.items()
        )
        self.collected = bytearray()
        self.retry_after = parse_retry_after(
            next((v for k, v in headers.items() if k.lower() == "retry-after"), None)
        )
        self.last_metadata: tuple[str | None, str | None] = (None, None)
        self.count, self.started = 0, time.monotonic()
        self.finished = False

    def feed(self, chunk: bytes) -> bool:
        self.count += len(chunk)
        if self.event_stream:
            self.metadata.feed(chunk)
        else:
            self.collected.extend(chunk)
        return self.has_new_acknowledgment()

    def role_acknowledgments(self, chunk: bytes) -> Iterator[None]:
        if not self.event_stream:
            self.feed(chunk)
            return
        self.count += len(chunk)
        for event in self.metadata.events.feed(chunk):
            self.metadata.event(event)
            if self.has_new_acknowledgment():
                # Persist an exact acknowledgment before evaluating the next SSE event.
                yield None

    def has_new_acknowledgment(self) -> bool:
        return bool(
            self.receipt
            and self.metadata.response_id
            and self.status < 400
            and self.receipt["state"] not in {"complete", "failed", "cancelled"}
            and (self.metadata.response_id, self.metadata.actual_model) != self.last_metadata
        )

    def _publish(self, state: str, *, result: dict[str, Any]) -> None:
        assert self.receipt is not None
        identifier = self.metadata.response_id
        if self.status < 400 and identifier and self.metadata.actual_model:
            known = role_response_id(self.receipt)
            if known and known != identifier:
                raise RoleResponsePending("Provider response identity changed during collection")
            self.receipt["observed_response_id"] = identifier
        try:
            self.proxy.receipts.publish(
                self.receipt, state, provider_job_id=identifier, result=result
            )
        except Exception:
            if self.role_attempt and role_response_id(self.receipt):
                # Keep the independently bound role checkpoint even when the native
                # receipt spool or shared writer cannot persist this acknowledgment.
                self.role_attempt.record(self.receipt)
            raise

    def running(self) -> None:
        assert self.receipt is not None
        if not self.metadata.response_id and not self.receipt.get("provider_job_id"):
            # The submitting intent is already durable. Read the stream to
            # capture its ID before any optional running journal checkpoint.
            return
        self._publish("running", result={"actual_model": self.metadata.actual_model})
        self.last_metadata = self.metadata.response_id, self.metadata.actual_model
        if self.role_attempt:
            self.role_attempt.record(self.receipt)

    def finish(self, *, durable_role_result: bool = False) -> None:
        metadata, receipt = self.metadata, self.receipt
        if not self.event_stream:
            with suppress(ValueError, UnicodeDecodeError):
                metadata.object(json.loads(self.collected))
        if receipt:
            if (
                self.status < 400
                and metadata.terminal
                and not metadata.failed
                and not metadata.refused
                and not durable_role_result
            ):
                raise ProviderContractError("Native role completion has no durable result")
            current = receipt
            state = (
                "complete"
                if metadata.terminal
                and not metadata.failed
                and not metadata.refused
                and self.status < 400
                else "failed"
                if metadata.terminal and (metadata.failed or metadata.refused) and self.status < 400
                else "running"
                if metadata.response_id or current["provider_job_id"]
                else "unknown"
            )
            error = None
            if self.status >= 400:
                error = classify_error(
                    ProviderHttpStatusError(
                        self.status,
                        definitely_not_accepted=(
                            not metadata.acceptance_indicated
                            and (
                                self.status in {400, 401, 402, 403, 422, 425, 429}
                                or explicitly_not_accepted(bytes(self.collected))
                            )
                        ),
                    ),
                    phase=self.operation,
                    known_job=bool(metadata.response_id or current["provider_job_id"]),
                )
                if metadata.refused:
                    error["category"] = "policy_rejection"
                if hint := role_failure_code(self.collected.decode(errors="replace")):
                    error["code"] = hint
                state = (
                    "failed"
                    if self.operation == "submission"
                    and error["definitely_not_accepted"]
                    and not error["known_job"]
                    else "unknown"
                )
            elif metadata.refused:
                error = {
                    "category": "policy_rejection",
                    "phase": self.operation,
                    "known_job": bool(metadata.response_id),
                    "code": metadata.error_code,
                    "definitely_not_accepted": True,
                    "retryable": True,
                }
            elif metadata.failed:
                rate_limited = (
                    self.operation == "submission"
                    and metadata.rate_limited
                    and not metadata.refused
                    and not metadata.has_output
                )
                unaccepted_limit = (
                    rate_limited
                    and metadata.rejection_indicated
                    and not metadata.acceptance_indicated
                    and not metadata.response_id
                    and not current["provider_job_id"]
                )
                if rate_limited and not unaccepted_limit and not role_response_id(current):
                    state = "unknown"
                error = {
                    "category": "rate_limit" if rate_limited else "native_response_failed",
                    "phase": self.operation,
                    "known_job": bool(metadata.response_id or current["provider_job_id"]),
                    "code": metadata.error_code,
                    **(
                        {
                            "definitely_not_accepted": True,
                            "retryable": True,
                        }
                        if unaccepted_limit
                        else {}
                    ),
                }
            if error is not None and self.retry_after is not None:
                error["retry_after_seconds"] = self.retry_after
            if error is not None and metadata.role_recovery_code:
                error["code"] = metadata.role_recovery_code
            # Query failures never rewrite a known terminal result.
            if current["state"] in {"complete", "failed", "cancelled"}:
                state = current["state"]
            self._publish(
                state,
                result={
                    "actual_model": metadata.actual_model,
                    "usage": metadata.usage,
                    "provider_request_id": metadata.response_id,
                    "error": error,
                },
            )
            record_event(
                self.proxy.coordinator,
                receipt["id"],
                "inference" if self.operation == "submission" else "query",
                state,
                {
                    "http_status": self.status,
                    "bytes": self.count,
                    "elapsed_seconds": time.monotonic() - self.started,
                    "actual_model": metadata.actual_model,
                    "usage": metadata.usage,
                },
            )
        # Provider completion is authoritative; a secondary image-confirmation
        # write must not turn a successful transport into a disconnect/failure.
        self.finished = True
        if (
            self.image_input
            and receipt
            and self.operation == "submission"
            and self.status < 400
            and metadata.terminal
            and not metadata.failed
            and not metadata.refused
            and metadata.response_id
        ):
            self.proxy.confirm_model_image(
                self.image_input, str(receipt["id"]), metadata.response_id
            )

    def fail(self, error: BaseException) -> None:
        if self.receipt is None or self.finished:
            return
        try:
            current = self.receipt
            if current["state"] not in {"complete", "failed", "cancelled"}:
                identifier = role_response_id(current) or self.metadata.validated_role_response_id
                if identifier and not role_response_id(current):
                    current["observed_response_id"] = identifier
                self.proxy.receipts.publish(
                    self.receipt,
                    "unknown",
                    provider_job_id=identifier,
                    result={
                        "error": classify_error(
                            error if isinstance(error, Exception) else RuntimeError(),
                            phase=self.operation,
                            known_job=bool(
                                identifier
                                or self.metadata.response_id
                                or current["provider_job_id"]
                            ),
                        ),
                        "actual_model": self.metadata.actual_model,
                    },
                )
        except Exception as journal_error:
            if self.role_attempt and role_response_id(self.receipt):
                with suppress(Exception):
                    self.role_attempt.record(self.receipt)
            # A sidecar retains any observed ID; cleanup cannot mask the cause.
            logger.error("Native receipt failure deferred (%s)", type(journal_error).__name__)

    def clear(self) -> None:
        self.metadata.events.buffer = b""
        self.collected.clear()
