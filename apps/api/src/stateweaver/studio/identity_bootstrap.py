"""Executive-scoped first-Clip textual identity; never visual evidence or supply."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, ValidationError

from .contracts import Actor, Clip, Contract, Proposal, ProposalRequirement, StudioProject
from .memory import digest, require
from .repository import select_entities
from .voice import VOICE_ASSET_KINDS

KEY = "text_identity_bootstrap"
VERSION = "text-identity-bootstrap-v1"
IDENTITY_ASSET_KINDS = VOICE_ASSET_KINDS | frozenset(
    {"animal", "pet", "robot", "creature", "stylized_character", "动物", "机器人", "生物"}
)


class IdentityBootstrapInput(Contract):
    asset_id: str
    state_id: str
    reason: str = Field(min_length=1)
    unknowns: list[str] = Field(min_length=1)
    proposal_id: str
    requirement_ids: list[str] = Field(min_length=1)


class IdentityBootstrap(IdentityBootstrapInput):
    version: Literal["text-identity-bootstrap-v1"] = "text-identity-bootstrap-v1"
    clip_id: str
    actor_id: str
    source_fingerprint: str
    observed_state_claim: Literal[False] = False
    supplies_media: Literal[False] = False


def _scope(p: StudioProject, clip: Clip, asset_id: str, state_id: str) -> bool:
    asset, state = p.assets.get(asset_id), p.states.get(state_id)
    production_set = p.sets.get(clip.set_id)
    return bool(
        not clip.archived_at
        and production_set
        and (production_set.order == 0)
        and (clip.order == 0)
        and asset
        and (asset.kind in IDENTITY_ASSET_KINDS)
        and state
        and (state.asset_id == asset_id)
        and state.is_base
        and any(
            x.asset_id == asset_id
            and x.start_state_id == state_id
            and (x.end_state_id in {None, state_id})
            for x in clip.asset_plan
        )
    )


def _fingerprint(p: StudioProject, clip: Clip, asset_id: str, state_id: str) -> str:
    return digest(
        {
            "source": p.screenplay.confirmed_hash,
            "clip": {
                "id": clip.id,
                "set_id": clip.set_id,
                "duration": clip.duration,
                "events": clip.story_event_ids,
                "requirements": clip.requirement_ids,
                "starts_at": clip.starts_at.model_dump() if clip.starts_at else None,
                "ends_at": clip.ends_at.model_dump() if clip.ends_at else None,
                "asset_plan": [item.model_dump() for item in clip.asset_plan],
            },
            "identity": p.assets[asset_id].description,
            "state": p.states[state_id].description,
        }
    )


def decision(p: StudioProject, clip: Clip) -> IdentityBootstrap | None:
    raw = clip.continuity.get(KEY)
    if not raw:
        return None
    try:
        value = IdentityBootstrap.model_validate_json(raw)
    except (ValidationError, ValueError):
        return None
    actor = p.actors.get(value.actor_id)
    if not (
        value.clip_id == clip.id
        and actor
        and actor.role == "executive"
        and _scope(p, clip, value.asset_id, value.state_id)
        and value.source_fingerprint == _fingerprint(p, clip, value.asset_id, value.state_id)
    ):
        return None
    return value


def guard_continuity(clip: Clip, data: dict[str, Any]) -> None:
    continuity = data.get("continuity")
    if continuity is not None:
        require(isinstance(continuity, dict), "Clip continuity must be an object.")
        require(
            continuity.get(KEY) == clip.continuity.get(KEY),
            "Textual identity authorization must use executive identity_bootstrap, "
            "not raw continuity.",
        )


def approve(p: StudioProject, actor: Actor, clip: Clip, data: dict[str, Any]) -> None:
    require(
        actor.role == "executive", "Only the executive can authorize textual identity bootstrap."
    )
    require(not clip.continuity.get(KEY), "The first-Clip textual bootstrap cannot be renewed.")
    require(
        not clip.candidate_ids and not select_entities(p.candidates, parent_id=clip.id),
        "Textual bootstrap is only authorized before the first candidate exists.",
    )
    asset_id, state_id = str(data.get("asset_id")), str(data.get("state_id"))
    require(
        _scope(p, clip, asset_id, state_id),
        "Textual bootstrap needs the exact first Clip/base identity state "
        "and no required external image.",
    )
    require(
        not p.states[state_id].selected_revisions,
        "An existing selected identity reference must be used instead of textual bootstrap.",
    )
    value = IdentityBootstrap.model_validate(
        {
            **data,
            "version": VERSION,
            "clip_id": clip.id,
            "actor_id": actor.id,
            "source_fingerprint": _fingerprint(p, clip, asset_id, state_id),
        }
    )
    require(
        value.reason.strip() and all(x.strip() for x in value.unknowns),
        "Record missing visual identity and the actual identity risks explicitly.",
    )
    proposal = p.proposals.get(value.proposal_id)
    require(
        proposal
        and proposal.status in {"pending", "accepted", "in_progress"}
        and clip.id in proposal.affected_clip_ids,
        "Defer the exact current first-Clip reference proposal.",
    )
    assert proposal is not None
    rows = {item.id: item for item in proposal.requirements}
    require(
        all(
            i in rows
            and rows[i].kind in {"reference", "view"}
            and rows[i].status == "pending"
            and rows[i].clip_ids == [clip.id]
            and not rows[i].evidence_ids
            for i in value.requirement_ids
        ),
        "Only the exact unsupplied first-Clip reference obligations may be deferred, "
        "never marked done.",
    )
    clip.continuity[KEY] = value.model_dump_json()


def _failed_without_footage(p: StudioProject, identifiers: list[str]) -> bool:
    for identifier in identifiers:
        candidate = p.candidates.get(identifier)
        job = p.jobs.get(candidate.job_id) if candidate else None
        if (
            not candidate
            or candidate.media_id
            or candidate.review
            or not job
            or job.status != "failed"
        ):
            return False
    return True


def _can_start(p: StudioProject, clip: Clip) -> bool:
    # The original authorization is never renewed. A normal, definite no-media
    # technical repair may reuse it; unknown or returned footage cannot.
    return not clip.candidate_ids or (
        clip.status == "planned" and _failed_without_footage(p, clip.candidate_ids)
    )


def covers(p: StudioProject, clip: Clip, asset_id: str, state_id: str) -> bool:
    value = decision(p, clip)
    return bool(
        value and _can_start(p, clip) and value.asset_id == asset_id and value.state_id == state_id
    )


def awaiting_source(p: StudioProject, clip: Clip) -> bool:
    """Keep only initial work/review deferred; failures and reviewed footage need intervention."""
    if not decision(p, clip):
        return False
    if _can_start(p, clip):
        return True
    if not _failed_without_footage(p, clip.candidate_ids[:-1]):
        return False
    candidate = p.candidates.get(clip.candidate_ids[-1])
    job = p.jobs.get(candidate.job_id) if candidate else None
    return bool(
        candidate
        and not candidate.review
        and job
        and job.status in {"queued", "submitting", "running", "complete"}
        and not job.query_outage_since
        and job.request.get("continuity", {}).get(KEY) == clip.continuity[KEY]
        and (job.status in {"queued", "submitting", "running"} or candidate.media_id)
    )


def deferred(p: StudioProject, clip: Clip, proposal: Proposal, item: ProposalRequirement) -> bool:
    value = decision(p, clip)
    return bool(
        value
        and item.status == "pending"
        and item.kind in {"reference", "view"}
        and item.clip_ids == [clip.id]
        and proposal.id == value.proposal_id
        and item.id in value.requirement_ids
        and awaiting_source(p, clip)
    )


def proposal_waiting(p: StudioProject, proposal: Proposal) -> bool:
    from .proposals import requirement_ready
    from .runtime_decisions import source_actor_can_progress

    outstanding = [item for item in proposal.requirements if not requirement_ready(p, item)]
    if not outstanding:
        return False
    return all(
        len(item.clip_ids) == 1
        and (clip := p.clips.get(item.clip_ids[0])) is not None
        and deferred(p, clip, proposal, item)
        and source_actor_can_progress(p, p.sets[clip.set_id].actor_id)
        for item in outstanding
    )
