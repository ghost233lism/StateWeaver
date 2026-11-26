"""Candidate-isolated review contexts; retain historical batch review records."""

from __future__ import annotations

from typing import Any

from .contracts import (
    Actor,
    ActorSession,
    Candidate,
    Job,
    ReviewBatch,
    ReviewFindingAssessment,
    StudioProject,
    VisualFinding,
    now,
)
from .harness import CANDIDATE_REVIEW, QUAD_EVIDENCE, REVIEW_IMAGE_LIMIT
from .memory import require
from .repository import select_entities
from .review_sampling import review_sampling
from .store import StudioStore


def candidate_session(candidate: Candidate) -> bool:
    return candidate.analysis.get("review_context_version") == CANDIDATE_REVIEW


def evidence_policy(project: StudioProject, candidate: Candidate) -> dict[str, str]:
    """New candidates use one conversation; never reinterpret saved legacy evidence."""
    layout = candidate.analysis.get("evidence_layout", {})
    if isinstance(layout, dict) and layout.get("version") == QUAD_EVIDENCE:
        return {
            "review_context_version": candidate.analysis.get(
                "review_context_version", CANDIDATE_REVIEW
            ),
            "evidence_layout": QUAD_EVIDENCE,
        }
    if not candidate.analysis or candidate_session(candidate):
        return {"review_context_version": CANDIDATE_REVIEW, "evidence_layout": QUAD_EVIDENCE}
    return {}


def queue_evidence(project: StudioProject, candidate: Candidate) -> Job | None:
    if not candidate.media_id:
        return None
    existing = select_entities(project.jobs, parent_id=candidate.id, subtype="analysis")
    if existing:
        return None
    job = Job(
        actor_id=project.sets[project.clips[candidate.clip_id].set_id].actor_id,
        kind="analysis",
        target_id=candidate.id,
        request={
            "review_mode": "director",
            "automatic": True,
            **evidence_policy(project, candidate),
            "sampling": review_sampling(project, candidate),
            "instructions": (
                "Check identity, story state, action, composition, camera "
                "movement and seams using the prepared visual evidence."
            ),
        },
    )
    project.jobs[job.id] = job
    return job


def active_batches(candidate: Candidate) -> list[ReviewBatch]:
    request_id = candidate.analysis.get("request_id")
    return [batch for batch in candidate.review_batches if batch.analysis_job_id == request_id]


CANDIDATE_IMAGE_LIMIT = 45


def compact_review_evidence(project: StudioProject, candidate: Candidate) -> dict[str, str]:
    """Share identical evidence within provenance; retain media and job records."""
    analysis = candidate.analysis
    pages = analysis.get("evidence_pages", [])
    identifiers = list(
        dict.fromkeys(
            [
                *[page["media_id"] for page in pages],
                *analysis.get("required_review_media_ids", []),
                *analysis.get("review_reference_media_ids", []),
            ]
        )
    )
    # The actual final frame is also a durable continuity reference; keep its ID.
    end_id = analysis.get("end_frame_media_id")
    if end_id in identifiers:
        identifiers = [end_id, *[item for item in identifiers if item != end_id]]
    provenance = {
        page["media_id"]: (
            page.get("source_candidate_id", candidate.id),
            "previous_tail" if page.get("kind") == "previous_tail" else "current",
        )
        for page in pages
    }
    reference_ids = set(analysis.get("review_reference_media_ids", []))
    seen: dict[tuple[str, str, str], str] = {}
    aliases: dict[str, str] = {}
    for identifier in identifiers:
        media = project.media.get(identifier)
        if media and media.sha256:
            source, kind = provenance.get(
                identifier,
                (candidate.id, "reference" if identifier in reference_ids else "current"),
            )
            canonical = seen.setdefault((media.sha256, source, kind), identifier)
            if canonical != identifier:
                aliases[identifier] = canonical

    def compact_ids(values: list[str]) -> list[str]:
        return list(dict.fromkeys(aliases.get(value, value) for value in values))

    merged: dict[str, dict[str, Any]] = {}
    for original in pages:
        page = dict(original)
        page_id: str = page["media_id"]
        retained_id = aliases.get(page_id, page_id)
        page["media_id"] = retained_id
        target = merged.setdefault(retained_id, page)
        if target is page:
            continue
        for field in ("profile_ids", "timestamps", "frame_indices"):
            if field in page:
                target[field] = list(dict.fromkeys([*target.get(field, []), *page[field]]))
        if "preprocessing" in page:
            target.setdefault("preprocessing", page["preprocessing"])
    analysis["evidence_pages"] = list(merged.values())
    for field in (
        "required_review_media_ids",
        "review_reference_media_ids",
        "sampled_frames_media_ids",
        "previous_tail_media_ids",
    ):
        if field in analysis:
            analysis[field] = compact_ids(analysis[field])
    for field in ("sampled_frames_media_id", "seam_media_id", "boundary_comparison_media_id"):
        if field in analysis:
            analysis[field] = aliases.get(analysis[field], analysis[field])
    # This small map explains old media citations without deleting their audit trail.
    if aliases:
        analysis["review_media_aliases"] = {**analysis.get("review_media_aliases", {}), **aliases}
    return aliases


