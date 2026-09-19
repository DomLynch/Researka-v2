# Release verification gaps — September 19, 2026

## Trigger and evidence
Review of the Claude/Kimi handover at Core `28f05be` found the normal suite green (1099 pass / 1 skip), but GitHub run 35370225619 failed and local quality/mypy checks reproduced the failures. Real Postgres 17.9 ran 35 repository tests successfully and failed the lease-reclaim ordering test.

## Root causes
- `create_app` exceeded its existing complexity/statement baseline after additions.
- Once complexity passed, the duplicated update-versus-merge enqueue transaction exceeded the existing clone gate.
- Eight type errors came from assigning a fake pool to a concrete pool attribute, reading an optional object without narrowing, and accessing panel-specific fields through a provider protocol.
- `claim_next_job` captured `now` before recording a reclaim, then backdated the subsequent lease event with that old timestamp. Sorting by timestamp placed the reclaim last. Equal timestamps also lacked an insertion-order tie-breaker.
- Seven database tests returned early without a DSN and therefore appeared as passes. CI supplied no Postgres instance.

## Local correction
Extract repository selection from API route construction; share the two metadata/enqueue transaction bodies; retain explicit TEXT/jsonb casts, parameter binding, atomic commit and parsing before commit. Narrow test values without ignores. Use event-construction timestamps for both repository implementations and `ts, id` order for Postgres. Keep the original lease assertion and verify the per-target sequence too. Add real database rollback coverage for both merge and replacement when enqueue fails on a duplicate job ID. Close fixture-owned pools. Configure Postgres 16 in CI and explicit local skips.

## Scope and prevention
No quality baselines, scientific thresholds or review rules were relaxed. No production database or service was changed. Tests only used newly created disposable local databases. CI's next pushed run must independently verify the Postgres 16 environment; local database verification used PostgreSQL 17.9.

The integration includes pool lifecycle, metadata merge and structured feedback improvements already present as ancestors of HEAD. The outstanding multi-object finalization race, producer-side feedback wiring and independent calibration are not claimed fixed here.

## Validation
- Full suite with disposable PostgreSQL 17.9: **1102 passed**, no skips, 2 snapshots passed; 49.88s. Six warnings concern the existing deprecated FastAPI shutdown-event API.
- `make quality`: exit 0; 41 existing complexity findings, zero new/worsened; zero new clones; both import contracts kept; 12 quality tests pass.
- `python -m mypy .`: exit 0, no issues in 109 source files.
- `python -m ruff check apps runtime_core contracts tests`: exit 0.
- `git diff --check`: clean. CI YAML parses and the full-suite test step receives the scratch database DSN.
- All ten handover/change commits checked are ancestors of local HEAD `28f05be`; no re-merge was necessary. Repairs and these notes are local uncommitted changes, not a new production release.

Raw receipts are in the workspace audit directory `../Researka-Review-2026-09-19/`: `release-full-postgres-tests.txt`, `release-quality.txt`, `release-mypy.txt`, `release-ruff.txt`, and `integrated-commits.json`. The deployed baseline's older failing GitHub run remains historical evidence until a new commit is pushed and tested.
