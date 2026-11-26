"""Decoded media observations; decisions about acceptance belong to directors."""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
from bisect import bisect_left
from collections.abc import Sequence
from dataclasses import dataclass, replace
from fractions import Fraction
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageOps
from pydantic import BaseModel, ConfigDict, Field, model_validator

from stateweaver.providers.base import AssetInput, ProviderContractError, ProviderExternalError


class MediaDecoderUnavailable(ProviderExternalError):
    """A required local media executable is missing; compatible with transport handling."""


class MediaDecoderTimeout(ProviderExternalError):
    """A local media operation exceeded its bounded time budget."""


@dataclass(frozen=True, slots=True)
class VideoMetadata:
    width: int
    height: int
    fps: float
    duration_seconds: float
    frame_count: int
    audio_streams: int
    video_codec: str
    video_start_offset_seconds: float = 0.0
    video_time_base: str | None = None


@dataclass(frozen=True, slots=True)
class Keyframe:
    frame_index: int
    timestamp_seconds: float
    path: Path
    sha256: str


def _run(command: list[str], *, timeout_seconds: float = 180.0) -> bytes:
    try:
        result = subprocess.run(command, capture_output=True, timeout=timeout_seconds, check=False)
    except FileNotFoundError:
        raise MediaDecoderUnavailable(
            f"media decoder {Path(command[0]).name} is unavailable"
        ) from None
    except subprocess.TimeoutExpired:
        raise MediaDecoderTimeout(
            f"media decoder {Path(command[0]).name} timed out after {timeout_seconds:g} seconds"
        ) from None
    if result.returncode != 0:
        # ffmpeg can echo paths or source URLs. Keep raw stderr out of transcript.
        raise ProviderContractError("media could not be decoded completely")
    return result.stdout


def _probe(path: Path, *, frames: bool = False) -> dict[str, Any]:
    command = ["ffprobe", "-v", "error", "-show_streams", "-show_format"]
    if frames:
        command += [
            "-select_streams",
            "v:0",
            "-show_frames",
            "-show_entries",
            "frame=best_effort_timestamp_time",
        ]
    command += ["-of", "json", str(path.resolve())]
    try:
        value: Any = json.loads(_run(command))
    except (UnicodeError, json.JSONDecodeError):
        raise ProviderContractError("media probe returned invalid metadata") from None
    if not isinstance(value, dict):
        raise ProviderContractError("media probe returned invalid metadata")
    return value


def _video_decode_timeout(duration_seconds: float) -> float:
    # Tiny clips retain the existing budget; long films get a bounded single-pass budget.
    return min(1800.0, max(180.0, 3 * duration_seconds))


def _decoded_frame_count(progress: bytes) -> int:
    """Require a successful terminal progress block, never a declared container count."""
    try:
        lines = progress.decode("ascii").splitlines()
    except UnicodeError:
        raise ProviderContractError("media decoder returned invalid frame progress") from None
    frame_count: int | None = None
    for index, line in enumerate(lines):
        key, separator, value = line.partition("=")
        if not separator:
            raise ProviderContractError("media decoder returned invalid frame progress")
        if key == "frame":
            if not value.isascii() or not value.isdecimal():
                raise ProviderContractError("media decoder returned invalid frame progress")
            frame_count = int(value)
        elif key == "progress":
            if value == "end" and index == len(lines) - 1 and frame_count is not None:
                if frame_count <= 0:
                    raise ProviderContractError("generated video has empty frames or duration")
                return frame_count
            if value != "continue":
                raise ProviderContractError("media decoder returned invalid frame progress")
            frame_count = None
    raise ProviderContractError("media decoder did not confirm complete frame progress")


