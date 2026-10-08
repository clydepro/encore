#!/usr/bin/env bash
# One-command GitHub repository setup (PBK Chapter 7, Chapter 10).
#
#   scripts/setup-github-repo.sh [--repo owner/name] [--dry-run]
#   scripts/setup-github-repo.sh --verify
#
# Applies everything that can be applied from the API: the label taxonomy, the
# branch protection rules for `main`, Dependabot alerts, and the code-scanning
# setting our CodeQL workflow depends on. Settings with no endpoint are reported
# as a checklist instead — the script never pretends to have done them.
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
    "$(gh api "repos/$REPO/labels" --paginate --jq '.[].name' | wc -l | tr -d ' ')"
  for required in bug P0 "area:playback" ready-for-review; do
    if gh api "repos/$REPO/labels/$required" >/dev/null 2>&1; then
      status "label ${required}" "present"
    else
      status "label ${required}" "MISSING"
    fi
  done
  if gh api "repos/$REPO/branches/main/protection" >/dev/null 2>&1; then
    status "branch protection on main" \
      "$(gh api "repos/$REPO/branches/main/protection" --jq '[.required_status_checks.contexts[]] | length') required checks"
  else
    status "branch protection on main" "MISSING (run: scripts/configure-branch-protection.sh)"
  fi
  if gh api "repos/$REPO/vulnerability-alerts" >/dev/null 2>&1; then
    status "Dependabot alerts" "enabled"
  else
    status "Dependabot alerts" "disabled"
  fi
  case "$(gh api "repos/$REPO/code-scanning/default-setup" --jq .state 2>/dev/null)" in
    not-configured) status "CodeQL default setup" "off (security.yml owns CodeQL)" ;;
    configured) status "CodeQL default setup" "ON - conflicts with security.yml" ;;
    *) status "CodeQL default setup" "unknown (no read permission?)" ;;
  esac
  status "workflows visible" \
    "$(gh api "repos/$REPO/actions/workflows" --jq '[.workflows[] | select(.path|startswith(".github/workflows/"))] | length') of 5 Encore workflows"
  echo
  echo "UI-only settings cannot be verified reliably via the API; check"
  echo "Settings → Security → Code security and analysis for code scanning,"
  echo "secret scanning and push protection."
  exit 0
fi

echo "==> 1/4 Labels (.github/labels.yml)"
if [[ "$DRY_RUN" == "1" ]]; then
  uv run python tools/sync_labels.py "$REPO" --dry-run
else
  uv run python tools/sync_labels.py "$REPO"
fi

echo "==> 2/4 Branch protection for main"
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

echo "==> 3/4 Dependabot alerts (also enables the dependency graph)"
if [[ "$DRY_RUN" == "0" ]]; then
  if gh api "repos/$REPO/vulnerability-alerts" >/dev/null 2>&1; then
    echo "  already enabled"
  elif gh api -X POST "repos/$REPO/vulnerability-alerts" >/dev/null 2>&1; then
    echo "  enabled"
  else
    echo "  not changed (needs Settings → Security → Dependabot)"
  fi
fi

echo "==> 4/4 Code scanning: CodeQL default setup must be off"
# GitHub rejects SARIF from advanced-configuration workflows while default setup
# is on, which would leave Security / codeql permanently red.
if [[ "$DRY_RUN" == "0" ]]; then
  case "$(gh api "repos/$REPO/code-scanning/default-setup" --jq .state 2>/dev/null)" in
    configured)
      gh api -X PATCH "repos/$REPO/code-scanning/default-setup" -f state=not-configured \
        >/dev/null && echo "  default setup disabled; security.yml now owns CodeQL"
      ;;
    not-configured) echo "  already off" ;;
    *) echo "  could not read state (Settings → Security → Code security and analysis)" ;;
  esac
fi

echo
echo "Not scriptable here — do them in the web UI (Settings):"
cat <<'CHECKLIST'
  [ ] Security → Code security and analysis
        - Code scanning alerts ........... must be available for SARIF uploads;
                                           the CodeQL *default setup* must stay off
                                           (handled above)
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
