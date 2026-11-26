"""Domain-neutral provider contracts and an injectable HTTP boundary."""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from enum import StrEnum
from http.client import HTTPException, HTTPMessage
from io import BytesIO
from typing import IO, Any, Protocol
from urllib.parse import urlsplit


class ProviderError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ProviderFailure:
    """Safe, structured details for a provider-reported terminal failure."""

    provider: str
    status: str
    message: str
    task_id: str | None = None
    code: str | None = None
    retryable: bool = False


class ProviderExternalError(ProviderError):
    """Credentials, quota, network, capacity, and availability failures."""

    def __init__(self, message: str, *, failure: ProviderFailure | None = None) -> None:
        super().__init__(message)
        self.failure = failure


class ProviderContractError(ProviderError):
    pass


class ProviderHttpRejectionError(ProviderContractError):
    """An HTTP rejection with explicitly requested, sanitized diagnostic fields."""

    def __init__(
        self, http_status: int, *, rejection: dict[str, str], request_method: str | None = None
    ) -> None:
        self.http_status = http_status
        self.rejection = rejection
        self.request_method = request_method
        self.definitely_not_accepted = request_method in {None, "POST"}
        super().__init__(f"provider rejected request (HTTP {http_status})")


class ProviderQueryUnavailable(ProviderExternalError):
    """A known remote task could not be queried; never authorize a new POST."""


class ProviderHttpStatusError(ProviderExternalError):
    """Keep explicit HTTP status metadata without assigning any retry policy."""

    def __init__(
        self,
        http_status: int,
        *,
        retry_after_seconds: float | None = None,
        rejection: dict[str, str] | None = None,
        definitely_not_accepted: bool | None = None,
        request_method: str | None = None,
    ) -> None:
        self.http_status = http_status
        self.retry_after_seconds = retry_after_seconds
        self.rejection = rejection or {}
        self.request_method = request_method
        self.definitely_not_accepted = request_method in {None, "POST"} and (
            http_status in {401, 402, 403, 425, 429}
            if definitely_not_accepted is None
            else definitely_not_accepted
        )
        super().__init__(f"provider unavailable (HTTP {http_status})")


class ProviderRequestDeferred(ProviderExternalError):
    """An explicit capacity response; no transport payload or credentials retained."""

    def __init__(self, http_status: int, *, retry_after_seconds: float | None = None) -> None:
        if http_status not in {429, 503}:
            raise ValueError("Only an explicit 429/503 response can defer a request")
        self.http_status = http_status
        self.retry_after_seconds = retry_after_seconds
        self.definitely_not_accepted = True
        super().__init__(f"provider requested deferred retry (HTTP {http_status})")


class ProviderInputContextLimitError(ProviderContractError):
    """A model request exceeded its configured textual context boundary.

    The exception intentionally retains only byte counts.  Model input may contain
    story text, prompts, or memory state and must never be copied into errors or audit
    records merely to explain a local contract failure.
    """

    code = "model_input_context_limit_exceeded"

    def __init__(self, *, actual_bytes: int, max_bytes: int) -> None:
        self.actual_bytes = actual_bytes
        self.max_bytes = max_bytes
        super().__init__(
            "model input text context exceeds the configured UTF-8 byte limit "
            f"(actual_bytes={actual_bytes}, max_bytes={max_bytes})"
        )


class ProviderResponseSizeLimitError(ProviderContractError):
    """A provider response exceeded its explicit transport byte boundary."""

    code = "provider_response_size_limit_exceeded"

    def __init__(self, *, observed_bytes: int, max_bytes: int) -> None:
        self.observed_bytes = observed_bytes
        self.max_bytes = max_bytes
        super().__init__(
            "provider response exceeds the configured byte limit "
            f"(observed_bytes={observed_bytes}, max_bytes={max_bytes})"
        )


