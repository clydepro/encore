#!/usr/bin/env bash
# Run the offline Library Builder (PBK 15, SAPRS Chapter 6, ADR-010).
#
# A convenience wrapper over `encore-builder`, which is the documented command.
# The one thing it adds is that a positional path here is a music directory: the
# Builder's own flag is `--music-dir`, and its positional-free interface is
# deliberate, because guessing which of four paths a bare argument means is not
# something a build that rewrites `library.db` should do.
set -euo pipefail

# Resolve arguments before changing directory. The Builder insists on absolute
# paths (SAPRS 6.10 validates them, and systemd gives the service no working
# directory), and a wrapper that `cd`s first would turn `run-builder.sh music`
# into a build of a directory that does not exist.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INVOKED_FROM="$PWD"

abspath() {
  case "$1" in
    /*) printf '%s\n' "$1" ;;
    *) printf '%s/%s\n' "$INVOKED_FROM" "$1" ;;
  esac
}

cd "$REPO_ROOT"

if [[ $# -eq 0 ]]; then
  echo "usage: scripts/run-builder.sh /path/to/music [extra encore-builder flags...]" >&2
  echo "       (with no argument, uses paths.music_dir from the config)" >&2
  echo "       (see encore-builder --help for the flags)" >&2
fi

if [[ ! -f apps/builder/main.py ]]; then
  cat >&2 <<'NOT_YET'
The Library Builder is not implemented yet.

Bootstrap (PBK) provides the repository, tooling and scaffolding only. Library
building arrives with milestone 6 in AIG Chapter 21, which is also when the
synthetic media generator in tests/support/media.py becomes real.
NOT_YET
  exit 69 # EX_UNAVAILABLE
fi

if [[ $# -eq 0 ]]; then
  exec uv run encore-builder
fi

MUSIC_DIR="$(abspath "$1")"
shift
exec uv run encore-builder --music-dir "$MUSIC_DIR" "$@"
