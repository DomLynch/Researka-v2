# PROJECT_STATE.md - Researka v2

## Current Sprint
Week of: 2026-04-19
Focus: backend-only runtime foundation for the gatekeeper flow: submission -> review -> editorial -> publish.

## Goal
Build a clean Python runtime that can replace the current hot-path publishing logic without dragging frontend or legacy product baggage into the rebuild.

## In Progress
- [x] Repo initialized
- [x] Architecture contract written
- [x] Minimal vertical slice scaffolded
- [x] Postgres repo implementation
- [x] Reviewer panel judge stack
- [x] Baseline Alembic migration scaffold
- [x] End-to-end publish flow with real persistence
- [ ] Shadow-mode comparison against v1

## Non-Goals
- Frontend rebuild
- Event-sourced rewrite on day one
- Admin surfaces
- Legacy compatibility theater

## Risks
- Rebuild drift if v2 absorbs writer-side concerns after the gatekeeper pivot
- Over-abstracting before one clean end-to-end slice exists
- Under-testing publish blockers

## Next Validation Step
- Run three live calibration submissions through the MiniMax + Mimo + DeepSeek judge stack, then add one Postgres concurrency lease test.
