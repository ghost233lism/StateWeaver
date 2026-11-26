"""Initialize the StateWeaver release Studio schema."""

import sqlalchemy as sa
from alembic import op

revision = "stateweaver_release_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "studio_projects",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("summary", sa.JSON(), server_default="{}", nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_studio_project_library", "studio_projects", ["summary", "updated_at", "id"])
    op.create_table(
        "studio_runtime_timings",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=True),
        sa.Column("actor_id", sa.String(length=64), nullable=True),
        sa.Column("phase", sa.String(length=32), nullable=False),
        sa.Column("operation", sa.String(length=128), nullable=False),
        sa.Column("started_at", sa.Float(), nullable=False),
        sa.Column("ended_at", sa.Float(), nullable=False),
        sa.Column("seconds", sa.Float(), nullable=False),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_studio_timing_project_phase",
        "studio_runtime_timings",
        ["project_id", "phase", "ended_at"],
    )
    op.create_table(
        "studio_bindings",
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("candidate_id", sa.String(length=128), nullable=False),
        sa.Column("revision_id", sa.String(length=128), nullable=False),
        sa.Column("asset_id", sa.String(length=128), nullable=False),
        sa.Column("state_id", sa.String(length=128), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["studio_projects.id"]),
        sa.PrimaryKeyConstraint("project_id", "candidate_id", "revision_id"),
    )
    op.create_index(
        "ix_studio_binding_asset", "studio_bindings", ["project_id", "asset_id", "candidate_id"]
    )
    op.create_index(
        "ix_studio_binding_revision",
        "studio_bindings",
        ["project_id", "revision_id", "candidate_id"],
    )
    op.create_table(
        "studio_bodies",
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("owner_actor_ids", sa.JSON(), server_default="[]", nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["studio_projects.id"]),
        sa.PrimaryKeyConstraint("project_id", "sha256"),
    )
    op.create_table(
        "studio_commands",
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("key", sa.String(length=256), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("cursor", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["studio_projects.id"]),
        sa.PrimaryKeyConstraint("project_id", "key"),
    )
    op.create_table(
        "studio_display_entities",
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["studio_projects.id"]),
        sa.PrimaryKeyConstraint("project_id", "kind", "id"),
    )
    op.create_table(
        "studio_entities",
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("parent_id", sa.String(length=128), nullable=True),
        sa.Column("actor_id", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=True),
        sa.Column("subtype", sa.String(length=32), nullable=True),
        sa.Column("paused", sa.Boolean(), nullable=False),
        sa.Column("due_at", sa.Float(), nullable=False),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_until", sa.Float(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("sort_key", sa.String(length=40), nullable=False),
        sa.Column("story_time", sa.Float(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["studio_projects.id"]),
        sa.PrimaryKeyConstraint("project_id", "kind", "id"),
    )
    op.create_index(
        "ix_studio_entity_actor", "studio_entities", ["project_id", "kind", "actor_id", "status"]
    )
    op.create_index(
        "ix_studio_entity_lease", "studio_entities", ["kind", "lease_owner", "lease_until"]
    )
    op.create_index(
        "ix_studio_entity_membership",
        "studio_entities",
        ["project_id", "kind", "actor_id", "parent_id"],
    )
    op.create_index(
        "ix_studio_entity_parent",
        "studio_entities",
        ["project_id", "kind", "parent_id", "sort_key", "id"],
    )
    op.create_index(
        "ix_studio_entity_schedule", "studio_entities", ["kind", "status", "paused", "due_at"]
    )
    op.create_index(
        "ix_studio_entity_status", "studio_entities", ["project_id", "kind", "status", "id"]
    )
    op.create_index(
        "ix_studio_entity_story_time",
        "studio_entities",
        ["project_id", "kind", "parent_id", "story_time"],
    )
    op.create_table(
        "studio_events",
        sa.Column("cursor", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["studio_projects.id"]),
        sa.PrimaryKeyConstraint("cursor"),
    )
    op.create_index("ix_studio_events_project_cursor", "studio_events", ["project_id", "cursor"])
    op.create_index(op.f("ix_studio_events_project_id"), "studio_events", ["project_id"])
    op.create_table(
        "studio_inspections",
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("actor_id", sa.String(length=64), nullable=False),
        sa.Column("media_id", sa.String(length=128), nullable=False),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column("item_id", sa.String(length=256), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["studio_projects.id"]),
        sa.PrimaryKeyConstraint("project_id", "actor_id", "media_id", "source_sha256"),
    )
    op.create_index("ix_studio_inspection_item", "studio_inspections", ["project_id", "item_id"])
    op.create_table(
        "studio_plan_dependencies",
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("plan_id", sa.String(length=128), nullable=False),
        sa.Column("bucket", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["studio_projects.id"]),
        sa.PrimaryKeyConstraint("project_id", "plan_id", "bucket"),
    )
    op.create_index(
        "ix_studio_plan_dependency_bucket",
        "studio_plan_dependencies",
        ["project_id", "bucket", "plan_id"],
    )
    op.create_table(
        "studio_routing_samples",
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("job_id", sa.String(length=128), nullable=False),
        sa.Column("route_id", sa.String(length=64), nullable=False),
        sa.Column("profile_key", sa.String(length=64), nullable=False),
        sa.Column("workload_key", sa.String(length=64), nullable=False),
        sa.Column("duration", sa.Float(), nullable=False),
        sa.Column("generation_seconds", sa.Float(), nullable=False),
        sa.Column("collection_seconds", sa.Float(), nullable=False),
        sa.Column("completed_at", sa.String(length=40), nullable=False),
        sa.Column("workload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["studio_projects.id"]),
        sa.PrimaryKeyConstraint("project_id", "job_id"),
    )
    op.create_index(
        "ix_studio_routing_profile_time", "studio_routing_samples", ["profile_key", "completed_at"]
    )
    op.create_index(
        "ix_studio_routing_workload",
        "studio_routing_samples",
        ["profile_key", "workload_key", "completed_at"],
    )
    op.create_table(
        "studio_text_chunks",
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("item_id", sa.String(length=256), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["studio_projects.id"]),
        sa.PrimaryKeyConstraint("project_id", "item_id", "ordinal"),
    )
    op.create_table(
        "studio_transcripts",
        sa.Column("sequence", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("item_id", sa.String(length=256), nullable=False),
        sa.Column("actor_id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["studio_projects.id"]),
        sa.PrimaryKeyConstraint("sequence"),
        sa.UniqueConstraint("project_id", "item_id", name="uq_studio_transcript_item"),
    )
    op.create_index(
        "ix_studio_transcript_actor_sequence",
        "studio_transcripts",
        ["project_id", "actor_id", "sequence"],
    )


def downgrade() -> None:
    op.drop_index("ix_studio_transcript_actor_sequence", table_name="studio_transcripts")
    op.drop_table("studio_transcripts")
    op.drop_table("studio_text_chunks")
    op.drop_index("ix_studio_routing_workload", table_name="studio_routing_samples")
    op.drop_index("ix_studio_routing_profile_time", table_name="studio_routing_samples")
    op.drop_table("studio_routing_samples")
    op.drop_index("ix_studio_plan_dependency_bucket", table_name="studio_plan_dependencies")
    op.drop_table("studio_plan_dependencies")
    op.drop_index("ix_studio_inspection_item", table_name="studio_inspections")
    op.drop_table("studio_inspections")
    op.drop_index(op.f("ix_studio_events_project_id"), table_name="studio_events")
    op.drop_index("ix_studio_events_project_cursor", table_name="studio_events")
    op.drop_table("studio_events")
    op.drop_index("ix_studio_entity_story_time", table_name="studio_entities")
    op.drop_index("ix_studio_entity_status", table_name="studio_entities")
    op.drop_index("ix_studio_entity_schedule", table_name="studio_entities")
    op.drop_index("ix_studio_entity_parent", table_name="studio_entities")
    op.drop_index("ix_studio_entity_membership", table_name="studio_entities")
    op.drop_index("ix_studio_entity_lease", table_name="studio_entities")
    op.drop_index("ix_studio_entity_actor", table_name="studio_entities")
    op.drop_table("studio_entities")
    op.drop_table("studio_display_entities")
    op.drop_table("studio_commands")
    op.drop_table("studio_bodies")
    op.drop_index("ix_studio_binding_revision", table_name="studio_bindings")
    op.drop_index("ix_studio_binding_asset", table_name="studio_bindings")
    op.drop_table("studio_bindings")
    op.drop_index("ix_studio_timing_project_phase", table_name="studio_runtime_timings")
    op.drop_table("studio_runtime_timings")
    op.drop_index("ix_studio_project_library", table_name="studio_projects")
    op.drop_table("studio_projects")
