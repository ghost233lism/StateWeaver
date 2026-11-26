"""Distinguish a synchronous image service refusal from a lost response.

These exact messages originate from our HTTP transport after an HTTP response.
Timeouts, broken connections and gateway/internal failures remain uncertain.
An asynchronous H3 submission must still be reconciled by its original task ID.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from stateweaver.providers.base import ProviderExternalError


class ImageSubmissionRejected(ProviderExternalError):
    """The synchronous image POST received an explicit service refusal."""

    def __init__(
        self,
        http_status: int,
        *,
        details: dict[str, str] | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(f"image submission rejected (HTTP {http_status})")
        self.http_status = http_status
        self.details = details or {}
        self.retry_after_seconds = retry_after_seconds
        self.definitely_not_accepted = True
        self.failure_category = image_failure_category(http_status, self.details)
        self.effective_http_status = 429 if self.failure_category == "rate_limit" else http_status


def image_failure_category(http_status: int, details: Mapping[str, Any]) -> str:
    """Inspect bounded gateway error envelopes, not prompts or arbitrary response fields.

    Preserve the outer HTTP status for diagnostics. Keep the exact, company-confirmed
    ``moderation_blocked`` code distinct so its audit history remains explicit while
    the delivery policy handles all terminal image errors consistently.
    """
    values: list[str] = []

    def visit(value: Any, depth: int = 0) -> None:
        if depth > 6 or len(values) >= 64:
            return
        if isinstance(value, Mapping):
            for key in ("code", "type", "message", "error", "cause", "err", "details"):
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

    visit(details)
    if any(value.strip() == "moderation_blocked" for value in values):
        return "moderation_blocked"
    if any(
        marker in value
        for value in values
        for marker in (
            "content_policy",
            "content policy",
            "policy violation",
            "safety system",
            "safety_violation",
            "safety violation",
            "moderation_blocked",
            "responsibleaipolicyviolation",
            "blocked due to content",
        )
    ):
        return "policy_rejection"
    if http_status == 429 or any(
        value == "429" or "engineoverloaded" in value or "rate_limit_exceeded" in value
        for value in values
    ):
        return "rate_limit"
    return "temporarily_unavailable" if http_status == 503 else "http_rejection"


def image_rejection_status(message: str) -> int | None:
    """Parse a transport HTTP error; this alone does not identify its request phase."""
    match = re.fullmatch(
        r"(?:Provider(?:External|Contract)Error: )?provider (?:unavailable|rejected request) "
        r"\(HTTP (400|401|402|403|422|425|429|503)\)",
        message,
    )
    return int(match[1]) if match else None


def recorded_image_post_rejection(message: str) -> int | None:
    """Only the image POST adapter emits this phase-specific error type."""
    match = re.fullmatch(
        r"ImageSubmissionRejected: image submission rejected "
        r"\(HTTP (400|401|402|403|422|425|429|503)\)",
        message,
    )
    return int(match[1]) if match else None
