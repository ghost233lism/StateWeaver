"""HTTP video generation without changing Studio's job and delivery lifecycle."""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote, urljoin, urlsplit

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
    _http_origin,
    join_url,
    require_binary,
    require_http_success,
    require_success,
)
from stateweaver.providers.video_http_config import (
    HTTP_VIDEO_PROVIDER,
    VideoHTTPConfig,
    render_template,
)

from .h3 import Ref2VARequest

_MAX_JSON = 1 << 20
_MAX_REFERENCE_BYTES = 64 << 20
_MAX_VIDEO_BYTES = 512 << 20
_TASK_ID = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.:-]{0,254}")
_REFERENCE_LABEL = re.compile(r"<(Picture|Video|Audio) ([0-9]+)>")


def _get(data: Any, path: str) -> Any:
    for key in path.split("."):
        if isinstance(data, Mapping):
            data = data.get(key)
        elif isinstance(data, list) and key.isascii() and key.isdecimal() and len(key) <= 6:
            index = int(key)
            data = data[index] if index < len(data) else None
        else:
            return None
    return data


def _task_id(value: Any) -> str:
    if type(value) is int:
        value = str(value)
    if not isinstance(value, str) or not _TASK_ID.fullmatch(value):
        raise ProviderContractError("Video API did not identify a valid task")
    return value


def text_only_prompt(prompt: str, *, language: Literal["zh-CN", "en"] = "en") -> str:
    """Retain scene descriptions while marking numbered references as prose."""
    prompt = re.sub(r"(?m)^(summary:\s*)\[[^\]\n]+\]\s*", r"\1", prompt)
    if language == "zh-CN":
        labels = {"Picture": "图片", "Video": "视频", "Audio": "音频"}
        prompt = _REFERENCE_LABEL.sub(
            lambda match: f"文字描述中的{labels[match[1]]} {match[2]}", prompt
        )
        return (
            "根据文字描述生成镜头和声音。当前请求只有文字，人物外观与前段状态以文字描述为准。"
            "\n\n" + prompt
        )
    prompt = _REFERENCE_LABEL.sub(lambda match: f"described {match[1].lower()} {match[2]}", prompt)
    return (
        "Generate from the written shot and sound descriptions. No image, video, or audio "
        "inputs are attached. Use the written descriptions of the subjects and prior scene "
        "state as context.\n\n" + prompt
    )


class VideoHTTPSubmissionUnknown(ProviderExternalError):
    def __init__(self) -> None:
        super().__init__("Video API submission outcome is unknown; reconcile the original request")


