#!/usr/bin/env bash
# Rewrite files into canonical form (ruff-format + ruff --fix + markdownlint).
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

echo "==> ruff check --fix"
uv run ruff check --fix .

echo "==> ruff format"
uv run ruff format .

if command -v npx >/dev/null 2>&1; then
  echo "==> markdownlint --fix"
  npx --yes markdownlint-cli2 --config .markdownlint-cli2.jsonc --fix "**/*.md" >/dev/null \
    || echo "markdownlint autofix skipped (issues remain or npx unavailable)"
fi

echo "Formatted. Review with: git diff"
