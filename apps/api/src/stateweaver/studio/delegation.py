"""Scoped production delegates, durable notifications and executive-owned decisions.

The same capability contract drives discovery, MCP and service authorization. A
delegate is a real production actor, not a prompt that impersonates an executive.
"""

from __future__ import annotations

import base64
import json
import time
from collections import defaultdict
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any, Literal

from .contracts import (
    Actor,
    DelegateApproval,
    DelegateReport,
    DelegateRequest,
    DelegateScope,
    DelegateTask,
    ExecutiveDocument,
    ExecutiveDocumentVersion,
    ExecutiveInboxItem,
    Job,
    ReferenceReplacementGrant,
    new_id,
    now,
)
from .memory import digest, memory_record, require
from .repository import select_entities
from .store import StudioError

if TYPE_CHECKING:
    from .contracts import StudioProject
    from .service import StudioService
    from .store import StudioStore

HANDOFF_STATE_FINGERPRINT_PREFIX = "handoff-state-v2:"

DELEGATE_ROLES = frozenset({"task_analyst", "material_executor"})
READ_TOOLS = frozenset(
    {
        "get_project",
        "get_required_context",
        "get_detail",
        "get_history",
        "get_body",
        "read_media",
        "batch_read_media",
        "batch_get_details",
        "get_operation_receipt",
    }
)
RECORD_READ_TOOLS = frozenset(
    {
        "get_project",
        "get_required_context",
        "get_detail",
        "get_history",
        "get_body",
        "batch_get_details",
        "get_operation_receipt",
    }
)
RECORD_REPORT_LIMITATION = (
    "仅核验静态契约、版本和选用关系元数据；未查看或认证图片、视频、事件或资产状态。"
    "此报告只可采纳记录，不能作为视觉检查、事件认证或状态变更的依据。"
)
ANALYST_ACTIONS = frozenset({"complete_delegate_task", "prepare_media", "set_status"})
MATERIAL_ACTIONS = ANALYST_ACTIONS | {
    "generate_reference",
    "review_reference",
    "select_reference",
}
SCOPE_FIELDS = {
    "assets": "asset_ids",
    "states": "state_ids",
    "sets": "set_ids",
    "clips": "clip_ids",
    "candidates": "candidate_ids",
    "references": "revision_ids",
    "media": "media_ids",
    "proposals": "proposal_ids",
    "story_events": "event_ids",
    "memory_entries": "memory_ids",
    "jobs": "job_ids",
    "reference_plans": "reference_plan_ids",
}
TASK_NAMES = {
    "candidate_review": "Candidate review",
    "repair_diagnosis": "Repair diagnosis",
    "material_preparation": "Material preparation and repair support",
    "handoff_verification": "Observed handoff verification",
    "revision_impact": "Revision impact analysis",
    "state_conflict": "State conflict analysis",
}


def enabled(project: StudioProject) -> bool:
    return project.director_mode == "delegated-v1"


def task_for(project: StudioProject, actor: Actor) -> DelegateTask:
    task = project.delegate_tasks.get(actor.delegate_task_id or "")
    require(
        actor.role in DELEGATE_ROLES and task and task.actor_id == actor.id,
        "专项任务不属于此子对话",
    )
    assert task is not None
    return task


def usage_fingerprint(project: StudioProject, revision_id: str) -> str:
    """New usages invalidate replacement permission without scanning other history."""
    return digest(
        {
            "candidates": [
                [
                    c.id,
                    digest(project.jobs[c.job_id].request),
                    c.status,
                    project.clips[c.clip_id].accepted_candidate_id,
                ]
                for c in select_entities(project.candidates, binding_revision=revision_id)
            ],
            "pending": pending_configurations(project, revision_id),
        }
    )


def pending_configurations(project: StudioProject, revision_id: str) -> list[dict[str, Any]]:
    result = [
        {
            "kind": "reference-plan",
            "id": r.id,
            "clip_id": r.clip_id,
            "fingerprint": digest(r.model_dump(mode="json")),
        }
        for r in select_entities(project.reference_plans, planned_revision=revision_id)
        if r.status not in {"submitted", "superseded"}
    ]
    result.extend(
        {
            "kind": "clip-preparation",
            "id": c.preparation.id,
            "clip_id": c.id,
            "fingerprint": digest(c.preparation.model_dump(mode="json")),
        }
        for c in select_entities(project.clips, prepared_revision=revision_id)
        if c.preparation and c.preparation.enabled and c.preparation.status != "consumed"
    )
    return sorted(result, key=lambda r: (r["kind"], r["id"]))


def stable_target(
    project: StudioProject, state_id: str, *, include_view_catalog: bool = True
) -> str:
    state = project.states[state_id]
    asset = project.assets[state.asset_id]
    asset_exclude = {"state_ids", "voice_references", "selected_voice_id"}
    if not include_view_catalog:
        asset_exclude.add("required_views")
    return digest(
        [
            state.model_dump(
                exclude={
                    "selected_revisions",
                    "revision_ids",
                    "repair_episodes",
                    "preparation_history",
                    "evolution",
                    "evolution_history",
                }
            ),
            asset.model_dump(exclude=asset_exclude),
        ]
    )


def source_fingerprint(
    project: StudioProject, kind: str, identifier: str, *, handoff_state: bool = False
) -> str:
    if kind == "states":
        # Exact selected sources have their own reference/media fingerprints.
        require(identifier in project.states, "授权的状态来源不存在")
        if handoff_state:
            # Adding a published view does not change already inspected footage.
            # Version the comparison so old tasks are never silently rebaselined.
            return HANDOFF_STATE_FINGERPRINT_PREFIX + stable_target(
                project, identifier, include_view_catalog=False
            )
        return stable_target(project, identifier)
    if kind == "memory_entries":
        record = memory_record(project, identifier)
        require(record, "授权的记忆来源不存在")
        assert record is not None
        return digest(record[1].model_dump(mode="json"))
    value = getattr(project, kind).get(identifier)
    require(value, f"授权的来源不存在：{kind}:{identifier}")
    if kind == "references":
        media = project.media.get(str(value.media_id))
        state = project.states[value.state_id]
        return digest(
            [
                value.id,
                value.state_id,
                value.view,
                value.media_id,
                media.sha256 if media else None,
                value.status,
                state.selected_revisions.get(value.view),
            ]
        )
    if kind == "candidates":
        return digest(
            [
                value.id,
                digest(project.jobs[value.job_id].request),
                value.media_id,
                project.clips[value.clip_id].accepted_candidate_id,
                project.media[str(value.media_id)].sha256 if value.media_id else None,
            ]
        )
    if kind == "sets":
        return digest(
            value.model_dump(
                include={
                    "id",
                    "story_goal",
                    "entry_state",
                    "exit_state",
                    "asset_ids",
                    "story_event_ids",
                    "dependency_revision",
                    "asset_handoffs",
                },
                mode="json",
            )
        )
    return digest(
        value.model_dump(
            mode="json",
            exclude={"material_requests_acknowledged_hash"} if kind == "proposals" else set(),
        )
    )


def stale_reasons(
    project: StudioProject, task: DelegateTask, *, refresh_usage: bool = False
) -> list[str]:
    result = []
    for identifier, expected in task.target_fingerprints.items():
        if identifier not in project.states or stable_target(project, identifier) != expected:
            result.append(f"state:{identifier} 的授权目标、适用范围或准备来源已变化")
    for key, expected in task.source_fingerprints.items():
        kind, identifier = key.split(":", 1)
        if (
            kind == "sets"
            and task.request.kind == "handoff_verification"
            and identifier in task.request.scope.set_ids
        ):
            # Completion checks the current target handoffs/required attributes.
            # Their publication revision is not a change to inspected source footage.
            if identifier not in project.sets:
                result.append(f"{key} 的目标片场已不存在")
            continue
        handoff_target = (
            task.request.kind == "handoff_verification"
            and identifier in task.request.scope.clip_ids
        )
        if (
            kind == "clips"
            and task.request.kind
            in {
                "candidate_review",
                "repair_diagnosis",
                "revision_impact",
                "material_preparation",
                "handoff_verification",
            }
            and (
                handoff_target
                if task.request.kind == "handoff_verification"
                else task.allowed_scope.clip_ids == [identifier]
            )
        ):
            # A downstream draft is editable; it is not the task's actual source.
            # Keep Clip ownership and exact submitted/reference/media sources fenced.
            clip = project.clips.get(identifier)
            candidate_matches = True
            if task.request.kind == "candidate_review" or (
                task.request.kind == "repair_diagnosis" and task.request.scope.candidate_ids
            ):
                candidate_id = next(iter(task.request.scope.candidate_ids), "")
                candidate = project.candidates.get(candidate_id)
                candidate_matches = bool(candidate and candidate.clip_id == identifier)
            allowed_sets = (
                task.request.scope.set_ids if handoff_target else task.allowed_scope.set_ids
            )
            if clip and clip.set_id in allowed_sets and candidate_matches:
                continue
            result.append(f"{key} 的候选或片场归属已变化")
            continue
        try:
            current = source_fingerprint(
                project,
                kind,
                identifier,
                handoff_state=(
                    kind == "states"
                    and task.request.kind == "handoff_verification"
                    and expected.startswith(HANDOFF_STATE_FINGERPRINT_PREFIX)
                ),
            )
        except StudioError:
            current = None
        if current != expected:
            result.append(f"{key} 的准确来源或版本已变化")
    for identifier, expected in task.usage_fingerprints.items():
        if task.request.kind == "revision_impact" and (task.status != "complete" or refresh_usage):
            # Analysts read and report the current index. Completed reports still
            # expire before approval/replacement if an actual use changes.
            continue
        if usage_fingerprint(project, identifier) != expected:
            result.append(f"reference:{identifier} 的实际使用范围已变化")
    return result


def allowed(project: StudioProject, actor: Actor, kind: str, identifier: str) -> bool:
    task = task_for(project, actor)
    if kind == "actors":
        return identifier == actor.id
    if kind == "delegate_tasks":
        return identifier == task.id
    if kind == "transcript":
        item = project.transcript.get(identifier)
        return bool(item and item.actor_id == actor.id)
    if kind == "references" and identifier in task.output_revision_ids:
        return True
    if kind == "jobs":
        job = project.jobs.get(identifier)
        if job and job.actor_id == actor.id and identifier in task.output_job_ids:
            return True
    if kind == "media":
        media = project.media.get(identifier)
        if identifier in task.output_media_ids:
            return True
        if media and media.source_revision_id in task.output_revision_ids:
            return True
        if any(project.references[r].media_id == identifier for r in task.output_revision_ids):
            return True
        if any(project.jobs[j].result.get("media_id") == identifier for j in task.output_job_ids):
            return True
    field = SCOPE_FIELDS.get(kind)
    return bool(field and identifier in getattr(task.allowed_scope, field))


