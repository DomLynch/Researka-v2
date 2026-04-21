# PROJECT_STATE.md - Researka v2

## Current Sprint
Week of: 2026-04-21
Focus: Phase 1 — internal AAA + safe invited pilot. 8-phase execution plan.
Latest: `gold_set_v1` has been expanded into the 30-entry working corpus (`gold-set-v1-working`), and the live judge-panel baseline is now frozen on disk (`artifacts/gold_set_eval_v2_working_baseline.json`, `artifacts/gold_set_eval_v2_working_baseline.md`) at 30/30 correct across both `rapid_evidence_synthesis` and `empirical_study`
Next: expand the working corpus into a true human/domain-labeled gold set, then rerun the 200-paper benchmark against the current reviewer to measure pilot readiness on a broader non-curated slice

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
- [ ] Run live 200-paper benchmark against judge_panel to target (working gold set passes, broader calibration still unproven)
- [ ] Expand `gold_set_v1` from seed corpus to a true human/domain-labeled gold set
- [ ] Open invited pilot (ops task)

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

## Key Architecture
- Core: `runtime_core/` — workflow, gates, compiler, providers, repos, ops, prompts
- Contracts: `contracts/` — schemas, enums, payloads
- API: `apps/runtime_api/app.py` — FastAPI endpoints
- Tests: 98 passing, 1 skipped (Postgres concurrency)

## VPS Deployment
- Host: 49.12.7.18 (root access via `ssh -i ~/.ssh/binance_futures_tool root@49.12.7.18`)
- Service: `researka-v2.service` (systemd, auto-restart)
- Database: Postgres `researka_v2` (user `researka_v2`)
- Health: http://49.12.7.18:8000/health
- **Shared with elite-trader benchmark** — other dev running 64-paper test against same LLM providers
- LLM providers: MiniMax (primary), MIMO (sparring, ~15s baseline), DeepSeek (fallback)
- Timeout: 60s per provider call (was 30s, MIMO needs headroom under shared load)
- Calibration artifact path: `artifacts/benchmark_baseline.json` (shared by benchmark runners and `/calibration`)
