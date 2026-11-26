"""Executive-delegated visual work with independent durable actors and evidence."""

from __future__ import annotations

from typing import Any

from .contracts import Actor, StudioProject, VisualAnalysisTask, VisualFinding, new_id, now
from .memory import digest, require
from .repository import select_entities
from .store import StudioStore

VISUAL_REQUEST_IMAGE_LIMIT = 50


def candidate_requests(project: StudioProject, request: dict[str, Any]) -> list[dict[str, Any]]:
    """Fan out versions, keeping only each candidate's already prepared seam evidence."""
    identifiers = list(dict.fromkeys(request.get("candidate_ids", [])))
    for identifier in identifiers:
        require(identifier in project.candidates, "委托候选不存在")
    # A selected predecessor supplied alongside its successors is context, not
    # another full video to expand. Each successor already owns its tail sheets.
    predecessors = {project.candidates[i].previous_candidate_id for i in identifiers}
    primary = [i for i in identifiers if i not in predecessors]
    if not primary:
        return [request]
    evidence = {
        i: set(project.candidates[i].analysis.get("required_review_media_ids", []))
        for i in identifiers
    }
    all_evidence = set().union(*evidence.values())
    result = []
    for identifier in primary:
        extras = request.get("media_ids", [])
        result.append(
            {
                **request,
                "candidate_ids": [identifier],
                "media_ids": [
                    i for i in extras if i not in all_evidence or i in evidence[identifier]
                ],
            }
        )
    return result


def task_for(project: StudioProject, actor: Actor) -> VisualAnalysisTask:
    task = project.visual_tasks.get(actor.visual_task_id or "")
    require(
        actor.role == "visual_analyst" and task and task.actor_id == actor.id,
        "看图任务不属于此子对话",
    )
    assert task is not None
    return task


def media_fingerprint(project: StudioProject, ids: list[str]) -> str:
    return digest([[i, project.media[i].sha256] for i in sorted(ids)])


def delegate(project: StudioProject, actor: Actor, data: dict[str, Any]) -> dict[str, Any]:
    require((actor.role == "executive"), "只有新流程总导演可委托看图")
    requests = data.get("tasks", [])
    require(
        isinstance(requests, list) and bool(requests),
        "需登记至少一项独立看图任务",
    )
    from .image_input_recovery import available_evidence, quarantine, review_gaps

    group_id = new_id()
    tasks = []
    expanded = [part for request in requests for part in candidate_requests(project, request)]
    for request in expanded:
        question = str(request.get("question", "")).strip()
        require(question, "需明确子对话要核查的问题")
        candidates = list(dict.fromkeys(request.get("candidate_ids", [])))
        references = list(dict.fromkeys(request.get("revision_ids", [])))
        media_ids = list(dict.fromkeys(request.get("media_ids", [])))
        limitations: list[str] = []
        for identifier in candidates:
            candidate = project.candidates.get(identifier)
            require(
                candidate and candidate.media_id and candidate.analysis,
                "候选尚未准备好可查看的帧证据",
            )
            assert candidate is not None
            media_ids.extend(available_evidence(project, candidate))
            limitations.extend(
                f"候选 {identifier} 的图片 {gap.media_id} 已隔离；"
                f"未确认时间点 {gap.timestamps}：{gap.reason}"
                for gap in review_gaps(project, candidate)
            )
        for identifier in references:
            reference = project.references.get(identifier)
            require(reference and reference.media_id, "指定素材还没有图片")
            assert reference is not None and reference.media_id
            media_ids.append(reference.media_id)
        if candidates:
            permitted = set(
                project.candidates[candidates[0]].analysis.get("required_review_media_ids", [])
            )
            permitted.update(str(project.references[i].media_id) for i in references)
            require(
                set(media_ids) <= permitted,
                "候选委托只能包含本候选全程、必要前段尾部及明确指定素材；其他候选请单独委托",
            )
        media_ids = list(dict.fromkeys(media_ids))
        require(
            media_ids
            and all(
                i in project.media and project.media[i].mime_type.startswith("image/")
                for i in media_ids
            ),
            "看图任务需要本项目已生成的图片证据",
        )
        excluded = [i for i in media_ids if quarantine(project, i)]
        available = [i for i in media_ids if i not in excluded]
        require(available, "指定图片均已隔离，不能通过新子对话重送")
        require(
            len(available) <= VISUAL_REQUEST_IMAGE_LIMIT,
            f"单候选看图任务有 {len(available)} 张必需图片，超过服务端 "
            f"{VISUAL_REQUEST_IMAGE_LIMIT} 张限制；保留当前候选全程和必要接缝证据，"
            "移除其他候选、前驱整片及无关图片后重新委托，不要反复重启同一超大任务",
        )
        source_task_id = request.get("source_task_id")
        if source_task_id:
            source_task = project.visual_tasks.get(str(source_task_id))
            require(
                source_task
                and source_task.parent_actor_id == actor.id
                and source_task.status == "failed",
                "替代委托须关联本总导演已有失败任务，保留原历史",
            )
        task_id = new_id()
        child = Actor(
            role="visual_analyst",
            parent_actor_id=actor.id,
            visual_task_id=task_id,
            goal=(
                (
                    "Complete the assigned visual analysis and use "
                    "complete_visual_analysis to return conclusions, issues, "
                    "timestamps, viewed evidence and unknown ranges. "
                )
                + question
            ),
            summary="Waiting for visual-analysis dispatch",
        )
        task = VisualAnalysisTask(
            id=task_id,
            parent_actor_id=actor.id,
            actor_id=child.id,
            question=question,
            candidate_ids=candidates,
            revision_ids=references,
            media_ids=available,
            group_id=group_id,
            source_task_id=source_task_id,
            limitations=[*limitations, *(f"图片 {i} 已隔离，未查看" for i in excluded)],
        )
        project.visual_tasks[task.id] = task
        project.actors[child.id] = child
        tasks.append(
            {
                "task_id": task.id,
                "actor_id": child.id,
                "status": task.status,
                "candidate_ids": candidates,
                "image_count": len(available),
                "group_id": group_id,
            }
        )
    return {"tasks": tasks, "summary": "独立看图任务已登记，按暂停和就绪条件调度；结果返回总导演"}


