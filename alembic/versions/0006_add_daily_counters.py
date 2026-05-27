from __future__ import annotations

from alembic import op  # type: ignore[attr-defined]


revision = "0006_add_daily_counters"
down_revision = "0005_add_osf_oauth_tokens"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS daily_counters (
            counter_key TEXT NOT NULL,
            day TEXT NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (counter_key, day)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS daily_counters")
