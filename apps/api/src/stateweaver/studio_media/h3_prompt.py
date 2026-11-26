"""The official H3 Ref2VA six-section wire format and image-label checks."""

from __future__ import annotations

import re

from stateweaver.providers.base import ProviderContractError

from .skills import load_media_skill

SET_FORMAT = "h3-ref2va-six-section-set-v1"
SECTIONS = (
    "subject_definitions",
    "summary",
    "retention_analysis",
    "detailed_description",
    "overall_soundscape",
    "non_diegetic_music",
)
_HEADERS = re.compile(r"^(" + "|".join(SECTIONS) + r"):[ \t]*", re.MULTILINE)
_FIELDS = re.compile(r"^([a-z_]+):", re.MULTILINE)
_LABEL = re.compile(r"<(Subject|Picture|Video|Audio) ([0-9]+)>")
_DEFINITION = re.compile(r"^<(Subject|Picture|Video|Audio) ([1-9][0-9]*)>", re.MULTILINE)
_MARKERS = r"fully_preserved|partially_preserved|attribute_transfer|weak_reference"


def validate_prompt(
    prompt: str,
    image_count: int,
    *,
    continuation: bool = False,
    video_count: int = 0,
    audio_count: int = 0,
    video_continuation: bool = False,
    video_editing: bool = False,
) -> dict[str, str]:
    """Check syntax and real input slots; semantic/quality decisions remain with the agent."""
    if not 0 <= image_count <= 9 or not 0 <= video_count <= 3 or not 0 <= audio_count <= 3:
        raise ProviderContractError(
            "H3 Ref2VA accepts 1–9 images (or no images with video), at most 3 videos "
            "and 3 audio inputs"
        )
    if not image_count + video_count or image_count + video_count + audio_count > 12:
        raise ProviderContractError("H3 requires visual references and at most 12 mixed inputs")
    matches = list(_HEADERS.finditer(prompt))
    if (
        tuple(m.group(1) for m in matches) != SECTIONS
        or tuple(_FIELDS.findall(prompt)) != SECTIONS
        or prompt[: matches[0].start()].strip()
        or "```" in prompt
    ):
        raise ProviderContractError(
            "H3 Ref2VA requires exactly six sections in order: " + ", ".join(SECTIONS)
        )
    sections = {
        m.group(1): prompt[
            m.end() : matches[i + 1].start() if i + 1 < len(matches) else None
        ].strip()
        for i, m in enumerate(matches)
    }
    if any(not body for body in sections.values()):
        raise ProviderContractError("H3 prompt sections must be nonempty; use N/A for no music")
    labels = set(_LABEL.findall(prompt))
    if any(int(index) < 1 or str(int(index)) != index for _, index in labels):
        raise ProviderContractError("H3 reference labels use positive unpadded indices")
    for media_kind, count in (
        ("Picture", image_count),
        ("Video", video_count),
        ("Audio", audio_count),
    ):
        supplied = {int(index) for kind, index in labels if kind == media_kind}
        if supplied != set(range(1, count + 1)):
            expected = ", ".join(f"<{media_kind} {i}>" for i in range(1, count + 1)) or "none"
            raise ProviderContractError(
                f"H3 {media_kind} labels must cover the exact supplied input order; "
                f"expected {expected}; found indices {sorted(supplied)}. "
                "Use angle brackets and a space, e.g. <Picture 1>, not plain Picture 1."
            )
    definitions = _DEFINITION.findall(sections["subject_definitions"])
    if not definitions or len(definitions) != len(set(definitions)):
        raise ProviderContractError("H3 subject_definitions must contain unique labelled entries")
    subjects = {(kind, index) for kind, index in labels if kind == "Subject"}
    if not subjects or not subjects <= set(definitions):
        raise ProviderContractError("Every H3 Subject label must have a subject_definitions entry")
    if not {(k, i) for k, i in labels if k in {"Video", "Audio"}} <= set(definitions):
        raise ProviderContractError("Every Video/Audio input needs a separate role definition")
    retained = set(_DEFINITION.findall(sections["retention_analysis"]))
    required = set(definitions)
    # A supplied picture can have its own supplementary retention entry even
    # when subject_definitions names only the depicted Subjects. Require the
    # defined roles, but do not misreport extra valid picture analysis as missing.
    allowed = required | {("Picture", str(index)) for index in range(1, image_count + 1)}
    missing, unexpected = required - retained, retained - allowed
    if missing or unexpected:

        def render(items: set[tuple[str, str]]) -> str:
            return (
                ", ".join(
                    f"<{kind} {index}>"
                    for kind, index in sorted(items, key=lambda item: (item[0], int(item[1])))
                )
                or "none"
            )

        raise ProviderContractError(
            "H3 retention_analysis must cover each separately defined label; "
            f"missing {render(missing)}; unexpected {render(unexpected)}"
        )
    for line in sections["retention_analysis"].splitlines():
        definition = _DEFINITION.match(line)
        markers = (
            "reference|weak_reference" if definition and definition[1] == "Audio" else _MARKERS
        )
        if definition and not re.search(r":\s*(?:" + markers + r")\s*-", line):
            raise ProviderContractError(
                "H3 retention_analysis requires an official relationship marker"
            )
    summary = sections["summary"]
    prefix = re.match(r"\[([^\]]+)\]", summary)
    if prefix is None:
        raise ProviderContractError("H3 summary requires a bracketed task-type prefix")
    task_types = prefix.group(1).split(" + ")
    allowed = {"reference generation", "keyframe completion"}
    if video_count:
        allowed.update({"video continuation", "video editing"})
    if audio_count or video_count:
        allowed.add("audio reference")
    if len(task_types) != len(set(task_types)) or not set(task_types) <= allowed:
        raise ProviderContractError(
            "H3 task types must describe the actual reference inputs; audio reuse is not enabled"
        )
    if audio_count and "audio reference" not in task_types:
        raise ProviderContractError("Audio inputs require the audio reference task type")
    detail = sections["detailed_description"]
    shots = re.findall(r"\[Shot ([0-9]+)\]", detail)
    if not shots or shots != [str(i) for i in range(1, len(shots) + 1)]:
        raise ProviderContractError(
            "Clip 内镜头须按 [Shot 1], [Shot 2] 连续编号，保持同一片场时空连续"
        )
    if video_editing and (
        video_count != 1
        or video_continuation
        or "video editing" not in task_types
        or "video continuation" in task_types
        or "<Video 1>" not in detail
    ):
        raise ProviderContractError(
            "Editing requires the full current candidate as Video 1, "
            "video editing in summary, and no predecessor tail video"
        )
    if not video_editing and (
        "video editing" in task_types
        or video_count != int(video_continuation)
        or continuation
        and not video_continuation
    ):
        raise ProviderContractError(
            "Set continuation requires the accepted previous tail as Video 1; "
            "video editing/other video inputs are not allowed"
        )
    if continuation and (
        ("Picture", "1") not in definitions
        or "keyframe completion" not in task_types
        or "<Picture 1>" not in detail
    ):
        raise ProviderContractError(
            "Continuation requires Picture 1 as the separately defined actual end-frame anchor, "
            "keyframe completion in summary, and Picture 1 in detailed_description"
        )
    if video_continuation and (
        ("Video", "1") not in definitions
        or "video continuation" not in task_types
        or "<Video 1>" not in detail
    ):
        raise ProviderContractError(
            "Video continuation requires Video 1 as the actual preceding tail in all sections"
        )
    return sections


def prepare_prompt(
    prompt: str,
    image_count: int,
    *,
    continuation: bool = False,
    video_count: int = 0,
    audio_count: int = 0,
    video_continuation: bool = False,
    video_editing: bool = False,
) -> str:
    sections = validate_prompt(
        prompt,
        image_count,
        continuation=continuation,
        video_count=video_count,
        audio_count=audio_count,
        video_continuation=video_continuation,
        video_editing=video_editing,
    )
    if continuation:
        instruction = load_media_skill("h3-ref2va-set-v2").text(
            "editing_continuation_prefix" if video_editing else "continuation_prefix"
        )
        if instruction not in sections["detailed_description"]:
            sections["detailed_description"] = sections["detailed_description"].replace(
                "[Shot 1]", "[Shot 1] " + instruction, 1
            )
    result = "\n\n".join(f"{name}:\n{sections[name]}" for name in SECTIONS)
    validate_prompt(
        result,
        image_count,
        continuation=continuation,
        video_count=video_count,
        audio_count=audio_count,
        video_continuation=video_continuation,
        video_editing=video_editing,
    )
    return result
