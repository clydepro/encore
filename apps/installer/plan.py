"""The change, worked out before any of it happens (SAPRS 13.7, 13.8, 13.9, 13.10).

This is the installer's argument, and it is a pure function: what the machine looks like plus
what the operator asked for, in; an ordered list of actions out. Nothing in this module runs a
command, opens a file for writing, or knows what a subprocess is, and that is what buys the
two properties Chapter 13 insists on.

**Idempotence (13.7)** stops being a claim about behaviour and becomes a fact about data: run
`install` against a machine that already has Encore and the plan is empty, which a test can
assert without a root shell. The alternative — actions that check before they act — is how an
installer ends up running `systemctl restart` on a box it changed nothing about, and an
unnecessary restart in the middle of a party is the bug the empty plan cannot have.

**Non-destruction (13.8)** becomes visible rather than distributed. There is no action that
overwrites a configuration file, and the only action that deletes anything is named
`RemovePath` with a path that came from a table of things the installer itself created — which
is why `library.db`, `runtime.db`, the artwork and the logs are not in it, and why SAPRS
13.10's three removal scopes are three flags over that table rather than three functions.

Ordering is in here too, because it is a decision rather than a detail: 13.9's upgrade is
stop → code → dependencies → files → check → start, and the reason the user-data backup sits
before the start is that the migrations run *during* the start and there is no way to undo
them afterwards.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from apps.installer.config_file import Decision, decide
from apps.installer.content import config_text, environment_text, tmpfiles_text, unit_text
from apps.installer.layout import SCOPE_CONFIGURATION, SCOPE_DATA, Layout, ManagedFile
from apps.installer.options import Command, Options
from apps.installer.system import ApplianceState
from encore.config.models import EncoreConfig

__all__ = [
    "Action",
    "BackupRuntimeDatabase",
    "CreateDirectories",
    "CreateServiceUser",
    "DaemonReload",
    "DeletePath",
    "EnableService",
    "EnsureFile",
    "ForgetCredential",
    "InstallApplication",
    "KeepExistingConfig",
    "Plan",
    "Planning",
    "PreserveConfig",
    "RepairPaths",
    "RestartService",
    "StartService",
    "StopService",
    "SyncEnvironment",
    "WriteConfig",
    "plan_for",
]


# -- actions -------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class Action:
    """One mutation, described before it is performed.

    `detail` is the sentence the operator reads, and it is computed by the plan rather than
    by the executor so that `--dry-run` and a real run cannot say different things about the
    same action.
    """

    detail: str

    @property
    def kind(self) -> str:
        return type(self).__name__


@dataclass(frozen=True, slots=True, kw_only=True)
class CreateServiceUser(Action):
    """`useradd --system`, and only when the account is missing (SAPRS 13.5).

    Attributes:
        audio_group: Whether to add the account to `audio`. Decided by the plan from the
            machine, because on a machine with no such group the flag would fail the install
            for a group that does not exist, and on one with it, omitting it means mpv opens
            a device and plays into nowhere.
    """

    user: str
    group: str
    home: Path
    shell: str = "/usr/sbin/nologin"
    audio_group: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class CreateDirectories(Action):
    """Directories that are not there yet, or are not directories."""

    entries: tuple[tuple[Path, str, str, int], ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class RepairPaths(Action):
    """Paths that exist with the wrong owner, group or mode (SAPRS 13.3).

    Reporting the drift and then fixing it is the point: a `library.db` in a directory that
    `encore` cannot read is an appliance that exits at startup, and the operator who chown'd
    it last month deserves to be told rather than silently overruled.
    """

    entries: tuple[tuple[Path, str, str, int], ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class EnsureFile(Action):
    """A rendered file the installer owns, written only when its text would change.

    These are the files an upgrade *does* overwrite — the unit, the environment, the tmpfiles
    rule — and the difference from `config.yaml` is that nothing in them is ever edited by
    hand except through `systemctl edit`, which writes a drop-in this file does not touch.
    """

    path: Path
    text: str
    owner: str
    group: str
    mode: int
    purpose: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class WriteConfig(Action):
    """The first `config.yaml`. There is exactly one action that can create it, and this."""

    path: Path
    text: str
    owner: str
    group: str
    mode: int


@dataclass(frozen=True, slots=True, kw_only=True)
class PreserveConfig(Action):
    """Leave the file alone, and say why in the report (SAPRS 13.8).

    Attributes:
        differences: `(setting, on_disk, asked_for)`, so "your `--domain` was not applied"
            is evidence rather than a hunch.
    """

    path: Path
    reason: str
    differences: tuple[tuple[str, object, object], ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class KeepExistingConfig(Action):
    """Leave a broken file alone and fail. The same rule, in the case where obeying it hurts."""

    path: Path
    problems: tuple[str, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class InstallApplication(Action):
    """Copy the checkout into `application/` (SAPRS 13.9's "update application").

    Refuses to run when source and destination are the same directory, which is the ordinary
    case for an operator who cloned straight into `/opt/encore/application` and then ran
    `encore-install upgrade`: a recursive copy onto itself is a disk full, not an install.
    """

    source: Path
    destination: Path


@dataclass(frozen=True, slots=True, kw_only=True)
class SyncEnvironment(Action):
    """`uv sync --frozen`, in the appliance's own environment (SAPRS 13.4).

    `--frozen` is what makes it reproducible: the lockfile is the answer and nothing resolves
    at install time. `--no-dev` keeps pytest and ruff off an appliance.
    """

    application: Path
    venv: Path
    offline: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class DaemonReload(Action):
    """`systemctl daemon-reload`, after a unit file changed and before anything reads it."""

    detail: str = "tell systemd about the unit file"


@dataclass(frozen=True, slots=True, kw_only=True)
class EnableService(Action):
    """`systemctl enable` — 13.6's "start at boot"."""

    service: str


@dataclass(frozen=True, slots=True, kw_only=True)
class DisableService(Action):
    service: str


@dataclass(frozen=True, slots=True, kw_only=True)
class StartService(Action):
    service: str


@dataclass(frozen=True, slots=True, kw_only=True)
class StopService(Action):
    """13.9's step 1, and the only reason the upgrade path exists in that order."""

    service: str


@dataclass(frozen=True, slots=True, kw_only=True)
class RestartService(Action):
    service: str


@dataclass(frozen=True, slots=True, kw_only=True)
class BackupRuntimeDatabase(Action):
    """A pre-upgrade copy of `runtime.db`, because the migrations do not go backwards.

    SAPRS 13.9's step 3 is "apply required migrations" and the appliance's answer to a failed
    migration is to refuse to start, correctly. What the specification does not say is that a
    forward-only migration of an operator's queue and history has no undo, so the upgrade
    takes one copy before starting anything. It is a copy, not `runtime.db.bak`: see
    `execute.py`, which uses SQLite's backup API in read-only mode rather than `shutil.copy`,
    because a WAL file the Server left behind would make the copy a lie.
    """

    source: Path
    destination: Path


@dataclass(frozen=True, slots=True, kw_only=True)
class DeletePath(Action):
    """Remove something the installer created. Scope is decided by the caller, not here."""

    path: Path
    scope: str  # "application" | "configuration" | "data"


@dataclass(frozen=True, slots=True, kw_only=True)
class ForgetCredential(Action):
    """Clear the administrator row, so the next `/admin` visit asks for a new one.

    The one action in the installer that writes `runtime.db`, and it is narrow on purpose: a
    single `DELETE` of a single row, with the service stopped, at an explicit request. The
    password is not reset to anything and nothing is generated, because a program that
    invents a credential has to tell the operator what it was, and that sentence is a
    security story nobody wants.
    """

    database: Path


# -- plans ---------------------------------------------------------------


class Planning(StrEnum):
    """Why a plan holds the actions it holds.

    `actions` alone cannot say it, because an empty list means three different things: the machine
    already matches the request (13.7's idempotence, which is a result), this command asks questions
    rather than making changes (`verify`), or the run never got as far as looking (a prerequisite
    failed). The report printed all three as the first of those sentences, which is how #43's
    output told an operator their machine was already installed and, four lines later, that a
    prerequisite was unmet.
    """

    PLANNED = "planned"
    NOT_ASKED = "not-asked"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True, kw_only=True)
