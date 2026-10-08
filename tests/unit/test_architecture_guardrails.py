"""Executable architectural guardrails (SAPRS 11.10, AIG 4, AEP 4).

The rules in the specification are stated in prose; this test states them in
code so a violation fails CI instead of a reviewer's memory. It inspects import
statements statically — no application behaviour is required for it to be
useful, which is exactly why it can exist during the bootstrap phase.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]

#: Packages that must stay free of web-framework imports.
DOMAIN_PACKAGES = (
    "encore/domain",
    "encore/services",
    "encore/events",
    "encore/repositories",
    "encore/playback",
    "encore/search",
    "encore/config",
)

FORBIDDEN_WEB_IMPORTS = ("fastapi", "starlette", "jinja2", "uvicorn", "htmx")

#: Modules that must not talk to SQLite directly; persistence is repository-only.
NO_DIRECT_SQL = ("encore/controllers", "encore/domain", "encore/services", "encore/search")

#: The Builder must never import runtime playback (ADR-001).
BUILDER_ROOT = "apps/builder"
PLAYBACK_MODULE = "encore.playback"


def _python_files(relative_dir: str) -> list[Path]:
    directory = PROJECT_ROOT / relative_dir
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.rglob("*.py") if "__pycache__" not in p.parts)


def _imports(path: Path) -> set[str]:
    """Return every imported dotted name in a file."""

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


@pytest.mark.parametrize("package", DOMAIN_PACKAGES)
def test_domain_packages_never_import_web_frameworks(package: str) -> None:
    for path in _python_files(package):
        offenders = {name for name in _imports(path) if name.split(".")[0] in FORBIDDEN_WEB_IMPORTS}
        assert not offenders, f"{path.relative_to(PROJECT_ROOT)} imports {sorted(offenders)}"


@pytest.mark.parametrize("package", NO_DIRECT_SQL)
def test_only_repositories_touch_sqlite(package: str) -> None:
    for path in _python_files(package):
        offenders = {name for name in _imports(path) if name.split(".")[0] == "sqlite3"}
        assert not offenders, f"{path.relative_to(PROJECT_ROOT)} bypasses repositories"


def test_playback_knows_nothing_about_http() -> None:
    for path in _python_files("encore/playback"):
        offenders = {
            name for name in _imports(path) if name.startswith(("encore.api", "encore.controllers"))
        }
        assert not offenders, f"{path.relative_to(PROJECT_ROOT)} leaks HTTP into playback"


def test_builder_does_not_import_runtime_playback() -> None:
    for path in _python_files(BUILDER_ROOT):
        offenders = {name for name in _imports(path) if name.startswith(PLAYBACK_MODULE)}
        assert not offenders, f"{path.relative_to(PROJECT_ROOT)} couples Builder to Server"


def test_package_skeleton_matches_the_mandated_layout() -> None:
    """AIG 5 / PBK 2 fix the layout; drift here is a defect, not a preference."""

    expected = {
        "encore/api",
        "encore/config",
        "encore/controllers",
        "encore/domain",
        "encore/events",
        "encore/playback",
        "encore/repositories",
        "encore/search",
        "encore/services",
        "encore/templates",
        "encore/static",
        "encore/utilities",
        "apps/server",
        "apps/builder",
        "tests/unit",
        "tests/integration",
        "tests/regression",
        "tests/performance",
        "tests/party_simulation",
        "docs/SAPRS",
        "docs/AIG",
        "docs/AEP",
        "docs/Developer",
        "docs/adr",
        "docs/api",
        "docs/images",
        "ai/prompts",
        "ai/context",
        "ai/task_templates",
        "ai/reviews",
        "ai/checklists",
    }
    missing = {name for name in expected if not (PROJECT_ROOT / name).is_dir()}
    assert not missing, f"mandated directories missing: {sorted(missing)}"
