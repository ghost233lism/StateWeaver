"""A deliberately small audit vocabulary; never persist opaque provider bodies."""

from __future__ import annotations

import hashlib
import logging
import math
import re
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from .base import HttpRequest, HttpResponse, HttpTransport, ProviderContractError

_PRIVATE_KEYS = frozenset(
    {"api_key", "token", "access_token", "authorization", "password", "secret", "headers"}
)
_PRIVATE_PARTS = ("thought", "signature", "encrypted", "private_uri", "upload_url")
logger = logging.getLogger(__name__)


def record_event(
    coordinator: Any,
    receipt_id: str,
    operation: str,
    status: str,
    metadata: Mapping[str, Any] | None = None,
) -> None:
    """Optional telemetry never hides a returned result or the original failure."""
    try:
        coordinator.event(receipt_id, operation, status, metadata, writer_timeout_seconds=0.25)
    except Exception as error:
        logger.error(
            "API audit write deferred receipt=%s type=%s", receipt_id, type(error).__name__
        )


def structural(value: Any) -> Any:
    """Validate structural configuration, failing closed without echoing its values."""
    if isinstance(value, Mapping):
        if any(
            str(k).lower() in _PRIVATE_KEYS or any(p in str(k).lower() for p in _PRIVATE_PARTS)
            for k in value
        ):
            raise ProviderContractError("Structural API metadata contains private fields")
        return {str(k): structural(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [structural(v) for v in value]
    if isinstance(value, str):
        if "://" in value or value.startswith(("data:", "Bearer ", "sk-")):
            raise ProviderContractError("Structural API metadata cannot contain private URIs/keys")
        return value
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise ProviderContractError("API audit metadata must be finite structural JSON")


def numeric_usage(value: Any) -> dict[str, Any] | None:
    """Only provider-reported numerical usage; absence remains unknown, not zero."""
    if not isinstance(value, Mapping):
        return None
    result: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_]+", key):
            continue
        if key.lower() in _PRIVATE_KEYS or any(p in key.lower() for p in _PRIVATE_PARTS):
            continue
        if isinstance(item, Mapping):
            nested = numeric_usage(item)
            if nested:
                result[key] = nested
        elif not isinstance(item, bool) and isinstance(item, (int, float)):
            try:
                valid = math.isfinite(item) and item >= 0
            except (OverflowError, ValueError):
                valid = False
            if valid:
                result[key] = item
    return result or None


def atomic_id(value: Any) -> str | None:
    return (
        value
        if isinstance(value, str)
        and re.fullmatch(r"[A-Za-z0-9_.:/-]{1,255}", value)
        and "://" not in value
        else None
    )


class AuditedTransport:
    """Hash exact wire bytes and classify operations without keeping bodies or URLs."""

    def __init__(self, transport: HttpTransport, coordinator: Any, receipt_id: str) -> None:
        self.transport, self.coordinator, self.receipt_id = transport, coordinator, receipt_id

    def send(self, request: HttpRequest) -> HttpResponse:
        path = urlsplit(request.url).path.rstrip("/")
        operation = request.audit_operation or (
            "submission"
            if request.method == "POST"
            else "capabilities"
            if path.endswith(("/health", "/models"))
            else "query"
            if "/videos/" in path and not path.endswith(("/content", "/download"))
            else "download"
        )
        started = time.monotonic()
        metadata: dict[str, Any] = {
            "wire_request_hash": hashlib.sha256(request.body or b"").hexdigest(),
            "request_bytes": len(request.body or b""),
            "method": request.method,
        }
        try:
            response = self.transport.send(request)
        except Exception:
            record_event(
                self.coordinator,
                self.receipt_id,
                operation,
                "transport_failed",
                {**metadata, "elapsed_seconds": time.monotonic() - started},
            )
            raise
        metadata.update(
            http_status=response.status_code,
            bytes=len(response.body),
            elapsed_seconds=time.monotonic() - started,
        )
        record_event(self.coordinator, self.receipt_id, operation, "received", metadata)
        return response
