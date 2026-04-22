-- ============================================================================
-- ops_views.sql — Researka v2 operational queries
-- Run against the Postgres instance backing runtime_core/repos.py
-- Compatible with the 6-table schema defined in SQLPostgresRepo._ensure()
--
-- Schemas (TEXT columns storing JSON):
--   research_objects.metadata  → { decision, rationale, gate_results, ... }
--   runtime_jobs.payload       → { failure_class, failure_reason, ... }
--   runtime_events.payload     → { failure_class, failure_reason, job_id, ... }
-- ============================================================================


-- ─────────────────────────────────────────────────────────────────────────────
-- 1. PIPELINE HEALTH
-- ─────────────────────────────────────────────────────────────────────────────

-- 1a. Submissions per day (last 30 days)
--     Shows intake velocity. Sudden drops signal API or provider issues.
SELECT
    DATE_TRUNC('day', created_at)::date AS day,
    COUNT(*)                            AS submissions
FROM research_objects
WHERE object_type = 'submission'
  AND created_at >= NOW() - INTERVAL '30 days'
GROUP BY 1
ORDER BY 1 DESC;

-- 1b. Submissions per day (full history)
SELECT
    DATE_TRUNC('day', created_at)::date AS day,
    COUNT(*)                            AS submissions
FROM research_objects
WHERE object_type = 'submission'
GROUP BY 1
ORDER BY 1 DESC;


-- 1c. Editorial decision ratios (accept / revise / reject) — last 30 days
--     Decision lives in metadata JSON: metadata->>'decision'
SELECT
    metadata->>'decision' AS decision,
    COUNT(*)              AS cnt
FROM research_objects
WHERE object_type = 'decision'
  AND created_at >= NOW() - INTERVAL '30 days'
  AND metadata->>'decision' IS NOT NULL
GROUP BY 1
ORDER BY 2 DESC;

-- 1d. Editorial decision ratios — all time
SELECT
    metadata->>'decision' AS decision,
    COUNT(*)              AS cnt
FROM research_objects
WHERE object_type = 'decision'
  AND metadata->>'decision' IS NOT NULL
GROUP BY 1
ORDER BY 2 DESC;


-- 1e. Mean / median turnaround — submission → decision (hours)
--     Returns 0 rows when no decisions exist yet.
SELECT
    AVG(EXTRACT(EPOCH FROM (dec.created_at - sub.created_at)) / 3600)
        AS avg_hours,
    PERCENTILE_CONT(0.5) WITHIN GROUP (
        ORDER BY EXTRACT(EPOCH FROM (dec.created_at - sub.created_at)) / 3600
    ) AS median_hours,
    COUNT(*) AS pair_count
FROM research_objects sub
JOIN research_objects dec
    ON dec.parent_object_id = sub.id
   AND dec.object_type = 'decision'
WHERE sub.object_type = 'submission';


-- 1f. Pipeline stage success / failure counts — last 7 days
--     Jobs are the stage-observable units; a completed job = stage success.
SELECT
    stage,
    status,
    COUNT(*) AS cnt
FROM runtime_jobs
WHERE created_at >= NOW() - INTERVAL '7 days'
GROUP BY 1, 2
ORDER BY 1, 2;


-- 1g. Submissions missing a decision (older than 1 hour)
--     Identifies stuck pipelines.
SELECT
    sub.id,
    sub.title,
    sub.created_at AS submitted_at,
    NOW() - sub.created_at AS age
FROM research_objects sub
WHERE sub.object_type = 'submission'
  AND sub.created_at < NOW() - INTERVAL '1 hour'
  AND NOT EXISTS (
      SELECT 1 FROM research_objects dec
      WHERE dec.parent_object_id = sub.id
        AND dec.object_type = 'decision'
  )
ORDER BY sub.created_at ASC;


-- 1h. Submissions missing a publication (accepted decisions older than 1 hour)
SELECT
    sub.id,
    sub.title,
    dec.created_at AS decision_at
FROM research_objects sub
JOIN research_objects dec
    ON dec.parent_object_id = sub.id
   AND dec.object_type = 'decision'
WHERE sub.object_type = 'submission'
  AND dec.metadata->>'decision' = 'accept'
  AND dec.created_at < NOW() - INTERVAL '1 hour'
  AND NOT EXISTS (
      SELECT 1 FROM research_objects pub
      WHERE pub.parent_object_id = sub.id
        AND pub.object_type = 'publication'
  )
