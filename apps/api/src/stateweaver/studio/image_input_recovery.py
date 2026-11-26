"""Exclude refused image inputs once, preserving media and honest review coverage."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .contracts import (
    Actor,
    ActorSession,
    Candidate,
    ImageInputBatch,
    ImageInputRecovery,
    ImageQuarantine,
    ReviewGap,
    StudioProject,
    now,
)
from .repository import select_entities

if TYPE_CHECKING:
    from .store import StudioStore

REFUSAL = "image processing blocked due to content policy violation"
GAP_REASON = "上游拒绝图像输入后隔离的疑似批次；未确认具体触发图片，这部分未完成审核"


def is_image_refusal(detail: str) -> bool:
    return REFUSAL in detail.lower()


def quarantine(project: StudioProject, media_id: str) -> ImageQuarantine | None:
    media = project.media.get(media_id)
    return project.image_quarantines.get(media.sha256) if media else None


def record_transport(project: StudioProject, actor: Actor, media_id: str, batch_id: str) -> None:
    """Record the returned input batch without claiming that the model inspected it."""
    batch = actor.last_image_input
    if not batch or batch.id != batch_id:
        batch = ImageInputBatch(
            id=batch_id,
            thread_id=actor.thread_id,
            turn_id=actor.turn_id,
            session_key=actor.session_key,
        )
        actor.last_image_input = batch
    if media_id not in batch.media_ids:
        batch.media_ids.append(media_id)


def confirm_delivery(project: StudioProject, actor: Actor, media_id: str) -> None:
    """Mark inspection only after a subsequent model tool call proves delivery."""
    session = actor.sessions.get(actor.session_key)
    if session and media_id not in session.viewed_media_ids:
        session.viewed_media_ids.append(media_id)


def acknowledge_pending_deliveries(project: StudioProject, actor: Actor) -> None:
    """Keep native stages pending; only legacy delivery uses later-tool acknowledgement."""
    from .review_sessions import reserve_image

    remaining = []
    for delivery in actor.pending_image_deliveries:
        session = actor.sessions.get(delivery.session_key)
        item = project.transcript.get(delivery.item_id)
        # Native and old unmarked stages need actual response confirmation.
        # The legacy helper must not expire or rewrite their original evidence.
        if not item or item.data.get("native_transport") is not False:
            remaining.append(delivery)
            continue
        same_context = bool(
            delivery.session_key == actor.session_key
            and delivery.actor_context_epoch == actor.context_epoch
            and session
            and delivery.session_context_epoch == session.context_epoch
            and delivery.thread_id == actor.thread_id
        )
        if not same_context:
            if item:
                item.data["delivery_confirmation"] = "expired_before_acknowledgement"
            continue
        assert session is not None
        reserve_image(project, actor, delivery.media_id)
        session.image_receipts[delivery.receipt_key] = delivery.media_id
        confirm_delivery(project, actor, delivery.media_id)
        if item:
            item.data["source"] = "mcp.read_media"
            item.data["delivery_confirmation"] = "acknowledged_by_next_tool_call"
    actor.pending_image_deliveries = remaining


def review_gaps(project: StudioProject, candidate: Candidate) -> list[ReviewGap]:
    pages = {
        page.get("media_id"): page
        for page in candidate.analysis.get("evidence_pages", [])
        if isinstance(page, dict)
    }
    gaps = []
    for media_id in dict.fromkeys(candidate.analysis.get("required_review_media_ids", [])):
        blocked = quarantine(project, media_id)
        if not blocked:
            continue
        page = pages.get(media_id, {})
        stamps = page.get("timestamps", page.get("sampled_frame_timestamps", []))
        if not isinstance(stamps, list):
            stamps = []
        gaps.append(
            ReviewGap(
                media_id=media_id,
                recovery_id=blocked.recovery_id,
                reason=blocked.reason,
                kind=str(page.get("kind") or page.get("purpose") or "image"),
                timestamps=[float(t) for t in stamps if isinstance(t, (float, int))],
            )
        )
    return gaps


def available_evidence(project: StudioProject, candidate: Candidate) -> list[str]:
    return [
        media_id
        for media_id in candidate.analysis.get("required_review_media_ids", [])
        if not quarantine(project, media_id)
    ]


def current_evidence(project: StudioProject, candidate: Candidate) -> set[str]:
    """Reference-only images cannot support a verdict about an unseen candidate."""
    noncurrent = set(candidate.analysis.get("review_reference_media_ids", [])) | set(
        candidate.analysis.get("previous_tail_media_ids", [])
    )
    noncurrent.update(
        page.get("media_id")
        for page in candidate.analysis.get("evidence_pages", [])
        if isinstance(page, dict) and page.get("kind") in {"previous_tail", "reference"}
    )
    return set(available_evidence(project, candidate)) - noncurrent


def inspected_evidence(
    project: StudioProject, actor: Actor, media_ids: list[str], store: StudioStore
) -> set[str]:
    inspected = store.inspected(project.id, actor.id, media_ids)
    if any(r.session_key == actor.session_key for r in actor.image_input_recoveries):
        session = actor.sessions.get(actor.session_key)
        inspected.intersection_update(session.viewed_media_ids if session else [])
    if actor.role == "executive":
        from .visual_analysis import delegated_evidence

        inspected.update(delegated_evidence(project, actor, media_ids, store))
        from .delegation import evidence

        inspected.update(evidence(project, actor, media_ids, store))
    return {identifier for identifier in inspected if not quarantine(project, identifier)}


def coverage_limitations(gaps: list[ReviewGap]) -> list[str]:
    return (
        [
            f"有限审核：{len(gaps)} 张疑似拒绝图片已从模型输入隔离；"
            "结论仅依据剩余已查看证据，未审范围不能视为通过。",
            *(
                f"未审图片 {gap.media_id}"
                + (
                    f"（{min(gap.timestamps):g}–{max(gap.timestamps):g} 秒）"
                    if gap.timestamps
                    else ""
                )
                + "："
                + gap.reason
                for gap in gaps
            ),
        ]
        if gaps
        else []
    )


def previous_endpoint_gaps(project: StudioProject, candidate: Candidate) -> list[ReviewGap]:
    """An audited uninspected boundary is not an asset inspection or state certificate."""
    confirmation = candidate.confirmed_end_state
    clip = project.clips[candidate.clip_id]
    if clip.accepted_candidate_id != candidate.id or not confirmation:
        return []
    frame_id = confirmation.end_state.frame_media_id
    blocked = quarantine(project, frame_id)
    if not blocked or confirmation.visual_state_ids:
        return []
    return [
        gap.model_copy(deep=True)
        for gap in confirmation.coverage_gaps
        if gap.media_id == frame_id and gap.recovery_id == blocked.recovery_id
    ]


def retained_previous_tail(
    project: StudioProject, candidate: Candidate
) -> list[dict[str, Any]] | None:
    """Reuse original pages after exclusion; never recompose a refused boundary."""
    gaps = review_gaps(project, candidate)
    references = set(candidate.analysis.get("review_reference_media_ids", []))
    footage = {
        page.get("media_id")
        for page in candidate.analysis.get("evidence_pages", [])
        if isinstance(page, dict) and page.get("kind") != "reference"
    }
    # Refused design inputs do not quarantine separately decoded footage. Only
    # actual footage gaps suppress new tail/seam extraction; every reference
    # exclusion and its original review gap remains intact.
    if not any(gap.media_id not in references or gap.media_id in footage for gap in gaps):
        return None
    duration = project.media[str(candidate.media_id)].duration or 0
    pages = []
    for page in candidate.analysis.get("evidence_pages", []):
        if not isinstance(page, dict) or page.get("kind") in {
            "previous_tail",
            "reference",
            "seam",
        }:
            continue
        stamps = page.get("timestamps", [])
        if stamps and max(stamps) >= duration - 3:
            pages.append({**page, "kind": "previous_tail", "source_candidate_id": candidate.id})
    return pages


def recovery_candidates(project: StudioProject, actor: Actor) -> list[Candidate]:
    session = actor.sessions.get(actor.session_key)
    if session and session.candidate_id:
        candidate = project.candidates.get(session.candidate_id)
        return [candidate] if candidate else []
    source_ids = {
        media.source_candidate_id
        for media_id in (actor.last_image_input.media_ids if actor.last_image_input else [])
        if (media := project.media.get(media_id)) and media.source_candidate_id
    }
    candidates = [project.candidates[c] for c in source_ids if c in project.candidates]
    current = [candidate for candidate in candidates if candidate.status == "review"]
    if candidates:
        return current or candidates
    if actor.set_id:
        for clip_id in project.sets[actor.set_id].clip_ids:
            clip = project.clips[clip_id]
            if not clip.archived_at and not clip.accepted_candidate_id:
                return select_entities(project.candidates, parent_id=clip.id, statuses=["review"])
    return []


def recovery_consumed(project: StudioProject, actor: Actor, recovery: ImageInputRecovery) -> bool:
    """Unlocated manual notices do not consume the one actual exclusion/retry."""
    if (
        recovery.status == "retrying"
        or recovery.batch_id
        or recovery.media_ids
        or recovery.replacement_thread_id
    ):
        return True
    return any(
        q.recovery_id == recovery.id
        for q in select_entities(project.image_quarantines, actor_id=actor.id)
    )


def recover_image_input(project: StudioProject, actor: Actor, detail: str) -> bool:
    """Called within a fenced transaction, only for this exact upstream refusal.

    A retry can exclude one suspected batch; another refusal cannot exclude more
    batches for the same candidate or retrying episode. Original receipts survive.
    """
    from .service import add_item

    actor.last_error = detail
    if actor.paused:
        actor.status, actor.wake_requested = "paused", False
        return False
    candidates = recovery_candidates(project, actor)
    scope_key = (
        "candidate:" + ",".join(sorted(c.id for c in candidates))
        if candidates
        else f"{actor.session_key}:progress:{actor.progress_version}"
    )
    prior = next(
        (
            r
            for r in reversed(actor.image_input_recoveries)
            if recovery_consumed(project, actor, r)
            and (
                r.status == "retrying"
                or r.scope_key == scope_key
                or r.scope_key.startswith("candidate:")
                and {c.id for c in candidates}
                & set(r.scope_key.removeprefix("candidate:").split(","))
            )
        ),
        None,
    )
    batch = actor.last_image_input
    # Native history retains this input batch across interrupted turns. Its
    # original turn stays provenance; thread/session identify the model context.
    valid = bool(
        batch
        and batch.media_ids
        and batch.thread_id == actor.thread_id
        and batch.session_key == actor.session_key
    )
    if prior or not valid:
        recovery = prior or ImageInputRecovery(
            scope_key=scope_key,
            status="manual",
            session_key=actor.session_key,
            previous_thread_id=actor.thread_id,
            previous_turn_id=actor.history_turn_id,
            reason=detail,
        )
        recovery.status, recovery.completed_at = "manual", now()
        if prior and actor.thread_id != prior.previous_thread_id:
            recovery.replacement_thread_id = actor.thread_id
        if not prior:
            actor.image_input_recoveries.append(recovery)
        actor.status, actor.wake_requested = "blocked", False
        actor.summary = (
            "图片输入恢复后仍被拒绝，需人工处理；原视频、未审范围和历史保留。"
            if prior
            else "图片输入被拒绝且无法可靠定位本次疑似批次，需人工处理。"
        )
        if actor.set_id:
            project.sets[actor.set_id].status = "blocked"
        add_item(
            project,
            actor.id,
            "error",
            actor.summary,
            {
                "image_input_recovery": recovery.model_dump(mode="json"),
                "upstream_error": detail,
            },
        )
        return False
    assert batch is not None
    recovery = ImageInputRecovery(
        scope_key=scope_key,
        batch_id=batch.id,
        media_ids=list(batch.media_ids),
        previous_thread_id=actor.thread_id,
        previous_turn_id=actor.history_turn_id,
        session_key=actor.session_key,
        reason=detail,
    )
    for media_id in batch.media_ids:
        media = project.media.get(media_id)
        if (
            media
            and media.mime_type.startswith("image/")
            and media.sha256 not in project.image_quarantines
        ):
            project.image_quarantines[media.sha256] = ImageQuarantine(
                id=media.sha256,
                media_id=media_id,
                actor_id=actor.id,
                recovery_id=recovery.id,
                reason=GAP_REASON,
            )
    for candidate in select_entities(project.candidates, statuses=["review"]):
        candidate.review_gaps = review_gaps(project, candidate)
    actor.image_input_recoveries.append(recovery)
    if candidates and not any(current_evidence(project, c) for c in candidates):
        recovery.status, recovery.completed_at = "manual", now()
        actor.status, actor.wake_requested = "blocked", False
        actor.summary = "隔离后没有当前候选的可用画面依据，不能自动作出审核结论，需人工处理。"
        if actor.set_id:
            project.sets[actor.set_id].status = "blocked"
        add_item(
            project,
            actor.id,
            "error",
            actor.summary,
            {
                "image_input_recovery": recovery.model_dump(mode="json"),
            },
        )
        return False
    session = actor.sessions.setdefault(actor.session_key, ActorSession(key=actor.session_key))
    session.thread_id = session.history_turn_id = None
    session.viewed_media_ids = []
    session.image_count = 0
    actor.thread_id = actor.history_turn_id = actor.turn_id = None
    from .director_efficiency import invalidate_context

    invalidate_context(project, actor, reason="image_policy_context_replacement")
    actor.status = "ready"
    actor.request_wake()
    actor.recovery_not_before = 0
    actor.summary = (
        f"已隔离 {len(batch.media_ids)} 张疑似图片，自动恢复 1/1；保留未审范围，继续原进度。"
    )
    if actor.set_id:
        project.sets[actor.set_id].status = "producing"
    add_item(
        project,
        actor.id,
        "status",
        actor.summary,
        {
            "image_input_recovery": recovery.model_dump(mode="json"),
        },
    )
    return True


def recovery_context(actor: Actor) -> dict[str, Any] | None:
    if not actor.image_input_recoveries:
        return None
    return {
        "latest": actor.image_input_recoveries[-1].model_dump(mode="json"),
        "instruction": (
            "Read the durable task context and remaining permitted evidence. Quarantined images "
            "are unavailable, not inspected. Do not rerequest, transform or route them elsewhere. "
            "A partial review may proceed with explicit coverage gaps and uncertainty. "
            "Keep the existing video, selections and quality counters."
        ),
    }


def complete_image_recovery(actor: Actor) -> None:
    for recovery in actor.image_input_recoveries:
        if recovery.status == "retrying" and recovery.session_key == actor.session_key:
            recovery.status = "recovered"
            recovery.replacement_thread_id = actor.thread_id
            recovery.completed_at = now()


def historical_batch(project: StudioProject, actor: Actor) -> ImageInputBatch | None:
    """Read-only bootstrap for an explicitly resumed, pre-upgrade blocked role."""
    if actor.last_image_input:
        return actor.last_image_input
    items = select_entities(project.transcript, actor_id=actor.id, limit=40, descending=True)
    items.sort(key=lambda item: (item.sequence, item.created_at), reverse=True)
    for item in items:
        data = item.data
        if data.get("type") != "mcpToolCall" or data.get("tool") not in {
            "read_media",
            "batch_read_media",
        }:
            continue
        args = data.get("arguments", {})
        ids = args.get("media_ids", [args.get("media_id")])
        if (
            not isinstance(ids, list)
            or not ids
            or not all(
                isinstance(i, str)
                and i in project.media
                and project.media[i].mime_type.startswith("image/")
                for i in ids
            )
        ):
            return None
        return ImageInputBatch(
            id=item.id,
            media_ids=list(dict.fromkeys(ids)),
            thread_id=actor.thread_id,
            turn_id=actor.history_turn_id,
            session_key=actor.session_key,
            created_at=item.created_at,
        )
    return None
