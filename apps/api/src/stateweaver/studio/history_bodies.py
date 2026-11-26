"""Integrity-checked codepoint paging of immutable archives and live snapshots."""

from __future__ import annotations

import codecs
import hashlib
import os
import re
import tempfile
from bisect import bisect_right
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from threading import RLock

from sqlalchemy import select, text

from stateweaver.persistence.models import StudioBodyRow, StudioTextChunkRow, StudioTranscriptRow
from stateweaver.studio.contracts import BodyChunk
from stateweaver.studio.store import StudioError, StudioStore

_BLOCK = 65536
_CACHE_SIZE = 32
_LOCK = RLock()


@dataclass(frozen=True)
class _Index:
    identity: tuple[int, int, int, int, int]
    offsets: tuple[int, ...]
    positions: tuple[int, ...]
    block_hashes: tuple[bytes, ...]
    total: int


_INDEXES: OrderedDict[tuple[str, str], _Index] = OrderedDict()
_SNAPSHOTS: OrderedDict[tuple[str, str, str, int, str], str] = OrderedDict()


def _identity(path: Path) -> tuple[int, int, int, int, int]:
    try:
        stamp = path.stat()
    except OSError as exc:
        raise StudioError("历史正文文件不可用", 500) from exc
    return stamp.st_dev, stamp.st_ino, stamp.st_size, stamp.st_mtime_ns, stamp.st_ctime_ns


def _path(store: StudioStore, digest: str) -> Path:
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise StudioError("历史正文引用无效", 422)
    return store.body_root / digest[:2] / digest


def _index(path: Path, digest: str) -> _Index:
    identity = _identity(path)
    key = (str(path), digest)
    with _LOCK:
        cached = _INDEXES.get(key)
        if cached is not None and cached.identity == identity:
            _INDEXES.move_to_end(key)
            return cached
    decoder = codecs.getincrementaldecoder("utf-8")()
    checksum = hashlib.sha256()
    offsets, positions = [0], [0]
    block_hashes: list[bytes] = []
    total, position = 0, 0
    try:
        with path.open("rb") as stream:
            while block := stream.read(_BLOCK):
                checksum.update(block)
                block_hashes.append(hashlib.sha256(block).digest())
                position += len(block)
                total += len(decoder.decode(block))
                offsets.append(total)
                positions.append(position - len(decoder.getstate()[0]))
            total += len(decoder.decode(b"", final=True))
    except (OSError, UnicodeError) as exc:
        raise StudioError("历史正文完整性校验失败", 500) from exc
    if checksum.hexdigest() != digest or _identity(path) != identity:
        raise StudioError("历史正文完整性校验失败", 500)
    result = _Index(identity, tuple(offsets), tuple(positions), tuple(block_hashes), total)
    with _LOCK:
        _INDEXES[key] = result
        _INDEXES.move_to_end(key)
        while len(_INDEXES) > _CACHE_SIZE:
            _INDEXES.popitem(last=False)
    return result


def _read(path: Path, index: _Index, offset: int, limit: int) -> str:
    checkpoint = max(0, bisect_right(index.offsets, offset) - 1)
    skip, remaining = offset - index.offsets[checkpoint], limit
    decoder = codecs.getincrementaldecoder("utf-8")()
    pieces: list[str] = []
    try:
        with path.open("rb") as stream:
            position = index.positions[checkpoint]
            block_number, prefix = divmod(position, _BLOCK)
            stream.seek(block_number * _BLOCK)
            while remaining and (block := stream.read(_BLOCK)):
                if (
                    block_number >= len(index.block_hashes)
                    or hashlib.sha256(block).digest() != index.block_hashes[block_number]
                ):
                    raise StudioError("历史正文完整性校验失败", 500)
                block_number += 1
                value = decoder.decode(block[prefix:])
                prefix = 0
                if skip >= len(value):
                    skip -= len(value)
                    continue
                value = value[skip : skip + remaining]
                skip = 0
                remaining -= len(value)
                pieces.append(value)
        if _identity(path) != index.identity:
            raise StudioError("历史正文在读取时发生变化", 500)
    except (OSError, UnicodeError) as exc:
        raise StudioError("历史正文完整性校验失败", 500) from exc
    return "".join(pieces)


