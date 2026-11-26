"""One approved full-image reference may share the exact real endpoint upload slot.

This is an internal physical-input projection. Logical bindings and inspection
identities remain intact; SHA equality alone never grants state coverage.
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict

from .adjudication import actual_end_state
from .contracts import Candidate, Clip, ReferenceBinding, ReferencePlan, StudioProject
from .reference_coverage import approved_states
from .set_handoffs import observation, same_source_image, scope_covers
from .store import StudioError


class EndpointCoverageAlias(TypedDict):
    policy: Literal["approved-exact-endpoint-v1"]
    clip_id: str
    source_candidate_id: str
    endpoint_media_id: str
    endpoint_sha256: str
    reference_revision_id: str
    reference_media_id: str
    reference_sha256: str
    coverage_approval_id: str
    covered_state_ids: list[str]


def approved_alias(
    project: StudioProject,
    clip: Clip,
    previous: Candidate | None,
    binding: ReferenceBinding,
    source: dict[str, Any],
) -> EndpointCoverageAlias | None:
    """Derive the alias from the original selected image and frozen approval."""
    if previous is None:
        return None
    production_set = project.sets.get(clip.set_id)
    previous_clip = project.clips.get(previous.clip_id)
    endpoint = actual_end_state(previous)
    revision = project.references.get(binding.revision_id)
    if (
        production_set is None
        or not 0 < clip.order < len(production_set.clip_ids)
        or production_set.clip_ids[clip.order - 1] != previous.clip_id
        or previous_clip is None
        or previous_clip.accepted_candidate_id != previous.id
        or endpoint is None
        or not previous.confirmed_end_state
        or source.get("prepared_reference_id")
        or source.get("state_role", "start") != "start"
        or revision is None
        or revision.state_id != binding.state_id
        or source.get("asset_id") != binding.asset_id
        or source.get("state_id") != binding.state_id
        or source.get("revision_id") != binding.revision_id
        or revision.job_id
        or not revision.source_observation_ids
        or revision.media_id not in revision.source_media_ids
        or not revision.selection
        or revision.selection.kind != "quality_passed"
        or not revision.review_history
        or revision.review_history[-1].get("decision") != "accept"
        or source.get("media_id") != revision.media_id
        or not any(
            item.asset_id == binding.asset_id and item.start_state_id == binding.state_id
            for item in clip.asset_plan
        )
    ):
        return None
    source_media = project.media.get(str(source.get("media_id")))
    endpoint_media = project.media.get(endpoint.frame_media_id)
    if (
        source_media is None
        or endpoint_media is None
        or source_media.source_candidate_id != previous.id
        or endpoint_media.source_candidate_id != previous.id
        or source.get("sha256") != source_media.sha256
        or not same_source_image(project, source_media.id, endpoint_media.id)
        or source_media.mime_type != endpoint_media.mime_type
        or source_media.byte_size != endpoint_media.byte_size
        or (source_media.width, source_media.height)
        != (endpoint_media.width, endpoint_media.height)
    ):
        return None
    observed = False
    for identifier in revision.source_observation_ids:
        try:
            entry = observation(project, identifier)
        except ValueError:
            continue
        if (
            entry.source_candidate_id == previous.id
            and entry.approved_asset_id == binding.asset_id
            and binding.state_id in entry.approved_state_ids
            and source_media.id in entry.evidence_media_ids
            and scope_covers(project, entry, project.sets[clip.set_id], clip)
        ):
            observed = True
            break
    if not observed:
        return None
    checked = dict(source)
    checked.pop("endpoint_coverage_alias", None)
    checked.pop("composite_state_coverage", None)
    try:
        covered = approved_states(project, clip, [(binding, checked)])
    except ValueError:
        return None
    if binding.state_id not in covered:
        return None
    coverage = checked["composite_state_coverage"]
    return {
        "policy": "approved-exact-endpoint-v1",
        "clip_id": clip.id,
        "source_candidate_id": previous.id,
        "endpoint_media_id": endpoint_media.id,
        "endpoint_sha256": endpoint_media.sha256,
        "reference_revision_id": binding.revision_id,
        "reference_media_id": source_media.id,
        "reference_sha256": source_media.sha256,
        "coverage_approval_id": coverage["approval_id"],
        "covered_state_ids": sorted(covered),
    }


def bind_alias(
    project: StudioProject,
    clip: Clip,
    previous: Candidate | None,
    resolved: list[tuple[ReferenceBinding, dict[str, Any]]],
) -> None:
    """Recompute controlled metadata; authored or stale metadata grants nothing."""
    aliases = []
    for binding, source in resolved:
        source.pop("endpoint_coverage_alias", None)
        alias = approved_alias(project, clip, previous, binding, source)
        if alias:
            aliases.append((source, alias))
    if len(aliases) > 1:
        raise StudioError(
            "Only one explicitly approved reference may share the endpoint image slot."
        )
    for source, alias in aliases:
        source["endpoint_coverage_alias"] = alias


def physical_image_sources(plan: ReferencePlan) -> list[dict[str, Any]]:
    """Return actual endpoint-first uploads while retaining logical reference uses."""
    endpoint = (
        {"media_id": plan.previous_end_state.frame_media_id, "endpoint": True}
        if plan.previous_end_state
        else None
    )
    ordinary = []
    found = False
    for source in plan.reference_inputs:
        alias = source.get("endpoint_coverage_alias")
        if alias is None:
            ordinary.append(source)
            continue
        if (
            endpoint is None
            or found
            or alias.get("policy") != "approved-exact-endpoint-v1"
            or alias.get("clip_id") != plan.clip_id
            or alias.get("source_candidate_id") != plan.previous_candidate_id
            or alias.get("endpoint_media_id") != endpoint["media_id"]
            or alias.get("reference_revision_id") != source.get("revision_id")
            or alias.get("reference_media_id") != source.get("media_id")
            or alias.get("reference_sha256") != source.get("sha256")
            or alias.get("coverage_approval_id")
            != (source.get("composite_state_coverage") or {}).get("approval_id")
        ):
            raise StudioError("Invalid pinned approved endpoint coverage mapping.")
        found = True
        # The actual bytes/provenance remain the real endpoint. The selected
        # notebook's scoped image semantics and approved coverage remain explicit.
        for key in (
            "asset_id",
            "state_id",
            "revision_id",
            "state_role",
            "purpose",
            "reference_semantics",
            "composite_state_coverage",
            "endpoint_coverage_alias",
        ):
            if key in source:
                endpoint[key] = source[key]
    return ([endpoint] if endpoint else []) + ordinary


def validate_pinned_alias(
    project: StudioProject, binding: ReferenceBinding, source: dict[str, Any]
) -> None:
    """Recheck an unsubmitted alias before constructing the physical H3 request."""
    alias = source.get("endpoint_coverage_alias")
    if alias is None:
        return
    clip = project.clips.get(str(alias.get("clip_id")))
    previous = project.candidates.get(str(alias.get("source_candidate_id")))
    actual = approved_alias(project, clip, previous, binding, source) if clip else None
    if actual is None or actual != alias:
        raise StudioError(
            "Pinned endpoint coverage dependencies changed; resave and inspect the plan."
        )
