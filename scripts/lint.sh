#!/usr/bin/env bash
# Quality checks only: lint, formatting verification, type checking (PBK 4/15).
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

echo "==> ruff check"
uv run ruff check .

echo "==> ruff format --check"
uv run ruff format --check .

echo "==> yamllint"
uv run yamllint --strict -c .yamllint.yml .

echo "==> mypy (strict)"
uv run mypy

echo
echo "Quality checks passed. Run scripts/test.sh for the test suites."
