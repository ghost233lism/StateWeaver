"""Lazy, disposable Set playback caches; never create or change production jobs."""

from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import subprocess
import tempfile
import time
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from threading import Lock
from typing import Any
from urllib.parse import quote

from sqlalchemy import select, text

from stateweaver.persistence.models import StudioEntityRow
from stateweaver.studio.browser_media import VerifiedBrowserFiles
from stateweaver.studio.contracts import Media, SetPreview, SetPreviewSegment
from stateweaver.studio.queries import StudioQueries
from stateweaver.studio.store import StudioError, StudioStore
from stateweaver.studio_media.evidence import decode_video


def preview_sources(
    store: StudioStore, project_id: str, set_id: str
) -> tuple[SetPreview, list[Media]]:
    """Read only this Set's active Clips and their exact selected video identities."""
    store.require_project(project_id)
    with store.database.session() as session:
        session.execute(text("BEGIN"))
        sets = StudioQueries._rows(session, project_id, "sets", ids=[set_id])
        if not sets:
            raise StudioError("片场不存在", 404)
        clip_ids = sets[0].payload.get("clip_ids", [])
        # Even SQL json_extract on a full Clip decodes its entire planning
        # history. Use the same version-fenced display rows as the canvas.
        clips = sorted(
            [
                {
                    "id": row.id,
                    "title": row.payload["title"],
                    "order": row.payload["order"],
                    "status": row.status,
                    "selected": row.payload.get("accepted_candidate_id"),
                }
                for row in StudioQueries._rows(session, project_id, "clips", ids=clip_ids)
                if row.parent_id == set_id and not row.payload.get("archived_at")
            ],
            key=lambda clip: (clip["order"], clip["id"]),
        )
        result = SetPreview(
            set_id=set_id,
            total_clip_count=len(clips),
            selected_clip_count=sum(
                c["status"] == "accepted" and bool(c["selected"]) for c in clips
            ),
        )
        prefix = []
        for clip in clips:
            if clip["status"] != "accepted" or not clip["selected"]:
                result.waiting_clip_id, result.waiting_clip_title = clip["id"], clip["title"]
                break
            prefix.append(clip)
        if not prefix:
            result.message = (
                "首条 Clip 选用后即可连播。" if clips else "规划并选用 Clip 后即可连播。"
            )
            return result, []
        candidates = {
            row.id: row
            for row in session.execute(
                select(
                    StudioEntityRow.id,
                    StudioEntityRow.payload["clip_id"].as_string().label("clip_id"),
                    StudioEntityRow.payload["media_id"].as_string().label("media_id"),
                ).where(
                    StudioEntityRow.project_id == project_id,
                    StudioEntityRow.kind == "candidates",
                    StudioEntityRow.id.in_([c["selected"] for c in prefix]),
                )
            )
        }
        media = {
            row.id: Media.model_validate(row.payload)
            for row in session.scalars(
                select(StudioEntityRow).where(
                    StudioEntityRow.project_id == project_id,
                    StudioEntityRow.kind == "media",
                    StudioEntityRow.id.in_([c.media_id for c in candidates.values() if c.media_id]),
                )
            )
        }
        sources = []
        for clip in prefix:
            candidate = candidates.get(clip["selected"])
            source = media.get(candidate.media_id) if candidate else None
            # An audited user/compromise selection can retain a rejected verdict.
            # The Clip's explicit selection is authoritative, not candidate.status.
            if (
                not candidate
                or candidate.clip_id != clip["id"]
                or not source
                or not source.mime_type.startswith("video/")
            ):
                raise StudioError(f"{clip['title']} 的已选版本缺少可播放视频，请检查该 Clip。", 422)
            sources.append(source)
            result.segments.append(
                SetPreviewSegment(
                    clip_id=clip["id"],
                    candidate_id=candidate.id,
                    media_id=source.id,
                    title=clip["title"],
                    order=clip["order"],
                    duration=source.duration,
                )
            )
        identity = [
            "set-preview-v1",
            project_id,
            set_id,
            [
                [s.clip_id, s.candidate_id, s.media_id, m.sha256]
                for s, m in zip(result.segments, sources, strict=True)
            ],
        ]
        result.fingerprint = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
        result.status = "idle"
        return result, sources


