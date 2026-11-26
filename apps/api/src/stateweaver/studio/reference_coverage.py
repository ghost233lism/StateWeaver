"""Inspected, Clip-scoped composite coverage; provenance alone grants no coverage."""

from __future__ import annotations

from typing import Any

from .contracts import Actor, Clip, ReferenceBinding, StudioProject
from .image_input_recovery import inspected_evidence, quarantine
from .memory import require
from .store import StudioStore


def selected_source(project: StudioProject, revision_id: str) -> dict[str, str]:
    revision = project.references.get(revision_id)
    state = project.states.get(revision.state_id) if revision else None
    media = project.media.get(str(revision.media_id)) if revision else None
    require(
        revision
        and state
        and media
        and media.mime_type.startswith("image/")
        and revision.status == "selected"
        and state.selected_revisions.get(revision.view) == revision.id,
        "Composite coverage requires the exact currently selected image revisions.",
    )
    assert revision is not None and state is not None and media is not None
    require(
        not quarantine(project, media.id), "Quarantined images cannot grant reference coverage."
    )
    return {
        "state_id": state.id,
        "revision_id": revision.id,
        "media_id": media.id,
        "sha256": media.sha256,
    }


def approve(
    project: StudioProject, actor: Actor, data: dict[str, Any], store: StudioStore
) -> dict[str, Any]:
    from .contracts import ReferenceCoverageApproval

    require(actor.role == "executive", "The executive must approve composite asset coverage.")
    revision = project.references.get(str(data.get("revision_id")))
    clip = project.clips.get(str(data.get("clip_id")))
    require(revision and clip and not clip.archived_at, "Specify a selected image and active Clip.")
    assert revision is not None and clip is not None
    composite = selected_source(project, revision.id)
    require(
        revision.selection
        and revision.selection.kind == "quality_passed"
        and revision.review_history
        and revision.review_history[-1].get("decision") == "accept",
        "Composite image must have an actual accepted image review before coverage approval.",
    )
    approval = ReferenceCoverageApproval(
        actor_id=actor.id,
        clip_id=clip.id,
        media_id=composite["media_id"],
        sha256=composite["sha256"],
        source_bindings=data.get("source_bindings", []),
        evidence_media_ids=data.get("evidence_media_ids", []),
        purpose=data.get("purpose", ""),
        reason=data.get("reason", ""),
        unknowns=data.get("unknowns", []),
    )
    sources = [source.model_dump() for source in approval.source_bindings]
    require(sources, "Explicit inspected source bindings are required; do not infer coverage.")
    state_ids = [source["state_id"] for source in sources]
    require(len(state_ids) == len(set(state_ids)), "Composite coverage cannot repeat a state.")
    allowed = {
        state_id
        for item in clip.asset_plan
        for state_id in (item.start_state_id, item.end_state_id)
        if state_id
    }
    require(set(state_ids) <= allowed, "Coverage must stay within the current Clip's asset plan.")
    for source in sources:
        require(
            selected_source(project, source["revision_id"]) == source,
            "Coverage source revision or media hash changed; inspect and approve it again.",
        )
    required = {composite["media_id"], *(source["media_id"] for source in sources)}
    evidence = set(approval.evidence_media_ids)
    require(required <= evidence, "Coverage evidence must include the composite and all sources.")
    require(
        all(identifier in project.media for identifier in evidence)
        and not any(quarantine(project, identifier) for identifier in evidence),
        "Coverage evidence is missing or quarantined.",
    )
    require(
        evidence <= inspected_evidence(project, actor, list(evidence), store),
        "Inspect the composite and every declared source before approving asset coverage.",
    )
    # A repeated command with the same content retains one durable approval. The
    # command receipt supplies transaction idempotency; this also avoids duplicates
    # when the director deliberately reaffirms unchanged evidence.
    value = approval.model_dump(exclude={"id", "created_at"})
    existing = next(
        (
            item
            for item in revision.coverage_approvals
            if item.model_dump(exclude={"id", "created_at"}) == value
        ),
        None,
    )
    if existing is None:
        revision.coverage_approvals.append(approval)
    else:
        approval = existing
    return {
        "coverage_approval_id": approval.id,
        "revision_id": revision.id,
        "clip_id": clip.id,
        "covered_state_ids": state_ids,
        "unknowns": approval.unknowns,
        "observed_state_claim": False,
        "summary": approval.reason,
    }


