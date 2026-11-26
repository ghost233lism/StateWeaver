"""One existing backup's complete baseline and compressed, framed byte deltas."""

from __future__ import annotations

import gzip
import hashlib
import os
import shutil
import struct
import tempfile
import time
from collections.abc import Callable
from io import RawIOBase
from pathlib import Path
from typing import Any
from uuid import uuid4

MAGIC = b"AVDELTA1"
BLOCK = 64 * 1024
READ_BATCH = 4 * 1024 * 1024
LENGTH = struct.Struct(">Q")
ENTRY = struct.Struct(">QI")
END = (1 << 64) - 1
HEADER_SIZE = len(MAGIC) + 32


def _temporary(parent: Path, prefix: str) -> Path:
    descriptor, name = tempfile.mkstemp(prefix=prefix, dir=parent)
    os.close(descriptor)
    return Path(name)


def _digest(path: Path) -> bytes:
    value = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(READ_BATCH):
            value.update(chunk)
    return value.digest()


def _atomic_copy(source: Path, destination: Path) -> None:
    pending = destination.with_name(destination.name + f".{uuid4().hex}.pending")
    try:
        with source.open("rb") as reader, pending.open("wb") as writer:
            shutil.copyfileobj(reader, writer, length=1024 * 1024)
            writer.flush()
            os.fsync(writer.fileno())
        pending.replace(destination)
    finally:
        pending.unlink(missing_ok=True)


class _Frame(RawIOBase):
    """Never let gzip consume the following length-framed record."""

    def __init__(self, source: Any, remaining: int) -> None:
        self.source, self.remaining = source, remaining

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        size = self.remaining if size < 0 else min(size, self.remaining)
        value: bytes = self.source.read(size)
        self.remaining -= len(value)
        return value


def _exact(source: Any, size: int) -> bytes:
    value: bytes = source.read(size)
    if len(value) != size:
        raise ValueError("Incomplete recovery record")
    return value