def actions_for(project: StudioProject, actor: Actor) -> frozenset[str]:
    task = task_for(project, actor)
    actions = frozenset(task.allowed_actions)
    return actions & {"complete_delegate_task", "set_status"} if records_only(task) else actions


def records_only_authorized(
    kind: str, parent_actor_id: str, version: int, followups: Sequence[dict[str, Any]]
) -> bool:
    """Use an owner-issued version grant; never infer permission from question text."""
    return kind == "state_conflict" and any(
        item.get("evidence_authorization", {}).get("evidence_policy") == "records-only"
        and item["evidence_authorization"].get("task_version") == version
        and item["evidence_authorization"].get("authorized_by") == parent_actor_id
        for item in followups
        if isinstance(item.get("evidence_authorization"), dict)
    )


def records_only(task: DelegateTask) -> bool:
    return records_only_authorized(
        task.request.kind, task.parent_actor_id, task.version, task.followups
    )


def evidence_authorization(actor: Actor, version: int, policy: str) -> dict[str, Any]:
    return {
        "evidence_policy": policy,
        "task_version": version,
        "authorized_by": actor.id,
    }


def read_tools_for(project: StudioProject, actor: Actor) -> frozenset[str]:
    return RECORD_READ_TOOLS if records_only(task_for(project, actor)) else READ_TOOLS


def execution_blockers(project: StudioProject, actor: Actor) -> list[str]:
    task = task_for(project, actor)
    parent = project.actors.get(task.parent_actor_id)
    blockers = ["总导演已暂停或不存在"] if not parent or parent.paused else []
    blockers.extend(stale_reasons(project, task))
    for identifier in task.request.depends_on_task_ids:
        prerequisite = project.delegate_tasks.get(identifier)
        if not prerequisite or prerequisite.status != "complete":
            blockers.append(f"前置任务 {identifier} 尚未完成，负责人 {task.parent_actor_id}")
    return blockers


def progressing_tasks(project: StudioProject, parent_id: str) -> list[DelegateTask]:
    """Only healthy, current delegations justify waiting; scope is still explicit."""
    from .runtime_decisions import source_actor_can_progress

    tasks = {
        task.id: task
        for task in select_entities(
            project.delegate_tasks, parent_id=parent_id, statuses=["queued", "running"]
        )
    }
    parent = project.actors.get(parent_id)
    if not parent or parent.paused:
        return []
    healthy: set[str] = set()

    def usable(task: DelegateTask, ancestors: frozenset[str]) -> bool:
        if task.id in ancestors:
            return False  # A dependency cycle is a real coordination issue.
        if task.id in healthy:
            return True
        if not source_actor_can_progress(project, task.actor_id):
            return False
        if stale_reasons(project, task):
            return False
        for identifier in task.request.depends_on_task_ids:
            prerequisite = project.delegate_tasks.get(identifier)
            if not prerequisite:
                return False
            if prerequisite.status == "complete":
                continue
            if identifier not in tasks or not usable(tasks[identifier], ancestors | {task.id}):
                return False
        jobs = select_entities(
            project.jobs,
            actor_id=task.actor_id,
            statuses=["queued", "submitting", "running", "ambiguous"],
        )
        if any(job.status == "ambiguous" or job.query_outage_since for job in jobs):
            return False
        healthy.add(task.id)
        return True

    return [task for task in tasks.values() if usable(task, frozenset())]


def progressing_clip_tasks(project: StudioProject, clip_id: str) -> list[DelegateTask]:
    """Do not confuse a provenance/source Clip with the delegation's actual target."""
    result = []
    for parent in select_entities(project.actors, subtype="executive"):
        for task in progressing_tasks(project, parent.id):
            if task.request.kind not in {
                "material_preparation",
                "candidate_review",
                "repair_diagnosis",
            }:
                continue
            scope = task.request.scope
            targets = set(scope.clip_ids)
            if task.request.kind in {"candidate_review", "repair_diagnosis"}:
                targets.update(
                    project.candidates[i].clip_id
                    for i in scope.candidate_ids
                    if i in project.candidates
                )
            if clip_id in targets:
                result.append(task)
    return result


def reference_has_worker(project: StudioProject, state_id: str, view: str) -> bool:
    return any(
        task.request.kind == "material_preparation"
        and task.request.state_id == state_id
        and view in task.request.views
        for parent in select_entities(project.actors, subtype="executive")
        for task in progressing_tasks(project, parent.id)
    )


def refresh_blockers(project: StudioProject) -> bool:
    """Turn failed prerequisites and stale queued work into durable, owned notices."""
    changed = False
    for task in select_entities(project.delegate_tasks, statuses=["queued", "running", "blocked"]):
        child = project.actors[task.actor_id]
        if child.lease_owner or child.turn_id or task.report:
            continue
        if child.status == "blocked" and task.status in {"queued", "running"}:
            fail(project, child, child.last_error or child.summary or "专项子对话已技术阻塞")
            changed = True
            continue
        failed = [
            i
            for i in task.request.depends_on_task_ids
            if project.delegate_tasks[i].status in {"blocked", "failed"}
        ]
        stale = stale_reasons(project, task)
        if failed or stale:
            if task.status == "blocked":
                continue
            task.status, task.completed_at = "blocked", now()
            task.blocked_dependency_ids = failed
            task.blocker = "；".join(
                stale + [f"前置任务 {i} 未完成；负责人 {task.parent_actor_id}" for i in failed]
            )
            child.status, child.summary, child.wake_requested = "waiting", task.blocker, False
            notify(project, task, "blocked", task.blocker)
            changed = True
        elif task.status == "blocked" and task.blocked_dependency_ids:
            task.version += 1
            task.status, task.blocker, task.completed_at = "queued", None, None
            task.blocked_dependency_ids = []
            child.status, child.last_error = "ready", None
            child.request_wake()
            changed = True
    return changed


def require_replacement_impact(
    project: StudioProject, parent_id: str, grant: ReferenceReplacementGrant
) -> None:
    impact = project.delegate_tasks.get(grant.impact_task_id)
    require(
        impact
        and impact.parent_actor_id == parent_id
        and impact.request.kind == "revision_impact"
        and impact.status == "complete"
        and impact.report_fingerprint == grant.expected_report_fingerprint,
        "已使用参考的替换必须关联准确的已完成修订影响报告",
    )
    assert impact is not None
    require(
        not stale_reasons(project, impact)
        and impact.usage_fingerprints.get(grant.revision_id) == grant.usage_fingerprint
        and any(
            a.get("action") in {"adopt_report", "assess_revision"}
            and a.get("task_version") == impact.version
            for a in impact.approvals
        ),
        "总导演尚未采纳最新使用范围的修订影响报告",
    )


def guard(project: StudioProject, actor: Actor, action: str, data: dict[str, Any]) -> None:
    task = task_for(project, actor)
    require(action in actions_for(project, actor), "专项子对话没有此动作的授权")
    if action == "complete_delegate_task" and task.status == "complete":
        return  # Return the original receipt; never overwrite a completed report.
    if task.status not in {"queued", "running"}:
        stale = stale_reasons(project, task)
        require(not stale, "专项任务授权已过期：" + "；".join(stale))
        require(
            task.status != "blocked",
            "专项任务被阻塞："
            + (task.blocker or "前置条件尚未满足")
            + f"；负责人 {task.parent_actor_id}",
        )
    require(task.status in {"queued", "running"}, "任务已交回；补查须由总导演退回原任务")
    if action == "set_status":
        require(data.get("status") != "complete", "请用 complete_delegate_task 交回有证据的报告")
        return
    if action == "complete_delegate_task":
        return  # A stale assignment must still be able to report its blocker.
    stale = stale_reasons(project, task)
    require(not stale, "专项任务授权已过期：" + "；".join(stale))
    blockers = execution_blockers(project, actor)
    require(not blockers, "专项任务尚未就绪：" + "；".join(blockers))
    if action in {
        "generate_reference",
        "review_reference",
        "select_reference",
        "adopt_observed_reference",
        "adopt_prepared_reference",
    }:
        state_id = data.get("state_id")
        reference = project.references.get(str(data.get("revision_id")))
        if reference:
            state_id = reference.state_id
        require(state_id == task.request.state_id, "素材执行只能修改获准的一个资产状态")
        view = reference.view if reference else data.get("view")
        require(view in task.request.views, "此视角不在本次素材任务授权中")
        if action == "generate_reference":
            sources = set(data.get("source_revision_ids", []))
            sources.update([data["parent_id"]] if data.get("parent_id") else [])
            plan = data.get("repair_plan") or {}
            if plan.get("source_revision_id"):
                sources.add(plan["source_revision_id"])
            require(
                sources <= set(task.request.source_revision_ids) | set(task.output_revision_ids),
                "生图来源必须是获准的准确修订或本任务自身的输出",
            )
            require(
                set(data.get("source_observation_ids", []))
                <= set(task.request.source_observation_ids),
                "实拍来源不在本次授权中",
            )
        if action in {
            "review_reference",
            "select_reference",
            "adopt_observed_reference",
            "adopt_prepared_reference",
        }:
            state = project.states[str(state_id)]
            old = state.selected_revisions.get(str(view))
            if old and (reference is None or old != reference.id):
                uses = select_entities(project.candidates, binding_revision=old, limit=1)
                if uses or pending_configurations(project, old):
                    grant = next(
                        (g for g in task.request.replacements if g.revision_id == old), None
                    )
                    require(
                        grant and grant.usage_fingerprint == usage_fingerprint(project, old),
                        f"已使用参考 {old} 的替换需总导演批准最新影响范围",
                    )
                    assert grant is not None
                    require_replacement_impact(project, task.parent_actor_id, grant)
    if action == "prepare_media":
        require(data.get("kind", "image") == "image", "分析取证仅能本地截帧，不可准备视频或音频")
    if action == "prepare_reference":
        require(task.request.allow_crop and data.get("crop"), "只允许获准的精确图片裁剪")
    if action == "review_candidate":
        require(task.request.kind == "candidate_review", "只有候选审核委托可以写入正式审核")
        require(
            data.get("candidate_id") in task.allowed_scope.candidate_ids,
            "只能审核本次委托指定的候选",
        )


def material_authority(
    project: StudioProject, actor: Actor, action: str, data: dict[str, Any]
) -> None:
    if actor.role == "executive":
        return
    require(actor.role == "material_executor", "该角色没有素材执行权限")
    guard(project, actor, action, data)


