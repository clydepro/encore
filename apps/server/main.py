"""`python -m apps.server.main` — the appliance as a process (SAPRS 13.1, AIG 6).

Thin, because a server's job is to bind a socket and not to decide anything. Configuration
comes from the same `config.yaml` the Builder read, so a library built against one
`paths.music_dir` cannot be played against another (ADR-010).

The order is the interesting part, and it is all failure ordering:

1. Configuration, then logging — a config that cannot be read is printed rather than
   logged, because the logging configuration is inside the file that failed.
2. Composition, which opens both databases. A missing `library.db` exits 78 (EX_CONFIG)
   before a port is bound, because an appliance that answers requests while unable to play
   is the worse design (SAPRS 12.1).
3. The signal handlers, installed before uvicorn installs its own — uvicorn's default
   behaviour on SIGTERM is to stop accepting and drain, which is what we want; what we add
   is the reason in the journal.
4. `uvicorn.run`, which owns the event loop from here on.

Shutdown has one promise to keep (SAPRS 8.7): the process the appliance launched is
killed, so a restart does not leave two mpv instances fighting for one sound card. That
is `Appliance.stop()`'s doing, reached from the app's lifespan rather than from here, so
it happens under `systemctl stop` and under a test client alike.

Exit codes: 0 clean, 2 configuration error, 78 unusable databases (sysexits' EX_CONFIG,
which is what a systemd unit's `ExecStartPost` will notice).
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final

import uvicorn
from pydantic import ValidationError

from apps.server.appliance import SERVER_VERSION, build
from encore.api.app import create_app
from encore.config.models import EncoreConfig
from encore.config.service import ConfigurationError, ConfigurationService
from encore.repositories.errors import (
    LibraryContractError,
    RuntimeContractError,
    StoreError,
    StoreNotFoundError,
)
from encore.services.logging_service import LoggingService

__all__ = ["EXIT_CONFIG", "EXIT_OK", "EXIT_USAGE", "main", "parse"]

EXIT_OK: Final = 0
EXIT_USAGE: Final = 2
EXIT_CONFIG: Final = 78


def parse(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """The command line. `scripts/run-server.sh` forwards everything it is given.

    There are few flags because the configuration file has few reasons to be contradicted
    at the prompt: an appliance started with `--port` different from the one its mDNS name
    resolves to is a room full of phones staring at a refused connection.
    """

    parser = argparse.ArgumentParser(
        prog="encore-server",
        description="Run the Encore jukebox: HTTP, HTMX, SSE, and one mpv.",
    )
    parser.add_argument("-c", "--config", type=Path, default=None, help="config.yaml to read")
    parser.add_argument("--host", default=None, help="override server.host for this run")
    parser.add_argument("--port", type=int, default=None, help="override server.port")
    parser.add_argument(
        "--log-level",
        default=None,
        choices=("debug", "info", "warning", "error"),
        help="override logging.level",
    )
    parser.add_argument(
        "--no-playback",
        action="store_true",
        help="serve the interface without launching mpv (development on a laptop)",
    )
    parser.add_argument("--version", action="store_true", help="print the version and exit")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Compose, serve, and return the process exit code. Never raises for a bad file."""

    arguments = parse(argv)
    if arguments.version:
        print(f"encore-server {SERVER_VERSION}")
        return EXIT_OK

    try:
        config = _override(ConfigurationService(arguments.config).load(), arguments)
    except (ConfigurationError, ValidationError) as error:
        # Before LoggingService: the logging section is inside the file that failed, so a
        # logger here would either not exist or log to a place the operator did not choose.
        # A bad `--port` is the same kind of problem as a bad YAML key, and says so the
        # same way, which is why the flag overrides are validated rather than applied.
        print(_explain(error), file=sys.stderr)
        return EXIT_USAGE

    config = _override(config, arguments)
    LoggingService(config.logging).configure()
    log = logging.getLogger("encore.main")

    try:
        appliance = build(config, launch_engine=not arguments.no_playback)
    except (StoreNotFoundError, LibraryContractError, RuntimeContractError, StoreError):
        # The library the installer said it built is not readable: the traceback is the
        # diagnosis, and systemd has somewhere to put it.
        log.exception("cannot start")
        return EXIT_CONFIG

    log.info(
        "serving",
        extra={
            "host": config.server.host,
            "port": config.server.port,
            "domain": config.server.domain,
            "playback": not arguments.no_playback,
        },
    )
    try:
        uvicorn.run(
            create_app(appliance),
            host=config.server.host,
            port=config.server.port,
            log_config=None,
            access_log=False,
            # One worker, in-process, always. APE 11.4's concurrency model is one appliance
            # holding one mpv; a second uvicorn worker would be a second queue reading the
            # same `runtime.db` and a second process on the same sound card.
            workers=1,
        )
    except KeyboardInterrupt:  # pragma: no cover - a terminal's Ctrl-C
        log.info("interrupted")
    finally:
        appliance.close()
    return EXIT_OK


def _override(config: EncoreConfig, arguments: argparse.Namespace) -> EncoreConfig:
    """Command-line host, port and level, re-validated rather than pasted in.

    `model_copy(update=...)` would be two lines shorter and would let `--port 99999999`
    reach uvicorn as an `OSError` at bind time. Round-tripping through `model_validate`
    means a flag that contradicts `server.port`'s bounds fails here with the same sentence a
    bad YAML value fails with (SAPRS 12.5's "names every problem at once").
    """

    sections = config.model_dump()
    if arguments.host is not None:
        sections["server"]["host"] = arguments.host
    if arguments.port is not None:
        sections["server"]["port"] = arguments.port
    if arguments.log_level is not None:
        sections["logging"]["level"] = arguments.log_level
    return EncoreConfig.model_validate(sections)


def _explain(error: Exception) -> str:
    """One block of stderr for a configuration that will not start the appliance."""

    if isinstance(error, ValidationError):
        return "\n".join(
            f"  {part}: {item['msg']}" for item in error.errors() for part in item["loc"]
        )
    return str(error)


if __name__ == "__main__":  # pragma: no cover - exercised through scripts/run-server.sh
    sys.exit(main(sys.argv[1:]))
