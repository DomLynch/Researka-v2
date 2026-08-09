CREATE TABLE IF NOT EXISTS research_objects (
    id TEXT PRIMARY KEY,
    object_type TEXT NOT NULL CHECK (object_type IN ('submission','review','decision','publication','audit_review','agent_query')),
    parent_object_id TEXT NULL REFERENCES research_objects(id) ON DELETE RESTRICT,
    title TEXT NOT NULL,
    body_markdown TEXT NOT NULL,
    metadata TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS runtime_jobs (
    id TEXT PRIMARY KEY,
    target_object_id TEXT NOT NULL REFERENCES research_objects(id) ON DELETE RESTRICT,
    stage TEXT NOT NULL CHECK (stage IN ('submission_intake','autonomous_review','autonomous_editorial_decision','autonomous_publish','osf_deposit','derivation_web_delivery','publication_finalize','agent_query')),
    status TEXT NOT NULL CHECK (status IN ('queued','leased','completed','failed')),
    payload TEXT NOT NULL,
    lease_expires_at TIMESTAMPTZ NULL,
    lease_token BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS runtime_events (
    id BIGSERIAL PRIMARY KEY,
    ts TIMESTAMPTZ NOT NULL,
    event_type TEXT NOT NULL CHECK (event_type IN ('job_queued','job_leased','job_completed','job_failed')),
    target_object_id TEXT NOT NULL REFERENCES research_objects(id) ON DELETE RESTRICT,
    job_id TEXT NULL REFERENCES runtime_jobs(id) ON DELETE RESTRICT,
    worker_id TEXT NULL,
    payload TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_publication_parent_unique
    ON research_objects(parent_object_id)
    WHERE object_type = 'publication' AND parent_object_id IS NOT NULL;