def read_archive(
    store: StudioStore, project_id: str, digest: str, offset: int = 0, limit: int = _BLOCK
) -> BodyChunk:
    """Return canonical JSON bytes decoded as text, without reparsing the whole body."""
    if offset < 0:
        raise StudioError("正文偏移无效", 422)
    path = _path(store, digest)
    with store.database.session() as session:
        if session.get(StudioBodyRow, (project_id, digest)) is None:
            raise StudioError("历史正文不存在", 404)
    index = _index(path, digest)
    limit = min(max(1, limit), _BLOCK)
    value = _read(path, index, offset, limit) if offset < index.total else ""
    end = min(index.total, offset + len(value))
    return BodyChunk(
        text=value,
        offset=offset,
        next_offset=end if end < index.total else None,
        total=index.total,
        body_ref=digest,
    )


def _seal_live(store: StudioStore, project_id: str, item_id: str) -> str:
    """Materialize one requested message version, streaming rows into a CAS file."""
    with store.database.session() as session:
        session.execute(text("BEGIN"))
        row = session.scalar(
            select(StudioTranscriptRow).where(
                StudioTranscriptRow.project_id == project_id,
                StudioTranscriptRow.item_id == item_id,
            )
        )
        if row is None:
            raise StudioError("历史消息不存在", 404)
        if row.kind != "message":
            raise StudioError("此记录不是流式文字消息", 422)
        owner_actor_id = row.actor_id
        base_ref = str(row.payload.get("body_ref") or "")
        key = (str(store.body_root), project_id, item_id, row.version, base_ref)
        with _LOCK:
            cached = _SNAPSHOTS.get(key)
            if cached is not None:
                _SNAPSHOTS.move_to_end(key)
                return cached
        if base_ref:
            if session.get(StudioBodyRow, (project_id, base_ref)) is None:
                raise StudioError("历史正文不存在", 404)
            base_text = str(store._read_body(base_ref).get("text", ""))
        else:
            base_text = str(row.payload.get("text", ""))
        descriptor, name = tempfile.mkstemp(prefix=".live-", dir=store.body_root)
        checksum, size = hashlib.sha256(), 0
        try:
            with os.fdopen(descriptor, "wb") as output:
                value = base_text.encode()
                output.write(value)
                checksum.update(value)
                size += len(value)
                chunks = session.scalars(
                    select(StudioTextChunkRow.text)
                    .where(
                        StudioTextChunkRow.project_id == project_id,
                        StudioTextChunkRow.item_id == item_id,
                    )
                    .order_by(StudioTextChunkRow.ordinal)
                ).yield_per(64)
                for chunk in chunks:
                    value = chunk.encode()
                    output.write(value)
                    checksum.update(value)
                    size += len(value)
                output.flush()
                os.fsync(output.fileno())
            digest = checksum.hexdigest()
            destination = _path(store, digest)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not destination.exists():
                os.replace(name, destination)
        finally:
            if os.path.exists(name):
                os.unlink(name)
    # The read snapshot above remains consistent even if more deltas now arrive.
    # Only registration takes the writer lock; file creation never holds it.
    with store.database.write_lock, store.database.session() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        body = session.get(StudioBodyRow, (project_id, digest))
        if body is None:
            session.add(
                StudioBodyRow(
                    project_id=project_id,
                    sha256=digest,
                    size=size,
                    owner_actor_ids=[owner_actor_id],
                )
            )
        elif owner_actor_id not in body.owner_actor_ids:
            body.owner_actor_ids = [*body.owner_actor_ids, owner_actor_id]
    with _LOCK:
        _SNAPSHOTS[key] = digest
        while len(_SNAPSHOTS) > _CACHE_SIZE * 2:
            _SNAPSHOTS.popitem(last=False)
    return digest


def read_live(
    store: StudioStore, project_id: str, item_id: str, offset: int = 0, limit: int = _BLOCK
) -> BodyChunk:
    """The returned ref pins paging to a complete immutable version of this text."""
    digest = (
        item_id.removeprefix("snapshot:")
        if item_id.startswith("snapshot:")
        else _seal_live(store, project_id, item_id)
    )
    page = read_archive(store, project_id, digest, offset, limit)
    return page.model_copy(
        update={
            "content_type": "text/plain",
            "body_ref": "live:snapshot:" + digest,
        }
    )
