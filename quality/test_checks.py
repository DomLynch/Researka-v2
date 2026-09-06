import json
import subprocess

import pytest

from quality.check_complexity import ROOT, RUFF_ARGS, findings, regressions, scope_at
from quality.mock_density import counts

BIN = ROOT / ".venv-quality/bin"


def test_mock_count_ignores_comments_and_strings():
    assert counts("# monkeypatch.setattr(a,b)\ns='assert False'\nassert 1\nmonkeypatch.setattr(a,b)\n") == (1, 1)


def test_baseline_only_allows_existing_or_improved_metrics():
    baseline = {"a:f:C901": 15}
    assert not regressions({"a:f:C901": 14}, baseline)
    assert not regressions({"a:f:C901": 15}, baseline)
    assert regressions({"a:f:C901": 16}, baseline)
    assert regressions({"a:g:C901": 11}, baseline)


def test_scope_survives_line_moves_and_distinguishes_methods():
    source = "class One:\n def check(self):\n  pass\nclass Two:\n def check(self):\n  pass\n"
    assert scope_at(source, 2) == "One.check"
    assert scope_at(source, 5) == "Two.check"
    assert scope_at("\n\n" + source, 4) == "One.check"


def test_scope_handles_conditional_definitions():
    source = "if True:\n class One:\n  def outer(self):\n   if True:\n    def inner():\n     pass\n"
    assert scope_at(source, 5) == "One.outer.inner"


@pytest.mark.parametrize("body,expected", [
    ("".join(f" if x == {i}:\n  return {i}\n" for i in range(12)), {"C901": 13}),
    ("".join(f" if x == {i}:\n  return {i}\n" for i in range(13)), {"C901": 14, "PLR0912": 13}),
    ("".join(f" x += {i}\n" for i in range(51)), {"PLR0915": 51}),
])
def test_real_ruff_reports_complexity(tmp_path, body, expected):
    path = tmp_path / "sample.py"
    path.write_text("def example(x):\n" + body)
    result = subprocess.run([str(BIN / "ruff"), *RUFF_ARGS, str(path)], capture_output=True, text=True, check=False)
    assert result.returncode == 1
    current = findings(tmp_path, json.loads(result.stdout))
    assert current == {f"sample.py:example:{code}": value for code, value in expected.items()}
    assert regressions(current, {})
    assert not regressions(current, current)


def test_noqa_and_external_config_cannot_hide_complexity_or_modify_source(tmp_path):
    path = tmp_path / "sample.py"
    source = "def example(x):  # noqa: C901\n" + "".join(f" if x == {i}:\n  return {i}\n" for i in range(12))
    path.write_text(source)
    (tmp_path / "ruff.toml").write_text('fix = true\n[lint.mccabe]\nmax-complexity = 1000\n')
    result = subprocess.run([str(BIN / "ruff"), *RUFF_ARGS, str(path)], capture_output=True, text=True, check=False)
    assert result.returncode == 1
    assert findings(tmp_path, json.loads(result.stdout)) == {"sample.py:example:C901": 13}
    assert path.read_text() == source


@pytest.mark.parametrize("source,target", [("runtime_core", "apps"), ("contracts", "runtime_core"), ("contracts", "apps")])
def test_import_contract_rejects_reverse_dependencies(tmp_path, source, target):
    for name in ("apps", "runtime_core", "contracts"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "__init__.py").touch()
    command = [str(BIN / "lint-imports"), "--config",
               str(ROOT / "quality/imports.ini"), "--no-cache"]
    clean = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True, check=False)
    assert clean.returncode == 0, clean.stdout + clean.stderr
    (tmp_path / source / "__init__.py").write_text(f"import {target}\n")
    broken = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True, check=False)
    assert broken.returncode == 1, broken.stdout + broken.stderr
    assert "BROKEN" in broken.stdout


def test_duplication_baseline_allows_old_but_blocks_new(tmp_path):
    source = "def example(x):\n" + "".join(f" value_{i} = x + {i}\n" for i in range(30))
    (tmp_path / "a.py").write_text(source)
    command = ["npm", "exec", "--offline", "--yes", "--package=jscpd@5.1.2", "--", "jscpd", ".",
               "--config", str(ROOT / "quality/jscpd.json"), "--pattern", "**/*.py",
               "--baseline", "baseline.json", "--no-tips", "--reporters", "json", "--output", "report"]
    def run(*args):
        return subprocess.run([*command, *args], cwd=tmp_path, capture_output=True, text=True, check=False)
    baseline = run("--update-baseline")
    assert baseline.returncode == 0, baseline.stdout + baseline.stderr
    assert run("--fail-on-new-clones").returncode == 0
    (tmp_path / "b.py").write_text(source)
    assert run("--fail-on-new-clones").returncode == 1
    assert run("--update-baseline").returncode == 0
    assert run("--fail-on-new-clones").returncode == 0
