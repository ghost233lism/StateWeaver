"""Small, explicitly labelled image views for long-lived model conversations.

Original CAS media and provider reference inputs are never changed by this view.
"""

from __future__ import annotations

from io import BytesIO

from PIL import Image, ImageOps

MAX_VIEW_BYTES = 256 * 1024
MAX_VIEW_EDGE = 1536
REVIEW_VIEW_SCALE = 1.0
REVIEW_VIEW_EDGE = 3072
REVIEW_VIEW_QUALITY = 95


def review_view_policy() -> dict[str, str | float | int]:
    """Fixed deployment policy, never a conversation/compaction-driven adjustment."""
    return {
        "version": "review-jpeg95-v2",
        "scale": REVIEW_VIEW_SCALE,
        "max_edge": REVIEW_VIEW_EDGE,
        "quality": REVIEW_VIEW_QUALITY,
        "subsampling": 0,
    }


def review_image_view(content: bytes, mime_type: str) -> tuple[bytes, str]:
    """Retain four-frame evidence detail; originals remain lossless in CAS.

    A native 1344x768 frame stays native in a 2688x1600 contact sheet. Unlike
    ordinary transcript previews, review images are not squeezed into 256 KiB.
    This is an application-side view, not a claim about the model's image encoder.
    """
    with Image.open(BytesIO(content)) as source:
        image = ImageOps.exif_transpose(source).convert("RGBA")
        background = Image.new("RGBA", image.size, "white")
        background.alpha_composite(image)
        image = background.convert("RGB")
    image.thumbnail((REVIEW_VIEW_EDGE, REVIEW_VIEW_EDGE), Image.Resampling.LANCZOS)
    if REVIEW_VIEW_SCALE != 1.0:
        image = image.resize(
            (
                max(1, round(image.width * REVIEW_VIEW_SCALE)),
                max(1, round(image.height * REVIEW_VIEW_SCALE)),
            ),
            Image.Resampling.LANCZOS,
        )
    output = BytesIO()
    image.save(output, format="JPEG", quality=REVIEW_VIEW_QUALITY, subsampling=0, optimize=True)
    return output.getvalue(), "image/jpeg"


def image_view(
    content: bytes,
    mime_type: str,
    *,
    original: bool = False,
    max_bytes: int = MAX_VIEW_BYTES,
    max_edge: int = MAX_VIEW_EDGE,
) -> tuple[bytes, str]:
    if max_bytes < 1024 or max_edge < 1:
        raise ValueError("Image view limits are too small")
    with Image.open(BytesIO(content)) as source:
        if original or (len(content) <= max_bytes and max(source.size) <= max_edge):
            return content, mime_type
        image = ImageOps.exif_transpose(source).convert("RGBA")
        background = Image.new("RGBA", image.size, "white")
        background.alpha_composite(image)
        image = background.convert("RGB")
    image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
    while True:
        for quality in (90, 85, 80):
            output = BytesIO()
            image.save(output, format="JPEG", quality=quality, optimize=True)
            data = output.getvalue()
            if len(data) <= max_bytes:
                return data, "image/jpeg"
        image = image.resize(
            (max(1, int(image.width * 0.85)), max(1, int(image.height * 0.85))),
            Image.Resampling.LANCZOS,
        )