def prepare_batches(
    project: StudioProject, candidate: Candidate, *, force_batches: bool = False
) -> None:
    analysis = candidate.analysis
    request_id = str(analysis.get("request_id") or "")
    pages: list[str] = list(dict.fromkeys(analysis.get("required_review_media_ids", [])))
    require(request_id and pages, "审核需要实际生成的完整抽帧证据")
    generation = project.jobs[candidate.job_id]
    references = list(
        dict.fromkeys(
            str(item["media_id"])
            for item in generation.request.get("reference_inputs", [])
            if item.get("media_id") in project.media
            and project.media[str(item["media_id"])].mime_type.startswith("image/")
        )
    )
    analysis["review_reference_media_ids"] = references
    analysis["required_review_media_ids"] = list(dict.fromkeys([*pages, *references]))
    aliases = compact_review_evidence(project, candidate)
    required = set(analysis["required_review_media_ids"])
    pages = list(
        dict.fromkeys(
            aliases.get(value, value) for value in pages if aliases.get(value, value) in required
        )
    )
    references = analysis["review_reference_media_ids"]
    batches = active_batches(candidate)
    if batches:
        retired = set()
        for batch in batches:
            if batch.status != "pending":
                continue
            retained = list(
                dict.fromkeys(
                    aliases.get(value, value)
                    for value in batch.media_ids
                    if aliases.get(value, value) in required
                )
            )
            if retained:
                batch.media_ids = retained
            else:
                retired.add(batch.id)
        candidate.review_batches = [
            batch for batch in candidate.review_batches if batch.id not in retired
        ]
        return
    distinct_images = {
        project.media[value].sha256 if value in project.media else value for value in required
    }
    if (
        candidate_session(candidate)
        and not force_batches
        and len(distinct_images) <= CANDIDATE_IMAGE_LIMIT
    ):
        if (
            candidate.confirmed_end_state
            and candidate.confirmed_end_state.end_state.frame_media_id
            != analysis.get("end_frame_media_id")
        ):
            candidate.confirmed_end_state = None
            candidate.end_state = None
        return
    anchors: list[str] = list(
        dict.fromkeys(
            i
            for i in [analysis.get("sampled_frames_media_id"), analysis.get("end_frame_media_id")]
            if isinstance(i, str) and i in pages
        )
    )
    for offset in range(0, len(references), REVIEW_IMAGE_LIMIT - len(anchors)):
        part = references[offset : offset + REVIEW_IMAGE_LIMIT - len(anchors)]
        candidate.review_batches.append(
            ReviewBatch(
                analysis_job_id=request_id,
                purpose="references",
                media_ids=list(dict.fromkeys([*part, *anchors])),
                question="将本次实际输入的参考素材与候选画面对照，核对身份、服饰、物件和状态。",
            )
        )
    for offset in range(0, len(pages), REVIEW_IMAGE_LIMIT):
        candidate.review_batches.append(
            ReviewBatch(
                analysis_job_id=request_id,
                media_ids=pages[offset : offset + REVIEW_IMAGE_LIMIT],
                question="检查这一批覆盖的动作、空间、机位、剧情事实和接缝；只报告画面支持的观察。",
            )
        )
    analysis["review_context_version"] = "isolated-batches-v1"
    if (
        candidate.confirmed_end_state
        and candidate.confirmed_end_state.end_state.frame_media_id
        != analysis.get("end_frame_media_id")
    ):
        candidate.confirmed_end_state = None
        candidate.end_state = None


