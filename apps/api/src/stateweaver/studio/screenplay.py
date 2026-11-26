"""New-project story authoring; canonical readable text also feeds director context."""

from __future__ import annotations

import re
from typing import Final

from .contracts import ScreenplayDraft
from .store import StudioError

STORY_FORMAT: Final = "story-v1"
_FACT_LABELS = {
    "identity": "Character identity",
    "event": "Key event",
    "ending": "Ending",
    "dialogue": "Required dialogue",
    "detail": "Story detail",
}


def _prose(text: str) -> str:
    """Structured fields are plain prose; only this renderer creates Markdown layout."""
    return "\n\n".join(
        re.sub(r"([\\`*_{}\[\]<>#+.!|~-])", r"\\\1", line.strip())
        for line in text.splitlines()
        if line.strip()
    )


def render_screenplay(draft: ScreenplayDraft) -> str:
    manuscript = draft.manuscript
    for paragraph in manuscript.paragraphs:
        if re.match(
            (
                "(?i)^\\[?(?:(?:set|clip)\\s*(?:\\d+|[ivx]+\\b)|(?:片场|分镜|镜头)\\s*[一二三"
                "四五六七八九十百\\d]|第\\s*[一二三四五六七八九十百\\d]+\\s*(?:片场|分镜|镜头))"
            ),
            paragraph.title,
        ) or re.search(r"\d{1,2}:\d{2}\s*[-–—~至]\s*\d{1,2}:\d{2}", paragraph.title):
            raise StudioError("剧情段落标题不能是 Set/Clip/镜头计划或逐段时间码；请按情节命名")

    estimate = manuscript.duration_estimate
    duration = f"{estimate.minimum_seconds:g}"
    if estimate.maximum_seconds != estimate.minimum_seconds:
        duration += f"–{estimate.maximum_seconds:g}"
    parts = [
        f"# {_prose(manuscript.title)}",
        "## Film overview",
        f"**Logline**: {_prose(manuscript.logline)}",
        f"**Style**: {_prose(manuscript.style)}",
        f"**Estimated overall duration**: {duration} seconds (writer estimate)",
        f"**Pacing rationale**: {_prose(estimate.basis)}",
        "## Characters",
    ]
    parts.extend(
        f"**{_prose(character.name)}**　{_prose(character.description)}"
        for character in manuscript.characters
    )
    if not manuscript.characters:
        parts.append("No separately listed characters.")
    parts.append("## Story")
    for paragraph in manuscript.paragraphs:
        parts.append(f"### {_prose(paragraph.title)}")
        for beat in paragraph.beats:
            label = {"action": "Action", "dialogue": "Dialogue", "sound": "Sound"}[beat.kind]
            if beat.speaker:
                label += f" · {_prose(beat.speaker)}"
            parts.append(f"**{label}**　{_prose(beat.text)}")
    parts.append("## Key facts")
    parts.extend(f"- **{_FACT_LABELS[fact.kind]}**　{_prose(fact.text)}" for fact in draft.facts)
    if not draft.facts:
        parts.append(
            "No separately listed key facts; the complete confirmed "
            "screenplay must still be preserved."
        )
    return "\n\n".join(parts)
