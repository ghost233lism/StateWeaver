"""Bind staged MCP pixels to successful native input without storing model bodies."""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
from dataclasses import dataclass
from typing import Any

from PIL import Image, UnidentifiedImageError

from .contracts import Actor, StudioProject


@dataclass(frozen=True)
class NativeImage:
    delivery_id: str
    item_id: str
    media_id: str
    source_sha256: str
    view_sha256: str
    payload_sha256: str
    width: int
    height: int


@dataclass(frozen=True)
class NativeImageInput:
    project_id: str
    actor_id: str
    lease_owner: str | None
    lease_generation: int
    thread_id: str | None
    session_key: str
    actor_context_epoch: int
    session_context_epoch: int
    images: tuple[NativeImage, ...]


def _stage_records(value: Any, depth: int = 0) -> list[dict[str, Any]]:
    if depth > 6:
        return []
    if isinstance(value, dict):
        if {"item_id", "media_id", "source_sha256", "view_sha256"} <= value.keys():
            return [value]
        return [r for v in value.values() for r in _stage_records(v, depth + 1)]
    if isinstance(value, list):
        return [r for v in value for r in _stage_records(v, depth + 1)]
    if isinstance(value, str) and value.lstrip().startswith(("{", "[")):
        try:
            return _stage_records(json.loads(value), depth + 1)
        except (ValueError, RecursionError):
            pass
    return []


def _native_image(part: dict[str, Any]) -> tuple[str, int, int] | None:
    url = part.get("image_url")
    if not isinstance(url, str) or not url.startswith("data:image/"):
        return None
    header, separator, encoded = url.partition(",")
    if not separator or not header.endswith(";base64"):
        return None
    try:
        raw = base64.b64decode(encoded, validate=True)
        with Image.open(io.BytesIO(raw)) as image:
            image.load()
            width, height = image.size
        return hashlib.sha256(raw).hexdigest(), width, height
    except (
        binascii.Error,
        ValueError,
        OSError,
        UnidentifiedImageError,
        Image.DecompressionBombError,
    ):
        return None


def match_image_input(
    project: StudioProject, actor: Actor, payload: dict[str, Any]
) -> NativeImageInput | None:
    """Only native tool outputs with original stage metadata can prove input supply.

    Native App Server resizes/reencodes MCP images. Its actual payload hash is
    independent of the server view hash; the trusted output's exact stage identity
    binds them. User messages, metadata alone and ambiguous multi-image outputs
    do not certify anything.
    """
    session = actor.sessions.get(actor.session_key)
    if not actor.pending_image_deliveries or not session:
        return None
    history = payload.get("input")
    if not isinstance(history, list):
        return None
    stages = {}
    for delivery in actor.pending_image_deliveries:
        media = project.media.get(delivery.media_id)
        item = project.transcript.get(delivery.item_id)
        if (
            delivery.session_key != actor.session_key
            or delivery.actor_context_epoch != actor.context_epoch
            or delivery.session_context_epoch != session.context_epoch
            or delivery.thread_id != actor.thread_id
            or not media
            or not item
            or item.actor_id != actor.id
            or item.data.get("media_id") != media.id
            or item.data.get("source_sha256") != media.sha256
            or not item.data.get("view_sha256")
        ):
            continue
        stages[item.id] = (delivery, item.data)
    matched: dict[str, NativeImage] = {}
    for output in history:
        if (
            not isinstance(output, dict)
            or output.get("type") != "function_call_output"
            or not isinstance(output.get("call_id"), str)
        ):
            continue
        parts = output.get("output")
        if not isinstance(parts, list):
            continue
        images = [p for p in parts if isinstance(p, dict) and p.get("type") == "input_image"]
        if not images:
            continue  # Do not parse unrelated text-only tool history on this hot path.
        records = {
            (r["item_id"], r["media_id"], r["source_sha256"], r["view_sha256"]): r
            for part in parts
            if isinstance(part, dict) and part.get("type") == "input_text"
            for r in _stage_records(part.get("text"))
            if all(
                isinstance(r[k], str)
                for k in ("item_id", "media_id", "source_sha256", "view_sha256")
            )
        }
        if not records:
            continue
        # Native batch output has explicit media_id markers before each image.
        # Without them, only one image and one exact stage is unambiguous.
        anchor = None
        candidates: list[tuple[dict[str, Any], tuple[str, int, int]]] = []
        ambiguous = False
        for part in parts:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "input_text" and str(part.get("text", "")).startswith(
                "media_id="
            ):
                anchor = str(part["text"]).removeprefix("media_id=").strip()
            if part.get("type") != "input_image":
                continue
            applicable = [r for r in records.values() if not anchor or r["media_id"] == anchor]
            if len(applicable) != 1 or (len(images) > 1 and not anchor):
                ambiguous = True
                break
            record = applicable[0]
            stage = stages.get(record["item_id"])
            if not stage or any(
                record[k] != stage[1].get(k) for k in ("media_id", "source_sha256", "view_sha256")
            ):
                ambiguous = True
                break
            # Decode only the exact still-pending stage, never arbitrary image
            # history or unknown metadata carried in an unrelated output.
            image = _native_image(part)
            if not image:
                ambiguous = True
                break
            candidates.append((record, image))
            anchor = None
        if ambiguous or len({r["item_id"] for r, _ in candidates}) != len(candidates):
            continue
        for record, (image_hash, width, height) in candidates:
            stage = stages.get(record["item_id"])
            if not stage:
                continue
            delivery, data = stage
            if any(record[k] != data.get(k) for k in ("media_id", "source_sha256", "view_sha256")):
                continue
            matched[delivery.id] = NativeImage(
                delivery.id,
                delivery.item_id,
                delivery.media_id,
                record["source_sha256"],
                record["view_sha256"],
                image_hash,
                width,
                height,
            )
    if not matched:
        return None
    return NativeImageInput(
        project.id,
        actor.id,
        actor.lease_owner,
        actor.lease_generation,
        actor.thread_id,
        actor.session_key,
        actor.context_epoch,
        session.context_epoch,
        tuple(matched.values()),
    )