def packet(project: StudioProject, actor: Actor) -> list[dict[str, Any]]:
    task = task_for(project, actor)
    from .image_input_recovery import quarantine
    from .review_context import review_context_records

    records: list[dict[str, Any]] = [
        {"type": "visual_assignment", "value": task.model_dump(mode="json")}
    ]
    for identifier in task.candidate_ids:
        records.extend(review_context_records(project, project.candidates[identifier]))
    for identifier in task.revision_ids:
        reference = project.references[identifier]
        state = project.states[reference.state_id]
        records.extend(
            [
                {
                    "type": "review_reference",
                    "value": reference.model_dump(exclude={"review_history"}, mode="json"),
                },
                {
                    "type": "state",
                    "value": state.model_dump(
                        exclude={"repair_episodes", "revision_ids"}, mode="json"
                    ),
                },
                {
                    "type": "asset",
                    "value": project.assets[state.asset_id].model_dump(
                        exclude={"voice_references"}, mode="json"
                    ),
                },
            ]
        )
    records.append(
        {
            "type": "available_images",
            "media_ids": [i for i in task.media_ids if not quarantine(project, i)],
        }
    )
    # Several candidates share exactly the same constraints/assets. Keep distinct
    # state versions, plans and submitted inputs; only identical records collapse.
    from .context_projection import compact_records

    return compact_records(records)


