from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class StudioProjectRow(Base):
    __tablename__ = "studio_projects"
    # A library read must not walk large core-payload overflow pages to reach
    # the summary column. This small covering index leaves core data untouched.
    __table_args__ = (Index("ix_studio_project_library", "summary", "updated_at", "id"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, server_default="{}")


class StudioEventRow(Base):
    __tablename__ = "studio_events"
    __table_args__ = (Index("ix_studio_events_project_cursor", "project_id", "cursor"),)

    cursor: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("studio_projects.id"), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class StudioCommandRow(Base):
    __tablename__ = "studio_commands"

    project_id: Mapped[str] = mapped_column(ForeignKey("studio_projects.id"), primary_key=True)
    key: Mapped[str] = mapped_column(String(256), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    cursor: Mapped[int | None] = mapped_column(Integer, nullable=True)


class StudioEntityRow(Base):
    """One validated domain entity per row; indexed relationships are not JSON scans."""

    __tablename__ = "studio_entities"
    __table_args__ = (
        Index("ix_studio_entity_parent", "project_id", "kind", "parent_id", "sort_key", "id"),
        Index("ix_studio_entity_actor", "project_id", "kind", "actor_id", "status"),
        Index("ix_studio_entity_membership", "project_id", "kind", "actor_id", "parent_id"),
        Index("ix_studio_entity_status", "project_id", "kind", "status", "id"),
        Index("ix_studio_entity_schedule", "kind", "status", "paused", "due_at"),
        Index("ix_studio_entity_lease", "kind", "lease_owner", "lease_until"),
        Index("ix_studio_entity_story_time", "project_id", "kind", "parent_id", "story_time"),
    )
    project_id: Mapped[str] = mapped_column(ForeignKey("studio_projects.id"), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), primary_key=True)
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    parent_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    actor_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    subtype: Mapped[str | None] = mapped_column(String(32), nullable=True)
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    due_at: Mapped[float] = mapped_column(Float, default=0)
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_until: Mapped[float] = mapped_column(Float, default=0)
    version: Mapped[int] = mapped_column(Integer, default=1)
    sort_key: Mapped[str] = mapped_column(String(40), default=lambda: utcnow().isoformat())
    story_time: Mapped[float | None] = mapped_column(Float, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class StudioDisplayRow(Base):
    """Rebuildable, transactionally versioned UI projection of heavy entities."""

    __tablename__ = "studio_display_entities"
    project_id: Mapped[str] = mapped_column(ForeignKey("studio_projects.id"), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), primary_key=True)
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class StudioRuntimeTimingRow(Base):
    """One bounded numeric measurement, without request or model content."""

    __tablename__ = "studio_runtime_timings"
    __table_args__ = (Index("ix_studio_timing_project_phase", "project_id", "phase", "ended_at"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    actor_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    phase: Mapped[str] = mapped_column(String(32), nullable=False)
    operation: Mapped[str] = mapped_column(String(128), nullable=False)
    started_at: Mapped[float] = mapped_column(Float, nullable=False)
    ended_at: Mapped[float] = mapped_column(Float, nullable=False)
    seconds: Mapped[float] = mapped_column(Float, nullable=False)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class StudioRoutingSampleRow(Base):
    """Bounded indexed learning reads; original job telemetry remains the audit source."""

    __tablename__ = "studio_routing_samples"
    __table_args__ = (
        Index("ix_studio_routing_workload", "profile_key", "workload_key", "completed_at"),
        Index("ix_studio_routing_profile_time", "profile_key", "completed_at"),
    )
    project_id: Mapped[str] = mapped_column(ForeignKey("studio_projects.id"), primary_key=True)
    job_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    route_id: Mapped[str] = mapped_column(String(64), nullable=False)
    profile_key: Mapped[str] = mapped_column(String(64), nullable=False)
    workload_key: Mapped[str] = mapped_column(String(64), nullable=False)
    duration: Mapped[float] = mapped_column(Float, nullable=False)
    generation_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    collection_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    completed_at: Mapped[str] = mapped_column(String(40), nullable=False)
    workload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class StudioPlanDependencyRow(Base):
    """Reverse lookup of live reference-plan dependencies, including query membership."""

    __tablename__ = "studio_plan_dependencies"
    __table_args__ = (Index("ix_studio_plan_dependency_bucket", "project_id", "bucket", "plan_id"),)

    project_id: Mapped[str] = mapped_column(ForeignKey("studio_projects.id"), primary_key=True)
    plan_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    bucket: Mapped[str] = mapped_column(String(64), primary_key=True)


class StudioTranscriptRow(Base):
    __tablename__ = "studio_transcripts"
    __table_args__ = (
        UniqueConstraint("project_id", "item_id", name="uq_studio_transcript_item"),
        Index("ix_studio_transcript_actor_sequence", "project_id", "actor_id", "sequence"),
    )
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("studio_projects.id"))
    item_id: Mapped[str] = mapped_column(String(256))
    actor_id: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(32))
    version: Mapped[int] = mapped_column(Integer, default=1)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class StudioTextChunkRow(Base):
    __tablename__ = "studio_text_chunks"
    project_id: Mapped[str] = mapped_column(ForeignKey("studio_projects.id"), primary_key=True)
    item_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    text: Mapped[str] = mapped_column(Text)


class StudioBodyRow(Base):
    __tablename__ = "studio_bodies"
    project_id: Mapped[str] = mapped_column(ForeignKey("studio_projects.id"), primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    size: Mapped[int] = mapped_column(Integer)
    owner_actor_ids: Mapped[list[str]] = mapped_column(JSON, default=list, server_default="[]")


class StudioInspectionRow(Base):
    __tablename__ = "studio_inspections"
    __table_args__ = (Index("ix_studio_inspection_item", "project_id", "item_id"),)
    project_id: Mapped[str] = mapped_column(ForeignKey("studio_projects.id"), primary_key=True)
    actor_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    media_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    source_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    item_id: Mapped[str] = mapped_column(String(256))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class StudioBindingRow(Base):
    __tablename__ = "studio_bindings"
    __table_args__ = (
        Index("ix_studio_binding_revision", "project_id", "revision_id", "candidate_id"),
        Index("ix_studio_binding_asset", "project_id", "asset_id", "candidate_id"),
    )
    project_id: Mapped[str] = mapped_column(ForeignKey("studio_projects.id"), primary_key=True)
    candidate_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    revision_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    asset_id: Mapped[str] = mapped_column(String(128))
    state_id: Mapped[str] = mapped_column(String(128))
