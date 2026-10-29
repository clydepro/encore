"""What is true about this machine, before anything is changed (SAPRS 13.1, 13.7, 13.11).

`encore-install`'s first duty is to refuse well. SAPRS 13.7 puts "verify prerequisites" ahead
of "create required directories" for the reason every installer learns late: a run that
discovers at step six that the machine has no `mpv` has left a half-staged install and a unit
that will not start, and the cleanup is the operator's rather than ours.

Every check returns a `Check` — a name, a verdict, one line of evidence, and the command that
would change the machine so the verdict were different. That last field is the reason this is
a module rather than a list of `if` statements in `main.py`: a prerequisite failure that does
not say what to do about it transfers the entire cost of understanding this repository to
whoever is standing in a room with a phone in their hand.

Nothing here decides what to do — that is `plan.py`. Nothing here mutates anything. The one
process started is `systemctl show`, and the one thing that reads a database does it in
read-only mode (`config_file`'s companion notes explain why that is not optional).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Final

from apps.installer.config_file import configuration_problems
from apps.installer.options import Command, Options
from apps.installer.system import ApplianceState

if TYPE_CHECKING:
    from encore.config.models import EncoreConfig

__all__ = ["Check", "Checks", "Verdict", "assess"]


class Verdict(StrEnum):
    """Whether a fact is a problem, and how big one.

    `WARN` is the level that lets an installer be honest about a machine it does not
    recognise. SAPRS 13.1 names four platforms and adds "where practical" for the rest, and a
    program that treated an unlisted kernel as a failure would be refusing to install
    something that works.
    """

    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True, slots=True, kw_only=True)
class Check:
    """One answer to one question.

    Attributes:
        name: What was asked, in the words an operator would use to look for it.
        verdict: The answer.
        detail: One line of evidence — a count, a path, a version. Never an adjective.
        fix: The command or file to change, when there is one. Empty means "nothing to do".
    """

    name: str
    verdict: Verdict
    detail: str
    fix: str = ""

    @property
    def blocking(self) -> bool:
        return self.verdict is Verdict.FAIL

    def line(self) -> str:
        mark = {Verdict.OK: "ok  ", Verdict.WARN: "warn", Verdict.FAIL: "FAIL"}[self.verdict]
        text = f"  {mark}  {self.name}: {self.detail}"
        return f"{text}\n         → {self.fix}" if self.fix else text


#: A name and a verdict, so the module reads as a list of questions.
Checks = tuple[Check, ...]

#: Below this an install fails in the middle of `uv sync` rather than before it. 2 GiB covers
#: the environment plus a library and its artwork cache, and is a warning rather than a
#: refusal because what a full disk is depends on what else is on it.
MINIMUM_FREE_BYTES: Final = 2 * 1024 * 1024 * 1024


def assess(
    state: ApplianceState,
    options: Options,
    *,
    config: EncoreConfig,
    library_db: Path,
    runtime_db: Path,
) -> Checks:
    """Everything SAPRS 13.7's first bullet and 13.11's list ask for, in the order it matters.

    Args:
        state: What `observe()` found.
        options: What was asked for — including the staging root, which changes the meaning
            of "you are not root" from a refusal to a remark.
        config: The configuration in force, for the bind address and the music directory.
        library_db: The staged `paths.library_db`, which is where the *appliance* will look.
        runtime_db: The staged `paths.runtime_db`.
    """

    return (
        _privilege(state, options),
        _platform(state),
        _service_manager(state, options),
        _binaries(state, options),
        _environment(state, options),
        _service_user(state, options),
        _port(state, config),
        _disk(state, options),
        _music(state, options, config),
        _configuration(state, options),
        _library(options, library_db),
        _credential(runtime_db),
        *_service(state, options),
        *_health(state, options),
    )


def blocked(checks: Checks) -> bool:
    """Whether any finding makes the requested change unsafe to attempt."""

    return any(check.blocking for check in checks)


def _privilege(state: ApplianceState, options: Options) -> Check:
    if state.root:
        return Check(name="privilege", verdict=Verdict.OK, detail="running as root")
    if options.staged or options.dry_run:
        return Check(
            name="privilege",
            verdict=Verdict.OK,
            detail="not root, and nothing here has to be",
        )
    return Check(
        name="privilege",
        verdict=Verdict.FAIL,
        detail="not running as root",
        fix=f"sudo encore-install {options.command.value}",
    )


def _platform(state: ApplianceState) -> Check:
    release = state.os_release
    if release.supported:
        return Check(name="platform", verdict=Verdict.OK, detail=release.describe())
    return Check(
        name="platform",
        # 13.1's "where practical": an unlisted machine is one nobody has measured, not one
        # that cannot work, and refusing it would be a claim about our own testing.
        verdict=Verdict.WARN,
        detail=f"{release.describe()} is not a platform SAPRS 13.1 names",
        fix="Fine if it is a systemd Linux with mpv; the manual steps are identical.",
    )


def _service_manager(state: ApplianceState, options: Options) -> Check:
    """13.6's prerequisite, and only that: does this machine have a service manager?

    The unit's own state is a different question, asked by `_service`, and the first install on a
    healthy Pi answers "no unit" and "systemd is running" in the same instant. Conflating them is
    #43: the check decided a machine was not installable because the thing `install` was there to
    do had not been done yet, and its advice — `--no-service` — was the one answer that leaves an
    appliance that never comes back after a power cut.
    """

    if not options.service:
        return Check(name="service manager", verdict=Verdict.OK, detail="not needed (--no-service)")
    if state.systemd_running:
        return Check(name="service manager", verdict=Verdict.OK, detail="systemd is running")
    return Check(
        name="service manager",
        verdict=Verdict.FAIL,
        detail=state.service.detail or "systemd is not running on this machine",
        fix="Install with --no-service and start the server by hand (SAPRS 13.2): "
        "scripts/run-server.sh, or set the Environment= lines in the unit file.",
    )


def _binaries(state: ApplianceState, options: Options) -> Check:
    missing = sorted(name for name, path in state.binaries.items() if path is None)
    if not missing:
        found = ", ".join(f"{name} at {path}" for name, path in sorted(state.binaries.items()))
        return Check(name="binaries", verdict=Verdict.OK, detail=found)
    # The detail names the directories searched, which is the difference between an operator
    # installing a package and an operator wasting an evening on one they already have. #38 was
    # this message's fault as much as the resolution's: "not found", on a machine where uv was
    # two directories away and root had never been told about them.
    detail = f"not found on this machine: {', '.join(missing)}"
    if state.search_path:
        detail = f"{detail} (looked in {state.search_path})"
    # A missing binary stops an install, and stops nothing else. `verify` is asked about an
    # appliance that is already running: an absent `uv` there is a note about the next upgrade,
    # and failing a healthy party over it would be the checker insulting the checked.
    if options.dry_run or options.command is Command.VERIFY:
        return Check(name="binaries", verdict=Verdict.WARN, detail=detail)
    fixes = {
        "mpv": "sudo apt-get install -y mpv  (SAPRS 7.7, ADR-005)",
        # Installs into `~/.local/bin`, which the search path covers for both the installing
        # user and, through `SUDO_USER`, the one who ran sudo (#38).
        "uv": "curl -LsSf https://astral.sh/uv/install.sh | sh  (AIG 3), as the user who will "
        "run the installer — then sudo -E, or add that ~/.local/bin to root's PATH",
    }
    return Check(
        name="binaries",
        verdict=Verdict.FAIL,
        detail=detail,
        fix=fixes.get(missing[0], ""),
    )


def _environment(state: ApplianceState, options: Options) -> Check:
    """13.4 as an acceptance item: an environment that exists and cannot start is not one.

    Checked separately from the lockfile comparison because the two fail differently. A stamp that
    disagrees with the lockfile means a sync was skipped, and an upgrade fixes that by itself; a
    venv with no `encore-server` in it means the sync ran and produced nothing, which the
    appliance will not notice until somebody starts it and systemd reports a success that lasted
    forty milliseconds.

    And a third way it fails, which the mode bits cannot see (#46): the entry point is there and
    executable, and the *interpreter its shebang names* is somewhere the account in `User=` cannot
    walk to. Debian 12 ships Python 3.11 and the project needs 3.12, so `uv` downloads one into
    whoever ran the install — root, whose `/root` is `drwx------`. systemd's answer is
    `status=203/EXEC`, and every bit check on the way there says the file is fine.
    """

    venv = options.layout.staged(options.layout.venv)
    if state.interpreter_blocker is not None:
        account = options.layout.user
        # Whether this is a prerequisite depends on who is being asked to fix it, which is the
        # distinction #43 taught and #46 re-tests: an unreachable interpreter is a fact about the
        # appliance, and `install`/`upgrade` are the commands that change it. Blocking on it would
        # make the installer refuse to repair the exact machine it broke — "a prerequisite is
        # unmet, so nothing was changed" printed above an appliance that needed precisely that run.
        # For `verify` and `remove` nothing downstream can help, so those get the failure.
        repairs = options.command in {Command.INSTALL, Command.UPGRADE}
        return Check(
            name="environment",
            verdict=Verdict.WARN if repairs else Verdict.FAIL,
            detail=f"{account} cannot reach {state.interpreter}, so {venv}/bin/encore-server "
            f"cannot be executed by the service: {state.interpreter_blocker} does not let "
            f"{account} through" + (" — this run will rebuild the environment" if repairs else ""),
            fix=f"sudo encore-install install  # puts the downloaded interpreter in "
            f"{options.layout.staged(options.layout.python)} (SAPRS 13.4, #46)",
        )
    if state.environment_runnable:
        return Check(
            name="environment", verdict=Verdict.OK, detail=f"{venv}/bin/encore-server is there"
        )
    if not state.present.get(options.layout.application, False):
        return Check(
            name="environment",
            verdict=Verdict.WARN,
            detail=f"{venv} is not built yet",
            fix="sudo encore-install install (SAPRS 13.4)",
        )
    return Check(
        name="environment",
        verdict=Verdict.FAIL,
        detail=f"{venv} has no encore-server in it",
        fix="sudo encore-install upgrade  # re-runs `uv sync` against the lockfile (13.4)",
    )


def _service_user(state: ApplianceState, options: Options) -> Check:
    """13.5 as three questions, not one.

    "Does `encore` exist" is the question an installer can answer by creating it. The other two —
    can it log in, and is its home the state directory — are only asked of an account that was
    already there, which is exactly the case where somebody made it by hand and this program is
    about to rely on it.
    """

    user = options.layout.user
    account = state.service_user
    if account is not None:
        if account.can_log_in:
            return Check(
                name="service user",
                verdict=Verdict.FAIL,
                detail=f"{user} has the login shell {account.shell}",
                fix=f"sudo usermod --shell /usr/sbin/nologin {user} (SAPRS 13.5)",
            )
        if options.layout.audio_group and options.layout.audio_group not in account.groups:
            # The difference between an appliance that reports healthy and one that is quiet.
            return Check(
                name="service user",
                verdict=Verdict.WARN,
                detail=f"{user} is not in {options.layout.audio_group}",
                fix=f"sudo usermod -aG {options.layout.audio_group} {user}, then restart Encore",
            )
        return Check(
            name="service user",
            verdict=Verdict.OK,
            detail=f"{user} exists, cannot log in, owns nothing it should not",
            fix="Existing files are left alone; the account is not modified (SAPRS 13.5).",
        )
    if state.user_exists:
        return Check(name="service user", verdict=Verdict.OK, detail=f"{user} exists")
    if options.staged:
        return Check(
            name="service user",
            verdict=Verdict.WARN,
            detail=f"{user} does not exist, and a staged install cannot create it",
            fix="On the real machine this creates it with useradd --system (13.5).",
        )
    return Check(
        name="service user", verdict=Verdict.OK, detail=f"{user} does not exist yet (13.7)"
    )


def _port(state: ApplianceState, config: EncoreConfig) -> Check:
    address = f"{config.server.host}:{config.server.port}"
    if state.port_free:
        return Check(name="port", verdict=Verdict.OK, detail=f"{address} is free")
    if state.service.active:
        return Check(
            name="port",
            verdict=Verdict.OK,
            detail=f"{address} is held by the appliance itself",
            fix="An upgrade stops it first (SAPRS 13.9).",
        )
    return Check(
        name="port",
        verdict=Verdict.FAIL,
        detail=f"{address} is taken by something else",
        fix="Install with --port 8081, or stop whatever answers it (SAPRS 12.2).",
    )


def _disk(state: ApplianceState, options: Options) -> Check:
    where = options.layout.staged(options.layout.state)
    free = f"{state.disk_free / 1e9:.1f} GB free"
    if state.disk_free >= MINIMUM_FREE_BYTES or options.staged or options.dry_run:
        return Check(name="disk", verdict=Verdict.OK, detail=f"{free} at {where}")
    return Check(
        name="disk",
        verdict=Verdict.WARN,
        detail=f"{free} at {where}",
        fix="The environment needs about 150 MB before artwork and a library are considered.",
    )


def _music(state: ApplianceState, options: Options, config: EncoreConfig) -> Check:
    path = options.layout.staged(str(config.paths.music_dir))
    if state.music_readable is None:
        return Check(name="music", verdict=Verdict.OK, detail=f"{path} is the Builder's problem")
    if state.music_readable:
        return Check(name="music", verdict=Verdict.OK, detail=f"{path} exists")
    return Check(
        name="music",
        # A warning rather than a failure, because the Server never scans this directory
        # (ADR-010): an appliance can be installed, started and entirely healthy with the
        # USB disk in a drawer. What it cannot do is be *built* with it in a drawer.
        verdict=Verdict.WARN,
        detail=f"{path} is not there for this user to read",
        fix="Mount it before running encore-builder; the Server does not need it (ADR-010).",
    )


def _configuration(state: ApplianceState, options: Options) -> Check:
    path = options.layout.staged(options.layout.config_file)
    if state.config_text is None:
        return Check(
            name="configuration",
            verdict=Verdict.OK,
            detail=f"{path} does not exist yet; one will be rendered",
        )
    problems = configuration_problems(state.config_text)
    if not problems:
        return Check(
            name="configuration",
            verdict=Verdict.OK,
            detail=f"{path} parses and validates",
            fix="Preserved. The installer never overwrites an existing configuration (13.8).",
        )
    return Check(
        name="configuration",
        verdict=Verdict.FAIL,
        detail="; ".join(problems[:3]),
        fix=f"Correct {path}; this program will not overwrite it (SAPRS 12.5, 13.8).",
    )


def _library(options: Options, library_db: Path) -> Check:
    if not library_db.is_file():
        return Check(
            name="library",
            # The warning an operator should not wave away. With no library the appliance
            # starts, serves an empty catalogue and plays nothing, so 13.11's list is
            # satisfied without 13.11's outcome being anything a party would notice.
            verdict=Verdict.WARN,
            detail=f"{library_db} is not there yet",
            fix=f"{options.layout.staged(options.layout.venv)}/bin/encore-builder"
            "  # builds and publishes it (ADR-010)",
        )
    read = _read_library(library_db)
    if read is None:
        return Check(
            name="library",
            verdict=Verdict.FAIL,
            detail=f"{library_db} is not a readable Encore library",
            fix="Publish one with encore-builder, or restore it from the build machine (ADR-006).",
        )
    songs, indexed = read
    return Check(
        name="library",
        verdict=Verdict.OK if indexed else Verdict.WARN,
        detail=f"{songs:,} songs" + (", no FTS index" if not indexed else ""),
        fix=""
        if indexed
        else "Rebuild the library; search falls back to LIKE, slowly (SAPRS 11.2).",
    )


def _credential(runtime_db: Path) -> Check:
    if not runtime_db.is_file():
        return Check(
            name="administrator",
            verdict=Verdict.WARN,
            detail="runtime.db does not exist yet; the first boot creates it",
            fix="Set a password at /admin once, or seed ENCORE_ADMIN_PASSWORD (SAPRS 12.6).",
        )
    configured = _has_administrator(runtime_db)
    if configured is None:
        return Check(
            name="administrator",
            verdict=Verdict.WARN,
            detail=f"{runtime_db} could not be read without writing to it",
            fix="Stop the service and run verify, or ask it: curl localhost/api/v1/health",
        )
    if configured:
        return Check(name="administrator", verdict=Verdict.OK, detail="a credential is set")
    return Check(
        name="administrator",
        verdict=Verdict.WARN,
        detail="no administrator yet; /admin will ask for one",
        fix="ADR-015: ENCORE_ADMIN_PASSWORD seeds it on the first boot, once.",
    )


def _service(state: ApplianceState, options: Options) -> tuple[Check, ...]:
    if not options.service:
        return ()
    service = state.service
    if not state.systemd_running:
        # The three lines below are a machine where the unit is missing; this is a machine where
        # there is nowhere for a unit to be, and saying "run the installer" about a container with
        # no PID 1 to register with is the circular advice #43's report was.
        return (
            Check(
                name="unit",
                verdict=Verdict.WARN,
                detail="there is no systemd here to install a unit into",
                fix="Install with --no-service (SAPRS 13.2); the unit file is still written.",
            ),
        )
    return (
        Check(
            name="unit",
            verdict=Verdict.OK if service.installed else Verdict.WARN,
            detail="systemd knows about encore.service"
            if service.installed
            else "encore.service is not installed",
            fix="" if service.installed else "sudo encore-install install",
        ),
        Check(
            name="at boot",
            verdict=Verdict.OK if service.enabled else Verdict.WARN,
            detail="enabled" if service.enabled else "not enabled (SAPRS 13.6)",
            fix="" if service.enabled else "sudo systemctl enable encore",
        ),
        Check(
            name="running",
            verdict=Verdict.OK if service.active else Verdict.WARN,
            detail="active" if service.active else "not running",
            fix="" if service.active else "journalctl -u encore --no-pager -n 40",
        ),
    )


def _health(state: ApplianceState, options: Options) -> tuple[Check, ...]:
    health = state.health
    if health is None:
        if not state.service.active or options.dry_run:
            return ()
        return (
            Check(
                name="health",
                verdict=Verdict.FAIL,
                detail="the appliance is running and did not answer /api/v1/health",
                fix="journalctl -u encore --since '-5 min'",
            ),
        )
    overall = str(health.get("status", "unknown"))
    parts = [
        Check(
            name="health",
            # `unavailable` is a failure and `degraded` is not: the first means a component
            # stopped answering, the second means one is answering badly. An appliance with a
            # dead queue thread cannot play anything, which is 13.11's "functional playback"
            # being false; an appliance whose library is momentarily busy still is not.
            verdict=(
                Verdict.OK
                if overall == "healthy"
                else Verdict.FAIL
                if overall == "unavailable"
                else Verdict.WARN
            ),
            detail=f"the appliance reports {overall}",
            fix="" if overall == "healthy" else "journalctl -u encore --since '-10 min'",
        )
    ]
    components = health.get("components")
    if isinstance(components, list):
        for component in components:
            if not isinstance(component, dict):
                continue
            status = str(component.get("status", ""))
            if status == "healthy":
                continue
            parts.append(
                Check(
                    name=f"health: {component.get('component', '?')}",
                    verdict=Verdict.WARN,
                    detail=str(component.get("detail") or status),
                )
            )
    return tuple(parts)


def _read_library(path: Path) -> tuple[int, bool] | None:
    """Songs, and whether they can be searched — from a database this process will not write.

    `mode=ro` is the point. SAPRS 12.3 and ADR-006 say the runtime never modifies
    `library.db`; an installer that opened it for a health check and left a root-owned `-wal`
    behind would be the thing that breaks the next boot. A file in a state this query cannot
    survive answers None, and the caller says so instead of guessing.
    """

    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
            songs = connection.execute("SELECT COUNT(*) FROM songs").fetchone()[0]
            indexed = connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='song_search'"
            ).fetchone()[0]
    except (sqlite3.Error, IndexError):
        return None
    return int(songs), bool(indexed)


def _has_administrator(path: Path) -> bool | None:
    """Whether `admin_state` has a row, read without writing.

    Read-only mode is not a nicety here: `runtime.db` is owned by the service user and is
    opened in WAL mode by the Server, and a root process opening it for a check can leave
    files behind that the appliance then cannot. A locked or absent database answers None —
    "unknown", which is honest — rather than reporting that there is no administrator.
    """

    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
            rows = connection.execute("SELECT COUNT(*) FROM admin_state").fetchone()[0]
    except (sqlite3.Error, IndexError):
        return None
    return bool(rows)
