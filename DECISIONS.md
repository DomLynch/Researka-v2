# DECISION JOURNAL

## 2026-05-27 — Public Self-Issued Agent Keys
**Decision:** Let external agents self-register through `/agents/register`, issuing normal hashed per-agent API keys with conservative daily submission limits plus per-IP and global registration throttles.
**Why:** The MCP can open to third-party agents without exposing `RESEARKA_V2_ADMIN_KEY` or creating a second auth system.
**Alternatives rejected:**
- Public admin key proxy — rejected because it expands the blast radius of the admin secret.
- New OAuth/account system — rejected because the current need is scoped agent keys, not human sessions.
- Unlimited anonymous submission — rejected because review cost and spam risk need a default fence.
**Revisit if:** abuse volume requires CAPTCHA, email/domain verification, paid quotas, or stronger identity checks.

## 2026-05-27 — Durable Public Registration Throttles
**Decision:** Store public registration throttle counters in `/var/lib/researka-v2/rate_limits.db` via SQLite `rl_counters`, with a SQLite `flags.public_registration` kill switch checked on every registration attempt.
**Why:** Self-service registration should survive API restarts, support an immediate ops-only panic switch, and avoid raw IP persistence while keeping MCP as a transport wrapper and Researka v2 as the canonical enforcement layer.
**Alternatives rejected:**
- MCP-side throttling — rejected because multiple entrypoints would drift and duplicate abuse policy.
- Raw IP counter storage — rejected because throttle enforcement does not require storing the address.
- Postgres runtime counters — rejected for this launch because registration abuse controls should be a small local guard independent of app repository migrations.
- Cached kill switch — rejected because incident response needs the next request to observe the flag.
- Redis/WAF-first dependency — rejected because this launch needs one durable local counter, not a new service.
**Revisit if:** registration traffic needs sliding-window limits, distributed edge enforcement, verified-agent tiers, or abuse-intel integration.

## 2026-04-25 — Replace paid DeepSeek/MiniMax live panel slots with OpenRouter paid models
**Decision:** Keep MiMo V2.5 Pro as the primary live reviewer, move the sparring reviewer to OpenRouter `google/gemma-4-31b-it`, and move fallback/tiebreak to OpenRouter `mistralai/mistral-small-2603`.
**Why:** DeepSeek pricing is now an operational risk, MiniMax subscription is being retired, and the panel still needs non-Xiaomi adjudication diversity. A/B feedback favored Gemma as reviewer and Mistral as judge; OpenRouter currently lists both target models as paid OpenRouter models with 262K context.
**Alternatives rejected:**
- Keep DeepSeek as tiebreak — rejected because it can burn cost unexpectedly.
- Keep MiniMax as reviewer — rejected because it forces a second migration when the subscription expires.
- Keep Nemotron as reviewer — rejected because the Gemma/Mistral A/B had slightly better quality at similar wall time.
- Use MiMo Flash as reviewer or judge — rejected because same-family review weakens disagreement signal.
**Revisit if:** OpenRouter paid endpoints disappear, JSON mode fails in live probes, or benchmark/gold-set quality regresses by more than 10%.

## 2026-04-19 — Start v2 as backend-only
**Decision:** Rebuild Researka v2 as a backend-only Python runtime, separate from the frontend.
**Why:** The current pain is runtime architecture, not the website. Separating frontend removes drag and keeps the rebuild small.
**Alternatives rejected:**
- Full-stack rebuild — rejected because it mixes concerns and slows the core runtime recovery.
- Go rewrite — rejected because the limiting factor is architecture and workflow quality, not CPU throughput.
- Direct v1 refactor only — rejected because v1 structure is already carrying too much legacy shape.
**Revisit if:** The runtime stabilizes and a dedicated frontend boundary contract needs expansion.

## 2026-04-19 — Use 3 top-level modules
**Decision:** Keep only `apps/`, `runtime_core/`, and `contracts/`.
**Why:** This preserves hard boundaries without creating premature module sprawl.
**Alternatives rejected:**
- 8-9 package split immediately — rejected because it invites taxonomy theater before real usage pressure.
**Revisit if:** Clear ownership and deployment boundaries emerge later.

## 2026-04-19 — Pivot v2 to gatekeeper-only
**Decision:** Strip writer-role logic from v2 and keep only submission intake, review, editorial, and accepted-publication storage.
**Why:** External agents are the authors. Researka's asymmetric leverage is the review gate and acceptance brand, not an in-house self-writing loop.
**Alternatives rejected:**
- Keep discovery / rebuttal / revision-discovery in v2 — rejected because they recreate the same mixed-role architecture that made v1 fragile.
- Build a frontend/public reader site in this repo — rejected because backend contracts are the product for now.
**Revisit if:** A separate writer/reference-agent repo needs a contract extension.

## 2026-04-19 — Use a three-model judge stack behind the existing review seam
**Decision:** Replace the single live reviewer with a judge panel: MiniMax `MiniMax-M2.7-highspeed` as primary reviewer, `mimo-v2.5-pro` as sparring partner, and `deepseek-reasoner` as fallback/tiebreaker, while keeping deterministic review only for tests and offline runs.
**Why:** This removes single-model outage/bias risk and gives the gatekeeper an institutionally defensible two-reviewer-plus-tiebreak workflow without adding new runtime stages.
**Alternatives rejected:**
- Single-provider live review — rejected because one model failure or bias can dominate the lane.
- A separate second review workflow stage — rejected because it adds architecture rather than capability.
**Revisit if:** One reviewer becomes clearly dominant enough to reduce panel calls, or if throughput/cost pressure requires routing fewer submissions to the full panel.

## 2026-04-19 — Add retry and Alembic without widening the runtime
**Decision:** Add a tiny retry/backoff loop only for transient reviewer-provider failures, remove the last silent editorial `accept` fallback, and scaffold Alembic with one baseline revision.
**Why:** These are the cheapest fixes that materially raise runtime credibility without changing the gatekeeper architecture or inflating LOC.
**Alternatives rejected:**
- Leave provider calls single-shot and rely on operator restarts — rejected because transient blips should not stall the review lane.
- Delay migrations until the first schema change — rejected because that repeats the exact debt pattern v1 accumulated.
- Add a broader job-retry framework — rejected because the provider seam only needed a narrow transient retry, not a new subsystem.
**Revisit if:** Provider instability or schema churn grows enough to justify broader retry policy or richer migration tooling.

## 2026-04-20 — Enforce Submission Template v1 at intake
**Decision:** Keep the submission contract in `contracts/templates.py` and `contracts/submissions.py`, then enforce it inside the intake stage with a single helper instead of leaving it as docs-only guidance.
**Why:** The earlier tranche wired required headings but left numeric constraints and source-bundle schema mostly unenforced. Intake is the right boundary because it preserves deterministic rejection, runtime events, and decision-object forensics without widening the API surface.
**Alternatives rejected:**
- Docs-only template — rejected because it creates a false AAA signal while bad submissions still slip through.
- API-layer rejection only — rejected because malformed-but-submitted work should still get intake-stage rejection and forensic trace.
- A separate template-validation subsystem — rejected because one helper plus tests solves the real problem.
**Revisit if:** External agents repeatedly fail on richer source metadata and URL/DOI requirements need to become mandatory, not just documented.
