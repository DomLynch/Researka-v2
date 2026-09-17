"""Frozen import surface for external producers (Research Agent Bot).

The producer loads ``runtime_core/evidence_quality.py`` by *file path* under
``RESEARKA_RUNTIME_ROOT`` with only ``contracts`` on sys.path, then calls the
symbols below at submit time. Its planned local exam additionally imports the
rubric keys and accept rules. Renaming, relocating, changing a signature, or
adding import-time side effects to any of these is a breaking change for the
producer and must be made deliberately, with this test updated in the same
commit.
"""
from __future__ import annotations

import importlib.util
import inspect
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# (module, symbol, exact positional/keyword parameter names) — None for constants.
FROZEN_SURFACE = (
    ("runtime_core.evidence_quality", "claim_candidates", ("text",)),
    ("runtime_core.evidence_quality", "support_for_claim",
     ("text", "sources", "require_quantitative_agreement", "require_evidence_alignment")),
    ("runtime_core.review_contract", "REVIEW_RUBRIC_KEYS", None),
    ("runtime_core.review_contract", "FINDING_DEFECT_TYPES", None),
    ("runtime_core.review_contract", "accept_contract_satisfied",
     ("rubric_scores", "major_issues", "required_revisions", "claim_support", "overclaim", "synthesis_quality")),
    ("runtime_core.review_contract", "accept_contract_failure",
     ("rubric_scores", "major_issues", "required_revisions", "claim_support", "overclaim", "synthesis_quality")),
)


def test_frozen_symbols_exist_with_stable_signatures() -> None:
    for module_name, symbol, params in FROZEN_SURFACE:
        module = importlib.import_module(module_name)
        assert hasattr(module, symbol), f"{module_name}.{symbol} was renamed or removed"
        obj = getattr(module, symbol)
        if params is None:
            assert obj, f"{symbol} is empty"
            continue
        actual = tuple(inspect.signature(obj).parameters)
        assert actual == params, f"{module_name}.{symbol} signature changed: {actual} != {params}"


def test_rubric_keys_are_the_six_the_producer_scores_against() -> None:
    from runtime_core.review_contract import REVIEW_RUBRIC_KEYS

    assert REVIEW_RUBRIC_KEYS == (
        "research_question_quality", "synthesis_quality", "claim_evidence_alignment",
        "limitations_quality", "gaps_quality", "source_grounding",
    )


def test_evidence_quality_loads_by_file_path_with_only_contracts_on_path() -> None:
    """Mirrors the producer's exact loading strategy — a fresh interpreter,
    the runtime root on sys.path, spec_from_file_location on the file. Any
    new import-time dependency, network call, or env requirement breaks this."""
    script = (
        "import importlib.util, sys; sys.path.insert(0, sys.argv[1]);"
        "spec = importlib.util.spec_from_file_location('x', sys.argv[1] + '/runtime_core/evidence_quality.py');"
        "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m);"
        "print(bool(m.claim_candidates('Risk fell 12% [1].')), bool(m.support_for_claim))"
    )
    result = subprocess.run(
        [sys.executable, "-c", script, str(ROOT)],
        capture_output=True, text=True, timeout=30,
        env={"PATH": "/usr/bin:/bin"},  # no RESEARKA_* env: import must not need it
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "True True"
