#!/usr/bin/env bash
# Run the offline Library Builder (PBK 15, SAPRS Chapter 6).
#
# The builder is milestone 6 work (AIG 21); this script exists so the documented
# command is already correct and so the placeholder behaviour is explicit.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

if [[ ! -f apps/builder/main.py ]]; then
  cat >&2 <<'NOT_YET'
The Library Builder is not implemented yet.

Bootstrap (PBK) provides the repository, tooling and scaffolding only. Library
building arrives with milestone 6 in AIG Chapter 21, which is also when the
synthetic media generator in tests/support/media.py becomes real.
NOT_YET
  exit 69 # EX_UNAVAILABLE
fi

SOURCE="${1:?usage: scripts/run-builder.sh /path/to/music}"
OUTPUT_DIR="${ENCORE_DATA_DIR:-./build-library}"
exec uv run python -m apps.builder.main --source "$SOURCE" --output "$OUTPUT_DIR"
