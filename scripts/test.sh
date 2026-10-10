#!/usr/bin/env bash
# Run tests (PBK 15).
#
#   scripts/test.sh              unit + integration + regression + e2e, with coverage
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

# `e2e` is in the default set because it is three seconds of the only tests that drive the
# product the way a guest does; the suites this command leaves out are the ones that are
# slow or meaningless under a tracer (`--all`, or the scheduled run).
exec uv run pytest tests/unit tests/integration tests/regression tests/e2e "$@"
