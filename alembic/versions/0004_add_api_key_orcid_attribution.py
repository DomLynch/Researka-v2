from __future__ import annotations

from alembic import op  # type: ignore[attr-defined]


revision = "0004_add_api_key_orcid_attribution"
down_revision = "0003_add_api_key_owner_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS owner_human_id TEXT NULL")
    op.execute("ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS owner_orcid_attribution TEXT NULL")
    op.execute("ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS owner_orcid_verified_at TIMESTAMPTZ NULL")


def downgrade() -> None:
    op.execute("ALTER TABLE api_keys DROP COLUMN IF EXISTS owner_orcid_verified_at")
    op.execute("ALTER TABLE api_keys DROP COLUMN IF EXISTS owner_orcid_attribution")
    op.execute("ALTER TABLE api_keys DROP COLUMN IF EXISTS owner_human_id")
