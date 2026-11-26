"""Authenticated coordination calls through the existing private API.

Only the API owner opens its hot journal. A missing/unavailable owner never
causes a client to become another filesystem writer.
"""

from __future__ import annotations

import copy
import json
import os
import sqlite3
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from .base import ProviderContractError
from .coordination import (
    ProviderCapacityWait,
    ProviderCoordinator,
    ProviderReceiptLeaseLost,
    ProviderSubmissionUnknown,
    _validate_result,
)
from .registry import APIService

ROUTE = "/api/internal/provider-coordinator"
READ_OPERATIONS = frozenset(
    {
        "prepare",
        "get",
        "by_provider_id",
        "events",
        "inflight",
        "active",
        "native_receipt_ids",
        "model_cooldown_hint",
        "query",
    }
)
WRITE_OPERATIONS = frozenset(
    {
        "reserve",
        "release_unsubmitted",
        "begin",
        "observe_capacity",
        "claim_collection",
        "adopt",
        "event",
        "update",
        "cooldown",
    }
)
OPERATIONS = READ_OPERATIONS | WRITE_OPERATIONS


class CoordinatorUnavailable(sqlite3.OperationalError):
    """Retry coordination, retaining the original provider result/request."""

    def __init__(self) -> None:
        super().__init__("Private coordination API is temporarily unavailable")
        self.sqlite_errorcode = sqlite3.SQLITE_BUSY
        self.reason = "coordinator_api_unavailable"
        self.retry_at = time.time() + 1


@dataclass(frozen=True)
class ClientConfiguration:
    url: str
    token_file: Path
    epoch: str = ""


def configuration(path: Path) -> ClientConfiguration | None:
    explicit = os.environ.get("AGENTVIDEO_COORDINATOR_URL")
    marker = Path(os.environ.get("AGENTVIDEO_COORDINATOR_MARKER", str(path) + ".coordinator.json"))
    data: dict[str, Any] = {}
    if not explicit and not marker.is_file():
        return None
    if not explicit:
        try:
            data = json.loads(marker.read_text())
        except (OSError, ValueError) as error:
            raise CoordinatorUnavailable() from error
        if data.get("version") != 1:
            raise ProviderContractError("Unsupported private coordinator marker version")
    url = explicit or data.get("url")
    token = os.environ.get("AGENTVIDEO_COORDINATOR_TOKEN_FILE") or data.get("token_file")
    if not isinstance(url, str) or not isinstance(token, str):
        raise ProviderContractError("Private coordinator requires URL and token-file configuration")
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path.rstrip("/") != ROUTE
    ):
        raise ProviderContractError("Invalid private coordinator API URL")
    return ClientConfiguration(url.rstrip("/"), Path(token), str(data.get("epoch", "")))


def coordinator(path: Path, *, direct: bool = False) -> ProviderCoordinator:
    """Compatibility factory; direct=True belongs only to the explicit API owner."""
    return ProviderCoordinator(path, direct=direct)


def selected_client(path: Path) -> ProviderCoordinator | None:
    selected = configuration(path)
    return RemoteProviderCoordinator(path, configuration=selected) if selected else None


