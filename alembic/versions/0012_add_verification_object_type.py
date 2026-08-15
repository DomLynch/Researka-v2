from __future__ import annotations

from alembic import op  # type: ignore[attr-defined]


revision = "0012_add_verification_object_type"
down_revision = "0011_runtime_invariants"
branch_labels = None
depends_on = None

_BASE_TYPES = "'submission','review','decision','publication','audit_review','agent_query'"


def _replace_constraint(types: str) -> None:
    op.execute("ALTER TABLE research_objects DROP CONSTRAINT IF EXISTS research_objects_type_check")
    op.execute(
        "ALTER TABLE research_objects ADD CONSTRAINT research_objects_type_check "
        f"CHECK (object_type IN ({types})) NOT VALID"
    )
    op.execute("ALTER TABLE research_objects VALIDATE CONSTRAINT research_objects_type_check")


def upgrade() -> None:
    _replace_constraint(f"{_BASE_TYPES},'verification'")


def downgrade() -> None:
    _replace_constraint(_BASE_TYPES)
