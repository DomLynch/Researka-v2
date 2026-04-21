# PROJECT_STATE.md - Researka v2

## Current Sprint
Week of: 2026-04-21
Focus: Phase 1 — internal AAA + safe invited pilot. 8-phase execution plan.
Latest: `gold_set_v1` seed corpus is committed and the first live judge-panel eval is on disk (`artifacts/gold_set_eval_v1.json`) with 8/10 accuracy; empirical-study controls scored 3/3, rapid-evidence-synthesis scored 5/7
Next: expand `gold_set_v1` beyond the seed set and tune rapid-evidence-synthesis reviewer/drafter behavior against the two live mismatches and dominant accept blockers (`claim_support_verdict`, `major_issues`, `required_revisions`)

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
- [ ] Run live 200-paper benchmark against judge_panel to target (current live baseline still below pilot bar)
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
- Tests: 95 passing, 1 skipped (Postgres concurrency)

## VPS Deployment
- Host: 49.12.7.18 (root access via `ssh -i ~/.ssh/binance_futures_tool root@49.12.7.18`)
- Service: `researka-v2.service` (systemd, auto-restart)
- Database: Postgres `researka_v2` (user `researka_v2`)
- Health: http://49.12.7.18:8000/health
- **Shared with elite-trader benchmark** — other dev running 64-paper test against same LLM providers
- LLM providers: MiniMax (primary), MIMO (sparring, ~15s baseline), DeepSeek (fallback)
- Timeout: 60s per provider call (was 30s, MIMO needs headroom under shared load)
- Calibration artifact path: `artifacts/benchmark_baseline.json` (shared by benchmark runners and `/calibration`)
