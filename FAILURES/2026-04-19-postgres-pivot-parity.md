# Failure: Postgres parity looked broken during the gatekeeper pivot

## Date
2026-04-19

## Trigger
The submission-rooted gatekeeper pivot landed while the Postgres end-to-end test still reflected assumptions from the earlier autonomous publishing shape.

## Symptom
The Postgres happy-path test reported a publication parent mismatch and made the new gatekeeper flow look unstable, even though isolated reproduction showed the queue and publication path were behaving correctly.

## Root cause
The failure signal came from testing during a partially-applied pivot: routes, stages, and object ancestry changed quickly, but the debugging path did not first isolate the database state and event sequence. That created a false sense that persistence or dedupe logic was broken when the real issue was stale assumptions around the new flow.

## Fix
Reproduce the failing test in isolation against the temp Postgres instance, inspect queued jobs and created objects after each step, then align the runtime and tests around the submission -> review -> editorial -> publish path. After the pivot stabilized, rerun the full Postgres suite.

## Prevention
When doing structural pivots, first prove the new event/stage sequence in one isolated reproduction before concluding that persistence is broken. Update the end-to-end discriminating test as soon as the root object or stage set changes.

## Related skills
- v4 default stack: change-impact map + discriminating test + maker/judge separation
