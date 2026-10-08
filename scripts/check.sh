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
  echo "==> Everything, including slow suites"
  exec uv run pytest --run-slow --cov=encore --cov-report=term-missing
fi

echo "==> Tests (unit + integration) with coverage"
uv run pytest tests/unit tests/integration --cov=encore --cov-report=term-missing

echo
echo "All local gates passed."
echo "Long-running suites: scripts/check.sh --slow"