def decode_video(path: Path, *, verify_audio: bool = False) -> VideoMetadata:
    """Validate video frames and optionally decode each declared audio stream."""
    data = _probe(path)
    streams = data.get("streams", [])
    if not isinstance(streams, list) or any(not isinstance(s, dict) for s in streams):
        raise ProviderContractError("media probe returned invalid metadata")
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise ProviderContractError("generated media contains no video stream")
    try:
        stream_start = video.get("start_time")
        container_start = data.get("format", {}).get("start_time")
        video_offset = (
            float(stream_start) - float(container_start)
            if stream_start is not None and container_start is not None
            else 0.0
        )
        result = VideoMetadata(
            width=int(video["width"]),
            height=int(video["height"]),
            fps=float(Fraction(video["avg_frame_rate"])),
            duration_seconds=float(video.get("duration") or data["format"]["duration"]),
            frame_count=0,
            audio_streams=sum(s.get("codec_type") == "audio" for s in streams),
            video_codec=str(video["codec_name"]),
            video_start_offset_seconds=video_offset,
            video_time_base=(str(Fraction(video["time_base"])) if video.get("time_base") else None),
        )
    except (KeyError, TypeError, ValueError, ZeroDivisionError, OverflowError):
        raise ProviderContractError("generated video has invalid dimensions or timing") from None
    timing = (result.fps, result.duration_seconds, video_offset)
    if not all(math.isfinite(value) for value in timing):
        raise ProviderContractError("generated video has invalid dimensions or timing")
    if min(result.width, result.height, result.fps, result.duration_seconds) <= 0:
        raise ProviderContractError("generated video has empty frames or duration")
    progress = _run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-xerror",
            "-err_detect",
            "explode",
            "-threads",
            "1",
            "-i",
            str(path.resolve()),
            "-map",
            "0:v:0",
            *(["-map", "0:a"] if verify_audio and result.audio_streams else []),
            "-vsync",
            "0",
            "-progress",
            "pipe:1",
            "-nostats",
            "-f",
            "null",
            "-",
        ],
        timeout_seconds=_video_decode_timeout(result.duration_seconds),
    )
    return replace(result, frame_count=_decoded_frame_count(progress))


