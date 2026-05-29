from __future__ import annotations

from alembic import op  # type: ignore[attr-defined]


revision = "0008_add_claim_cards"
down_revision = "0007_drop_daily_counters"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS claim_cards (
            id TEXT PRIMARY KEY,
            publication_id TEXT NOT NULL,
            claim_text TEXT NOT NULL,
            evidence_grade TEXT NOT NULL,
            citation_support TEXT NOT NULL DEFAULT '[]',
            contradiction_status TEXT NOT NULL DEFAULT 'none',
            source_ids TEXT NOT NULL DEFAULT '[]',
            dw_chain_url TEXT NULL,
            created_at TIMESTAMPTZ NOT NULL
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_claim_cards_publication_id "
        "ON claim_cards(publication_id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_claim_cards_publication_id")
    op.execute("DROP TABLE IF EXISTS claim_cards")