def provider_config_hash(config: Mapping[str, Any]) -> str:
    """Hash deterministic provider configuration without exposing its values."""

    try:
        encoded = json.dumps(
            config,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise ProviderContractError("provider configuration is not canonical JSON") from None
    return hashlib.sha256(encoded).hexdigest()


def enforce_utf8_byte_limit(parts: tuple[str, ...], *, max_bytes: int) -> int:
    """Validate aggregate UTF-8 bytes without truncating or retaining source text."""

    if max_bytes <= 0:
        raise ValueError("model input context byte limit must be positive")
    try:
        actual_bytes = sum(len(part.encode("utf-8")) for part in parts)
    except UnicodeEncodeError:
        raise ProviderContractError("model input text context is not valid UTF-8") from None
    if actual_bytes > max_bytes:
        raise ProviderInputContextLimitError(
            actual_bytes=actual_bytes,
            max_bytes=max_bytes,
        )
    return actual_bytes


class ProviderJobError(ProviderError):
    """A well-formed provider job reached a non-retryable failure terminal."""

    def __init__(self, failure: ProviderFailure) -> None:
        self.failure = failure
        code = f" ({failure.code})" if failure.code else ""
        task = f" task {failure.task_id}" if failure.task_id else " job"
        super().__init__(
            f"{failure.provider}{task} ended as {failure.status}{code}: {failure.message}"
        )


class ProviderJobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class ProviderJob:
    provider: str
    task_id: str | None
    state: ProviderJobState
    provider_status: str
    output_id: str | None = None
    endpoint_id: str | None = None
    progress: float | None = None
    failure: ProviderFailure | None = None
    metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def terminal(self) -> bool:
        return self.state in {
            ProviderJobState.SUCCEEDED,
            ProviderJobState.FAILED,
            ProviderJobState.CANCELLED,
        }


@dataclass(frozen=True, slots=True)
class AssetInput:
    name: str
    content: bytes
    media_type: str


@dataclass(frozen=True, slots=True)
class GeneratedAsset:
    media_type: str | None = None
    url: str | None = field(default=None, repr=False)
    base64_data: str | None = field(default=None, repr=False)
    provider_asset_id: str | None = None
    content: bytes | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not (self.url or self.base64_data or self.provider_asset_id or self.content is not None):
            raise ValueError("asset requires url, base64_data, provider_asset_id, or content")


@dataclass(frozen=True, slots=True)
class GenerationResult:
    provider: str
    model: str
    assets: tuple[GeneratedAsset, ...] = ()
    task_id: str | None = None
    status: str = "completed"
    usage: Mapping[str, int] = field(default_factory=dict)
    request_id: str | None = None
    # Absence is unknown, not a fabricated echo of the requested alias.
    observed_model: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class HttpRequest:
    method: str
    url: str = field(repr=False)
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)
    body: bytes | None = field(default=None, repr=False)
    timeout_seconds: float = 30.0
    max_response_bytes: int | None = None
    capture_rejection_details: bool = False
    retain_rejection_details: bool = True
    allow_redirects: bool = True
    audit_operation: str | None = None

    def __post_init__(self) -> None:
        if self.max_response_bytes is not None and (
            isinstance(self.max_response_bytes, bool) or self.max_response_bytes <= 0
        ):
            raise ValueError("max_response_bytes must be a positive integer")


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status_code: int
    headers: Mapping[str, str]
    body: bytes

    def json(self) -> Mapping[str, Any]:
        try:
            value = json.loads(self.body)
        except (ValueError, RecursionError) as exc:
            raise ProviderContractError("provider returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise ProviderContractError("JSON root must be an object")
        return value


class HttpTransport(Protocol):
    def send(self, request: HttpRequest) -> HttpResponse: ...


_SENSITIVE_REQUEST_HEADERS = frozenset(
    {
        "api-key",
        "apikey",
        "authorization",
        "cookie",
        "cookie2",
        "ocp-apim-subscription-key",
        "proxy-authorization",
        "x-access-token",
        "x-amz-security-token",
        "x-api-key",
        "x-auth-token",
        "x-goog-api-key",
    }
)
_DEFAULT_HTTP_PORTS = {"http": 80, "https": 443}


def _http_origin(url: str) -> tuple[str, str, int] | None:
    """Return a normalized HTTP origin, including its effective port."""

    try:
        parsed = urlsplit(url)
        scheme = parsed.scheme.lower()
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if scheme not in _DEFAULT_HTTP_PORTS or hostname is None:
        return None
    effective_port = port if port is not None else _DEFAULT_HTTP_PORTS[scheme]
    return scheme, hostname.lower(), effective_port


def _has_sensitive_request_headers(request: urllib.request.Request) -> bool:
    return any(
        name.lower() in _SENSITIVE_REQUEST_HEADERS for name, _value in request.header_items()
    )


class ProviderRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Keep credentials on the original origin while retaining public redirects."""

    def __init__(self, allow_redirects: bool = True) -> None:
        self.allow_redirects = allow_redirects

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        if not self.allow_redirects:
            return None
        source_origin = _http_origin(req.full_url)
        target_origin = _http_origin(newurl)
        if _has_sensitive_request_headers(req) and (
            source_origin is None or target_origin is None or source_origin != target_origin
        ):
            try:
                fp.close()
            finally:
                raise ProviderContractError(
                    "provider redirect violates authenticated-origin policy"
                )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class UrllibTransport:
    def send(self, request: HttpRequest) -> HttpResponse:
        raw = urllib.request.Request(
            request.url, data=request.body, headers=dict(request.headers), method=request.method
        )
        opener = urllib.request.build_opener(ProviderRedirectHandler(request.allow_redirects))
        try:
            with opener.open(raw, timeout=request.timeout_seconds) as response:
                return HttpResponse(
                    response.status,
                    dict(response.headers),
                    _read_response_body(response, max_bytes=request.max_response_bytes),
                )
        except urllib.error.HTTPError as exc:
            try:
                rejection = {}
                not_accepted = exc.code in {401, 402, 403, 425, 429}
                if request.capture_rejection_details:
                    try:
                        body = _read_response_body(exc, max_bytes=16384)
                        rejection = http_rejection_details(body, request)
                        not_accepted = not_accepted or explicitly_not_accepted(body)
                    except (ProviderContractError, OSError, ValueError, HTTPException):
                        # Diagnostics never change a definite HTTP response into
                        # an uncertain submission or retain an unbounded body.
                        pass
                if exc.code in {401, 402, 403, 408, 425, 429} or exc.code >= 500:
                    raise ProviderHttpStatusError(
                        exc.code,
                        retry_after_seconds=parse_retry_after(exc.headers.get("Retry-After")),
                        rejection=rejection,
                        definitely_not_accepted=not_accepted,
                        request_method=request.method,
                    ) from exc
                if request.capture_rejection_details:
                    raise ProviderHttpRejectionError(
                        exc.code, rejection=rejection, request_method=request.method
                    ) from exc
                raise ProviderContractError(f"provider rejected request (HTTP {exc.code})") from exc
            finally:
                exc.close()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ProviderExternalError("provider network request failed") from exc


def parse_retry_after(value: str | None) -> float | None:
    """Accept a finite delay or HTTP date; never retain other response headers/body."""
    if value is None:
        return None
    try:
        delay = float(value)
    except (ValueError, TypeError):
        try:
            delay = parsedate_to_datetime(value).timestamp() - time.time()
        except (ValueError, TypeError, OverflowError):
            return None
    return delay if math.isfinite(delay) and delay > 0 else None


_RESPONSE_READ_CHUNK_BYTES = 64 * 1024


def _read_response_body(response: IO[bytes], *, max_bytes: int | None) -> bytes:
    """Read one response incrementally and abort as soon as its byte cap is crossed."""

    if max_bytes is None:
        return response.read()

    headers = getattr(response, "headers", None)
    raw_content_length = headers.get("Content-Length") if headers is not None else None
    declared_bytes: int | None = None
    if raw_content_length is not None:
        try:
            declared_bytes = int(raw_content_length)
        except (TypeError, ValueError):
            raise ProviderContractError("provider returned an invalid Content-Length") from None
        if declared_bytes < 0:
            raise ProviderContractError("provider returned an invalid Content-Length")
        if declared_bytes > max_bytes:
            raise ProviderResponseSizeLimitError(
                observed_bytes=declared_bytes,
                max_bytes=max_bytes,
            )

    received_bytes = 0
    output = BytesIO()
    while True:
        remaining_with_sentinel = max_bytes - received_bytes + 1
        chunk = response.read(min(_RESPONSE_READ_CHUNK_BYTES, remaining_with_sentinel))
        if not chunk:
            break
        received_bytes += len(chunk)
        if received_bytes > max_bytes:
            raise ProviderResponseSizeLimitError(
                observed_bytes=received_bytes,
                max_bytes=max_bytes,
            )
        output.write(chunk)
    if declared_bytes is not None and received_bytes != declared_bytes:
        raise ProviderContractError("provider response did not match Content-Length")
    return output.getvalue()


def join_url(base: str, path: str) -> str:
    return f"{base.rstrip('/')}/{path.lstrip('/')}"


def json_request(
    method: str, url: str, key: str | None, payload: Mapping[str, Any] | None, timeout: float
) -> HttpRequest:
    headers = {"Accept": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    body = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload, separators=(",", ":")).encode()
    return HttpRequest(method, url, headers, body, timeout)


def require_success(
    response: HttpResponse, *, request: HttpRequest | None = None
) -> Mapping[str, Any]:
    require_http_success(response, request=request)
    return response.json()


def require_http_success(response: HttpResponse, *, request: HttpRequest | None = None) -> None:
    if (
        request is not None
        and request.capture_rejection_details
        and not 200 <= response.status_code < 300
    ):
        rejection = http_rejection_details(response.body, request)
        if response.status_code in {401, 402, 403, 408, 425, 429} or response.status_code >= 500:
            raise ProviderHttpStatusError(
                response.status_code,
                rejection=rejection,
                retry_after_seconds=parse_retry_after(
                    next(
                        (v for k, v in response.headers.items() if k.lower() == "retry-after"), None
                    )
                ),
                definitely_not_accepted=(
                    response.status_code in {401, 402, 403, 425, 429}
                    or explicitly_not_accepted(response.body)
                ),
                request_method=request.method,
            )
        raise ProviderHttpRejectionError(
            response.status_code, rejection=rejection, request_method=request.method
        )
    if response.status_code in {401, 402, 403, 408, 425, 429} or response.status_code >= 500:
        raise ProviderHttpStatusError(
            response.status_code,
            retry_after_seconds=parse_retry_after(
                next((v for k, v in response.headers.items() if k.lower() == "retry-after"), None)
            ),
            definitely_not_accepted=(
                response.status_code in {401, 402, 403, 425, 429}
                or explicitly_not_accepted(response.body)
            ),
            request_method=request.method if request else None,
        )
    if not 200 <= response.status_code < 300:
        raise ProviderContractError(f"provider rejected request (HTTP {response.status_code})")


def explicitly_not_accepted(body: bytes) -> bool:
    """Only an explicit non-acceptance marker, never a gateway error's prose."""
    if len(body) > 16384:
        return False
    try:
        value = json.loads(body)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return False
    if not isinstance(value, dict):
        return False
    error = value.get("error")
    marker = value if value.get("accepted") is False else error
    return (
        isinstance(marker, dict)
        and marker.get("accepted") is False
        and not any(value.get(k) or marker.get(k) for k in {"id", "task_id", "job_id"})
    )


def require_binary(response: HttpResponse, *, max_bytes: int | None = None) -> bytes:
    """Validate a fully-read media response without exposing its source URL."""

    require_http_success(response)
    if not response.body:
        raise ProviderContractError("provider returned an empty media response")
    if max_bytes is not None and len(response.body) > max_bytes:
        raise ProviderResponseSizeLimitError(
            observed_bytes=len(response.body),
            max_bytes=max_bytes,
        )
    content_type = response_media_type(response) or ""
    if content_type in {
        "application/json",
        "application/problem+json",
    }:
        raise ProviderContractError("provider returned JSON instead of video bytes")
    return response.body


def response_media_type(response: HttpResponse) -> str | None:
    value = next(
        (value for key, value in response.headers.items() if key.lower() == "content-type"), ""
    )
    normalized = value.lower().split(";", 1)[0].strip()
    return normalized or None


_URL_PATTERN = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_EXTERNAL_FAILURE_MARKERS = (
    "auth",
    "api_key",
    "api key",
    "credential",
    "unauthorized",
    "forbidden",
    "quota",
    "rate_limit",
    "rate limit",
    "too many requests",
    "insufficient balance",
    "billing",
    "capacity",
    "overloaded",
    "temporarily unavailable",
    "service unavailable",
    "timeout",
    "timed out",
)
_EXTERNAL_FAILURE_CODES = frozenset(
    {"401", "402", "403", "408", "425", "429", "1002", "1004", "1008"}
)


def safe_provider_text(value: object, *, secrets: tuple[str, ...] = ()) -> str:
    """Bound and redact untrusted provider text before it reaches errors or audit records."""

    if isinstance(value, str):
        rendered = value
    else:
        try:
            rendered = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            rendered = type(value).__name__
    for secret in secrets:
        if secret:
            rendered = rendered.replace(secret, "<redacted>")
    rendered = _URL_PATTERN.sub("<redacted-url>", rendered)
    return rendered[:500]


def http_rejection_details(body: bytes, request: HttpRequest) -> dict[str, str]:
    """Keep only bounded error fields, never raw responses, prompts or credentials."""
    if not request.retain_rejection_details or len(body) > 16384:
        return {}
    try:
        value = json.loads(body)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return {}
    if not isinstance(value, dict):
        return {}
    error = value.get("error", value)
    if isinstance(error, str):
        error = {"message": error}
    if not isinstance(error, dict):
        return {}
    secrets = tuple(
        part
        for name, header in request.headers.items()
        if name.lower() in _SENSITIVE_REQUEST_HEADERS
        for part in (header, header.split(" ", 1)[-1])
        if part
    )
    result = {}
    for key in ("code", "type", "param", "message"):
        original = error.get(key)
        if not isinstance(original, (str, int)) or isinstance(original, bool):
            continue
        text = re.sub(r"data:[^\s,]+,[A-Za-z0-9+/=]+", "<redacted-data>", str(original))
        text = re.sub(
            r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]+",
            "<redacted-credential>",
            text,
        )
        text = re.sub(
            r"(?i)(?:api[-_ ]?key|authorization|credential|password|secret|token|signature)"
            r"[\"']?\s*[:=]\s*[\"']?[^\s,;\"'}]+",
            "<redacted-credential>",
            text,
        )
        result[key] = safe_provider_text(text, secrets=secrets)[: 500 if key == "message" else 128]
    return result


def failure_is_external(failure: ProviderFailure) -> bool:
    code = (failure.code or "").strip().lower()
    if code in _EXTERNAL_FAILURE_CODES:
        return True
    text = f"{code} {failure.message}".lower()
    return any(marker in text for marker in _EXTERNAL_FAILURE_MARKERS)


def raise_for_failed_job(job: ProviderJob) -> None:
    if job.state not in {ProviderJobState.FAILED, ProviderJobState.CANCELLED}:
        return
    failure = job.failure or ProviderFailure(
        provider=job.provider,
        task_id=job.task_id,
        status=job.provider_status,
        message="provider job failed without details",
    )
    if failure.retryable or failure_is_external(failure):
        raise ProviderExternalError(
            f"{failure.provider} job could not complete: {failure.message}", failure=failure
        )
    raise ProviderJobError(failure)
