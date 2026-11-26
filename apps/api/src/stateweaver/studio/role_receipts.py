"""Durable receipt pointers for a pending first-party role model request."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from stateweaver.providers.audit import atomic_id
from stateweaver.providers.base import ProviderContractError
from stateweaver.providers.coordination import ProviderCoordinator
from stateweaver.storage.cas import ContentAddressedStore

_BINDING_FIELDS = ("id", "service_id", "model", "request_hash", "config_hash")
_UNSENT = {"reserved", "cancelled"}
_TERMINAL = {"complete", "failed"}
_REBUILDABLE = _UNSENT | {"failed"}
_COLLECTIBLE = {"submitting", "running", "collecting", "unknown"}


class RoleRunEnded(RuntimeError):
    """The actor was paused, replaced, or moved to another business context."""


class RoleReconciliationRequired(RuntimeError):
    """A pending request must be repaired or reconciled before further dispatch."""


@dataclass(frozen=True)
class RoleAttempt:
    original: dict[str, Any] | None
    save: Callable[[dict[str, Any]], None]
    check_previous_context: bool = False
    reject_result: Callable[[str], None] | None = None

    def can_reserve_again(self, receipt: Mapping[str, Any]) -> bool:
        return receipt["state"] in _UNSENT

    def record(self, receipt: dict[str, Any], *, historical: bool = False) -> None:
        saved = {
            field: receipt.get(field)
            for field in (*_BINDING_FIELDS, "generation", "state", "provider_job_id")
        }
        if historical:
            saved["historical"] = True
        if not saved["provider_job_id"] and (
            observed := atomic_id(receipt.get("observed_response_id"))
        ):
            saved["observed_response_id"] = observed
        self.save(saved)


def role_response_id(receipt: Mapping[str, Any]) -> str | None:
    return atomic_id(receipt.get("provider_job_id")) or atomic_id(
        receipt.get("observed_response_id")
    )


def rejected_role_result(receipt: Mapping[str, Any]) -> bool:
    return (
        receipt.get("state") == "complete"
        and receipt.get("rejected_result") is True
        and atomic_id(receipt.get("provider_job_id")) is not None
    )


def pending_role_started(journal: Mapping[str, Any]) -> bool:
    if not journal.get("awaiting_model"):
        return False
    return any(
        receipt["state"] not in _UNSENT for receipt in journal.get("attempt_receipts", {}).values()
    )


def role_rebuild_requires_reconciliation(journal: Mapping[str, Any]) -> bool:
    if not journal.get("awaiting_model"):
        return False
    if journal.get("receipt_protocol") != 1:
        return True
    attempts = journal.get("attempt_receipts", {})
    return any(
        receipt["state"] not in _REBUILDABLE
        and not (receipt.get("historical") and receipt["state"] == "complete")
        and not rejected_role_result(receipt)
        for receipt in attempts.values()
    )


def verify_pending_role_assets(
    work_root: Path, journal: Mapping[str, Any], checked: set[str]
) -> None:
    """Verify only the private CAS referenced by a possibly-sent pending model call."""
    try:
        if not pending_role_started(journal):
            return
        root = work_root / "role-context"
        if not root.is_dir():
            raise FileNotFoundError()
        assets = ContentAddressedStore(root)
        for digest in (journal["pending_context"], *journal["messages"]):
            if not isinstance(digest, str):
                raise ValueError()
            if digest not in checked:
                with assets.open(digest):
                    pass
                checked.add(digest)
    except (KeyError, TypeError, ValueError, OSError) as error:
        raise ValueError("StateWeaver pending role CAS is missing or corrupt") from error


def scoped_role_receipt(
    receipt: dict[str, Any], workspace: str, project_id: str, actor_id: str
) -> bool:
    context = receipt.get("context")
    return bool(
        isinstance(context, dict)
        and context.get("operation") == "native_responses"
        and context.get("workspace") == workspace
        and context.get("project_id") == project_id
        and context.get("actor_id") == actor_id
    )


def original_role_receipt(
    coordinator: ProviderCoordinator,
    saved: dict[str, Any],
    workspace: str,
    project_id: str,
    actor_id: str,
) -> dict[str, Any]:
    current = coordinator.get(saved["id"])
    observed = saved.get("observed_response_id")
    if (
        not scoped_role_receipt(current, workspace, project_id, actor_id)
        or any(saved[field] != current[field] for field in _BINDING_FIELDS)
        or current["generation"] < saved["generation"]
        or rejected_role_result(saved)
        and (current["generation"] != saved["generation"] or current["state"] != "complete")
        or saved.get("provider_job_id")
        and current["provider_job_id"] != saved["provider_job_id"]
        or observed is not None
        and (
            atomic_id(observed) is None
            or current["generation"] != saved["generation"]
            or current["provider_job_id"] not in {None, observed}
            or current["provider_job_id"] is None
            and current["state"] not in _COLLECTIBLE
        )
        or current["generation"] == saved["generation"]
        and (
            (saved["state"] not in _UNSENT and current["state"] in _UNSENT)
            or (saved["state"] in _TERMINAL and current["state"] != saved["state"])
            or (saved["state"] == "unknown" and current["state"] == "submitting")
        )
    ):
        raise ProviderContractError("Original role model receipt binding or outcome changed")
    if observed is not None:
        current["observed_response_id"] = observed
    return current
