from __future__ import annotations

from alembic import op  # type: ignore[attr-defined]


revision = "0009_runtime_lifecycle_indexes"
down_revision = "0008_add_claim_cards"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_runtime_events_target_ts "
        "ON runtime_events(target_object_id, ts)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_runtime_jobs_status_created "
        "ON runtime_jobs(status, created_at)"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_runtime_jobs_active_target_stage "
        "ON runtime_jobs(target_object_id, stage) WHERE status IN ('queued', 'leased')"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_research_objects_parent_type "
        "ON research_objects(parent_object_id, object_type, created_at)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_research_objects_parent_type")
    op.execute("DROP INDEX IF EXISTS idx_runtime_jobs_active_target_stage")
    op.execute("DROP INDEX IF EXISTS idx_runtime_jobs_status_created")
    op.execute("DROP INDEX IF EXISTS idx_runtime_events_target_ts")