def complete(
    project: StudioProject, actor: Actor, data: dict[str, Any], store: StudioStore
) -> dict[str, Any]:
    task = task_for(project, actor)
    require(task.status in {"queued", "running"}, "看图任务已完成")
    require(data.get("task_id") == task.id, "需提交本子对话的任务 ID")
    require(str(data.get("conclusion", "")).strip(), "需记录分析结论")
    from .image_input_recovery import complete_image_recovery, inspected_evidence, quarantine

    viewed = inspected_evidence(project, actor, task.media_ids, store)
    viewed = {i for i in viewed if not quarantine(project, i)}
    require(viewed, "必须实际查看至少一张当前任务图片，不能只复述计划")
    findings = [VisualFinding.model_validate(item) for item in data.get("findings", [])]
    require(
        all(
            set(f.evidence_media_ids) <= viewed
            and (f.end_seconds is None or f.end_seconds >= f.start_seconds)
            for f in findings
        ),
        "每项问题必须引用此子对话实际查看的画面证据",
    )
    limitations = list(dict.fromkeys([*task.limitations, *data.get("limitations", [])]))
    missing = set(task.media_ids) - viewed
    require(not missing or limitations, "部分图片未看，须明确未确认范围与判断限制")
    limitations.extend(f"未查看图片 {i}" for i in sorted(missing))
    task.conclusion, task.findings = str(data["conclusion"]), findings
    task.viewed_media_ids = sorted(viewed)
    task.limitations = list(dict.fromkeys(limitations))
    task.evidence_fingerprint = media_fingerprint(project, task.viewed_media_ids)
    task.status, task.completed_at = "complete", now()
    complete_image_recovery(actor)
    actor.status, actor.summary, actor.wake_requested = "complete", "看图分析已交回总导演", False
    parent = project.actors[task.parent_actor_id]
    if not parent.native_session_blocked:
        parent.request_wake()
        parent.summary = "看图子对话已返回证据与结论，等待总导演决策"
        if not parent.paused and parent.status != "running":
            parent.status = "ready"
    return {
        "task_id": task.id,
        "conclusion": task.conclusion,
        "viewed_media_ids": task.viewed_media_ids,
        "limitations": task.limitations,
    }


def fail(
    project: StudioProject,
    actor: Actor,
    reason: str,
    store: StudioStore,
    *,
    unconfirmed_media_ids: list[str] | None = None,
) -> None:
    """Close a failed assignment with its actual partial inspection, not a false verdict."""
    task = task_for(project, actor)
    if task.status == "complete":
        return
    from .image_input_recovery import inspected_evidence, quarantine
    from .service import wake_executive

    viewed = inspected_evidence(project, actor, task.media_ids, store)
    task.viewed_media_ids = sorted(
        i for i in viewed if not quarantine(project, i) and i not in (unconfirmed_media_ids or [])
    )
    task.evidence_fingerprint = media_fingerprint(project, task.viewed_media_ids)
    task.limitations = list(
        dict.fromkeys(
            [
                *task.limitations,
                reason,
                *(f"未确认图片 {i}" for i in task.media_ids if i not in task.viewed_media_ids),
            ]
        )
    )
    task.status, task.completed_at = "failed", now()
    wake_executive(project, "看图子对话未完成，保留已看证据与未确认范围：" + task.id)


def delegated_evidence(
    project: StudioProject, actor: Actor, ids: list[str], store: StudioStore
) -> set[str]:
    if actor.role != "executive":
        return set()
    from .image_input_recovery import quarantine

    result: set[str] = set()
    requested = set(ids)
    for task in select_entities(
        project.visual_tasks,
        statuses=["complete"],
        parent_id=actor.id,
        viewed_media_ids_any=ids,
    ):
        if task.parent_actor_id != actor.id or not requested.intersection(task.viewed_media_ids):
            continue
        if any(i not in project.media for i in task.viewed_media_ids) or (
            media_fingerprint(project, task.viewed_media_ids) != task.evidence_fingerprint
        ):
            continue
        inspected = store.inspected(project.id, task.actor_id, task.viewed_media_ids)
        result.update(
            i
            for i in requested.intersection(task.viewed_media_ids, inspected)
            if not quarantine(project, i)
        )
    return result


def delegation_receipts(
    project: StudioProject, actor: Actor, ids: list[str], store: StudioStore
) -> list[str]:
    if actor.role != "executive":
        return []
    valid = delegated_evidence(project, actor, ids, store)
    receipts = [
        task.id
        for task in select_entities(
            project.visual_tasks,
            statuses=["complete"],
            parent_id=actor.id,
            viewed_media_ids_any=list(valid),
        )
        if task.parent_actor_id == actor.id
        and set(valid).intersection(task.viewed_media_ids)
        and not any(i not in project.media for i in task.viewed_media_ids)
        and media_fingerprint(project, task.viewed_media_ids) == task.evidence_fingerprint
    ]
    from .delegation import evidence

    valid_delegated = evidence(project, actor, ids, store)
    receipts.extend(
        task.id
        for task in select_entities(
            project.delegate_tasks,
            parent_id=actor.id,
            statuses=["complete", "blocked"],
            viewed_media_ids_any=list(valid_delegated),
        )
        if set(task.viewed_media_ids).intersection(valid_delegated)
    )
    return receipts