class Plan:
    """A complete answer to "what will you do?".

    Attributes:
        command: What was asked for.
        actions: In execution order. Empty means the machine already matches the request,
            which is a result rather than an absence of one — unless `planning` says otherwise.
        notes: Anything the report should say that is not an action — a preserved
            configuration, a group that does not exist, a rollback copy that will be made.
        planning: Which of the three kinds of answer this is. Defaulted, because a plan built by
            the planner is `PLANNED` by definition; it is `main.py`'s two early exits that have to
            say which kind of nothing they are reporting.
    """

    command: Command
    actions: tuple[Action, ...] = ()
    notes: tuple[str, ...] = ()
    planning: Planning = Planning.PLANNED

    @property
    def is_empty(self) -> bool:
        return not self.actions

    def describe(self) -> str:
        """The plan as the answer to "what will you do?", one numbered line per action.

        Numbered because an operator reads it against SAPRS 13.7's list, and because a report
        that says "eleven actions performed" without an order is a report nobody can check.
        """

        if self.planning is Planning.BLOCKED:
            return "no plan — a prerequisite is unmet, so the machine was not examined (13.7)"
        if self.planning is Planning.NOT_ASKED:
            return "no plan — this command answers questions rather than changing the machine"
        if not self.actions:
            return "nothing to do — the machine already matches this request (SAPRS 13.7)"
        return "\n".join(
            f"  {index:>2}. {action.detail}" for index, action in enumerate(self.actions, 1)
        )