def desired_session(project: StudioProject, actor: Actor) -> ActorSession:
    if actor.role == "executive" and actor.production_context_started_at:
        return ActorSession(key="executive-production", purpose="executive_production")
    from .planning_sessions import covered_plan_work, desired_planning

    if covered_plan_work(project, actor):
        # A safe new claim can finish administrative planning before film review.
        # Existing live review sessions remain stored and resume afterwards.
        return desired_planning(project, actor)
    from .candidate_lifecycle import pending_review_candidates

    if actor.role == "set_director":
        for candidate in pending_review_candidates(project, actor):
            if candidate.analysis.get("required_review_media_ids"):
                prepare_batches(project, candidate)
            if candidate_session(candidate):
                return ActorSession(
                    key=f"review:{candidate.id}",
                    purpose="candidate_review",
                    candidate_id=candidate.id,
                )
            batches = active_batches(candidate)
            if not batches:
                continue
            pending = next((batch for batch in batches if batch.status == "pending"), None)
            if pending:
                return ActorSession(
                    key=f"review:{candidate.id}:{pending.id}:{pending.context_attempt}",
                    purpose="review_batch",
                    candidate_id=candidate.id,
                    batch_id=pending.id,
                )
            return ActorSession(
                key=f"summary:{candidate.id}:{candidate.analysis['request_id']}:{len(batches)}",
                purpose="review_summary",
                candidate_id=candidate.id,
            )
    from .planning_sessions import desired_planning
    from .planning_sessions import enabled as short_planning

    if short_planning(project, actor):
        return desired_planning(project, actor)
    return ActorSession(key="planning")


def activate_session(project: StudioProject, actor: Actor) -> None:
    from .planning_sessions import enabled as short_planning
    from .planning_sessions import save_checkpoint, start_at_boundary

    start_at_boundary(project, actor)
    desired = desired_session(project, actor)
    previous = actor.sessions.setdefault(actor.session_key, ActorSession(key=actor.session_key))
    previous.thread_id, previous.history_turn_id = actor.thread_id, actor.history_turn_id
    if actor.session_key == desired.key:
        return
    completed_lookahead = _completed_lookahead(project, actor)
    if short_planning(project, actor):
        save_checkpoint(project, actor, previous)
    if completed_lookahead:
        from .memory import digest

        previous.checkpoint.setdefault(
            "runtime_handoff",
            {
                "reason": "completed_lookahead_task_boundary",
                "next_session_key": desired.key,
                "no_progress_count": actor.no_progress_count,
                "recovery_attempts": actor.recovery_attempts,
                "recorded_at": now(),
            },
        )
        previous.checkpoint["fingerprint"] = digest(
            {k: v for k, v in previous.checkpoint.items() if k != "fingerprint"}
        )
        actor.no_progress_count = 0
    target = actor.sessions.setdefault(desired.key, desired)
    if short_planning(project, actor):
        save_checkpoint(project, actor, target)
    actor.session_key = target.key
    actor.thread_id, actor.history_turn_id = target.thread_id, target.history_turn_id
    actor.context_epoch += 1


def _completed_lookahead(project: StudioProject, actor: Actor) -> bool:
    """Determine completion from the saved draft, never native goal status."""
    from .planning_sessions import enabled as short_planning

    session = actor.sessions.get(actor.session_key)
    if (
        not short_planning(project, actor)
        or actor.paused
        or actor.native_session_blocked
        or (actor.status not in {"running", "ready", "waiting"})
        or (not session)
        or (session.purpose != "planning")
        or (session.planning_kind != "lookahead")
    ):
        return False
    clip = project.clips.get(session.planning_clip_id or "")
    preparation = clip.preparation if clip else None
    return not (
        not clip
        or clip.set_id != actor.set_id
        or not preparation
        or preparation.actor_id != actor.id
        or not preparation.enabled
        or preparation.user_disabled
        or preparation.status != "draft"
        or preparation.stable_status != "ready"
        or preparation.pending_requirements
    )


def handoff_completed_lookahead(project: StudioProject, actor: Actor) -> bool:
    """Leave a saved ready draft at a successful turn boundary, without recovery."""
    if not _completed_lookahead(project, actor):
        return False
    if desired_session(project, actor).key == actor.session_key:
        return False
    activate_session(project, actor)
    actor.status = "ready"
    actor.request_wake()
    actor.summary = "已保存预准备草稿，按当前业务记录切换后续会话"
    return True