def explicit_observation_source(
    project: StudioProject,
    actor: Actor,
    observation: Any,
    data: dict[str, Any],
) -> bool:
    """A scoped material source keeps its own approved asset/state ownership."""
    if actor.role != "material_executor" or data.get("source_policy") != "explicit":
        return False
    task = task_for(project, actor)
    if (
        task.request.kind != "material_preparation"
        or observation.id not in task.request.source_observation_ids
        or observation.approved_by != task.parent_actor_id
        or observation.basis != "observed"
        or observation.approved_asset_id not in task.allowed_scope.asset_ids
    ):
        return False
    for identifier in data.get("source_revision_ids", []):
        source = project.references.get(identifier)
        if not source or identifier not in task.request.source_revision_ids:
            continue
        source_state = project.states.get(source.state_id)
        if (
            source_state
            and source_state.asset_id == observation.approved_asset_id
            and source_state.id in observation.approved_state_ids
            and source_state.id in task.allowed_scope.state_ids
            and source.media_id in observation.evidence_media_ids
        ):
            return True
    return False


def _scope(project: StudioProject, request: DelegateRequest) -> DelegateScope:
    scope = request.scope.model_copy(deep=True)
    if request.state_id:
        scope.state_ids.append(request.state_id)
    scope.revision_ids.extend(request.source_revision_ids)
    scope.memory_ids.extend(request.source_observation_ids)
    for field in DelegateScope.model_fields:
        setattr(scope, field, list(dict.fromkeys(getattr(scope, field))))
    # Expand only explicit provenance, never a whole peer Set or its history.
    for identifier in scope.candidate_ids:
        candidate = project.candidates.get(identifier)
        require(candidate and candidate.media_id, "指定候选没有可用视频")
        assert candidate is not None
        scope.media_ids.append(str(candidate.media_id))
        if candidate.end_state:
            scope.media_ids.append(candidate.end_state.frame_media_id)
        scope.clip_ids.append(candidate.clip_id)
        scope.job_ids.append(candidate.job_id)
        if candidate.analysis:
            from .image_input_recovery import available_evidence

            scope.media_ids.extend(available_evidence(project, candidate))
        for binding in candidate.bindings:
            scope.revision_ids.append(binding.revision_id)
            scope.state_ids.append(binding.state_id)
        submitted = project.jobs[candidate.job_id].request
        for plan in submitted.get("asset_plan", []):
            scope.state_ids.extend(
                plan[k] for k in ("start_state_id", "end_state_id") if plan.get(k)
            )
        for key in ("reference_inputs", "video_inputs", "audio_inputs"):
            scope.media_ids.extend(
                item["media_id"] for item in submitted.get(key, []) if item.get("media_id")
            )
        if previous := submitted.get("previous_end_state"):
            scope.media_ids.append(previous["frame_media_id"])
        # Observed values are descriptive findings, not catalog state IDs.
        # The bindings/asset plan above carry actual state provenance; only the
        # observation keys identify additional assets, still checked below.
        scope.asset_ids.extend(candidate.observed_asset_states)
    for identifier in list(dict.fromkeys(scope.revision_ids)):
        revision = project.references.get(identifier)
        require(revision, "指定参考修订不存在")
        assert revision is not None
        scope.state_ids.append(revision.state_id)
        if revision.media_id:
            scope.media_ids.append(revision.media_id)
    for identifier in list(dict.fromkeys(scope.state_ids)):
        require(identifier in project.states, "指定状态不存在")
        scope.asset_ids.append(project.states[identifier].asset_id)
    for identifier in list(dict.fromkeys(scope.clip_ids)):
        require(identifier in project.clips, "指定 Clip 不存在")
        clip = project.clips[identifier]
        scope.set_ids.append(clip.set_id)
        scope.event_ids.extend(clip.story_event_ids)
        if clip.reference_plan_id:
            scope.reference_plan_ids.append(clip.reference_plan_id)
    for identifier in scope.memory_ids:
        record = memory_record(project, identifier)
        require(record and record[0] == "global", "委托只能读取指定的已发布全局记忆")
        assert record is not None
        scope.media_ids.extend(record[1].evidence_media_ids)
    for kind, field in SCOPE_FIELDS.items():
        identifiers = list(dict.fromkeys(getattr(scope, field)))
        setattr(scope, field, identifiers)
        if kind != "memory_entries":
            require(
                all(i in getattr(project, kind) for i in identifiers), f"委托范围含不存在的 {kind}"
            )
    from .image_input_recovery import quarantine

    scope.media_ids = [i for i in scope.media_ids if not quarantine(project, i)]
    return scope


def delegate(project: StudioProject, actor: Actor, data: dict[str, Any]) -> dict[str, Any]:
    require(enabled(project) and actor.role == "executive", "只有委托模式总导演可发布专项任务")
    require(project.screenplay.confirmed_at, "确认剧本后才能派发制作任务")
    requests = [DelegateRequest.model_validate(r) for r in data.get("tasks", [])]
    require(requests, "需登记至少一项专项任务")
    results = []
    for request in requests:
        require(
            request.evidence_policy == "current",
            "仅记录授权须由原总导演通过同任务普通补查启用",
        )
        scope = _scope(project, request)
        if request.kind in {"candidate_review", "repair_diagnosis", "revision_impact"}:
            require(
                len(request.scope.candidate_ids) <= 1, "每个子对话只复核一个候选；多版本须分别委托"
            )
        if request.kind == "candidate_review":
            require(len(request.scope.candidate_ids) == 1, "需指定一个候选版本")
        if request.kind == "repair_diagnosis" and not request.scope.candidate_ids:
            require(len(request.scope.clip_ids) == 1, "规划诊断需指定一个明确 Clip")
        if request.kind in {"candidate_review", "repair_diagnosis"} and request.scope.candidate_ids:
            require(scope.media_ids, "候选尚无可查看证据；先完成本地抽帧")
        from .visual_analysis import VISUAL_REQUEST_IMAGE_LIMIT

        images = [i for i in scope.media_ids if project.media[i].mime_type.startswith("image/")]
        require(
            len(images) <= VISUAL_REQUEST_IMAGE_LIMIT,
            "此任务必需图片超过服务端容量，请缩小明确范围",
        )
        material = request.kind == "material_preparation"
        if request.kind == "handoff_verification":
            require(
                len(request.scope.asset_ids) == 1 and request.scope.set_ids,
                "交接任务须明确一个目标资产和需要核验的目标片场",
            )
            require(
                len(request.scope.candidate_ids) <= 1
                and (request.scope.candidate_ids or request.source_observation_ids),
                "交接核验必须绑定一个准确来源候选或已批准观察",
            )
            require(
                all(
                    any(
                        h.asset_id == request.scope.asset_ids[0]
                        for h in project.sets[s].asset_handoffs
                    )
                    for s in request.scope.set_ids
                ),
                "目标片场没有这个资产的交接要求，请先由总导演发布",
            )
            observations = [memory_record(project, i) for i in request.source_observation_ids]
            require(
                all(
                    r
                    and r[1].approved_asset_id == request.scope.asset_ids[0]
                    and r[1].approved_by == actor.id
                    and r[1].basis == "observed"
                    and r[1].status == "effective"
                    and r[1].source_candidate_id
                    for r in observations
                ),
                "交接观察必须是这个资产的有效已批准实拍来源",
            )
            observed_sources = {
                *request.scope.candidate_ids,
                *(r[1].source_candidate_id for r in observations if r),
            }
            require(len(observed_sources) == 1, "同一核验任务只能使用一个准确来源候选")
            if request.scope.candidate_ids and not observations:
                candidate = project.candidates[request.scope.candidate_ids[0]]
                require(
                    project.clips[candidate.clip_id].accepted_candidate_id == candidate.id,
                    "交接来源尚未选用，不能把失败或目标画面视为真实末态",
                )
        if request.kind == "revision_impact":
            require(request.scope.revision_ids, "修订影响任务须指定准确的参考修订")
        if material:
            require(request.state_id and request.views, "素材任务需明确一个状态及获准视角")
            state = project.states[str(request.state_id)]
            require(
                set(request.views) <= set(project.assets[state.asset_id].required_views),
                "新状态和视角目录必须先由总导演批准发布",
            )
            require(set(request.source_revision_ids) <= set(scope.revision_ids), "素材来源越界")
            for grant in request.replacements:
                require(
                    grant.revision_id in scope.revision_ids
                    and grant.usage_fingerprint == usage_fingerprint(project, grant.revision_id),
                    "替换授权需核对准确旧修订及其最新使用范围",
                )
                require_replacement_impact(project, actor.id, grant)
                parent = project.references[grant.revision_id]
                if parent.state_id == request.state_id and parent.view in request.views:
                    # The exact approved edit target is also an image input.
                    # Keep one source list for authorization, receipts and fingerprints.
                    request.source_revision_ids = list(
                        dict.fromkeys([*request.source_revision_ids, grant.revision_id])
                    )
        require(
            all(
                t in project.delegate_tasks
                and project.delegate_tasks[t].parent_actor_id == actor.id
                for t in request.depends_on_task_ids
            ),
            "前置任务必须属于同一总导演",
        )
        if (
            request.kind == "repair_diagnosis"
            and request.scope.candidate_ids
            and not request.defects
        ):
            candidate = project.candidates[scope.candidate_ids[0]]
            request.defects = list(
                dict.fromkeys(
                    [
                        *candidate.issues,
                        *(
                            (f.description for f in candidate.review.findings)
                            if candidate.review
                            else []
                        ),
                        *(candidate.repair_plan.defects if candidate.repair_plan else []),
                    ]
                )
            )
        key_data = request.model_dump(exclude={"requirement_ids", "priority"}, mode="json")
        dedupe = digest(key_data)
        existing = next(
            (
                t
                for t in select_entities(project.delegate_tasks, parent_id=actor.id)
                if t.dedupe_key == dedupe
                and t.status not in {"failed", "blocked"}
                and not stale_reasons(project, t)
            ),
            None,
        )
        if existing:
            existing.request.priority = max(existing.request.priority, request.priority)
            existing.requirement_ids = list(
                dict.fromkeys([*existing.requirement_ids, *request.requirement_ids])
            )
            results.append(
                {"task_id": existing.id, "actor_id": existing.actor_id, "deduplicated": True}
            )
            continue
        task_id = new_id()
        child = Actor(
            role="material_executor" if material else "task_analyst",
            parent_actor_id=actor.id,
            delegate_task_id=task_id,
            summary="Waiting for authorized specialist-task dispatch",
            goal=TASK_NAMES[request.kind] + "：" + request.question,
        )
        actions = set(MATERIAL_ACTIONS if material else ANALYST_ACTIONS)
        if request.kind == "candidate_review":
            actions.add("review_candidate")
        if material and request.allow_crop:
            actions.update({"prepare_reference", "adopt_prepared_reference"})
        if material and request.allow_observed_adoption:
            require(request.source_observation_ids, "复用实拍必须指定已批准观察")
            actions.add("adopt_observed_reference")
        targets = (
            {str(request.state_id): stable_target(project, str(request.state_id))}
            if material
            else {}
        )
        sources = {
            f"{kind}:{identifier}": source_fingerprint(
                project, kind, identifier, handoff_state=request.kind == "handoff_verification"
            )
            for kind, identifiers in (
                ("references", request.source_revision_ids if material else scope.revision_ids),
                ("candidates", scope.candidate_ids),
                ("sets", scope.set_ids),
                ("memory_entries", scope.memory_ids),
                ("media", scope.media_ids),
                ("states", [] if material else scope.state_ids),
                ("clips", [] if material else scope.clip_ids),
                ("story_events", scope.event_ids),
            )
            for identifier in identifiers
        }
        usages = (
            {i: usage_fingerprint(project, i) for i in scope.revision_ids}
            if request.kind == "revision_impact"
            else {}
        )
        task = DelegateTask(
            id=task_id,
            parent_actor_id=actor.id,
            actor_id=child.id,
            # Keep the stored request readable by old StudioProject readers.
            request=DelegateRequest.model_validate(request.model_dump(exclude={"evidence_policy"})),
            allowed_scope=scope,
            allowed_actions=sorted(actions),
            target_fingerprints=targets,
            source_fingerprints=sources,
            usage_fingerprints=usages,
            input_fingerprint=digest([targets, sources, key_data]),
            dedupe_key=dedupe,
            requirement_ids=request.requirement_ids,
        )
        from .production_efficiency import bind_supply_targets

        bind_supply_targets(project, task)
        project.delegate_tasks[task.id] = task
        project.actors[child.id] = child
        results.append({"task_id": task.id, "actor_id": child.id, "deduplicated": False})
    return {
        "tasks": results,
        "summary": (
            "Ready authorized tasks are dispatched automatically. Do not "
            "merge conflicts or rewrite submitted recipes."
        ),
    }


