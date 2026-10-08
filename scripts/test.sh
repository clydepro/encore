#!/usr/bin/env bash
# Run tests (PBK 15).
#
#   scripts/test.sh              unit + integration + regression with coverage
#   scripts/test.sh unit         one category
#   scripts/test.sh --all        everything including slow suites
#   scripts/test.sh -k pattern   any extra args go straight to pytest
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

if [[ $# -gt 0 && "$1" == "--all" ]]; then
  shift
  exec uv run pytest --run-slow "$@"
fi

if [[ $# -gt 0 && -d "tests/$1" ]]; then
  target="tests/$1"
  shift
  exec uv run pytest "$target" "$@"
fi

exec uv run pytest tests/unit tests/integration tests/regression "$@"
