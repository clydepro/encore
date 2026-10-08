"""The composition root: what "the process is up" means before any feature exists.

`build_core_services()` is the only place in Encore allowed to know how the core
services are constructed (AEP 9). These tests hold that claim down: the wiring is
real, it is reversible, and it does not quietly reach for a global.
"""

from __future__ import annotations

import ast
import dataclasses
import io
import json
import logging
from pathlib import Path

import pytest
import yaml

from encore.config import ConfigurationError, ConfigurationService, EncoreConfig
from encore.domain import QueueItemId, SongId
from encore.events import EventBus, SongQueued
from encore.services import CoreServices, LoggingService, build_core_services

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_CONFIG = REPO_ROOT / "examples" / "config.yaml"
CONTAINER_SOURCE = (REPO_ROOT / "encore" / "services" / "container.py").read_text(encoding="utf-8")


@pytest.fixture
def isolated_logging() -> object:
    """Undo whatever a test installed on the root logger.

    AEP 9's rule about owned state applies to the composition root too: it
    configures logging in a process, so a test that uses it has to hand the
    process back the way it found it.
    """

    root = logging.getLogger()
    handlers = list(root.handlers)
    level = root.level
    yield
    for handler in root.handlers:
        if handler not in handlers:
            root.removeHandler(handler)
            handler.close()
    root.handlers = handlers
    root.setLevel(level)


# -- what it builds ----------------------------------------------------


def test_it_returns_the_three_things_every_process_needs() -> None:
    core = build_core_services(config=EncoreConfig())

    assert isinstance(core, CoreServices)
    assert isinstance(core.config, EncoreConfig)
    assert isinstance(core.logging, LoggingService)
    assert isinstance(core.events, EventBus)
    assert core.logger.name == "encore"


def test_the_container_is_frozen_and_names_its_parts() -> None:
    """A container that could be re-pointed mid-run is a global with extra steps."""

    core = build_core_services(config=EncoreConfig())

    with pytest.raises(dataclasses.FrozenInstanceError):
        core.events = EventBus()  # type: ignore[misc]


def test_no_service_is_reachable_by_import_from_a_global() -> None:
    """AEP 9: nothing may be fetched without being passed.

    Checked as syntax rather than by importing and poking at attributes: a
    module-level name may be an import, a constant or a singleton, and `hasattr`
    cannot tell them apart. The composition root is allowed imports, definitions
    and `__all__` - an instance would need an assignment, and an assignment here
    fails this test.
    """

    tree = ast.parse(CONTAINER_SOURCE)
    bound = [
        target.id
        for statement in tree.body
        if isinstance(statement, (ast.Assign, ast.AnnAssign))
        for target in (
            statement.targets if isinstance(statement, ast.Assign) else [statement.target]
        )
        if isinstance(target, ast.Name)
    ]

    assert [name for name in bound if name != "__all__"] == [], (
        f"container.py binds {bound} at module level; a service reachable by import "
        "is a singleton (AEP 9)"
    )
    assert "def build_core_services" in CONTAINER_SOURCE


def test_the_same_bus_arrives_at_every_consumer() -> None:
    """The claim that matters for DI: two services built from one container are
    wired to each other, not to two instances that will never meet."""

    core = build_core_services(config=EncoreConfig())
    seen: list[str] = []

    class Queue:
        def __init__(self, bus: EventBus) -> None:
            bus.subscribe(SongQueued, lambda event: seen.append(str(event.song_id)))

    class Search:
        def __init__(self, bus: EventBus) -> None:
            self.bus = bus

    queue, search = Queue(core.events), Search(core.events)
    search.bus.publish(
        SongQueued(song_id=SongId(7), queue_item_id=QueueItemId(7), position=1, queue_length=1)
    )

    assert seen == ["7"]
    assert isinstance(queue, Queue)
    assert isinstance(search, Search)


# -- configuration -----------------------------------------------------


def test_a_configuration_file_is_read_and_installed(isolated_logging: object) -> None:
    core = build_core_services(path=EXAMPLE_CONFIG)

    assert core.config.logging.format == "json"
    assert core.logging.level == logging.INFO


def test_the_container_holds_the_configuration_the_service_loaded() -> None:
    service = ConfigurationService(path=EXAMPLE_CONFIG)
    service.load()
    core = build_core_services(config=service.config)

    assert core.config is service.config


