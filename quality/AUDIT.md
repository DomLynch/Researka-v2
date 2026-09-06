# Initial audit - 2026-09-06

Scope: `Researka v2`, the backend checkout used by `18/04 - Researka`.
This tooling task changed no runtime code, deployment, credentials or active developer environment.

## Findings

- Committed baseline: 43 complexity warnings. `create_app` has complexity 133
  and 384 statements; extracting cohesive route groups is a candidate for a
  separate tested refactor, not a tooling-install side effect.
- Four clone fingerprints at 10 lines/100 tokens: OSF/workflow metadata
  construction, two regions in repository implementations, and reviewer/workflow
  processing. Clone detection identifies candidates, not proof of incorrect
  behavior. In-memory/Postgres duplication may be deliberate parity code.
- Two architecture contracts pass across 37 modules: core cannot import apps;
  contracts cannot import core/apps.
- Vulture reports unused `cls` parameters in two Pydantic validators. These are
  required classmethod signatures, not functions to delete.
- Highest mock/assert density in this scan: `test_worker.py` 17/28,
  `test_integrity_workflow.py` 26/60, `test_verify.py` 25/75. Review whether tests
  exercise the real subject; those counts alone do not establish over-mocking.

Concurrent uncommitted changes raised `OpenAICompatibleProvider.complete` to
complexity 11, `ReviewerPanel.complete` to 12, and `reviewer_from_env` to 12.
The new gate reports these rather than including them in the committed-code baseline.
The owning developer should assess/refactor those changes; no automated rewrite
or suppression was applied here. Complexity findings are not runtime bug proof.

## Verification

Tooling fixtures cover clean/violating import graphs, old/new duplicated code,
all three Ruff rules, improving/worsening metrics, line shifts, conditional
functions, and resistance to `noqa`/inherited settings. One independent review
found the last three hardening gaps; each was fixed and regression-tested.

An earlier application suite passed: 556 passed, 1 skipped. During concurrent
edits a rerun hit an import error for `MODEL_QUORUM_POLICY`. The final rerun
collected successfully but returned 7 failed, 549 passed, 1 skipped: the new
reviewer path imports `runtime_core.codex_provider`, not yet present on disk.
The active developer must finish that change and revalidate. The quality gate
also reports the concurrent complexity regressions; this is not a clean-tree claim.
CI configuration is verified separately when pushed.

## Pre-deployment recheck

At runtime commit `152ca67189170843a316a12b619d712bea33065b`, the owning developer
has resolved the intervening failures: 720 application tests passed, 1 skipped;
12 tooling tests passed; complexity is 42 findings with zero new/worsened;
both architecture contracts pass and no new duplication is reported.
The original baselines remain unchanged. Deployment of these tooling files
does not require a service restart or a runtime dependency change.
