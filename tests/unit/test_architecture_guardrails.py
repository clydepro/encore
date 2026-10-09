"""Executable architectural guardrails (SAPRS 11.10, AIG 4, AEP 4).

The rules in the specification are stated in prose; this test states them in
code so a violation fails CI instead of a reviewer's memory. It inspects import
statements statically — no application behaviour is required for it to be
useful, which is exactly why it can exist during the bootstrap phase.
"""

from __future__ import annotations

import ast
import dataclasses
from pathlib import Path
from typing import Any

import pytest

import encore.config
import encore.domain
import encore.events
import encore.services
import encore.utilities
from encore.config import ConfigurationService
from encore.events import EVENT_VOCABULARY, EventBus
from encore.services import LoggingService

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

#: Modules that must not talk to a database directly; persistence is repository-only.
#:
#: Both drivers, not just `sqlite3`. ADR-009 gives the runtime side SQLAlchemy, so a
#: service that opened an `engine` would bypass the repositories just as surely as one
#: that opened a file — and it would look less suspicious doing it, because a session
#: does not read like raw SQL. Issue #19's acceptance criterion asks for both.
NO_DIRECT_SQL = ("encore/controllers", "encore/domain", "encore/services", "encore/search")

#: The one module of the persistence layer the Builder may import: names, not access.
REPOSITORY_CONTRACT = "encore.repositories.contract"

#: Driver modules that only the Server's persistence layer may import. `apps/builder/`
#: is a separate application and owns its own writes (ADR-010), so it is not covered by
#: the rule and is checked by its own instead.
STORAGE_IMPORTS = ("sqlite3", "sqlalchemy")

#: The Builder must never import runtime playback (ADR-001).
BUILDER_ROOT = "apps/builder"
PLAYBACK_MODULE = "encore.playback"

#: `encore.utilities` is the shared leaf by construction (AIG 5), so the domain may
#: use a clock or a redaction helper without that counting as reaching outward.
OUTWARD_ALLOWED = ("encore.domain", "encore.utilities")

#: Fields that mention a person-shaped word and are still anonymous. A count of
#: guests is a number; `guest` would be an identity.
IDENTITY_EXCUSED = frozenset({"guest_count", "guests_seen", "session_minutes"})

#: Substrings that mean a record has started to track who asked.
GUEST_IDENTITY_MARKERS = (
    "guest",
    "requester",
    "user_id",
    "patron",
    "account",
    "source_ip",
    "client_address",
    "session_id",
)


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
        offenders = {name for name in _imports(path) if name.split(".")[0] in STORAGE_IMPORTS}
        assert not offenders, f"{path.relative_to(PROJECT_ROOT)} bypasses repositories"


def test_storage_drivers_are_imported_by_the_persistence_layer_alone() -> None:
    """Within the Server, only `encore/repositories/` opens a database.

    Enumerating the forbidden packages is how a rule stops applying the moment code
    lands somewhere new — `encore/api/`, `encore/events/` and `encore/utilities/` are
    outside `NO_DIRECT_SQL` for unrelated reasons, and a store opened in any of them
    would be a real violation with no test attached. Scanning the tree and whitelisting
    the one owner of persistence inverts the default: new Server code is covered the day
    it appears.

    `apps/builder/` is excluded because it is a different application, not a layer of
    this one. ADR-010 gives it sole ownership of `library.db`'s schema, so it must open
    the file — the rule that keeps Builder and Server decoupled is the one above this,
    and the Builder is checked there for importing nothing from `encore/`'s persistence.
    """

    offenders: list[str] = []
    for path in _python_files("encore"):
        if "repositories" in path.parts:
            continue
        found = {n for n in _imports(path) if n.split(".")[0] in STORAGE_IMPORTS}
        if found:
            offenders.append(f"{path.relative_to(PROJECT_ROOT)}: {sorted(found)}")

    assert not offenders, "only the repository layer opens a database: " + "; ".join(offenders)


def test_the_builder_does_not_reuse_the_servers_persistence() -> None:
    """ADR-010's separation, checked instead of assumed.

    The Builder owning its own DDL is the same decision that keeps it out of
    `encore/repositories/`: if either side could reach through to the other, the
    immutable-library boundary would be a convention between two writers. `schema.py`
    is the Builder's DDL and `construction.py` writes the rows — both are allowed
    `sqlite3` and neither is allowed the Server's repositories.

    `encore/repositories/contract.py` is the deliberate exception, and the narrower rule
    above is what makes it safe: the Builder may import the *names* of tables, columns
    and enums, because sharing them is the whole mitigation for two applications
    describing one schema, but it may not import a connection, a store or a repository,
    so a `Session` or a raw row cannot reach it. That distinction is ADR-009's,
    expressed as an import rule.
    """

    offenders: list[str] = []
    for path in _python_files(BUILDER_ROOT):
        # `_imports` yields both `encore.repositories.contract` and the qualified names
        # it was asked for, so the exclusion is by module, not by string equality.
        found = {
            name
            for name in _imports(path)
            if name.startswith("encore.repositories")
            and name.rstrip(".").split(".")[:3] != ["encore", "repositories", "contract"]
        }
        if found:
            offenders.append(f"{path.relative_to(PROJECT_ROOT)}: {sorted(found)}")

    assert not offenders, "the Builder must own its own writes: " + "; ".join(offenders)


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


