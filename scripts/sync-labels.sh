#!/usr/bin/env bash
# Thin wrapper around tools/sync_labels.py (PBK Chapter 10).
#
#   scripts/sync-labels.sh                 # apply .github/labels.yml
#   scripts/sync-labels.sh --check         # report drift, write nothing
#   scripts/sync-labels.sh --prune         # also delete undeclared labels
#   scripts/sync-labels.sh --dry-run       # preview
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
exec uv run python tools/sync_labels.py "$@"
