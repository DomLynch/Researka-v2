# PROJECT_STATE.md - Researka v2

## Current Sprint
Week of: 2026-09-06
Focus: Researka Verify as the public product while the agent-publication gatekeeper continues in the background.
Latest: Core reviewer migration to ChatGPT-authenticated Codex: Sol high plus Terra medium, with GLM 5.3 Flash only on a reviewer failure. Verify remains separate and does not call these models.
Next: measure Verify usage before adding more repositories or bounded semantic support; complete independent judge calibration separately.

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
- Tests: 720 passing, 1 skipped (local full suite on 2026-09-06)

## VPS Deployment
- Checkout: `/opt/researka-v2` under the dedicated `researka` service account after this release
- Service: `researka-v2.service` (systemd, auto-restart)
- Database: Postgres `researka_v2` (user `researka_v2`)
- Health: `https://api.researka.org/health`
- LLM providers: Codex GPT-5.6-Sol high (primary), Codex GPT-5.6-Terra medium (second review), OpenRouter z-ai/glm-5.3-flash (failure-only backup). See `docs/codex-review-cutover.md` for deployment and rollback checks.
- Timeout: 600s per Codex reviewer, 60s for the single backup request; worker lease heartbeat remains enabled. Two distinct valid model votes required; this is not provider independence.
- Calibration artifact path: `artifacts/gold_set_eval_v3_current.json` (current 30-case working receipt; not certified as an adjudicated gold set)
