from __future__ import annotations

from alembic import op  # type: ignore[attr-defined]


revision = "0011_runtime_invariants"
down_revision = "0010_add_job_fencing"
branch_labels = None
depends_on = None


def _add_constraint(name: str, ddl: str, table: str) -> None:
    op.execute(
        f"""DO $$ BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = '{name}') THEN
            ALTER TABLE {table} ADD CONSTRAINT {name} {ddl} NOT VALID;
        END IF;
        END $$"""
    )
    op.execute(f"ALTER TABLE {table} VALIDATE CONSTRAINT {name}")


def upgrade() -> None:
    # Preserve public publications and their proof lineage; keep rejected,
    # revised, query, and unrelated legacy objects private by default.
    op.execute(
        """UPDATE research_objects
        SET metadata = jsonb_set(
            metadata::jsonb,
            '{public_visibility}',
            to_jsonb(CASE
                WHEN object_type = 'publication' AND EXISTS (
                    SELECT 1 FROM research_objects decision
                    WHERE decision.object_type = 'decision'
                      AND decision.parent_object_id = research_objects.parent_object_id
                      AND (decision.metadata::jsonb)->>'decision' = 'accept'
                      AND COALESCE((decision.metadata::jsonb)->>'superseded_by', '') = ''
                ) THEN 'listed'
                WHEN object_type IN ('submission', 'review', 'decision') AND EXISTS (
                    SELECT 1 FROM research_objects publication
                    WHERE publication.object_type = 'publication'
                      AND publication.parent_object_id = CASE
                          WHEN research_objects.object_type = 'submission' THEN research_objects.id
                          ELSE research_objects.parent_object_id
                      END
                      AND COALESCE((publication.metadata::jsonb)->>'public_visibility', 'listed') = 'listed'
                      AND EXISTS (
                          SELECT 1 FROM research_objects accepted
                          WHERE accepted.object_type = 'decision'
                            AND accepted.parent_object_id = publication.parent_object_id
                            AND (accepted.metadata::jsonb)->>'decision' = 'accept'
                            AND COALESCE((accepted.metadata::jsonb)->>'superseded_by', '') = ''
                      )
                ) THEN 'listed'
                ELSE 'hidden'
            END),
            true
        )::text
        WHERE NOT (metadata::jsonb ? 'public_visibility')"""
    )
    op.execute("ALTER TABLE runtime_events ADD COLUMN IF NOT EXISTS id BIGSERIAL")
    op.execute(
        """DO $$ BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conrelid = 'runtime_events'::regclass AND contype = 'p'
        ) THEN
            ALTER TABLE runtime_events ADD CONSTRAINT runtime_events_pkey PRIMARY KEY (id);
        END IF;
        END $$"""
    )
    _add_constraint(
        "research_objects_type_check",
        "CHECK (object_type IN ('submission','review','decision','publication','audit_review','agent_query'))",
        "research_objects",
    )
    _add_constraint(
        "research_objects_parent_fk",
        "FOREIGN KEY (parent_object_id) REFERENCES research_objects(id) ON DELETE RESTRICT",
        "research_objects",
    )
    _add_constraint(
        "runtime_jobs_stage_check",
        "CHECK (stage IN ('submission_intake','autonomous_review','autonomous_editorial_decision','autonomous_publish','osf_deposit','derivation_web_delivery','publication_finalize','agent_query'))",
        "runtime_jobs",
    )
    _add_constraint(
        "runtime_jobs_status_check",
        "CHECK (status IN ('queued','leased','completed','failed'))",
        "runtime_jobs",
    )
    _add_constraint(
        "runtime_jobs_target_fk",
        "FOREIGN KEY (target_object_id) REFERENCES research_objects(id) ON DELETE RESTRICT",
        "runtime_jobs",
    )
    _add_constraint(
        "runtime_events_type_check",
        "CHECK (event_type IN ('job_queued','job_leased','job_completed','job_failed'))",
        "runtime_events",
    )
    _add_constraint(
        "runtime_events_target_fk",
        "FOREIGN KEY (target_object_id) REFERENCES research_objects(id) ON DELETE RESTRICT",
        "runtime_events",
    )
    _add_constraint(
        "runtime_events_job_fk",
        "FOREIGN KEY (job_id) REFERENCES runtime_jobs(id) ON DELETE RESTRICT",
        "runtime_events",
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_publication_parent_unique "
        "ON research_objects(parent_object_id) "
        "WHERE object_type = 'publication' AND parent_object_id IS NOT NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_review_operation_unique "
        "ON research_objects(parent_object_id, ((metadata::jsonb)->>'operation_id')) "
        "WHERE object_type = 'review' AND (metadata::jsonb ? 'operation_id')"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_decision_operation_unique "
        "ON research_objects(parent_object_id, ((metadata::jsonb)->>'operation_id')) "
        "WHERE object_type = 'decision' AND (metadata::jsonb ? 'operation_id')"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_decision_operation_unique")
    op.execute("DROP INDEX IF EXISTS idx_review_operation_unique")
    op.execute("DROP INDEX IF EXISTS idx_publication_parent_unique")
    for table, name in (
        ("runtime_events", "runtime_events_job_fk"),
        ("runtime_events", "runtime_events_target_fk"),
        ("runtime_events", "runtime_events_type_check"),
        ("runtime_jobs", "runtime_jobs_target_fk"),
        ("runtime_jobs", "runtime_jobs_status_check"),
        ("runtime_jobs", "runtime_jobs_stage_check"),
        ("research_objects", "research_objects_parent_fk"),
        ("research_objects", "research_objects_type_check"),
    ):
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}")
    op.execute("ALTER TABLE runtime_events DROP CONSTRAINT IF EXISTS runtime_events_pkey")
    op.execute("ALTER TABLE runtime_events DROP COLUMN IF EXISTS id")
