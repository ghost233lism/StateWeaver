"""Secret-safe normalization and stable identities for durable metadata."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

_SENSITIVE_KEY_PARTS = (
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "password",
    "secret",
    "signature",
    "token",
)
_URL_PATTERN = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_AUTH_PATTERN = re.compile(
    r"\b(?:basic|bearer)\s+[A-Za-z0-9._~+/=-]+",
    re.IGNORECASE,
)
_SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"(?:[\"']?(?:authorization|api[-_ ]?key|credential|password|secret|signature|token)"
    r"[\"']?\s*[:=]\s*[\"']?)(?:basic\s+|bearer\s+)?[^\s,;\"'}]+[\"']?",
    re.IGNORECASE,
)


def redact_durable_value(value: Any, *, key: str = "") -> Any:
    """Return a JSON-compatible value with credentials and remote URLs removed.

    Provider URLs can be short-lived signed capabilities even when their query
    parameters use an unfamiliar name. Durable provenance therefore records that
    a URL existed, never the URL itself. Local ``cas://`` identifiers are retained.
    """

    normalized_key = key.lower().replace("-", "_").replace(" ", "_")
    if any(part in normalized_key for part in _SENSITIVE_KEY_PARTS):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {
            str(item_key): redact_durable_value(item_value, key=str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_durable_value(item) for item in value]
    if isinstance(value, str):
        redacted = _URL_PATTERN.sub("[REDACTED_URL]", value)
        redacted = _SECRET_ASSIGNMENT_PATTERN.sub("[REDACTED_CREDENTIAL]", redacted)
        return _AUTH_PATTERN.sub("[REDACTED_CREDENTIAL]", redacted)
    return value


def sanitize_provenance(value: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize one artifact provenance object before persistence or hashing."""

    sanitized = redact_durable_value(value)
    if not isinstance(sanitized, dict):  # pragma: no cover - Mapping always yields a dict
        raise ValueError("artifact provenance must be an object")
    try:
        json.dumps(sanitized, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise ValueError("artifact provenance must be JSON serializable") from exc
    return sanitized


def artifact_identity_hash(
    *,
    sha256: str,
    media_type: str,
    byte_size: int,
    relative_path: str,
    provenance: Mapping[str, Any],
) -> str:
    """Hash logical artifact metadata while physical bytes remain CAS-deduplicated."""

    encoded = json.dumps(
        {
            "sha256": sha256,
            "media_type": media_type,
            "byte_size": byte_size,
            "relative_path": relative_path,
            "provenance": provenance,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
