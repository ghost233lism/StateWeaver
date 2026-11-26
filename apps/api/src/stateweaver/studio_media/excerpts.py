"""Local, decoded excerpts with exact source timing; never a generation request."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from PIL import Image

from stateweaver.providers.base import ProviderContractError

from .evidence import _probe, _run, decode_video


@dataclass(frozen=True, slots=True)
class MediaExcerpt:
    path: Path
    mime_type: str
    duration: float | None
    width: int | None
    height: int | None
    source_start: float
    source_end: float | None
    frame_count: int | None = None
    fps: float | None = None
    source_start_frame: int | None = None
    source_end_frame: int | None = None


def extract_video_tail(
    source: Path, destination: Path, *, frame_count: int, fps: int
) -> MediaExcerpt:
    """Keep the final real frames by index, without padding or rate conversion."""
    metadata = decode_video(source)
    if (
        type(frame_count) is not int
        or type(fps) is not int
        or fps <= 0
        or not 2 <= frame_count / fps <= 15
        or abs(metadata.fps - fps) > 1e-6
        or metadata.frame_count < frame_count
        or abs(metadata.duration_seconds - metadata.frame_count / fps) > 0.001
    ):
        raise ProviderContractError(
            "Continuation tail requires a complete source on its native frame grid"
        )
    start, end = metadata.frame_count - frame_count, metadata.frame_count
    destination.mkdir(parents=True, exist_ok=True)
    path = destination / "reference.mp4"
    _run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-nostdin",
            "-y",
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-an",
            "-vf",
            f"trim=start_frame={start}:end_frame={end},setpts=PTS-STARTPTS",
            "-vsync",
            "0",
            "-c:v",
            "libx264",
            "-crf",
            "15",
            "-preset",
            "fast",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(path),
        ]
    )
    decoded = decode_video(path)
    if (
        decoded.frame_count != frame_count
        or abs(decoded.fps - fps) > 1e-6
        or abs(decoded.duration_seconds - frame_count / fps) > 0.001
        or (decoded.width, decoded.height) != (metadata.width, metadata.height)
        or decoded.audio_streams != 0
    ):
        raise ProviderContractError(
            "Prepared continuation does not contain the exact final frame window"
        )
    return MediaExcerpt(
        path,
        "video/mp4",
        frame_count / fps,
        decoded.width,
        decoded.height,
        start / fps,
        end / fps,
        frame_count,
        float(fps),
        start,
        end,
    )


def extract_media(
    source: Path,
    destination: Path,
    *,
    kind: Literal["video", "audio", "image"],
    start_seconds: float,
    end_seconds: float | None = None,
) -> MediaExcerpt:
    metadata = decode_video(source)
    if not math.isfinite(start_seconds) or not 0 <= start_seconds < metadata.duration_seconds:
        raise ProviderContractError("Excerpt starts outside the source video")
    if kind != "image" and (
        end_seconds is None
        or not math.isfinite(end_seconds)
        or not 2 <= end_seconds - start_seconds <= 15
        or end_seconds > metadata.duration_seconds + 0.001
    ):
        raise ProviderContractError("Reference excerpt must be a 2–15 second source interval")
    destination.mkdir(parents=True, exist_ok=True)
    command = ["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", str(source)]
    if kind == "image":
        # Use actual PTS to record which frame was extracted, not a nominal-fps guess.
        frames = _probe(source, frames=True).get("frames", [])
        stamps = [
            float(f["best_effort_timestamp_time"])
            for f in frames
            if "best_effort_timestamp_time" in f
        ]
        if not stamps:
            raise ProviderContractError("Source frame timestamps are unavailable")
        origin = stamps[0]
        index = min(range(len(stamps)), key=lambda i: abs(stamps[i] - origin - start_seconds))
        actual = stamps[index] - origin
        path = destination / "frame.png"
        command += ["-vf", f"select=eq(n\\,{index})", "-frames:v", "1", str(path)]
        _run(command)
        with Image.open(path) as pixels:
            pixels.load()
            return MediaExcerpt(path, "image/png", None, pixels.width, pixels.height, actual, None)
    assert end_seconds is not None
    command += ["-ss", f"{start_seconds:.9f}", "-t", f"{end_seconds - start_seconds:.9f}"]
    if kind == "video":
        path = destination / "reference.mp4"
        # Visual continuation/edit sources carry no implicit soundtrack reuse.
        command += [
            "-map",
            "0:v:0",
            "-an",
            "-c:v",
            "libx264",
            "-crf",
            "15",
            "-preset",
            "fast",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(path),
        ]
        _run(command)
        decoded = decode_video(path)
        if not 2 <= decoded.duration_seconds <= 15.001:
            raise ProviderContractError("Decoded video reference is outside H3 duration limits")
        return MediaExcerpt(
            path,
            "video/mp4",
            min(decoded.duration_seconds, 15),
            decoded.width,
            decoded.height,
            start_seconds,
            end_seconds,
        )
    if metadata.audio_streams == 0:
        raise ProviderContractError("Selected source contains no audio stream")
    path = destination / "reference.wav"
    command += ["-map", "0:a:0", "-vn", "-ac", "1", "-ar", "48000", "-c:a", "pcm_s16le", str(path)]
    _run(command)
    probe = json.loads(_run(["ffprobe", "-v", "error", "-show_format", "-of", "json", str(path)]))
    duration = float(probe["format"]["duration"])
    if not 2 <= duration <= 15.001:
        raise ProviderContractError("Decoded audio reference is outside H3 duration limits")
    return MediaExcerpt(
        path, "audio/wav", min(duration, 15), None, None, start_seconds, end_seconds
    )