def test_an_invalid_file_stops_the_process_before_anything_exists(tmp_path: Path) -> None:
    """Startup must fail loudly (SAPRS 12.5): no bus to publish into, no handler
    attached to the root logger, nothing half-built."""

    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"server": {"port": "not-a-number"}}), encoding="utf-8")
    root = logging.getLogger()
    before = list(root.handlers)

    with pytest.raises(ConfigurationError):
        build_core_services(path=path)

    assert list(root.handlers) == before, "a refused startup must not leave a handler behind"


def test_supplying_both_a_path_and_a_config_is_refused(tmp_path: Path) -> None:
    """Two sources of truth in one call is a bug; the loader says so instead of
    quietly preferring one."""

    with pytest.raises(ValueError, match="not both"):
        build_core_services(tmp_path, config=EncoreConfig())


def test_no_arguments_yet_produces_the_documented_appliance() -> None:
    """A developer running the server without `/etc/encore/config.yaml` gets the
    Chapter 13 defaults rather than a crash at import time."""

    core = build_core_services()

    assert core.config.server.port == 8080
    assert core.config.queue.max_items == 200


# -- logging -----------------------------------------------------------


def test_logging_is_installed_on_the_process_and_removed_again(
    isolated_logging: object,
) -> None:
    root = logging.getLogger()
    before = list(root.handlers)

    core = build_core_services(config=EncoreConfig())
    assert len(root.handlers) == len(before) + 1

    core.logging.remove()
    assert list(root.handlers) == before


def test_the_configured_level_is_the_one_in_force(isolated_logging: object) -> None:
    core = build_core_services(
        config=EncoreConfig(logging={"level": "WARNING", "format": "console"})  # type: ignore[arg-type]
    )
    try:
        assert core.logging.level == logging.WARNING
        assert logging.getLogger().level == logging.WARNING
    finally:
        core.logging.remove()


def test_events_published_through_the_container_are_loggable(isolated_logging: object) -> None:
    """Proves the two halves of the container work together: a handler that fails
    is reported through the logging the same call installed."""

    buffer = io.StringIO()
    core = build_core_services(config=EncoreConfig(logging={"format": "json"}))  # type: ignore[arg-type]
    core.logging.remove()
    services = LoggingService(EncoreConfig().logging, stream=buffer)
    services.configure()
    core.events.subscribe(SongQueued, lambda _event: (_ for _ in ()).throw(RuntimeError("boom")))

    report = core.events.publish(
        SongQueued(song_id=SongId(1), queue_item_id=QueueItemId(1), position=1, queue_length=1)
    )

    assert report.delivered == 0
    line = json.loads(buffer.getvalue())
    assert line["event"] == "SongQueued"
    assert line["error_type"] == "RuntimeError"
    services.remove()


def test_a_logger_can_be_supplied_for_attribution(isolated_logging: object) -> None:
    """The container should not decide who owns the root logger's configuration;
    a caller passing a logger is how a test keeps the process quiet."""

    collector = logging.getLogger("encore.test.root")
    collector.addHandler(handler := logging.NullHandler())
    try:
        core = build_core_services(config=EncoreConfig(), logger=collector)
        assert core.logger is collector
    finally:
        collector.removeHandler(handler)


# -- the event bus -----------------------------------------------------


def test_the_bus_starts_with_no_subscribers() -> None:
    """The composition root wires services, and there are no services yet. A
    non-zero count here would mean something installed itself globally."""

    assert build_core_services(config=EncoreConfig()).events.subscription_count == 0


def test_two_containers_do_not_share_a_bus() -> None:
    """The opposite of a singleton, asserted directly: if these two buses were one
    object, `docs/Developer/Testing.md`'s isolation claim would already be false."""

    first, second = (
        build_core_services(config=EncoreConfig()),
        build_core_services(config=EncoreConfig()),
    )
    seen: list[str] = []
    first.events.subscribe(SongQueued, lambda event: seen.append(event.name))

    second.events.publish(
        SongQueued(song_id=SongId(1), queue_item_id=QueueItemId(1), position=1, queue_length=1)
    )

    assert seen == []
    assert first.events is not second.events