def assignment(project: StudioProject, actor: Actor) -> dict[str, Any] | None:
    session = actor.sessions.get(actor.session_key)
    if not session or session.purpose in {"planning", "executive_production"}:
        return None
    candidate = project.candidates[str(session.candidate_id)]
    if session.purpose == "candidate_review":
        from .image_input_recovery import available_evidence, review_gaps

        required = available_evidence(project, candidate)
        return {
            "purpose": session.purpose,
            "candidate_id": candidate.id,
            "clip_id": candidate.clip_id,
            "analysis_job_id": candidate.analysis.get("request_id"),
            "image_budget": CANDIDATE_IMAGE_LIMIT,
            "batch_read_limit": REVIEW_IMAGE_LIMIT,
            "images_already_returned": session.image_count,
            "required_review_media_ids": required,
            "coverage_gaps": [
                gap.model_dump(mode="json") for gap in review_gaps(project, candidate)
            ],
            "remaining_media_ids": [i for i in required if i not in session.viewed_media_ids],
            "evidence_pages": candidate.analysis.get("evidence_pages", []),
            "reference_media_ids": candidate.analysis.get("review_reference_media_ids", []),
            "end_state": candidate.confirmed_end_state.model_dump(mode="json")
            if candidate.confirmed_end_state
            else None,
        }
    batches = active_batches(candidate)
    batch = next((b for b in batches if b.id == session.batch_id), None)
    return {
        "purpose": session.purpose,
        "candidate_id": candidate.id,
        "clip_id": candidate.clip_id,
        "analysis_job_id": candidate.analysis.get("request_id"),
        "batch": batch.model_dump(mode="json") if batch else None,
        "image_budget": REVIEW_IMAGE_LIMIT if batch else 0,
        "images_already_returned": session.image_count,
        "completed_batches": [b.model_dump(mode="json") for b in batches if b.status == "complete"],
        "pending_batch_ids": [b.id for b in batches if b.status != "complete"],
        "end_state": candidate.confirmed_end_state.model_dump(mode="json")
        if candidate.confirmed_end_state
        else None,
    }


def reserve_image(
    project: StudioProject,
    actor: Actor,
    media_id: str,
    *,
    reused: bool = False,
    commit: bool = True,
) -> None:
    """Count newly delivered images; read_media reuses same-context image receipts."""

    if actor.role in {"task_analyst", "material_executor"}:
        from .delegation import allowed
        from .visual_analysis import VISUAL_REQUEST_IMAGE_LIMIT

        require(allowed(project, actor, "media", media_id), "图片不属于专项任务范围")
        delegate_session = actor.sessions.setdefault(
            actor.session_key, ActorSession(key=actor.session_key)
        )
        require(
            reused or delegate_session.image_count < VISUAL_REQUEST_IMAGE_LIMIT,
            "专项任务已达服务端图片容量；报告现有证据和未确认范围，不重启绕过",
        )
        if commit:
            delegate_session.image_count += int(not reused)
            if media_id not in delegate_session.viewed_media_ids:
                delegate_session.viewed_media_ids.append(media_id)
        return
    if actor.role == "visual_analyst":
        from .visual_analysis import VISUAL_REQUEST_IMAGE_LIMIT, task_for

        task = task_for(project, actor)
        visual_session = actor.sessions.setdefault(
            actor.session_key, ActorSession(key=actor.session_key)
        )
        require(media_id in task.media_ids, "图片不属于本候选的委托范围")
        require(
            reused or visual_session.image_count < VISUAL_REQUEST_IMAGE_LIMIT,
            "已达到服务端 50 张图片容量，请依据已有画面完成分析并如实记录未审范围；"
            "重复读图不能扩充容量，不能重新启动同一超大任务",
        )
        if commit:
            visual_session.image_count += int(not reused)
            if media_id not in visual_session.viewed_media_ids:
                visual_session.viewed_media_ids.append(media_id)
        return
    if actor.role != "set_director":
        return
    session = actor.sessions.get(actor.session_key)
    if session and session.purpose == "candidate_review":
        candidate = project.candidates[str(session.candidate_id)]
        require(candidate.status == "review", "当前候选审核已结束，请返回规划会话")
        require(
            media_id in candidate.analysis.get("required_review_media_ids", []),
            "本审核会话只查看当前候选、前段接续及本次参考素材的证据",
        )
        if commit:
            session.image_count += int(not reused)
            if media_id not in session.viewed_media_ids:
                session.viewed_media_ids.append(media_id)
        return
    if session and session.purpose != "planning":
        require(
            session.purpose == "review_batch", "汇总会话不加载图片；需要回看时 request_review_batch"
        )
        candidate = project.candidates[str(session.candidate_id)]
        batch = next((b for b in active_batches(candidate) if b.id == session.batch_id), None)
        require(batch and batch.status == "pending", "本批审核已结束，请切换到下一审核会话")
        assert batch is not None
        require(media_id in batch.media_ids, "该图片不属于本批证据；需要其他画面时另建复查批次")
        require(
            reused or session.image_count < REVIEW_IMAGE_LIMIT,
            "本审核上下文已达 6 张图片；请完成本批或换新会话复查",
        )
        if commit:
            session.image_count += int(not reused)
            if media_id not in session.viewed_media_ids:
                session.viewed_media_ids.append(media_id)
    else:
        for candidate in select_entities(
            project.candidates,
            parent_ids=list(project.sets[str(actor.set_id)].clip_ids),
            statuses=["review"],
        ):
            evidence = {
                page.get("media_id") for page in candidate.analysis.get("evidence_pages", [])
            }
            if candidate.status == "review" and media_id in evidence:
                require(False, "候选证据由独立审核会话查看；规划会话请读取分批结论及证据索引")
        session = actor.sessions.setdefault(actor.session_key, ActorSession(key=actor.session_key))
        if commit:
            session.image_count += int(not reused)
            if media_id not in session.viewed_media_ids:
                session.viewed_media_ids.append(media_id)