def _run(command: list[str]) -> bytes:
    try:
        process = subprocess.run(command, capture_output=True, check=False, timeout=600)
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError("预览处理暂不可用，请重试。") from None
    if process.returncode:
        # ffmpeg errors may contain paths or source URLs; keep them out of HTTP responses.
        raise ValueError("预览拼接失败，请重试。")
    return process.stdout


def _probe(path: Path) -> dict[str, Any]:
    value = json.loads(
        _run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_streams",
                "-show_format",
                "-show_data_hash",
                "sha256",
                "-of",
                "json",
                str(path),
            ]
        )
    )
    if not isinstance(value, dict):
        raise ValueError("视频信息无效")
    return value


def _layout(probe: dict[str, Any]) -> list[dict[str, Any]]:
    fields = (
        "codec_type",
        "codec_name",
        "codec_tag_string",
        "profile",
        "level",
        "extradata_hash",
        "width",
        "height",
        "pix_fmt",
        "sample_aspect_ratio",
        "field_order",
        "time_base",
        "avg_frame_rate",
        "sample_fmt",
        "sample_rate",
        "channels",
        "channel_layout",
        "color_range",
        "color_space",
        "color_transfer",
        "color_primaries",
    )
    return [
        {k: stream.get(k) for k in fields}
        for stream in probe["streams"]
        if stream.get("codec_type") in {"video", "audio"}
    ]


def concat_preview(paths: list[Path], destination: Path) -> None:
    """Copy compatible streams; reuse final-film normalization for mixed layouts."""
    probes = [_probe(path) for path in paths]
    layouts = [_layout(probe) for probe in probes]
    compatible = all(layout == layouts[0] for layout in layouts)
    types = [stream["codec_type"] for stream in layouts[0]]
    if compatible and types.count("video") == 1 and types.count("audio") <= 1:
        try:
            durations = [
                float(
                    next(s for s in p["streams"] if s["codec_type"] == "video").get("duration")
                    or p["format"]["duration"]
                )
                for p in probes
            ]
            if any(not math.isfinite(d) or d <= 0 for d in durations):
                raise ValueError("视频时长无效")
            listing = destination.parent / "sources.txt"
            # Names are local numbered links, independent of user filenames or CAS paths.
            listing.write_text(
                "".join(
                    f"file '{path.name}'\nduration {duration:.9f}\n"
                    for path, duration in zip(paths, durations, strict=True)
                )
            )
            _run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-y",
                    "-f",
                    "concat",
                    "-safe",
                    "1",
                    "-i",
                    str(listing),
                    "-map",
                    "0:v:0",
                    "-map",
                    "0:a:0?",
                    "-c",
                    "copy",
                    "-movflags",
                    "+faststart",
                    str(destination),
                ]
            )
            decoded = decode_video(destination)
            if (
                decoded.audio_streams == types.count("audio")
                and abs(decoded.duration_seconds - sum(durations)) <= 0.2
            ):
                return
        except (ValueError, RuntimeError):
            pass
    from stateweaver.studio.job_executor import JobExecutor

    JobExecutor._concat(paths, destination)
    # Put the movie header first for immediate HTTP playback after normalization.
    playable = destination.with_name("faststart.mp4")
    _run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-i",
            str(destination),
            "-map",
            "0",
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(playable),
        ]
    )
    playable.replace(destination)


def _preview_geometry(path: Path) -> tuple[int, int, float]:
    try:
        metadata = _probe(path)
        video = next(s for s in metadata["streams"] if s["codec_type"] == "video")
        width, height = video["width"], video["height"]
        duration = float(video.get("duration") or metadata["format"]["duration"])
        if (
            type(width) is int
            and type(height) is int
            and width > 0
            and height > 0
            and math.isfinite(duration)
            and duration > 0
        ):
            return width, height, duration
    except (KeyError, StopIteration, TypeError, ValueError):
        pass
    raise ValueError("连播预览的视频尺寸或时长无效")