class IncrementalRecovery:
    """One owner serializes publishing without comparing remote copies."""

    def __init__(
        self,
        recovery: Path,
        *,
        merge_bytes: int | None = None,
        merge_seconds: float = 1800,
        valid: Callable[[Path], bool] = lambda _: True,
    ) -> None:
        self.recovery = recovery
        self.delta = recovery.with_name(recovery.name + ".delta")
        self.merge_bytes, self.merge_seconds = merge_bytes, merge_seconds
        self.baseline_bytes = 0
        self.valid = valid
        self.previous: Path | None = None
        self.previous_digest = b""
        self.offset = HEADER_SIZE
        self.merged_at = 0.0
        self.force_baseline = False
        self.sequence = 0

    def _initialize(self, stage: Path, sequence: int) -> dict[str, Any] | None:
        if not self.recovery.exists():
            if self.previous is not None:
                raise FileNotFoundError("Provider recovery baseline is missing")
            return self._baseline(stage, sequence)
        if not self.valid(stage):
            raise ValueError("Initial provider snapshot is incomplete")
        previous = _temporary(stage.parent, ".recovery-previous-")
        try:
            restored = self.restore(previous, self.valid)
            digest = bytes.fromhex(restored["image_sha256"])
            baseline_bytes = self.recovery.stat().st_size
            reset_header = not self.delta.exists() or restored["stale_delta_ignored"]
            if reset_header:
                self._reset_header(stage.parent, digest)
                self.offset = HEADER_SIZE
            else:
                self.offset = restored["valid_delta_bytes"]
            if self.previous is None or reset_header:
                self.merged_at = time.monotonic()
            self.previous, self.previous_digest = previous, digest
            self.baseline_bytes = baseline_bytes
            self.sequence = restored["event_sequence"] or 0
            self.force_baseline = False
        except BaseException:
            previous.unlink(missing_ok=True)
            raise
        return None

    def _reset_header(self, parent: Path, digest: bytes) -> None:
        header = _temporary(parent, ".recovery-header-")
        try:
            header.write_bytes(MAGIC + digest)
            _atomic_copy(header, self.delta)
        finally:
            header.unlink(missing_ok=True)

    def _baseline(self, stage: Path, sequence: int) -> dict[str, Any]:
        self.force_baseline = True
        started = time.monotonic()
        if not self.valid(stage):
            raise ValueError("Provider baseline snapshot is incomplete")
        validated_at = time.monotonic()
        digest = self.previous_digest if stage == self.previous else _digest(stage)
        publishing_at = time.monotonic()
        self.recovery.parent.mkdir(parents=True, exist_ok=True)
        # The current image is already the last complete delta target. Thus an
        # interrupted head reset is provably a merge, not arbitrary mismatch.
        _atomic_copy(stage, self.recovery)
        self._reset_header(stage.parent, digest)
        self.offset, self.merged_at = HEADER_SIZE, time.monotonic()
        self.force_baseline = False
        self.sequence = sequence
        self.baseline_bytes = stage.stat().st_size
        self._retain(stage, digest)
        return {
            "backup_mode": "baseline",
            "event_sequence": sequence,
            "baseline_validation_seconds": validated_at - started,
            "local_delta_seconds": publishing_at - validated_at,
            "destination_publish_seconds": time.monotonic() - publishing_at,
            "published_bytes": stage.stat().st_size + HEADER_SIZE,
        }

    def _retain(self, stage: Path, digest: bytes) -> None:
        previous, self.previous = self.previous, stage
        self.previous_digest = digest
        if previous is not None and previous != stage:
            previous.unlink(missing_ok=True)

    def _merge_limit(self) -> int:
        if self.merge_bytes is not None:
            return self.merge_bytes
        return max(512 * 1024 * 1024, self.baseline_bytes)

    def publish(self, stage: Path, sequence: int) -> dict[str, Any]:
        started = time.monotonic()
        # The previous local image can disappear during temporary file cleanup.
        if self.previous is None or not self.previous.exists():
            initialized = self._initialize(stage, sequence)
            if initialized is not None:
                return initialized
        if self.force_baseline:
            # Complete an interrupted merge before extending its former chain.
            assert self.previous is not None
            self._baseline(self.previous, self.sequence)
        assert self.previous is not None
        comparing_at = time.monotonic()
        initialization_seconds = comparing_at - started
        patch = _temporary(stage.parent, ".recovery-patch-")
        digest = hashlib.sha256()
        try:
            with (
                stage.open("rb") as current,
                self.previous.open("rb") as previous,
                patch.open("wb") as packed,
                gzip.GzipFile(fileobj=packed, mode="wb", compresslevel=1, mtime=0) as output,
            ):
                output.write(self.previous_digest + LENGTH.pack(stage.stat().st_size))
                offset = 0
                while batch := current.read(READ_BATCH):
                    digest.update(batch)
                    old = previous.read(len(batch))
                    if batch != old:
                        for position in range(0, len(batch), BLOCK):
                            chunk = batch[position : position + BLOCK]
                            if chunk != old[position : position + BLOCK]:
                                output.write(ENTRY.pack(offset + position, len(chunk)) + chunk)
                    offset += len(batch)
                output.write(ENTRY.pack(END, 0))
                output.write(digest.digest() + LENGTH.pack(sequence))
            compared_at = time.monotonic()
            local_delta_seconds = compared_at - comparing_at
            if digest.digest() == self.previous_digest:
                self._retain(stage, digest.digest())
                self.sequence = sequence
                if time.monotonic() - self.merged_at >= self.merge_seconds:
                    merged = self._baseline(stage, sequence)
                    merged["local_delta_seconds"] += local_delta_seconds
                    merged["initialization_seconds"] = initialization_seconds
                    return merged
                return {
                    "backup_mode": "unchanged",
                    "event_sequence": sequence,
                    "initialization_seconds": initialization_seconds,
                    "local_delta_seconds": local_delta_seconds,
                    "destination_publish_seconds": 0,
                    "published_bytes": 0,
                }
            length = patch.stat().st_size
            with self.delta.open("r+b") as output, patch.open("rb") as input_file:
                # Discard only an unacknowledged previous tail after a failed
                # write/fsync. The retained local stage still matches this offset.
                output.seek(self.offset)
                output.truncate()
                output.write(LENGTH.pack(length))
                shutil.copyfileobj(input_file, output, length=1024 * 1024)
                output.flush()
                os.fsync(output.fileno())
            publish_seconds = time.monotonic() - compared_at
            self.offset += LENGTH.size + length
            self._retain(stage, digest.digest())
            self.sequence = sequence
            if (
                self.offset - HEADER_SIZE >= self._merge_limit()
                or time.monotonic() - self.merged_at >= self.merge_seconds
            ):
                merged = self._baseline(stage, sequence)
                merged["local_delta_seconds"] += local_delta_seconds
                merged["destination_publish_seconds"] += publish_seconds
                merged["published_bytes"] += LENGTH.size + length
                merged["initialization_seconds"] = initialization_seconds
                return merged
            return {
                "backup_mode": "delta",
                "event_sequence": sequence,
                "initialization_seconds": initialization_seconds,
                "local_delta_seconds": local_delta_seconds,
                "destination_publish_seconds": publish_seconds,
                "published_bytes": LENGTH.size + length,
            }
        finally:
            patch.unlink(missing_ok=True)

    def restore(self, local: Path, valid: Callable[[Path], bool]) -> dict[str, Any]:
        """Read the recovery store once, apply complete frames locally, then validate the result."""
        local.parent.mkdir(parents=True, exist_ok=True)
        stage = _temporary(local.parent, ".recovery-restore-")
        applied, ignored_tail, stale_delta, sequence = 0, False, False, None
        valid_bytes = HEADER_SIZE
        try:
            _atomic_copy(self.recovery, stage)
            image_digest = digest = _digest(stage)
            if self.delta.exists():
                with self.delta.open("rb") as records, stage.open("r+b") as target:
                    file_size = os.fstat(records.fileno()).st_size
                    header = _exact(records, HEADER_SIZE)
                    if header[: len(MAGIC)] != MAGIC:
                        raise ValueError("Unsupported recovery delta format")
                    digest = header[len(MAGIC) :]
                    stale_delta = digest != image_digest
                    while records.tell() < file_size:
                        prefix = records.read(LENGTH.size)
                        if len(prefix) != LENGTH.size:
                            ignored_tail = True
                            break
                        length = LENGTH.unpack(prefix)[0]
                        if length > file_size - records.tell():
                            ignored_tail = True
                            break
                        frame = _Frame(records, length)
                        with gzip.GzipFile(fileobj=frame, mode="rb") as patch:
                            if _exact(patch, 32) != digest:
                                raise ValueError("Recovery delta chain does not match baseline")
                            size = LENGTH.unpack(_exact(patch, LENGTH.size))[0]
                            while True:
                                offset, count = ENTRY.unpack(_exact(patch, ENTRY.size))
                                if offset == END and count == 0:
                                    break
                                if (
                                    count == 0
                                    or count > BLOCK
                                    or offset % BLOCK
                                    or offset + count > size
                                ):
                                    raise ValueError("Invalid recovery block")
                                value = _exact(patch, count)
                                if not stale_delta:
                                    target.seek(offset)
                                    target.write(value)
                            digest = _exact(patch, 32)
                            sequence = LENGTH.unpack(_exact(patch, LENGTH.size))[0]
                            if patch.read(1) or frame.remaining:
                                raise ValueError("Unexpected recovery frame contents")
                        valid_bytes = records.tell()
                        if not stale_delta:
                            target.truncate(size)
                            applied += 1
                # Every frame is compressed with CRC protection; verify the final
                # image once rather than rehashing the complete DB per record.
                if stale_delta and image_digest != digest:
                    raise ValueError("Baseline mismatch is not a proven complete merge")
                if not stale_delta and _digest(stage) != digest:
                    raise ValueError("Recovered database bytes do not match final delta")
            if not valid(stage):
                raise ValueError("Recovered provider database is incomplete")
            stage.replace(local)
            return {
                "applied_deltas": applied,
                "ignored_incomplete_tail": ignored_tail,
                "stale_delta_ignored": stale_delta,
                "event_sequence": sequence,
                "valid_delta_bytes": valid_bytes,
                "image_sha256": digest.hex(),
            }
        finally:
            stage.unlink(missing_ok=True)

    def close(self) -> None:
        if self.previous is not None:
            self.previous.unlink(missing_ok=True)
            self.previous = None