def plan_for(
    options: Options,
    state: ApplianceState,
    config: EncoreConfig,
    *,
    requested: EncoreConfig | None = None,
) -> Plan:
    """Dispatch on the command, so `main.py` holds no policy at all.

    `requested` is the configuration the *flags* add up to, as opposed to `config`, the one in
    force on the machine. They are the same object whenever there is no `config.yaml` to read, and
    they differ the moment an operator's file disagrees with this invocation's `--port`. Both are
    needed, and `main.py` is the only place that can tell them apart.
    """

    wanted = requested if requested is not None else config
    if options.command is Command.INSTALL:
        return install_plan(options, state, config, requested=wanted)
    if options.command is Command.UPGRADE:
        return upgrade_plan(options, state, config, requested=wanted)
    if options.command is Command.REMOVE:
        return remove_plan(options, state)
    if options.command is Command.RESET_CREDENTIAL:
        return reset_credential_plan(options, state)
    return Plan(
        command=options.command,
        planning=Planning.NOT_ASKED,
        notes=(f"{options.command.value} asks questions; it does not act",),
    )


def install_plan(
    options: Options,
    state: ApplianceState,
    config: EncoreConfig,
    *,
    requested: EncoreConfig | None = None,
) -> Plan:
    """SAPRS 13.7's list, in the order that cannot leave a half-machine behind.

    The user before the directories (they will be chowned to it), the directories before the
    code (it is copied into one), the code before the environment (`uv sync` runs against the
    project it just placed), the files before systemd is told about them, and systemd last,
    because a service that starts before its configuration exists is the one failure mode an
    installer can guarantee to avoid and a machine can never recover from on its own.
    """

    layout = options.layout
    actions: list[Action] = []
    notes: list[str] = []

    if not state.user_exists and not options.staged:
        actions.append(
            CreateServiceUser(
                user=layout.user,
                group=layout.group,
                home=layout.staged(layout.state),
                audio_group=state.audio_group_exists,
                detail=f"create the {layout.user} service account (SAPRS 13.5)",
            )
        )
    elif not state.user_exists and options.staged:
        notes.append(
            f"{layout.user} is not created in a staged install; on the real machine, `useradd "
            "--system` is step two of SAPRS 13.7."
        )

    actions.extend(_path_actions(layout, state))

    application = layout.staged(layout.application)
    venv = layout.staged(layout.venv)
    actions.extend(_code_actions(state, options, application=application, venv=venv, notes=notes))

    written = _file_actions(layout, state, options, config, notes=notes, requested=requested)
    actions.extend(written)

    if not options.service:
        notes.append("No systemd integration (--no-service). Start it with scripts/run-server.sh.")
    elif registers_host_service(options):
        actions.extend(
            _service_actions(state, unit_changed=_touched(written, layout.staged(layout.unit_file)))
        )
    else:
        notes.append(host_service_note(layout))

    return Plan(command=options.command, actions=tuple(actions), notes=tuple(notes))


