"""Lossless, bounded crops of inspected references; never generate new pixels."""

from __future__ import annotations

import math
from dataclasses import dataclass
from io import BytesIO

from PIL import Image, ImageOps

from stateweaver.providers.base import ProviderContractError


def is_reference_board(view: str) -> bool:
    return view in {"three_view", "expression_grid"}


def valid_h3_image_aspect(width: int, height: int) -> bool:
    return min(width, height) > 0 and width <= 4 * height and height <= 4 * width


def validate_h3_image_aspect(width: int, height: int) -> None:
    if not valid_h3_image_aspect(width, height):
        raise ProviderContractError(
            f"H3 参考图片宽高比须在 1:4 至 4:1 之间，当前 {width}x{height}；"
            "请查看原图后调整裁剪范围或选择合规参考"
        )


@dataclass(frozen=True, slots=True)
class ReferenceCrop:
    content: bytes
    box: tuple[int, int, int, int]
    source_size: tuple[int, int]
    size: tuple[int, int]


def crop_reference(content: bytes, box: tuple[float, float, float, float]) -> ReferenceCrop:
    if not all(math.isfinite(v) and 0 <= v <= 1 for v in box):
        raise ProviderContractError("裁剪区域必须位于原图范围内")
    left, top, right, bottom = box
    if left >= right or top >= bottom:
        raise ProviderContractError("裁剪区域没有有效面积")
    with Image.open(BytesIO(content)) as source:
        source.load()
        image = ImageOps.exif_transpose(source)
        width, height = image.size
        pixels = (
            math.floor(left * width),
            math.floor(top * height),
            math.ceil(right * width),
            math.ceil(bottom * height),
        )
        cropped = image.crop(pixels)
        if min(cropped.size) < 32:
            raise ProviderContractError("裁剪区域过小，无法作为清晰的单视图参考")
        validate_h3_image_aspect(*cropped.size)
        output = BytesIO()
        cropped.save(output, format="PNG")
        return ReferenceCrop(output.getvalue(), pixels, image.size, cropped.size)
