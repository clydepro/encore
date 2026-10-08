#!/usr/bin/env bash
# Create/refresh the development environment (PBK Chapter 15).
#
#   scripts/bootstrap.sh
#
# Idempotent: safe to re-run after pulling changes to pyproject.toml or uv.lock.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

if ! command -v uv >/dev/null 2>&1; then
  cat >&2 <<'MISSING'
uv is required but not installed (AIG 3).

  curl -LsSf https://astral.sh/uv/install.sh | sh

Then re-run: scripts/bootstrap.sh
MISSING
  exit 1
fi

echo "==> Python pinned to: $(cat .python-version)"
uv python install "$(cat .python-version)" >/dev/null 2>&1 || true

echo "==> Installing dependencies from uv.lock (editable project install)"
uv sync --frozen --all-groups

echo "==> Installing git hooks"
uv run pre-commit install

echo
echo "Done. Next:"
echo "  scripts/check.sh    # everything CI will run"
echo "  scripts/test.sh     # fast test loop"
