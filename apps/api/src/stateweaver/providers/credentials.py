"""Load explicit service credentials from dotenv-style files and the environment."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path

_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def load_secret_config(
    paths: Sequence[Path], environ: Mapping[str, str] | None = None, *, required: bool = False
) -> dict[str, str]:
    values: dict[str, str] = {}
    for path in paths:
        if not path.is_file():
            if required:
                raise FileNotFoundError("Required credential file is not a regular file")
            continue
        for line in path.read_text("utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            key = key.strip()
            if _KEY_RE.match(key):
                values[key] = value.strip().strip('"').strip("'")
    values.update(dict(os.environ if environ is None else environ))
    return values