def owned_candidate(project: StudioProject, actor: Actor, identifier: str) -> Candidate:
    candidate = project.candidates.get(identifier)
    require(
        (
            actor.role == "set_director"
            and candidate
            and (project.clips[candidate.clip_id].set_id == actor.set_id)
        ),
        "只能审核当前片场的候选",
    )
    assert candidate is not None
    return candidate


def finish_batch(
    project: StudioProject, actor: Actor, data: dict[str, Any], store: StudioStore
) -> dict[str, Any]:
    candidate = owned_candidate(project, actor, str(data.get("candidate_id")))
    session = actor.sessions.get(actor.session_key)
    batch = next((b for b in active_batches(candidate) if b.id == data.get("batch_id")), None)
    require(
        batch and batch.status == "pending" and session and session.batch_id == batch.id,
        "请在分配给本批次的独立审核会话中提交结论",
    )
    assert batch is not None and session is not None
    from .image_input_recovery import coverage_limitations, quarantine, review_gaps

    available = {media_id for media_id in batch.media_ids if not quarantine(project, media_id)}
    require(
        available <= set(session.viewed_media_ids)
        and available <= store.inspected(project.id, actor.id, list(available)),
        "本批证据尚未全部实际查看，不能提前汇总结论",
    )
    reason = str(data.get("reason") or "").strip()
    require(reason, "需要记录本批画面的观察结论")
    findings = [VisualFinding.model_validate(value) for value in data.get("findings", [])]
    duration = project.media[str(candidate.media_id)].duration or 0
    for finding in findings:
        require(
            set(finding.evidence_media_ids) <= available,
            "问题证据必须来自本批实际查看的画面",
        )
        require(
            finding.start_seconds <= duration
            and (
                finding.end_seconds is None
                or finding.start_seconds <= finding.end_seconds <= duration
            ),
            "问题时间范围超出当前候选的视频时长",
        )
    if (
        candidate.analysis.get("end_frame_media_id") in available
        and not candidate.confirmed_end_state
    ):
        require(data.get("final_end_state"), "查看实际末帧的批次必须记录动作、空间、机位和人物末态")
    if data.get("final_end_state"):
        from .adjudication import confirm_end_state

        require(
            candidate.analysis.get("end_frame_media_id") in batch.media_ids,
            "只有查看实际末帧的批次可以确认末态",
        )
        confirm_end_state(project, actor, candidate, data, store, required=True)
    batch.reason, batch.findings = reason, findings
    batch.limitations = list(dict.fromkeys(str(x) for x in data.get("limitations", [])))
    batch.limitations += coverage_limitations(
        [gap for gap in review_gaps(project, candidate) if gap.media_id in batch.media_ids]
    )
    batch.status, batch.actor_id, batch.completed_at = "complete", actor.id, now()
    from .image_input_recovery import complete_image_recovery

    complete_image_recovery(actor)
    actor.summary = "本批审核已完成，切换独立上下文继续审核或汇总结论"
    actor.request_wake()
    return {"candidate_id": candidate.id, "batch_id": batch.id, "completed": True}