def confirm_image_input(
    project: StudioProject, binding: NativeImageInput, receipt_id: str, response_id: str
) -> None:
    """Called only after the matching native response genuinely completed successfully."""
    from .image_input_recovery import confirm_delivery, quarantine
    from .review_sessions import reserve_image

    actor = project.actors.get(binding.actor_id)
    if not actor or project.id != binding.project_id or not receipt_id or not response_id:
        return
    session = actor.sessions.get(actor.session_key)
    parent = project.actors.get(actor.parent_actor_id or "")
    if (
        not session
        or actor.paused
        or parent
        and parent.paused
        or actor.lease_owner != binding.lease_owner
        or actor.lease_generation != binding.lease_generation
        or actor.thread_id != binding.thread_id
        or actor.session_key != binding.session_key
        or actor.context_epoch != binding.actor_context_epoch
        or session.context_epoch != binding.session_context_epoch
    ):
        return
    matched = {image.delivery_id: image for image in binding.images}
    remaining = []
    for delivery in actor.pending_image_deliveries:
        image = matched.get(delivery.id)
        item = project.transcript.get(delivery.item_id)
        media = project.media.get(delivery.media_id)
        if (
            not image
            or delivery.item_id != image.item_id
            or delivery.media_id != image.media_id
            or delivery.session_key != binding.session_key
            or delivery.actor_context_epoch != binding.actor_context_epoch
            or delivery.session_context_epoch != binding.session_context_epoch
            or delivery.thread_id != binding.thread_id
            or not item
            or not media
            or media.sha256 != image.source_sha256
            or quarantine(project, media.id)
            or item.data.get("source_sha256") != image.source_sha256
            or item.data.get("view_sha256") != image.view_sha256
        ):
            remaining.append(delivery)
            continue
        reserve_image(project, actor, media.id)
        session.image_receipts[delivery.receipt_key] = media.id
        confirm_delivery(project, actor, media.id)
        item.data["source"] = "mcp.read_media"
        item.data["delivery_confirmation"] = "acknowledged_by_successful_native_input"
        item.data["native_input_binding"] = {
            "request_receipt_id": receipt_id,
            "response_id": response_id,
            "payload_sha256": image.payload_sha256,
            "width": image.width,
            "height": image.height,
        }
    actor.pending_image_deliveries = remaining
