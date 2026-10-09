"""`encore-builder` — the command-line entry point (SAPRS 6.1, AIG 6, ADR-001).

Thin on purpose. Configuration comes from the same `config.yaml` the Server reads,
because a library built against a different `paths.music_dir` than the appliance plays
from is a defect nobody can see until a guest searches for something that is not
there. The flags below override a path or toggle a stage; none of them chooses where
the output goes, and the reason is ADR-006's — an `--output` argument would make
publishing a decision of the moment rather than of the installation, and the whole
point of the artifact is that the Server's configuration says where it is.

Exit codes, because a systemd timer or an operator's shell may both read them:

| Code | Meaning |
| ---- | ------- |
| 0 | Built and published (or built and validated, with `--dry-run`) |
| 1 | Built, but validation refused publication — the previous library is intact |
| 2 | Usage or configuration error (SAPRS 12.5: it names every problem at once) |
| 3 | The build could not run: unreadable root, unwritable paths, nothing to build |

A `--dry-run` that validated exits 0, because the operator who asked for one was asking
a question and got an answer; a dry run that failed validation exits 1, because the
question it answered was "would this publish?".
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from apps.builder.artwork import ArtworkLimits
from apps.builder.pipeline import BUILDER_VERSION, BuildOptions, run
from encore.config.models import EncoreConfig
from encore.config.service import ConfigurationError, ConfigurationService
from encore.services.logging_service import LoggingService

__all__ = ["EXIT_ABORTED", "EXIT_NOT_PUBLISHED", "EXIT_OK", "EXIT_USAGE", "main", "parse"]

EXIT_OK: Final = 0
EXIT_NOT_PUBLISHED: Final = 1
EXIT_USAGE: Final = 2
EXIT_ABORTED: Final = 3


def parse(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """The command line, in one place.

    `scripts/run-builder.sh` forwards whatever it is given, so anything a human can
    type has to appear here rather than in a shell script's argument list.
    """

    parser = argparse.ArgumentParser(
        prog="encore-builder",
        description="Build an immutable Encore library.db from a music directory.",
    )
    parser.add_argument("-c", "--config", type=Path, default=None, help="config.yaml to read")
    parser.add_argument("--music-dir", type=Path, default=None, help="override paths.music_dir")
    parser.add_argument("--artwork-dir", type=Path, default=None, help="override paths.artwork_dir")
    parser.add_argument("--temp-dir", type=Path, default=None, help="override paths.temp_dir")
    parser.add_argument("--library", type=Path, default=None, help="override paths.library_db")
    parser.add_argument(
        "--full", action="store_true", help="ignore the previous build and checkpoints"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="build and validate, publish nothing"
    )
    parser.add_argument(
        "--musicbrainz",
        action="store_true",
        help="fill missing metadata from MusicBrainz (optional, rate-limited, off by default)",
    )
    parser.add_argument(
        "--max-artwork",
        type=int,
        default=ArtworkLimits().max_assets,
        help="cap on distinct images written this run",
    )
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    parser.add_argument("--version", action="store_true", help="print the Builder version and exit")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:  # noqa: PLR0911 - one return per exit code
    """Run one build and return the process exit code. Never raises for a build failure.

    A failed *build* is a report and exit 1, because there is a previous library and it
    still works. A failed *invocation* is exit 2 or 3, because an operator is going to
    have to do something. Telling those apart is the reason this function does not
    simply let exceptions out.
    """

    arguments = parse(argv)
    if arguments.version:
        print(f"encore-builder {BUILDER_VERSION}")
        return EXIT_OK

    try:
        options, config = _options(arguments)
    except ConfigurationError as error:
        print(str(error), file=sys.stderr)
        return EXIT_USAGE
    except (OSError, ValueError) as error:
        print(f"cannot start the build: {error}", file=sys.stderr)
        return EXIT_ABORTED

    LoggingService(config.logging).configure()
    limits = ArtworkLimits(max_assets=max(0, arguments.max_artwork))
    try:
        report = run(_with(options, limits), bus=None)
    except (OSError, ValueError) as error:
        print(f"build aborted: {error}\nThe previous library is unchanged.", file=sys.stderr)
        return EXIT_ABORTED

    print(report.json() if arguments.json else report.text())
    if report.scanned == 0:
        # Nothing to build is not a build that failed validation: there was no library
        # to validate. The report's error line says which root was empty, and a cron
        # wrapper needs a code that distinguishes "fix the mount" from "fix the tags".
        return EXIT_ABORTED
    if report.published_to is not None:
        return EXIT_OK
    if arguments.dry_run and report.validated:
        return EXIT_OK
    return EXIT_NOT_PUBLISHED if report.validated or report.songs else EXIT_ABORTED


def _options(arguments: argparse.Namespace) -> tuple[BuildOptions, EncoreConfig]:
    """Turn parsed arguments plus configuration into one `BuildOptions`."""

    config = ConfigurationService(arguments.config).config
    paths = config.paths
    overrides = {
        "music_dir": arguments.music_dir,
        "library_db": arguments.library,
        "artwork_dir": arguments.artwork_dir,
        "temp_dir": arguments.temp_dir,
    }
    chosen = {
        name: (value if value is not None else getattr(paths, name))
        for name, value in overrides.items()
    }
    return (
        BuildOptions(
            # Absolute, whatever the operator typed. `library.db` stores the paths it
            # finds on disk and validation (SAPRS 6.10) rejects relative ones, so a
            # `--music-dir ./music` — the shape of every example a human would copy —
            # would otherwise build a library that refuses to publish. systemd gives the
            # service no working directory, so a relative path is meaningless at runtime
            # even when it would resolve here.
            music_dir=Path(chosen["music_dir"]).resolve(),
            library_db=Path(chosen["library_db"]).resolve(),
            artwork_dir=Path(chosen["artwork_dir"]).resolve(),
            temp_dir=Path(chosen["temp_dir"]).resolve(),
            incremental=not arguments.full,
            enrich=bool(arguments.musicbrainz),
            publish=not arguments.dry_run,
        ),
        config,
    )


def _with(options: BuildOptions, limits: ArtworkLimits) -> BuildOptions:
    """Attach the artwork limits the command line set, without a mutable argument."""

    from dataclasses import replace

    return replace(options, artwork_limits=limits)


if __name__ == "__main__":  # pragma: no cover - exercised through scripts/run-builder.sh
    sys.exit(main(sys.argv[1:]))
