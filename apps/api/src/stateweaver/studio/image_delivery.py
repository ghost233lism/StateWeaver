"""Bounded retries of the original fixed image request after definite rejection."""

from __future__ import annotations

import math
from typing import Any, Protocol

from stateweaver.providers.base import ProviderContractError
from stateweaver.studio.contracts import Job, now

RETRYABLE_IMAGE_ERRORS = frozenset({"rate_limit", "temporarily_unavailable"})


class DeliverySettings(Protocol):
    max_retries: int
    base_delay_seconds: float
    max_delay_seconds: float


def initialize(job: Job, primary_model: str, settings: DeliverySettings) -> dict[str, Any]:
    """Persist same-request retry policy and model when first dispatched."""
    if "image_delivery" not in job.result:
        job.result["image_delivery"] = {
            "version": "image-delivery-v2",
            "model": primary_model,
            "retry_limit": settings.max_retries,
            "backoff_base_seconds": settings.base_delay_seconds,
            "backoff_max_seconds": settings.max_delay_seconds,
            "state": "ready",
            "history": [],
        }
    delivery: dict[str, Any] = job.result["image_delivery"]
    if delivery.get("version") != "image-delivery-v2" or delivery.get("model") != primary_model:
        raise ProviderContractError("Image retry policy or fixed model changed")
    return delivery


def schedule_capacity_retry(
    job: Job,
    *,
    primary_model: str,
    settings: DeliverySettings,
    category: str,
    http_status: int | None,
    retry_after_seconds: float | None,
    stamp: float,
    definitely_not_accepted: bool = True,
    outcome_known: bool = False,
) -> bool:
    """Wait on the original registration only after a definitely rejected POST."""
    if category not in RETRYABLE_IMAGE_ERRORS or job.provider_job_id or not definitely_not_accepted:
        return False
    delivery = initialize(job, primary_model, settings)
    if delivery["state"] in {"complete", "exhausted", "policy_blocked"}:
        return False
    history = delivery["history"]
    failures = sum(bool(item.get("category")) for item in history)
    entry = {
        "attempt": failures + 1,
        "model": primary_model,
        "category": category,
        "http_status": http_status,
        "failed_at": now(),
        "receipt_id": job.request.get("api_receipt_id"),
        "receipt_generation": job.result.get("api_receipt_generation"),
        "same_registration": True,
        "definitely_not_accepted": definitely_not_accepted,
        "outcome_known": outcome_known,
    }
    history.append(entry)
    delivery.update(
        last_attempted_model=primary_model,
        last_failure_category=category,
        last_receipt_id=entry["receipt_id"],
    )
    if failures >= delivery["retry_limit"]:
        delivery["state"] = "exhausted"
        job.next_poll_at = 0
        entry["action"] = "exhausted"
        job.result.setdefault("technical_retry_history", []).append(entry.copy())
        return False
    local_delay = min(
        delivery["backoff_max_seconds"],
        delivery["backoff_base_seconds"] * 2 ** min(failures, 30),
    )
    hint = retry_after_seconds or 0
    if not math.isfinite(hint) or hint < 0:
        hint = 0
    job.status = "queued"
    job.next_poll_at = stamp + max(local_delay, hint)
    delivery["state"] = "backoff"
    entry.update(
        action="retry_same_binding", retry_number=failures + 1, retry_after=job.next_poll_at
    )
    job.result.setdefault("technical_retry_history", []).append(entry.copy())
    return True


def record_success(
    job: Job,
    *,
    effective_model_name: str,
    actual_model: str,
    actual_model_source: str,
    receipt_id: str | None,
) -> None:
    """Persist the effective binding and provider-reported (or bound) model."""
    delivery = job.result.get("image_delivery")
    if not isinstance(delivery, dict):
        return
    completed_at = now()
    delivery.update(
        state="complete",
        effective_model=effective_model_name,
        actual_model=actual_model,
        actual_model_source=actual_model_source,
        completed_at=completed_at,
    )
    if not any(
        h.get("action") == "complete"
        and h.get("receipt_id") == receipt_id
        and h.get("actual_model") == actual_model
        for h in delivery.get("history", [])
    ):
        delivery.setdefault("history", []).append(
            {
                "attempt": 1 + sum(bool(h.get("category")) for h in delivery.get("history", [])),
                "model": effective_model_name,
                "action": "complete",
                "actual_model": actual_model,
                "actual_model_source": actual_model_source,
                "receipt_id": receipt_id,
                "completed_at": completed_at,
            }
        )
