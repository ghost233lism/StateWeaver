"""Cached browser derivatives and verified, range-capable original media delivery.

These files are display-only. They never replace CAS sources, model inputs or
review evidence. A changed file must pass integrity verification again.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections import OrderedDict
from pathlib import Path
from threading import Lock

from PIL import Image, ImageOps

from stateweaver.storage.cas import ContentAddressedStore


class VerifiedBrowserFiles:
    """Share bounded, single-flight integrity checks across media responses."""

    def __init__(self, cas: ContentAddressedStore) -> None:
        self.cas = cas
        self._verified: OrderedDict[Path, tuple[str, tuple[int, ...]]] = OrderedDict()
        self._guard = Lock()
        self._locks = [Lock() for _ in range(32)]

    @staticmethod
    def _stamp(path: Path) -> tuple[int, ...]:
        value = path.stat()
        return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns

    def original(self, digest: str, byte_size: int | None = None) -> Path:
        return self._verify(self.cas.path_for(digest), digest, byte_size, cas_source=True)

    def derivative(self, path: Path, digest: str, byte_size: int) -> Path:
        return self._verify(path, digest, byte_size, cas_source=False)

    def _verify(self, path: Path, digest: str, byte_size: int | None, *, cas_source: bool) -> Path:
        # Simultaneous browser range requests must not rehash the same video.
        with self._locks[hash(path) % len(self._locks)]:
            stamp = self._stamp(path)
            if byte_size is not None and stamp[2] != byte_size:
                raise OSError("Browser media size differs from its receipt")
            proof = (digest, stamp)
            with self._guard:
                if self._verified.get(path) == proof:
                    self._verified.move_to_end(path)
                    return path
            if cas_source:
                with self.cas.open(digest):
                    pass
            else:
                with path.open("rb") as source:
                    if hashlib.file_digest(source, "sha256").hexdigest() != digest:
                        raise OSError("Browser media digest differs from its receipt")
            if self._stamp(path) != stamp:
                raise OSError("Media changed during integrity verification")
            with self._guard:
                self._verified[path] = proof
                self._verified.move_to_end(path)
                while len(self._verified) > 1024:
                    self._verified.popitem(last=False)
        return path


class BrowserMedia:
    def __init__(self, cas: ContentAddressedStore, cache: Path) -> None:
        self.cache = cache
        self.files = VerifiedBrowserFiles(cas)
        self._thumbnail_locks = [Lock() for _ in range(32)]

    def original(self, digest: str) -> Path:
        return self.files.original(digest)

    def _thumbnail_from_receipt(self, path: Path, receipt: Path) -> Path:
        record = json.loads(receipt.read_text())
        if (
            not isinstance(record, dict)
            or not isinstance(record.get("sha256"), str)
            or type(record.get("byte_size")) is not int
        ):
            raise OSError("Invalid browser thumbnail receipt")
        return self.files.derivative(path, record["sha256"], record["byte_size"])

    def thumbnail(self, digest: str, edge: int) -> Path:
        if edge not in {256, 512, 1024}:
            raise ValueError("Unsupported preview size")
        source = self.original(digest)
        path = self.cache / "image-v1" / digest[:2] / f"{digest}-{edge}.webp"
        receipt = path.with_suffix(".json")
        with self._thumbnail_locks[hash(path) % len(self._thumbnail_locks)]:
            try:
                return self._thumbnail_from_receipt(path, receipt)
            except (OSError, ValueError):
                # Older caches had no digest. Rebuild them from the verified original.
                pass
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix=".preview-", dir=path.parent) as temporary:
                staged = Path(temporary) / path.name
                with Image.open(source) as original:
                    original.draft("RGB", (edge, edge))
                    picture = ImageOps.exif_transpose(original)
                    picture.thumbnail((edge, edge), Image.Resampling.LANCZOS)
                    picture.convert("RGBA" if "A" in picture.getbands() else "RGB").save(
                        staged, "WEBP", quality=82, method=4
                    )
                with staged.open("rb") as output:
                    checksum = hashlib.file_digest(output, "sha256").hexdigest()
                    byte_size = os.fstat(output.fileno()).st_size
                staged_receipt = staged.with_suffix(".json")
                staged_receipt.write_text(json.dumps({"sha256": checksum, "byte_size": byte_size}))
                os.replace(staged, path)
                os.replace(staged_receipt, receipt)
            return self._thumbnail_from_receipt(path, receipt)
