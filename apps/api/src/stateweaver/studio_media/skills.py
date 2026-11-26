"""Verified provider prompt packages consumed by runtime adapters and directors."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from stateweaver.providers.base import ProviderContractError


@dataclass(frozen=True, slots=True)
class MediaSkill:
    name: str
    body: str
    sha256: str
    config: dict[str, Any]

    def text(self, key: str) -> str:
        value = self.config.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ProviderContractError("media skill is missing a required text recipe")
        return value


def load_media_skill(name: str, *, root: Path | None = None) -> MediaSkill:
    if name not in {
        "image25-v1",
        "h3-ref2va-set-v2",
        "video-self-review-v1",
    }:
        raise ProviderContractError("unknown media skill package")
    base = root or Path(__file__).resolve().parents[5] / "skills" / "studio-media"
    try:
        data = (base / name / "SKILL.md").read_bytes()
        manifest = json.loads((base / name / "manifest.json").read_text("utf-8"))
        body = data.decode("utf-8")
    except (OSError, UnicodeError, ValueError):
        raise ProviderContractError("media skill or manifest is missing or invalid") from None
    digest = hashlib.sha256(data).hexdigest()
    if (
        not isinstance(manifest, dict)
        or manifest.get("name") != name
        or manifest.get("sha256") != digest
    ):
        raise ProviderContractError("media skill hash does not match its manifest")
    if not body.startswith("---\n") or f"\nname: {name}\n" not in body:
        raise ProviderContractError("media skill has invalid identity metadata")
    parts = body.split("---", 2)
    if len(parts) != 3 or not parts[2].strip():
        raise ProviderContractError("media skill body is empty")
    matches = re.findall(r"```json\s*\n(.*?)\n```", parts[2], flags=re.DOTALL)
    if len(matches) != 1:
        raise ProviderContractError("media skill requires one runtime recipe block")
    try:
        config = json.loads(matches[0])
    except ValueError:
        raise ProviderContractError("media skill runtime recipe is invalid JSON") from None
    if not isinstance(config, dict) or not config:
        raise ProviderContractError("media skill runtime recipe is empty")
    skill = MediaSkill(name, parts[2].strip(), digest, config)
    required = {
        "image25-v1": "prompt_template",
        "h3-ref2va-set-v2": "continuation_prefix",
        "video-self-review-v1": "evidence_instruction",
    }
    skill.text(required[name])
    if name == "h3-ref2va-set-v2":
        skill.text("editing_continuation_prefix")
    return skill
