from __future__ import annotations

from alembic import op  # type: ignore[attr-defined]

revision = "0007_drop_daily_counters"
down_revision = "0006_add_daily_counters"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP TABLE IF EXISTS daily_counters")


def downgrade() -> None:
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