def request_batch(project: StudioProject, actor: Actor, data: dict[str, Any]) -> dict[str, Any]:
    candidate = owned_candidate(project, actor, str(data.get("candidate_id")))
    require(not candidate_session(candidate), "当前候选的补看和结论始终在同一个审核会话中完成")
    if data.get("restart_batch_id"):
        require(
            not any(r.status == "retrying" for r in actor.image_input_recoveries),
            "图片拒绝恢复只允许一次上下文替换；请在当前恢复会话完成剩余审核",
        )
        batch = next(
            (b for b in active_batches(candidate) if b.id == data["restart_batch_id"]), None
        )
        require(batch and batch.status == "pending", "只能为尚未完成的批次重建图片上下文")
        assert batch is not None
        require(batch.context_attempt < 3, "本批审核上下文已恢复三次，请报告具体技术故障")
        require(str(data.get("question") or "").strip(), "重建上下文需要说明原因")
        batch.context_attempt += 1
        actor.request_wake()
        return {
            "candidate_id": candidate.id,
            "batch_id": batch.id,
            "context_attempt": batch.context_attempt,
        }
    ids = list(dict.fromkeys(data.get("media_ids", [])))
    from .image_input_recovery import available_evidence

    allowed = set(available_evidence(project, candidate))
    require(
        ids and len(ids) <= REVIEW_IMAGE_LIMIT and set(ids) <= allowed,
        "复查需要指定 1–6 张本候选已有证据",
    )
    question = str(data.get("question") or "").strip()
    require(question, "需要明确本次复查的问题")
    for existing in active_batches(candidate):
        require(
            not (set(existing.media_ids) == set(ids) and existing.question == question),
            "同一画面和问题已有审核记录，请读取已有结论或说明新的疑点",
        )
    batch = ReviewBatch(
        analysis_job_id=str(candidate.analysis["request_id"]),
        media_ids=ids,
        purpose="followup",
        question=question,
    )
    candidate.review_batches.append(batch)
    actor.request_wake()
    return {"candidate_id": candidate.id, "batch_id": batch.id}


def require_complete(
    project: StudioProject,
    actor: Actor,
    candidate: Candidate,
    data: dict[str, Any],
    *,
    externally_inspected: set[str] | None = None,
) -> None:
    if candidate_session(candidate):
        if externally_inspected is not None:
            required = set(candidate.analysis.get("required_review_media_ids", []))
            if end_frame := candidate.analysis.get("end_frame_media_id"):
                required.add(end_frame)
            require(
                required and required <= externally_inspected,
                "外部候选审核必须覆盖当前候选的全部必需证据",
            )
            return
        delegated_review = actor.role == "task_analyst"
        if delegated_review:
            from .delegation import guard

            # The original specialist conversation is already isolated by its
            # active, current task authorization. Preserve its actual inspections.
            guard(project, actor, "review_candidate", {"candidate_id": candidate.id})
        session = actor.sessions.get(actor.session_key)
        require(
            session
            and (
                delegated_review
                or session.purpose == "candidate_review"
                and session.candidate_id == candidate.id
            ),
            "请在当前候选的独立审核会话中完成验收",
        )
        assert session is not None
        from .image_input_recovery import available_evidence

        required = set(available_evidence(project, candidate))
        require(
            required and required <= set(session.viewed_media_ids),
            "本候选的全部必需证据尚未在当前审核会话中实际查看",
        )
        return
    batches = active_batches(candidate)
    require(
        batches and all(b.status == "complete" for b in batches), "所有证据批次完成后才能验收候选"
    )
    if externally_inspected is None:
        session = actor.sessions.get(actor.session_key)
        require(
            session
            and session.purpose == "review_summary"
            and session.candidate_id == candidate.id,
            "请在独立汇总会话中综合全部批次，规划会话不直接验收候选",
        )
    supplied = [VisualFinding.model_validate(value) for value in data.get("findings", [])]
    assessments = [
        ReviewFindingAssessment.model_validate(value) for value in data.get("batch_assessments", [])
    ]
    indexed = {batch.id: batch for batch in batches}
    seen = set()
    for assessment in assessments:
        batch = indexed.get(assessment.batch_id)
        key = (assessment.batch_id, assessment.finding_index)
        require(
            batch and assessment.finding_index < len(batch.findings) and key not in seen,
            "分批问题复核索引无效或重复",
        )
        seen.add(key)
        checked = {
            media_id
            for other in batches
            if other.status == "complete"
            for media_id in other.media_ids
        }
        from .image_input_recovery import quarantine

        checked = (
            checked & externally_inspected
            if externally_inspected is not None
            else {media_id for media_id in checked if not quarantine(project, media_id)}
        )
        require(
            set(assessment.evidence_media_ids) <= checked, "分批问题复核需要已完成审核的实际画面"
        )
        if assessment.conclusion == "dismissed":
            followup = {
                media_id
                for other in batches
                if other.purpose == "followup"
                for media_id in other.media_ids
            }
            require(
                set(assessment.evidence_media_ids) & followup,
                "推翻分批问题需要独立复查画面和具体依据",
            )
    for batch in batches:
        for index, finding in enumerate(batch.findings):
            dismissed = any(
                a.batch_id == batch.id and a.finding_index == index and a.conclusion == "dismissed"
                for a in assessments
            )
            require(
                finding in supplied or dismissed,
                "汇总不能丢弃分批记录的问题；需先通过复查说明和更新结论",
            )