ORDER BY dec.created_at ASC;


-- ─────────────────────────────────────────────────────────────────────────────
-- 2. JOB OBSERVABILITY
-- ─────────────────────────────────────────────────────────────────────────────

-- 2a. Queue depth right now
--     The most important single number: how many jobs are waiting?
SELECT
    stage,
    COUNT(*) AS queued
FROM runtime_jobs
WHERE status = 'queued'
GROUP BY 1
ORDER BY 2 DESC;


-- 2b. Stuck / overdue leases
--     A lease whose expiry is in the past but status is still 'leased'.
--     The worker either crashed or is hanging.
SELECT
    j.id,
    j.target_object_id,
    j.stage,
    j.lease_expires_at,
    NOW() - j.lease_expires_at AS overdue_by
FROM runtime_jobs j
WHERE j.status = 'leased'
  AND j.lease_expires_at IS NOT NULL
  AND j.lease_expires_at < NOW()
ORDER BY j.lease_expires_at ASC;


-- 2c. Per-stage throughput (completed) — last 24 hours
SELECT
    stage,
    COUNT(*) AS completed,
    AVG(EXTRACT(EPOCH FROM (
        (SELECT MIN(j2.created_at) FROM runtime_jobs j2
         WHERE j2.target_object_id = j.target_object_id
           AND j2.stage = j.stage
           AND j2.status IN ('completed', 'failed')
           AND j2.created_at > j.created_at)
        - j.created_at
    )) / 60) AS avg_minutes
FROM runtime_jobs j
WHERE j.status = 'completed'
  AND j.created_at >= NOW() - INTERVAL '24 hours'
GROUP BY 1
ORDER BY 2 DESC;

-- Simpler alternative: completed jobs per stage per hour (last 24h)
SELECT
    stage,
    DATE_TRUNC('hour', created_at) AS hour,
    COUNT(*) AS completed
FROM runtime_jobs
WHERE status = 'completed'
  AND created_at >= NOW() - INTERVAL '24 hours'
GROUP BY 1, 2
ORDER BY 1, 2;


-- 2d. Failure rate by stage — last 7 days
--     high failure rate at a specific stage = investigate that gate/step
SELECT
    stage,
    COUNT(*) FILTER (WHERE status = 'completed') AS completed,
    COUNT(*) FILTER (WHERE status = 'failed')    AS failed,
    ROUND(
        100.0 * COUNT(*) FILTER (WHERE status = 'failed')
        / NULLIF(COUNT(*), 0),
        1
    ) AS fail_pct
FROM runtime_jobs
WHERE created_at >= NOW() - INTERVAL '7 days'
GROUP BY 1
ORDER BY fail_pct DESC;


-- 2e. Job duration distribution (minutes) for completed jobs — last 7 days
--     P50 / P95 / P99 via percentile
SELECT
    stage,
    PERCENTILE_CONT(0.50) WITHIN GROUP (
        ORDER BY EXTRACT(EPOCH FROM (completed_at - j.created_at)) / 60
    ) AS p50_min,
    PERCENTILE_CONT(0.95) WITHIN GROUP (
        ORDER BY EXTRACT(EPOCH FROM (completed_at - j.created_at)) / 60
    ) AS p95_min,
    PERCENTILE_CONT(0.99) WITHIN GROUP (
        ORDER BY EXTRACT(EPOCH FROM (completed_at - j.created_at)) / 60
    ) AS p99_min,
    COUNT(*) AS sample_size
FROM (
    SELECT j.*,
           LEAD(j2.created_at) OVER (
               PARTITION BY j.target_object_id, j.stage
               ORDER BY j2.created_at
           ) AS completed_at
    FROM runtime_jobs j
    JOIN runtime_jobs j2
        ON j2.target_object_id = j.target_object_id
       AND j2.stage = j.stage
       AND j2.status = 'completed'
       AND j2.created_at >= j.created_at
    WHERE j.status IN ('completed', 'queued', 'leased')
      AND j.created_at >= NOW() - INTERVAL '7 days'
) j
WHERE completed_at IS NOT NULL
GROUP BY 1;


