"""Reject new/worsened Ruff complexity debt without line-number baselines."""

import argparse
import ast
import json
from pathlib import Path
import re
import subprocess
import sys

RULES = {"C901", "PLR0912", "PLR0915"}
RUFF_ARGS = ["check", "--isolated", "--no-fix", "--ignore-noqa",
             "--select", ",".join(sorted(RULES)), "--output-format", "json"]
ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "quality/complexity-baseline.json"


def scope_at(source, row):
    scopes = [node for node in ast.walk(ast.parse(source))
              if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
              and node.lineno <= row <= node.end_lineno]
    return ".".join(node.name for node in sorted(scopes, key=lambda node: node.lineno))


def findings(root, rows):
    root = root.resolve()
    result = {}
    for item in rows:
        path = Path(item["filename"]).resolve()
        scope = scope_at(path.read_text(), item["location"]["row"])
        metric = re.search(r"\((\d+) > \d+\)", item["message"])
        if item["code"] not in RULES or not scope or metric is None:
            raise ValueError(f"Unexpected Ruff diagnostic: {item}")
        key = f"{path.relative_to(root).as_posix()}:{scope}:{item['code']}"
        result[key] = max(result.get(key, 0), int(metric[1]))
    return result


def regressions(current, baseline):
    return {key: value for key, value in current.items() if value > baseline.get(key, 0)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", help="Production source directories")
    parser.add_argument("--update-baseline", action="store_true")
    args = parser.parse_args()
    run = subprocess.run(
        [sys.executable, "-m", "ruff", *RUFF_ARGS, *args.paths],
        cwd=ROOT, capture_output=True, text=True, timeout=60,
    )
    if run.returncode not in (0, 1):
        raise RuntimeError(run.stderr or run.stdout)
    current = findings(ROOT, json.loads(run.stdout))
    if args.update_baseline:
        BASELINE.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n")
        print(f"Recorded {len(current)} complexity findings; review baseline changes before commit.")
        return 0
    new = regressions(current, json.loads(BASELINE.read_text()))
    for key, value in sorted(new.items()):
        print(f"REGRESSION {key}: {value}")
    print(f"Complexity: {len(current)} existing findings; {len(new)} new/worsened.")
    return int(bool(new))


if __name__ == "__main__":
    raise SystemExit(main())
