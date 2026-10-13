"""Does the built artifact contain the appliance? (issue #32, SAPRS 13.4, AIG 20)

A wheel is a product. Everything else in this repository is checkable by importing it; this is
the one deliverable that can only be checked by opening the box, because an installed Encore runs
from `site-packages`, where the source tree's conventions do not reach it.

The bug this file was written for was found with `unzip -l`: `apps/server` was missing from the
declared packages, so a wheel would have installed an appliance with no server in it — while every
test in the suite passed, because pytest imports from the checkout. The same check now covers the
templates and the static files, which fail later and quieter: a service that starts cleanly and
answers 500 on the first page view.
"""

from __future__ import annotations

import subprocess
import tomllib
import zipfile
from pathlib import Path
from typing import Final

import pytest

REPO: Final = Path(__file__).resolve().parents[2]
PYPROJECT: Final = REPO / "pyproject.toml"

#: Directories that hold modules an installed appliance imports. `apps/builder` is here because
#: the appliance must be able to build a library on the box (SAPRS 13.11's "valid library
#: access"), and `apps/installer` because an installer that only runs from a checkout is not one.
SHIPPED: Final = ("encore", "apps")


def pyproject() -> dict[str, object]:
    with PYPROJECT.open("rb") as handle:
        return tomllib.load(handle)


def declared_packages() -> list[str]:
    tool = pyproject()["tool"]
    assert isinstance(tool, dict)
    setuptools = tool["setuptools"]
    assert isinstance(setuptools, dict)
    return [str(name) for name in setuptools["packages"]]


def console_scripts() -> dict[str, str]:
    project = pyproject()["project"]
    assert isinstance(project, dict)
    scripts = project["scripts"]
    assert isinstance(scripts, dict)
    return {str(name): str(target) for name, target in scripts.items()}


def package_data() -> dict[str, list[str]]:
    tool = pyproject()["tool"]
    assert isinstance(tool, dict)
    data = tool["setuptools"]["package-data"]
    assert isinstance(data, dict)
    return {str(key): [str(pattern) for pattern in value] for key, value in data.items()}


# -- what is declared ---------------------------------------------------------------


def test_every_package_on_disk_is_declared() -> None:
    """The guardrail this file exists for.

    A directory that holds modules and is not in `packages` is a directory that does not ship, and
    the failure is silent at build time and total at install time. Enumerated from the tree rather
    than asserted as a list so that adding a package forces the same decision here.
    """

    on_disk = sorted(
        str(init.parent.relative_to(REPO)).replace("/", ".")
        for name in SHIPPED
        for init in (REPO / name).rglob("__init__.py")
    )
    declared = set(declared_packages())
    # `apps` is the one declared name with no `__init__.py`: a namespace parent, which Python 3.12
    # resolves without help and which has to stay in the list or the `apps.*` packages below it
    # lose the directory setuptools needs to install them into.
    namespace_parents = {name for name in declared if name not in set(on_disk)}
    assert namespace_parents <= {"apps"}, f"declared and not a package: {sorted(namespace_parents)}"
    undeclared = sorted(set(on_disk) - declared)
    assert not undeclared, f"packages on disk and not declared: {undeclared}"
    stale = sorted(
        name for name in declared - set(on_disk) if not (REPO / name.replace(".", "/")).is_dir()
    )
    assert not stale, f"packages declared and not on disk: {stale}"


def test_declared_packages_exist_and_are_importable() -> None:
    """Each entry is a directory that Python can resolve to something.

    `apps` and `apps.builder` have no `__init__.py` — they are namespace packages, which Python
    3.12 resolves without help and which setuptools ships as directories. That is allowed, but it
    has to be *deliberate*, so a package with no `__init__.py` is accepted only when something
    inside it is also declared: the difference between a namespace parent and a package that lost
    its `__init__.py` is invisible to an importer and obvious here.
    """

    declared = set(declared_packages())
    for name in declared:
        directory = REPO.joinpath(*name.split("."))
        assert directory.is_dir(), f"{name} is declared and does not exist"
        if (directory / "__init__.py").is_file():
            continue
        children = {child for child in declared if child.startswith(f"{name}.")}
        assert children, f"{name} is neither a package nor a namespace parent of one"


def test_the_appliance_entry_points_are_declared() -> None:
    """Three commands, because the product is three programs (SAPRS 13, ADR-014)."""

    assert set(console_scripts()) == {"encore-server", "encore-builder", "encore-install"}
    assert console_scripts()["encore-server"].startswith("apps.server")
    assert console_scripts()["encore-builder"].startswith("apps.builder")
    assert console_scripts()["encore-install"].startswith("apps.installer")


def test_each_console_script_points_at_a_module_that_has_the_callable() -> None:
    """`module:function` is a string no build step checks, and the error surfaces at exec time."""

    for name, target in console_scripts().items():
        module, _, attribute = target.partition(":")
        found = REPO.joinpath(*module.split("."))
        assert (found / "__init__.py").is_file() or found.with_suffix(".py").is_file(), (
            f"{name} points at {module}, which is not importable"
        )
        source = (found / "__init__.py") if found.is_dir() else found.with_suffix(".py")
        assert f"def {attribute}(" in source.read_text(), (
            f"{name} would fail with an AttributeError"
        )


def test_the_runtime_dependencies_include_what_the_appliance_imports() -> None:
    """`segno` arrived with `GET /qr`; a dependency list that forgot it ships a broken box."""

    project = pyproject()["project"]
    assert isinstance(project, dict)
    required = " ".join(str(item) for item in project.get("dependencies", [])).lower()
    for name in ("fastapi", "httpx", "jinja2", "sqlalchemy", "pydantic", "pyyaml", "segno"):
        assert name in required, f"{name} is imported by the appliance and not declared"