def upgrade_plan(
    options: Options,
    state: ApplianceState,
    config: EncoreConfig,
    *,
    requested: EncoreConfig | None = None,
) -> Plan:
    """13.9's five steps, and one the specification leaves out.

    Stop, update the application, let the migrations happen, validate, start. Step three is
    the interesting one: this build's migrations run when the Server opens `runtime.db`, and
    an installer that tried to apply them itself would be a second implementation of
    `encore/repositories/runtime/migrations.py` — the failure ADR-010 warns about for the
    library schema, reproduced against an operator's queue and history. So the step here is a
    copy, and the migration is done by the only program that knows how.
    """

    layout = options.layout
    actions: list[Action] = []
    notes: list[str] = []

    if state.service.active and registers_host_service(options):
        actions.append(StopService(service="encore", detail="stop Encore (SAPRS 13.9 step 1)"))

    application = layout.staged(layout.application)
    venv = layout.staged(layout.venv)
    actions.extend(
        _code_actions(state, options, application=application, venv=venv, notes=notes, upgrade=True)
    )

    runtime = layout.staged(layout.state + "/runtime.db")
    if state.present.get(f"{layout.state}/runtime.db", False):
        actions.append(
            BackupRuntimeDatabase(
                source=runtime,
                destination=runtime.parent / "runtime.db.pre-upgrade",
                detail="copy runtime.db before the migrations run (SAPRS 13.9 step 3)",
            )
        )
        notes.append(
            f"Migrations are forward-only and run when the Server opens the database. "
            f"{runtime.parent / 'runtime.db.pre-upgrade'} is the way back, and the appliance "
            "will not take it for you (ADR-014)."
        )

    actions.extend(_path_actions(layout, state))
    written = _file_actions(layout, state, options, config, notes=notes, requested=requested)
    actions.extend(written)

    if not options.service:
        pass
    elif registers_host_service(options):
        actions.extend(
            _service_actions(
                state,
                unit_changed=_touched(written, layout.staged(layout.unit_file)),
                restart=state.service.active,
            )
        )
    else:
        notes.append(host_service_note(layout))

    return Plan(command=options.command, actions=tuple(actions), notes=tuple(notes))


