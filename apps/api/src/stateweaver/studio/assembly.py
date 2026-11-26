"""Identify the selected timeline served by an assembled film."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from .contracts import Clip, ProductionSet, StudioProject
from .repository import select_entities


def ordered_assembly_sets(project: StudioProject) -> Iterator[tuple[ProductionSet, list[Clip]]]:
    sets = sorted(select_entities(project.sets), key=lambda item: (item.order, item.id))
    by_id = {
        clip.id: clip
        for clip in select_entities(
            project.clips,
            ids=[clip_id for production_set in sets for clip_id in production_set.clip_ids],
        )
    }
    for production_set in sets:
        yield (
            production_set,
            sorted(
                (
                    by_id[clip_id]
                    for clip_id in production_set.clip_ids
                    if not by_id[clip_id].archived_at
                ),
                key=lambda item: (item.order, item.id),
            ),
        )


def matches_selected_timeline(project: StudioProject, manifest: dict[str, Any]) -> bool:
    """Check a completed output against the current ready cut, in film order."""
    segments = manifest.get("segments")
    if (
        project.deleted_at
        or manifest.get("project_id") != project.id
        or manifest.get("screenplay_hash") != project.screenplay.confirmed_hash
        or not isinstance(segments, list)
        or not segments
    ):
        return False
    try:
        ordered_sets = list(ordered_assembly_sets(project))
    except KeyError:
        return False
    if any(production_set.status != "complete" for production_set, _ in ordered_sets):
        return False
    clips = [(production_set, clip) for production_set, clips in ordered_sets for clip in clips]
    if len(segments) != len(clips):
        return False
    candidates = {
        candidate.id: candidate
        for candidate in select_entities(
            project.candidates,
            ids=[clip.accepted_candidate_id for _, clip in clips if clip.accepted_candidate_id],
        )
    }
    outputs = {
        media.id: media
        for media in select_entities(
            project.media,
            ids=[candidate.media_id for candidate in candidates.values() if candidate.media_id],
        )
    }
    for (production_set, clip), segment in zip(clips, segments, strict=True):
        candidate = candidates.get(clip.accepted_candidate_id or "")
        media = outputs.get(candidate.media_id or "") if candidate else None
        if (
            clip.status != "accepted"
            or candidate is None
            or candidate.clip_id != clip.id
            or candidate.status != "accepted"
            or media is None
            or not isinstance(segment, dict)
            or (
                segment.get("set_id"),
                segment.get("clip_id"),
                segment.get("candidate_id"),
                segment.get("media_id"),
                segment.get("sha256"),
            )
            != (production_set.id, clip.id, candidate.id, media.id, media.sha256)
        ):
            return False
    return True


def current_final_media_id(project: StudioProject) -> str | None:
    """Keep finalized and legacy exports readable, fence a tracked unfinished cut."""
    if (
        project.final_media_id
        and project.status != "complete"
        and "segments" in project.export_manifest
        and not matches_selected_timeline(project, project.export_manifest)
    ):
        return None
    return project.final_media_id
