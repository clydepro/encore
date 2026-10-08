#!/usr/bin/env bash
# Create/update GitHub labels from .github/labels.yml (PBK Chapter 10).
#
#   scripts/sync-labels.sh [owner/repo]
#
# Requires an authenticated GitHub CLI (`gh auth login`).
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

exec uv run python tools/sync_labels.py "${1:-}"
