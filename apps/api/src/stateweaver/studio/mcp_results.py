"""Small structured results and recovery instructions without echoing private arguments."""

import json
from typing import Any

from pydantic import ValidationError

from stateweaver.studio.store import (
    StudioContextRestartRequired,
    StudioError,
    StudioPrerequisiteError,
    StudioTransactionRolledBack,
)


def result(value: dict[str, Any], *, content: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "isError": False,
        "structuredContent": value,
        "content": content or [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}],
    }


def native_result(value: dict[str, Any]) -> dict[str, Any]:
    """Keep images in the native model input instead of a metadata-only output."""
    content = value.get("content")
    if not isinstance(content, list) or not any(
        isinstance(item, dict) and item.get("type") == "image" for item in content
    ):
        return value
    # Include metadata with the image content in the model input.
    content = [dict(item) for item in content]
    metadata = value.get("structuredContent")
    if metadata is not None:
        text = json.dumps(metadata, ensure_ascii=False)
        if not any(item.get("type") == "text" and item.get("text") == text for item in content):
            content.append({"type": "text", "text": text})
    return {
        key: item
        for key, item in {**value, "content": content}.items()
        if key != "structuredContent"
    }


def error_result(
    exc: Exception, *, write: bool = False, tool_name: str | None = None
) -> dict[str, Any]:
    if isinstance(exc, ValidationError):
        message = "; ".join(
            ".".join(map(str, item["loc"])) + ": " + item["msg"]
            for item in exc.errors(include_input=False, include_url=False)
        )
        code, recovery = "invalid_arguments", "按工具参数修正请求"
    elif isinstance(exc, StudioTransactionRolledBack) and write:
        # Commands only create local durable jobs; provider submission happens
        # after the atomic transaction commits. An integrity failure rolls it back.
        message = "本地事务已回滚，未创建新的生成任务"
        code = "operation_not_committed"
        recovery = "修复本地数据错误后，核对当前上下文并用原幂等键重试；不要直接重发外部生成请求"
    elif isinstance(exc, StudioContextRestartRequired):
        message = str(exc)
        code = "context_conflict"
        recovery = (
            "丢弃本轮已拼接分页、旧 fingerprint 和 snapshot_id；按 restart_request 重读，"
            "逐页使用新返回的 next_request。不要重发旧分页参数；"
            "连续两次重启仍冲突时停止本轮批量读取并返回导演主循环。"
        )
    elif isinstance(exc, StudioPrerequisiteError):
        message = str(exc)
        code, recovery = "domain_conflict", exc.recovery
    elif isinstance(exc, StudioError):
        message = str(exc)
        if any(
            word in message for word in ("版本", "已更新", "基线", "相关记录", "上下文", "读取凭据")
        ):
            code, recovery = (
                "context_conflict",
                (
                    "重读相关内容并重新判断；新／不完整基线自动全读，"
                    "全文核验须 mode=full 与具体 full_verification_reason"
                ),
            )
        elif "租约" in message:
            code, recovery = (
                "lease_expired",
                "等待后台沿有效会话恢复，勿重放旧租约写操作",
            )
        elif exc.status == 404:
            code, recovery = "not_found", "核对同项目的精确实体或素材 ID"
        else:
            code, recovery = "domain_conflict", "按具体缺项处理；创作决策交给负责导演"
    else:
        message = "工具暂时无法完成：" + type(exc).__name__
        code = "operation_uncertain" if write else "read_unavailable"
        recovery = (
            "用原 idempotency_key 调用 get_operation_receipt，核对任务后再决定"
            if write
            else "只重试失败读取项"
        )
    error: dict[str, Any] = {
        "code": code,
        "message": message,
        "recovery": recovery,
        "retryable": code == "read_unavailable",
    }
    if isinstance(exc, StudioTransactionRolledBack) and exc.diagnostic_id:
        error["diagnostic_id"] = exc.diagnostic_id
        error["root_cause"] = "unidentified"
    if isinstance(exc, StudioPrerequisiteError):
        error["details"] = exc.details
    if isinstance(exc, StudioContextRestartRequired):
        error["discard_partial_context"] = True
        error["restart_request"] = {
            "phase": exc.phase,
            "clip_id" if tool_name == "get_clip_work_packet" else "target_id": exc.target_id,
            "page": 0,
            "mode": "full",
        }
        if tool_name == "get_review_work_packet":
            error["restart_request"].pop("phase", None)
            error["restart_request"]["candidate_id"] = error["restart_request"].pop("target_id")
    value = {"ok": False, "error": error}
    return {**result(value), "isError": True}
