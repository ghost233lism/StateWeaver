"""Crash-safe, metadata-only handoff for native response IDs.

This sidecar contains no request body, output text, reasoning or credential.
It is published before the shared journal and replayed only against its exact
original receipt fence. Recovery never sends a provider request or wakes an actor.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any

from stateweaver.providers.audit import atomic_id
from stateweaver.providers.base import ProviderContractError
from stateweaver.providers.coordination import ProviderCoordinator, _validate_result

logger = logging.getLogger(__name__)
IDENTITY = ("id", "owner", "generation", "request_hash", "config_hash", "model", "context")
FIELDS = {"receipt", "state", "provider_job_id", "result"}


def durable_outcome(state: str, identifier: Any, result: dict[str, Any]) -> bool:
    if atomic_id(identifier):
        return True
    error = result.get("error")
    # An explicit unaccepted HTTP failure needs no response ID. Uncertain
    # submissions without an ID remain unknown and never authorize a POST.
    return (
        identifier is None
        and state == "failed"
        and isinstance(error, dict)
        and error.get("phase") == "submission"
        and error.get("definitely_not_accepted") is True
        and not error.get("known_job")
    )


class NativeReceipts:
    def __init__(self, coordinator: ProviderCoordinator, *, root: Path | None = None) -> None:
        self.coordinator = coordinator
        self.root = root or coordinator.path.parent / (coordinator.path.name + ".native-receipts")

    def path(self, receipt: dict[str, Any]) -> Path:
        if (
            not isinstance(receipt.get("id"), str)
            or not re.fullmatch(r"[a-f0-9]{32}", receipt["id"])
            or not isinstance(receipt.get("generation"), int)
            or isinstance(receipt["generation"], bool)
            or receipt["generation"] < 1
        ):
            raise ProviderContractError("Native metadata receipt identity is invalid")
        return self.root / f"{receipt['id']}-{receipt['generation']}.json"

    def _validate_sidecar(self, path: Path, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict) or set(payload) != FIELDS:
            raise ProviderContractError("Native metadata sidecar schema is invalid")
        receipt = payload["receipt"]
        if not isinstance(receipt, dict) or set(receipt) != set(IDENTITY):
            raise ProviderContractError("Native metadata sidecar identity is invalid")
        if self.path(receipt) != path:
            raise ProviderContractError("Native metadata sidecar filename is invalid")
        context = receipt["context"]
        if not isinstance(context, dict) or context.get("operation") != "native_responses":
            raise ProviderContractError("Native metadata sidecar operation is invalid")
        _validate_result(payload["result"])
        if not durable_outcome(payload["state"], payload["provider_job_id"], payload["result"]):
            raise ProviderContractError("Native metadata sidecar outcome is invalid")
        return receipt

    def pending_completion(
        self,
        provider_id: str,
        *,
        scope: dict[str, Any],
        service_id: str,
        model: str,
    ) -> dict[str, Any] | None:
        """Find a unique, fenced completion awaiting its shared response ID."""
        pending = None
        for path in self.root.glob("*.json"):
            try:
                payload = json.loads(path.read_text())
                if (
                    not isinstance(payload, dict)
                    or payload.get("state") != "complete"
                    or payload.get("provider_job_id") != provider_id
                ):
                    continue
                receipt = self._validate_sidecar(path, payload)
                context = receipt["context"]
                if receipt["model"] != model or any(
                    context.get(key) != value for key, value in scope.items()
                ):
                    continue
                current = self.coordinator.get(receipt["id"])
            except (FileNotFoundError, ProviderContractError, KeyError, TypeError, ValueError):
                continue
            if (
                any(current.get(key) != receipt[key] for key in IDENTITY)
                or current["service_id"] != service_id
                or current["provider_job_id"] is not None
                or current["state"] not in {"submitting", "running", "collecting", "unknown"}
            ):
                continue
            if pending is not None:
                return None
            pending = current
        return pending

    def publish(
        self,
        receipt: dict[str, Any],
        state: str,
        *,
        provider_job_id: str | None = None,
        result: dict[str, Any] | None = None,
    ) -> None:
        data = {key: value for key, value in (result or {}).items() if value is not None}
        _validate_result(data)
        identifier = provider_job_id or receipt.get("provider_job_id")
        target = None
        preserve_pending = False
        if durable_outcome(state, identifier, data):
            target = self.path(receipt)
            self.root.mkdir(parents=True, exist_ok=True)
            payload = {
                "receipt": {key: receipt.get(key) for key in IDENTITY},
                "state": state,
                "provider_job_id": identifier,
                "result": data,
            }
            if target.exists():
                old = json.loads(target.read_text())
                preserve_pending = (
                    old.get("receipt") == payload["receipt"]
                    and old.get("provider_job_id") == identifier
                    and old.get("state") in {"complete", "failed", "cancelled"}
                    and state not in {"complete", "failed", "cancelled"}
                )
            descriptor, name = tempfile.mkstemp(prefix=".pending-", dir=self.root)
            try:
                with os.fdopen(descriptor, "w") as stream:
                    json.dump(payload, stream, ensure_ascii=False, allow_nan=False)
                    stream.flush()
                    os.fsync(stream.fileno())
                if not preserve_pending:
                    os.replace(name, target)
                directory = os.open(self.root, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            finally:
                Path(name).unlink(missing_ok=True)
        try:
            self.coordinator.update(
                receipt["id"],
                state,
                provider_job_id=provider_job_id,
                result=data,
                owner=receipt["owner"],
                generation=receipt["generation"],
                writer_timeout_seconds=0.25,
            )
        except sqlite3.OperationalError as error:
            code = getattr(error, "sqlite_errorcode", None)
            if not (
                target
                and target.exists()
                and isinstance(code, int)
                and code & 0xFF in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
            ):
                raise
            # The response ID is already durable. Deliver the returned stream and
            # let recover() finish only the journal write, without another POST.
            receipt.update(state=state, result=data, updated=time.time())
            if provider_job_id:
                receipt["provider_job_id"] = provider_job_id
            logger.warning("Native response journal deferred for %s", receipt["id"])
            return
        receipt.update(state=state, result=data, updated=time.time())
        if provider_job_id:
            receipt["provider_job_id"] = provider_job_id
        if target and not preserve_pending:
            target.unlink(missing_ok=True)

    def recover(self, *, project_id: str | None = None, actor_id: str | None = None) -> int:
        """Replay durable sidecars once, quarantining stale metadata.

        A sidecar is only a journal handoff; it is not a reason to stop the
        native actor forever.  Older releases left malformed or cross-fence
        sidecars in place, so every subsequent request logged the same error
        and appeared blocked even though there was no replayable receipt.
        Keep the fail-closed checks for a *valid* replay, but move an invalid
        sidecar out of the hot path after the first observation.  This does
        not submit a provider request and does not change an unknown receipt.
        The quarantine file is retained for diagnostics and can be restored
        manually if its provenance is repaired.
        """
        recovered = 0
        scoped = project_id is not None or actor_id is not None
        for path in sorted(self.root.glob("*.json")):
            try:
                parse_error: ValueError | None = None
                try:
                    payload = json.loads(path.read_text())
                except FileNotFoundError:
                    # Another recovery/publisher already removed this exact handoff.
                    continue
                except ValueError as error:
                    if not scoped:
                        raise
                    payload, parse_error = None, error
                current: dict[str, Any] | None = None
                if scoped:
                    claimed = payload.get("receipt") if isinstance(payload, dict) else None
                    context = claimed.get("context") if isinstance(claimed, dict) else None
                    if isinstance(context, dict):
                        if (
                            project_id is not None
                            and isinstance(context.get("project_id"), str)
                            and context["project_id"] != project_id
                        ):
                            continue
                        if (
                            actor_id is not None
                            and isinstance(context.get("actor_id"), str)
                            and context["actor_id"] != actor_id
                        ):
                            continue
                    # Authenticate ownership before even quarantining malformed
                    # metadata. A claimed context cannot own another actor's file.
                    filename = re.fullmatch(r"([a-f0-9]{32})-[1-9][0-9]*\.json", path.name)
                    claimed_id = claimed.get("id") if isinstance(claimed, dict) else None
                    identifier = filename[1] if filename else claimed_id
                    if not isinstance(identifier, str) or not re.fullmatch(
                        r"[a-f0-9]{32}", identifier
                    ):
                        logger.warning("Native metadata scope unavailable: %s", path.name)
                        continue
                    try:
                        current = self.coordinator.get(identifier)
                    except ProviderContractError as error:
                        logger.warning(
                            "Native metadata original scope unavailable (%s): %s",
                            type(error).__name__,
                            path.name,
                        )
                        continue
                    current_context = current.get("context")
                    if (
                        not isinstance(current_context, dict)
                        or project_id is not None
                        and current_context.get("project_id") != project_id
                        or actor_id is not None
                        and current_context.get("actor_id") != actor_id
                    ):
                        logger.warning("Native metadata original scope differs: %s", path.name)
                        continue
                    if parse_error is not None:
                        raise parse_error
                receipt = self._validate_sidecar(path, payload)
                context = receipt["context"]
                if project_id is not None and context.get("project_id") != project_id:
                    continue
                if actor_id is not None and context.get("actor_id") != actor_id:
                    continue
                if current is None:
                    current = self.coordinator.get(receipt["id"])
                if any(current.get(key) != receipt[key] for key in IDENTITY):
                    raise ProviderContractError("Native metadata sidecar fence no longer matches")
                if current["state"] in {"complete", "failed", "cancelled"}:
                    if current["provider_job_id"] != payload["provider_job_id"] or (
                        payload["provider_job_id"] is None and current["state"] != payload["state"]
                    ):
                        raise ProviderContractError("Native metadata terminal ID conflicts")
                    path.unlink(missing_ok=True)
                    continue
                self.coordinator.update(
                    receipt["id"],
                    payload["state"],
                    provider_job_id=payload["provider_job_id"],
                    result=payload["result"],
                    owner=receipt["owner"],
                    generation=receipt["generation"],
                    writer_timeout_seconds=0.25,
                )
                path.unlink(missing_ok=True)
                recovered += 1
            except Exception as error:
                # Keep retrying I/O/ledger outages; only quarantine a sidecar
                # when its own metadata is demonstrably malformed or stale.
                # This prevents a transient SQLite/filesystem fault from
                # discarding a receipt that may still be needed for recovery.
                quarantineable = isinstance(
                    error,
                    (
                        ProviderContractError,
                        KeyError,
                        TypeError,
                        ValueError,
                    ),
                )
                if not quarantineable:
                    logger.error("Native metadata recovery deferred (%s)", type(error).__name__)
                    code = getattr(error, "sqlite_errorcode", None)
                    if (
                        isinstance(error, sqlite3.OperationalError)
                        and isinstance(code, int)
                        and code & 0xFF in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
                    ):
                        break  # One short wait per pass; retain every pending sidecar.
                    continue
                # Do not retry the same invalid sidecar on every native
                # request.  Keep the evidence, but remove it from the replay
                # glob so a stale fence cannot create a permanent hot-path
                # blocker.  A collision is handled by a timestamped suffix.
                quarantine = path.with_suffix(path.suffix + ".quarantine")
                if quarantine.exists():
                    quarantine = path.with_name(
                        f"{path.stem}.{int(time.time() * 1000)}{path.suffix}.quarantine"
                    )
                # Only a completed move establishes that metadata was quarantined.
                try:
                    path.replace(quarantine)
                except FileNotFoundError:
                    # A concurrent recovery already collected this exact handoff.
                    continue
                except OSError as quarantine_error:
                    logger.error(
                        "Native metadata quarantine deferred (%s): %s",
                        type(quarantine_error).__name__,
                        path.name,
                    )
                    continue
                logger.warning(
                    "Native metadata sidecar quarantined (%s): %s",
                    type(error).__name__,
                    quarantine.name,
                )
        return recovered
