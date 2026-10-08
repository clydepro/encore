#!/usr/bin/env bash
# Apply branch protection for `main` (PBK Chapter 7).
#
#   scripts/configure-branch-protection.sh [owner/repo] [branch]
#
# Declarative source of truth: .github/branch_protection.json.
# Requires an authenticated GitHub CLI with admin rights on the repository.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

REPO="${1:-$(git config --get remote.origin.url | sed -E 's#.*github\.com[:/]##; s#\.git$##')}"
BRANCH="${2:-main}"

if ! command -v gh >/dev/null 2>&1; then
  echo "gh (GitHub CLI) is required: https://cli.github.com" >&2
  exit 1
fi

echo "==> Applying protection to ${REPO}@${BRANCH}"
gh api -X PUT "repos/${REPO}/branches/${BRANCH}/protection" \
  --input .github/branch_protection.json \
  --jq '{required_status_checks: .required_status_checks.contexts,
         reviews: .required_pull_request_reviews.required_approving_review_count,
         linear_history: .required_linear_history.enabled}'

echo
echo "For the rest of the first-time setup (labels, Dependabot alerts, the"
echo "settings checklist): scripts/setup-github-repo.sh"
echo "Settings-only items are listed in docs/Developer/Repository-Administration.md"