def record_outputs(project: StudioProject, actor: Actor, outcome: Any) -> None:
    if actor.role not in DELEGATE_ROLES or not isinstance(outcome, dict):
        return
    task = task_for(project, actor)
    preparation = project.jobs.get(str(outcome.get("job_id")))
    if (
        preparation
        and preparation.kind == "reference"
        and preparation.actor_id == actor.id
        and outcome.get("reference_id")
        and preparation.request.get("revision_id") == outcome.get("reference_id")
    ):
        # prepare_reference returns its borrowed source, not a new revision.
        # Its owned output job exposes the eventual crop; allowed_scope keeps
        # the original source without claiming its parent's job as an output.
        if preparation.id not in task.output_job_ids:
            task.output_job_ids.append(preparation.id)
        if outcome.get("media_id") and outcome["media_id"] not in task.output_media_ids:
            task.output_media_ids.append(outcome["media_id"])
        return
    for field, key in (
        ("output_revision_ids", "reference_id"),
        ("output_job_ids", "job_id"),
        ("output_media_ids", "media_id"),
    ):
        if outcome.get(key) and outcome[key] not in getattr(task, field):
            getattr(task, field).append(outcome[key])
    if outcome.get("reference_id"):
        reference = project.references[outcome["reference_id"]]
        if reference.job_id and reference.job_id not in task.output_job_ids:
            task.output_job_ids.append(reference.job_id)
        if reference.media_id and reference.media_id not in task.output_media_ids:
            task.output_media_ids.append(reference.media_id)
        job = project.jobs.get(str(reference.job_id))
        if job:
            sources = set(job.request.get("source_revision_ids", []))
            sources.update(
                [job.request["anchor_revision_id"]] if job.request.get("anchor_revision_id") else []
            )
            denied_sources = sorted(
                sources - (set(task.request.source_revision_ids) | set(task.output_revision_ids))
            )
            require(
                sources <= set(task.request.source_revision_ids) | set(task.output_revision_ids),
                "自动锚定或回退来源不在素材任务授权中；请使用显式来源；"
                f"denied_source_revision_ids={denied_sources}; "
                f"anchor_revision_id={job.request.get('anchor_revision_id')!r}",
            )
        if reference.status == "selected":
            # Only the authorized selection just performed can advance this
            # task's same-state source baseline; unrelated changes remain stale.
            for identifier in task.request.source_revision_ids:
                source = project.references[identifier]
                if source.state_id == reference.state_id and source.view == reference.view:
                    task.source_fingerprints[f"references:{identifier}"] = source_fingerprint(
                        project, "references", identifier
                    )


def packet(project: StudioProject, actor: Actor) -> list[dict[str, Any]]:
    task = task_for(project, actor)
    from .context_projection import clip_preparation, compact_records
    from .review_context import review_context_records
    from .visibility import Visibility

    visibility = Visibility(project, actor.id)

    text_state_conflict = (
        actor.role == "task_analyst"
        and task.request.kind == "state_conflict"
        and (
            not (
                task.allowed_scope.media_ids
                or task.allowed_scope.revision_ids
                or task.allowed_scope.candidate_ids
            )
        )
    )
    assignment = task.model_dump(exclude={"followups", "approvals"})
    assignment = {
        "id": task.id,
        "version": task.version,
        "kind": task.request.kind,
        "question": task.request.question,
        "current_followup": task.followups[-1]["question"] if task.followups else None,
        "request": task.request.model_dump(exclude={"question", "scope"}),
        "allowed_scope": task.allowed_scope.model_dump(),
        "allowed_actions": task.allowed_actions,
        "repair_supply_targets": task.repair_supply_targets,
        "delivered_revision_ids": task.delivered_revision_ids,
        "execution": (
            "Repair independently within original authorization and "
            "ordinary allowances; deliver items automatically after actual "
            "review and selection. Escalate scope changes or exhaustion to "
            "the executive."
        ),
        "history_available": {
            "followups": len(task.followups),
            "approvals": len(task.approvals),
        },
    }
    if records_only(task):
        assignment["allowed_actions"] = sorted(actions_for(project, actor))
        assignment["request"]["evidence_policy"] = "records-only"
        assignment["evidence_basis"] = "records-only"
        assignment["limitations"] = [RECORD_REPORT_LIMITATION]
        assignment["execution"] = (
            "Compare authorized contract and revision-selection metadata only. "
            "Do not read pixels or certify images, events, repairs or asset states. "
            "Report no visual arrays; adoption accepts records only."
        )
    if task.request.kind == "repair_diagnosis" and not task.request.scope.candidate_ids:
        assignment["diagnosis_basis"] = (
            "Planning advice for the explicit Clip only; no generated candidate exists. "
            "Do not certify rendered defects, visual quality or actual end states."
        )
    records: list[dict[str, Any]] = [
        {
            "type": "delegate_assignment",
            "value": assignment,
        },
        {
            "type": "hard_constraints",
            "facts": [f.model_dump() for f in project.screenplay.facts],
            "screenplay_hash": project.screenplay.confirmed_hash,
        },
    ]
    if task.request.kind != "material_preparation":
        for i in task.allowed_scope.candidate_ids:
            records.extend(
                visibility.records(review_context_records(project, project.candidates[i]))
            )
    for kind, field in SCOPE_FIELDS.items():
        if kind in {"media", "jobs", "candidates", "reference_plans"}:
            continue  # Exact requests remain in get_detail, not repeated in the task packet.
        for identifier in getattr(task.allowed_scope, field):
            if kind == "memory_entries":
                record = memory_record(project, identifier)
                if record:
                    records.append(
                        {"type": "memory", "scope": record[0], "value": record[1].model_dump()}
                    )
            else:
                value = getattr(project, kind)[identifier]
                exclude = {
                    "history",
                    "review_history",
                    "repair_episodes",
                    "revision_ids",
                    "state_ids",
                    "memory",
                }
                if kind == "sets":
                    exclude.add("clip_ids")
                if text_state_conflict and kind == "states":
                    # Unrelated image selection is not evidence for text-only
                    # analysis. Keep descriptions, bounds and preparation sources.
                    exclude.add("selected_revisions")
                exclude.update({"preparation_history", "evolution_history", "evolution"})
                if kind == "references":
                    exclude.update({"prompt", "selection", "selection_history"})
                if kind == "clips":
                    exclude.update({"repair_attempts", "quality_episode", "preparation", "prompt"})
                projection = value.model_dump(exclude=exclude, mode="json")
                if kind == "clips":
                    projection["preparation"] = clip_preparation(value)
                records.append({"type": kind, "value": projection})
    if task.request.kind == "material_preparation":
        state = project.states[str(task.request.state_id)]
        for view in task.request.views:
            episode = state.repair_episodes.get(view)
            records.append(
                {
                    "type": "image_quality_progress",
                    "view": view,
                    "value": {
                        "selected_revision_id": state.selected_revisions.get(view),
                        "ordinary_used": episode.ordinary_attempts if episode else 0,
                        "ordinary_limit": project.config.repairs_per_reference,
                        "extra_used": episode.extra_used if episode else 0,
                        "extra_granted": episode.extra_granted if episode else 0,
                        "repair_plan": episode.plan.model_dump()
                        if episode and episode.plan
                        else None,
                    },
                }
            )
    if task.request.kind == "revision_impact":
        for identifier in task.allowed_scope.revision_ids:
            records.append(
                {
                    "type": "revision_usage",
                    "revision_id": identifier,
                    "usage_fingerprint": usage_fingerprint(project, identifier),
                    "uses": [
                        {
                            "candidate_id": c.id,
                            "clip_id": c.clip_id,
                            "status": c.status,
                            "request_hash": digest(project.jobs[c.job_id].request),
                            "selected": project.clips[c.clip_id].accepted_candidate_id == c.id,
                            "full_evidence_authorized": c.id in task.allowed_scope.candidate_ids,
                        }
                        for c in select_entities(project.candidates, binding_revision=identifier)
                    ],
                    "pending_configurations": pending_configurations(project, identifier),
                    "instruction": (
                        "Use indexes do not authorize other private candidate "
                        "histories. Do not certify unseen footage; request scoped "
                        "follow-up if needed."
                    ),
                }
            )
    records.append(
        {
            "type": "available_images",
            "media_ids": list(
                dict.fromkeys(
                    [
                        *task.allowed_scope.media_ids,
                        *task.output_media_ids,
                        *(
                            str(project.references[i].media_id)
                            for i in task.output_revision_ids
                            if project.references[i].media_id
                        ),
                    ]
                )
            ),
        }
    )
    return compact_records(records)