class SetPreviewCache:
    def __init__(self, store: StudioStore, files: VerifiedBrowserFiles, root: Path) -> None:
        self.store, self.files, self.root = store, files, root.resolve()
        # Starts no threads until a user requests preparation. One worker bounds CPU/disk work.
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="set-preview")
        self.pending: dict[Path, Future[None]] = {}
        self.lock = Lock()
        self._geometry: OrderedDict[str, tuple[int, int, float]] = OrderedDict()

    def close(self) -> None:
        self.executor.shutdown(wait=True, cancel_futures=True)

    def _folder(self, project_id: str, set_id: str, fingerprint: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            raise StudioError("预览版本无效", 404)
        scope = hashlib.sha256(json.dumps([project_id, set_id]).encode()).hexdigest()
        return self.root / scope / fingerprint

    def _cache_record(
        self, folder: Path, project_id: str, set_id: str
    ) -> tuple[Media, bool] | None:
        try:
            record = json.loads((folder / "ready.json").read_text())
        except FileNotFoundError:
            return None
        if not isinstance(record, dict) or not isinstance(payload := record.get("media"), dict):
            raise ValueError("Invalid Set preview receipt")
        source_digest = record.get("source_sha256")
        # Route identity and format suffice for historical byte serving. Status
        # supplies display geometry from the selected source or the verified MP4.
        media = Media.model_validate(
            {
                **payload,
                "url": self._url(project_id, set_id, folder.name),
                "width": None,
                "height": None,
                "duration": None,
                **({"mime_type": "video/mp4"} if source_digest is None else {}),
            }
        )
        if (
            media.id != f"set-preview-{folder.name}"
            or not media.mime_type.startswith("video/")
            or (source_digest is not None and source_digest != media.sha256)
        ):
            raise ValueError("Set preview receipt does not match its media")
        return media, source_digest is not None

    def _cache_file(self, folder: Path, media: Media, uses_original: bool) -> Path:
        if uses_original:
            return self.files.original(media.sha256, media.byte_size)
        return self.files.derivative(folder / "preview.mp4", media.sha256, media.byte_size)

    def _remember_geometry(self, digest: str, geometry: tuple[int, int, float]) -> None:
        self._geometry[digest] = geometry
        self._geometry.move_to_end(digest)
        if len(self._geometry) > 1024:
            self._geometry.popitem(last=False)

    def _stitched_media(self, folder: Path, media: Media) -> Media:
        path = self._cache_file(folder, media, False)
        geometry = self._geometry.get(media.sha256)
        if geometry is None:
            geometry = _preview_geometry(path)
            # Keep the memo tied to the verified bytes if the file changed during probing.
            self._cache_file(folder, media, False)
        self._remember_geometry(media.sha256, geometry)
        width, height, duration = geometry
        return media.model_copy(update={"width": width, "height": height, "duration": duration})

    def get(
        self, project_id: str, set_id: str, *, prepare: bool = False, retry: bool = False
    ) -> SetPreview:
        result, sources = preview_sources(self.store, project_id, set_id)
        if not result.fingerprint:
            return result
        folder = self._folder(project_id, set_id, result.fingerprint)
        with self.lock:
            self.pending = {
                key: future for key, future in self.pending.items() if not future.done()
            }
            if folder in self.pending:
                result.status = "preparing"
                return result
            record = None
            damaged = False
            try:
                record = self._cache_record(folder, project_id, set_id)
                if record:
                    media, uses_original = record
                    original = sources[0] if len(sources) == 1 else None
                    if uses_original != (original is not None) or (
                        original is not None
                        and (original.sha256, original.byte_size) != (media.sha256, media.byte_size)
                    ):
                        result.status = "failed"
                        result.message = "预览回执与当前选片不一致；各 Clip 原视频仍可单独查看。"
                        return result
                    if original is not None:
                        self.files.original(original.sha256, original.byte_size)
                        media = original.model_copy(update={"id": media.id, "url": media.url})
                    else:
                        media = self._stitched_media(folder, media)
                    result.status, result.media = "ready", media
                    return result
            except (OSError, ValueError):
                damaged = True
            if (damaged or (folder / "failed").exists()) and not (retry and prepare):
                result.status, result.message = (
                    "failed",
                    "预览校验或拼接失败，请重试；各 Clip 原视频仍可单独查看。",
                )
                return result
            if len(sources) == 1:
                source = sources[0]
                try:
                    self.files.original(source.sha256, source.byte_size)
                except (OSError, ValueError):
                    result.status, result.message = "failed", "已选视频暂不可读取，请稍后重试。"
                    return result
                media = source.model_copy(
                    update={
                        "id": f"set-preview-{result.fingerprint}",
                        "url": self._url(project_id, set_id, result.fingerprint),
                    }
                )
                self._publish(folder, media, source.sha256)
                result.status, result.media = "ready", media
            elif prepare:
                if len(self.pending) >= 32:
                    result.message = "预览处理繁忙，请稍后重试。"
                    return result
                folder.mkdir(parents=True, exist_ok=True)
                (folder / "failed").unlink(missing_ok=True)
                self.pending[folder] = self.executor.submit(
                    self._build,
                    project_id,
                    set_id,
                    result.fingerprint,
                    sources,
                    folder,
                    record[0] if record and not record[1] else None,
                )
                result.status = "preparing"
        return result

    @staticmethod
    def _url(project_id: str, set_id: str, fingerprint: str) -> str:
        return (
            f"/api/v1/studio/projects/{quote(project_id, safe='')}/sets/"
            f"{quote(set_id, safe='')}/preview/{fingerprint}.mp4"
        )

    def file(self, project_id: str, set_id: str, fingerprint: str) -> tuple[Media, Path]:
        self.store.require_project(project_id)
        folder = self._folder(project_id, set_id, fingerprint)
        try:
            record = self._cache_record(folder, project_id, set_id)
            if record is not None:
                return record[0], self._cache_file(folder, *record)
        except (OSError, ValueError) as error:
            raise OSError("Unable to verify browser Set preview") from error
        raise StudioError("这版预览已过期或尚未准备好，请重新打开片场连播。", 404)

    @staticmethod
    def _publish(folder: Path, media: Media, source_sha256: str | None = None) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", dir=folder, delete=False, suffix=".json"
        ) as temporary:
            json.dump(
                {"media": media.model_dump(mode="json"), "source_sha256": source_sha256}, temporary
            )
        Path(temporary.name).replace(folder / "ready.json")

    def _build(
        self,
        project_id: str,
        set_id: str,
        fingerprint: str,
        sources: list[Media],
        folder: Path,
        expected: Media | None,
    ) -> None:
        try:
            with tempfile.TemporaryDirectory(prefix=".build-", dir=folder) as temporary:
                directory = Path(temporary)
                paths = []
                for index, source in enumerate(sources):
                    original = self.files.original(source.sha256, source.byte_size)
                    path = directory / f"source-{index:04d}.mp4"
                    path.symlink_to(original.resolve())
                    paths.append(path)
                output = directory / "output.mp4"
                concat_preview(paths, output)
                geometry = _preview_geometry(output)
                width, height, duration = geometry
                with output.open("rb") as stream:
                    digest = hashlib.file_digest(stream, "sha256").hexdigest()
                media = Media(
                    id=f"set-preview-{fingerprint}",
                    sha256=digest,
                    mime_type="video/mp4",
                    byte_size=output.stat().st_size,
                    width=width,
                    height=height,
                    duration=duration,
                    url=self._url(project_id, set_id, fingerprint),
                )
                if expected and (media.sha256, media.byte_size) != (
                    expected.sha256,
                    expected.byte_size,
                ):
                    raise OSError("Rebuilt Set preview differs from its immutable receipt")
                output.replace(folder / "preview.mp4")
                self._publish(folder, media)
                with self.lock:
                    self._remember_geometry(media.sha256, geometry)
            self._prune(folder)
        except Exception:
            # Persist only a safe marker, so failures don't restart on every poll.
            (folder / "failed").touch()

    @staticmethod
    def _prune(current: Path) -> None:
        # Only disposable caches for this Set. Keep two versions and an hour's grace
        # for players already seeking an older selection; source media is untouched.
        versions = sorted(
            (
                p
                for p in current.parent.iterdir()
                if p.is_dir() and re.fullmatch(r"[0-9a-f]{64}", p.name)
            ),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for folder in versions[2:]:
            if folder != current and folder.stat().st_mtime < time.time() - 3600:
                shutil.rmtree(folder)
