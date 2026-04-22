from __future__ import annotations

from scripts.run_benchmark import (
    EXPECTED_BY_QUALITY,
    actual_label_for_record,
    aggregate,
    expected_decision_for_paper,
    generate_papers,
)
from scripts.calibrate_reviewer import build_micro_papers, compare_aggregates, load_micro_set, ordered_micro_ids


def test_generate_papers_assigns_expected_decisions() -> None:
    papers = generate_papers(4)
    assert [p["_benchmark_quality"] for p in papers] == ["high", "high", "medium", "medium"]
    assert [p["_benchmark_expected_decision"] for p in papers] == ["accept", "accept", "revise", "revise"]


def test_generate_papers_separates_high_and_low_manuscripts() -> None:
    papers = generate_papers(8)
    high = papers[0]["sections"]
    low = papers[6]["sections"]

    assert "publication-ready bounded conclusion" in high["Conclusion"]
    assert "scope reset" in low["Conclusion"]
    assert "directly supported" in high["Limitations"]
    assert "indirect, heterogeneous, and too weakly matched" in low["Limitations"]


def test_expected_decision_defaults_from_quality() -> None:
    assert expected_decision_for_paper({"_benchmark_quality": "high"}) == EXPECTED_BY_QUALITY["high"]
    assert expected_decision_for_paper({"_benchmark_quality": "medium"}) == EXPECTED_BY_QUALITY["medium"]
    assert expected_decision_for_paper({"_benchmark_quality": "low"}) == EXPECTED_BY_QUALITY["low"]
    assert expected_decision_for_paper({"_benchmark_quality": "broken"}) == EXPECTED_BY_QUALITY["broken"]
    assert expected_decision_for_paper({"_benchmark_expected_decision": "accept", "_benchmark_quality": "low"}) == "accept"
    assert expected_decision_for_paper({"_benchmark_editorial_verdict": "reject", "_benchmark_quality": "high"}) == "reject"


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


def test_micro_set_selects_balanced_fixture() -> None:
    spec = load_micro_set("calibration/calibration_micro_set.json")
    assert ordered_micro_ids(spec) == [1, 2, 10, 11, 19, 3, 4, 5, 6, 12, 7, 8, 16, 17, 25, 9, 18, 27, 36, 45]
    papers = build_micro_papers(spec)
    assert len(papers) == 20
    assert [paper["_benchmark_quality"] for paper in papers[:5]] == ["high"] * 5
    assert [paper["_benchmark_quality"] for paper in papers[5:10]] == ["medium"] * 5
    assert [paper["_benchmark_quality"] for paper in papers[10:15]] == ["low"] * 5
    assert [paper["_benchmark_quality"] for paper in papers[15:20]] == ["broken"] * 5


def test_compare_aggregates_emits_quality_deltas() -> None:
    previous = {
        "accuracy": 0.55,
        "accepts": 0,
        "revises": 18,
        "rejects": 2,
        "by_quality": {
            "high": {"accuracy": 0.0, "accept_rate": 0.0, "revise_rate": 1.0, "reject_rate": 0.0},
            "medium": {"accuracy": 1.0, "accept_rate": 0.0, "revise_rate": 1.0, "reject_rate": 0.0},
            "low": {"accuracy": 0.0, "accept_rate": 0.0, "revise_rate": 1.0, "reject_rate": 0.0},
            "broken": {"accuracy": 1.0, "accept_rate": 0.0, "revise_rate": 0.0, "reject_rate": 1.0},
        },
    }
    current = {
        "accuracy": 0.75,
        "accepts": 4,
        "revises": 10,
        "rejects": 6,
        "by_quality": {
            "high": {"accuracy": 0.4, "accept_rate": 0.4, "revise_rate": 0.6, "reject_rate": 0.0},
            "medium": {"accuracy": 1.0, "accept_rate": 0.0, "revise_rate": 1.0, "reject_rate": 0.0},
            "low": {"accuracy": 0.6, "accept_rate": 0.0, "revise_rate": 0.4, "reject_rate": 0.6},
            "broken": {"accuracy": 1.0, "accept_rate": 0.0, "revise_rate": 0.0, "reject_rate": 1.0},
        },
    }

    delta = compare_aggregates(current, previous)

    assert delta["accuracy_delta"] == 0.2
    assert delta["accept_delta"] == 4
    assert delta["reject_delta"] == 4
    assert delta["by_quality"]["high"]["accept_rate_delta"] == 0.4
    assert delta["by_quality"]["low"]["reject_rate_delta"] == 0.6
