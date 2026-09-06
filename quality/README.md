# Development quality checks

Shared by Codex and Claude in this repository, not the V3 writer or the website.
Run `make quality-setup` once (uv and npm required), then `make quality` before
completing a code change. Tools live in `.venv-quality`, leaving the active
developer's `.venv` and all production dependencies unchanged.

## What runs

- Existing edit hooks still run ordinary Ruff checks. No new paid auditor or MCP.
- `make quality`: Ruff defaults plus a complexity ratchet (C901, PLR0912,
  PLR0915), two Import Linter architecture contracts, jscpd new-clone gate,
  and real positive/negative tooling tests.
- GitHub Actions runs the same command on pushes and pull requests **after
  these files are committed and pushed**. Branch-protection requirements must
  be configured separately; this file alone cannot enforce merge protection.
- `make dead-code`: Vulture at 100% confidence, report-only. Unused callback
  parameters are not evidence that the callback can be deleted.
- `make mock-audit`: AST counts to prioritize manual test review, not a pass/fail
  rule. External-service stubs can be appropriate; inspect whether the actual
  behavior under test is replaced. Counts omit aliases and indirect assertions.

Complexity baselines identify file + qualified function + rule, not line
numbers. Existing or improved metrics pass; new functions and higher metrics
fail.
Complexity uses fixed Ruff defaults and ignores `noqa`; inherited configuration
cannot silently relax these limits. Ordinary Ruff lint explicitly uses this
repo's `pyproject.toml` and disables automatic fixes.
Duplication scans production Python only, minimum 10 lines/100 tokens;
small duplicates and semantic equivalents are outside this check's coverage.
Tests and generated artifacts are not part of either debt baseline.

Baselines are deliberate accepted debt, not auto-updated. Inspect improvements
or explicitly justify accepted debt before updating; never refresh just to make
CI green. The baseline files and rule thresholds require code review.
Initial debt is from `9e4d51b8e8826deff202c79f9bc524049bd40ce2`: 43 complexity
findings and four clone fingerprints. Concurrent, uncommitted developer edits
are not included in this accepted baseline.

```sh
.venv-quality/bin/python quality/check_complexity.py --update-baseline
npm exec --yes --package=jscpd@5.1.2 -- jscpd . --config quality/jscpd.json --baseline quality/duplication-baseline.json --update-baseline
```

## Tools deliberately not made mandatory

Semble + Codegraph remain the discovery/impact tools. No extra code-search MCP.
Semgrep is deferred until a specific recurring unsafe pattern has a tested
rule; it cannot generically prove two functions have equivalent error handling.
Skylos overlaps existing lint/dead-code checks and is not added.
Hypothesis/stateful tests and narrow mutmut runs need a selected behavioral
contract and belong with its developer, not a blanket hook. pytest-randomly,
branch coverage, VizTracer and py-spy remain targeted diagnostics; they are not
enabled for every edit. No OpenTelemetry instrumentation or VPS changes here.
