from __future__ import annotations

from collections import Counter

from contracts import RuntimeEvent

from .failure_classifier import classify_failure_reason


def classify_failure(reason: str):
    return classify_failure_reason(reason)


def summarize_events(events: list[RuntimeEvent]) -> dict[str, int]:
    return dict(Counter(event.event_type.value for event in events))
