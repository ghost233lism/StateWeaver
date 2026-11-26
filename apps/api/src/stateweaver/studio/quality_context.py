"""Executive quality packets: complete current verdict, indexed older defects."""

from __future__ import annotations

from typing import Any

from .contracts import Clip, StudioProject
from .quality_progress import quality_progress
from .repository import select_entities


def decision_records(project: StudioProject, clip: Clip) -> list[dict[str, Any]]:
    candidates = sorted(
        select_entities(project.candidates, parent_id=clip.id),
        key=lambda item: (item.created_at, item.id),
    )
    reviewed = [item for item in candidates if item.review is not None]
    current_id = reviewed[-1].id if reviewed else None
    episode = clip.quality_episode
    records: list[dict[str, Any]] = [
        {
            "type": "quality_progress",
            "clip_id": clip.id,
            "value": quality_progress(project, clip).model_dump(mode="json"),
        },
        {
            "type": "quality_decision_scope",
            "clip_id": clip.id,
            "current_report_candidate_id": current_id,
            "latest_candidate_id": candidates[-1].id if candidates else None,
            "accepted_candidate_id": clip.accepted_candidate_id,
            "episode": episode.model_dump(mode="json") if episode else None,
            "instruction": (
                "当前完整审核不等于自动选用；保留原拒绝结论与妥协区别。旧版仅提供缺陷索引，需查提示词、分析或画面时读取具体候选详情或委托，未"
                "查看内容不得声称已审核。真实末态仅由已选结果授权。"
            ),
        },
    ]
    for candidate in candidates:
        review = candidate.review
        value: dict[str, Any] = candidate.model_dump(
            include={
                "id",
                "clip_id",
                "job_id",
                "media_id",
                "status",
                "created_at",
                "issues",
                "selection",
                "end_state",
                "confirmed_end_state",
                "observed_asset_states",
                "previous_candidate_id",
            },
            mode="json",
        )
        value["detail_scope"] = f"candidate:{candidate.id}"
        value["report_kind"] = (
            "current_complete" if candidate.id == current_id else "history_summary"
        )
        if candidate.id == current_id:
            value["review"] = review.model_dump(mode="json") if review else None
            value["repair_guidance"] = candidate.analysis.get("repair_guidance", [])
            value["evidence_index"] = {
                "required_media_ids": candidate.analysis.get("required_review_media_ids", []),
                "review_gaps": [gap.model_dump(mode="json") for gap in candidate.review_gaps],
            }
        elif review:
            value["defects"] = {
                "decision": review.decision,
                "reason": review.reason,
                "created_at": review.created_at,
                "findings": [item.model_dump(mode="json") for item in review.findings],
                "limitations": review.limitations,
                "coverage_gaps": [gap.model_dump(mode="json") for gap in review.coverage_gaps],
            }
        if review and review.selection_assessment is not None:
            value["selection_assessment"] = review.selection_assessment.model_dump(mode="json")
        value["prior_verdicts"] = [
            {"decision": item.decision, "reason": item.reason, "created_at": item.created_at}
            for item in candidate.review_history
        ]
        records.append({"type": "quality_candidate", "value": value})
    return records