def remove_plan(options: Options, state: ApplianceState) -> Plan:
    """13.10's three scopes, from one table.

    The table is `Layout.removals`, and the scopes are the reason it exists: an operator who ran
    `remove` should get their music player back by removing the application, not by arguing with
    a program that deleted their library on the way. `execute.py` re-checks every deletion
    against `Layout.owned_roots`, derived from the same table, so a path outside it cannot be
    planned without also being made deletable — which is the arrangement that turns "the two
    lists disagreed" into a bug that cannot be written.

    What is absent matters more than what is present. The service user is not deleted and
    `/opt/music` is not touched in any scope: an account may have been created by hand years ago
    and may own other things, and a program that removes a user because it once removed a package
    is the reason the word "purge" needs a paragraph. Both appear in the report as commands, with
    the sentence that says why they are not run.
    """

    layout = options.layout
    actions: list[Action] = []
    notes: list[str] = []

    if registers_host_service(options):
        if state.service.active:
            actions.append(StopService(service="encore", detail="stop the appliance"))
        if state.service.installed:
            actions.append(DisableService(service="encore", detail="remove it from the boot order"))
    elif options.service and (state.service.active or state.service.installed):
        notes.append(host_service_note(layout, appliance="systemctl disable --now encore"))

    kept: set[str] = set()
    for final, scope in layout.removals:
        if scope == SCOPE_CONFIGURATION and not options.purge_config:
            kept.add(scope)
            continue
        if scope == SCOPE_DATA and not options.purge_data:
            kept.add(scope)
            continue
        if state.present.get(final, False):
            path = layout.staged(final)
            actions.append(DeletePath(path=path, scope=scope, detail=f"remove {path} ({scope})"))

    if SCOPE_CONFIGURATION in kept:
        notes.append(
            f"{layout.staged('/etc/encore')} is kept: configuration removal is a separate "
            "decision, and `--purge-config` is how you make it (SAPRS 13.10)."
        )
    if SCOPE_DATA in kept:
        notes.append(
            f"{layout.staged(layout.state)} is kept: library.db, runtime.db, artwork and "
            "whatever the queue remembers. `--purge-data` is the flag that means it (13.10)."
        )
    elif options.purge_data:
        notes.append(
            "Data removal does not touch the music directory. Encore never wrote to it "
            "(ADR-010) and neither does this."
        )

    notes.append(
        f"The {layout.user} account is left alone. If you created it for Encore, "
        f"`sudo userdel -r {layout.user}` is the command; the installer will not run it."
    )

    return Plan(command=options.command, actions=tuple(actions), notes=tuple(notes))


def reset_credential_plan(options: Options, state: ApplianceState) -> Plan:
    """The locked-out operator's path, and the only write to `runtime.db` there is."""

    layout = options.layout
    database = layout.staged(layout.state + "/runtime.db")
    if not state.present.get(f"{layout.state}/runtime.db", False):
        return Plan(
            command=options.command,
            notes=(f"{database} does not exist; there is no credential to forget.",),
        )
    actions: list[Action] = []
    notes = [
        "The next visit to /admin asks for a new password. Sessions live in memory, so a "
        "restart ends any that were open when the row was cleared (ADR-013).",
    ]
    # The stop is here because the appliance owns the database this writes to. In a rehearsal the
    # appliance owns the *host's* database, which is not the file being touched, so stopping it is a
    # host effect bought for nothing — #44 in its most musical form.
    host_service = state.service.active and registers_host_service(options)
    if state.service.active:
        if host_service:
            actions.append(
                StopService(service="encore", detail="stop the appliance, which owns the database")
            )
        elif options.service:
            notes.append(
                host_service_note(layout, appliance="systemctl stop encore, then start it again")
            )
    actions.append(
        ForgetCredential(
            database=database,
            detail=f"clear the administrator row in {database} (SAPRS 12.6, ADR-013)",
        )
    )
    if options.service and host_service:
        actions.append(StartService(service="encore", detail="start it again"))
    return Plan(command=options.command, actions=tuple(actions), notes=tuple(notes))


# -- shared steps --------------------------------------------------------


def _path_actions(layout: Layout, state: ApplianceState) -> list[Action]:
    """Create what is missing, repair what drifted, and say nothing about what matches.

    The third clause is idempotence. An installer that `chmod`s what it just made in the same
    run is an installer that cannot tell whether it changed anything, and a second run that
    reports twelve actions performed is indistinguishable from a second run that broke
    something twelve ways.

    Ownership is compared only when there is an account to compare against: on a fresh machine
    the plan creates the user and then the directories, so the observation was taken while
    nobody could have owned anything (see `PathState.matches`).
    """

    missing: list[tuple[Path, str, str, int]] = []
    drifted: list[tuple[Path, str, str, int]] = []
    for entry in layout.directories:
        path = layout.staged(entry.final)
        observed = state.directory(entry.final)
        if not observed.exists:
            missing.append((path, entry.owner, entry.group, entry.mode))
        elif not observed.matches(
            owner=entry.owner, group=entry.group, mode=entry.mode, ownership=state.user_exists
        ):
            drifted.append((path, entry.owner, entry.group, entry.mode))

    actions: list[Action] = []
    if missing:
        listed = ", ".join(str(path) for path, *_ in missing[:3])
        more = "" if len(missing) == 1 else f" (+{len(missing) - 1} more)"
        actions.append(
            CreateDirectories(
                entries=tuple(missing),
                detail=f"create {listed}{more} (SAPRS 13.3)",
            )
        )
    if drifted:
        actions.append(
            RepairPaths(
                entries=tuple(drifted),
                detail=f"repair ownership and mode on {len(drifted)} path(s) that drifted",
            )
        )
    return actions


