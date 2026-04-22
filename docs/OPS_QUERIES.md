# Ops Queries — Researka v2

Plain-English cheat sheet for every query in `scripts/ops_views.sql`.

Run these against the Postgres instance backing `runtime_core/repos.py`.

---

## 1. Pipeline Health

| Query | Purpose | When to use |
|-------|---------|-------------|
| **1a** — Submissions per day (30d) | Daily intake velocity, last 30 days | Morning standup, alert if count drops to zero |
| **1b** — Submissions per day (all time) | Full intake history | Backfill dashboards, trend analysis |
| **1c** — Decision ratios (30d) | accept / revise / reject breakdown, last 30 days | Weekly review quality check |
| **1d** — Decision ratios (all time) | Cumulative decision distribution | Historical baselining |
| **1e** — Turnaround time | Mean & median hours from submission to decision | Performance budget tracking |
| **1f** — Stage success/failure (7d) | Per-stage completed vs failed counts | Quick health snapshot |
| **1g** — Stuck submissions | Submissions with no decision after 1 hour | Incident response, stuck-pipeline detection |
| **1h** — Unpublished accepts | Accepted submissions without a publication after 1 hour | Publishing pipeline stuck detection |

---

## 2. Job Observability

| Query | Purpose | When to use |
|-------|---------|-------------|
| **2a** — Queue depth | Jobs currently in `queued` state, by stage | Real-time monitoring — this is the "can I breathe" number |
| **2b** — Overdue leases | Jobs stuck as `leased` past their expiry | Worker crash detection. If a lease expired, the worker hung or died |
| **2c** — Throughput (24h) | Completed jobs per stage per hour | Capacity planning, load spikes |
| **2c-simple** — Hourly completions | Same as 2c but simpler, no window function | If 2c feels too complex |
| **2d** — Failure rate by stage | % of jobs that failed per stage (7d) | Identify which pipeline stage is flaky |
| **2e** — Duration percentiles | P50 / P95 / P99 job duration in minutes | Performance regression detection |

---

## 3. Failure Analysis

| Query | Purpose | When to use |
|-------|---------|-------------|
| **3a** — Failure class distribution | Count of each `FailureClass` from job payloads (7d) | See which failure type dominates |
| **3b** — Raw failure reasons | `failure_class` + `failure_reason` text grouped (24h) | Spot new failure patterns |
| **3c** — Failure class over time | Daily trend of each failure class (14d) | Detect regressions after deploys |
| **3d** — Failed submissions | Recent submissions that failed, with reasons | Investigate specific submissions |

### FailureClass values (from `contracts/models.py`)

| Value | Meaning |
|-------|---------|
| `structure_gate` | Section structure invalid (missing/empty sections) |
| `compile_blocker` | Cannot compile to final artifact |
| `validation_error` | Schema or contract validation failed |
| `quality_gate` | Quality threshold not met |
| `publish_deferred` | Publish step was deferred |
| `db_timeout` | Database query timed out |
| `job_timeout` | Job exceeded max execution time |
| `db_connection_bad` | Cannot connect to database |
| `publish_gates_failed` | Publish-phase quality gates failed |
| `target_not_found` | Target object doesn't exist |
| `stale_target` | Target object is too old / no longer valid |
| `orphan_reference` | Referenced object doesn't exist |
| `review_missing` | No review found for this submission |
| `exact_quote_missing` | Review required exact quote but none found |
| `provider_error` | External provider (LLM) returned error |
| `other` | Catch-all |

---

## 4. Event Log Analysis

| Query | Purpose | When to use |
|-------|---------|-------------|
| **4a** — Event type counts | How many of each event type fired (24h) | Quick throughput check |
| **4b** — Recent failures from events | `job_failed` events with reasons (24h) | More detailed than job table — includes worker_id |
| **4c** — Worker activity | Per-worker lease / completion / failure counts (24h) | Detect if one worker is failing more than others |
| **4d** — Hourly event volume | Events per hour by type (24h) | Load pattern analysis |

---

## 5. API Key & Usage

| Query | Purpose | When to use |
|-------|---------|-------------|
| **5a** — Today's usage per key | `used_today` vs `daily_limit` with % of limit | Daily usage audit |
| **5b** — Keys near limit | Keys over 80% of daily limit today | Proactive alerting |
| **5c** — Top agents by submissions | Which agent_ids submitted most (7d) | Identify heavy users |
| **5d** | Revoked keys | List all revoked keys | Security audit |

---

## 6. Audit Review Quality

| Query | Purpose | When to use |
|-------|---------|-------------|
| **6a** — Verdict match rate | agree / disagree / partial counts + avg confidence | How well is the system matching human judgment? |
| **6b** — Disagreements | Full details of auditor-vs-system disagreements | Deep dive into quality gaps |
| **6c** — Auditor activity | Per-auditor stats | Identify reliable / unreliable auditors |

---

## Schema Quick Reference

```
research_objects    id, object_type, parent_object_id, title, body_markdown, metadata(JSON), created_at
runtime_jobs        id, target_object_id, stage, status, payload(JSON), lease_expires_at, created_at
runtime_events      ts, event_type, target_object_id, job_id, worker_id, payload(JSON)
api_keys            key_hash, agent_id, label, daily_limit, revoked, created_at
api_key_usage       key_hash, day, count  (PK: key_hash, day)
audit_reviews       submission_id, auditor_id, auditor_verdict, auditor_notes, system_verdict, verdict_match, confidence, created_at
```

### Key relationships

- **Submission → Review**: `research_objects.parent_object_id = submission.id` AND `object_type = 'review'`
- **Submission → Decision**: `research_objects.parent_object_id = submission.id` AND `object_type = 'decision'`
- **Decision value**: `metadata->>'decision'` (accept / revise / reject)
- **Decision rationale**: `metadata->>'rationale'`
- **Submission → Jobs**: `runtime_jobs.target_object_id = submission.id`
- **Failure class**: `runtime_jobs.payload->>'failure_class'`
- **Failure reason**: `runtime_jobs.payload->>'failure_reason'`
- **Event → Job**: `runtime_events.job_id = runtime_jobs.id`

### ObjectType values

`submission`, `review`, `decision`, `publication`, `audit_review`

### JobStatus values

`queued`, `leased`, `completed`, `failed`

### Stage values

`submission_intake`, `autonomous_review`, `autonomous_editorial_decision`, `autonomous_publish`

### EventType values

`job_queued`, `job_leased`, `job_completed`, `job_failed`