def encode(value: Any) -> Any:
    if isinstance(value, APIService):
        return {"$api_service": value.model_dump(mode="json")}
    if isinstance(value, Path):
        return {"$path": str(value)}
    if isinstance(value, Mapping):
        return {str(k): encode(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [encode(v) for v in value]
    return value


def decode(value: Any) -> Any:
    if isinstance(value, dict):
        if set(value) == {"$api_service"}:
            return APIService.model_validate(value["$api_service"])
        if set(value) == {"$path"}:
            return Path(value["$path"])
        return {k: decode(v) for k, v in value.items()}
    if isinstance(value, list):
        return [decode(v) for v in value]
    return value


class RemoteRow:
    def __init__(self, columns: list[str], values: list[Any]) -> None:
        self.columns, self.values = columns, values

    def keys(self) -> list[str]:
        return self.columns

    def __getitem__(self, key: str | int) -> Any:
        return self.values[key if isinstance(key, int) else self.columns.index(key)]

    def __iter__(self) -> Iterator[Any]:
        return iter(self.values)


class RemoteCursor:
    def __init__(self, data: dict[str, Any]) -> None:
        self.rows = [RemoteRow(data["columns"], row) for row in data["rows"]]
        self.position = 0

    def fetchone(self) -> RemoteRow | None:
        if self.position >= len(self.rows):
            return None
        self.position += 1
        return self.rows[self.position - 1]

    def fetchall(self) -> list[RemoteRow]:
        rows, self.position = self.rows[self.position :], len(self.rows)
        return rows

    def fetchmany(self, size: int = 1) -> list[RemoteRow]:
        rows = self.rows[self.position : self.position + size]
        self.position += len(rows)
        return rows

    def __iter__(self) -> Iterator[RemoteRow]:
        return iter(self.fetchall())


class RemoteReadConnection:
    def __init__(self, owner: RemoteProviderCoordinator) -> None:
        self.owner = owner

    def execute(self, sql: str, parameters: Sequence[Any] | Mapping[str, Any] = ()) -> RemoteCursor:
        return RemoteCursor(self.owner.invoke("query", (sql, parameters), {}))


def _without_optional_rejection_diagnostics(kwargs: Mapping[str, Any]) -> dict[str, Any] | None:
    """A validated update may omit only the new optional HTTP diagnostic field."""
    result = kwargs.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("error"), dict):
        return None
    if "upstream_rejection" not in result["error"]:
        return None
    allowed = {
        "actual_model",
        "usage",
        "media",
        "requested_latent",
        "applied_latent",
        "error",
        "provider_request_id",
        "outcome",
    }
    candidate = copy.deepcopy(result)
    data = {key: value for key, value in candidate.items() if value is not None}
    if set(data) - allowed:
        return None
    try:
        _validate_result(copy.deepcopy(data))
        del candidate["error"]["upstream_rejection"]
        _validate_result(
            {key: copy.deepcopy(value) for key, value in candidate.items() if value is not None}
        )
    except (ProviderContractError, TypeError, ValueError, KeyError):
        return None
    return {**kwargs, "result": candidate}


def _without_optional_video_proof(
    args: Sequence[Any], kwargs: Mapping[str, Any], message: str
) -> dict[str, Any] | None:
    """Support older owners that reject only the new bounded video evidence."""
    result = kwargs.get("result")
    if not isinstance(result, dict) or len(args) != 2:
        return None
    data = {key: copy.deepcopy(value) for key, value in result.items() if value is not None}
    if set(data) - {
        "actual_model",
        "usage",
        "media",
        "requested_latent",
        "applied_latent",
        "error",
        "provider_request_id",
        "outcome",
        "video_completion",
    }:
        return None
    try:
        _validate_result(data)
        completion = data.get("video_completion")
        if completion and (
            message == "API result contains non-receipt fields"
            or (
                message == "API recovery metadata violates its exact structural schema"
                and completion.get("invalid_latent_receipt") is True
            )
        ):
            if (
                args[1] not in {"running", "collecting"}
                or kwargs.get("provider_job_id") != data["video_completion"]["provider_job_id"]
                or not kwargs.get("owner")
                or type(kwargs.get("generation")) is not int
            ):
                return None
            del data["video_completion"]
        elif (
            message == "API recovery metadata violates its exact structural schema"
            and args[1] == "complete"
            and "video_delivery" in data.get("outcome", {}).get("result", {})
        ):
            del data["outcome"]["result"]["video_delivery"]
        else:
            return None
    except (ProviderContractError, TypeError, ValueError, KeyError):
        return None
    return {**kwargs, "result": data}


class RemoteProviderCoordinator(ProviderCoordinator):
    def __init__(
        self,
        path: Path,
        *,
        direct: bool = False,
        configuration: ClientConfiguration | None = None,
    ) -> None:
        if "_configuration" in self.__dict__:
            return
        selected = configuration or globals()["configuration"](path)
        if selected is None:
            raise ProviderContractError("Remote coordinator configuration disappeared")
        self.path = path.resolve()
        self._configuration = selected
        self._token: str | None = None
        self._http = httpx.Client(
            trust_env=False,
            follow_redirects=False,
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
            timeout=httpx.Timeout(10, connect=2),
        )

    @property
    def is_remote(self) -> bool:
        return True

    def close(self) -> None:
        self._http.close()

    def __getattribute__(self, name: str) -> Any:
        if name in OPERATIONS and name != "query":

            def call(*args: Any, **kwargs: Any) -> Any:
                result = self.invoke(name, args, kwargs)
                if name == "model_cooldown_hint":
                    return tuple(result)
                return result

            return call
        return super().__getattribute__(name)

    @contextmanager
    def connection(
        self,
        *,
        write: bool = True,
        timeout_seconds: float = 30,
        submission_wait: bool = False,
    ) -> Iterator[sqlite3.Connection]:
        if write:
            raise ProviderContractError("Remote clients cannot open a coordination writer")
        yield cast(sqlite3.Connection, RemoteReadConnection(self))

    def invoke(self, operation: str, args: Sequence[Any], kwargs: Mapping[str, Any]) -> Any:
        try:
            token = self._token or self._configuration.token_file.read_text().strip()
            self._token = token
        except OSError as error:
            raise CoordinatorUnavailable() from error
        if len(token) < 32:
            raise ProviderContractError("Private coordinator authentication is not configured")
        body = {
            "version": 1,
            "request_id": uuid4().hex,
            "ledger": str(self.path),
            "operation": operation,
            "args": encode(args),
            "kwargs": encode(kwargs),
        }
        # Retry this exact RPC once. No provider HTTP request is made here.
        # If an owner's reply is lost, its bounded nonce cache returns that reply.
        for attempt in range(2):
            try:
                response = self._http.post(
                    self._configuration.url,
                    json=body,
                    headers={"Authorization": "Bearer " + token},
                )
            except httpx.RequestError as error:
                if attempt == 0:
                    time.sleep(0.1)
                    continue
                raise CoordinatorUnavailable() from error
            try:
                data = response.json()
            except ValueError as error:
                raise CoordinatorUnavailable() from error
            if not isinstance(data, dict):
                raise CoordinatorUnavailable()
            if response.status_code == 200 and "result" in data:
                return decode(data["result"])
            failure = data.get("error", {})
            if not isinstance(failure, dict):
                raise CoordinatorUnavailable()
            kind = failure.get("kind")
            if kind == "capacity_wait":
                raise ProviderCapacityWait(float(failure["retry_at"]), str(failure["reason"]))
            if kind == "submission_unknown":
                raise ProviderSubmissionUnknown(str(failure["receipt_id"]))
            if kind == "lease_lost":
                raise ProviderReceiptLeaseLost("Coordination owner/generation lease changed")
            if kind == "contract":
                message = str(failure.get("message", "Invalid coordination operation"))
                if response.status_code == 422 and operation == "update":
                    compatible = _without_optional_video_proof(args, kwargs, message)
                    if (
                        compatible is None
                        and message == "API recovery metadata violates its exact structural schema"
                    ):
                        compatible = _without_optional_rejection_diagnostics(kwargs)
                    if compatible is not None:
                        # The owner's exact schema rejection preceded all journal I/O.
                        # A new nonce is required for the changed command. Preserve
                        # the caller's result, receipt, state, owner and generation.
                        return self.invoke(operation, args, compatible)
                raise ProviderContractError(message)
            if kind in {"database_busy", "unavailable"}:
                raise CoordinatorUnavailable()
            raise ProviderContractError("Private coordinator rejected the authenticated operation")
        raise CoordinatorUnavailable()
