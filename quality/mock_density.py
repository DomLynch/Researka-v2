"""Triage test files by mock-call density; never a test-quality verdict."""

import ast
from pathlib import Path


def counts(source):
    nodes = list(ast.walk(ast.parse(source)))
    mocks = sum(isinstance(node, ast.Call) and ast.unparse(node.func) in {
        "monkeypatch.setattr", "mocker.patch", "mocker.patch.object", "patch", "patch.object",
        "mock.patch", "mock.patch.object", "unittest.mock.patch",
    } for node in nodes)
    return mocks, sum(isinstance(node, ast.Assert) for node in nodes)


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    rows = [(counts(path.read_text()), str(path.relative_to(root)))
            for path in (root / "tests").rglob("test_*.py")]
    print("mock_calls / assert_statements / file (triage only; aliases and indirect assertions are not counted)")
    for (mocks, assertions), path in sorted(rows, key=lambda row: row[0][0] / max(1, row[0][1]), reverse=True)[:10]:
        print(f"{mocks:4} / {assertions:4} / {path}")
