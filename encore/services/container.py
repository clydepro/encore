"""The composition root: where dependencies are chosen, once (SAPRS 11.5, AIG 17).

Every service in Encore takes its collaborators in its constructor; something
still has to decide what those collaborators *are*. That something is this
module, and it is deliberately small and explicit rather than clever:

* **No registry, no decorators, no container library.** AEP 9 rules out
  singletons and module-level state, and AEP 10 asks whether the standard library
  is sufficient. A function that builds three objects and returns them is the
  whole mechanism.
* **SAPRS 11.7 startup order, in that order.** Configuration first, because
  nothing else can be built without it; logging second, because the third step
  should be able to report what it did; the Event Bus last, since it is the thing
  the rest of the runtime talks through.
* **Database and playback wiring arrive with their milestones** (steps 3, 4 and 6
  of SAPRS 11.7). `apps/server/main.py` (milestone 11) will extend this list
  rather than invent its own, and `apps/builder` (milestone 6) will build only
  what a terminating offline process needs.

`CoreServices` is a value, not a service locator: it holds named attributes and
offers no `get(name)`, so a missing dependency is an attribute error at the call
site rather than a string lookup at runtime.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from logging import Logger
from pathlib import Path

from encore.config.models import EncoreConfig
from encore.config.service import ConfigurationService
from encore.events.bus import EventBus
from encore.services.logging_service import LoggingService

__all__ = ["CoreServices", "build_core_services"]


@dataclass(frozen=True, slots=True, kw_only=True)
class CoreServices:
    """What every Encore process needs before it has a single feature.

    Attributes:
        config: The validated configuration, already loaded.
        logging: The configured logging service. Call `remove()` at shutdown to
            undo `configure()` in a process that outlives the services (tests).
        events: The Event Bus. Nothing else may stand between services.
        logger: A logger for the composition root's own reporting.
    """

    # A field named `logging` shadows the module for the rest of this class body,
    # which is why `logger` below is annotated `Logger` rather than
    # `logging.Logger`. Keep that in mind before adding another annotation here.
    config: EncoreConfig
    logging: LoggingService
    events: EventBus
    logger: Logger


def build_core_services(
    path: Path | None = None,
    *,
    config: EncoreConfig | None = None,
    logger: Logger | None = None,
) -> CoreServices:
    """Load configuration, install logging and create the Event Bus.

    Args:
        path: `config.yaml` to read. Exactly one of `path` or `config`.
        config: Pre-validated configuration, for tests and embedders.
        logger: Logger to attach the bus to. Defaults to
            ``logging.getLogger("encore")``.

    Returns:
        The wired core, ready for the first real service to subscribe to it.

    Raises:
        ValueError: Both `path` and `config` were supplied.
        encore.config.service.ConfigurationError: The configuration is unusable
            (SAPRS 12.5), so startup stops here with a diagnostic.
    """

    service = ConfigurationService(path, config=config)
    loaded = service.config
    bound = logger if logger is not None else logging.getLogger("encore")
    logging_service = LoggingService(loaded.logging).configure()
    bound.debug(
        "core services built",
        extra={"configuration_path": None if path is None else str(path)},
    )
    return CoreServices(
        config=loaded,
        logging=logging_service,
        events=EventBus(logger=bound),
        logger=bound,
    )
