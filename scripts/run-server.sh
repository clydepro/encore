#!/usr/bin/env bash
# Start the Encore jukebox server (PBK 15).
#
# The server is milestone 11-13 work (AIG 21); during the bootstrap phase this
# script exists so that the documented command is already correct.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

if [[ ! -f apps/server/main.py ]]; then
  cat >&2 <<'NOT_YET'
The Encore server is not implemented yet.

Bootstrap (PBK) provides the repository, tooling and scaffolding only. The
runnable appliance arrives with milestones 11-13 (FastAPI, HTMX, SSE) in
AIG Chapter 21.

Track progress: https://github.com/clydepro/encore/issues
NOT_YET
  exit 69 # EX_UNAVAILABLE
fi

CONFIG="${ENCORE_CONFIG:-/etc/encore/config.yaml}"
exec uv run python -m apps.server.main --config "$CONFIG"