def checkout(start: Path | None = None) -> Path:
    """The repository this installer is standing in, which is what it installs from.

    Walked up from `__file__` rather than from the current directory, because `sudo
    encore-install` may be invoked from anywhere and because the answer a test needs is "the
    tree containing this code". `--source` overrides it for the case where somebody is
    installing from an unpacked artifact rather than a clone.
    """

    here = (start if start is not None else Path(__file__)).resolve()
    for parent in (here, *here.parents):
        if (parent / "pyproject.toml").is_file() and (parent / "encore" / "__init__.py").is_file():
            return parent
    return Path.cwd()


def _code_actions(
    state: ApplianceState,
    options: Options,
    *,
    application: Path,
    venv: Path,
    notes: list[str],
    upgrade: bool = False,
) -> list[Action]:
    """Copy the code and build the environment — when either would change anything.

    The two comparisons come from `observe()`, so the plan's idea of "already installed" is a
    fact a report can name: the files in `application/` match the ones being offered, and the
    stamp in `venv/` matches the lockfile being read. An operator who pulled a release by hand
    still gets a dependency sync, which is the case a step skipped on assumption would
    otherwise be silent about.
    """

    source = options.source if options.source is not None else checkout()
    actions: list[Action] = []
    if source.resolve() == application.resolve():
        notes.append(
            f"The checkout being installed from is {application} itself; nothing is copied."
        )
    elif not state.application_matches:
        step = " (SAPRS 13.9 step 2)" if upgrade else ""
        actions.append(
            InstallApplication(
                source=source,
                destination=application,
                detail=f"install the application into {application}{step}",
            )
        )
    if not state.environment_matches:
        step = " (SAPRS 13.9 step 2)" if upgrade else " (SAPRS 13.4)"
        actions.append(
            SyncEnvironment(
                application=application,
                venv=venv,
                offline=options.offline,
                detail=f"install Python dependencies into {venv}{step}",
            )
        )
    return actions


def _touched(actions: list[Action], path: Path) -> bool:
    """Whether any action in `actions` writes `path`. systemd needs to be told exactly then."""

    return any(isinstance(action, EnsureFile) and action.path == path for action in actions)


def _file_actions(
    layout: Layout,
    state: ApplianceState,
    options: Options,
    config: EncoreConfig,
    *,
    notes: list[str],
    requested: EncoreConfig | None = None,
) -> list[Action]:
    """Render each managed file, and write only the ones whose text would change."""

    actions: list[Action] = []
    wanted = requested if requested is not None else config
    rendered = {
        layout.config_file: config_text(config, layout),
        layout.environment_file: environment_text(
            admin_password=options.admin_password, layout=layout
        ),
        layout.unit_file: unit_text(
            config,
            layout,
            application=layout.staged(layout.application),
            venv=layout.staged(layout.venv),
            config_path=layout.staged(layout.config_file),
        ),
        layout.tmpfiles_file: tmpfiles_text(layout),
    }

    for entry in layout.files:
        text = rendered[entry.final]
        if entry.final == layout.config_file:
            actions.extend(
                _config_action(entry, state, options, text=text, asked=config_text(wanted, layout))
            )
            continue
        if not options.service and entry.final in (layout.unit_file, layout.tmpfiles_file):
            continue
        if state.contents.get(entry.final) == text and state.file(entry.final).exists:
            continue
        actions.append(
            EnsureFile(
                path=layout.staged(entry.final),
                text=text,
                owner=entry.owner,
                group=entry.group,
                mode=entry.mode,
                purpose=entry.purpose,
                detail=f"write {layout.staged(entry.final)}"
                + (f" ({entry.purpose})" if entry.purpose else ""),
            )
        )
    if not options.service:
        notes.append("The unit and tmpfiles rules are not written (--no-service).")
    return actions