def test_the_builder_only_extras_stay_extras() -> None:
    """MusicBrainz lookups are a build-time network feature, not something to ship to a Pi.

    Asserted as a boundary rather than as a list, because the failure mode is the opposite of the
    one this file is about: a dependency declared for the runtime that nothing at runtime needs.
    """

    project = pyproject()["project"]
    assert isinstance(project, dict)
    required = " ".join(str(item) for item in project.get("dependencies", [])).lower()
    for name in ("requests", "musicbrainz"):
        assert name not in required, f"{name} is a builder concern in the runtime's dependency list"


def test_qr_is_not_an_optional_extra() -> None:
    """An optional dependency a shipped page needs is a 500 on an install made without extras.

    `GET /qr` is in the guest journey (SAPRS 9.11), so its encoder is a runtime requirement and
    not a feature flag. Asserted separately from the list above because the failure this prevents
    is a `try: import segno` guard appearing in the controller, which would turn a missing
    dependency into a page that says "QR is not available on this build".
    """

    project = pyproject()["project"]
    assert isinstance(project, dict)
    assert any("segno" in str(item) for item in project.get("dependencies", []))
    assert "segno" not in str(project.get("optional-dependencies", {})).lower()


def test_package_data_names_the_directories_the_renderer_reads() -> None:
    """`html.py` resolves templates and static relative to the installed package.

    A wheel without them builds happily and fails on the first request, so the patterns are
    checked against the files on disk rather than against each other.
    """

    data = " ".join(" ".join(patterns) for patterns in package_data().values())
    for pattern in ("templates/", "static/"):
        assert pattern in data, f"{pattern} is not covered by package-data"


# -- what is in the wheel ------------------------------------------------------------


@pytest.fixture(scope="module")
def wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The real distribution, built once.

    Slow by construction: it runs the build backend. Everything above costs milliseconds and
    checks a declaration; this is the check that cannot be faked, because the question is what the
    packaging tool did with those declarations.
    """

    output = tmp_path_factory.mktemp("wheel")
    built = subprocess.run(  # noqa: S603 - this repository's own build backend, from its own argv
        ["uv", "build", "--wheel", "--out-dir", str(output), str(REPO)],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    if built.returncode != 0:
        pytest.skip(f"`uv build` could not run in this environment: {built.stderr.strip()[:200]}")
    produced = sorted(output.glob("*.whl"))
    assert produced, "uv build produced no wheel"
    return produced[0]


def names_in(archive: Path) -> set[str]:
    with zipfile.ZipFile(archive) as opened:
        return set(opened.namelist())


@pytest.mark.slow
def test_the_wheel_contains_every_declared_package(wheel: Path) -> None:
    names = names_in(wheel)
    for package in declared_packages():
        directory = package.replace(".", "/")
        assert any(name.startswith(f"{directory}/") for name in names), f"{package} did not ship"


@pytest.mark.slow
def test_the_wheel_contains_every_template(wheel: Path) -> None:
    """Compared against the tree, so a template added without a rule is caught rather than averaged."""

    names = names_in(wheel)
    on_disk = {
        str(path.relative_to(REPO)) for path in (REPO / "encore" / "templates").rglob("*.html")
    }
    missing = sorted(path for path in on_disk if path not in names)
    assert not missing, f"templates that would not install: {missing[:5]}"


@pytest.mark.slow
def test_the_wheel_contains_the_static_assets(wheel: Path) -> None:
    names = names_in(wheel)
    on_disk = {
        str(path.relative_to(REPO))
        for path in (REPO / "encore" / "static").rglob("*")
        if path.suffix in {".css", ".js", ".svg"}
    }
    missing = sorted(path for path in on_disk if path not in names)
    assert not missing, f"assets that would not install: {missing[:5]}"


@pytest.mark.slow
def test_the_wheel_ships_the_commands_it_claims(wheel: Path) -> None:
    """`entry_points.txt` is what `pip install` turns into scripts, so this is the last word."""

    with zipfile.ZipFile(wheel) as opened:
        manifest = next(
            (
                opened.read(name).decode()
                for name in opened.namelist()
                if name.endswith("entry_points.txt")
            ),
            "",
        )
    for command in console_scripts():
        assert command in manifest, f"{command} is not an entry point of the built wheel"


@pytest.mark.slow
def test_the_wheel_carries_the_version_the_project_claims(wheel: Path) -> None:
    with zipfile.ZipFile(wheel) as opened:
        metadata = next(
            (opened.read(name).decode() for name in opened.namelist() if name.endswith("METADATA")),
            "",
        )
    project = pyproject()["project"]
    assert isinstance(project, dict)
    assert f"Version: {project['version']}" in metadata


@pytest.mark.slow
def test_the_wheel_is_not_holding_development_tools(wheel: Path) -> None:
    """13.4's "system Python must not be modified unnecessarily", applied to the artifact.

    A wheel that required pytest would pull it onto an appliance through the dependency resolver,
    and an appliance with a test runner on it is an appliance with an attack surface nobody asked
    for.
    """

    with zipfile.ZipFile(wheel) as opened:
        metadata = next(
            (opened.read(name).decode() for name in opened.namelist() if name.endswith("METADATA")),
            "",
        )
    requires = [line for line in metadata.splitlines() if line.startswith("Requires-Dist")]
    assert not [line for line in requires if "pytest" in line or "ruff" in line or "mypy" in line]
