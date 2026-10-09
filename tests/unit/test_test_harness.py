"""Verifies the bootstrap deliverables themselves (PBK Chapter 19).

These are the acceptance criteria of the Project Bootstrap Kit expressed as
tests. They cost milliseconds and fail loudly if scaffolding is removed,
renamed or never finished.
"""

from __future__ import annotations

import ast
import re
import tomllib
from importlib import metadata
from pathlib import Path

import pytest

import encore
from tests.support.media import MediaSpec, generate_track
from tests.support.mpv import MockMpv
from tests.support.party_profiles import PartyProfile, load_profile
from tests.support.sqlite import TempDatabase

PROJECT_ROOT = Path(__file__).resolve().parents[2]

ADR_DIRECTORY = PROJECT_ROOT / "docs" / "adr"

#: The eight decisions mandated by SAPRS 15.9 / PBK 13.
REQUIRED_ADRS: dict[str, str] = {
    "ADR-001-separate-library-builder-from-server.md": "Separate Library Builder from Server",
    "ADR-002-htmx-instead-of-spa.md": "HTMX Instead of SPA",
    "ADR-003-sqlite-as-the-storage-engine.md": "SQLite as the Storage Engine",
    "ADR-004-internal-event-bus.md": "Internal Event Bus",
    "ADR-005-mpv-playback-engine.md": "mpv Playback Engine",
    "ADR-006-immutable-library-database.md": "Immutable Library Database",
    "ADR-007-anonymous-guest-model.md": "Anonymous Guest Model",
    "ADR-008-appliance-first-philosophy.md": "Appliance-First Philosophy",
}

#: SAPRS 15.8 requires each of these sections in every ADR.
ADR_SECTIONS = ("Status", "Context", "Decision", "Consequences", "Alternatives considered")

#: PBK Chapter 11.
REQUIRED_ROOT_DOCUMENTS = (
    "README.md",
    "CONTRIBUTING.md",
    "CODE_OF_CONDUCT.md",
    "SECURITY.md",
    "CHANGELOG.md",
    "LICENSE",
)


def test_shared_fixtures_are_wired(
    library_db: TempDatabase,
    runtime_db: TempDatabase,
    mpv: MockMpv,
    media_dir: Path,
) -> None:
    assert library_db.path.name == "library.db"
    assert runtime_db.path.name == "runtime.db"
    assert library_db.path.parent == runtime_db.path.parent
    assert isinstance(mpv, MockMpv)
    assert media_dir.is_dir()


def test_unit_tests_are_marked_automatically(request: pytest.FixtureRequest) -> None:
    assert request.node.get_closest_marker("unit") is not None


def test_slow_suite_opt_in_option_is_registered(pytestconfig: pytest.Config) -> None:
    assert pytestconfig.getoption("--run-slow") in (True, False)


def test_party_profile_option_defaults_to_a_real_file(pytestconfig: pytest.Config) -> None:
    path = Path(str(pytestconfig.getoption("--party-profile")))
    assert path.is_file(), f"default party profile missing: {path}"
    assert isinstance(load_profile(path), PartyProfile)


def test_synthetic_media_is_real(tmp_path: Path) -> None:
    """PBK 16 shipped this as a placeholder that raised; milestone 6 made it produce files.

    The assertion is that a generated track exists and is non-empty, which is the
    vacuous-suite failure the placeholder existed to prevent, checked directly.
    """

    path = generate_track(MediaSpec(title="x", artist="y", album="z"), tmp_path)
    assert path.is_file()
    assert path.stat().st_size > 0


def test_project_is_installed_editably_and_metadata_agrees() -> None:
    """PBK 3 requires an editable development install; versions must not drift."""

    with (PROJECT_ROOT / "pyproject.toml").open("rb") as handle:
        project_version: str = tomllib.load(handle)["project"]["version"]

    assert project_version == encore.__version__ == metadata.version("encore")
    assert Path(encore.__file__).resolve().is_relative_to(PROJECT_ROOT)


@pytest.mark.parametrize(("filename", "title"), sorted(REQUIRED_ADRS.items()))
def test_initial_adrs_exist_with_the_mandated_shape(filename: str, title: str) -> None:
    path = ADR_DIRECTORY / filename
    assert path.is_file(), f"missing ADR: {filename} ({title})"
    text = path.read_text(encoding="utf-8")
    assert title.lower() in text.lower(), f"{filename} must be titled {title!r}"
    for section in ADR_SECTIONS:
        assert f"## {section}" in text, f"{filename} is missing a '{section}' section"
    assert "## Date" in text, f"{filename} is missing a date (SAPRS 15.8)"


