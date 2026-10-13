"""`encore-install` — the command line, and nothing else (SAPRS 13.7, AIG 21 step 15).

Four jobs, in this order: read the arguments, look at the machine, ask `plan.py` what to do,
and tell `report.py` what happened. The refusal logic lives here too, because "no" is a
decision and a decision belongs where the flags are.

There is no policy in this file on purpose. Which of a `useradd` and a `daemon-reload` comes
first, and which actions a running service suppresses, are the interesting parts of an
installer, and they are worth being able to read in one place (`plan.py`) and to test without a
root shell. An entry point that held the rules would have to be *run* to be understood, and
there is no CI runner that is a Raspberry Pi.

Exit codes, because a provisioning script reads them and an operator does not:

| Code | Meaning |
| ---- | ------- |
| 0 | done, or already done — 13.7's idempotence is a success, not a no-op |
| 2 | usage, or a destructive command run without the confirmation 13.8 asks for |
| 3 | a prerequisite is unmet (13.1, 13.7): the machine is not ready, nothing was changed |
| 4 | the change happened and the acceptance list does not pass (13.11) — read the checks |

3 and 4 are separated because they are answered differently. 3 is "fix the machine and run this
again"; 4 is "the machine is fine and something about Encore is not".
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from apps.installer.checks import Check, Verdict, assess, blocked
from apps.installer.execute import Execution, Executor
from apps.installer.layout import Layout
from apps.installer.options import Command, Options, Settings
from apps.installer.plan import Plan, checkout, plan_for
from apps.installer.report import Report
from apps.installer.system import ApplianceState, HttpProbe, Machine, RealMachine, observe
from encore.config.models import EncoreConfig
from encore.config.service import ConfigurationError, ConfigurationService
from encore.services.logging_service import LoggingService

__all__ = ["EXIT_OK", "EXIT_PREREQUISITES", "EXIT_USAGE", "EXIT_VERIFICATION", "main", "parse"]

EXIT_OK: Final = 0
EXIT_USAGE: Final = 2
EXIT_PREREQUISITES: Final = 3
EXIT_VERIFICATION: Final = 4

#: The environment variable SAPRS 12.6 names for the credential. Read in exactly one place,
#: which is this file, and only to hand it to the appliance.
PASSWORD_VARIABLE: Final = "ENCORE_ADMIN_PASSWORD"  # noqa: S105 - a variable name, not a value

#: The highest address a TCP port can have.
MAX_PORT: Final = 65535

VERSION = "0.1.0"


def parse(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """The flags. Few, because the settings live in the configuration file (SAPRS 12.2)."""

    parser = argparse.ArgumentParser(
        prog="encore-install",
        description=(
            "Install, upgrade, verify or remove the Encore jukebox appliance (SAPRS Chapter 13). "
            "Run as root on the machine that will play the music."
        ),
        epilog=(
            "Idempotent (SAPRS 13.7) and non-destructive by default (13.8): an existing\n"
            "config.yaml, library.db, runtime.db and the artwork cache are never overwritten.\n"
            "The manual steps in docs/Administrator-Guide.md are authoritative (13.2); this\n"
            "automates them and prints the same commands."
        ),
    )
    parser.add_argument(
        "command", nargs="?", default=Command.INSTALL.value, choices=[c.value for c in Command]
    )
    parser.add_argument("--version", action="version", version=f"encore-install {VERSION}")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path("/"),
        metavar="DIR",
        help="build the whole appliance under DIR instead of / (a rehearsal, or a self-contained tree)",
    )
    parser.add_argument("--user", default="encore", help="service account (SAPRS 13.5)")
    parser.add_argument("--group", default="encore", help="service group")
    parser.add_argument("--host", default=None, help="server.host to write")
    parser.add_argument("--port", type=int, default=None, help="server.port to write")
    parser.add_argument("--hostname", default=None, help="server.hostname, the mDNS name")
    parser.add_argument("--domain", default=None, help="server.domain, the name guests resolve")
    parser.add_argument("--music-dir", type=Path, default=None, help="paths.music_dir")
    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        default=None,
        dest="config_file",
        help="an existing config.yaml to read instead of generating one (SAPRS 13.8)",
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=None,
        help="the checkout to install from (default: the one this command is running out of)",
    )
    parser.add_argument(
        "--log-format",
        default=None,
        choices=("json", "console"),
        help="logging.format to write; json is right for the journal",
    )
    parser.add_argument("--no-service", action="store_true", help="skip systemd entirely")
    parser.add_argument(
        "--offline", action="store_true", help="resolve dependencies from cache only"
    )
    parser.add_argument(
        "-n", "--dry-run", action="store_true", help="print the plan, change nothing"
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="do not ask; required for anything that deletes (SAPRS 13.8)",
    )
    parser.add_argument("--purge-config", action="store_true", help="with remove: /etc/encore too")
    parser.add_argument(
        "--purge-data", action="store_true", help="with remove: the databases, artwork and logs"
    )
    parser.add_argument(
        "--json", action="store_true", dest="as_json", help="the same report, as data"
    )
    return parser.parse_args(argv)


def main(  # noqa: PLR0911 - one return per exit code
    argv: Sequence[str] | None = None,
    *,
    machine: Machine | None = None,
    http: HttpProbe | None = None,
) -> int:
    """Run one command. Never raises for a machine it does not like.

    Args:
        argv: The arguments, or `sys.argv[1:]` when nobody handed any.
        machine: The host to install onto. Production passes nothing and gets the real one;
            the integration suite passes a `FakeMachine`, which is the same filesystem with
            scripted answers about users, systemd and ports. The seam is here rather than in a
            module-level factory because the alternative is for a test to rebuild this function's
            nine steps, and a rebuilt pipeline is a pipeline that stops being tested.
        http: The probe `verify` uses to ask a running appliance how it is. Separate from
            `machine` for the reason `HttpProbe` gives: it is the one observation that needs the
            service to be up, so a test of the acceptance list has to be able to say "the
            appliance said it was degraded" without building an appliance.
    """

    arguments = parse(argv)
    try:
        options = _options(arguments)
    except ValueError as error:
        print(f"encore-install: {error}", file=sys.stderr)
        return EXIT_USAGE

    if (refusal := _decline(options)) is not None:
        print(f"encore-install: {refusal}", file=sys.stderr)
        return EXIT_USAGE

    host = machine if machine is not None else RealMachine()
    runner = host.commands
    try:
        config = _configuration(options, host)
    except ConfigurationError as error:
        # Before LoggingService, for the reason `apps/server/main.py` gives: the logging
        # section is inside the file that just failed.
        print(f"encore-install: {error}", file=sys.stderr)
        return EXIT_PREREQUISITES

    LoggingService(config.logging).configure()
    log = logging.getLogger("encore.installer")
    log.info(
        "installer started",
        extra={
            "command": options.command.value,
            "root": str(options.root),
            "domain": config.server.domain,
            "dry_run": options.dry_run,
        },
    )

    state = _observe(host, options, config, http=http)
    checks = _assess(options, config, state)

    if options.command is Command.VERIFY:
        return _finish(
            options, Plan(command=options.command), config, checks, code=_exit_for(checks)
        )

    if blocked(checks):
        return _finish(
            options,
            Plan(command=options.command),
            config,
            checks,
            code=EXIT_PREREQUISITES,
            notes=("A prerequisite is unmet, so nothing was changed (SAPRS 13.7).",),
        )

    plan = plan_for(options, state, config, requested=_requested(options, host))
    if options.dry_run:
        return _finish(options, plan, config, checks, code=EXIT_OK)

    execution = Executor(runner, layout=options.layout).apply(plan.actions)
    after = _assess(options, config, _observe(host, options, config, http=http))
    code = EXIT_OK if execution.succeeded else EXIT_PREREQUISITES
    if code is EXIT_OK and options.command in {Command.INSTALL, Command.UPGRADE}:
        code = _exit_for(after)
    return _finish(options, plan, config, after, execution=execution, code=code)


# -- the request ---------------------------------------------------------


def _options(arguments: argparse.Namespace) -> Options:
    """Arguments into one immutable request, with the values checked before a machine is."""

    settings = Settings(
        host=arguments.host or Settings().host,
        port=arguments.port if arguments.port is not None else Settings().port,
        hostname=arguments.hostname or Settings().hostname,
        domain=arguments.domain or Settings().domain,
        music_dir=arguments.music_dir or Settings().music_dir,
        log_format=arguments.log_format or Settings().log_format,
    )
    if not 1 <= settings.port <= MAX_PORT:
        raise ValueError(f"--port {settings.port} is not a port")
    if not settings.domain.strip():
        raise ValueError("--domain cannot be empty; it is the address the QR code will say")
    if arguments.purge_config and arguments.command != Command.REMOVE.value:
        raise ValueError("--purge-config only means something with `remove`")
    if arguments.purge_data and arguments.command != Command.REMOVE.value:
        raise ValueError("--purge-data only means something with `remove`")
    return Options(
        command=Command(arguments.command),
        layout=Layout(root=arguments.root, user=arguments.user, group=arguments.group),
        source=arguments.source,
        config_path=arguments.config_file,
        settings=settings,
        service=not arguments.no_service,
        dry_run=arguments.dry_run,
        assume_yes=arguments.yes,
        offline=arguments.offline,
        purge_config=arguments.purge_config,
        purge_data=arguments.purge_data,
        as_json=arguments.as_json,
        admin_password=_seed_password(),
    )


def _seed_password() -> str | None:
    """The credential to seed, from the environment and from nowhere else.

    This is the one line in the installer that knows where a secret came from, and there is one
    acceptable answer: the environment of the process root started (SAPRS 12.6). A
    `--admin-password` flag would put the appliance's only credential in every user's `ps`
    output for the lifetime of the process and in somebody's shell history, which is a worse
    version of the thing 12.6 exists to prevent — so the CLI does not offer one, and an empty
    value is treated as "not supplied" rather than as "seed the empty string".
    """

    value = os.environ.get(PASSWORD_VARIABLE, "")
    return value or None


def _decline(options: Options) -> str | None:
    """Refuse, in one sentence, anything 13.8 says needs an explicit act.

    `remove` and `reset-credential` are the two, and `--purge-data` inside a `remove` is the one
    nothing can undo. An interactive prompt is deliberately not the answer: a provisioning system
    has no terminal to answer with, and a program that accepts "yes" from a pipe is a program one
    `yes | encore-install remove --purge-data` away from a destroyed library index.

    A `--dry-run` is not the act 13.10 is guarding, so it is not refused. Describing a deletion is
    how an operator finds out what a removal would cost them before they are the one paying for
    it, and a rule that blocks the rehearsal would only teach people to run the real thing to see
    what it says.
    """

    if options.assume_yes or options.dry_run:
        return None
    if options.command is Command.REMOVE:
        scope = ["the application"]
        if options.purge_config:
            scope.append("the configuration")
        if options.purge_data:
            scope.append("the databases, the artwork and the logs")
        return (
            f"removing {' and '.join(scope)} deletes files this program cannot reconstruct. "
            "Re-run with --yes when you meant it (SAPRS 13.10)."
        )
    if options.command is Command.RESET_CREDENTIAL:
        return (
            "reset-credential locks out whoever can sign in right now. Re-run with --yes; it "
            "changes nothing about the music (ADR-013)."
        )
    return None


# -- the truth -----------------------------------------------------------


def _configuration(options: Options, machine: Machine) -> EncoreConfig:
    """The settings this run will write, validate or preserve — one of the three.

    A file that already exists wins over the flags, because SAPRS 13.8 makes preservation the
    default and a flag that quietly rewrote an operator's edited configuration would be a bug
    wearing a feature. What the two disagree about is reported by the `PreserveConfig` action,
    and the URL printed at the end is the file's, because that is the appliance that is running.

    Raises:
        ConfigurationError: A named file cannot be read, or a generated configuration fails the
            validators the Server would have applied.
    """

    if options.config_path is not None:
        return ConfigurationService(options.config_path).load()

    staged = options.layout.staged(options.layout.config_file)
    if staged.is_file():
        try:
            return ConfigurationService(staged).load()
        except ConfigurationError:
            # Unusable, and still not ours to overwrite. `checks.py` turns this into a named
            # failure with the offending settings, and the plan changes nothing; replacing a
            # file nobody can read would destroy the evidence and the operator's intent at the
            # same moment.
            return _generated(options, machine)
    return _generated(options, machine)


def _requested(options: Options, machine: Machine) -> EncoreConfig:
    """What the flags asked for, as a configuration — the other half of 13.8's report.

    Separate from `_configuration` because the two are different questions. "What will this
    appliance run?" is answered by the file that is already there; "what did this command mean?"
    is answered by the command line. Only the second one can tell an operator that the flag they
    typed was not applied, and an installer that quietly ignores a flag is worse than one that
    refuses it.
    """

    if options.config_path is not None:
        # An adopted file *is* the request; there is no flag behind it to disagree with.
        return ConfigurationService(options.config_path).load()
    return _generated(options, machine)


def _generated(options: Options, machine: Machine) -> EncoreConfig:
    """A configuration from the flags, the defaults, and what this machine turns out to have.

    Two values are decided by looking rather than by asking, because an operator cannot be
    expected to know them and both are silent failures when wrong: where mpv actually is, and
    whether the box is a PipeWire machine or an ALSA one (SAPRS 7.7 leaves both to the
    installer, and the installer is here).
    """

    mpv = machine.which("mpv") or Path("/usr/bin/mpv")
    output = "pipewire" if machine.which("pipewire") is not None else "alsa"
    return options.settings.config(audio_output=output, mpv_path=mpv)


def _observe(
    machine: Machine, options: Options, config: EncoreConfig, *, http: HttpProbe | None = None
) -> ApplianceState:
    return observe(
        machine,
        http=http,
        layout=options.layout,
        bind_host=config.server.host,
        bind_port=config.server.port,
        music_dir=options.layout.staged(str(config.paths.music_dir)),
        source=options.source or checkout(),
    )


def _assess(options: Options, config: EncoreConfig, state: ApplianceState) -> tuple[Check, ...]:
    return assess(
        state,
        options,
        config=config,
        library_db=options.layout.staged(str(config.paths.library_db)),
        runtime_db=options.layout.staged(str(config.paths.runtime_db)),
    )


# -- the answer ----------------------------------------------------------


def _exit_for(checks: Sequence[Check]) -> int:
    """13.11 as an exit code: a failing check is a failure, a warning is not.

    Warnings leave the code alone on purpose. An appliance with no library is a correctly
    installed appliance with work left to do — 13.7 lists "report next steps" as a step — and an
    exit code that conflated the two would make every provisioning system's first install look
    broken, which is how `set -e` ends up around an installer and how installers get skipped.
    """

    return EXIT_VERIFICATION if any(check.blocking for check in checks) else EXIT_OK


def _finish(
    options: Options,
    plan: Plan,
    config: EncoreConfig,
    checks: Sequence[Check],
    *,
    execution: Execution | None = None,
    code: int = EXIT_OK,
    notes: tuple[str, ...] = (),
) -> int:
    done = execution if execution is not None else Execution()
    report = Report(
        options=options,
        plan=plan,
        execution=done,
        checks=tuple(checks),
        notes=notes + plan.notes,
        exit_code=code,
    )
    print(report.json(config=config) if options.as_json else report.text(config=config))
    for check in checks:
        if check.verdict is Verdict.FAIL and check.fix:
            print(f"         → {check.fix}", file=sys.stderr)
    return report.exit_code


if __name__ == "__main__":  # pragma: no cover - the console script's entry point
    sys.exit(main(sys.argv[1:]))
