"""Image 2.5 generation and reference edits, with decoded image materialization."""

from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from io import BytesIO
from typing import Any

from PIL import Image, UnidentifiedImageError

from stateweaver.providers.base import (
    AssetInput,
    GeneratedAsset,
    GenerationResult,
    HttpRequest,
    HttpTransport,
    ProviderContractError,
    ProviderExternalError,
    UrllibTransport,
    join_url,
    json_request,
    require_binary,
    require_http_success,
    require_success,
)
from stateweaver.studio_media.submission_errors import (
    ImageSubmissionRejected,
    image_rejection_status,
)

IMAGE_MODEL = "gt-im-2.5-sun-a"


@dataclass(frozen=True, slots=True)
class ImageRequest:
    prompt: str = field(repr=False)
    size: str = "1024x1024"
    quality: str = "high"
    count: int = 1

    def __post_init__(self) -> None:
        if not self.prompt.strip():
            raise ValueError("image prompt must not be empty")
        if self.count < 1:
            raise ValueError("image count must be positive")
        if self.quality not in {"auto", "low", "medium", "high"}:
            raise ValueError("unsupported image quality")


@dataclass(frozen=True, slots=True)
class Image25Adapter:
    base_url: str = field(repr=False)
    api_key: str = field(repr=False)
    model: str = IMAGE_MODEL
    timeout_seconds: float = 600.0
    max_download_bytes: int = 64 * 1024 * 1024
    transport: HttpTransport = field(default_factory=UrllibTransport, repr=False)

    def __post_init__(self) -> None:
        if not self.model.strip():
            raise ProviderContractError("image API requires an explicitly configured model")
        if not self.api_key.strip() or not self.base_url.strip():
            raise ProviderExternalError("image provider credentials or endpoint are missing")
        if self.timeout_seconds <= 0 or self.max_download_bytes <= 0:
            raise ValueError("timeouts and response byte limits must be positive")

    def generate(self, request: ImageRequest) -> GenerationResult:
        """One submission, with no automatic retry of ambiguous network failures."""
        response_payload = self._submit(
            json_request(
                "POST",
                join_url(self.base_url, "/images/generations"),
                self.api_key,
                self._fields(request),
                self.timeout_seconds,
            )
        )
        return self._parse(response_payload)

    def edit(
        self, request: ImageRequest, images: Sequence[AssetInput], mask: AssetInput | None = None
    ) -> GenerationResult:
        if not images:
            raise ValueError("reference editing requires at least one image")
        for image in images:
            _inspect_image(image.content)
        if mask is not None:
            _inspect_image(mask.content)
        payload = self._fields(request)
        payload["image"] = [image_data_url(image) for image in images]
        if mask is not None:
            payload["mask"] = image_data_url(mask)
        response_payload = self._submit(
            json_request(
                "POST",
                join_url(self.base_url, "/images/edits"),
                self.api_key,
                payload,
                self.timeout_seconds,
            )
        )
        return self._parse(response_payload)

    def _submit(self, request: HttpRequest) -> Mapping[str, Any]:
        request = replace(request, capture_rejection_details=True)
        try:
            return require_success(self.transport.send(request), request=request)
        except (ProviderExternalError, ProviderContractError) as exc:
            status = getattr(exc, "http_status", None) or image_rejection_status(str(exc))
            if status == 503 and getattr(exc, "definitely_not_accepted", False) is not True:
                raise
            if status in {400, 401, 402, 403, 422, 425, 429, 503}:
                raise ImageSubmissionRejected(
                    status,
                    details=getattr(exc, "rejection", None),
                    retry_after_seconds=getattr(exc, "retry_after_seconds", None),
                ) from exc
            raise

    def materialize(self, asset: GeneratedAsset, *, name: str = "reference.png") -> AssetInput:
        """Download or decode and inspect pixels before returning an image to CAS."""
        if asset.content is not None:
            content = asset.content
        elif asset.base64_data:
            try:
                content = base64.b64decode(asset.base64_data, validate=True)
            except (binascii.Error, ValueError):
                raise ProviderContractError("image response contains invalid base64") from None
        elif asset.url:
            # A provider result may be a signed public URL. Never forward our API key.
            request = HttpRequest(
                "GET",
                asset.url,
                timeout_seconds=self.timeout_seconds,
                max_response_bytes=self.max_download_bytes,
            )
            response = self.transport.send(request)
            require_http_success(response, request=request)
            content = require_binary(response, max_bytes=self.max_download_bytes)
        else:
            raise ProviderContractError("image response has no materializable pixels")
        if len(content) > self.max_download_bytes:
            raise ProviderContractError("generated image exceeds the response byte limit")
        media_type = _inspect_image(content)
        return AssetInput(name, content, media_type)

    def _fields(self, request: ImageRequest) -> dict[str, Any]:
        return {
            "model": self.model,
            "prompt": request.prompt,
            "size": request.size,
            "quality": request.quality,
            "n": request.count,
            "output_format": "png",
            "output_compression": 100,
        }

    def _parse(self, payload: Mapping[str, Any]) -> GenerationResult:
        observed = payload.get("model")
        if observed is not None and observed != self.model:
            raise ProviderContractError("image provider returned a different model")
        entries = payload.get("data")
        if not isinstance(entries, list) or not entries:
            raise ProviderContractError("image response requires non-empty data")
        assets = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise ProviderContractError("image response entry must be an object")
            url, encoded = entry.get("url"), entry.get("b64_json")
            has_url = isinstance(url, str) and bool(url.strip())
            has_encoded = isinstance(encoded, str) and bool(encoded.strip())
            if has_url == has_encoded:
                raise ProviderContractError("image entry requires exactly one URL or base64 image")
            assets.append(
                GeneratedAsset(
                    url=url if has_url else None, base64_data=encoded if has_encoded else None
                )
            )
        raw_usage = payload.get("usage", {})
        usage = (
            {str(k): v for k, v in raw_usage.items() if isinstance(v, int)}
            if isinstance(raw_usage, dict)
            else {}
        )
        return GenerationResult(
            "image25",
            self.model,
            tuple(assets),
            usage=usage,
            request_id=str(payload.get("id", payload.get("created", ""))) or None,
            observed_model=observed if isinstance(observed, str) else None,
        )


def _inspect_image(content: bytes) -> str:
    try:
        with Image.open(BytesIO(content)) as image:
            image.load()
            media_type = Image.MIME.get(image.format or "")
            if not media_type or not media_type.startswith("image/"):
                raise ProviderContractError("unsupported generated image encoding")
            return media_type
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise ProviderContractError("provider returned an undecodable image") from None


def image_data_url(image: AssetInput) -> str:
    """Encode actual bytes and their detected media type, ignoring caller MIME guesses."""
    media_type = _inspect_image(image.content)
    return f"data:{media_type};base64,{base64.b64encode(image.content).decode('ascii')}"