@dataclass(frozen=True, slots=True)
class HTTPVideoAdapter:
    base_url: str = field(repr=False)
    api_key: str = field(repr=False)
    model: str
    endpoint_id: str
    timeout_seconds: float
    config: VideoHTTPConfig
    transport: HttpTransport = field(default_factory=UrllibTransport, repr=False)

    def _headers(self, accept: str, *, auth: bool) -> dict[str, str]:
        if auth and self.config.auth_required and not self.api_key:
            raise ProviderExternalError("Configured video API credential reference is unavailable")
        headers = {"Accept": accept}
        if auth and self.api_key:
            headers[self.config.auth_header] = self.config.auth_prefix + self.api_key
        return headers

    def _request(
        self,
        path: str,
        *,
        operation: str,
        max_bytes: int = _MAX_JSON,
        timeout: float | None = None,
    ) -> HttpRequest:
        return HttpRequest(
            "GET",
            join_url(self.base_url, path),
            self._headers("application/json", auth=True),
            timeout_seconds=self.timeout_seconds if timeout is None else timeout,
            max_response_bytes=max_bytes,
            capture_rejection_details=True,
            allow_redirects=False,
            retain_rejection_details=False,
            audit_operation=operation,
        )

    def probe(self) -> None:
        if self.config.health_path:
            request = self._request(self.config.health_path, operation="capabilities")
            require_http_success(self.transport.send(request), request=request)
        else:
            self._headers("application/json", auth=True)

    def prepare_submission(self, request: Ref2VARequest, *, idempotency_key: str) -> HttpRequest:
        if request.latent_continuation or request.video_continuation:
            raise ProviderContractError("Video HTTP mode does not provide H3 latent continuation")
        config = self.config
        if request.video_editing and not config.edit_submit_path:
            raise ProviderContractError("Video HTTP mode has no configured edit endpoint")
        headers = self._headers("application/json", auth=True)
        values: dict[str, Any] = {
            "model": self.model,
            "prompt": request.prompt,
            "duration_seconds": request.duration_seconds,
            "aspect_ratio": request.aspect_ratio,
            "short_edge": request.short_edge,
            "seed": request.seed,
            "job_key": idempotency_key,
        }
        if config.reference_policy == "references":
            images = tuple(filter(None, (request.previous_end_frame, *request.reference_images)))
            media = {
                "images": tuple(Path(path) for path in images),
                "videos": tuple(Path(item.uri) for item in request.reference_videos),
                "audio": tuple(Path(item.uri) for item in request.reference_audio),
            }
            budget = _MAX_REFERENCE_BYTES
            for name, paths in media.items():
                if paths and name not in config.inputs:
                    raise ProviderContractError(f"Video API has no mapping for the required {name}")
                inputs = []
                for path in paths:
                    with path.open("rb") as stream:
                        content = stream.read(budget + 1)
                    budget -= len(content)
                    if budget < 0:
                        raise ProviderContractError("Video API reference upload exceeds 64 MiB")
                    inputs.append(base64.b64encode(content).decode("ascii"))
                values[name] = inputs
        submit_path = config.edit_submit_path if request.video_editing else config.submit_path
        assert submit_path is not None
        body = json.dumps(render_template(config.request_body, values), separators=(",", ":"))
        return HttpRequest(
            "POST",
            join_url(self.base_url, submit_path),
            {**headers, "Content-Type": "application/json", "Idempotency-Key": idempotency_key},
            body.encode(),
            self.timeout_seconds,
            max_response_bytes=_MAX_JSON,
            capture_rejection_details=True,
            allow_redirects=False,
            retain_rejection_details=False,
            audit_operation="submission",
        )

    def submit(self, request: Ref2VARequest, *, idempotency_key: str) -> ProviderJob:
        return self.submit_prepared(
            self.prepare_submission(request, idempotency_key=idempotency_key)
        )

    def submit_prepared(self, request: HttpRequest) -> ProviderJob:
        try:
            response = self.transport.send(request)
            payload = require_success(response, request=request)
            identifier = _task_id(_get(payload, self.config.submit_id_field))
        except ProviderHttpStatusError:
            raise
        except ProviderHttpRejectionError as error:
            # Redirects and conflicts can follow an already accepted creation.
            if not 400 <= error.http_status < 500 or error.http_status in {409, 499}:
                raise VideoHTTPSubmissionUnknown() from None
            raise
        except (ProviderExternalError, ProviderContractError):
            raise VideoHTTPSubmissionUnknown() from None
        # A valid ID proves acceptance even if the submit response omits or adds
        # status values. Only an exact-ID query establishes terminal state.
        return ProviderJob(HTTP_VIDEO_PROVIDER, identifier, ProviderJobState.QUEUED, "accepted")

    def _status(self, task_id: str) -> tuple[ProviderJob, Mapping[str, Any]]:
        path = self.config.status_path.replace("{id}", quote(_task_id(task_id), safe=""))
        request = self._request(path, operation="query", timeout=min(self.timeout_seconds, 10))
        data = require_success(self.transport.send(request), request=request)
        if (
            self.config.status_id_field
            and _task_id(_get(data, self.config.status_id_field)) != task_id
        ):
            raise ProviderContractError("Video API status does not match the original task")
        raw = _get(data, self.config.state_field)
        if not isinstance(raw, str) or (state := self.config.states.get(raw)) is None:
            raise ProviderContractError("Video API returned an unmapped task state")
        failure = (
            ProviderFailure(HTTP_VIDEO_PROVIDER, state.value, f"Video API task {state.value}")
            if state in {ProviderJobState.FAILED, ProviderJobState.CANCELLED}
            else None
        )
        return ProviderJob(HTTP_VIDEO_PROVIDER, task_id, state, state.value, failure=failure), data

    def poll(self, task_id: str) -> ProviderJob:
        return self._status(task_id)[0]

    def download(self, task_id: str) -> GeneratedAsset:
        config = self.config
        if config.content_path is not None:
            url = join_url(
                self.base_url,
                config.content_path.replace("{id}", quote(_task_id(task_id), safe="")),
            )
            auth = True
        else:
            status, data = self._status(task_id)
            if status.state != ProviderJobState.SUCCEEDED:
                raise ProviderExternalError("Video API has not confirmed delivery")
            assert config.output_url_field is not None
            output_url = _get(data, config.output_url_field)
            if output_url is None:
                raise ProviderExternalError("Video output URL is not available yet")
            if (
                not isinstance(output_url, str)
                or not output_url
                or re.search(r"[\\\x00-\x20\x7f]", output_url)
            ):
                raise ProviderContractError("Video API did not return a valid MP4 URL")
            try:
                url = urljoin(self.base_url.rstrip("/") + "/", output_url)
                parsed = urlsplit(url)
            except ValueError:
                raise ProviderContractError("Video API did not return a valid MP4 URL") from None
            origin = _http_origin(url)
            auth = origin is not None and origin == _http_origin(self.base_url)
            external = origin in {_http_origin("https://" + host) for host in config.download_hosts}
            if (
                origin is None
                or parsed.username
                or parsed.password
                or parsed.fragment
                or (not auth and not external)
            ):
                raise ProviderContractError(
                    "Video download origin is outside the configured API/CDN"
                )
        http = HttpRequest(
            "GET",
            url,
            self._headers("video/mp4", auth=auth),
            timeout_seconds=self.timeout_seconds,
            max_response_bytes=_MAX_VIDEO_BYTES,
            capture_rejection_details=True,
            allow_redirects=False,
            retain_rejection_details=False,
            audit_operation="download",
        )
        try:
            response = self.transport.send(http)
            if response.status_code in {404, 409}:
                raise ProviderHttpStatusError(response.status_code, request_method="GET")
        except ProviderHttpRejectionError as error:
            if error.http_status in {404, 409}:
                raise ProviderHttpStatusError(error.http_status, request_method="GET") from None
            raise
        content = require_binary(response, max_bytes=_MAX_VIDEO_BYTES)
        if content[4:8] != b"ftyp":
            raise ProviderContractError("Video HTTP delivery must be an MP4 file")
        return GeneratedAsset(media_type="video/mp4", provider_asset_id=task_id, content=content)
