"""Immutable context pages and scoped read dependencies, independent of transcript history."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from typing import Any, cast

from stateweaver.studio.contracts import Actor, ContextRead, StudioProject
from stateweaver.studio.memory import digest, require
from stateweaver.studio.repository import entity_payload
from stateweaver.studio.store import StudioStore


def session_key(actor: Actor, project: StudioProject | None = None) -> str:

    if project is not None and actor.role == "set_director":
        session = actor.sessions.get(actor.session_key)
        return digest(
            [actor.id, actor.session_key, actor.thread_id, session.context_epoch if session else 0]
        )
    return digest([actor.id, actor.thread_id, actor.context_epoch])


def wake_fingerprint(actor: Actor) -> str:
    # Include the full durable goal so an operator instruction cannot be
    # acknowledged by an older read, including legacy maintenance writers.
    # The boolean also detects an older writer's false-to-true transition;
    # the version distinguishes further notifications while it is already true.
    return digest([actor.wake_version, actor.wake_requested, actor.goal])


def overview_wake_observed(p: StudioProject, actor: Actor) -> bool:
    """Retain a completed overview read when a wait uses another scoped token.

    This acknowledges only the current notification, not creative dependencies.
    Provider polling can change overview row versions without creating a wake;
    actor, session, core and wake fingerprints remain mandatory fences.
    """
    receipt = p.context_reads.get(digest(["tool-read-v2", actor.id, "overview"])[:32])
    return bool(
        receipt
        and receipt.actor_id == actor.id
        and receipt.target_id == "overview"
        and receipt.read_kind == "tool"
        and receipt.complete
        and receipt.protocol_version == 2
        and receipt.session_key == session_key(actor, p)
        and receipt.core_fingerprint == core_fingerprint(p)
        and receipt.wake_fingerprint == wake_fingerprint(actor)
    )


def core_fingerprint(p: StudioProject) -> str:
    return digest(
        [
            "review-remediation-v1",
            p.workflow_version,
            p.harness_version,
            p.production_efficiency_policy,
            p.handoff_policy,
            p.playback_policy,
            *([p.set_director_policy]),
            p.title,
            p.brief,
            p.screenplay.model_dump(mode="json"),
            p.catalog_frozen_at,
            p.config.model_dump(),
        ]
    )


# Polling and ownership are runtime data, not new creative evidence.
JOB_RUNTIME_FIELDS = {
    "lease_owner",
    "lease_until",
    "lease_generation",
    "next_poll_at",
    "poll_attempts",
    "query_error_count",
    "query_outage_since",
    "last_query_success_at",
    "query_recoveries",
}


def context_entity_payload(kind: str, payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if payload is None or kind != "jobs":
        return payload
    result = {key: value for key, value in payload.items() if key not in JOB_RUNTIME_FIELDS}
    if "result" in result:
        result["result"] = {
            key: value for key, value in result["result"].items() if key != "timings"
        }
    return result


def context_changes(
    p: StudioProject,
) -> tuple[bool, list[tuple[str, str, Any, Any, str | None]]]:
    """Compute business changes once per command, using only already touched entities."""
    cached = p.__dict__.get("_context_changes")
    if cached is not None:
        return cast(tuple[bool, list[tuple[str, str, Any, Any, str | None]]], cached)
    maps = p.__dict__.get("_repositories")
    if not maps:
        return True, []
    core_changed = p.__dict__.get("_context_core_original") != [
        p.screenplay.model_dump(mode="json"),
        p.catalog_frozen_at,
        p.config.model_dump(),
        p.handoff_policy,
    ]
    changes = []
    for kind, entities in maps.items():
        if kind in {"actors", "transcript", "context_reads", "reference_plans"}:
            continue
        for identifier, value in dict.items(entities):
            before = entities.original.get(identifier)
            after = entity_payload(kind, value)
            if context_entity_payload(kind, before) != context_entity_payload(kind, after):
                changes.append((kind, identifier, value, before, entities.parents.get(identifier)))
        for identifier in entities.deleted:
            changes.append(
                (
                    kind,
                    identifier,
                    None,
                    entities.original.get(identifier),
                    entities.parents.get(identifier),
                )
            )
    return core_changed, changes


def track_reads(p: StudioProject) -> None:
    object.__setattr__(p, "_track_reads", True)
    object.__setattr__(p, "_read_queries", {})
    object.__setattr__(p, "_tracked_read_ids", set())


def dependencies(p: StudioProject) -> list[dict[str, Any]]:
    # Repository access hooks include cached IDs actually read during this operation.
    # Unrelated rows loaded before track_reads are not part of every later baseline.
    return list(p.__dict__.get("_read_queries", {}).values())


def dependencies_current(
    p: StudioProject, items: list[dict[str, Any]], core: str, *, current_core: str | None = None
) -> bool:
    if core != (current_core if current_core is not None else core_fingerprint(p)):
        return False
    query = p.__dict__.get("_query_versions")
    if query is None:
        return False
    return all(query(item["kind"], item["filters"]) == item["signature"] for item in items)


def context_read_current(
    p: StudioProject, receipt: ContextRead, *, semantic_fallback: bool = False
) -> bool:
    """Use one freshness rule for immutable pages and the commands they authorize.

    Repository versions can change when a provider job is polled or its lease
    renewed. Mandatory context has an exact, scoped content fingerprint; compare
    it before discarding a read whose business context is unchanged. Core,
    session and complete-read checks remain separate mandatory fences.
    """
    current_core = core_fingerprint(p)
    if receipt.core_fingerprint != current_core:
        return False
    if dependencies_current(
        p, receipt.dependencies, receipt.core_fingerprint, current_core=current_core
    ):
        return True
    if not semantic_fallback or receipt.read_kind != "mandatory":
        return False
    from stateweaver.studio.task_context import context_fingerprint

    return receipt.fingerprint == context_fingerprint(
        p, receipt.target_id, receipt.phase, receipt.actor_id
    )


@lru_cache(maxsize=16)
def _load_snapshot(path: str) -> dict[str, Any]:
    value = json.loads(Path(path).read_text())
    if not isinstance(value, dict):
        raise ValueError("Invalid context snapshot")
    return value


def load_snapshot(store: StudioStore, identifier: str) -> dict[str, Any]:
    require(
        len(identifier) == 64 and all(c in "0123456789abcdef" for c in identifier),
        "上下文快照标识无效",
    )
    return _load_snapshot(str(store.body_root / "context-snapshots" / identifier))


def save_snapshot(store: StudioStore, value: dict[str, Any]) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    identifier = hashlib.sha256(raw).hexdigest()
    directory = store.body_root / "context-snapshots"
    directory.mkdir(exist_ok=True)
    destination = directory / identifier
    if not destination.exists():
        fd, name = tempfile.mkstemp(dir=directory)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
            os.replace(name, destination)
        finally:
            if os.path.exists(name):
                os.unlink(name)
    return identifier


def _read_receipt(p: StudioProject, actor: Actor, token: str) -> ContextRead:
    identifier, _, version = token.partition(":")
    receipt = p.context_reads.get(identifier)
    require(
        receipt
        and receipt.actor_id == actor.id
        and receipt.complete
        and receipt.fingerprint == version,
        "读取凭据无效，请重新读取相关上下文",
    )
    assert receipt is not None
    require(
        receipt.protocol_version == 2 and receipt.session_key == session_key(actor, p),
        "会话或上下文基线已变化，请重新读取相关范围以取得当前凭据",
    )
    return receipt


def validate_job_wait_token(p: StudioProject, actor: Actor, token: str, job_id: str) -> ContextRead:
    """Validate authority for a live job wait, not a stale creative decision.

    The command reconciles this actor's existing job inside the same transaction.
    Its polling/lease updates must not force another aggregate read just to wait.
    All other commands, including a general wait, retain dependency validation.
    """
    receipt = _read_receipt(p, actor, token)
    require(
        receipt.core_fingerprint == core_fingerprint(p),
        "项目制作基线已变化，请重新读取后再登记等待",
    )
    job = p.jobs.get(job_id)
    require(job and job.actor_id == actor.id, "等待凭据只能核对本人已有的任务")
    require(not actor.native_session_blocked, "模型会话技术恢复已耗尽，请通过恢复入口继续")
    return receipt


def validate_read_token(p: StudioProject, actor: Actor, token: str) -> ContextRead:
    receipt = _read_receipt(p, actor, token)
    require(
        context_read_current(p, receipt, semantic_fallback=True),
        "相关记录或依赖集合已更新，请重新读取并判断，不可盲目重试",
    )
    return receipt


def guarded_read(
    store: StudioStore,
    project_id: str,
    actor_id: str,
    scope: str,
    operation: Callable[[StudioProject], dict[str, Any]],
) -> dict[str, Any]:
    def build(p: StudioProject) -> tuple[dict[str, Any], ContextRead]:
        track_reads(p)
        value = operation(p)
        deps, core = dependencies(p), core_fingerprint(p)
        observed_wake = wake_fingerprint(p.actors[actor_id]) if scope == "overview" else None
        receipt = ContextRead(
            id=digest(["tool-read-v2", actor_id, scope])[:32],
            actor_id=actor_id,
            target_id=scope,
            phase="planning",
            read_kind="tool",
            complete=True,
            protocol_version=2,
            session_key=session_key(p.actors[actor_id], p),
            fingerprint=digest([deps, core, observed_wake])
            if observed_wake is not None
            else digest([deps, core]),
            dependencies=deps,
            core_fingerprint=core,
            wake_fingerprint=observed_wake,
        )
        return value, receipt

    value, receipt = store.read(project_id, build)

    def commit(p: StudioProject) -> None:
        require(
            session_key(p.actors[actor_id], p) == receipt.session_key
            and dependencies_current(p, receipt.dependencies, receipt.core_fingerprint)
            and (
                receipt.wake_fingerprint is None
                or receipt.wake_fingerprint == wake_fingerprint(p.actors[actor_id])
            ),
            "相关记录在读取时发生变化，请重新读取",
        )
        p.context_reads[receipt.id] = receipt

    store.mutate(project_id, commit, advance_revision=False, actor_id=actor_id, kind="context.read")
    return {**value, "read_token": f"{receipt.id}:{receipt.fingerprint}"}
