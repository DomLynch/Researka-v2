# Subscription reviewer cutover

Core uses `gpt-5.6-sol` high and `gpt-5.6-terra` medium through the
ChatGPT-authenticated Codex CLI. Calls consume the subscription allowance;
zero API spend is not unlimited usage. No API key is forwarded to Codex.
One failed reviewer permits one `z-ai/glm-5.3-flash` OpenRouter request.
Valid disagreement never triggers paid fallback. Two failed GPT reviewers
cannot be replaced by a single GLM vote. Acceptance still needs two distinct
valid agreeing models and an authenticated, manuscript-bound review receipt.
This is model diversity, not independence between providers.

The review response permits up to 200,000 input and 12,000 output tokens.
Returned usage is validated after execution; these checks are not a billing
cap. The input allowance includes manuscript, evidence bundle, and instructions.

## Deploy

1. Run the full tests, mypy, Ruff, and `make quality`; audit the diff twice.
   Install the CLI separately from agent runtimes:
   `npm install --prefix /opt/researka-codex --ignore-scripts --no-audit --no-fund @openai/codex@0.153.1`.
   The older system CLI 0.116.0 does not support the isolation flags.
2. Install `ops/codex-review.env.example` as `/etc/researka/codex-review.env`.
   Install `ops/systemd/researka-v2-codex-review.conf` as `99-codex-review.conf`
   in both API and worker systemd drop-in directories. Keep other drop-ins.
3. Provision approved ChatGPT CLI authentication in
   `/var/lib/researka-v2/codex`, owned by `researka` (directory 0700, auth 0600).
   Never log credentials. Both services must use the existing same review
   attestation secret. No agent code, schedules, or database schema change.
4. Reload systemd; drain active jobs before stopping services. Fetch the
   reviewed commit with `git pull --ff-only`, then start both services.
   `KillMode=mixed` allows an in-flight worker to finish; shutdown has 1800s
   for two 600s GPT calls plus a 60s backup and processing overhead.
   Existing lease heartbeat remains enabled.
5. Verify login and small Sol/Terra requests as `researka` inside the same
   service sandbox. Check actual process routing, `/version`, and `/health`.
6. Requeue each original failed review once with a deterministic recovery
   job ID. Preserve the original submissions and failed attempt history.
   Follow resulting decisions/publications; never override an editorial gate.

Rollback: keep the previous SHA and nonsecret config backup. Stop/drain the
worker before switching releases; do not roll back to an exhausted provider
and resume spending blindly. Leave failed work recorded for operator recovery.