def approved_states(
    project: StudioProject,
    clip: Clip,
    resolved: list[tuple[ReferenceBinding, dict[str, Any]]],
) -> set[str]:
    """Validate frozen approval dependencies and annotate the exact uploaded image."""
    covered: set[str] = set()
    starts = {item.start_state_id for item in clip.asset_plan}
    for binding, source in resolved:
        # Cropping/preparation creates a different image and can remove identities.
        # A full-image approval never transfers to it without a new reviewed revision.
        # The declared use does not change the full image's explicitly approved coverage.
        # Guidance stays guidance; only the frozen source states below gain coverage.
        if source.get("prepared_reference_id") or source.get("state_role", "start") not in {
            "start",
            "guidance",
        }:
            continue
        # An external operator may approve a quarantined image for its own exact
        # binding and Clip, but that does not make the image valid evidence for
        # broader composite multi-state coverage. Keep the binding usable while
        # declining to grant any transitive coverage from it.
        if quarantine(project, str(source.get("media_id"))):
            from .external_review import media_is_external_approved

            owner_id = project.sets[clip.set_id].actor_id
            if media_is_external_approved(
                project,
                str(source.get("media_id")),
                "reference_use",
                actor_id=owner_id,
                clip_id=clip.id,
            ) and media_is_external_approved(
                project,
                str(source.get("media_id")),
                "generation_input",
                actor_id=owner_id,
                clip_id=clip.id,
            ):
                continue
        revision = project.references.get(binding.revision_id)
        if revision is None:
            continue
        current = selected_source(project, revision.id)
        if (
            source.get("media_id") != current["media_id"]
            or source.get("sha256") != current["sha256"]
        ):
            continue
        for approval in reversed(revision.coverage_approvals):
            if (
                approval.clip_id != clip.id
                or approval.media_id != current["media_id"]
                or approval.sha256 != current["sha256"]
            ):
                continue
            valid: list[str] = []
            for evidence in approval.source_bindings:
                expected = evidence.model_dump()
                try:
                    actual = selected_source(project, evidence.revision_id)
                except ValueError:
                    break
                if actual != expected or evidence.state_id not in starts:
                    break
                valid.append(evidence.state_id)
            else:
                if not valid or any(
                    identifier not in project.media or quarantine(project, identifier)
                    for identifier in approval.evidence_media_ids
                ):
                    continue
                source["composite_state_coverage"] = {
                    "approval_id": approval.id,
                    "approved_by": approval.actor_id,
                    "purpose": approval.purpose,
                    "reason": approval.reason,
                    "state_ids": valid,
                    "source_bindings": [item.model_dump() for item in approval.source_bindings],
                    "evidence_media_ids": list(approval.evidence_media_ids),
                    "basis": "inspected_composite_design",
                    "observed_state_claim": False,
                    "unknowns": list(approval.unknowns),
                    "composite_sha256": approval.sha256,
                }
                covered.update(valid)
                break
    return covered


def semantic_record(project: StudioProject, revision_id: str, state_role: str) -> dict[str, Any]:
    """Separate what an image depicts from how this plan intends to use it."""
    revision = project.references[revision_id]
    if revision.source_observation_ids:
        basis = (
            "observed_frame"
            if not revision.job_id and revision.media_id in revision.source_media_ids
            else "design_derived_from_observation"
        )
    else:
        basis = "design"
    return {
        "use": "planned_target" if state_role == "target" else state_role,
        "image_basis": basis,
        "source_observation_ids": list(revision.source_observation_ids),
        "observed_state_claim": basis == "observed_frame" and state_role != "target",
        "actual_deviations_preserved": bool(revision.source_observation_ids),
    }


def context_records(project: StudioProject, actor: Actor, clip: Clip) -> list[dict[str, Any]]:
    """Discover valid approvals without granting any additional entity/media access."""
    from .repository import select_entities
    from .visibility import Visibility

    if actor.role not in {"executive", "set_director"}:
        return []
    visibility = Visibility(project, actor.id)
    if not visibility.allowed("clips", clip.id):
        return []
    records = []
    for revision in select_entities(project.references, statuses=["selected"]):
        if not any(item.clip_id == clip.id for item in revision.coverage_approvals):
            continue
        if not visibility.allowed("references", revision.id):
            continue
        state = project.states[revision.state_id]
        binding = ReferenceBinding(
            asset_id=state.asset_id, state_id=state.id, revision_id=revision.id
        )
        try:
            source: dict[str, Any] = {
                **selected_source(project, revision.id),
                "state_role": "start",
            }
            covered = approved_states(project, clip, [(binding, source)])
        except ValueError:
            continue
        if not covered:
            continue
        coverage = source["composite_state_coverage"]
        if not all(
            visibility.allowed("media", identifier) for identifier in coverage["evidence_media_ids"]
        ):
            continue
        records.append(
            {
                "type": "approved_composite_reference",
                "value": {
                    "clip_id": clip.id,
                    **binding.model_dump(),
                    "view": revision.view,
                    "media_id": source["media_id"],
                    "sha256": source["sha256"],
                    "coverage": coverage,
                },
            }
        )
    return records