# -- rules that became checkable once the core existed -------------------


def test_the_domain_is_the_innermost_layer() -> None:
    """AIG 4 states the well-known half - "domain services never import FastAPI".
    The other half is that the model imports nothing from the layers above it. An
    entity that reached for a repository or a service could not be constructed by
    the Builder, which is ADR-001's whole point."""

    for path in _python_files("encore/domain"):
        offenders = {
            name
            for name in _imports(path)
            if name.startswith("encore.") and not name.startswith(OUTWARD_ALLOWED)
        }
        assert not offenders, (
            f"{path.relative_to(PROJECT_ROOT)} reaches out of the domain: {sorted(offenders)}"
        )


def test_no_core_service_is_reachable_as_a_module_global() -> None:
    """AEP 9 forbids singleton service instances.

    Checked on the imported modules rather than by reading source, because the
    failure it guards is the accidental one: a module-level `BUS = EventBus()` that
    two tests then silently share, which no amount of skimming catches."""

    service_types = (EventBus, ConfigurationService, LoggingService)
    modules = (
        *vars(encore.config).values(),
        *vars(encore.domain).values(),
        *vars(encore.events).values(),
        *vars(encore.services).values(),
        *vars(encore.utilities).values(),
    )
    offenders: list[str] = []
    for module in modules:
        if not hasattr(module, "__dict__") or not str(getattr(module, "__name__", "")).startswith(
            "encore."
        ):
            continue
        offenders += [
            f"{module.__name__}.{attribute}"
            for attribute, value in vars(module).items()
            if not attribute.startswith("_") and isinstance(value, service_types)
        ]

    assert offenders == [], f"singleton services reachable by import: {offenders}"


def test_no_model_or_event_carries_guest_identity() -> None:
    """AIG 22: "do not implement guest accounts", SAPRS 8.2: guests are anonymous.

    Enforced on the vocabulary rather than in review, because the way to break it
    is small and reasonable-looking - one `source_ip` on an event to make a
    dashboard nicer."""

    offenders: list[str] = []
    for declared in (*vars(encore.domain).values(), *vars(encore.events).values()):
        if not (isinstance(declared, type) and dataclasses.is_dataclass(declared)):
            continue
        owner = str(getattr(declared, "__module__", ""))
        if not owner.startswith(("encore.domain", "encore.events")):
            continue
        for field in dataclasses.fields(declared):
            name = field.name.lower()
            if name in IDENTITY_EXCUSED:
                continue
            if any(marker in name for marker in GUEST_IDENTITY_MARKERS):
                offenders.append(f"{declared.__name__}.{field.name}")

    assert offenders == [], f"guest identity entered the model: {sorted(set(offenders))}"


def test_events_are_immutable_and_timestamped() -> None:
    """SAPRS 11.2 says events are immutable; ADR-004 makes them the only channel
    between services, so a mutable event is one service editing a fact after the
    bus handed it to someone else."""

    # `list[Any]` because `type[Event]` does not declare `__dataclass_params__`;
    # the check below is the one that matters, not the annotation.
    vocabulary: list[Any] = list(EVENT_VOCABULARY)
    for event_type in vocabulary:
        assert event_type.__dataclass_params__.frozen, f"{event_type.__name__} is mutable"
        assert hasattr(event_type, "__slots__"), f"{event_type.__name__} is not slotted"
        assert "occurred_at" in {field.name for field in dataclasses.fields(event_type)}


def test_configuration_cannot_write() -> None:
    """SAPRS 12.3: Encore must not edit and persist its own YAML.

    The cleanest way to satisfy a rule like this is to have no way to break it, so
    the Configuration package contains no write at all - and this test is what
    keeps that true when someone adds a "save settings" button and reaches for the
    obvious module to put it in."""

    writes = ("write_text", "write_bytes", "touch", "mkdir", "truncate", "chmod", "unlink")
    offenders: list[str] = []
    for path in _python_files("encore/config"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            called = ast.unparse(node.func)
            opened_for_writing = called == "open" and any(
                isinstance(argument, ast.Constant)
                and isinstance(argument.value, str)
                and set(argument.value) & set("wax+")
                for argument in [*node.args, *(keyword.value for keyword in node.keywords)]
            )
            if called.endswith(writes) or called.startswith(("yaml.dump", "yaml.safe_dump")):
                offenders.append(f"{path.relative_to(PROJECT_ROOT)}:{node.lineno} {called}")
            elif opened_for_writing:
                offenders.append(f"{path.relative_to(PROJECT_ROOT)}:{node.lineno} open(..., w)")

    assert offenders == [], f"the Configuration package wrote something: {offenders}"
