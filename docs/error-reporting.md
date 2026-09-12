# Core operational error reporting

Sentry is opt-in using `SENTRY_DSN` from the dedicated Core project. Configure it
in protected `/etc/researka/sentry-core.env`, referenced by an optional systemd
EnvironmentFile drop-in for both `researka-v2.service` and
`researka-v2-worker.service`. `SENTRY_ENVIRONMENT` accepts production, staging,
development or test; it defaults to `RESEARKA_V2_ENV`. Release is
`researka-core@<SERVICE_GIT_SHA>` and the service tag is api or worker. Restart
both services after changing environment. An empty DSN disables reporting;
invalid configuration logs a constant warning and does not stop Core.

The dedicated SDK client has no automatic integrations, traces, profiles, logs,
metrics, sessions, breadcrumbs, Spotlight routing or client reports. It receives a constructed
event, never the original exception, request or job payload. Events retain only
builtin exception type (other types become ApplicationError), redacted value,
Core Python file/line locations, release/environment, static service/stage,
technical failure class and UUID job/target identifiers. Outgoing events remove
SDK-added context. No manuscript, evidence, prompts, URLs, auth, request bodies,
locals, source lines, attachments or exception messages are supplied.

Unexpected API and app-factory startup errors are re-raised with existing behavior. Handled HTTP
errors and normal REVISE/REJECT outcomes are excluded. Worker reporting covers
terminal technical job failures, lease-renewal exceptions, maintenance, loop
and startup failures. Scheduled retries and scientific/validation gate outcomes
are excluded. Capture/flush failures cannot change review or retry behavior.

Validation: `tests/test_error_reporting.py` captures actual SDK envelopes in an
in-memory transport, exercising real ASGI errors and the worker retry path with
canary secrets. This proves local capture and filtering, not Sentry ingestion.
After deployment, send a synthetic `RuntimeError` with stage `smoke` through the
deployed helper, flush, then retrieve the resulting event in the dedicated Core
Sentry project and confirm its release, environment and sanitized fields. Do
not add a public crash endpoint or send a real manuscript as a smoke test.

Discovery used three Semble queries for existing reporting, API startup and
worker failures, followed by CodeGraph call/impact inspection. No existing SDK
wiring was found. Explicit capture was selected over automatic request/provider
integrations to keep sensitive research data out of the SDK. Gates are unchanged.
