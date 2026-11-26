"""Explicit H3 endpoint anchoring; source selection belongs to the Studio domain."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from stateweaver.providers.base import ProviderContractError

ENDPOINT_ANCHOR = "endpoint-anchor-v1"
CONTINUATION_FPS = 24
CONTINUATION_FRAMES = 73
CONTINUATION_SECONDS = CONTINUATION_FRAMES / CONTINUATION_FPS


@dataclass(frozen=True, slots=True)
class EndpointAnchor:
    source_candidate_id: str
    endpoint_sha256: str
    tail_sha256: str
    tail_frame_count: int = 72
    endpoint_only: bool = False

    def __post_init__(self) -> None:
        if type(self.tail_frame_count) is not int or self.tail_frame_count not in (72, 73):
            raise ProviderContractError(
                "Latent continuation requires 72 legacy or 73 real tail frames"
            )
        if (
            not isinstance(self.source_candidate_id, str)
            or not self.source_candidate_id.strip()
            or len(self.source_candidate_id) > 128
        ):
            raise ProviderContractError("Latent continuation requires a pinned predecessor")
        if any(
            not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None
            for value in (
                (self.endpoint_sha256,)
                if self.endpoint_only
                else (self.endpoint_sha256, self.tail_sha256)
            )
        ):
            raise ProviderContractError("Latent continuation requires exact endpoint/tail hashes")

    def payload(self) -> dict[str, Any]:
        if self.endpoint_only:
            return {
                "mode": ENDPOINT_ANCHOR,
                "binding": "endpoint-only-v1",
                "image_index": 1,
                "source_candidate_id": self.source_candidate_id,
                "endpoint_sha256": self.endpoint_sha256,
            }
        result = {
            "mode": ENDPOINT_ANCHOR,
            "image_index": 1,
            "video_index": 1,
            "source_candidate_id": self.source_candidate_id,
            "endpoint_sha256": self.endpoint_sha256,
            "tail_sha256": self.tail_sha256,
        }
        # Preserve the original wire descriptor for already-pinned 72-frame jobs.
        if self.tail_frame_count != 72:
            result["tail_frame_count"] = self.tail_frame_count
        return result

    @classmethod
    def from_payload(cls, value: dict[str, Any]) -> EndpointAnchor:
        return cls(
            value["source_candidate_id"],
            value["endpoint_sha256"],
            value.get("tail_sha256", ""),
            value.get("tail_frame_count", 72),
            value.get("binding") == "endpoint-only-v1",
        )


def advertised_modes(value: Any) -> tuple[str, ...]:
    if not isinstance(value, dict) or value.get("enabled") is not True:
        return ("off",)
    modes = value.get("modes")
    if not isinstance(modes, list) or ENDPOINT_ANCHOR not in modes:
        return ("off",)
    return ("off", ENDPOINT_ANCHOR)


def advertised_tail_frame_counts(value: Any) -> tuple[int, ...]:
    if ENDPOINT_ANCHOR not in advertised_modes(value):
        return ()
    counts = value.get("tail_frame_counts", [72])
    if not isinstance(counts, list):
        return ()
    return tuple(sorted({n for n in counts if type(n) is int and n in (72, 73)}))
