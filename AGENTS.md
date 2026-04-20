# AGENTS.md - Researka v2

## Purpose
Researka v2 is the backend-only rebuild of Researka's agent-native review gatekeeper runtime.

This repo exists to deliver the smallest clean Python runtime that can do the gatekeeper flow end to end:
- submission intake
- review
- editorial decision
- accepted-publication storage

## Codex Startup - MANDATORY
- Codex reads MCP from `~/.codex/config.toml`, not project `.mcp.json`.
- On every new Codex thread:
  1. Call `get_playbook()`
  2. Call `get_aaa_protocol()`
  3. Call `get_agents_md("Researka")`
  4. Read `PROJECT_STATE.md` before changing anything
- If MCP tools are unavailable, stop and report it.

## Architecture
Keep this repo small and legible.

Top-level modules only:
1. `apps/`
   - entrypoints only
   - `runtime_api/`
   - `worker/`
2. `runtime_core/`
   - business logic only
   - workflow, gates, compiler, providers, repos, ops, prompts
3. `contracts/`
   - schemas, enums, payloads

## Constraints
- Backend only. No frontend in this repo.
- Python only for now.
- Prefer Postgres-backed repos with in-memory parity for tests and local runs.
- Reuse good logic from v1. Do not port the v1 file structure.
- No snapshot-mirror architecture.
- No monolith files by default.
- Stay under ~10k app LOC until proven otherwise.

## Code Rules
- One module, one reason to change.
- Vertical slices first. Platform abstractions later.
- Prefer explicit code over frameworks and wrappers.
- Add a new layer only with written justification.
- Delete before generalizing.

## Default v4 Toolkit
Do not run the whole playbook on every task. Use this default stack unless risk clearly requires more:
1. Constraint surface
2. Smallest design note
3. Deletion check
4. Change-impact map
5. Maker vs judge
6. One discriminating test
7. Output discipline

Escalate only when needed:
- Add adversarial break-it pass for risky workflow or persistence changes
- Add performance budget for provider/runtime hot-path changes
- Add rollback/blast-fence planning before real deployment work

See `specs/v2-session-checklist.md` for the exact session template.

## Testing Rules
- Run `python3 -m pytest -q`
- New workflow logic = new test
- New gate = new test
- New API route = at least one smoke test

## Definition of Done
- The repo can model and execute the submission -> review -> editorial -> publish chain end to end.
- Contracts are explicit.
- Core gates exist.
- Publish is idempotent.
- The codebase stays small, modular, and easy to explain.
