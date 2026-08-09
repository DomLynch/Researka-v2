from __future__ import annotations

from alembic import op  # type: ignore[attr-defined]


revision = "0010_add_job_fencing"
down_revision = "0009_runtime_lifecycle_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE runtime_jobs ADD COLUMN IF NOT EXISTS lease_token BIGINT NOT NULL DEFAULT 0")


def downgrade() -> None:
    op.execute("ALTER TABLE runtime_jobs DROP COLUMN IF EXISTS lease_token")
