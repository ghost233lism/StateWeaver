from __future__ import annotations

import fcntl
import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4


@dataclass(frozen=True, slots=True)
class CASAsset:
    sha256: str
    byte_size: int
    path: Path


class CASIntegrityError(OSError):
    """A stored or proposed blob disagrees with its exact content address."""


class ContentAddressedStore:
    """Immutable local content-addressed storage with atomic publication."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, digest: str) -> Path:
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError("invalid sha256 digest")
        return self.root / "sha256" / digest[:2] / digest[2:4] / digest

    def put_bytes(self, data: bytes) -> CASAsset:
        return self.put_stream(memoryview(data))

    def put_file(self, source: Path) -> CASAsset:
        with source.open("rb") as stream:
            return self.put_stream(stream)

    def put_stream(self, source: BinaryIO | memoryview) -> CASAsset:
        return self._put_stream(source)

    def restore_bytes(self, data: bytes, *, sha256: str, byte_size: int) -> CASAsset:
        """Repair an existing binding only from bytes that match its pinned identity."""
        return self._put_stream(memoryview(data), expected=(sha256, byte_size))

    def _put_stream(
        self, source: BinaryIO | memoryview, *, expected: tuple[str, int] | None = None
    ) -> CASAsset:
        digest = hashlib.sha256()
        size = 0
        staging_root = self.root / ".staging"
        staging_root.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(prefix="cas-", dir=staging_root)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "wb") as destination:
                if isinstance(source, memoryview):
                    destination.write(source)
                    digest.update(source)
                    size = len(source)
                else:
                    while chunk := source.read(1024 * 1024):
                        destination.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
                destination.flush()
                os.fsync(destination.fileno())
            checksum = digest.hexdigest()
            if expected is not None and (checksum, size) != expected:
                raise CASIntegrityError("Downloaded bytes do not match the saved CAS hash and size")
            final_path = self.path_for(checksum)
            final_path.parent.mkdir(parents=True, exist_ok=True)
            if expected is not None:
                self._publish_repair(temporary, final_path, checksum, size)
            elif not final_path.exists():
                os.replace(temporary, final_path)
            return CASAsset(sha256=checksum, byte_size=size, path=final_path)
        finally:
            temporary.unlink(missing_ok=True)

    def _publish_repair(self, staged: Path, target: Path, digest: str, size: int) -> None:
        with (self.root / ".restore.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                with self.open(digest) as existing:
                    if existing.seek(0, 2) != size:
                        raise CASIntegrityError("CAS blob size differs from its pinned size")
                    return
            except FileNotFoundError:
                pass
            except CASIntegrityError:
                evidence = self.root / ".corrupt" / digest
                evidence.mkdir(parents=True, exist_ok=True)
                os.link(target, evidence / f"{uuid4().hex}.blob")
                self._sync_directory(evidence)
                self._sync_directory(evidence.parent)
                self._sync_directory(self.root)
            os.replace(staged, target)
            self._sync_directory(target.parent)

    @staticmethod
    def _sync_directory(path: Path) -> None:
        directory = os.open(path, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def read_bytes(self, digest: str) -> bytes:
        data = self.path_for(digest).read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise CASIntegrityError(f"CAS integrity error for {digest}")
        return data

    def open(self, digest: str) -> BinaryIO:
        path = self.path_for(digest)
        stream = path.open("rb")
        try:
            if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
                raise CASIntegrityError(f"CAS integrity error for {digest}")
            stream.seek(0)
        except BaseException:
            stream.close()
            raise
        return stream
