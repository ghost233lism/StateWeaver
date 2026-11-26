"""NanoBanana native image protocol; final images only, no refusal rerouting."""

from __future__ import annotations

import base64
import binascii
import hashlib
import math
from collections.abc import Mapping, Sequence
from dataclasses import replace
from io import BytesIO
from typing import Any

from PIL import Image, ImageOps

from stateweaver.providers.base import (
    AssetInput,
    GenerationResult,
    ProviderContractError,
    join_url,
    json_request,
)

from .images import Image25Adapter, ImageRequest, image_data_url

MODEL = "ge-na-ba-2.1-a"
CANONICAL = "gemini-nano-banana-2.1"

NATIVE_ASPECTS = ("1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9")


def native_aspect(size: str) -> tuple[str, str]:
    width, height = map(int, size.split("x"))
    if min(width, height) <= 0 or max(width, height) > 4096:
        raise ProviderContractError("Native target canvas is outside the verified size range")
    divisor = math.gcd(width, height)
    requested = f"{width // divisor}:{height // divisor}"
    if requested in NATIVE_ASPECTS:
        return requested, requested
    # Native generation uses a supported canvas; existing contain_pad delivery
    # preserves every output pixel and the exact requested target dimensions.
    # No reference input is cropped, stretched or reencoded here.
    ratio = width / height
    if not 0.25 <= ratio <= 4:
        raise ProviderContractError("Native target image aspect is outside supported input bounds")
    selected = min(
        NATIVE_ASPECTS,
        key=lambda value: abs(
            math.log(ratio / (int(value.split(":")[0]) / int(value.split(":")[1])))
        ),
    )
    return requested, selected


class NativeImageResponseError(ProviderContractError):
    """An explicit returned result is terminal, not proof that POST was unaccepted."""

    outcome_known = True
    definitely_not_accepted = False


class NativeImagePolicyRejection(NativeImageResponseError):
    failure_category = "policy_rejection"


