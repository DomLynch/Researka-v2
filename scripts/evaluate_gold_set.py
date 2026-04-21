#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from runtime_core.goldset import evaluate_gold_set, load_gold_set


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate a human-labeled gold set against the current reviewer.")
    parser.add_argument("input", help="Path to gold set JSON")
    parser.add_argument("--output", default="artifacts/gold_set_eval.json", help="Output artifact path")
    parser.add_argument("--progress-log", default="", help="Optional JSONL progress log path")
    parser.add_argument("--partial-output", default="", help="Optional path for partial artifact flushes")
    args = parser.parse_args()

    corpus = load_gold_set(args.input)
    progress_log = args.progress_log.strip()
    partial_output = args.partial_output.strip()

    def _progress(index: int, total: int, record: dict, artifact: dict) -> None:
        line = {
            "index": index,
            "total": total,
            "entry_id": record.get("entry_id"),
            "article_type": record.get("article_type"),
            "expected_decision": record.get("expected_decision"),
            "actual_decision": record.get("actual_decision"),
            "stage_reached": record.get("stage_reached"),
            "error": record.get("error"),
            "accuracy_so_far": artifact["summary"]["accuracy"],
        }
        print(
            f"[{index}/{total}] {record.get('entry_id')} -> {record.get('actual_decision') or 'pending'} "
            f"stage={record.get('stage_reached')} accuracy={artifact['summary']['accuracy']:.1%}"
        )
        if progress_log:
            os.makedirs(os.path.dirname(progress_log), exist_ok=True)
            with open(progress_log, "a") as handle:
                handle.write(json.dumps(line) + "\n")
        if partial_output:
            os.makedirs(os.path.dirname(partial_output), exist_ok=True)
            with open(partial_output, "w") as handle:
                json.dump(artifact, handle, indent=2)

    artifact = evaluate_gold_set(corpus, progress_callback=_progress if (progress_log or partial_output) else None)

    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w") as handle:
        json.dump(artifact, handle, indent=2)

    summary = artifact["summary"]
    print(f"Gold set:       {args.input}")
    print(f"Entries:        {summary['total']}")
    print(f"Accuracy:       {summary['correct']} / {summary['total']} ({summary['accuracy']:.1%})")
    print(f"Mismatches:     {summary['mismatch_count']}")
    print("By article type:")
    for article_type, stats in summary.get("by_article_type", {}).items():
        print(f"  {article_type:24s} n={stats['count']:3d} accuracy={stats['accuracy']:.0%}")
    print("Top accept blockers:")
    for blocker, count in list(summary.get("accept_blockers", {}).items())[:5]:
        print(f"  {blocker}: {count}")
    print(f"Artifact saved: {args.output}")


if __name__ == "__main__":
    main()
