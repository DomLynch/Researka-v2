# Public Agent Query Endpoints

Date: 2026-06-03

## Endpoints

- `POST /agent-query/jobs`
- `GET /agent-query/jobs/{job_id}`

## Runtime Flags

- `RESEARKA_V2_AGENT_QUERY_ENABLED=1` enables public query intake.
- Missing, `0`, `false`, `no`, or `off` disables intake with `503 agent_query_disabled`.
- `RESEARKA_V2_AGENT_QUERY_PER_IP_PER_DAY` controls per-client daily intake. The runtime stores only a hashed client/day bucket on each job, not raw IPs.
- `RESEARKA_V2_AGENT_QUERY_MAX_RUNTIME_SEC`, `RESEARKA_V2_AGENT_QUERY_MAX_SOURCES`, and `RESEARKA_V2_AGENT_QUERY_MAX_COST_USD` are stored on each job as worker caps.

## Boundary

Agent query jobs are stored as `ObjectType.AGENT_QUERY` research objects. They do not enqueue `RuntimeJob`, do not enter `/jobs/queue`, and do not touch submission, review, editorial, publish, or scheduled bot state.

Email receipt input is reduced to `contact_email_provided`; the raw email is not stored in the runtime object.

The local public runner updates the same object metadata with:

```json
{
  "status": "running|completed|failed|expired",
  "updated_at": "ISO timestamp",
  "result": {
    "title": "...",
    "summary": "...",
    "answerMarkdown": "...",
    "citations": [],
    "generatedAt": "ISO timestamp"
  }
}
```

## Live Enablement Checklist

1. Deploy the backend code to the `researka-v2.service` host.
2. Keep `RESEARKA_V2_AGENT_QUERY_ENABLED` unset until the service is healthy.
3. Verify `POST /agent-query/jobs` returns `503 agent_query_disabled`.
4. Set `RESEARKA_V2_AGENT_QUERY_ENABLED=1` only when public intake should open.
5. Verify a sample `POST /agent-query/jobs` returns `202`, then poll `GET /agent-query/jobs/{job_id}` until `completed`.
6. Verify `GET /jobs/queue` stays empty for agent-query jobs.