def notify(
    project: StudioProject,
    task: DelegateTask,
    kind: Literal["complete", "blocked", "failed", "followup"],
    summary: str,
) -> None:
    identity = digest([task.id, task.version, kind])
    if identity in project.executive_inbox:
        return
    item = ExecutiveInboxItem(
        id=identity,
        parent_actor_id=task.parent_actor_id,
        task_id=task.id,
        task_version=task.version,
        kind=kind,
        priority=task.request.priority,
        summary=summary,
        report_snapshot=task.report.model_copy(deep=True) if task.report else None,
        report_fingerprint=task.report_fingerprint,
        viewed_media_ids=list(task.viewed_media_ids),
    )
    from .proposals import clip_pending_proposals

    automatic = bool(
        kind == "complete"
        and task.request.kind == "material_preparation"
        and task.report
        and (set(task.report.selected_revision_ids) <= set(task.delivered_revision_ids))
        and (
            not any(
                clip_pending_proposals(project, project.clips[identifier])
                for identifier in task.allowed_scope.clip_ids
                if not project.clips[identifier].archived_at
            )
        )
    )
    if automatic:
        item.status, item.acknowledged_at = "acknowledged", now()
    project.executive_inbox[item.id] = item
    if automatic:
        return  # Durable notification is visible, but needs no model approval turn.
    from .service import wake_executive

    wake_executive(
        project,
        "A specialist task has returned; read get_executive_inbox: " + task.id,
        dependency_event=True,
    )


def candidate_evidence(project: StudioProject, candidate_id: str, ids: list[str]) -> set[str]:
    """Reference pictures cannot certify an unseen candidate or observed handoff."""
    from .image_input_recovery import current_evidence

    candidate = project.candidates[candidate_id]
    result = current_evidence(project, candidate)
    if candidate.end_state:
        result.add(candidate.end_state.frame_media_id)
    result.update(
        i
        for i in ids
        if i in project.media and project.media[i].source_candidate_id == candidate_id
    )
    return result


def record_report(report: DelegateReport | None) -> bool:
    return bool(
        report
        and any(
            item.get("kind") == "report_evidence_basis"
            and item.get("evidence_basis") == "records-only"
            for item in report.provenance
        )
    )


def report_basis(task: DelegateTask, report: DelegateReport) -> None:
    """Only an explicit owner request can authorize a records-only report."""
    report.provenance = [
        item for item in report.provenance if item.get("kind") != "report_evidence_basis"
    ]
    if not records_only(task):
        return
    require(
        not (
            report.findings
            or report.repairs
            or report.handoffs
            or report.revision_impacts
            or report.selected_revision_ids
        ),
        "仅记录报告不能认证视觉、事件、修复、选图或状态变更结论",
    )
    report.limitations = list(dict.fromkeys([*report.limitations, RECORD_REPORT_LIMITATION]))
    report.provenance.append(
        {
            "kind": "report_evidence_basis",
            "evidence_basis": "records-only",
            "task_id": task.id,
            "task_version": task.version,
            "visual_certification": False,
            "state_change_authorization": False,
        }
    )


def authorization_payload(
    project: StudioProject, actor: Actor, action: str, data: dict[str, Any]
) -> dict[str, Any]:
    """A usage-index citation is not permission to read the indexed private object."""
    if action != "complete_delegate_task" or actor.role not in DELEGATE_ROLES:
        return data
    task = task_for(project, actor)
    report = DelegateReport.model_validate(data["report"])
    from .production_efficiency import provenance

    report.provenance = provenance(project, task)
    report_basis(task, report)
    require(
        report.owner_id in {None, task.parent_actor_id},
        "报告负责人必须是本任务总导演；不允许引用其他角色",
    )
    # Routing metadata does not grant access to the parent's private records.
    # Keep the original owner in the stored report, but validate actual sources
    # independently. All nested evidence/source IDs retain the ordinary fence.
    source_report = report.model_dump(exclude={"owner_id"})
    if task.request.kind != "revision_impact":
        return {**data, "report": source_report}
    impacts = []
    for assessment in report.revision_impacts:
        require(
            assessment.revision_id in task.allowed_scope.revision_ids,
            "修订影响索引不属于任务授权",
        )
        used_ids = {
            c.id
            for c in select_entities(project.candidates, binding_revision=assessment.revision_id)
        }
        pending_ids = {r["id"] for r in pending_configurations(project, assessment.revision_id)}
        require(
            {u.candidate_id for u in assessment.uses} <= used_ids
            and set(assessment.pending_configuration_ids) <= pending_ids,
            "报告只能引用当前授权修订的实际使用索引",
        )
        # Only these already-validated index IDs are omitted from the general
        # source fence. Other IDs, actual evidence and all reading paths retain
        # the ordinary allowlist; complete() also checks full report coverage.
        impacts.append(
            {
                **assessment.model_dump(exclude={"uses", "pending_configuration_ids"}),
                "uses": [u.model_dump(exclude={"candidate_id"}) for u in assessment.uses],
            }
        )
    return {
        **data,
        "report": {**source_report, "revision_impacts": impacts},
    }


def handoff_target_pairs(project: StudioProject, task: DelegateTask) -> set[tuple[str, str]]:
    """Observed handoffs keep the delegate's explicit source and target scope."""
    target_events = set(task.request.scope.event_ids)
    for identifier in task.request.scope.clip_ids:
        clip = project.clips[identifier]
        if clip.set_id in task.request.scope.set_ids:
            target_events.update(clip.story_event_ids)
    source_candidates = set(task.request.scope.candidate_ids)
    for identifier in task.request.source_observation_ids:
        record = memory_record(project, identifier)
        if record and record[1].source_candidate_id:
            source_candidates.add(record[1].source_candidate_id)
    source_clips = [
        project.clips[candidate.clip_id]
        for identifier in source_candidates
        if (candidate := project.candidates.get(identifier)) and candidate.clip_id in project.clips
    ]
    return {
        (s, h.id)
        for s in task.request.scope.set_ids
        for h in project.sets[s].asset_handoffs
        if h.asset_id in task.request.scope.asset_ids
        and (
            not target_events
            or not h.target_event_ids
            or target_events.intersection(h.target_event_ids)
        )
        and (
            h.mode != "observed"
            or any(
                (not h.source_set_id or h.source_set_id == source.set_id)
                and (not h.source_event_id or h.source_event_id in source.story_event_ids)
                for source in source_clips
            )
        )
    }


