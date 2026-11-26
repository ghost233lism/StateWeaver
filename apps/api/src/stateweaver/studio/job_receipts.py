"""Durable result handoff independent of a cancelled asyncio waiter.

Provider calls run on daemon workers: shutdown has a real deadline. A late result
is atomically journalled before notifying the event loop, and reconciled against
the exact request and job lease generation. This module never submits a request.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
from collections.abc import Callable
from concurrent.futures import Future
from pathlib import Path
from threading import Thread
from typing import Any

from .contracts import Job, Media
from .job_executor import JobOutcome

LOCAL_VIDEO_RECOVERY_GENERATION = "local_video_terminal_receipt_generation"


class JobReceiptError(RuntimeError):
    """The provider returned, but local durable publication did not complete."""


async def detached_call[T](function: Callable[..., T], *args: Any) -> T:
    future: Future[T] = Future()

    def work() -> None:
        try:
            future.set_result(function(*args))
        except BaseException as error:
            future.set_exception(error)

    Thread(target=work, name="studio-job", daemon=True).start()
    # Cancelling the coroutine cannot cancel a submitted operation or its receipt.
    return await asyncio.shield(asyncio.wrap_future(future))


def request_hash(job: Job) -> str:
    return hashlib.sha256(json.dumps(job.request, sort_keys=True).encode()).hexdigest()


class JobReceipts:
    def __init__(self, root: Path) -> None:
        self.root = root / "job-receipts"

    def path(self, project_id: str, job: Job) -> Path:
        key = f"{project_id}:{job.id}:{job.lease_generation}"
        return self.root / (hashlib.sha256(key.encode()).hexdigest() + ".json")

    def local_video_outcome(self, project_id: str, job: Job) -> JobOutcome | None:
        generation = job.result.get(LOCAL_VIDEO_RECOVERY_GENERATION)
        if generation is None:
            return None
        try:
            if type(generation) is not int or not 0 <= generation < job.lease_generation:
                raise ValueError("Invalid original receipt generation")
            original = self.path(
                project_id, job.model_copy(update={"lease_generation": generation})
            )
            try:
                payload = json.loads(original.read_text())
            except FileNotFoundError:
                payload = json.loads(original.with_suffix(".archived").read_text())
            outcome = self.outcome(payload)
            if (
                payload["project_id"] != project_id
                or payload["job_id"] != job.id
                or payload["generation"] != generation
                or payload["request_hash"] != request_hash(job)
                or outcome.status not in {"complete", "failed"}
                or not job.provider_job_id
                or outcome.provider_job_id != job.provider_job_id
            ):
                raise ValueError("Original outcome identity differs from the active job")
            return outcome
        except (KeyError, TypeError, ValueError, OSError) as error:
            raise JobReceiptError("Saved H3 terminal receipt needs reconciliation") from error

    def save(self, project_id: str, job: Job, outcome: JobOutcome) -> None:
        try:
            self._save(project_id, job, outcome)
        except Exception as error:
            raise JobReceiptError("生成结果本地回执写入失败，原提交需要核对") from error

    def _save(self, project_id: str, job: Job, outcome: JobOutcome) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        payload = {
            "project_id": project_id,
            "job_id": job.id,
            "generation": job.lease_generation,
            "request_hash": request_hash(job),
            "outcome": {
                "status": outcome.status,
                "provider_job_id": outcome.provider_job_id,
                "result": outcome.result,
                "media": [m.model_dump(mode="json") for m in outcome.media],
            },
        }
        descriptor, name = tempfile.mkstemp(prefix=".pending-", dir=self.root)
        try:
            with os.fdopen(descriptor, "w") as stream:
                json.dump(payload, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path(project_id, job))
            directory = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            Path(name).unlink(missing_ok=True)

    @staticmethod
    def outcome(payload: dict[str, Any]) -> JobOutcome:
        data = payload["outcome"]
        return JobOutcome(
            status=data["status"],
            provider_job_id=data.get("provider_job_id"),
            result=data.get("result", {}),
            media=[Media.model_validate(m) for m in data.get("media", [])],
        )