def _config_action(
    entry: ManagedFile,
    state: ApplianceState,
    options: Options,
    *,
    text: str,
    asked: str,
) -> list[Action]:
    """The 13.8 branch, decided by `config_file.decide` rather than here.

    A configuration is the one file an operator edits, so the three outcomes are separated
    finely enough to be reported honestly: written when there was nothing to write over,
    preserved with the differences named when there was, and preserved with a failure when the
    file that is there is not usable. Overwriting the third case would turn a five-minute
    edit into a reinstallation.
    """

    decision = decide(state.config_text, options.layout, rendered=text, asked=asked)
    if decision.action == Decision.WRITE:
        return [
            WriteConfig(
                path=decision.path,
                text=decision.text,
                owner=entry.owner,
                group=entry.group,
                mode=entry.mode,
                detail=f"write {decision.path} (SAPRS 12.2)",
            )
        ]
    if decision.action == Decision.INVALID:
        return [
            KeepExistingConfig(
                path=decision.path,
                problems=decision.problems,
                detail=f"leave {decision.path} alone: it is not valid, and this will not start",
            )
        ]
    if decision.action == Decision.UNCHANGED:
        return []
    return [
        PreserveConfig(
            path=decision.path,
            reason=decision.problems[0] if decision.problems else "an existing file is kept",
            differences=decision.settings,
            detail=f"keep {decision.path} as it is (SAPRS 13.8)",
        )
    ]


#: What the appliance itself runs to bring its unit up. A constant because install and upgrade
#: quote the same sentence and `remove` quotes its mirror.
REGISTRATION = "systemctl daemon-reload && systemctl enable --now encore"


def registers_host_service(options: Options) -> bool:
    """Whether this run may talk to this machine's service manager.

    One function, because three commands do it and #44 is what happens when the rule is written per
    call site: `systemctl` asks PID 1 about `encore.service`, and a unit file staged under a prefix
    is not what it will find. A rehearsal's `daemon-reload`, `enable`, `start`, `stop` and `disable`
    therefore change nothing about the tree being rehearsed and everything about the host running it
    — on a machine where Encore is installed and playing, `remove --root /tmp/x --yes` stops the
    appliance. Staging already declines to run `useradd` for this same reason, and `next_steps`
    already refuses to print `systemctl start encore` for a rehearsal; the plan was the half that
    had not been told.
    """

    return options.service and not options.staged


def host_service_note(layout: Layout, *, appliance: str = REGISTRATION) -> str:
    """The sentence that replaces those actions, so a rehearsal does not skip a step in silence.

    `appliance` is the command the machine that *is* the appliance will run, which differs between
    installing it and removing it; the reason for the rehearsal to say anything at all is that a
    skipped step nobody mentions is how 13.6 becomes a silent flag.
    """

    return (
        "The host's service manager was not asked to do anything: `systemctl` talks to this "
        f"machine, and a rehearsal rooted at {layout.root} is not this machine. On the appliance: "
        f"{appliance}."
    )


def _service_actions(
    state: ApplianceState, *, unit_changed: bool, restart: bool = False
) -> list[Action]:
    """Tell, enable, start — and none of them when nothing changed.

    Every caller is behind `registers_host_service`, which is why a rehearsal never reaches here.

    `restart` is the upgrade's answer to a service that is already running: `start` would be a
    no-op, and a no-op that leaves the old code loaded while the report says "started" is how
    an upgrade appears to succeed.
    """

    actions: list[Action] = []
    if unit_changed:
        actions.append(DaemonReload())
    if not state.service.enabled:
        actions.append(EnableService(service="encore", detail="start Encore at boot (SAPRS 13.6)"))
    if not state.service.active:
        actions.append(StartService(service="encore", detail="start Encore"))
    elif restart:
        actions.append(RestartService(service="encore", detail="restart Encore onto the new code"))
    return actions
