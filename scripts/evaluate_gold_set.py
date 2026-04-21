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
    args = parser.parse_args()

    corpus = load_gold_set(args.input)
    artifact = evaluate_gold_set(corpus)

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
