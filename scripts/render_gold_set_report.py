#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from runtime_core.goldset import render_gold_set_report


def main() -> None:
    parser = argparse.ArgumentParser(description="Render a markdown report from a gold-set eval artifact.")
    parser.add_argument("input", help="Path to eval artifact JSON")
    parser.add_argument("--output", default="artifacts/gold_set_eval.md", help="Output markdown path")
    args = parser.parse_args()

    with open(args.input) as handle:
        artifact = json.load(handle)

    report = render_gold_set_report(artifact)
    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w") as handle:
        handle.write(report)

    print(args.output)


if __name__ == "__main__":
    main()
