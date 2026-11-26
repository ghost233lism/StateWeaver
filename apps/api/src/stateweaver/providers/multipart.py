from __future__ import annotations

from collections.abc import Mapping, Sequence

from .base import AssetInput


def encode_multipart(
    fields: Mapping[str, str],
    files: Sequence[tuple[str, AssetInput]],
    *,
    boundary: str = "agentvideo-boundary",
) -> tuple[str, bytes]:
    chunks: list[bytes] = []
    marker = boundary.encode()
    for name, value in fields.items():
        chunks += [
            b"--" + marker + b"\r\n",
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
            value.encode(),
            b"\r\n",
        ]
    for field, item in files:
        safe = item.name.replace('"', "_").replace("\r", "_").replace("\n", "_")
        chunks += [
            b"--" + marker + b"\r\n",
            f'Content-Disposition: form-data; name="{field}"; filename="{safe}"\r\n'.encode(),
            f"Content-Type: {item.media_type}\r\n\r\n".encode(),
            item.content,
            b"\r\n",
        ]
    chunks.append(b"--" + marker + b"--\r\n")
    return f"multipart/form-data; boundary={boundary}", b"".join(chunks)