-- ─────────────────────────────────────────────────────────────────────────────
-- 3. FAILURE ANALYSIS
-- ─────────────────────────────────────────────────────────────────────────────

-- 3a. Failure class distribution (from job payload JSON)
--     payload->>'failure_class' is set by fail_job() in repos.py
SELECT
    payload->>'failure_class' AS failure_class,
    COUNT(*)                  AS cnt
FROM runtime_jobs
WHERE status = 'failed'
  AND payload->>'failure_class' IS NOT NULL
  AND created_at >= NOW() - INTERVAL '7 days'
GROUP BY 1
ORDER BY 2 DESC;

-- 3b. Failure reasons — raw text, last 24 hours
--     Useful for spotting new failure patterns
SELECT
    payload->>'failure_class'  AS failure_class,
    payload->>'failure_reason' AS reason,
    COUNT(*)                   AS cnt
FROM runtime_jobs
WHERE status = 'failed'
  AND created_at >= NOW() - INTERVAL '24 hours'
GROUP BY 1, 2
ORDER BY 3 DESC
LIMIT 50;


-- 3c. Failure class over time (daily) — last 14 days
SELECT
    DATE_TRUNC('day', created_at)::date AS day,
    payload->>'failure_class'           AS failure_class,
    COUNT(*)                            AS cnt
FROM runtime_jobs
WHERE status = 'failed'
  AND payload->>'failure_class' IS NOT NULL
  AND created_at >= NOW() - INTERVAL '14 days'
GROUP BY 1, 2
ORDER BY 1 DESC, 3 DESC;


-- 3d. Failed submissions — which submissions failed most recently
SELECT
    j.target_object_id,
    s.title,
    j.stage,
    j.payload->>'failure_class'  AS failure_class,
    j.payload->>'failure_reason' AS reason,
    j.created_at
FROM runtime_jobs j
JOIN research_objects s
    ON s.id = j.target_object_id
   AND s.object_type = 'submission'
WHERE j.status = 'failed'
  AND j.created_at >= NOW() - INTERVAL '7 days'
ORDER BY j.created_at DESC
LIMIT 50;


-- ─────────────────────────────────────────────────────────────────────────────
-- 4. EVENT LOG ANALYSIS
-- ─────────────────────────────────────────────────────────────────────────────

-- 4a. Event type counts — last 24 hours
SELECT
    event_type,
    COUNT(*) AS cnt
FROM runtime_events
WHERE ts >= NOW() - INTERVAL '24 hours'
GROUP BY 1
ORDER BY 2 DESC;

-- 4b. Recent failures from the event log
--     The event log captures what the worker reported at runtime.
SELECT
    ts,
    event_type,
    target_object_id,
    job_id,
    worker_id,
    payload->>'failure_class'  AS failure_class,
    payload->>'failure_reason' AS reason
FROM runtime_events
WHERE event_type = 'job_failed'
  AND ts >= NOW() - INTERVAL '24 hours'
ORDER BY ts DESC
LIMIT 100;


-- 4c. Worker activity — which workers ran jobs, last 24 hours
SELECT
    worker_id,
    COUNT(*) FILTER (WHERE event_type = 'job_leased')   AS leases,
    COUNT(*) FILTER (WHERE event_type = 'job_completed') AS completions,
    COUNT(*) FILTER (WHERE event_type = 'job_failed')    AS failures
FROM runtime_events
WHERE worker_id IS NOT NULL
  AND ts >= NOW() - INTERVAL '24 hours'
GROUP BY 1
ORDER BY 2 DESC;


-- 4d. Event volume by hour — last 24 hours
--     Sudden spikes or drops indicate load changes or outages.
SELECT
    DATE_TRUNC('hour', ts) AS hour,
    event_type,
    COUNT(*) AS cnt
FROM runtime_events
WHERE ts >= NOW() - INTERVAL '24 hours'
GROUP BY 1, 2
ORDER BY 1 DESC, 3 DESC;


-- ─────────────────────────────────────────────────────────────────────────────
-- 5. API KEY & USAGE
-- ─────────────────────────────────────────────────────────────────────────────

-- 5a. Today's usage per API key
SELECT
    u.key_hash,
    k.agent_id,
    k.label,
    k.daily_limit,
    u.count AS used_today,
    CASE
        WHEN k.daily_limit > 0
        THEN ROUND(100.0 * u.count / k.daily_limit, 1)
        ELSE NULL
    END AS pct_of_limit
