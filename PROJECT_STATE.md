# PROJECT_STATE.md - Researka v2

## Current Sprint — 2026-09-19
Focus: integrate the verified Claude/Kimi Core improvements and repair release verification before claiming unattended publishing readiness.

### Verified releases
- Runtime release `164ec11656e328e1f797cb6ce74b1b7961499799` verified September 19 09:06:37 UTC: MacBook/GitHub/both VPS trees matched and were clean; API, worker and watchdog active. Public health/version HTTP 200, version SHA matched. GitHub CI 35433597968 passed with Postgres 16. Receipt: `../Researka-Review-2026-09-19/core-release-164ec11.json`.
- Claude/Kimi improvements remain integrated: pooled connections/shutdown, structured findings and outcome, producer import boundaries, approved reviewer models, auditable development fail-open, atomic metadata patches with explicit TEXT/jsonb casts.
- September 19 corrections repaired type/complexity/duplication regressions, lease-event chronology and CI database coverage. Final publication now merges only owned metadata keys across publication/review/decision in one atomic transaction, preserving concurrent writes.
- Local full suite for runtime changes: 1108 passed against disposable Postgres 17.9; quality, ruff and mypy109 files passed. CI independently verifies Postgres16. No review thresholds or production fail-closed policy changed.

### Calibration preparation follow-up
- A fresh production sampling attempt exposed a historical payload that violates today's source-bundle cap. The sampler now excludes contract-invalid historical rows only when an explicit audit counter is supplied; that count is included in the hashed private manifest and public freeze receipt. It never truncates or rewrites the source bundle. Default direct validation remains strict. The regression and calibration tests pass (16 tests).
- Independent certification remains blocked: both existing 120-case packets have zero completed labels; no genuine empirical-study case in the frozen corpus; no current evaluation/human signoff. See `calibration/READINESS-2026-09-19.md`. Do not treat the stale working-set score as current judge accuracy.

### Producer and publishing status
- V3's release owner deployed `e916a908` with clean Mac/GitHub/both-VPS parity and green CI, and started the normal fresh lane September19 13:14 Dubai. The writer began `metformin_measurement_methods`. This is active writing, not proof of submission or publication.
- V3 outcome/material-findings integration `68d03d3a` is committed separately. The input-budget follow-up is being verified in an isolated V3 worktree and is not yet deployed; coordinate with the V3 task before updating its active writer checkout.
- The separate preflight cleaner `4fd711e` is deployed: it no longer inserts spaces inside identifiers such as p.V42L. Twenty-nine tests, types/lint and deployed exact-payload replay pass. V3 still requires re-review of genuinely changed packages.
- Latest audited historical Core decision `b6897b61` remains revise/NOT_PUBLISHED, 21/30 claim matches versus 24 required. No old decision was overwritten or bypassed.
- Historical Sentry CORE-2/CORE-3 remain unresolved: redacted database exceptions on September13/17 releases. No new Core issue was returned for the period after runtime release164ec11 restarted at09:05UTC; this does not prove historical causes fixed.

## Goal
Build a clean Python runtime that can replace the current hot-path publishing logic without dragging frontend or legacy product baggage into the rebuild.

