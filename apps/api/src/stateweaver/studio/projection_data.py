"""Bounded display projection of native tool result envelopes."""

from __future__ import annotations

import json
from typing import Any


def transcript_data(value: Any) -> Any:
    """Project only a native MCP result; keep creative arguments verbatim."""
    if not isinstance(value, dict) or value.get("type") != "mcpToolCall" or "result" not in value:
        return value
    arguments = value.get("arguments")
    media_id = (
        arguments.get("media_id")
        if value.get("tool") == "read_media" and isinstance(arguments, dict)
        else None
    )
    return {**value, "result": _tool_result(value["result"], media_id=media_id)}


def _tool_result(value: Any, *, media_id: str | None = None) -> Any:
    """Keep tool result evidence without embedding media or another project snapshot.

    App Server can wrap MCP content as JSON inside a text block, as well as
    forwarding structured content. This projection only affects our UI/audit
    copy; the actual MCP response and Codex native history retain their inputs.
    """
    if isinstance(value, dict):
        if "retry_ids" in value and "items" in value and isinstance(value["items"], list):
            return {
                "type": "batch_read_summary",
                "count": len(value["items"]),
                "partial": value.get("partial", False),
                "retry_ids": value["retry_ids"],
                "failures": [item for item in value["items"] if not item.get("ok")],
                "item_ids": [
                    item["result"]["item_id"]
                    for item in value["items"]
                    if item.get("ok")
                    and isinstance(item.get("result"), dict)
                    and isinstance(item["result"].get("item_id"), str)
                ],
            }
        if {"context_id", "page", "page_count", "fingerprint", "content"} <= value.keys():
            return {
                key: value[key]
                for key in (
                    "context_id",
                    "target_id",
                    "phase",
                    "page",
                    "page_count",
                    "next_page",
                    "mode",
                    "read_reason",
                    "payload_bytes",
                    "full_payload_bytes",
                    "baseline_context_id",
                )
                if key in value
            }
        if (
            "items" in value
            and "has_more" in value
            and isinstance(value["items"], list)
            and all(isinstance(item, dict) and "memory_scope" in item for item in value["items"])
        ):
            return {
                "type": "memory_page_summary",
                "count": len(value["items"]),
                "has_more": value["has_more"],
            }
        if value.get("type") in ("image", "audio"):
            return {
                **{
                    key: _tool_result(item, media_id=media_id)
                    for key, item in value.items()
                    if key != "data"
                },
                **({"media_id": media_id} if media_id else {}),
            }
        if {"id", "revision", "actors", "global_memory", "transcript", "clips"} <= value.keys():
            actor = value.get("you")
            return {
                "type": "project_summary",
                "project_id": value["id"],
                "revision": value["revision"],
                "status": value.get("status"),
                "actor": {
                    key: actor[key]
                    for key in ("id", "role", "set_id", "status", "summary")
                    if key in actor
                }
                if isinstance(actor, dict)
                else None,
                "counts": {
                    key: len(value[key])
                    for key in ("sets", "clips", "assets", "states", "references", "jobs")
                    if isinstance(value.get(key), dict)
                },
                "issues": value.get("issues", []),
                "final_media_id": value.get("final_media_id"),
            }
        return {key: _tool_result(item, media_id=media_id) for key, item in value.items()}
    if isinstance(value, list):
        return [_tool_result(item, media_id=media_id) for item in value]
    if isinstance(value, str):
        if value.startswith(("data:image/", "data:audio/")):
            return "[media content served by MCP]"
        if value.lstrip().startswith(("{", "[")):
            try:
                parsed = json.loads(value)
            except (ValueError, RecursionError):
                return value
            projected = _tool_result(parsed, media_id=media_id)
            if projected != parsed:
                return json.dumps(projected, ensure_ascii=False, separators=(",", ":"))
    return value


def _transcript_item(item: dict[str, Any]) -> dict[str, Any]:
    # Business cards may themselves contain literal JSON/tool examples. Only
    # the native tool record's data is eligible; never descend into card payloads.
    if item.get("kind") != "tool":
        return item
    return {**item, "data": transcript_data(item.get("data", {}))}