class GeminiImageAdapter(Image25Adapter):
    @property
    def generation_path(self) -> str:
        if self.model != MODEL:
            raise ProviderContractError("Native image binding requires its exact configured model")
        return "/models/" + MODEL + ":generateContent"

    def native_payload(self, fields: Mapping[str, Any]) -> dict[str, Any]:
        _, aspect = native_aspect(str(fields["size"]))
        if fields.get("n", 1) != 1 or "mask" in fields:
            raise ProviderContractError(
                "Native image protocol does not support masks or multiple outputs"
            )
        parts = []
        for value in fields.get("image", []):
            if not isinstance(value, str) or not value.startswith("data:image/"):
                raise ProviderContractError(
                    "Native image reference must contain exact inline pixels"
                )
            header, encoded = value.split(",", 1)
            content = base64.b64decode(encoded, validate=True)
            with Image.open(BytesIO(content)) as image:
                image.load()
                mime = Image.MIME.get(image.format or "")
            if header != f"data:{mime};base64":
                raise ProviderContractError("Native reference image MIME does not match its pixels")
            parts.append({"inlineData": {"mimeType": mime, "data": encoded}})
        parts.append({"text": fields["prompt"]})
        return {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {
                "responseModalities": ["TEXT", "IMAGE"],
                "imageConfig": {"aspectRatio": aspect},
            },
        }

    def normalize_response(self, data: Mapping[str, Any], size: str) -> dict[str, Any]:
        try:
            return self._normalize_response(data, size)
        except NativeImageResponseError:
            raise
        except (ProviderContractError, ValueError, TypeError, KeyError, AttributeError, OSError):
            raise NativeImageResponseError(
                "Native image response fails its pinned contract"
            ) from None

    def _normalize_response(self, data: Mapping[str, Any], size: str) -> dict[str, Any]:
        feedback = data.get("promptFeedback", {})
        if isinstance(feedback, Mapping) and feedback.get("blockReason"):
            raise NativeImagePolicyRejection("Native image service reports a blocked prompt")
        observed = data.get("modelVersion")
        if observed not in {MODEL, CANONICAL}:
            raise ProviderContractError("Native image response has an absent or unexpected model")
        images: list[Mapping[str, Any]] = []
        for candidate in data.get("candidates", []):
            if candidate.get("finishReason") in {
                "SAFETY",
                "IMAGE_SAFETY",
                "RECITATION",
                "BLOCKLIST",
                "PROHIBITED_CONTENT",
                "SPII",
                "IMAGE_PROHIBITED_CONTENT",
            }:
                raise NativeImagePolicyRejection("Native image service reports a blocked result")
            for part in candidate.get("content", {}).get("parts", []):
                if part.get("thought") is True:
                    continue
                inline = part.get("inlineData", part.get("inline_data"))
                if isinstance(inline, Mapping) and str(
                    inline.get("mimeType", inline.get("mime_type", ""))
                ).startswith("image/"):
                    images.append(inline)
        if len(images) != 1:
            raise ProviderContractError(
                "Native response requires exactly one non-thought final image"
            )
        encoded = images[0]["data"]
        if not isinstance(encoded, str) or len(encoded) > 4 * ((self.max_download_bytes + 2) // 3):
            raise ProviderContractError("Native image exceeds the decoded image byte limit")
        try:
            content = base64.b64decode(encoded, validate=True)
        except binascii.Error:
            raise ProviderContractError("Native final image has invalid base64") from None
        target_width, target_height = map(int, size.split("x"))
        target = (target_width, target_height)
        if min(target) <= 0 or max(target) > 4096:
            raise ProviderContractError("Native target canvas is outside the verified size range")
        with Image.open(BytesIO(content)) as image:
            image.load()
            if (
                Image.MIME.get(image.format or "")
                != images[0].get("mimeType", images[0].get("mime_type"))
                or getattr(image, "n_frames", 1) != 1
            ):
                raise ProviderContractError("Native final image MIME or animation is invalid")
            native_size = image.size
            fitted = ImageOps.contain(image.convert("RGB"), target, Image.Resampling.LANCZOS)
            canvas = Image.new("RGB", target, "black")
            canvas.paste(
                fitted, ((target[0] - fitted.width) // 2, (target[1] - fitted.height) // 2)
            )
            buffer = BytesIO()
            canvas.save(buffer, format="PNG")
        geometry = {
            "policy": "contain_pad",
            "requested_aspect_ratio": native_aspect(size)[0],
            "native_request_aspect_ratio": native_aspect(size)[1],
            "native_width": native_size[0],
            "native_height": native_size[1],
            "native_sha256": hashlib.sha256(content).hexdigest(),
            "width": target[0],
            "height": target[1],
        }
        usage = data.get("usageMetadata", {})
        if not isinstance(usage, Mapping):
            usage = {}
        return {
            "model": MODEL,
            "observed_model": observed,
            "id": data.get("responseId"),
            "data": [{"b64_json": base64.b64encode(buffer.getvalue()).decode()}],
            "usage": {
                k: v for k, v in usage.items() if isinstance(v, int) and not isinstance(v, bool)
            },
            "native_canvas": geometry,
        }

    def _parse(self, payload: Mapping[str, Any]) -> GenerationResult:
        result = super()._parse(payload)
        return replace(
            result,
            provider="gemini-native-image",
            observed_model=payload.get("observed_model"),
            metadata={"native_canvas": payload.get("native_canvas")},
        )

    def generate(self, request: ImageRequest) -> GenerationResult:
        return self.edit(request, [])

    def edit(
        self, request: ImageRequest, images: Sequence[AssetInput], mask: AssetInput | None = None
    ) -> GenerationResult:
        fields = self._fields(request)
        fields["image"] = [image_data_url(image) for image in images]
        if mask is not None:
            raise ProviderContractError("Native image masks are unsupported")
        native = self.native_payload(fields)
        data = self._submit(
            json_request(
                "POST",
                join_url(self.base_url, self.generation_path),
                self.api_key,
                native,
                self.timeout_seconds,
            )
        )
        return self._parse(self.normalize_response(data, request.size))
