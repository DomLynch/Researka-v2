from __future__ import annotations

from contracts import FailureClass

_PATTERNS: list[tuple[FailureClass, tuple[str, ...]]] = [
    (FailureClass.STRUCTURE_GATE, ("structure_gate",)),
    (FailureClass.COMPILE_BLOCKER, ("compile_rapid_publication_blocked",)),
    (FailureClass.VALIDATION_ERROR, ("validation error",)),
    (FailureClass.QUALITY_GATE, ("quality_gate_failed",)),
    (FailureClass.PUBLISH_DEFERRED, ("publication deferred",)),
    (FailureClass.DB_TIMEOUT, ("canceling statement due to statement timeout",)),
    (FailureClass.JOB_TIMEOUT, ("job_timeout_exceeded",)),
    (
        FailureClass.DB_CONNECTION_BAD,
        ("connection already closed", "server closed the connection", "connection to server", "ssl syscall error", "eof detected"),
    ),
    (FailureClass.TARGET_NOT_FOUND, ("unknown submission", "unknown proposal", "unknown review")),
    (
        FailureClass.STALE_TARGET,
        ("research object not found", "unknown research object", "target object_deleted", "target already terminal"),
    ),
    (FailureClass.ORPHAN_REFERENCE, ("uuid(",)),
    (FailureClass.REVIEW_MISSING, ("requires at least one completed review",)),
    (FailureClass.EXACT_QUOTE_MISSING, ("expected at least one exact quote",)),
    (FailureClass.PROVIDER_ERROR, ("provider_error:",)),
    (FailureClass.SYSTEM_UNAVAILABLE, ("system_unavailable:",)),
    (FailureClass.PUBLISH_GATES_FAILED, ("publish_gates_failed", "publish_blocked_by_integrity")),
]


def classify_failure_reason(reason: str | None) -> FailureClass:
    if not reason:
        return FailureClass.OTHER
    lowered = reason.lower()
    for failure_class, needles in _PATTERNS:
        if any(needle in lowered for needle in needles):
            return failure_class
    return FailureClass.OTHER
