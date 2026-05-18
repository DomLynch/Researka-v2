from __future__ import annotations

from alembic import op  # type: ignore[attr-defined]


revision = "0003_add_api_key_owner_identity"
down_revision = "0002_add_api_keys"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS owner_name TEXT NULL")
    op.execute("ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS owner_orcid TEXT NULL")


def downgrade() -> None:
    op.execute("ALTER TABLE api_keys DROP COLUMN IF EXISTS owner_orcid")
    op.execute("ALTER TABLE api_keys DROP COLUMN IF EXISTS owner_name")
