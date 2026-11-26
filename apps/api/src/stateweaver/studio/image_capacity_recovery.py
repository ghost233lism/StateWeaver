"""Move an oversized candidate review onto the existing isolated batch lifecycle."""

from __future__ import annotations

from .contracts import Actor, StudioProject, now
from .review_sessions import activate_session, prepare_batches


def recover_image_capacity(project: StudioProject, actor: Actor, detail: str) -> bool:
    session = actor.sessions.get(actor.session_key)
    if (
        actor.paused
        or actor.role != "set_director"
        or not session
        or session.purpose != "candidate_review"
        or "maximum number of images" not in detail.lower()
    ):
        return False
    candidate = project.candidates.get(str(session.candidate_id))
    if not candidate or candidate.status != "review":
        return False
    # A batch failure is not retried through this path. The rejected conversation
    # stays archived; fresh batches must actually inspect their assigned evidence.
    prepare_batches(project, candidate, force_batches=True)
    previous_thread = actor.thread_id
    event = {
        "reason": "image_capacity_split",
        "detail": detail,
        "created_at": now(),
        "previous_thread_id": previous_thread,
        "images_returned": session.image_count,
        "viewed_media_ids": list(session.viewed_media_ids),
    }
    session.context_events.append(event)
    actor.recovery_history.append({**event, "reason_category": "image_capacity", "rebuild": True})
    activate_session(project, actor)
    if actor.session_key == session.key:
        return False
    actor.last_error = None
    actor.no_progress_count = 0
    actor.recovery_not_before = 0
    actor.status = "ready"
    actor.request_wake()
    actor.summary = "审核图片已去重并分批；保留生成结果和原审核记录，继续逐批检查"
    project.sets[str(actor.set_id)].status = "producing"
    return True
