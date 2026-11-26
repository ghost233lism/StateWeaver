"""Audited human media decisions without impersonating native model inspection."""

from __future__ import annotations

from collections.abc import Iterable

from .contracts import (
    ExternalMediaReview,
    ExternalReviewItem,
    ExternalReviewPurpose,
    ImageQuarantine,
    StudioProject,
)
from .repository import select_entities


def _scope_matches(
    review: ExternalMediaReview,
    purpose: ExternalReviewPurpose,
    *,
    actor_id: str | None,
    clip_id: str | None,
    candidate_id: str | None,
) -> bool:
    if purpose not in review.scope.purposes:
        return False
    for recorded, current in (
        (review.scope.actor_id, actor_id),
        (review.scope.clip_id, clip_id),
        (review.scope.candidate_id, candidate_id),
    ):
        # An omitted receipt field is intentionally project-wide. A constrained
        # receipt is never consumed unless the caller supplies the same context.
        if recorded is not None and recorded != current:
            return False
    return True


def _matching_items(
    project: StudioProject,
    media_id: str,
    purpose: ExternalReviewPurpose,
    *,
    actor_id: str | None = None,
    clip_id: str | None = None,
    candidate_id: str | None = None,
    receipt_ids: Iterable[str] | None = None,
) -> list[tuple[ExternalMediaReview, ExternalReviewItem]]:
    media = project.media.get(media_id)
    if media is None:
        return []
    allowed_ids = set(receipt_ids) if receipt_ids is not None else None
    matches: list[tuple[ExternalMediaReview, ExternalReviewItem]] = []
    for review in select_entities(project.external_reviews):
        if (
            review.project_id != project.id
            or review.revoked_at is not None
            or (allowed_ids is not None and review.id not in allowed_ids)
            or not _scope_matches(
                review,
                purpose,
                actor_id=actor_id,
                clip_id=clip_id,
                candidate_id=candidate_id,
            )
        ):
            continue
        for item in review.items:
            if item.media_id == media_id and item.source_sha256 == media.sha256:
                matches.append((review, item))
    return sorted(matches, key=lambda match: (match[0].created_at, match[0].id))


def external_review_item(
    project: StudioProject,
    media_id: str,
    purpose: ExternalReviewPurpose,
    *,
    actor_id: str | None = None,
    clip_id: str | None = None,
    candidate_id: str | None = None,
    receipt_ids: Iterable[str] | None = None,
) -> tuple[ExternalMediaReview, ExternalReviewItem] | None:
    """Return the latest exact-hash decision in the supplied production scope."""

    matches = _matching_items(
        project,
        media_id,
        purpose,
        actor_id=actor_id,
        clip_id=clip_id,
        candidate_id=candidate_id,
        receipt_ids=receipt_ids,
    )
    return matches[-1] if matches else None


def media_is_external_approved(
    project: StudioProject,
    media_id: str,
    purpose: ExternalReviewPurpose,
    *,
    actor_id: str | None = None,
    clip_id: str | None = None,
    candidate_id: str | None = None,
    receipt_ids: Iterable[str] | None = None,
) -> bool:
    """Check an exact hash-bound approval; this never changes native inspection state."""

    match = external_review_item(
        project,
        media_id,
        purpose,
        actor_id=actor_id,
        clip_id=clip_id,
        candidate_id=candidate_id,
        receipt_ids=receipt_ids,
    )
    return bool(match and match[1].verdict == "approved")


def blocked_quarantine(
    project: StudioProject,
    media_id: str,
    purpose: ExternalReviewPurpose,
    *,
    actor_id: str | None = None,
    clip_id: str | None = None,
    candidate_id: str | None = None,
) -> ImageQuarantine | None:
    """Return raw quarantine unless a scoped external approval overrides eligibility."""

    from .image_input_recovery import quarantine

    blocked = quarantine(project, media_id)
    if blocked is None:
        return None
    if media_is_external_approved(
        project,
        media_id,
        purpose,
        actor_id=actor_id,
        clip_id=clip_id,
        candidate_id=candidate_id,
    ):
        return None
    return blocked


def validate_external_media_review(project: StudioProject, review: ExternalMediaReview) -> None:
    """Validate target identity, current hashes and acknowledged refusal history."""

    if review.project_id != project.id:
        raise ValueError("外部审核收据不属于当前项目")
    if review.scope.actor_id is not None and review.scope.actor_id not in project.actors:
        raise ValueError("外部审核角色不存在")
    if review.scope.clip_id is not None and review.scope.clip_id not in project.clips:
        raise ValueError("外部审核片段不存在")
    if (
        review.scope.candidate_id is not None
        and review.scope.candidate_id not in project.candidates
    ):
        raise ValueError("外部审核候选不存在")
    if review.scope.candidate_id is not None and review.scope.clip_id is not None:
        candidate = project.candidates[review.scope.candidate_id]
        if candidate.clip_id != review.scope.clip_id:
            raise ValueError("外部审核候选不属于指定片段")
    quarantines_by_id = {item.id: item for item in select_entities(project.image_quarantines)}
    for item in review.items:
        media = project.media.get(item.media_id)
        if media is None:
            raise ValueError(f"外部审核素材不存在：{item.media_id}")
        if media.sha256 != item.source_sha256:
            raise ValueError(f"外部审核素材哈希已变化：{item.media_id}")
        from .image_input_recovery import quarantine

        blocked = quarantine(project, item.media_id)
        known_quarantines = dict(quarantines_by_id)
        if blocked is not None:
            known_quarantines[blocked.id] = blocked
        unknown = set(item.acknowledged_quarantine_ids) - set(known_quarantines)
        if unknown:
            identifier = sorted(unknown)[0]
            raise ValueError(f"外部审核引用了不存在的隔离记录：{identifier}")
        unrelated = [
            identifier
            for identifier in item.acknowledged_quarantine_ids
            if known_quarantines[identifier].media_id != item.media_id
            and identifier != media.sha256
        ]
        if unrelated:
            raise ValueError(f"外部审核引用了其他素材的隔离记录：{unrelated[0]}")
        if blocked is not None and blocked.id not in item.acknowledged_quarantine_ids:
            raise ValueError(f"外部审核未确认素材的原隔离记录：{item.media_id}")