@pytest.mark.parametrize("document", REQUIRED_ROOT_DOCUMENTS)
def test_root_documents_are_present(document: str) -> None:
    assert (PROJECT_ROOT / document).is_file(), f"{document} missing (PBK 11)"


def test_github_configuration_is_present() -> None:
    github = PROJECT_ROOT / ".github"
    for name in ("CODEOWNERS", "dependabot.yml", "pull_request_template.md", "labels.yml"):
        assert (github / name).is_file(), f".github/{name} missing"
    workflows = sorted(p.name for p in (github / "workflows").glob("*.yml"))
    assert workflows == [
        "docs.yml",
        "release.yml",
        "security.yml",
        "test.yml",
        "validate.yml",
    ], "PBK 6 requires exactly these five workflows"


def test_issue_templates_cover_the_required_set() -> None:
    templates = {p.name for p in (PROJECT_ROOT / ".github" / "ISSUE_TEMPLATE").glob("*.yml")}
    assert templates >= {
        "bug_report.yml",
        "feature_request.yml",
        "documentation.yml",
        "performance.yml",
        "security_concern.yml",
        "refactoring_proposal.yml",
        "question.yml",
    }, "PBK 8 requires all seven issue forms"


def test_ai_support_materials_are_organised() -> None:
    ai = PROJECT_ROOT / "ai"
    for directory in ("prompts", "context", "task_templates", "reviews", "checklists"):
        assert (ai / directory).is_dir(), f"ai/{directory} missing (PBK 18)"
    assert any((ai / "prompts").glob("*.md")), "ai/prompts must contain at least one prompt"


#: Full semver tags or commit SHAs. Moving major tags (`@v3`) are not published by
#: every action author — `astral-sh/setup-uv@v10` failed CI with "unable to find
#: version v10" because that repository tags only full versions from v8 onward.
#: Pinning also satisfies SAPRS 15.10's "reproducible" requirement, and the
#: github-actions Dependabot ecosystem keeps the pins current weekly.
FULLY_PINNED = re.compile(r"^(?:v\d+\.\d+\.\d+|[0-9a-f]{40})$")


def test_workflow_actions_are_pinned_to_resolvable_versions() -> None:
    offenders: list[str] = []
    for workflow in sorted((PROJECT_ROOT / ".github" / "workflows").glob("*.yml")):
        for line in workflow.read_text(encoding="utf-8").splitlines():
            found = re.search(r"uses:\s*(\S+)", line)
            if found is None or "@" not in found.group(1):
                continue
            action, _, revision = found.group(1).partition("@")
            if action.startswith("./") or FULLY_PINNED.match(revision):
                continue
            offenders.append(f"{workflow.name}: {action}@{revision}")
    assert not offenders, "pin every action to a full version tag:\n" + "\n".join(offenders)


def test_regression_tests_cite_their_defect() -> None:
    """`tests/regression/README.md` promises "CI checks the link"; here it does.

    An uncited regression test cannot be read against the defect it is supposed to
    block, and the suite turns into folklore (SAPRS 14.14, AEP 13). `test_index.py`
    is the suite's placeholder for the bootstrap, not a test of a defect.
    """
    offenders: list[str] = []
    for path in sorted((PROJECT_ROOT / "tests" / "regression").glob("test_*.py")):
        if path.name == "test_index.py":
            continue
        docstring = ast.get_docstring(ast.parse(path.read_text(encoding="utf-8"))) or ""
        if not re.match(r"^Regression:\s+#\d+", docstring.strip()):
            offenders.append(path.name)
    assert not offenders, "start the module docstring with 'Regression: #<issue>': " + ", ".join(
        offenders
    )


#: Categories that must be collected on every commit, not only at release.
FAST_TEST_CATEGORIES = ("unit", "integration", "regression")


def test_every_fast_test_category_is_invoked_somewhere() -> None:
    """A directory nothing collects is a directory nobody tests.

    `tests/regression` was exactly this gap: the marker existed, its README demands
    a test for every fixed defect (SAPRS 14.14), and no local gate or workflow
    collected the folder — so a regression would surface at release time only,
    where `pytest --run-slow` happens to gather everything.
    """
    local = (PROJECT_ROOT / "scripts" / "check.sh").read_text(encoding="utf-8")
    ci = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (PROJECT_ROOT / ".github" / "workflows").glob("*.yml")
    )
    for category in FAST_TEST_CATEGORIES:
        assert f"tests/{category}" in local, f"scripts/check.sh never runs tests/{category}"
        assert f"tests/{category}" in ci, f"no workflow runs tests/{category}"
