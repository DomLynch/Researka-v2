# Publication recovery: reproduce before refactoring

## Ownership and success

The V3 developer owns the outgoing manuscript and exact source/evidence bundle.
The platform developer owns intake, review, decision and delivery. A single
failure packet links their work; neither side should silently change the other's
contract or weaken scientific gates to make a test pass.

"Submitted", "accepted" and "published" are separate states. A publication claim
requires the matching submission ID, persisted publication ID, canonical public
record and a fresh unauthenticated request showing the intended paper/version.
Record required delivery statuses under the active release policy. Do not assume
OSF is universally mandatory from one historic example or mint a DOI as a test.
Offline tests below do NOT establish public availability or research quality.

## Commands (from the platform root)

```sh
UV_PROJECT_ENVIRONMENT=.tmp/contract-venv uv sync --locked --extra dev
UV_PROJECT_ENVIRONMENT=.tmp/contract-venv make contract-test
UV_PROJECT_ENVIRONMENT=.tmp/contract-venv uv run --locked --extra dev python -m scripts.replay_intake_contract /path/to/request.json
UV_PROJECT_ENVIRONMENT=.tmp/contract-venv uv run --locked --extra dev python -m scripts.replay_intake_contract /path/to/private-export.json --submission-id EXACT_ID
```

The replay reads a request with explicit `sections`/`source_bundle`, or an array
of stored records (`type`, `id`, `title`, `metadata`) with an exact ID. It reports
the input-byte SHA256 and template gate booleans, without printing manuscript
text. Exit 0 means template gates passed, 1 means at least one failed, 2 means
invalid input. It does not call models, resolve sources, run workers or publish.
In particular, passing it cannot rebut a `SOURCE_EVIDENCE_MATCH` rejection.

The new tests are collected by ordinary pytest and therefore the existing CI
job, which installs the locked `dev` extra. No additional agent hook is needed.
Schemathesis generates 20 bounded examples for POST `/submissions` in memory;
it checks server errors, expected intake responses and zero publications. It is
not exhaustive OpenAPI conformance or live API fuzzing. Known schema gap: the
route can return 400 for an unknown parent/body parse failure, while its OpenAPI
responses declare only 200/422. Runtime response documentation remains with the
platform developer; these tests assert those specific 400 reasons explicitly.

Syrupy snapshots record the meaningful intake outcomes of a synthetic paired
case: a parenthesized DOI present in the bundle versus absent. Explicit assertions
check the expected decisions before the snapshot. No gate under test is mocked.
External DOI metadata/integrity checks remain disabled by existing fixtures;
both new test modules prohibit network connections. Existing
`tests/test_e2e_publish.py` covers synthetic review/delivery/idempotency with mocked
external services. None of these tests proves those services work in production.

## One failure packet, one discriminating test

1. Record canonical checkout, commit, deployed release, submission ID and parent
   ID. Never join a parent's decision to a child's regenerated payload.
2. Freeze the original outgoing request plus Core's stored submission and decision
   locally under `.quality-reports/` (ignored). Hash each file. Compare canonical
   body and bundle content before claiming exact wire equivalence; export hashes
   alone do not establish it. Keep unpublished manuscripts, reviews and credentials
   out of commits, snapshots, prompts and public issue reports.
3. Replay the implicated boundary. A template pass narrows the search but says
   nothing about external source verification. For source mismatch, preserve the
   authoritative response and test the real verifier with only HTTP transport
   substituted. Missing authoritative evidence is a gap, not permission to pass.
4. Minimize into a synthetic regression with a legitimate passing control and a
   deliberate failing variation. Show the old implementation fails that test.
5. Fix the first proven divergence, not every large module. Run the targeted test,
   existing end-to-end tests, full pytest and mypy before owner review/deployment.
6. Review snapshot changes line by line. Use `--snapshot-update` only for an
   intentional, justified contract change; never auto-update snapshots in CI.

Keep existing Semble/CodeGraph discovery and quality ratchets. Defer a broad
cleanup framework, additional review hooks and LibCST migrations until a specific
replay identifies a necessary behavior-preserving transformation.
