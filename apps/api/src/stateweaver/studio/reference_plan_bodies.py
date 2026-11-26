"""Bounded, current-version JSON reads for exactly authorized reference plans."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from stateweaver.persistence.models import StudioEntityRow

from .contracts import BodyChunk, StudioProject
from .memory import require
from .repository import EntityMap
from .review_sessions import guard_read
from .store import StudioError
from .visibility import Visibility

BODY_PREFIX = "reference-plan:"
MAX_CHUNK_CHARACTERS = 65536


def _text(payload: dict[str, Any]) -> str:
    # Serialize the original stored JSON fields, never a compact projection or
    # a revalidated model that could add defaults or remove original fields.
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def body_metadata(
    project_id: str, identifier: str, payload: dict[str, Any], version: int
) -> dict[str, Any]:
    require(payload.get("id") == identifier and version >= 1, "参考计划正文身份无效")
    text = _text(payload)
    checksum = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return {
        "body_ref": f"{BODY_PREFIX}{project_id}:{identifier}:{version}:{checksum}",
        "body_scope": f"reference-plan:{identifier}",
        "body_version": version,
        "body_sha256": checksum,
        "body_total_chars": len(text),
        "body_content_type": "application/json",
    }


def _authorized_row(project: StudioProject, actor_id: str, identifier: str) -> StudioEntityRow:
    actor = project.actors.get(actor_id)
    require(actor is not None, "参考计划正文角色不存在")
    assert actor is not None
    # Body reads grant exactly the authority of an ordinary detail read, including
    # its current review-session fences. They never satisfy mandatory-page receipts.
    scope = f"reference-plan:{identifier}"
    guard_read(project, actor, "get_detail", {"scope": scope})
    Visibility(project, actor_id).scope(scope)
    plans = project.reference_plans
    require(plans.get(identifier) is not None, "参考计划不存在")
    if not isinstance(plans, EntityMap):
        raise StudioError("参考计划正文需要持久化版本", 409)
    row = plans.rows.get(identifier)
    require(
        isinstance(row, StudioEntityRow)
        and row.project_id == project.id
        and row.kind == "reference_plans"
        and row.id == identifier,
        "参考计划正文身份无效",
    )
    assert isinstance(row, StudioEntityRow)
    return row


def detail_metadata(
    project: StudioProject, actor_id: str, identifier: str, expected_version: int
) -> dict[str, Any]:
    row = _authorized_row(project, actor_id, identifier)
    require(row.version == expected_version, "参考计划版本已变化，请重新读取详情")
    return body_metadata(project.id, identifier, dict(row.payload), row.version)


def read_body(
    project: StudioProject,
    actor_id: str,
    body_ref: str,
    offset: int = 0,
    limit: int = MAX_CHUNK_CHARACTERS,
) -> dict[str, Any]:
    parts = body_ref.split(":")
    if (
        len(body_ref) > 1024
        or len(parts) != 5
        or parts[0] != "reference-plan"
        or not parts[1]
        or not parts[2]
        or len(parts[2]) > 256
        or re.fullmatch(r"[1-9][0-9]*", parts[3]) is None
        or re.fullmatch(r"[0-9a-f]{64}", parts[4]) is None
        or offset < 0
    ):
        raise StudioError("参考计划正文引用或偏移无效", 422)
    _, project_id, identifier, version, _ = parts
    require(project_id == project.id, "参考计划正文不属于当前项目")
    row = _authorized_row(project, actor_id, identifier)
    metadata = body_metadata(project.id, identifier, dict(row.payload), row.version)
    require(
        row.version == int(version) and metadata["body_ref"] == body_ref,
        "参考计划版本或正文已变化，请重新读取详情",
    )
    text = _text(dict(row.payload))
    limit = min(max(1, limit), MAX_CHUNK_CHARACTERS)
    chunk = text[offset : offset + limit]
    end = min(len(text), offset + len(chunk))
    return BodyChunk(
        text=chunk,
        offset=offset,
        next_offset=end if end < len(text) else None,
        total=len(text),
        body_ref=body_ref,
    ).model_dump(mode="json")
