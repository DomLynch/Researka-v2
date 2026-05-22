from __future__ import annotations

from alembic import op  # type: ignore[attr-defined]


revision = "0005_add_osf_oauth_tokens"
down_revision = "0002_add_api_keys"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS osf_oauth_tokens (
            agent_id TEXT PRIMARY KEY,
            token_metadata TEXT NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS osf_oauth_tokens")