def guard_action(project: StudioProject, actor: Actor, action: str, data: dict[str, Any]) -> None:
    session = actor.sessions.get(actor.session_key)
    from .planning_sessions import enabled as short_planning

    if short_planning(project, actor) and session and session.purpose == "planning":
        target_clip = data.get("clip_id")
        if data.get("reference_plan_id") in project.reference_plans:
            target_clip = project.reference_plans[data["reference_plan_id"]].clip_id
        if session.planning_clip_id and target_clip:
            require(
                target_clip == session.planning_clip_id,
                "本规划会话仅处理分配的 Clip；等待调度切换",
            )
        if session.planning_kind == "lookahead":
            require(
                action
                in {
                    "save_clip_preparation",
                    "prepare_reference",
                    "propose_global",
                    "write_memory",
                    "set_status",
                    "update_clip_context",
                },
                "预准备会话只保存可修改的稳定草稿；不能登记修正、定稿或提交视频",
            )
            if action == "write_memory":
                require(
                    data.get("entry", {}).get("basis") != "observed",
                    "预准备不能把尚未选用的前段写成已观察事实",
                )
    if not session or session.purpose in {"planning", "executive_production"}:
        return
    if (
        action == "plan_clips"
        and data.get("mode") == "append_unplanned_suffix"
        and data.get("clips") == []
        and data.get("complete") is True
    ):
        from .planning_sessions import covered_plan_work

        if covered_plan_work(project, actor) == "confirmation":
            # This only confirms existing coverage; it cannot add or edit a Clip.
            # The service still checks the actor's current planning/Set read token.
            return
    if session.purpose == "candidate_review":
        require(
            action not in {"finish_review_batch", "request_review_batch"},
            "当前候选在同一会话中审核；直接查看证据或 prepare_review 补抽，再 review_candidate",
        )
    require(
        action
        in {
            "finish_review_batch",
            "request_review_batch",
            "review_candidate",
            "confirm_candidate_state",
            "analyze_candidate",
            "set_status",
        },
        "独立审核会话只处理当前候选；规划、修正提示词和提交生成由规划会话继续",
    )
    if action != "set_status":
        require(data.get("candidate_id") == session.candidate_id, "审核操作必须指向当前分配的候选")


REVIEW_TOOLS = {
    "get_review_work_packet",
    "get_project",
    "get_required_context",
    "get_detail",
    "batch_get_details",
    "read_media",
    "batch_read_media",
    "get_history",
    "get_operation_receipt",
    "finish_review_batch",
    "request_review_batch",
    "review_candidate",
    "confirm_candidate_state",
    "analyze_candidate",
    "set_status",
}