def complete(
    project: StudioProject, actor: Actor, data: dict[str, Any], store: StudioStore
) -> dict[str, Any]:
    task = task_for(project, actor)
    require(data.get("task_id") == task.id, "必须交回本任务 ID")
    if task.status == "complete":
        return {
            "task_id": task.id,
            "status": task.status,
            "report_fingerprint": task.report_fingerprint,
            "already_completed": True,
        }
    guard(project, actor, "complete_delegate_task", data)
    report = DelegateReport.model_validate(data["report"])
    from .production_efficiency import provenance

    report.provenance = provenance(project, task)
    report_basis(task, report)
    require(
        report.owner_id in {None, task.parent_actor_id},
        "报告负责人必须是本任务总导演；不允许引用其他角色",
    )
    stale = stale_reasons(project, task)
    require(not stale or report.blocker, "授权已变化，报告只能返回阻塞，不能认证旧结论")
    if stale:
        report.limitations.extend(stale)
    from .image_input_recovery import inspected_evidence, quarantine

    media_ids = list(
        dict.fromkeys(
            [
                *task.allowed_scope.media_ids,
                *task.output_media_ids,
                *(
                    str(project.references[i].media_id)
                    for i in task.output_revision_ids
                    if project.references[i].media_id
                ),
                *(
                    str(project.jobs[i].result["media_id"])
                    for i in task.output_job_ids
                    if project.jobs[i].result.get("media_id")
                ),
            ]
        )
    )
    images = [i for i in media_ids if project.media[i].mime_type.startswith("image/")]
    viewed = (
        set()
        if records_only(task)
        else {
            i
            for i in inspected_evidence(project, actor, images, store)
            if not quarantine(project, i)
        }
    )
    text_only_impact = (
        task.request.kind == "revision_impact"
        and not (
            report.findings or report.repairs or report.handoffs or report.selected_revision_ids
        )
        and not any(u.evidence_media_ids for r in report.revision_impacts for u in r.uses)
    )
    planning_diagnosis = (
        task.request.kind == "repair_diagnosis" and not task.request.scope.candidate_ids
    )
    text_only_diagnosis = planning_diagnosis and not (
        report.findings
        or report.handoffs
        or report.selected_revision_ids
        or any(repair.evidence_media_ids for repair in report.repairs)
    )
    if planning_diagnosis:
        report.limitations.append(
            "仅提供当前 Clip 的规划建议；尚无生成候选，不认证实拍缺陷或实际末态"
        )
    if text_only_impact:
        report.limitations.append("仅核验修订使用关系；未认证图片或视频画面")
    require(
        not images
        or viewed
        or report.blocker
        or text_only_impact
        or text_only_diagnosis
        or records_only(task),
        "视觉结论须实际看图；无法查看时只能交回阻塞，不能认证画面",
    )
    cited = set(
        i
        for group in [
            *[f.evidence_media_ids for f in report.findings],
            *[f.evidence_media_ids for f in report.repairs],
            *[f.evidence_media_ids for f in report.handoffs],
            *[u.evidence_media_ids for r in report.revision_impacts for u in r.uses],
        ]
        for i in group
    )
    require(cited <= viewed, "报告只能引用本子对话实际查看的证据")
    missing = set(images) - viewed
    require(not missing or report.limitations or report.blocker, "有未看画面，须明确未确认范围")
    report.limitations = list(
        dict.fromkeys([*report.limitations, *("未查看图片 " + i for i in sorted(missing))])
    )
    if (
        task.request.kind in {"candidate_review", "repair_diagnosis"}
        and task.request.scope.candidate_ids
        and not report.blocker
    ):
        candidate_id = task.request.scope.candidate_ids[0]
        require(
            viewed.intersection(candidate_evidence(project, candidate_id, media_ids)),
            "未查看当前候选的实际画面，只看参考图不能形成候选结论",
        )
    if task.request.kind == "repair_diagnosis" and not report.blocker:
        require(
            report.repairs or (planning_diagnosis and not task.request.defects),
            "修复报告需逐项列出缺陷、证据、改法、是否需素材及其作用",
        )
        require(
            len({r.defect.strip().casefold() for r in report.repairs})
            >= len({d.strip().casefold() for d in task.request.defects}),
            "修复报告尚未逐项覆盖登记的缺陷；可改写表述，每项须独立列出",
        )
    if task.request.kind == "handoff_verification" and not report.blocker:
        require(report.handoffs, "交接核验需逐目标返回属性、视角、缺口和独立结论")
        require(
            all(h.set_id in task.request.scope.set_ids for h in report.handoffs),
            "交接目标不在授权范围",
        )
        pairs = [(h.set_id, h.handoff_id) for h in report.handoffs]
        require(len(set(pairs)) == len(pairs), "同一交接目标不能返回重复结论")
        expected_pairs = handoff_target_pairs(project, task)
        require(set(pairs) == expected_pairs, "交接报告须覆盖全部获准目标，不可遗漏片场")
        observed_evidence: set[str] = set()
        for candidate_id in task.request.scope.candidate_ids:
            observed_evidence.update(candidate_evidence(project, candidate_id, media_ids))
        for identifier in task.request.source_observation_ids:
            record = memory_record(project, identifier)
            if record:
                observed_evidence.update(record[1].evidence_media_ids)
                if record[1].source_candidate_id:
                    observed_evidence.update(
                        candidate_evidence(project, record[1].source_candidate_id, media_ids)
                    )
        for assessment in report.handoffs:
            handoff = next(
                (
                    h
                    for h in project.sets[assessment.set_id].asset_handoffs
                    if h.id == assessment.handoff_id
                ),
                None,
            )
            require(
                handoff and handoff.asset_id in task.request.scope.asset_ids,
                "此报告的资产交接不属于授权目标",
            )
            assert handoff is not None
            require(
                set(handoff.required_attributes)
                <= set(assessment.verified_attributes + assessment.missing_attributes),
                "逐目标报告须覆盖全部必需属性或明确缺口",
            )
            require(
                set(assessment.reusable_revision_ids) <= set(task.allowed_scope.revision_ids),
                "复用修订不在核验范围",
            )
            require(
                not set(assessment.verified_attributes).intersection(assessment.missing_attributes),
                "已核验与未确认属性不能重叠",
            )
            require(
                not assessment.verified_attributes or assessment.evidence_media_ids,
                "核验可见属性必须引用实际查看的来源证据",
            )
            require(
                not assessment.verified_attributes
                or observed_evidence.intersection(assessment.evidence_media_ids),
                "交接属性必须依据准确来源实拍，目标参考图不等于已观察事实",
            )
    if task.request.kind == "revision_impact" and not report.blocker:
        summaries = {r.revision_id: r for r in report.revision_impacts}
        require(
            len(summaries) == len(report.revision_impacts)
            and set(summaries) == set(task.allowed_scope.revision_ids),
            "修订影响报告须逐一覆盖获准修订，不可含重复或无关结论",
        )
        for identifier, summary in summaries.items():
            usages = {
                c.id for c in select_entities(project.candidates, binding_revision=identifier)
            }
            require(
                len({u.candidate_id for u in summary.uses}) == len(summary.uses)
                and {u.candidate_id for u in summary.uses} == usages,
                "修订影响报告须逐一覆盖实际使用记录",
            )
            pending_ids = {r["id"] for r in pending_configurations(project, identifier)}
            require(
                set(summary.pending_configuration_ids) == pending_ids
                and (not pending_ids or summary.pending_configuration_reason.strip()),
                "需覆盖实际未提交配置并说明局部调整或不受影响的理由",
            )
        current_usages = {i: usage_fingerprint(project, i) for i in summaries}
        if task.usage_fingerprints != current_usages:
            task.followups.append(
                {
                    "kind": "usage_scope_revalidated",
                    "previous_usage_fingerprints": dict(task.usage_fingerprints),
                    "usage_fingerprints": current_usages,
                    "at": now(),
                }
            )
        task.usage_fingerprints = current_usages
    if report.blocker and not report.owner_id:
        report.owner_id = task.parent_actor_id
    if actor.role == "material_executor":
        require(
            all(
                i in task.output_revision_ids or i in task.allowed_scope.revision_ids
                for i in report.selected_revision_ids
            ),
            "素材交付只能包含授权修订及自身输出",
        )
        require(
            all(project.references[i].status == "selected" for i in report.selected_revision_ids),
            "交付选图尚未实际选用",
        )
        pending = select_entities(
            project.jobs,
            ids=task.output_job_ids,
            statuses=["queued", "submitting", "running", "ambiguous"],
        )
        require(not pending or report.blocker, "先回收原素材任务，未知提交不能另发或假称完成")
        state = project.states[str(task.request.state_id)]
        delivered = {
            project.references[i].view
            for i in report.selected_revision_ids
            if project.references[i].state_id == state.id
        }
        require(
            set(task.request.views) <= delivered or report.blocker,
            "素材任务未交齐获准视角，需明确阻塞和负责人",
        )
    task.report = report
    task.viewed_media_ids = sorted(viewed)
    task.evidence_fingerprint = digest([[i, project.media[i].sha256] for i in sorted(viewed)])
    task.report_fingerprint = digest(
        [task.version, task.input_fingerprint, report.model_dump(), task.evidence_fingerprint]
    )
    task.status, task.completed_at = ("blocked" if report.blocker else "complete"), now()
    task.blocker = report.blocker
    actor.status, actor.summary, actor.wake_requested = "complete", report.conclusion, False
    notify(project, task, "blocked" if report.blocker else "complete", report.conclusion)
    for waiting in select_entities(
        project.delegate_tasks, parent_id=task.parent_actor_id, statuses=["queued"]
    ):
        if task.id in waiting.request.depends_on_task_ids:
            project.actors[waiting.actor_id].request_wake()
    return {
        "task_id": task.id,
        "report_fingerprint": task.report_fingerprint,
        "viewed_media_ids": task.viewed_media_ids,
        "status": task.status,
    }


def fail(project: StudioProject, actor: Actor, reason: str) -> None:
    task = task_for(project, actor)
    if task.status == "complete":
        return
    task.status, task.blocker, task.completed_at = "failed", reason, now()
    notify(project, task, "failed", reason)


def supersede(
    project: StudioProject,
    actor: Actor,
    reason: str,
    *,
    output_media_ids: list[str] | None = None,
) -> None:
    """Close a failed child after its parent has completed the same critical-path need."""
    task = task_for(project, actor)
    previous = {"status": task.status, "blocker": task.blocker, "at": now()}
    supplied = list(dict.fromkeys(i for i in output_media_ids or [] if i in project.media))
    metadata_only = records_only(task)
    task.followups.append(
        {
            "kind": "superseded_by_parent_takeover",
            "supplied_media_ids": supplied,
            **previous,
            **(
                {
                    "report": task.report.model_dump() if task.report else None,
                    "report_fingerprint": task.report_fingerprint,
                    "viewed_media_ids": list(task.viewed_media_ids),
                    "evidence_fingerprint": task.evidence_fingerprint,
                }
                if metadata_only
                else {}
            ),
        }
    )
    task.report = (None if metadata_only else task.report) or DelegateReport(
        conclusion=reason,
        limitations=[
            "The original child failure remains in followups and inbox history; "
            "the parent takeover, not this child, supplied the usable result."
        ],
        suggested_actions=["Continue the parent-owned production path."],
    )
    if metadata_only:
        report_basis(task, task.report)
        task.viewed_media_ids = []
        task.evidence_fingerprint = digest([])
        task.report_fingerprint = digest(
            [
                task.version,
                task.input_fingerprint,
                task.report.model_dump(),
                task.evidence_fingerprint,
            ]
        )
    task.status, task.blocker, task.completed_at = "complete", None, now()
    for identifier in output_media_ids or []:
        if identifier in project.media and identifier not in task.output_media_ids:
            task.output_media_ids.append(identifier)
    actor.status, actor.summary, actor.wake_requested = "complete", reason, False
    actor.last_error = None
    from .production_efficiency import deliver_requirements

    deliver_requirements(project, task)
    notify(project, task, "complete", reason)


def _material_job_in_scope(project: StudioProject, task: DelegateTask, job: Job) -> bool:
    reference = project.references.get(job.target_id)
    state = project.states.get(str(task.request.state_id))
    return bool(
        task.request.kind == "material_preparation"
        and job.kind == "image"
        and job.actor_id == task.actor_id
        and reference
        and state
        and reference.state_id == state.id
        and state.asset_id in task.allowed_scope.asset_ids
        and reference.view in task.request.views
        and job.request.get("view") == reference.view
    )


def _unsubmitted_material_job(
    project: StudioProject,
    task: DelegateTask,
    job: Job,
    read_receipt: Callable[[str], dict[str, Any]] | None,
) -> bool:
    if (
        not _material_job_in_scope(project, task, job)
        or job.status != "queued"
        or job.lease_owner
        or job.lease_until > time.time()
        or job.submitted_at
        or job.provider_job_id
        or any(job.result.get(k) for k in ("outcome", "media", "media_id", "provider_job_id"))
        or project.references[job.target_id].media_id
    ):
        return False
    identifier = job.request.get("api_receipt_id") or job.result.get("api_receipt_id")
    if not identifier:
        return True
    if read_receipt is None:
        return False
    receipt = read_receipt(str(identifier))
    context, result = receipt.get("context") or {}, receipt.get("result") or {}
    if (
        context.get("project_id") != project.id
        or context.get("job_id") != job.id
        or receipt.get("provider_job_id")
        or any(result.get(k) for k in ("outcome", "media", "media_id", "provider_job_id"))
    ):
        return False
    error = result.get("error") or {}
    return bool(
        (
            receipt.get("state") in {"reserved", "cancelled"}
            and receipt.get("execution_generation") is None
            and receipt.get("local_request_hash") is None
        )
        or (
            receipt.get("state") in {"failed", "cancelled"}
            and error.get("phase") == "submission"
            and error.get("definitely_not_accepted") is True
            and not error.get("known_job")
        )
    )


def _require_unchanged_material_recipe(
    project: StudioProject, task: DelegateTask, job: Job
) -> None:
    """Administrative authorization changes do not rewrite an already queued recipe."""
    state = project.states[str(task.request.state_id)]
    asset = project.assets[state.asset_id]
    description = f"{asset.name}. {asset.description}. State: {state.label}. {state.description}. "
    preparation = state.preparation.model_dump(mode="json") if state.preparation else None
    reference = project.references[job.target_id]
    sources = set(task.allowed_scope.revision_ids) | set(task.output_revision_ids)
    require(
        job.request.get("asset_description") == description
        and job.request.get("state_preparation") == preparation
        and job.request.get("prompt") == reference.prompt
        and job.request.get("source_revision_ids", []) == reference.source_revision_ids
        and set(reference.source_revision_ids) <= sources
        and (
            not job.request.get("anchor_revision_id")
            or job.request["anchor_revision_id"] in sources
        ),
        "原排队素材的实际目标或来源已变化；须明确处理旧配方，不能仅刷新授权",
    )
    for key, expected in task.source_fingerprints.items():
        kind, identifier = key.split(":", 1)
        if kind != "sets":
            require(
                source_fingerprint(project, kind, identifier) == expected,
                "原排队素材的实际来源已变化；不能仅刷新授权",
            )


