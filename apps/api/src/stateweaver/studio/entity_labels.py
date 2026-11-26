"""Small, indexed display-name batches for visible historical prose links."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select, tuple_
from sqlalchemy.orm import Session, aliased

from stateweaver.persistence.models import StudioEntityRow, StudioTranscriptRow
from stateweaver.studio.contracts import EntityLabel, EntityLabelBatch

ENTITY_KINDS = (
    "actors",
    "sets",
    "clips",
    "assets",
    "states",
    "references",
    "candidates",
    "media",
    "jobs",
    "memory_entries",
    "proposals",
    "story_events",
    "reference_plans",
    "visual_tasks",
)
_FIELDS = (
    "title",
    "name",
    "label",
    "role",
    "set_id",
    "clip_id",
    "state_id",
    "asset_id",
    "view",
    "kind",
    "target_id",
    "actor_id",
    "mime_type",
    "width",
    "height",
    "duration",
    "summary",
    "description",
    "reason",
    "text",
    "created_at",
    "media_id",
    "section",
    "revision",
    "question",
)
_ROLES = {
    "screenwriter": "编剧",
    "executive": "总导演",
    "set_director": "片场导演",
    "visual_analyst": "看图分析",
}
_MEMORY = {"plan": "制作计划", "observation": "实际观察", "constraint": "已确认事实"}
_VIEWS = {
    "close_up": "面部特写",
    "face_closeup": "面部特写",
    "face_close_up": "面部特写",
    "closeup": "面部特写",
    "full_body": "全身图",
    "three_view": "三视图",
    "three_views": "三视图",
    "expression_grid": "表情九宫格",
    "expression_sheet": "表情九宫格",
    "expressions": "表情九宫格",
    "establishing": "场景全貌",
    "wide": "场景全貌",
    "detail": "细节图",
    "master": "主参考图",
    "opening_composition": "开场构图",
    "reverse": "反打视角",
    "geography": "空间布局",
    "entrance_detail": "入口细节",
}
_ACTIONS = {
    "write_global_memory": "更新全局记忆",
    "write_set_memory": "更新片场记忆",
    "generate_reference": "生成参考图",
    "prepare_reference": "准备参考素材",
    "select_reference": "选用参考图",
    "create_asset": "建立视觉资产",
    "create_state": "建立剧情状态",
    "record_state_evolution": "确认状态演变",
    "generate_clip": "生成片段",
    "review_clip": "检查片段",
    "accept_clip": "选用片段",
    "inspect_media": "查看素材",
    "get_project": "读取画布",
}


def _excerpt(value: object, limit: int = 120) -> str:
    if not isinstance(value, str):
        return ""
    # A name must not reintroduce the opaque IDs or URLs it is meant to replace.
    value = re.sub(r"https?://\S+", "素材链接", value)
    value = re.sub(
        r"(?<![a-zA-Z0-9])(?:[a-fA-F0-9]{32}|[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}"
        r"-[a-fA-F0-9]{12})(?![a-zA-Z0-9])",
        "相关内容",
        value,
    )
    value = re.sub(r"\s+", " ", value).strip(" #*`\n\t")
    return value if len(value) <= limit else value[: limit - 1] + "…"


@dataclass
class _Record:
    id: str
    kind: str
    parent_id: str | None
    values: dict[str, Any]


def _rows(session: Session, project_id: str, ids: set[str], kinds: set[str]) -> list[_Record]:
    if not ids or not kinds:
        return []
    # Project only bounded display fields, not prompts, analysis, request/results
    # or entire payloads. The composite primary key scopes every lookup.
    columns = [
        func.substr(StudioEntityRow.payload[key].as_string(), 1, 500).label(key) for key in _FIELDS
    ]
    rows = session.execute(
        select(StudioEntityRow.id, StudioEntityRow.kind, StudioEntityRow.parent_id, *columns)
        .where(
            StudioEntityRow.project_id == project_id,
            StudioEntityRow.kind.in_(kinds),
            StudioEntityRow.id.in_(ids),
        )
        .limit(len(ids) * len(kinds))
    ).all()
    return [
        _Record(row[0], row[1], row[2], dict(zip(_FIELDS, row[3:], strict=True))) for row in rows
    ]


def _parents(record: _Record) -> list[tuple[str, str]]:
    value, result = record.values, []
    field = {
        "actors": ("sets", "set_id"),
        "clips": ("sets", "set_id"),
        "candidates": ("clips", "clip_id"),
        "references": ("states", "state_id"),
        "states": ("assets", "asset_id"),
        "proposals": ("sets", "set_id"),
        "reference_plans": ("clips", "clip_id"),
        "transcript": ("actors", "actor_id"),
    }.get(record.kind)
    if field and value.get(field[1]):
        result.append((field[0], value[field[1]]))
    if record.kind == "memory_entries" and record.parent_id not in {None, "global"}:
        result.append(("sets", record.parent_id))
    if record.kind == "jobs":
        kind = "references" if value.get("kind") in {"image", "reference"} else "candidates"
        if value.get("kind") != "assembly" and value.get("target_id"):
            result.append((kind, value["target_id"]))
        if value.get("actor_id"):
            result.append(("actors", value["actor_id"]))
    return result


def read_entity_labels(
    session: Session,
    project_id: str,
    ids: list[str],
    project: dict[str, Any],
) -> EntityLabelBatch:
    requested = list(dict.fromkeys(ids))
    records = _rows(session, project_id, set(requested), set(ENTITY_KINDS))
    transcripts = session.execute(
        select(
            StudioTranscriptRow.item_id,
            StudioTranscriptRow.actor_id,
            StudioTranscriptRow.kind,
            func.substr(StudioTranscriptRow.payload["text"].as_string(), 1, 500),
            func.substr(StudioTranscriptRow.payload["activity"]["summary"].as_string(), 1, 500),
        )
        .where(
            StudioTranscriptRow.project_id == project_id,
            StudioTranscriptRow.item_id.in_(requested),
        )
        .limit(len(requested))
    ).all()
    records.extend(
        _Record(
            row[0],
            "transcript",
            None,
            {"actor_id": row[1], "kind": row[2], "text": row[3], "summary": row[4]},
        )
        for row in transcripts
    )
    by_id: dict[str, list[_Record]] = defaultdict(list)
    for record in records:
        by_id[record.id].append(record)
    related = {(record.kind, record.id): record for record in records}
    frontier = records
    # Three indexed parent hops suffice even for job → reference → state → asset.
    for _ in range(3):
        needed = {parent for record in frontier for parent in _parents(record)} - related.keys()
        if not needed:
            break
        frontier = _rows(session, project_id, {r[1] for r in needed}, {r[0] for r in needed})
        related.update({(record.kind, record.id): record for record in frontier})

    numbered = [
        record for record in related.values() if record.kind in {"references", "candidates"}
    ]
    ordinals: dict[tuple[str, str], int] = {}
    if numbered:
        previous = aliased(StudioEntityRow)
        count = (
            select(func.count())
            .where(
                previous.project_id == StudioEntityRow.project_id,
                previous.kind == StudioEntityRow.kind,
                previous.parent_id == StudioEntityRow.parent_id,
                tuple_(previous.sort_key, previous.id)
                <= tuple_(StudioEntityRow.sort_key, StudioEntityRow.id),
            )
            .correlate(StudioEntityRow)
            .scalar_subquery()
        )
        rows = session.execute(
            select(StudioEntityRow.kind, StudioEntityRow.id, count).where(
                StudioEntityRow.project_id == project_id,
                StudioEntityRow.kind.in_(("references", "candidates")),
                StudioEntityRow.id.in_({record.id for record in numbered}),
            )
        ).all()
        ordinals = {(row[0], row[1]): row[2] for row in rows}

    def get(kind: str, identifier: str | None) -> _Record | None:
        return related.get((kind, identifier or ""))

    def name(record: _Record | None, depth: int = 0) -> str:
        if record is None or depth > 5:
            return ""
        v, kind = record.values, record.kind
        if kind in {"assets", "sets", "clips"}:
            return _excerpt(v.get("name") or v.get("title"), 100)
        if kind == "actors":
            role = (
                "片场导演"
                if project.get("workflow_version") == "set-v1" and v.get("role") == "set_director"
                else _ROLES.get(v.get("role", ""), "导演")
            )
            production_set = name(get("sets", v.get("set_id")), depth + 1)
            return f"{production_set} · {role}" if production_set else role
        if kind == "states":
            return " · ".join(
                filter(
                    None,
                    (
                        name(get("assets", v.get("asset_id")), depth + 1),
                        _excerpt(v.get("label"), 60),
                    ),
                )
            )
        if kind in {"references", "candidates"}:
            ordinal = ordinals.get((kind, record.id))
            if kind == "references":
                view = _VIEWS.get(v.get("view", ""), _excerpt(v.get("view"), 30))
                owner = name(get("states", v.get("state_id")), depth + 1) or "参考图"
                return f"{owner} · {view} · 修订 {ordinal}" if ordinal else f"{owner} · {view}"
            owner = name(get("clips", v.get("clip_id")), depth + 1) or "视频片段"
            return f"{owner} · 第 {ordinal} 次生成" if ordinal else f"{owner} · 生成结果"
        if kind == "jobs":
            task = {
                "image": "生成参考图",
                "video": "生成视频",
                "analysis": "检查片段",
                "assembly": "合成短片",
                "reference": "准备参考素材",
            }.get(v.get("kind", ""), "制作任务")
            owner_kind = "references" if v.get("kind") in {"image", "reference"} else "candidates"
            owner = name(get(owner_kind, v.get("target_id")), depth + 1)
            if not owner:
                owner = name(get("actors", v.get("actor_id")), depth + 1)
            return f"{owner} · {task}" if owner else task
        if kind == "media":
            if record.id == project.get("final_media_id"):
                return f"{_excerpt(project.get('title'), 100)} · 成片视频"
            mime = v.get("mime_type") or ""
            media_kind = (
                "视频素材"
                if mime.startswith("video/")
                else "图像素材"
                if mime.startswith("image/")
                else "音频素材"
                if mime.startswith("audio/")
                else "素材文件"
            )
            dimensions = (
                f"{v['width']} × {v['height']}" if v.get("width") and v.get("height") else ""
            )
            duration = f"{float(v['duration']):g} 秒" if v.get("duration") else ""
            return " · ".join(filter(None, (media_kind, duration, dimensions)))
        if kind == "memory_entries":
            scope = (
                "全局记忆"
                if record.parent_id == "global"
                else name(get("sets", record.parent_id), depth + 1) or "片场记忆"
            )
            category = {
                "screenplay": "剧本事实",
                "events": "事件与状态",
                "directives": "导演约定",
                "set_shared": "片场共用约定",
                "clip_plan": "片段计划",
                "clip_actual": "已选实际结果",
                "clip_continuation": "接续要求",
            }.get(v.get("section", ""), _MEMORY.get(v.get("kind", ""), "剧情记忆"))
            return f"{scope} · {category}：{_excerpt(v.get('text'), 80)}"
        if kind == "story_events":
            return f"剧情事件：{_excerpt(v.get('title'), 100)}"
        if kind == "visual_tasks":
            return f"看图任务：{_excerpt(v.get('question'), 100)}"
        if kind == "reference_plans":
            owner = name(get("clips", v.get("clip_id")), depth + 1) or "视频片段"
            return f"{owner} · 参考清单 {v.get('revision') or 1}"
        if kind == "proposals":
            scope = name(get("sets", v.get("set_id")), depth + 1)
            return f"{scope + ' · ' if scope else ''}修改建议：{_excerpt(v.get('text'), 80)}"
        if kind == "transcript":
            actor = name(get("actors", v.get("actor_id")), depth + 1) or "导演"
            excerpt = _ACTIONS.get(
                v.get("text", ""), _excerpt(v.get("summary") or v.get("text"), 90)
            )
            return f"{actor}：{excerpt}" if excerpt else f"{actor}的输出"
        return ""

    result: dict[str, EntityLabel] = {}
    for identifier in requested:
        if identifier == project_id:
            result[identifier] = EntityLabel(
                label=_excerpt(project.get("title"), 200) or "当前项目",
                description="查看项目的全局记忆",
                kind="project",
            )
            continue
        matches = by_id[identifier]
        # Ambiguous IDs never silently choose an arbitrary record.
        if len(matches) != 1:
            continue
        record = matches[0]
        label = _excerpt(name(record), 240)
        if not label:
            continue
        values = record.values
        description = _excerpt(
            values.get("reason")
            or values.get("description")
            or values.get("summary")
            or values.get("text"),
            500,
        )
        result[identifier] = EntityLabel(label=label, description=description, kind=record.kind)
    return EntityLabelBatch(
        labels=result,
        missing_ids=[identifier for identifier in requested if identifier not in result],
    )
