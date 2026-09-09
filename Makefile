QUALITY_BIN := .venv-quality/bin
JSCPD := npm exec --yes --package=jscpd@5.1.2 -- jscpd

.PHONY: quality-setup quality complexity architecture duplicates quality-test dead-code mock-audit contract-test
quality-setup:
	uv venv --allow-existing .venv-quality
	uv pip install --python $(QUALITY_BIN)/python -r requirements-quality.txt
	$(JSCPD) --version

quality: complexity architecture duplicates quality-test dead-code mock-audit
	$(QUALITY_BIN)/ruff check --config pyproject.toml --no-fix --no-fix-only apps runtime_core contracts quality

complexity:
	$(QUALITY_BIN)/python quality/check_complexity.py apps runtime_core contracts

architecture:
	$(QUALITY_BIN)/lint-imports --config quality/imports.ini --no-cache

duplicates:
	$(JSCPD) . --config quality/jscpd.json --baseline quality/duplication-baseline.json --fail-on-new-clones --no-tips --reporters json --output .quality-reports
	$(QUALITY_BIN)/python -c 'import json; s=json.load(open(".quality-reports/jscpd-report.json"))["statistics"]["total"]; print(s); assert s["sources"] > 0, "Empty duplication scan"'

quality-test:
	$(QUALITY_BIN)/python -m pytest -q -o addopts= quality/test_checks.py

# Report-only: Vulture exit 3 means candidates, not a proven runtime defect.
dead-code:
	$(QUALITY_BIN)/vulture apps runtime_core contracts --min-confidence 100; code=$$?; test $$code -eq 0 -o $$code -eq 3

mock-audit:
	$(QUALITY_BIN)/python quality/mock_density.py

# Override UV_PROJECT_ENVIRONMENT when another developer owns the local .venv.
contract-test:
	uv run --locked --extra dev python -m pytest -q tests/test_publication_contract.py tests/test_submission_schema.py
