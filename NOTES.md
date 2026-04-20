# NOTES

## Constraint surface
- Backend only
- Python only
- Keep module count small
- Keep v2 separate from v1 frontend/runtime sprawl
- Must NOT recreate snapshot-monster architecture
- Gatekeeper only: submission -> intake -> review -> editorial -> publish
- No writer-role logic in this repo

## Current objective
Lock Submission Template v1 into the real intake path so docs, sanitizer, and runtime enforce the same contract.

## Task mode
repair

## Success condition
Intake rejects submissions that miss the documented contract: required sections, research-question word budget, minimum citation count, recency ratio, and source-bundle schema.

## Risk tier
medium

## Evidence available
- MiniMax official model/API docs
- DeepSeek official API docs
- user-provided Mimo compatibility endpoint details
- v4 playbook + AAA protocol
- current single-provider judge seam

## Likely files
- `contracts/templates.py`
- `contracts/submissions.py`
- `runtime_core/workflow.py`
- `runtime_core/compiler.py`
- `tests/*`
- `docs/SUBMISSION_TEMPLATE_v1.md`
- `NOTES.md`
- `DECISIONS.md`

## Change-impact map
- Surfaces touched: submission contract, intake gate, compiler ordering, tests, notes/docs
- Blast radius: medium
- Verify: pytest, py_compile, template rejection tests, end-to-end publish path

## Deletion opportunity
- Keep a single template source of truth. Do not duplicate required sections or keep stale section-order hints.

## Branches considered
- A: keep template as docs only
- B: enforce template inside intake with one helper
- C: enforce template at API validation time

## Rejected paths
- A rejected: green docs with a half-wired runtime is false confidence
- C rejected: request-layer rejection would skip runtime decision objects and intake forensics

## Winning path
- B: keep one intake stage, but drive it from the template contract with a small helper and discriminating tests

## Open risks
- Reviewer rubric still needs calibration across real accept/revise/reject examples
- No concurrency race test yet for two simultaneous Postgres workers
- Source-bundle schema is still intentionally minimal; URL/DOI presence is documented but not yet required
- Postgres parity was not rerun here because `TEST_POSTGRES_DSN` is unset

## Next validation step
- Run three live contract-compliant submissions through `judge_panel`, then add one Postgres concurrency lease test.