class SamplingInterval(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(gt=0)
    fps: float = Field(default=8, ge=2, le=8)

    @model_validator(mode="after")
    def ordered(self) -> SamplingInterval:
        if self.end_seconds <= self.start_seconds:
            raise ValueError("sampling interval must end after it starts")
        return self


def sampling_indices(
    timestamps: Sequence[float],
    *,
    fps: float = 1,
    boundary_fps: float = 4,
    focus_intervals: Sequence[SamplingInterval] = (),
) -> list[int]:
    """Choose real presentation timestamps, with dense first/last-second evidence."""
    if not timestamps or any(not math.isfinite(t) for t in timestamps):
        raise ProviderContractError("decoded frame timestamps are invalid")
    if any(b < a for a, b in zip(timestamps, timestamps[1:], strict=False)):
        raise ProviderContractError("decoded frame timestamps are not ordered")
    if not all(math.isfinite(rate) and 0 < rate <= 8 for rate in (fps, boundary_fps)):
        raise ValueError("evidence sampling rates must be positive and at most 8 fps")
    times = [t - timestamps[0] for t in timestamps]
    last = times[-1]
    indices = {0, len(times) - 1}

    def sample(start: float, end: float, rate: float) -> None:
        for target in [start + i / rate for i in range(math.floor((end - start) * rate) + 1)] + [
            end
        ]:
            right = min(bisect_left(times, target), len(times) - 1)
            left = max(0, right - 1)
            indices.add(min((left, right), key=lambda i: abs(times[i] - target)))

    sample(0, last, fps)
    sample(0, min(1, last), boundary_fps)
    sample(max(0, last - 1), last, boundary_fps)
    for interval in focus_intervals:
        if interval.start_seconds > last:
            raise ProviderContractError("focus interval starts after the video's last frame")
        sample(interval.start_seconds, min(interval.end_seconds, last), interval.fps)
    return sorted(indices)


def extract_keyframes(
    path: Path,
    output_dir: Path,
    *,
    count: int | None = None,
    fps: float = 1,
    boundary_fps: float = 4,
    focus_intervals: Sequence[SamplingInterval] = (),
    excluded_intervals: Sequence[tuple[float, float]] = (),
) -> tuple[Keyframe, ...]:
    """Sample decoded frame indices, always including the exact first and last.

    A duration-minus-epsilon seek can return an earlier frame or no frame at all.
    Frame-index selection and actual presentation timestamps avoid that ambiguity.
    """
    if count is not None and count < 2:
        raise ValueError("at least two keyframes are required for boundary evidence")
    metadata = decode_video(path)
    probe = _probe(path, frames=True)
    frames = probe.get("frames", [])
    if not isinstance(frames, list) or len(frames) != metadata.frame_count:
        raise ProviderContractError("decoded frame timestamps do not match frame count")
    try:
        timestamps = [float(frame["best_effort_timestamp_time"]) for frame in frames]
    except (KeyError, TypeError, ValueError):
        raise ProviderContractError("keyframe presentation timestamp is missing") from None
    if count is None:
        indices = sampling_indices(
            timestamps, fps=fps, boundary_fps=boundary_fps, focus_intervals=focus_intervals
        )
    else:
        sample_count = min(count, metadata.frame_count)
        indices = sorted(
            {
                round(i * (metadata.frame_count - 1) / max(1, sample_count - 1))
                for i in range(sample_count)
            }
        )
    if any(
        not math.isfinite(start) or not math.isfinite(end) or start < 0 or end < start
        for start, end in excluded_intervals
    ):
        raise ValueError("invalid excluded keyframe interval")
    # Apply exclusions before ffmpeg creates any image; never extract then discard.
    indices = [
        index
        for index in indices
        if not any(
            start - 1e-6 <= timestamps[index] <= end + 1e-6 for start, end in excluded_intervals
        )
    ]
    if not indices:
        return ()
    output_dir.mkdir(parents=True, exist_ok=True)
    # A unique prefix ties all frames to this content; callers can reuse output dirs.
    content_hash = hashlib.sha256(path.read_bytes() + json.dumps(indices).encode()).hexdigest()[:16]
    output_pattern = output_dir.resolve() / f"{content_hash}-%03d.png"
    expression = "+".join(f"eq(n\\,{index})" for index in indices)
    _run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-xerror",
            "-i",
            str(path.resolve()),
            "-map",
            "0:v:0",
            "-vf",
            "select=" + expression,
            "-vsync",
            "0",
            "-y",
            str(output_pattern),
        ]
    )
    result = []
    for position, index in enumerate(indices, 1):
        frame_path = output_dir.resolve() / f"{content_hash}-{position:03d}.png"
        if not frame_path.is_file():
            raise ProviderContractError("decoder did not produce every requested keyframe")
        try:
            timestamp = float(frames[index]["best_effort_timestamp_time"])
        except (KeyError, TypeError, ValueError):
            raise ProviderContractError("keyframe presentation timestamp is missing") from None
        result.append(
            Keyframe(
                index, timestamp, frame_path, hashlib.sha256(frame_path.read_bytes()).hexdigest()
            )
        )
    return tuple(result)


def boundary_comparison(
    previous: Keyframe, current: Keyframe, *, tile_size: tuple[int, int] = (768, 432)
) -> AssetInput:
    """One labeled image makes geometric discontinuity at a Clip seam inspectable."""
    width, height = tile_size
    sheet = Image.new("RGB", (width * 2, height + 40), "white")
    drawing = ImageDraw.Draw(sheet)
    for index, (label, frame) in enumerate(
        (("Previous accepted END", previous), ("Current candidate START", current))
    ):
        content = frame.path.read_bytes()
        if hashlib.sha256(content).hexdigest() != frame.sha256:
            raise ProviderContractError("boundary frame bytes changed after extraction")
        with Image.open(BytesIO(content)) as original:
            fitted = ImageOps.contain(original.convert("RGB"), tile_size)
            sheet.paste(
                fitted,
                (index * width + (width - fitted.width) // 2, 40 + (height - fitted.height) // 2),
            )
        drawing.text((index * width + 12, 12), label, fill="black")
    output = BytesIO()
    sheet.save(output, format="PNG")
    return AssetInput("clip-boundary-comparison.png", output.getvalue(), "image/png")
