#!/usr/bin/env bash
# One-command GitHub repository setup (PBK Chapter 7, Chapter 10).
#
#   scripts/setup-github-repo.sh [--repo owner/name] [--dry-run]
#   scripts/setup-github-repo.sh --verify
#
# Applies everything the API allows: the label taxonomy, branch protection,
# Dependabot alerts, the code-scanning arrangement our workflows assume, private
# vulnerability reporting and the merge/branch options. What has no endpoint is
# printed as a checklist — the script never pretends to have done it.
#
# `gh` must be authenticated with admin access to the repository. Note that this
# environment's gh (2.23) has no `--repo` flag for `gh api`, so API calls spell
# out full `repos/OWNER/REPO/...` paths; `gh label` does take `--repo`.
set -euo pipefail

REPO="${GITHUB_REPOSITORY:-}"
DRY_RUN=0
VERIFY_ONLY=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo)
      REPO="$2"
      shift 2
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --verify)
      VERIFY_ONLY=1
      shift
      ;;
    -h | --help)
      sed -n '2,13p' "${BASH_SOURCE[0]}"
      exit 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

command -v gh >/dev/null || {
  echo "gh is required: https://cli.github.com/ (then 'gh auth login')" >&2
  exit 1
}
if ! gh auth status >/dev/null 2>&1; then
  echo "gh is not authenticated. Run: gh auth login --scope repo" >&2
  exit 1
fi
if [[ -z "$REPO" ]]; then
  REPO=$(gh repo view --json nameWithOwner --jq .nameWithOwner 2>/dev/null || true)
fi
[[ -n "$REPO" ]] || {
  echo "cannot determine the repository; pass --repo owner/name" >&2
  exit 1
}
REPO="${REPO#https://github.com/}"
REPO="${REPO%.git}"

status() { printf '  %-34s %s\n' "$1" "$2"; }

# Ask the API; failures are reported as "unknown" rather than aborting the run.
probe() { gh api "$@" 2>/dev/null || true; }

if [[ "$VERIFY_ONLY" == "1" ]]; then
  echo "==> Verifying $REPO"
  status "labels" "$(python3 tools/sync_labels.py "$REPO" --check || true)"
  status "branch protection on main" \
    "$(probe "repos/$REPO/branches/main/protection" | python3 -c "
