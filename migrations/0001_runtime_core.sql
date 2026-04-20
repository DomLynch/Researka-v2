CREATE TABLE IF NOT EXISTS research_objects (
    id TEXT PRIMARY KEY,
    object_type TEXT NOT NULL,
    parent_object_id TEXT NULL,
    title TEXT NOT NULL,
    body_markdown TEXT NOT NULL,
    metadata TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS runtime_jobs (
    id TEXT PRIMARY KEY,
    target_object_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    status TEXT NOT NULL,
    payload TEXT NOT NULL,
    lease_expires_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS runtime_events (
    ts TIMESTAMPTZ NOT NULL,
    event_type TEXT NOT NULL,
    target_object_id TEXT NOT NULL,
    job_id TEXT NULL,
    worker_id TEXT NULL,
    payload TEXT NOT NULL
);
