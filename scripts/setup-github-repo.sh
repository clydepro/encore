#!/usr/bin/env bash
# One-command GitHub repository setup (PBK Chapter 7, Chapter 10).
#
#   scripts/setup-github-repo.sh [--repo owner/name] [--dry-run]
#   scripts/setup-github-repo.sh --verify
#
# Applies everything that can be applied from the API: the label taxonomy, the
# branch protection rules for `main`, and Dependabot alerts. Settings that have
# no stable endpoint for this plan are reported as a checklist instead — the
# script never pretends to have done them.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

REPO="${GITHUB_REPO:-}"
DRY_RUN=0
VERIFY_ONLY=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) REPO="${2:?--repo needs owner/name}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --verify) VERIFY_ONLY=1; shift ;;
    -h|--help) sed -n '1,12p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

if ! command -v gh >/dev/null 2>&1; then
  echo "gh (GitHub CLI) is required: https://cli.github.com" >&2
  exit 1
fi

if ! gh auth status --hostname github.com >/dev/null 2>&1; then
  cat >&2 <<'AUTH'
Not authenticated with github.com. Run:

  gh auth login            # browser flow, or
  gh auth login --scopes repo,read:org --web

A token with the `repo` scope is enough for labels, branch protection and
Dependabot alerts. Then re-run this script.
AUTH
  exit 1
fi

if [[ -z "$REPO" ]]; then
  REPO="$(git remote get-url origin | sed -E 's#.*github\.com[:/]##; s#\.git$##')"
fi
echo "Repository: ${REPO}  (as $(gh api user --jq .login))"

status() { printf '  %-34s %s\n' "$1" "$2"; }

if [[ "$VERIFY_ONLY" == "1" ]]; then
  echo "==> Current state"
  status "labels present in repository" \
    "$(gh api labels --paginate --jq '.[].name' --repo "$REPO" | wc -l | tr -d ' ')"
  for required in bug P0 "area:playback" ready-for-review; do
    if gh api "labels/$required" --repo "$REPO" >/dev/null 2>&1; then
      status "label ${required}" "present"
    else
      status "label ${required}" "MISSING"
    fi
  done
  if gh api "branches/main/protection" --repo "$REPO" >/dev/null 2>&1; then
    status "branch protection on main" \
      "$(gh api "branches/main/protection" --jq '[.required_status_checks.contexts[]] | length' --repo "$REPO") required checks"
  else
    status "branch protection on main" "MISSING (run: scripts/configure-branch-protection.sh)"
  fi
  if gh api "vulnerability-alerts" --repo "$REPO" >/dev/null 2>&1; then
    status "Dependabot alerts" "enabled"
  else
    status "Dependabot alerts" "disabled"
  fi
  status "workflows visible" \
    "$(gh api "actions/workflows" --jq '.total_count' --repo "$REPO") (5 expected after push)"
  echo
  echo "UI-only settings cannot be verified reliably via the API; check"
  echo "Settings → Security → Code security and analysis for code scanning,"
  echo "secret scanning and push protection."
  exit 0
fi

echo "==> 1/3 Labels (.github/labels.yml)"
if [[ "$DRY_RUN" == "1" ]]; then
  uv run python tools/sync_labels.py "$REPO" --dry-run
else
  uv run python tools/sync_labels.py "$REPO"
fi

echo "==> 2/3 Branch protection for main"
if [[ "$DRY_RUN" == "1" ]]; then
  uv run python -c '
import json, pathlib
rules = json.loads(pathlib.Path(".github/branch_protection.json").read_text())
print("  would require", len(rules["required_status_checks"]["contexts"]), "checks:",
      ", ".join(rules["required_status_checks"]["contexts"]))
'
else
  scripts/configure-branch-protection.sh "$REPO"
fi

echo "==> 3/3 Dependabot alerts (also enables the dependency graph)"
if [[ "$DRY_RUN" == "0" ]]; then
  if gh api -X POST "vulnerability-alerts" --repo "$REPO" >/dev/null 2>&1; then
    echo "  enabled"
  else
    echo "  not changed (already enabled, or needs Settings → Security → Dependabot)"
  fi
fi

echo
echo "Not scriptable here — do them in the web UI (Settings):"
cat <<'CHECKLIST'
  [ ] Security → Code security and analysis
        - Code scanning (CodeQL) ......... required before Security / codeql can
                                           upload SARIF; PRs otherwise stall
        - Secret scanning + push protection
  [ ] Security → Dependabot
        - Dependabot version updates ..... .github/dependabot.yml is committed;
                                           enabling alerts above is the gate
  [ ] General → Pull Requests
        - Automatically delete head branches
        - Merge queue (optional; PBK 7)
        - Keep "Allow merge commits" off, or required_linear_history blocks merges
  [ ] Security → Private vulnerability reporting (SECURITY.md links to it)
  [ ] Settings → Environments → create "pypi" with required reviewers, if publishing
CHECKLIST

if ! GIT_TERMINAL_PROMPT=0 git ls-remote --heads origin main 2>/dev/null |
  grep -q "$(git rev-parse main 2>/dev/null || echo '')"; then
  echo "Note: local main is not yet on the remote. Push with: git push -u origin main"
  echo "      Branch protection and the workflows only take effect after that."
fi

echo
echo "Verify any time with: scripts/setup-github-repo.sh --verify"