import json,sys
try: d=json.load(sys.stdin)
except Exception: print('not set'); sys.exit()
print(f\"{len(d['required_status_checks']['contexts'])} required checks, \"
      f\"{d['required_pull_request_reviews']['required_approving_review_count']} approvals, \"
      f\"code owners={d['required_pull_request_reviews']['require_code_owner_reviews']}\")
" 2>/dev/null || echo "?")"
  if gh api "repos/$REPO/vulnerability-alerts" >/dev/null 2>&1; then
    status "Dependabot alerts" "enabled"
  else
    status "Dependabot alerts" "disabled"
  fi
  case "$(probe "repos/$REPO/code-scanning/default-setup" --jq .state)" in
    not-configured) status "CodeQL default setup" "off (security.yml owns CodeQL)" ;;
    configured) status "CodeQL default setup" "ON - conflicts with security.yml" ;;
    *) status "CodeQL default setup" "unknown (needs admin read)" ;;
  esac
  status "private vulnerability reporting" \
    "$(case "$(probe "repos/$REPO/private-vulnerability-reporting" --jq .enabled)" in
        true) echo enabled ;;
        false) echo disabled ;;
        *) echo unknown ;;
      esac)"
  merge_options=$(probe "repos/$REPO" --jq '
    [.allow_squash_merge, .allow_rebase_merge, .allow_merge_commit, .delete_branch_on_merge]
    | "squash=\(.[0]) rebase=\(.[1]) merge-commit=\(.[2]) auto-delete-branch=\(.[3])"')
  status "merge options" "${merge_options:-unknown}"
  if [[ "$(probe "repos/$REPO/secret-scanning/alerts" --jq 'type')" == "array" ]]; then
    status "secret scanning" "alerts endpoint reachable (verify push protection in Settings)"
  else
    status "secret scanning" "unknown (check Settings)"
  fi
  status "workflows visible" \
    "$(probe "repos/$REPO/actions/workflows" --jq '[.workflows[] | select(.path|startswith(".github/workflows/"))] | length') of 5 Encore workflows"
  exit 0
fi

echo "==> 1/6 Labels (.github/labels.yml)"
if [[ "$DRY_RUN" == "0" ]]; then
  python3 tools/sync_labels.py "$REPO"
else
  python3 tools/sync_labels.py "$REPO" --dry-run
fi

echo "==> 2/6 Branch protection for main"
if [[ "$DRY_RUN" == "0" ]]; then
  REPO="$REPO" bash scripts/configure-branch-protection.sh
else
  echo "  would apply .github/branch_protection.json"
fi

echo "==> 3/6 Dependabot alerts (also enables the dependency graph)"
if [[ "$DRY_RUN" == "0" ]]; then
  if gh api "repos/$REPO/vulnerability-alerts" >/dev/null 2>&1; then
    echo "  already enabled"
  elif gh api -X POST "repos/$REPO/vulnerability-alerts" >/dev/null 2>&1; then
    echo "  enabled"
  else
    echo "  not changed (needs Settings → Security → Dependabot)"
  fi
fi

echo "==> 4/6 Code scanning: default setup off, our workflow owns CodeQL"
# GitHub rejects SARIF from advanced-configuration workflows while the default
# setup is on, which would leave Security / codeql permanently red.
if [[ "$DRY_RUN" == "0" ]]; then
  case "$(probe "repos/$REPO/code-scanning/default-setup" --jq .state)" in
    configured)
      if gh api -X PATCH "repos/$REPO/code-scanning/default-setup" -f state=not-configured \
        >/dev/null 2>&1; then
        echo "  default setup disabled; security.yml now owns CodeQL"
      else
        echo "  could not disable it (Settings → Security → Code security and analysis)"
      fi
      ;;
    not-configured) echo "  already off" ;;
    *) echo "  state unreadable; check Settings → Security → Code security and analysis" ;;
  esac
fi

echo "==> 5/6 Private vulnerability reporting (SECURITY.md links to it)"
if [[ "$DRY_RUN" == "0" ]]; then
  case "$(probe "repos/$REPO/private-vulnerability-reporting" --jq .enabled)" in
    true) echo "  already enabled" ;;
    false)
      if gh api -X PUT "repos/$REPO/private-vulnerability-reporting" -f enabled=true \
        >/dev/null 2>&1; then
        echo "  enabled"
      else
        echo "  not changed (Settings → Security → Code security and analysis)"
      fi
      ;;
    *) echo "  state unreadable; enable it in Settings" ;;
  esac
fi

echo "==> 6/6 Merge and branch options"
# `required_linear_history` already blocks merge commits on main; matching the
# repository options keeps the UI honest about what is offered.
if [[ "$DRY_RUN" == "0" ]]; then
  gh api -X PATCH "repos/$REPO" \
    -f allow_merge_commit=false \
    -f allow_squash_merge=true \
    -f allow_rebase_merge=true \
    -f delete_branch_on_merge=true \
    --jq '"  squash=\(.allow_squash_merge) rebase=\(.allow_rebase_merge) merge_commit=\(.allow_merge_commit) auto_delete=\(.delete_branch_on_merge)"' \
    || echo "  not changed (needs Settings → General)"
fi

cat <<CHECKLIST

Setup applied for $REPO. Verify with: scripts/setup-github-repo.sh --verify

Still only in the web UI (Settings) — no endpoint for these on this plan:
  [ ] Security → Code security and analysis
        - Secret scanning + push protection. Alerts are already on for public
          repositories; push protection has to be ticked here.
  [ ] Settings → Environments → create "pypi" with required reviewers, if and
      when this project publishes packages (release.yml expects that name).
  [ ] General → Pull Requests → allow merge queue (optional; PBK 7).

Deliberately NOT enabled while one person maintains this repository:
  [ ] required_approving_review_count / require_code_owner_reviews /
      require_last_push_approval — nobody can approve their own pull request, so
      turning them on now makes every pull request unmergeable. See
      docs/Developer/Repository-Administration.md for what to flip later.
CHECKLIST
