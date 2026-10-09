#!/usr/bin/env bash
# The full local gate: everything CI will run (PBK 4, AEP 26).
#
#   scripts/check.sh           standard gate (pre-commit, lint, types, tests)
#   scripts/check.sh --slow    also run performance + Party Simulation suites
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

SLOW=0
if [[ "${1:-}" == "--slow" ]]; then
  SLOW=1
elif [[ $# -gt 0 ]]; then
  echo "usage: $0 [--slow]" >&2
  exit 2
fi

echo "==> Lockfile consistency"
uv lock --check

echo "==> pre-commit (formatting, lint, hygiene, YAML/TOML/Markdown, secrets)"
uv run pre-commit run --all-files

echo "==> Quality checks"
scripts/lint.sh

echo "==> Documentation link check"
uv run python tools/check_links.py

if [[ "$SLOW" == "1" ]]; then
  # The budgets in tests/performance are wall-clock, and coverage tracing multiplies
  # SQLite-heavy work by several times. So the slow suites run untraced, and coverage comes
  # from the same command the standard gate uses. One command that did both would either
  # report a false failure or silently skip the numbers it exists to check.
  echo "==> Coverage (unit + integration + regression)"
  uv run pytest tests/unit tests/integration tests/regression \
    --cov=encore --cov-report=term-missing

  echo "==> Everything, including slow suites (untraced, so the budgets mean something)"
  exec uv run pytest --run-slow --no-cov
fi

echo "==> Tests (unit + integration + regression) with coverage"
uv run pytest tests/unit tests/integration tests/regression \
  --cov=encore --cov-report=term-missing

echo
echo "All local gates passed."
echo "Long-running suites: scripts/check.sh --slow"
