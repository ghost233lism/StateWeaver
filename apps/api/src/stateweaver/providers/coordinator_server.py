"""Private owner routes on an existing API; journal locks cover only transactions."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import sqlite3
import time
from collections import OrderedDict
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .base import ProviderContractError
from .coordination import (
    ProviderCapacityWait,
    ProviderCoordinator,
    ProviderReceiptLeaseLost,
    ProviderSubmissionUnknown,
)
from .coordinator_client import OPERATIONS, READ_OPERATIONS, ROUTE, decode, encode


class Command(BaseModel):
    version: int
    request_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    ledger: str
    operation: str
    args: list[Any] = Field(default_factory=list)
    kwargs: dict[str, Any] = Field(default_factory=dict)


class CoordinatorOwner:
    def __init__(
        self,
        coordinator: ProviderCoordinator,
        canonical_path: Path,
        token_file: Path,
        recovery_path: Path | None = None,
        *,
        admission_paused: bool = False,
    ) -> None:
        if coordinator.is_remote:
            raise ProviderContractError(
                "An API coordination owner must use its explicit local journal"
            )
        self.coordinator, self.canonical_path = coordinator, canonical_path.resolve()
        from .coordination_storage import CoordinatorBackup

        self.backup = CoordinatorBackup(coordinator.path, recovery_path) if recovery_path else None
        if self.backup:
            self.backup.restore_if_missing()
        coordinator.prepare()
        self.admission_paused = admission_paused
        self.token = token_file.read_text().strip()
        if len(self.token) < 32:
            raise ProviderContractError("Private coordinator authentication is not configured")
        # Prefetch/serialization can run concurrently. Only coordinator.connection's
        # local transaction lock is serialized, never an entire public method.
        self.read_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="coordinator-read")
        self.bulk_read_pool = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="coordinator-bulk-read"
        )
        self.operation_pool = ThreadPoolExecutor(
            max_workers=8, thread_name_prefix="coordinator-operation"
        )
        self.outcome_pool = ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="coordinator-outcome"
        )
        self.pending: dict[str, tuple[str, asyncio.Future[Any]]] = {}
        self.read_pending: dict[str, asyncio.Future[Any]] = {}
        self.completed: OrderedDict[str, tuple[str, float, Any]] = OrderedDict()

    @property
    def admission_paused(self) -> bool:
        return self.coordinator.admission_paused

    @admission_paused.setter
    def admission_paused(self, value: bool) -> None:
        self.coordinator.admission_paused = value

    async def close(self) -> None:
        await asyncio.to_thread(self.operation_pool.shutdown, wait=True)
        await asyncio.to_thread(self.outcome_pool.shutdown, wait=True)
        await asyncio.to_thread(self.read_pool.shutdown, wait=True)
        await asyncio.to_thread(self.bulk_read_pool.shutdown, wait=True)
        if self.backup:
            await asyncio.to_thread(self.backup.close)
        self.coordinator.close()

    def status(self) -> dict[str, Any]:
        return {
            "mode": "local_owner",
            "admission_paused": self.admission_paused,
            "pending_commands": len(self.pending),
            "pending_reads": len(self.read_pending),
            "backup": self.backup.status() if self.backup else {"state": "not_configured"},
        }

    def execute(self, command: Command) -> Any:
        args, kwargs = decode(command.args), decode(command.kwargs)
        if command.operation == "begin" and self.admission_paused:
            row = self.coordinator.get(args[0])
            if row["state"] == "reserved":
                raise ProviderCapacityWait(time.time() + 1, "coordinator_maintenance")
        if command.operation == "query":
            sql, parameters = args
            if not isinstance(sql, str) or not sql.lstrip().upper().startswith("SELECT "):
                raise ProviderContractError("Private read compatibility accepts only SELECT")
            with self.coordinator.connection(write=False) as db:
                db.execute("PRAGMA query_only=ON")
                cursor = db.execute(sql, parameters)
                return {
                    "columns": [str(column[0]) for column in cursor.description or ()],
                    "rows": [list(row) for row in cursor.fetchall()],
                }
        return encode(getattr(self.coordinator, command.operation)(*args, **kwargs))

    async def invoke(self, command: Command) -> Any:
        if command.version != 1 or command.operation not in OPERATIONS:
            raise ProviderContractError("Unsupported private coordination operation")
        if Path(command.ledger).resolve() != self.canonical_path:
            raise ProviderContractError(
                "Private coordination request belongs to a different ledger"
            )
        digest = hashlib.sha256(
            json.dumps(
                {"operation": command.operation, "args": command.args, "kwargs": command.kwargs},
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode()
        ).hexdigest()
        # Deduplicate transport retries during the short request window without
        # adding a second permanent request/event ledger. A restart keeps the
        # receipt submission intent; it never grants a fresh provider POST.
        if command.operation not in READ_OPERATIONS:
            now = time.monotonic()
            while self.completed:
                first, item = next(iter(self.completed.items()))
                if len(self.completed) <= 2048 and item[1] > now - 30:
                    break
                self.completed.pop(first)
            cached = self.completed.get(command.request_id)
            if cached:
                if cached[0] != digest:
                    raise ProviderContractError("Coordination retry changed its command")
                return cached[2]
            pending = self.pending.get(command.request_id)
            if pending:
                if pending[0] != digest:
                    raise ProviderContractError("Coordination retry changed its command")
                return await asyncio.shield(pending[1])
        if command.operation == "reserve" and self.admission_paused:
            raise ProviderCapacityWait(time.time() + 1, "coordinator_maintenance")
        if command.operation in READ_OPERATIONS:
            # Share only an actual in-progress identical read. There is no
            # completed-result cache: the next read observes current journal state.
            previous_read = self.read_pending.get(digest)
            if previous_read is not None and not previous_read.done():
                return await asyncio.shield(previous_read)
            bulk_read = (
                command.operation == "native_receipt_ids"
                and command.kwargs.get("candidate_ids") is None
            )
            pool = self.bulk_read_pool if bulk_read else self.read_pool
        elif command.operation in {
            "update",
            "release_unsubmitted",
            "claim_collection",
            "adopt",
            "event",
        }:
            pool = self.outcome_pool
        else:
            pool = self.operation_pool
        future = asyncio.get_running_loop().run_in_executor(pool, partial(self.execute, command))
        if command.operation in READ_OPERATIONS:
            self.read_pending[digest] = future

            def read_finished(done: asyncio.Future[Any]) -> None:
                if self.read_pending.get(digest) is done:
                    self.read_pending.pop(digest, None)
                if not done.cancelled():
                    done.exception()  # A disconnected reader must not leak an unobserved error.

            future.add_done_callback(read_finished)
            return await asyncio.shield(future)
        self.pending[command.request_id] = digest, future

        def finished(done: asyncio.Future[Any]) -> None:
            self.pending.pop(command.request_id, None)
            if not done.cancelled() and done.exception() is None:
                self.completed[command.request_id] = digest, time.monotonic(), done.result()

        future.add_done_callback(finished)
        try:
            result = await asyncio.shield(future)
            self.completed[command.request_id] = digest, time.monotonic(), result
            return result
        finally:
            # A disconnected HTTP coroutine must not erase an in-progress
            # command. Keep it until the underlying operation has finished.
            if future.done():
                self.pending.pop(command.request_id, None)


def error_response(error: Exception) -> JSONResponse:
    if isinstance(error, ProviderCapacityWait):
        return JSONResponse(
            {
                "error": {
                    "kind": "capacity_wait",
                    "retry_at": error.retry_at,
                    "reason": error.reason,
                }
            },
            status_code=503,
        )
    if isinstance(error, ProviderSubmissionUnknown):
        return JSONResponse(
            {"error": {"kind": "submission_unknown", "receipt_id": error.receipt_id}},
            status_code=409,
        )
    if isinstance(error, ProviderReceiptLeaseLost):
        return JSONResponse({"error": {"kind": "lease_lost"}}, status_code=409)
    if isinstance(error, ProviderContractError):
        return JSONResponse({"error": {"kind": "contract", "message": str(error)}}, status_code=422)
    if isinstance(error, sqlite3.OperationalError):
        return JSONResponse({"error": {"kind": "database_busy"}}, status_code=503)
    return JSONResponse({"error": {"kind": "unavailable"}}, status_code=503)


def install_owner(
    app: FastAPI,
    coordinator: ProviderCoordinator,
    canonical_path: Path,
    token_file: Path,
    recovery_path: Path | None = None,
    *,
    admission_paused: bool = False,
) -> CoordinatorOwner:
    owner = CoordinatorOwner(
        coordinator, canonical_path, token_file, recovery_path, admission_paused=admission_paused
    )
    previous = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        try:
            if owner.backup:
                owner.backup.start()
            async with previous(application):
                yield
        finally:
            await owner.close()

    app.router.lifespan_context = lifespan
    app.state.coordinator_owner = owner

    @app.post(ROUTE, include_in_schema=False)
    async def command(request: Request) -> JSONResponse:
        authorization = request.headers.get("authorization", "")
        if not hmac.compare_digest(authorization, "Bearer " + owner.token):
            return JSONResponse({"error": {"kind": "unauthorized"}}, status_code=401)
        try:
            body = await request.json()
            operation = Command.model_validate(body)
            result = await owner.invoke(operation)
            return JSONResponse({"result": result, "coordinator": owner.status()})
        except Exception as error:
            return error_response(error)

    @app.post(ROUTE + "/admission", include_in_schema=False)
    async def admission(request: Request) -> JSONResponse:
        if not hmac.compare_digest(
            request.headers.get("authorization", ""), "Bearer " + owner.token
        ):
            return JSONResponse({"error": {"kind": "unauthorized"}}, status_code=401)
        body = await request.json()
        if not isinstance(body, dict) or type(body.get("paused")) is not bool:
            return JSONResponse({"error": {"kind": "contract"}}, status_code=422)
        owner.admission_paused = body["paused"]
        return JSONResponse({"coordinator": owner.status()})

    return owner