def guard_read(project: StudioProject, actor: Actor, name: str, args: dict[str, Any]) -> None:
    from .planning_sessions import assignment as planning_assignment

    planning = planning_assignment(project, actor)
    if planning and planning["clip_id"] and name == "get_clip_work_packet":
        require(args.get("clip_id") == planning["clip_id"], "本规划工作包只读取分配的 Clip")
    current = assignment(project, actor)
    if current is None:
        return
    if current["purpose"] == "candidate_review":
        require(
            name not in {"finish_review_batch", "request_review_batch"},
            "当前候选使用单审核会话，无需创建或汇总其他审核批次",
        )
        if name in {"read_media", "batch_read_media"}:
            require(not args.get("original"), "审核图片统一使用 JPEG 95，不提供 PNG 回读")
    require(
        name in REVIEW_TOOLS,
        "独立审核会话仅加载当前候选及必要证据，请通过 get_required_context 读取",
    )
    candidate_id = str(current["candidate_id"])
    clip_id = str(current["clip_id"])
    if name == "get_review_work_packet":
        require(
            (args.get("candidate_id") == candidate_id),
            "审核工作包仅用于已启用新流程的当前候选会话",
        )
    if name == "get_required_context":
        from .planning_sessions import covered_plan_work

        own_plan_confirmation = (
            args.get("phase") == "planning"
            and args.get("target_id") == actor.set_id
            and covered_plan_work(project, actor) == "confirmation"
        )
        require(
            own_plan_confirmation
            or (args.get("phase") == "review" and args.get("target_id") == candidate_id),
            "审核上下文必须对应当前候选；已覆盖计划可读取本片场确认凭据",
        )
    elif name in {"get_detail", "batch_get_details"}:
        scopes = [args.get("scope")] if name == "get_detail" else args.get("scopes", [])
        allowed = {
            f"candidate:{candidate_id}",
            f"clip:{clip_id}",
            f"actor:{actor.id}",
            "screenplay",
        }
        if current["purpose"] == "candidate_review":
            # Focused sampling wakes this same review. Its exact job result is
            # part of the assigned evidence, not another director's private work.
            allowed.add(f"job:{project.candidates[candidate_id].job_id}")
            allowed.update(
                f"job:{job.id}" for job in select_entities(project.jobs, parent_id=candidate_id)
            )
        require(
            all(scope in allowed for scope in scopes),
            "本审核会话只读取当前角色、当前候选、其任务、Clip 和确认剧本；素材信息在必读上下文中",
        )
    elif name == "get_history":
        require(
            args.get("kind") == "jobs" and args.get("parent_id") == candidate_id,
            "审核历史仅限当前候选的任务记录",
        )


def actor_instruction_detail(project: StudioProject, actor: Actor) -> dict[str, Any]:
    """Return the complete current instruction without unrelated actor history."""
    session = actor.sessions.get(actor.session_key)
    return {
        "scope": f"actor:{actor.id}",
        "actor_id": actor.id,
        "session_key": actor.session_key,
        "context_epoch": actor.context_epoch,
        "session_context_epoch": session.context_epoch if session else None,
        "total_chars": len(actor.goal),
        "project": {
            "id": project.id,
            "revision": project.revision,
            "actors": {
                actor.id: {
                    "id": actor.id,
                    "role": actor.role,
                    "set_id": actor.set_id,
                    "goal": actor.goal,
                    "session_key": actor.session_key,
                    "context_epoch": actor.context_epoch,
                }
            },
        },
    }


def actor_instruction_packet(actor: Actor) -> dict[str, Any]:
    """Project the exact durable goal; keep large goals in existing actor details."""
    return {
        "actor_id": actor.id,
        "session_key": actor.session_key,
        "total_chars": len(actor.goal),
        "goal": actor.goal if len(actor.goal) <= 3000 else None,
        "detail_request": {"scope": f"actor:{actor.id}"} if len(actor.goal) > 3000 else None,
    }


def overview(project: StudioProject, actor: Actor) -> dict[str, Any]:
    return {
        "id": project.id,
        "title": project.title,
        "workflow_version": project.workflow_version,
        "harness_version": project.harness_version,
        "actor_id": actor.id,
        "role": actor.role,
        "review_assignment": assignment(project, actor),
        "actor_instructions": actor_instruction_packet(actor),
        "instruction": (
            "先读取当前候选的完整 review 必读上下文和分配任务；"
            "candidate_review 在同一会话读完全部证据并判定，历史批次遵循原任务。"
        ),
    }
