"""H3 Ref2VA-only asynchronous submission, polling and media collection.

The caller owns durable submission intent, endpoint leases and retry decisions.
This adapter never automatically resubmits a POST, including after timeouts.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.parse import quote

from stateweaver.providers.base import (
    GeneratedAsset,
    HttpRequest,
    HttpTransport,
    ProviderContractError,
    ProviderExternalError,
    ProviderFailure,
    ProviderHttpRejectionError,
    ProviderHttpStatusError,
    ProviderJob,
    ProviderJobState,
    UrllibTransport,
    failure_is_external,
    join_url,
    json_request,
    require_binary,
    require_success,
    safe_provider_text,
)

from .h3_prompt import validate_prompt
from .latent import (
    CONTINUATION_FPS,
    ENDPOINT_ANCHOR,
    EndpointAnchor,
    advertised_modes,
    advertised_tail_frame_counts,
)

H3_MODEL = "MiniMax-H3-Sol"
_STATES = {
    "queued": ProviderJobState.QUEUED,
    "pending": ProviderJobState.QUEUED,
    "submitted": ProviderJobState.QUEUED,
    "in_progress": ProviderJobState.RUNNING,
    "running": ProviderJobState.RUNNING,
    "processing": ProviderJobState.RUNNING,
    "completed": ProviderJobState.SUCCEEDED,
    "succeeded": ProviderJobState.SUCCEEDED,
    "failed": ProviderJobState.FAILED,
    "error": ProviderJobState.FAILED,
    "cancelled": ProviderJobState.CANCELLED,
    "canceled": ProviderJobState.CANCELLED,
}


class SubmissionUncertain(ProviderExternalError):
    """No trustworthy provider ID: reconcile externally, never blind-resubmit."""

    def __init__(self, idempotency_key: str) -> None:
        self.idempotency_key = idempotency_key
        super().__init__("H3 submission outcome is unknown; reconciliation is required")


class H3CapabilityUnavailable(ProviderContractError):
    """The fresh probe cannot support a valid, unsubmitted request."""


class H3ProbeUnavailable(ProviderExternalError):
    """The H3 service could not confirm readiness to generate."""


@dataclass(frozen=True, slots=True)
class TimedReference:
    uri: str = field(repr=False)
    duration_seconds: float

    def __post_init__(self) -> None:
        if not math.isfinite(self.duration_seconds) or not 2 <= self.duration_seconds <= 15:
            raise ProviderContractError("H3 video/audio references must each last 2–15 seconds")
        if not Path(self.uri).is_absolute():
            raise ProviderContractError("H3 references require absolute shared media paths")


@dataclass(frozen=True, slots=True)
class Ref2VARequest:
    prompt: str = field(repr=False)
    reference_images: tuple[str, ...] = field(repr=False)
    duration_seconds: float = 6.0
    previous_end_frame: str | None = field(default=None, repr=False)
    aspect_ratio: str = "16:9"
    short_edge: int = 768
    seed: int | None = None
    reference_videos: tuple[TimedReference, ...] = field(default=(), repr=False)
    reference_audio: tuple[TimedReference, ...] = field(default=(), repr=False)
    video_continuation: bool = False
    latent_continuation: EndpointAnchor | None = None
    video_editing: bool = False
    previous_frame_is_soft_reference: bool = False

    def __post_init__(self) -> None:
        if not self.prompt.strip():
            raise ValueError("Ref2VA prompt must not be empty")
        if not (self.reference_images or self.reference_videos or self.previous_end_frame):
            raise ValueError("Ref2VA requires at least one image or video reference")
        if any(not item.strip() for item in self.reference_images):
            raise ValueError("Ref2VA reference image URIs must not be empty")
        if not 5 <= self.duration_seconds <= 362 / 24:
            raise ValueError("H3 Ref2VA duration must be between 5 and 362/24 seconds")
        if self.short_edge <= 0:
            raise ValueError("video short edge must be positive")
        refs = (
            *self.reference_images,
            *((self.previous_end_frame,) if self.previous_end_frame else ()),
        )
        if len(refs) > 9:
            raise ProviderContractError(
                "H3 Ref2VA accepts at most 9 images including the end frame"
            )
        for references in (self.reference_videos, self.reference_audio):
            if len(references) > 3 or sum(r.duration_seconds for r in references) > 15:
                raise ProviderContractError(
                    "H3 allows at most 3 videos and 3 audio references, 15 seconds per modality"
                )
        if len(refs) + len(self.reference_videos) + len(self.reference_audio) > 12:
            raise ProviderContractError("H3 mixed reference inputs must not exceed 12 files")
        if self.video_continuation and not self.reference_videos:
            raise ProviderContractError("Video continuation requires the actual source video")
        if self.latent_continuation is not None and (
            not self.previous_end_frame
            or not self.latent_continuation.endpoint_only
            and (
                not self.video_continuation
                or not self.reference_videos
                or abs(
                    self.reference_videos[0].duration_seconds
                    - self.latent_continuation.tail_frame_count / CONTINUATION_FPS
                )
                >= 0.001
            )
        ):
            raise ProviderContractError(
                "Latent continuation requires the actual endpoint "
                "and exact pinned tail frame window"
            )
        for uri in refs:
            path = Path(uri)
            if not path.is_absolute():
                raise ProviderContractError(
                    "H3 Ref2VA references require absolute shared image paths"
                )
        if self.previous_end_frame is not None and not self.previous_end_frame.strip():
            raise ValueError("previous end frame URI must not be empty")
        if re.fullmatch(r"[1-9][0-9]*:[1-9][0-9]*", self.aspect_ratio) is None:
            raise ValueError("aspect ratio must be positive width:height")


@dataclass(frozen=True, slots=True)
class H3Capabilities:
    model: str
    task_type: str
    workers_ready: int
    workers_total: int
    revision: str = "unknown"
    topology: str = "unknown"
    latent_continuation_modes: tuple[str, ...] = ("off",)
    latent_tail_frame_counts: tuple[int, ...] = (72,)
    latent_endpoint_only: bool = False


@dataclass(frozen=True, slots=True)
class H3Ref2VAAdapter:
    base_url: str = field(repr=False)
    api_key: str = field(repr=False)
    model: str = H3_MODEL
    endpoint_id: str = "h3-ref2va"
    timeout_seconds: float = 120.0
    download_timeout_seconds: float = 300.0
    max_download_bytes: int = 512 * 1024 * 1024
    shared_media_root: Path | None = None
    transport: HttpTransport = field(default_factory=UrllibTransport, repr=False)
    capacity_limit: int | None = None
    sglang_ref2va: bool = False
    ref2va_verified_job: str | None = None

    def __post_init__(self) -> None:
        if not self.model.strip():
            raise ProviderContractError("H3 API requires an explicitly configured Ref2VA model")
        if not self.base_url.strip():
            raise ProviderExternalError("H3 Ref2VA endpoint is missing")
        if min(self.timeout_seconds, self.download_timeout_seconds, self.max_download_bytes) <= 0:
            raise ValueError("timeouts and response byte limits must be positive")
        if self.sglang_ref2va and self.capacity_limit != 1:
            raise ValueError("SGLang 8-GPU Ref2VA requires a single configured worker")

    def capabilities(self) -> H3Capabilities:
        health = self._get_json("/health")
        status = health.get("status")
        if not isinstance(status, str):
            raise ProviderContractError("H3 health response has invalid status")
        if status != "ok":
            raise H3ProbeUnavailable("H3 service reported unhealthy status")
        models = self._get_json("/v1/models")
        entries = models.get("data", [])
        matches = (
            [
                entry
                for entry in entries
                if isinstance(entry, dict) and entry.get("id") == self.model
            ]
            if isinstance(entries, list)
            else []
        )
        if not matches or (
            matches[0].get("task_type") != "ref2va"
            and not (
                self.sglang_ref2va
                and matches[0].get("task_type") == "TI2V"
                and matches[0].get("num_gpus") == 8
            )
        ):
            raise ProviderContractError("H3 endpoint does not advertise the required Ref2VA model")
        if self.sglang_ref2va and matches[0].get("task_type") == "TI2V":
            # TI2V is shared by the Ref2VA and FL2VA partitions. A healthy
            # worker with this label alone does not advertise Ref2VA support.
            task_id = self.ref2va_verified_job
            proof = self._lookup_response(task_id) if task_id else None
            if proof and not self._matches_task_type(proof):
                raise H3ProbeUnavailable("H3 probe identifies a different task type from Ref2VA")
            if (
                not proof
                or self._parse(proof, expected_id=task_id).state != ProviderJobState.SUCCEEDED
            ):
                raise H3ProbeUnavailable("H3 TI2V partition requires a completed Ref2VA probe")
        ready, total = health.get("workers_ready"), health.get("workers_total")
        if (
            ready is None
            and total is None
            and ("busy_job" in matches[0] or self.sglang_ref2va and matches[0].get("num_gpus") == 8)
        ):
            # An eight-GPU tensor-parallel service uses all GPUs for one generation.
            # num_gpus is tensor parallelism, never independent request capacity.
            ready = total = 1
        if type(ready) is not int or type(total) is not int or not 0 <= ready <= total:
            raise ProviderContractError("H3 health response has invalid worker capacity")
        if self.capacity_limit is not None:
            ready, total = min(ready, self.capacity_limit), min(total, self.capacity_limit)
        model = matches[0]
        capacity = model.get("capacity") or {}
        stack = model.get("stack") or {}
        if not isinstance(capacity, dict) or not isinstance(stack, dict):
            raise ProviderContractError("H3 model response has invalid capability metadata")
        latent = model.get("latent_continuation") or stack.get("latent_continuation") or {}
        if not isinstance(latent, dict):
            raise ProviderContractError("H3 model response has invalid latent capability metadata")
        return H3Capabilities(
            self.model,
            "ref2va",
            ready,
            total,
            str(
                health.get("revision")
                or capacity.get("revision")
                or stack.get("deploy_revision")
                or "unknown"
            ),
            str(
                model.get("topology")
                or health.get("topology")
                or (f"cp{model['num_gpus']}" if model.get("num_gpus") else "unknown")
            ),
            advertised_modes(latent),
            advertised_tail_frame_counts(latent),
            latent.get("endpoint_only") is True,
        )

    def verified_capacity(self) -> int:
        """Live ready-worker ceiling; transient health failure never means unlimited."""
        return self.capabilities().workers_ready

    def _submission_root(self) -> Path:
        root = self.shared_media_root
        if root is None or not root.is_absolute():
            raise ProviderContractError("H3 submissions require an absolute shared_media_root")
        return root

    def payload(self, request: Ref2VARequest) -> dict[str, Any]:
        root = self._submission_root().resolve()
        refs = list(request.reference_images)
        prompt = request.prompt
        if request.previous_end_frame is not None:
            # Ref2VA reference conditioning is not a hard first-frame guarantee.
            # Include the actual accepted boundary and explicitly request its state.
            refs.insert(0, request.previous_end_frame)
        validate_prompt(
            prompt,
            len(refs),
            continuation=request.previous_end_frame is not None
            and not request.previous_frame_is_soft_reference,
            video_count=len(request.reference_videos),
            audio_count=len(request.reference_audio),
            video_continuation=request.video_continuation,
            video_editing=request.video_editing,
        )

        all_paths = [
            *refs,
            *(r.uri for r in request.reference_videos),
            *(r.uri for r in request.reference_audio),
        ]
        if any(not Path(uri).resolve().is_relative_to(root) for uri in all_paths):
            raise ProviderContractError(
                "H3 references are outside its configured shared filesystem root"
            )
        result: dict[str, Any] = {
            "model": self.model,
            "task": "ref2va",
            "prompt": prompt,
            "conditions": [
                *({"type": "image", "uri": uri} for uri in refs),
                *({"type": "video", "uri": r.uri} for r in request.reference_videos),
                *({"type": "audio", "uri": r.uri} for r in request.reference_audio),
            ],
            "target": {
                "duration_seconds": request.duration_seconds,
                "short_edge": request.short_edge,
                "aspect_ratio": request.aspect_ratio,
            },
            "num_outputs_per_prompt": 1,
            "num_inference_steps": 30,
        }
        if request.seed is not None:
            result["seed"] = request.seed
        if request.latent_continuation is not None:
            result["latent_continuation"] = request.latent_continuation.payload()
        if self.sglang_ref2va:
            # The SGLang Ref2VA route uses shared file URIs and a top-level
            # duration. Its main_ref2va.py profile specifies the shifts and steps.
            result["conditions"] = [
                {**item, "uri": Path(item["uri"]).resolve().as_uri(), "role": "reference"}
                for item in result["conditions"]
            ]
            duration = float(request.duration_seconds)
            if duration.is_integer():
                result["seconds"] = int(duration)
            result.update(num_inference_steps=50, flow_shift=12.0, audio_flow_shift=3.0)
        return result

    def prepare_submission(self, request: Ref2VARequest, *, idempotency_key: str) -> HttpRequest:
        """Validate and build the wire request before recording its POST intent."""
        if not idempotency_key.strip():
            raise ValueError("persisted submission idempotency key is required")
        payload = self.payload(request)
        if request.latent_continuation is not None:
            try:
                capabilities = self.capabilities()
            except ProviderContractError:
                raise H3CapabilityUnavailable("H3 capability verification failed") from None
            if capabilities.workers_ready == 0:
                raise H3ProbeUnavailable("H3 has no ready workers")
            if ENDPOINT_ANCHOR not in capabilities.latent_continuation_modes:
                raise H3CapabilityUnavailable(
                    "H3 service has not enabled endpoint-anchor-v1; no generation submitted"
                )
            if (
                not request.latent_continuation.endpoint_only
                and request.latent_continuation.tail_frame_count
                not in capabilities.latent_tail_frame_counts
            ):
                raise H3CapabilityUnavailable(
                    "H3 service does not support the requested tail frame count; "
                    "no generation submitted"
                )
            if request.latent_continuation.endpoint_only and not capabilities.latent_endpoint_only:
                raise H3CapabilityUnavailable(
                    "H3 service has not loaded endpoint-only binding support; "
                    "no generation submitted"
                )
        raw = json_request(
            "POST",
            join_url(self.base_url, "/v1/videos"),
            self.api_key,
            payload,
            self.timeout_seconds,
        )
        raw = replace(
            raw,
            headers={**raw.headers, "Idempotency-Key": idempotency_key},
            capture_rejection_details=True,
        )
        if not self.api_key:
            raw = replace(
                raw, headers={k: v for k, v in raw.headers.items() if k.lower() != "authorization"}
            )
        return raw

    def submit(self, request: Ref2VARequest, *, idempotency_key: str) -> ProviderJob:
        prepared = self.prepare_submission(request, idempotency_key=idempotency_key)
        return self.submit_prepared(prepared)

    def submit_prepared(self, raw: HttpRequest) -> ProviderJob:
        idempotency_key = raw.headers["Idempotency-Key"]
        try:
            response = self.transport.send(raw)
        except ProviderHttpStatusError:
            # A typed HTTP reply retains Retry-After and non-acceptance evidence.
            # A generic 503 is still uncertain; the journal must not repost it.
            raise
        except ProviderExternalError:
            # The service has not documented lookup-by-idempotency-key. The header
            # is correlation only; do not claim it makes resubmission safe.
            raise SubmissionUncertain(idempotency_key) from None
        try:
            return self._parse(require_success(response, request=raw))
        except (ProviderContractError, ProviderExternalError):
            if 200 <= response.status_code < 300:
                raise SubmissionUncertain(idempotency_key) from None
            raise

    def poll(self, task_id: str) -> ProviderJob:
        return self._parse(self._get_json(self._job_path(task_id)), expected_id=task_id)

    def lookup(self, task_id: str) -> ProviderJob | None:
        """Read-only route reconciliation for a persisted task ID, never a submission."""
        response = self._lookup_response(task_id)
        return self._parse(response, expected_id=task_id) if response is not None else None

    def _lookup_response(self, task_id: str) -> Mapping[str, Any] | None:
        request = json_request(
            "GET",
            join_url(self.base_url, self._job_path(task_id)),
            self.api_key,
            None,
            min(self.timeout_seconds, 10),
        )
        if not self.api_key:
            request = replace(
                request,
                headers={k: v for k, v in request.headers.items() if k.lower() != "authorization"},
            )
        response = self.transport.send(request)
        if response.status_code == 404:
            return None
        return require_success(response)

    def download(self, task_id: str) -> GeneratedAsset:
        request = HttpRequest(
            "GET",
            join_url(self.base_url, self._job_path(task_id) + "/content"),
            {
                "Accept": "video/mp4",
                **({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}),
            },
            timeout_seconds=self.download_timeout_seconds,
            max_response_bytes=self.max_download_bytes,
            capture_rejection_details=True,
        )
        try:
            response = self.transport.send(request)
            if response.status_code in {404, 409}:
                raise ProviderHttpStatusError(response.status_code, request_method="GET")
        except ProviderHttpRejectionError as error:
            if error.http_status in {404, 409}:
                # A finished job can precede availability of its content object.
                raise ProviderHttpStatusError(error.http_status, request_method="GET") from None
            raise
        content = require_binary(response, max_bytes=self.max_download_bytes)
        # Container decoding is a separate helper. Preserve the original bytes,
        # including native audio streams, for CAS and provenance.
        return GeneratedAsset(media_type="video/mp4", provider_asset_id=task_id, content=content)

    def _get_json(self, path: str) -> Mapping[str, Any]:
        request = json_request(
            "GET", join_url(self.base_url, path), self.api_key, None, self.timeout_seconds
        )
        if not self.api_key:
            request = replace(
                request,
                headers={k: v for k, v in request.headers.items() if k.lower() != "authorization"},
            )
        return require_success(self.transport.send(request))

    @staticmethod
    def _job_path(task_id: str) -> str:
        if not task_id.strip():
            raise ValueError("provider task ID must not be empty")
        return "/v1/videos/" + quote(task_id, safe="")

    def _matches_task_type(self, data: Mapping[str, Any]) -> bool:
        resolved = data.get("resolved")
        descriptors = (data, resolved) if isinstance(resolved, Mapping) else (data,)
        allowed = {"ref2va", "ti2v"} if self.sglang_ref2va else {"ref2va"}
        return all(
            isinstance(task, str) and task.casefold() in allowed
            for descriptor in descriptors
            for field in ("task", "task_type")
            if (task := descriptor.get(field)) is not None
        )

    def _parse(self, data: Mapping[str, Any], *, expected_id: str | None = None) -> ProviderJob:
        task_id, status = data.get("id"), data.get("status")
        if not isinstance(task_id, str) or not task_id.strip():
            raise ProviderContractError("H3 response requires a provider task ID")
        if expected_id is not None and task_id != expected_id:
            raise ProviderContractError("H3 poll returned a different task ID")
        if not isinstance(status, str) or status not in _STATES:
            raise ProviderContractError("H3 response has an unknown job status")
        state = _STATES[status]
        observed_model = data.get("model")
        if observed_model is not None and observed_model != self.model:
            raise ProviderContractError("H3 response returned a different model")
        if not self._matches_task_type(data):
            raise ProviderContractError("H3 response returned a different task type")
        failure = None
        if state in {ProviderJobState.FAILED, ProviderJobState.CANCELLED}:
            error = data.get("error")
            if self.sglang_ref2va and isinstance(error, Mapping):
                error = error.get("message") or error
            message = safe_provider_text(
                str(error or "H3 generation failed"), secrets=(self.api_key,)
            )
            failure = ProviderFailure("h3-ref2va", status, message, task_id)
            failure = replace(failure, retryable=failure_is_external(failure))
        return ProviderJob(
            "h3-ref2va",
            task_id,
            state,
            status,
            output_id=task_id if state == ProviderJobState.SUCCEEDED else None,
            endpoint_id=self.endpoint_id,
            failure=failure,
            metrics={
                **({"actual_model": observed_model} if observed_model else {}),
                **{
                    key: data[key]
                    for key in ("inference_time_s", "peak_memory_mb", "resolved")
                    if key in data
                },
            },
        )
