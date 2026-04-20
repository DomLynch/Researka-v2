from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0001_runtime_core"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "research_objects",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("object_type", sa.Text(), nullable=False),
        sa.Column("parent_object_id", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body_markdown", sa.Text(), nullable=False),
        sa.Column("metadata", sa.Text(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
    )
    op.create_table(
        "runtime_jobs",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("target_object_id", sa.Text(), nullable=False),
        sa.Column("stage", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("lease_expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
    )
    op.create_table(
        "runtime_events",
        sa.Column("ts", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("target_object_id", sa.Text(), nullable=False),
        sa.Column("job_id", sa.Text(), nullable=True),
        sa.Column("worker_id", sa.Text(), nullable=True),
        sa.Column("payload", sa.Text(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("runtime_events")
    op.drop_table("runtime_jobs")
    op.drop_table("research_objects")