FROM api_key_usage u
JOIN api_keys k ON k.key_hash = u.key_hash
WHERE u.day = CURRENT_DATE::text
ORDER BY u.count DESC;

-- 5b. Keys approaching daily limit (>80% used today)
SELECT
    u.key_hash,
    k.agent_id,
    k.label,
    k.daily_limit,
    u.count AS used_today
FROM api_key_usage u
JOIN api_keys k ON k.key_hash = u.key_hash
WHERE u.day = CURRENT_DATE::text
  AND k.daily_limit > 0
  AND u.count > (k.daily_limit * 0.8)
ORDER BY u.count DESC;


-- 5c. Top agents by submission volume — last 7 days
--     author_agent_id is in metadata JSON for submissions
SELECT
    s.metadata->>'author_agent_id' AS agent_id,
    COUNT(*) AS submissions
FROM research_objects s
WHERE s.object_type = 'submission'
  AND s.created_at >= NOW() - INTERVAL '7 days'
  AND s.metadata->>'author_agent_id' IS NOT NULL
GROUP BY 1
ORDER BY 2 DESC
LIMIT 20;


-- 5d. Revoked keys
SELECT key_hash, agent_id, label, created_at
FROM api_keys
WHERE revoked = TRUE
ORDER BY created_at DESC;


-- ─────────────────────────────────────────────────────────────────────────────
-- 6. AUDIT REVIEW QUALITY
-- ─────────────────────────────────────────────────────────────────────────────

-- 6a. Verdict match rate (system vs human auditor)
SELECT
    verdict_match,
    COUNT(*)    AS cnt,
    ROUND(AVG(confidence), 3) AS avg_confidence
FROM audit_reviews
GROUP BY 1
ORDER BY 2 DESC;

-- 6b. Disagreements — where auditor disagreed with system
SELECT
    submission_id,
    auditor_id,
    auditor_verdict,
    system_verdict,
    verdict_match,
    confidence,
    LEFT(auditor_notes, 200) AS notes_preview,
    created_at
FROM audit_reviews
WHERE verdict_match = 'disagree'
ORDER BY created_at DESC
LIMIT 50;

-- 6c. Auditor activity
SELECT
    auditor_id,
    COUNT(*) AS reviews,
    COUNT(*) FILTER (WHERE verdict_match = 'agree')    AS agrees,
    COUNT(*) FILTER (WHERE verdict_match = 'disagree') AS disagrees,
    ROUND(AVG(confidence), 3) AS avg_confidence
FROM audit_reviews
GROUP BY 1
ORDER BY 2 DESC;


-- ─────────────────────────────────────────────────────────────────────────────
-- 7. MATERIALIZED VIEWS (optional — uncomment to create)
-- ─────────────────────────────────────────────────────────────────────────────

-- CREATE MATERIALIZED VIEW IF NOT EXISTS mv_daily_pipeline AS
-- SELECT
--     DATE_TRUNC('day', s.created_at)::date AS day,
--     COUNT(DISTINCT s.id)                   AS submissions,
--     COUNT(DISTINCT r.id)                   AS reviews,
--     COUNT(DISTINCT d.id)                   AS decisions,
--     COUNT(DISTINCT d.id) FILTER (WHERE d.metadata->>'decision' = 'accept')  AS accepted,
--     COUNT(DISTINCT d.id) FILTER (WHERE d.metadata->>'decision' = 'reject')  AS rejected,
--     COUNT(DISTINCT d.id) FILTER (WHERE d.metadata->>'decision' = 'revise')  AS revised
-- FROM research_objects s
-- LEFT JOIN research_objects r
--     ON r.parent_object_id = s.id AND r.object_type = 'review'
-- LEFT JOIN research_objects d
--     ON d.parent_object_id = s.id AND d.object_type = 'decision'
-- WHERE s.object_type = 'submission'
-- GROUP BY 1
-- ORDER BY 1 DESC;

-- CREATE UNIQUE INDEX IF NOT EXISTS idx_mv_daily_pipeline_day
--     ON mv_daily_pipeline (day);

-- REFRESH MATERIALIZED VIEW CONCURRENTLY mv_daily_pipeline;
