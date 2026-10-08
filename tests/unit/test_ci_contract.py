"""CI contract: every required check must be a check GitHub will actually see.

Branch protection lists contexts by name, and GitHub blocks a merge until each
one reports. A context that no job produces — a renamed job, a typo, a job that
only runs in a context where it is skipped — therefore stalls the pull request
forever. That is not hypothetical here: `CodeQL` and `Dependabot` were listed as
required checks even though no such job exists in this repository, and both
matrix jobs and hand-written names make the drift easy to miss.

These tests read `.github/branch_protection.json` and the workflow files, and
refuse to pass when the two disagree.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIRECTORY = PROJECT_ROOT / ".github" / "workflows"
BRANCH_PROTECTION_FILE = PROJECT_ROOT / ".github" / "branch_protection.json"

#: Workflows that gate a change. `release.yml` only publishes artefacts, so it
#: contributes no required checks.
GATING_WORKFLOWS = ("validate.yml", "test.yml", "docs.yml", "security.yml")


def _load_protection() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(BRANCH_PROTECTION_FILE.read_text(encoding="utf-8"))
    return loaded


def _required_contexts() -> list[str]:
    return list(_load_protection()["required_status_checks"]["contexts"])


def _manifest(path: Path) -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return loaded


def _matrix_combinations(job: dict[str, Any]) -> list[dict[str, str]]:
    """Expand the matrix forms used in this repository.

    Covers `matrix: {key: [values]}` and `matrix: {include: [{...}]}`; a job
    without a matrix yields a single empty combination.
    """
    matrix = (job.get("strategy") or {}).get("matrix") or {}
    combinations: list[dict[str, str]] = []
    for key, values in matrix.items():
        if key == "include" or not isinstance(values, list):
            continue
        combinations.extend({key: str(value)} for value in values)
    combinations.extend(
        {str(key): str(value) for key, value in entry.items()}
        for entry in matrix.get("include", [])
        if isinstance(entry, dict)
    )
    return combinations or [{}]


def _expand(template: str, combination: dict[str, str]) -> str:
    name = template
    for key, value in combination.items():
        name = name.replace(f"${{{{ matrix.{key} }}}}", value)
    return name


def actual_check_names() -> set[str]:
    return {name for names in _checks_by_file().values() for name in names}


def _checks_by_file() -> dict[str, list[str]]:
    """Map each workflow file to the check names GitHub will see for it."""
    produced: dict[str, list[str]] = {}
    for path in sorted(WORKFLOW_DIRECTORY.glob("*.yml")):
        manifest = _manifest(path)
        workflow_name = str(manifest.get("name", path.stem))
        names: list[str] = []
        for job_id, job in (manifest.get("jobs") or {}).items():
            template = str(job.get("name") or f"{workflow_name} / {job_id}")
            names.extend(_expand(template, combo) for combo in _matrix_combinations(job))
        produced[path.name] = names
    return produced


def test_branch_protection_file_has_the_expected_shape() -> None:
    rules = _load_protection()
    assert rules["required_status_checks"]["strict"] is True
    assert rules["enforce_admins"] is True
    assert "required_approving_review_count" in rules["required_pull_request_reviews"]
    assert rules["required_linear_history"] is True
    assert rules["allow_force_pushes"] is False
    assert rules["allow_deletions"] is False
    assert rules["required_conversation_resolution"] is True


def test_review_rules_do_not_block_a_single_maintainer() -> None:
    """A pull request nobody can approve is a broken repository, not a strict one.

    GitHub refuses self-approval, and an admin merge skips status checks but never
    review requirements — so `required_approving_review_count: 1` (or
    `require_last_push_approval: true`) with one maintainer makes every pull
    request permanently unmergeable. The review gates stay off until a second
    person has write access; `enforce_admins` keeps the fourteen status checks
    binding for everyone meanwhile. Raise these to 1/true/true, and update this
    test, when that changes: docs/Developer/Repository-Administration.md.
    """
    reviews = _load_protection()["required_pull_request_reviews"]
    assert reviews["required_approving_review_count"] == 0
    assert reviews["require_code_owner_reviews"] is False
    assert reviews["require_last_push_approval"] is False


def test_required_checks_are_unique() -> None:
    contexts = _required_contexts()
    duplicates = sorted({c for c in contexts if contexts.count(c) > 1})
    assert not duplicates, f"listed twice in {BRANCH_PROTECTION_FILE.name}: {duplicates}"


def test_required_checks_have_no_unresolved_templates() -> None:
    offenders = [context for context in _required_contexts() if "${{" in context]
    assert not offenders, f"template placeholders leaked into a context: {offenders}"


def test_required_checks_exist_in_the_workflows() -> None:
    known = actual_check_names()
    missing = sorted(set(_required_contexts()) - known)
    assert not missing, (
        "branch protection requires checks no job produces (a pull request could "
        f"never merge): {missing}; produced checks are: {sorted(known)}"
    )


@pytest.mark.parametrize("workflow", GATING_WORKFLOWS)
def test_every_gating_workflow_is_required(workflow: str) -> None:
    """PBK 8: these four workflows gate every change."""
    produced = set(_checks_by_file()[workflow])
    gating = produced & set(_required_contexts())
    assert gating, f"{workflow} gates nothing: add its job(s) to required_status_checks"


def test_jobs_use_explicit_github_facing_names() -> None:
    """Each gating job names itself, consistently within its workflow.

    Without an explicit `name:`, GitHub renders `"<Workflow> (<job id>)"`, which
    is easy to mistype in branch protection and hard to read in the UI.
    """
    for workflow in GATING_WORKFLOWS:
        manifest = _manifest(WORKFLOW_DIRECTORY / workflow)
        prefixes: dict[str, set[str]] = {}
        for job_id, job in (manifest.get("jobs") or {}).items():
            name = str(job.get("name") or "")
            assert name, f"{workflow}:{job_id} has no explicit name"
            assert " / " in name, f"{workflow}:{job_id} name '{name}' is not 'Workflow / job'"
            prefixes.setdefault(name.split(" / ", maxsplit=1)[0], set()).add(job_id)
        assert len(prefixes) == 1, f"{workflow} mixes job-name prefixes: {sorted(prefixes)}"
