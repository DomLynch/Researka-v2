# PROJECT_STATE.md - Researka v2

## Current Sprint
Week of: 2026-04-21
Focus: Phase 1 — internal AAA + safe invited pilot. 8-phase execution plan.
Latest: the revised synthetic corpus plus `reviewer-v6-triage-anchors` now clears the repaired broad 200-paper benchmark at `187/200 = 93.5%` (`high=97.8% accept`, `medium=86.4% revise`, `low=100% reject`, `broken=100% reject`). The micro-set remains `20/20` (`100%`). The important remaining caveat is style robustness: the terser elite v3 cross-check still failed hard (`0/40`, all `reject`), so the current win is strong on the revised benchmark corpus but not yet style-invariant.
Next: decide whether invited pilot will use the controlled Researka drafter style only. If yes, invited pilot can open behind that fence. If no, align the external/terser drafter style with the accepted house style, rerun the v3 cross-check, and only then widen intake.

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
- [x] Rerun the full 200-paper benchmark with the revised corpus and triage/anchor reviewer prompt (`artifacts/benchmark_baseline.json` = repaired broad baseline at `93.5%`)
- [x] Cross-check style robustness on the terser elite v3 corpus (`artifacts/benchmark_v3_vs_v6_prompt.json` = `0/40`, all reject; style sensitivity still open)
- [ ] Expand `gold_set_v1` from seed corpus to a true human/domain-labeled gold set
- [ ] Open invited pilot (ops task)
- [ ] Align external/terser drafter style with the accepted house style and rerun the v3 cross-check

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
- Cross-style calibration drift: the current reviewer/corpus combo is strong on the revised synthetic benchmark but still rejects the terser elite v3 style wholesale

## Key Architecture
- Core: `runtime_core/` — workflow, gates, compiler, providers, repos, ops, prompts
- Contracts: `contracts/` — schemas, enums, payloads
- API: `apps/runtime_api/app.py` — FastAPI endpoints
- Tests: 102 passing, 1 skipped (Postgres concurrency)

## VPS Deployment
- Host: 49.12.7.18 (root access via `ssh -i ~/.ssh/binance_futures_tool root@49.12.7.18`)
- Service: `researka-v2.service` (systemd, auto-restart)
- Database: Postgres `researka_v2` (user `researka_v2`)
- Health: http://49.12.7.18:8000/health
- **Shared with elite-trader benchmark** — other dev running 64-paper test against same LLM providers
- LLM providers: MiniMax (primary), MIMO (sparring, ~15s baseline), DeepSeek (fallback)
- Timeout: 60s per provider call (was 30s, MIMO needs headroom under shared load)
- Calibration artifact path: `artifacts/benchmark_baseline.json` (shared by benchmark runners and `/calibration`)