def followup(
    project: StudioProject,
    actor: Actor,
    data: dict[str, Any],
    *,
    read_receipt: Callable[[str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    require(actor.role == "executive", "只有总导演可退回补查")
    task = project.delegate_tasks.get(str(data.get("task_id")))
    require(task and task.parent_actor_id == actor.id, "任务不属于此总导演")
    assert task is not None
    require(task.version == data.get("expected_task_version"), "任务版本已变化")
    refresh = data.get("refresh_authorization") is True
    child = project.actors[task.actor_id]
    quiescent_material_refresh = (
        task.status in {"queued", "running"}
        and task.request.kind == "material_preparation"
        and refresh
        and child.status in {"ready", "waiting"}
        and not child.paused
        and not child.lease_owner
        and child.lease_until <= time.time()
        and not child.turn_id
    )
    require(
        task.status in {"complete", "blocked", "failed"} or quiescent_material_refresh,
        "原任务尚在执行，不能并发重启",
    )
    stale = stale_reasons(project, task, refresh_usage=True)
    require(not quiescent_material_refresh or bool(stale), "原任务尚在执行，不能并发重启")
    require(not stale or refresh, "目标或来源已变更；须重新授权，不可借补查扩大范围")
    question = str(data.get("question", "")).strip()
    require(question, "需明确原报告还要补查什么")
    require(not child.lease_owner and not child.turn_id, "等待原子对话回合释放后补查")
    policy = data.get("evidence_policy")
    previous_policy = "records-only" if records_only(task) else "current"
    next_policy = str(policy) if policy is not None else previous_policy
    if policy is not None:
        # Validate the input DTO, while preserving the legacy persisted request shape.
        DelegateRequest.model_validate({**task.request.model_dump(), "evidence_policy": policy})
    previous_authorization = None
    if refresh:
        require(task.request.kind == "material_preparation", "重新授权仅用于同范围素材任务")
        reusable: dict[str, Job] = {}
        for job in select_entities(
            project.jobs,
            ids=task.output_job_ids,
            statuses=["queued", "submitting", "running", "ambiguous"],
        ):
            require(
                _unsubmitted_material_job(project, task, job, read_receipt),
                "先回收原素材任务；重新授权不能改动待回收或未知提交",
            )
            _require_unchanged_material_recipe(project, task, job)
            reusable[job.id] = job
        state = project.states.get(str(task.request.state_id))
        require(
            state and state.asset_id in task.allowed_scope.asset_ids,
            "素材目标的资产归属已变化；须另行明确授权",
        )
        assert state is not None
        require(
            set(task.request.views) <= set(project.assets[state.asset_id].required_views),
            "原获准视角必须仍在已发布目录中",
        )
        previous_authorization = {
            "target_fingerprints": dict(task.target_fingerprints),
            "source_fingerprints": dict(task.source_fingerprints),
            "input_fingerprint": task.input_fingerprint,
            "stale_reasons": stale,
            "reused_unsubmitted_job_ids": sorted(reusable),
        }
        targets = {str(task.request.state_id): stable_target(project, str(task.request.state_id))}
        sources = {
            key: source_fingerprint(project, *key.split(":", 1)) for key in task.source_fingerprints
        }
        task.target_fingerprints, task.source_fingerprints = targets, sources
        task.input_fingerprint = digest(
            [task.input_fingerprint, targets, sources, task.version + 1]
        )
        for job in reusable.values():
            if job.id not in task.output_job_ids:
                task.output_job_ids.append(job.id)
            if job.target_id not in task.output_revision_ids:
                task.output_revision_ids.append(job.target_id)
    task.followups.append(
        {
            "version": task.version,
            **({"previous_authorization": previous_authorization} if refresh else {}),
            "question": question,
            **(
                {
                    "previous_evidence_policy": previous_policy,
                    "viewed_media_ids": list(task.viewed_media_ids),
                    "evidence_fingerprint": task.evidence_fingerprint,
                }
                if policy is not None
                else {}
            ),
            **(
                {
                    "evidence_authorization": evidence_authorization(
                        actor, task.version + 1, next_policy
                    )
                }
                if policy is not None or next_policy == "records-only"
                else {}
            ),
            "report": task.report.model_dump() if task.report else None,
            "report_fingerprint": task.report_fingerprint,
            "at": now(),
        }
    )
    if policy is not None:
        task.input_fingerprint = digest(
            [task.input_fingerprint, {"evidence_policy": next_policy}, task.version + 1]
        )
    task.version += 1
    task.status, task.completed_at, task.blocker = "queued", None, None
    child.status, child.last_error, child.no_progress_count = "ready", None, 0
    child.goal = TASK_NAMES[task.request.kind] + " follow-up: " + question
    child.request_wake()
    return {
        "task_id": task.id,
        "actor_id": child.id,
        "version": task.version,
        "same_conversation": True,
    }


def evidence(project: StudioProject, actor: Actor, ids: list[str], store: StudioStore) -> set[str]:
    if actor.role != "executive":
        return set()
    from .image_input_recovery import quarantine

    result: set[str] = set()
    for task in select_entities(
        project.delegate_tasks,
        parent_id=actor.id,
        statuses=["complete", "blocked"],
        viewed_media_ids_any=ids,
    ):
        if stale_reasons(project, task) or any(
            i not in project.media for i in task.viewed_media_ids
        ):
            continue
        fingerprint = digest([[i, project.media[i].sha256] for i in sorted(task.viewed_media_ids)])
        if fingerprint != task.evidence_fingerprint:
            continue
        viewed = store.inspected(project.id, task.actor_id, task.viewed_media_ids)
        result.update(
            i
            for i in set(ids).intersection(task.viewed_media_ids, viewed)
            if not quarantine(project, i)
        )
    return result


def _notice_projection(item: ExecutiveInboxItem, *, full: bool = False) -> dict[str, Any]:
    """Index bulky immutable reports; read one exact snapshot only on demand."""
    report = item.report_snapshot
    value = item.model_dump(exclude={"report_snapshot", "summary", "viewed_media_ids"})
    excerpts: list[str] = []

    def excerpt(text: str, field: str) -> str:
        if full or len(text) <= 320:
            return text
        excerpts.append(field)
        return text[:320] + "…"

    if report is None or item.summary != report.conclusion:
        value["summary"] = excerpt(item.summary, "summary")
    else:
        value["summary_source"] = "report.conclusion"
    value["report"] = (
        report.model_dump()
        if full and report
        else {
            "conclusion": excerpt(report.conclusion, "report.conclusion"),
            "counts": {
                field: len(getattr(report, field))
                for field in (
                    "findings",
                    "repairs",
                    "handoffs",
                    "revision_impacts",
                    "selected_revision_ids",
                    "limitations",
                    "suggested_actions",
                )
            },
            "blocker": excerpt(report.blocker, "report.blocker") if report.blocker else None,
            "owner_id": report.owner_id,
            **({"provenance": report.provenance} if record_report(report) else {}),
        }
        if report
        else None
    )
    if full:
        value["viewed_media_ids"] = item.viewed_media_ids
    else:
        value["viewed_media_count"] = len(item.viewed_media_ids)
        value["excerpt_fields"] = excerpts
        value["snapshot_request"] = {
            "tool": "get_executive_inbox",
            "arguments": {"item_id": item.id},
        }
    value["detail_scope"] = "delegate-task:" + item.task_id
    return value


def inbox(
    project: StudioProject,
    actor: Actor,
    *,
    after: str | None = None,
    limit: int = 50,
    item_id: str | None = None,
) -> dict[str, Any]:
    require(actor.role == "executive", "只有总导演可读取生产收件箱")
    if item_id is not None:
        require(isinstance(item_id, str) and bool(item_id), "通知编号无效")
        require(after is None, "完整通知读取不接受分页游标")
        item = project.executive_inbox.get(item_id)
        require(item is not None and item.parent_actor_id == actor.id, "通知不属于此总导演")
        assert item is not None
        return {"projection": "notification_snapshot", "item": _notice_projection(item, full=True)}
    filters: dict[str, Any] = {"priority_order": True, "limit": max(1, min(limit, 200)) + 1}
    if after:
        try:
            cursor = json.loads(base64.urlsafe_b64decode(after.encode()))
            require(
                isinstance(cursor, list) and len(cursor) == 3 and isinstance(cursor[0], int),
                "收件箱游标无效",
            )
            require(
                not isinstance(cursor[0], bool)
                and isinstance(cursor[1], str)
                and isinstance(cursor[2], str),
                "收件箱游标无效",
            )
            filters["priority_after"] = cursor
        except (ValueError, UnicodeDecodeError):
            raise StudioError("收件箱游标无效") from None
    items = select_entities(
        project.executive_inbox, parent_id=actor.id, statuses=["pending"], **filters
    )
    page = items[: max(1, min(limit, 200))]
    return {
        "projection": "summary_index",
        "items": [
            {
                **_notice_projection(i),
                "current_task_version": project.delegate_tasks[i.task_id].version,
            }
            for i in page
        ],
        "next_cursor": base64.urlsafe_b64encode(
            json.dumps([-page[-1].priority, page[-1].created_at, page[-1].id]).encode()
        ).decode()
        if len(items) > len(page)
        else None,
        "has_more": len(items) > len(page),
    }


def acknowledge(project: StudioProject, actor: Actor, data: dict[str, Any]) -> dict[str, Any]:
    require(actor.role == "executive", "只有总导演可确认收件箱通知")
    identifiers = list(dict.fromkeys(data.get("item_ids", [])))
    require(
        all(
            i in project.executive_inbox and project.executive_inbox[i].parent_actor_id == actor.id
            for i in identifiers
        ),
        "通知不属于此总导演",
    )
    for i in identifiers:
        project.executive_inbox[i].acknowledged_at = now()
        project.executive_inbox[i].status = "acknowledged"
    return {"acknowledged_ids": identifiers, "no_progress": True}


def save_document(project: StudioProject, actor: Actor, data: dict[str, Any]) -> dict[str, Any]:
    require(
        enabled(project) and actor.role == "executive", "只有委托模式总导演可保存自己的运行小结"
    )
    identifier = digest([actor.id, data["key"]])
    old = project.executive_documents.get(identifier)
    require((old.revision if old else 0) == data.get("expected_revision"), "小结版本已变化")
    doc = ExecutiveDocument(
        id=identifier,
        actor_id=actor.id,
        key=data["key"],
        revision=old.revision + 1 if old else 1,
        title=data["title"],
        text=data["text"],
        source_refs=data.get("source_refs", []),
        supersedes_revision=old.revision if old else None,
    )
    project.executive_documents[identifier] = doc
    version_id = digest([identifier, doc.revision])
    project.executive_document_versions[version_id] = ExecutiveDocumentVersion(
        **{**doc.model_dump(), "id": version_id},
        document_id=identifier,
    )
    return {"document_id": doc.id, "revision": doc.revision, "no_progress": True}


def document_index(project: StudioProject, actor: Actor) -> list[dict[str, Any]]:
    require(actor.role == "executive", "运行小结仅总导演可读")
    return [
        d.model_dump(exclude={"text"})
        for d in select_entities(project.executive_documents, parent_id=actor.id)
    ]


def validate_approval(
    project: StudioProject, actor: Actor, approval: DelegateApproval
) -> DelegateTask:
    task = project.delegate_tasks.get(approval.task_id)
    require(task and task.parent_actor_id == actor.id and task.report, "审批任务没有可采纳的报告")
    assert task is not None
    require(
        task.status in {"complete", "blocked"}
        and task.version == approval.expected_task_version
        and task.report_fingerprint == approval.expected_report_fingerprint,
        "审批报告版本已变化",
    )
    require(not stale_reasons(project, task), "审批来源或目标版本已变化，退回受影响项")
    from .task_context import require_context

    require_context(project, actor, {"context_token": approval.context_token}, "review", task.id)
    from .visibility import Visibility

    # Final approval remains executive authority but cannot launder a report into
    # authorization for a different asset/Clip/candidate.
    source_payload = approval.payload
    if approval.action == "confirm_set_handoff":
        production_set = project.sets.get(str(approval.payload.get("set_id")))
        handoff = (
            next(
                (
                    item
                    for item in production_set.asset_handoffs
                    if item.id == approval.payload.get("handoff_id")
                ),
                None,
            )
            if production_set
            else None
        )
        references = approval.payload.get("reference_ids")
        if handoff and handoff.confirmation and isinstance(references, list):
            # Retaining an existing executive confirmation does not broaden the
            # specialist's scope. The final handoff still checks every reference
            # against current state/source ownership and actual viewing evidence.
            retained = set(handoff.confirmation.reference_ids)
            source_payload = {
                **approval.payload,
                "reference_ids": [
                    identifier for identifier in references if identifier not in retained
                ],
            }
    Visibility(project, task.actor_id).sources(source_payload)
    validate_action_target(project, task, approval)
    return task


def validate_action_target(
    project: StudioProject, task: DelegateTask, approval: DelegateApproval
) -> None:
    """A report is not a blanket capability to approve unrelated business changes."""
    require(
        not (records_only(task) or record_report(task.report)) or approval.action == "adopt_report",
        "仅记录报告只能采纳记录，不能授权视觉、事件或状态变更",
    )
    actions = {
        "adopt_report": set(TASK_NAMES),
        "approve_asset_observation": {"handoff_verification"},
        "confirm_set_handoff": {"handoff_verification"},
        "assess_revision": {"revision_impact"},
        "supply_repair_references": {"material_preparation"},
        "resolve_quality": {"candidate_review", "repair_diagnosis", "material_preparation"},
        "resolve_proposal": {"state_conflict", "repair_diagnosis", "revision_impact"},
        "correct_state_scope": {"state_conflict"},
    }
    require(task.request.kind in actions[approval.action], "此类型报告不能授权该审批动作")
    if approval.action == "adopt_report":
        require(not approval.payload, "采纳报告不接受额外业务写入参数")
        return
    from pydantic import ValidationError

    from .mcp_inputs import F, model

    fields = F[approval.action]
    data = {**approval.payload, **({"reason": approval.reason} if "reason" in fields else {})}
    try:
        data = (
            model("DelegateApprovalPayload", fields)
            .model_validate(data)
            .model_dump(exclude_none=True)
        )
    except ValidationError:
        raise StudioError("审批参数不符合该动作的类型契约") from None
    assert task.report is not None
    if "clip_id" in data:
        require(data["clip_id"] in task.allowed_scope.clip_ids, "审批 Clip 不属于报告目标")
    if "state_id" in data:
        require(data["state_id"] in task.allowed_scope.state_ids, "审批状态不属于报告目标")
    if "proposal_id" in data:
        require(data["proposal_id"] in task.allowed_scope.proposal_ids, "审批提案不属于报告目标")
    if approval.action == "resolve_quality":
        if data.get("clip_id"):
            for key in ("candidate_id", "fallback_id"):
                if data.get(key):
                    require(
                        data[key] in task.allowed_scope.candidate_ids, "选片或兜底不属于此候选报告"
                    )
        else:
            require(
                task.request.kind == "material_preparation"
                and data.get("state_id") == task.request.state_id
                and data.get("view") in task.request.views,
                "图片质量审批需针对获准的状态和视角",
            )
    elif approval.action == "supply_repair_references":
        require(
            set(data.get("revision_ids", [])) <= set(task.report.selected_revision_ids),
            "补强登记必须使用本报告实际选用的新素材",
        )
    elif approval.action == "confirm_set_handoff":
        assessment = next(
            (
                h
                for h in task.report.handoffs
                if h.set_id == data["set_id"] and h.handoff_id == data["handoff_id"]
            ),
            None,
        )
        require(
            assessment and not assessment.missing_attributes and not assessment.missing_views,
            "此交接没有对应的完整逐目标结论，退回原子对话补查",
        )
    elif approval.action == "approve_asset_observation":
        require(
            data["asset_id"] in task.request.scope.asset_ids
            and set(data["state_ids"]) <= set(task.allowed_scope.state_ids),
            "观察认证不属于报告资产和状态",
        )
        attributes = {v for h in task.report.handoffs for v in h.verified_attributes}
        require(set(data["attributes"]) <= attributes, "不能将未核验的属性认证为已观察事实")
        require(
            data["preparation_job_id"] in project.jobs
            and (
                data["preparation_job_id"] in task.allowed_scope.job_ids
                or data["preparation_job_id"] in task.output_job_ids
            ),
            "观察截图任务不属于本报告",
        )
        job = project.jobs[data["preparation_job_id"]]
        require(job.result.get("media_id") in task.viewed_media_ids, "观察截图尚未由本报告实际查看")
    elif approval.action == "assess_revision":
        parts = data["issue"].split(":")
        require(
            len(parts) == 3 and parts[1] in task.allowed_scope.revision_ids,
            "修订审批不属于报告范围",
        )
        usages = {c.id for c in select_entities(project.candidates, binding_revision=parts[1])}
        summary = next((r for r in task.report.revision_impacts if r.revision_id == parts[1]), None)
        require(
            summary and set(data["assessments"]) == usages, "修订影响审批必须对应完整的逐项报告"
        )
        assert summary is not None
        expected = {u.candidate_id: f"{u.disposition}: {u.reason}" for u in summary.uses}
        require(data["assessments"] == expected, "请采用本报告的逐项判断；需修改时退回原子对话补查")
    approval.payload = data


def approve(
    service: StudioService, project: StudioProject, actor: Actor, data: dict[str, Any]
) -> dict[str, Any]:
    require(actor.role == "executive" and enabled(project), "只有委托模式总导演可以审批专项报告")
    groups: dict[str, list[DelegateApproval]] = defaultdict(list)
    for n, raw in enumerate(data.get("approvals", [])):
        approval = DelegateApproval.model_validate(raw)
        groups[approval.group_id or f"independent:{n}"].append(approval)
    require(groups, "需提交至少一项审批")
    from .repository import mutation_savepoint

    results = []
    for group_id, approvals in groups.items():
        try:
            with mutation_savepoint(project):
                tasks = [validate_approval(project, actor, a) for a in approvals]
                outcomes = []
                for item, task in zip(approvals, tasks, strict=True):
                    payload = {**item.payload, "reason": item.reason}
                    if item.action == "adopt_report":
                        outcome: dict[str, Any] = {
                            "task_id": task.id,
                            "evidence_adopted": task.viewed_media_ids,
                        }
                        if record_report(task.report):
                            assert task.report is not None
                            outcome.update(
                                evidence_basis="records-only",
                                limitations=task.report.limitations,
                                visual_certification=False,
                                state_change_authorization=False,
                            )
                    else:
                        object.__setattr__(
                            project, "_approved_delegate_context", (task.id, item.context_token)
                        )
                        try:
                            outcome = getattr(service, "_" + item.action)(project, actor, payload)
                        finally:
                            project.__dict__.pop("_approved_delegate_context", None)
                    task.approvals.append(
                        {
                            "action": item.action,
                            "payload": payload,
                            "actor_id": actor.id,
                            "task_version": task.version,
                            "group_id": group_id,
                            "at": now(),
                        }
                    )
                    outcomes.append({"task_id": task.id, "action": item.action, "outcome": outcome})
                results.append({"group_id": group_id, "status": "applied", "items": outcomes})
        except StudioError as exc:
            results.append(
                {
                    "group_id": group_id,
                    "status": "returned",
                    "reason": str(exc),
                    "task_ids": [a.task_id for a in approvals],
                }
            )
    return {"groups": results, "no_progress": not any(g["status"] == "applied" for g in results)}


def executive_packet(project: StudioProject, task: DelegateTask) -> list[dict[str, Any]]:
    """Final decisions see semantic reports/current targets, not the delegate's bulk input."""
    records = [
        {
            "type": "delegate_result",
            "value": {
                "id": task.id,
                "version": task.version,
                "kind": task.request.kind,
                "question": task.request.question,
                "targets": task.allowed_scope.model_dump(),
                "report": task.report.model_dump() if task.report else None,
                "report_fingerprint": task.report_fingerprint,
                "viewed_media_ids": task.viewed_media_ids,
                "stale_reasons": stale_reasons(project, task),
            },
        }
    ]
    for identifier in task.allowed_scope.state_ids:
        state = project.states[identifier]
        records.append(
            {
                "type": "state",
                "value": state.model_dump(
                    exclude={
                        "revision_ids",
                        "repair_episodes",
                        "evolution_history",
                        "preparation_history",
                    },
                    mode="json",
                ),
            }
        )
    for identifier in task.allowed_scope.set_ids:
        production_set = project.sets[identifier]
        records.append(
            {
                "type": "set",
                "value": production_set.model_dump(
                    include={
                        "id",
                        "story_goal",
                        "entry_state",
                        "exit_state",
                        "asset_handoffs",
                        "dependency_revision",
                    },
                    mode="json",
                ),
            }
        )
    from .quality_progress import quality_progress

    for identifier in task.allowed_scope.clip_ids:
        clip = project.clips[identifier]
        if task.request.kind in {"candidate_review", "repair_diagnosis"}:
            from .quality_context import decision_records

            records.extend(decision_records(project, clip))
            continue
        records.append(
            {
                "type": "quality_progress",
                "clip_id": clip.id,
                "value": quality_progress(project, clip).model_dump(mode="json"),
            }
        )
    return records
