from __future__ import annotations

from scripts.run_benchmark import (
    EXPECTED_BY_QUALITY,
    actual_label_for_record,
    aggregate,
    expected_decision_for_paper,
    generate_papers,
)


def test_generate_papers_assigns_expected_decisions() -> None:
    papers = generate_papers(4)
    assert [p["_benchmark_quality"] for p in papers] == ["high", "high", "medium", "medium"]
    assert [p["_benchmark_expected_decision"] for p in papers] == ["accept", "accept", "revise", "revise"]


def test_expected_decision_defaults_from_quality() -> None:
    assert expected_decision_for_paper({"_benchmark_quality": "high"}) == EXPECTED_BY_QUALITY["high"]
    assert expected_decision_for_paper({"_benchmark_quality": "medium"}) == EXPECTED_BY_QUALITY["medium"]
    assert expected_decision_for_paper({"_benchmark_quality": "low"}) == EXPECTED_BY_QUALITY["low"]
    assert expected_decision_for_paper({"_benchmark_quality": "broken"}) == EXPECTED_BY_QUALITY["broken"]
    assert expected_decision_for_paper({"_benchmark_expected_decision": "accept", "_benchmark_quality": "low"}) == "accept"


def test_aggregate_emits_real_accuracy_and_confusion_matrix() -> None:
    records = [
        {
            "paper_id": 1,
            "title": "High paper",
            "quality": "high",
            "domain": "longevity",
            "expected_decision": "accept",
            "decision": "accept",
            "outcome": "accept",
            "stage_reached": "done",
            "route": "consensus",
            "cost_usd": 0.01,
            "duration_s": 5.0,
            "error": None,
        },
        {
            "paper_id": 2,
            "title": "Medium paper",
            "quality": "medium",
            "domain": "ai-ethics",
            "expected_decision": "revise",
            "decision": "revise",
            "outcome": "revise",
            "stage_reached": "done",
            "route": "consensus",
            "cost_usd": 0.02,
            "duration_s": 6.0,
            "error": None,
        },
        {
            "paper_id": 3,
            "title": "Low paper",
            "quality": "low",
            "domain": "genomics",
            "expected_decision": "reject",
            "decision": "revise",
            "outcome": "revise",
            "stage_reached": "done",
            "route": "consensus",
            "cost_usd": 0.03,
            "duration_s": 7.0,
            "error": None,
        },
        {
            "paper_id": 4,
            "title": "Broken paper",
            "quality": "broken",
            "domain": "neuroscience",
            "expected_decision": "reject",
            "decision": "reject",
            "outcome": "intake_rejected",
            "stage_reached": "intake",
            "route": None,
            "cost_usd": 0.0,
            "duration_s": 1.0,
            "error": ["intake gate failure"],
        },
    ]

    agg = aggregate(records)

    assert agg["correct"] == 3
    assert agg["accuracy"] == 0.75
    assert agg["confusion_matrix"]["accept"]["accept"] == 1
    assert agg["confusion_matrix"]["revise"]["revise"] == 1
    assert agg["confusion_matrix"]["reject"]["revise"] == 1
    assert agg["confusion_matrix"]["reject"]["reject"] == 1
    assert len(agg["mismatches"]) == 1
    assert agg["mismatches"][0]["paper_id"] == 3
    assert agg["by_quality"]["medium"]["accuracy"] == 1.0
    assert agg["by_quality"]["low"]["accuracy"] == 0.0
    assert agg["by_quality"]["broken"]["accuracy"] == 1.0


def test_actual_label_for_record_uses_reject_for_intake_rejection() -> None:
    assert actual_label_for_record({"decision": None, "outcome": "intake_rejected"}) == "reject"
    assert actual_label_for_record({"decision": "revise", "outcome": "review"}) == "revise"
