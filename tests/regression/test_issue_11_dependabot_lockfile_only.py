"""Regression: #11 — Dependabot's pip updates left `uv.lock` behind.

Dependabot's default strategy for the `pip` ecosystem is a *requirement* update:
it raises the `>=` floor in `pyproject.toml` to the newest release and never
regenerates the lockfile, because it cannot run `uv lock`. CI installs from the
lock (`uv sync --frozen`), so manifest and lock disagreed and every job on all
eight pull requests failed at install — permanently unmergeable.

These tests pin the configuration that prevents it. The failure itself is not
reproduced here: it needs a `uv` resolution against PyPI, and
`Validate / dependencies` already runs `uv lock --check` on every commit, which is
the same assertion with better fidelity.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEPENDABOT_FILE = PROJECT_ROOT / ".github" / "dependabot.yml"
LABELS_FILE = PROJECT_ROOT / ".github" / "labels.yml"


def _updates() -> list[dict[str, Any]]:
    config: dict[str, Any] = yaml.safe_load(DEPENDABOT_FILE.read_text(encoding="utf-8"))
    updates: list[dict[str, Any]] = config["updates"]
    return updates


def _pip_updates() -> dict[str, Any]:
    ecosystems = [u for u in _updates() if u.get("package-ecosystem") == "pip"]
    assert len(ecosystems) == 1, "expected exactly one pip ecosystem entry"
    return ecosystems[0]


def test_pip_updates_change_the_lockfile_not_the_floors() -> None:
    assert _pip_updates().get("versioning-strategy") == "lockfile-only", (
        "requirement-style updates leave uv.lock behind, and no CI job can fix "
        "that without a human running `uv lock` (#11)"
    )


def test_pip_updates_arrive_as_one_pull_request() -> None:
    """Eight concurrent PRs is eight times the review tax for no extra safety."""
    groups = _pip_updates().get("groups") or {}
    assert groups, "group the pip updates; #11 produced one PR per dependency"


def test_every_ecosystem_labels_exist_in_the_taxonomy() -> None:
    document: dict[str, list[dict[str, Any]]] = yaml.safe_load(
        LABELS_FILE.read_text(encoding="utf-8")
    )
    declared = {str(entry["name"]) for group in document.values() for entry in group}
    for update in _updates():
        used = set(update.get("labels", []))
        missing = sorted(used - declared)
        assert not missing, (
            f"{update['package-ecosystem']} applies labels that do not exist: {missing}"
        )


def test_pip_security_updates_are_not_disabled_by_the_version_updates() -> None:
    """Vulnerability PRs come from the alerts setting, not from this block.

    A `versioning-strategy` change must not be read as switching dependency
    security off; `uv audit` in `Security / vulnerabilities` is the other half.
    """
    assert _pip_updates().get("open-pull-requests-limit", 0) > 0
    audit = (PROJECT_ROOT / ".github" / "workflows" / "security.yml").read_text("utf-8")
    assert "uv audit" in audit, "the OSV audit disappeared with it"