## Phase 1 Progress
- [x] Repo initialized
- [x] Architecture contract written
- [x] Minimal vertical slice scaffolded
- [x] Postgres repo implementation
- [x] Reviewer panel judge stack
- [x] Baseline Alembic migration scaffold
- [x] End-to-end publish flow with real persistence
- [x] Live accept/revise/reject proven on the production VPS
- [x] Per-agent pilot keys (Postgres-backed, hashed, daily limits)
- [x] `/ops/summary` endpoint (submissions, decisions, disagreement rate, costs)
- [x] Run Alembic migrations as an explicit deploy step before service restart
- [x] Structured scoring/provenance on papers (GET /submissions/{id}/provenance)
- [x] Public `/calibration` endpoint (benchmark trust data)
- [x] External auditor endpoints (POST/GET /audit, GET /audit-summary)
- [x] `empirical_study` article-type scaffold (intake/review/publish path)
- [x] Gold-set contracts + evaluator script (`scripts/evaluate_gold_set.py`)
- [x] `gold_set_v1` seed corpus committed (`calibration/gold_set_v1.json`)
- [x] First live gold-set eval artifact committed (`artifacts/gold_set_eval_v1.json`)
- [x] `gold_set_v1` working corpus builder and execution board (`scripts/build_gold_set_v1.py`, `calibration/week1_execution_board.md`)
- [x] Freeze 120 real submissions under the blinded v1 adjudication protocol (`calibration/real_gold_set_v1_freeze_receipt.json`)
- [x] Live 30-entry working gold-set baseline frozen (`artifacts/gold_set_eval_v2_working_baseline.json`, `.md`)
- [x] Run live 200-paper benchmark against judge_panel and freeze stage-1 artifact (`artifacts/benchmark_vps_200_stage1.json`)
- [x] Build focused micro-set + live calibrator (`calibration/calibration_micro_set.json`, `scripts/calibrate_reviewer.py`)
- [x] Tighten synthetic benchmark corpus so `high` and `low` fixtures are genuinely separable (`artifacts/calibration_micro_fixture_v2.json` = `19/20`)
- [x] Karpathy-loop prep: intervention log written (`docs/reviewer_prompt_interventions.md`, 5 interventions documented, 20/20 micro-set = triage+anchors on revised corpus)
- [x] Rerun the full 200-paper benchmark with the revised corpus and triage/anchor reviewer prompt (`artifacts/benchmark_vps_200_v6_repaired.json` = repaired broad baseline at `93.5%`)
- [x] Cross-check style robustness on the terser elite v3 corpus (`artifacts/benchmark_v3_vs_v6_prompt.json` = `0/40`, all reject; style sensitivity still open)
- [x] Merge the clean `codex/house-medium-fix` line forward: house fix, v3 article-type routing, empirical benchmark scaffolding, frozen v3 smoke artifact
- [x] Build and run the 200-paper style-diverse v7 benchmark (`artifacts/benchmark_style_v7.json` = `88.5%` overall; `terser/verbose/external` clear threshold, `house` still fails at `78.0%`)
- [x] Fix house-style medium over-accept: reviewer prompt v8, medium sections use invalidating phrases, retry logic. House = `96.0%`, medium = `100%` revise
- [x] Merge `mimo/style-alignment` into `main` as the clean base for future work
- [x] Persist submissions, first jobs, and queue receipts atomically
- [x] Add bounded review retries, lease recovery, idempotent stage enqueue, and stalled-handoff reconciliation
- [x] Expose lifecycle attempts/failures, operational alerts, and a daily deterministic publish canary
- [x] Add paste-first Verify checks for citation identity, quoted wording, and exact number/unit agreement
- [x] Add Verify-only PMC retrieval, exact passage receipts, numeric contradiction checks, and source-type/retraction transparency
- [x] Add Verify-only arXiv HTML retrieval for AI, economics, mathematics, and physics preprints
- [ ] Complete two-reviewer adjudication and conflict resolution for the frozen real corpus
- [ ] Add a genuine empirical-study case; current production empirical rows are synthetic benchmark fixtures and remain excluded
- [ ] Open invited pilot fenced to the controlled Researka drafter style
- [ ] Collect real third-party submission data before resuming open-style empirical tuning

## Non-Goals
- Frontend rebuild
- Event-sourced rewrite on day one
- Dashboard
- General-purpose identity platform
- Plugin system
- Abstraction layer

## Risks
- Rebuild drift if v2 absorbs writer-side concerns after the gatekeeper pivot
- Over-abstracting before one clean end-to-end slice exists
- Under-testing publish blockers
- Topic-coherence gate currently checks markdown table rows; prose-only evidence maps may need a later guard if agents start emitting them.
- Open-style empirical calibration needs current re-baselining before broad claims.

## Key Architecture
- Core: `runtime_core/` — workflow, gates, compiler, providers, repos, ops, prompts
- Contracts: `contracts/` — schemas, enums, payloads
- API: `apps/runtime_api/app.py` — FastAPI endpoints
- Tests: 1108 passed with real disposable PostgreSQL 17.9 for the finalization follow-up; make quality, mypy (109 files) and ruff pass. First release b38d48c is deployed and CI-verified; this finalization follow-up still requires its own release receipt.

## VPS Deployment
- Checkout: `/opt/researka-v2` under the dedicated `researka` service account after this release
- Service: `researka-v2.service` (systemd, auto-restart)
- Database: Postgres `researka_v2` (user `researka_v2`)
- Health: `https://api.researka.org/health`
- LLM providers: Codex GPT-5.6-Sol high (primary), Codex GPT-5.6-Terra medium (second review), OpenRouter z-ai/glm-5.3-flash (failure-only backup). See `docs/codex-review-cutover.md` for deployment and rollback checks.
- Timeout: 600s per Codex reviewer, 60s for the single backup request; worker lease heartbeat remains enabled. Two distinct valid model votes required; this is not provider independence.
- Calibration artifact path: `artifacts/gold_set_eval_v3_current.json` (current 30-case working receipt; not certified as an adjudicated gold set)