def _project_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        **payload,
        "transcript": {
            key: _transcript_item(item) for key, item in payload.get("transcript", {}).items()
        },
    }


def activity_data(text: str, status: str, data: dict[str, Any]) -> dict[str, Any]:
    """Small factual operation metadata; original arguments/results stay in the archive."""
    arguments = data.get("arguments")
    arguments = arguments if isinstance(arguments, dict) else {}
    parameters = arguments.get("payload")
    parameters = parameters if isinstance(parameters, dict) else arguments
    explicit = data.get("activity")
    explicit = explicit if isinstance(explicit, dict) else {}
    action = (
        explicit.get("action")
        or arguments.get("action")
        or data.get("action")
        or data.get("tool")
        or data.get("name")
        or text
    )
    result: dict[str, Any] = {"action": str(action)[:100], "status": status}
    if data.get("type") == "mcpToolCall":
        result["type"] = "mcpToolCall"
    result_data = data.get("result")
    receipt = (
        result_data.get("structuredContent", result_data) if isinstance(result_data, dict) else None
    )
    if (
        data.get("status") == "failed"
        or data.get("error")
        or isinstance(result_data, dict)
        and result_data.get("isError")
        or isinstance(receipt, dict)
        and (receipt.get("ok") is False or receipt.get("error") or receipt.get("partial"))
    ):
        result["status"] = "failed"
    for source in (data, parameters, explicit):
        for key in (
            "scope",
            "set_id",
            "clip_id",
            "candidate_id",
            "asset_id",
            "state_id",
            "reference_id",
            "reference_plan_id",
            "event_id",
            "revision_id",
            "parent_id",
            "before_reference_id",
            "media_id",
            "job_id",
            "proposal_id",
            "voice_reference_id",
            "source_candidate_id",
            "observation_id",
            "selection_kind",
            "view",
            "entry_id",
            "entry_kind",
            "item_id",
            "summary",
            "reason",
        ):
            value = source.get(key)
            if isinstance(value, str) and value:
                result[key] = value[:500] if key in {"summary", "reason"} else value[:256]
        if "before_reference_id" in source and source["before_reference_id"] is None:
            result["before_reference_id"] = None
        for key in ("defects", "preserve", "deviations"):
            values = source.get(key)
            if isinstance(values, list):
                result[key] = [item[:500] for item in values[:12] if isinstance(item, str)]
        entry = source.get("entry")
        if isinstance(entry, dict) and str(action).split("__")[-1] == "write_memory":
            if isinstance(entry.get("text"), str) and entry["text"]:
                result.setdefault("summary", entry["text"][:500])
            if isinstance(entry.get("kind"), str):
                result["entry_kind"] = entry["kind"][:100]
            revision = source.get("memory_revision")
            if type(revision) is int and revision >= 0:
                result["memory_revision_before"] = revision
                if result["status"] == "complete":
                    result["memory_revision_after"] = revision + 1
        if str(action).split("__")[-1] in {"create_asset", "create_state"}:
            description = source.get("description")
            if isinstance(description, str) and description:
                result.setdefault("summary", description[:500])
    if isinstance(result_data, dict):
        if isinstance(receipt, dict) and isinstance(receipt.get("error"), dict):
            message = receipt["error"].get("message")
            if isinstance(message, str):
                result["error"] = message[:500]
        if isinstance(receipt, dict) and receipt.get("partial"):
            result["status"] = "failed"
            result["error"] = "部分读取失败，请重试失败项；成功结果已保留"
        if isinstance(receipt, dict) and isinstance(receipt.get("item_id"), str):
            result["item_id"] = receipt["item_id"][:256]
        if isinstance(receipt, dict) and isinstance(receipt.get("item_ids"), list):
            result["item_ids"] = [
                item[:256] for item in receipt["item_ids"][:12] if isinstance(item, str)
            ]
    return result
