"""Persistent candidate-review context recovery for the StateWeaver role runner."""

from __future__ import annotations

from typing import Any

from .contracts import Actor, ActorSession, StudioProject, now

VERSION = "original-review-native-context-recovery-v1"


def is_context_overflow(detail: str) -> bool:
    normalized = detail.lower()
    return any(
        value in normalized
        for value in (
            "context_length_exceeded",
            "contextwindowexceeded",
            "context window exceeded",
            "context window overflow",
            "exceeds the context window",
            "exceeded the context window",
        )
    )


def review_session(project: StudioProject, actor: Actor) -> ActorSession | None:
    session = actor.sessions.get(actor.session_key)
    candidate = project.candidates.get(str(session.candidate_id)) if session else None
    if (
        project.deleted_at
        or project.status in {"complete", "failed"}
        or actor.role != "set_director"
        or actor.paused
        or actor.status in {"paused", "complete"}
        or not session
        or session.purpose != "candidate_review"
        or not actor.thread_id
        or session.thread_id != actor.thread_id
        or not candidate
        or candidate.status != "review"
        or candidate.review is not None
    ):
        return None
    return session


def recovery_record(session: ActorSession) -> dict[str, Any] | None:
    return next(
        (event for event in reversed(session.context_events) if event.get("reason") == VERSION),
        None,
    )


def pending_recovery(project: StudioProject, actor: Actor) -> bool:
    session = review_session(project, actor)
    record = recovery_record(session) if session else None
    return bool(record and record.get("status") == "pending")


def queue_recovery(project: StudioProject, actor: Actor) -> bool:
    session = review_session(project, actor)
    if not session:
        return False
    record = recovery_record(session)
    if record:
        return record.get("status") == "pending"
    session.context_events.append(
        {
            "reason": VERSION,
            "status": "pending",
            "cause": "context_length_exceeded",
            "thread_id": actor.thread_id,
            "session_key": actor.session_key,
            "candidate_id": session.candidate_id,
            "checkpoint_turn_id": actor.history_turn_id,
            "created_at": now(),
        }
    )
    return True
