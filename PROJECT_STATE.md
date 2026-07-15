# PROJECT_STATE.md - Researka v2

## Current Sprint
Week of: 2026-07-15
Focus: public pilot hardening for agent submissions, OSF/DOI reliability, zero-trust intake, and evidence-map quality.
Latest: OSF DOI minting retries read timeouts, listed DOI failures are being backfilled, evidence maps now get a deterministic topic-coherence intake gate, and public publication APIs expose DOI/OSF fields at top level.
Next: keep publication DOI status at zero failed listed records, monitor topic-coherence false positives, and continue pilot calibration with real third-party submissions.

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
- [x] Live accept/revise/reject proven on VPS (49.12.7.18)
- [x] Per-agent pilot keys (Postgres-backed, hashed, daily limits)
- [x] `/ops/summary` endpoint (submissions, decisions, disagreement rate, costs)
- [x] Wire Alembic into deploy (auto-schema-migration on restart)
- [x] Structured scoring/provenance on papers (GET /submissions/{id}/provenance)
- [x] Public `/calibration` endpoint (benchmark trust data)
- [x] External auditor endpoints (POST/GET /audit, GET /audit-summary)
- [x] `empirical_study` article-type scaffold (intake/review/publish path)
- [x] Gold-set contracts + evaluator script (`scripts/evaluate_gold_set.py`)
- [x] `gold_set_v1` seed corpus committed (`calibration/gold_set_v1.json`)
- [x] First live gold-set eval artifact committed (`artifacts/gold_set_eval_v1.json`)
- [x] `gold_set_v1` working corpus builder and execution board (`scripts/build_gold_set_v1.py`, `calibration/week1_execution_board.md`)
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
- [ ] Expand `gold_set_v1` from seed corpus to a true human/domain-labeled gold set
- [ ] Open invited pilot fenced to the controlled Researka drafter style
- [ ] Collect real third-party submission data before resuming open-style empirical tuning

## Non-Goals
- Frontend rebuild
- Event-sourced rewrite on day one
- Dashboard
- Auth platform
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
- Tests: 309 passing, 1 skipped (latest local full suite on 2026-07-15)

## VPS Deployment
- Host: 49.12.7.18 (root access via `ssh -i ~/.ssh/binance_futures_tool root@49.12.7.18`)
- Service: `researka-v2.service` (systemd, auto-restart)
- Database: Postgres `researka_v2` (user `researka_v2`)
- Health: http://49.12.7.18:8000/health
- **Shared with elite-trader benchmark** — other dev running 64-paper test against same LLM providers
- LLM providers: MiniMax M3 (primary), OpenRouter Gemma 4 31B (sparring), OpenRouter Mistral Small 2603 (fallback)
- Timeout: 60s per provider call (provider calls need headroom under shared load)
- Calibration artifact path: `artifacts/benchmark_style_v7.json` (style-diverse 200-case receipt exposed by `/calibration`)
