#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.run_benchmark import aggregate, generate_papers
from scripts.run_vps_benchmark import submit_and_drain


def load_micro_set(path: str) -> dict:
    return json.loads(Path(path).read_text())


def ordered_micro_ids(spec: dict) -> list[int]:
    ids: list[int] = []
    for quality in ("high", "medium", "low", "broken"):
        ids.extend(int(value) for value in spec["paper_ids"].get(quality, []))
    return ids


def build_micro_papers(spec: dict, *, pool_size: int = 200) -> list[dict]:
    paper_map = {paper["_benchmark_paper_id"]: paper for paper in generate_papers(pool_size)}
    return [paper_map[paper_id] for paper_id in ordered_micro_ids(spec)]


def micro_records_from_artifact(spec: dict, artifact_path: str) -> list[dict]:
    artifact = json.loads(Path(artifact_path).read_text())
    record_map = {int(record["paper_id"]): record for record in artifact.get("papers", [])}
    return [record_map[paper_id] for paper_id in ordered_micro_ids(spec)]


def compare_aggregates(current: dict, previous: dict) -> dict:
    delta = {
        "accuracy_delta": round(current["accuracy"] - previous["accuracy"], 3),
        "accept_delta": int(current["accepts"] - previous["accepts"]),
        "revise_delta": int(current["revises"] - previous["revises"]),
        "reject_delta": int(current["rejects"] - previous["rejects"]),
        "by_quality": {},
    }
    for quality in ("high", "medium", "low", "broken"):
        current_stats = current.get("by_quality", {}).get(quality, {})
        previous_stats = previous.get("by_quality", {}).get(quality, {})
        delta["by_quality"][quality] = {
            "accuracy_delta": round(float(current_stats.get("accuracy", 0.0)) - float(previous_stats.get("accuracy", 0.0)), 3),
            "accept_rate_delta": round(float(current_stats.get("accept_rate", 0.0)) - float(previous_stats.get("accept_rate", 0.0)), 3),
            "revise_rate_delta": round(float(current_stats.get("revise_rate", 0.0)) - float(previous_stats.get("revise_rate", 0.0)), 3),
            "reject_rate_delta": round(float(current_stats.get("reject_rate", 0.0)) - float(previous_stats.get("reject_rate", 0.0)), 3),
        }
    return delta


def main() -> None:
    parser = argparse.ArgumentParser(description="Run or derive a 20-paper reviewer calibration loop.")
    parser.add_argument("--micro-set", default="calibration/calibration_micro_set.json", help="Path to micro-set spec JSON")
    parser.add_argument("--base-url", default="http://49.12.7.18:8000", help="API base URL for live runs")
    parser.add_argument("--output", default="artifacts/calibration_micro_latest.json", help="Output artifact path")
    parser.add_argument("--from-artifact", default="", help="Existing benchmark artifact to slice instead of running live")
    parser.add_argument("--compare-to", default="", help="Optional prior micro artifact for delta reporting")
    args = parser.parse_args()

    spec = load_micro_set(args.micro_set)
    records: list[dict]
    mode = "artifact"

    if args.from_artifact:
        records = micro_records_from_artifact(spec, args.from_artifact)
    else:
        api_key = os.environ.get("RESEARKA_V2_API_KEY")
        if not api_key:
            raise SystemExit("RESEARKA_V2_API_KEY must be set for live micro-set runs.")
        mode = "live"
        papers = build_micro_papers(spec)
        records = []
        for index, paper in enumerate(papers, start=1):
            record = submit_and_drain(paper, args.base_url.rstrip("/"), api_key, timeout_s=600.0)
            records.append(record)
            print(
                f"[{index}/{len(papers)}] id={record['paper_id']} q={record['quality']} -> "
                f"{record['decision'] or record['outcome']} ({record['duration_s']:.2f}s)"
            )
            time.sleep(1.0)

    aggregates = aggregate(records)
    artifact = {
        "run_meta": {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "mode": mode,
            "micro_set_version": spec.get("version", "micro-set-v1"),
            "micro_set_size": len(records),
            "base_url": args.base_url,
            "source_artifact": args.from_artifact or spec.get("source_artifact", ""),
        },
        "aggregates": aggregates,
        "papers": records,
    }
    if args.compare_to:
        previous = json.loads(Path(args.compare_to).read_text())
        artifact["comparison"] = compare_aggregates(aggregates, previous["aggregates"])

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(artifact, indent=2))

    print(f"Accuracy: {aggregates['correct']} / {aggregates['total']} ({aggregates['accuracy']:.1%})")
    for quality, stats in aggregates.get("by_quality", {}).items():
        print(
            f"{quality}: accuracy={stats['accuracy']:.0%} accept={stats['accept_rate']:.0%} "
            f"revise={stats['revise_rate']:.0%} reject={stats['reject_rate']:.0%}"
        )
    if "comparison" in artifact:
        print(json.dumps(artifact["comparison"], indent=2))
    print(f"Artifact saved to: {output}")


if __name__ == "__main__":
    main()
