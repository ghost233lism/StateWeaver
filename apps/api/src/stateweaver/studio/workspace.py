"""Project-owned presentation settings, independent of production commands."""

from __future__ import annotations

from sqlalchemy import select, text

from stateweaver.persistence.models import StudioEntityRow, StudioProjectRow
from stateweaver.studio.contracts import WorkspaceLayout, WorkspaceLayoutUpdate
from stateweaver.studio.store import StudioError, StudioStore


class WorkspaceStore:
    def __init__(self, store: StudioStore) -> None:
        self.store = store

    def get(self, project_id: str) -> WorkspaceLayout:
        with self.store.database.session() as session:
            if session.get(StudioProjectRow, project_id) is None:
                raise StudioError("项目不存在", 404)
            row = session.get(StudioEntityRow, (project_id, "workspace_layout", "canvas"))
            return WorkspaceLayout.model_validate(row.payload) if row else WorkspaceLayout()

    def update(self, project_id: str, update: WorkspaceLayoutUpdate) -> WorkspaceLayout:
        if update.reset and (update.nodes or update.asset_order):
            raise StudioError("重置布局不能同时包含位置或排序修改", 422)
        with self.store.database.write_lock, self.store.database.session() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            if session.get(StudioProjectRow, project_id) is None:
                raise StudioError("项目不存在", 404)
            row = session.get(StudioEntityRow, (project_id, "workspace_layout", "canvas"))
            current = WorkspaceLayout.model_validate(row.payload) if row else WorkspaceLayout()
            if update.expected_version != current.version:
                raise StudioError("布局已在其他窗口更新，请重新加载布局后保存", 409)
            if update.reset:
                result = WorkspaceLayout(version=current.version + 1)
            else:
                for key in update.nodes:
                    if not key or len(key) > 256:
                        raise StudioError("布局节点标识无效", 422)
                for kind, ids in update.asset_order.items():
                    if not kind or len(kind) > 100 or len(ids) > 4096 or len(set(ids)) != len(ids):
                        raise StudioError("资产排序无效", 422)
                    if any(not item or len(item) > 128 for item in ids):
                        raise StudioError("资产排序标识无效", 422)
                    assets = list(
                        session.scalars(
                            select(StudioEntityRow).where(
                                StudioEntityRow.project_id == project_id,
                                StudioEntityRow.kind == "assets",
                                StudioEntityRow.id.in_(ids),
                            )
                        )
                    )
                    if len(assets) != len(ids) or any(
                        a.payload.get("kind") != kind for a in assets
                    ):
                        raise StudioError("只能在同一资产分类中排序", 422)
                result = WorkspaceLayout(
                    version=current.version + 1,
                    nodes={**current.nodes, **update.nodes},
                    asset_order={**current.asset_order, **update.asset_order},
                )
            if row is None:
                row = StudioEntityRow(
                    project_id=project_id, kind="workspace_layout", id="canvas", payload={}
                )
                session.add(row)
            row.payload = result.model_dump(mode="json")
            row.version = result.version
            return result
