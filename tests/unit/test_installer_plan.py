"""The plan: observed state plus a request, and nothing else (SAPRS 13.7, 13.9, 13.10).

`plan.py` is a pure function, and these tests are the reason it is one. Every question here —
"does a second install do nothing?", "in what order does an upgrade stop the service?", "which
paths does a default removal name?" — is answered by a table lookup that needs no filesystem, no
root user and no systemd. The integration suite proves the plan is *executed*; this file proves
the plan is *right*, which is the half an operator reads before pressing return.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import yaml

from apps.installer.content import config_text, environment_text, tmpfiles_text, unit_text
from apps.installer.layout import Layout
from apps.installer.options import Command, Options, Settings
from apps.installer.plan import (
    BackupRuntimeDatabase,
    CreateDirectories,
    CreateServiceUser,
    DaemonReload,
    DeletePath,
    DisableService,
    EnableService,
    EnsureFile,
    InstallApplication,
    KeepExistingConfig,
    PreserveConfig,
    RepairPaths,
    RestartService,
    StartService,
    StopService,
    SyncEnvironment,
    WriteConfig,
    install_plan,
    plan_for,
    remove_plan,
    reset_credential_plan,
    upgrade_plan,
)
from apps.installer.system import ApplianceState, OsRelease, PathState, ServiceState
from encore.config.models import EncoreConfig

RUNTIME_DB = "/var/lib/encore/runtime.db"


#: The production layout. Used by the tests whose subject is the step a staged install
#: deliberately skips — creating a system account — and by no others, because a test written
#: against `/var/lib/encore` cannot tell whether staging works.
production = Layout()


def stage(root: Path) -> Layout:
    """The appliance, staged under a test's temporary directory.

    Staged rather than production because a plan is pure and a bug in `Layout.staged` is not: an
    assertion written against `/var/lib/encore` would pass even if the installer computed paths
    outside the staging root, and the integration suite would be the only thing to notice.
    """

    return Layout(root=root)


def options(
    layout: Layout,
    *,
    command: Command = Command.INSTALL,
    service: bool = False,
    **values: object,
) -> Options:
    """A request. Tests pass a staged layout; one passes the production paths (see below)."""

    music = layout.staged("/opt/music")
    return Options(
        command=command,
        layout=layout,
        source=Path("/checkout"),
        settings=Settings(music_dir=music),
        service=service,
        **values,  # type: ignore[arg-type]
    )


def empty(layout: Layout, **overrides: object) -> ApplianceState:
    """A machine with nothing installed, stated rather than defaulted.

    Every field is given on purpose: an `ApplianceState` whose venv arrives from a dataclass
    default would let a test claim "fresh" while describing a box that was half-built.
    """

    base: dict[str, object] = {
        "layout": layout,
        "os_release": OsRelease(id="debian"),
        "service": ServiceState(),
        "binaries": {"mpv": Path("/usr/bin/mpv"), "uv": Path("/usr/bin/uv")},
        "user_exists": False,
        "audio_group_exists": True,
        "directories": {
            entry.final: PathState(path=layout.staged(entry.final)) for entry in layout.directories
        },
        "files": {
            entry.final: PathState(path=layout.staged(entry.final)) for entry in layout.files
        },
        "contents": {},
        "config_text": None,
        "disk_free": 64 * 1024**3,
        "port_free": True,
        "hostname": "jukebox",
        "root": True,
        "present": {final: False for final, _ in layout.removals} | {RUNTIME_DB: False},
    }
    return ApplianceState(**{**base, **overrides})  # type: ignore[arg-type]


def installed(layout: Layout, **overrides: Any) -> ApplianceState:
    """What a finished install looks like to an observer.

    Built from the same `content` functions the installer writes with, which is the arrangement
    that keeps this honest: change what a generated unit file says and this stops being "already
    installed", so the idempotence tests below fail instead of quietly agreeing with whatever the
    code happens to do.
    """

    config = EncoreConfig()
    contents = {
        layout.config_file: config_text(config, layout),
        layout.environment_file: environment_text(admin_password=None, layout=layout),
        layout.unit_file: unit_text(
            config,
            layout,
            application=layout.staged(layout.application),
            venv=layout.staged(layout.venv),
            config_path=layout.staged(layout.config_file),
        ),
        layout.tmpfiles_file: tmpfiles_text(layout),
    }
    state = empty(
        layout,
        user_exists=True,
        service=ServiceState(installed=True, enabled=True, active=True),
        directories={
            entry.final: PathState(
                path=layout.staged(entry.final),
                exists=True,
                is_dir=True,
                owner=entry.owner,
                group=entry.group,
                mode=entry.mode,
            )
            for entry in layout.directories
        },
        files={
            entry.final: PathState(
                path=layout.staged(entry.final),
                exists=True,
                owner=entry.owner,
                group=entry.group,
                mode=entry.mode,
            )
            for entry in layout.files
        },
        contents=contents,
        config_text=contents[layout.config_file],
        application_matches=True,
        environment_matches=True,
        environment_runnable=True,
        present={final: True for final, _ in layout.removals},
    )
    return replace(state, **overrides) if overrides else state


def action_kinds(actions: Sequence[Any]) -> list[str]:
    """The action types, in order — the shape a plan test is really about.

    Named kinds rather than classes, because a failure that prints
    `['CreateDirectories', 'WriteConfig']` against an expected list reads as a diff, and a
    failure that prints two `<object>` reprs reads as a mystery.
    """

    return [type(action).__name__ for action in actions]


# -- install ----------------------------------------------------------------


def test_a_fresh_machine_gets_the_documented_order() -> None:
    """SAPRS 13.7's list, in the order that cannot leave a half-machine behind.

    The user before the directories that will be chowned to it; the directories before the code
    copied into them; the code before the environment built from it; every file before systemd is
    told a service exists. Starting comes last, because a service launched before its
    configuration is on disk is the one failure an installer can promise to avoid (13.7).
    """

    plan = install_plan(options(production, service=True), empty(production), EncoreConfig())
    assert action_kinds(plan.actions) == [
        CreateServiceUser.__name__,
        CreateDirectories.__name__,
        InstallApplication.__name__,
        SyncEnvironment.__name__,
        WriteConfig.__name__,
        EnsureFile.__name__,
        EnsureFile.__name__,
        EnsureFile.__name__,
        DaemonReload.__name__,
        EnableService.__name__,
        StartService.__name__,
    ]


def test_a_completed_install_plans_nothing(tmp_path: Path) -> None:
    """13.7's "idempotent", as an empty list rather than as an adjective."""

    plan = install_plan(
        options(stage(tmp_path), service=True), installed(stage(tmp_path)), EncoreConfig()
    )
    assert not plan.actions, [action.detail for action in plan.actions]


def test_an_interrupted_install_gets_only_the_missing_half(tmp_path: Path) -> None:
    """The power cut between copying the code and building the environment is the real case.

    Asserted by kind rather than by count so the test says which step is missing: an installer
    that re-ran every step would also "work", and the operator would only learn the difference
    from the report.
    """

    state = installed(stage(tmp_path), environment_matches=False)
    assert action_kinds(
        install_plan(options(stage(tmp_path), service=True), state, EncoreConfig()).actions
    ) == [SyncEnvironment.__name__]


def test_drift_is_repaired_rather_than_recreated(tmp_path: Path) -> None:
    """A directory that exists with the wrong mode is reported as drift, not as "not installed".

    Somebody chown'd it, once, for a reason. `RepairPaths` is the action that says so;
    `CreateDirectories` would be silent, and silence is how the fix gets undone again.
    """

    layout = stage(tmp_path)
    state = installed(layout)
    drifted = dict(state.directories)
    drifted["/var/lib/encore"] = PathState(
        path=layout.staged("/var/lib/encore"),
        exists=True,
        is_dir=True,
        owner="root",
        group="root",
        mode=0o755,
    )
    plan = install_plan(
        options(stage(tmp_path), service=True),
        replace(state, directories=drifted),
        EncoreConfig(),
    )
    assert RepairPaths.__name__ in action_kinds(plan.actions)
    assert CreateDirectories.__name__ not in action_kinds(plan.actions)
    repaired = next(a for a in plan.actions if isinstance(a, RepairPaths))
    assert [entry[0] for entry in repaired.entries] == [layout.staged("/var/lib/encore")]


def test_the_service_account_is_created_once_and_read_from_the_machine() -> None:
    """`--groups audio` where the group exists, and not where it does not (SAPRS 13.5).

    Written against the production layout because a staged install never creates an account at
    all — see the test below — so this is the one place the step itself is observable.
    """

    with_audio = install_plan(options(production, service=True), empty(production), EncoreConfig())
    assert isinstance(with_audio.actions[0], CreateServiceUser)
    assert with_audio.actions[0].audio_group is True

    silent = install_plan(
        options(production, service=True),
        empty(production, audio_group_exists=False),
        EncoreConfig(),
    )
    assert isinstance(silent.actions[0], CreateServiceUser)
    assert silent.actions[0].audio_group is False


def test_a_staged_install_does_not_create_an_account(tmp_path: Path) -> None:
    """Rehearsing under `--root` must not change the machine doing the rehearsing."""

    plan = install_plan(
        options(stage(tmp_path), service=True), empty(stage(tmp_path)), EncoreConfig()
    )
    assert CreateServiceUser.__name__ not in action_kinds(plan.actions)
    assert any("staged install" in note for note in plan.notes)


def test_the_service_user_is_created_before_the_directories_it_owns() -> None:
    """An install that made `/var/lib/encore` first would chown it to a uid that is not there."""

    plan = install_plan(options(production, service=True), empty(production), EncoreConfig())
    order = action_kinds(plan.actions)
    assert order.index(CreateServiceUser.__name__) < order.index(CreateDirectories.__name__)


def test_no_service_writes_no_unit_and_runs_no_systemctl(tmp_path: Path) -> None:
    """13.2's container: the appliance is installed, and the host is not asked to manage it."""

    plan = install_plan(
        options(stage(tmp_path), service=False), empty(stage(tmp_path)), EncoreConfig()
    )
    written = [a.path.name for a in plan.actions if isinstance(a, EnsureFile)]
    assert written == ["environment"], written
    assert not {DaemonReload.__name__, EnableService.__name__, StartService.__name__} & set(
        action_kinds(plan.actions)
    )


def test_an_existing_configuration_is_preserved_even_when_it_disagrees(tmp_path: Path) -> None:
    """13.8, and the reason the rule is not "write the new one": the file is the operator's.

    The differences are named, because "your `--domain` did not apply" without the two values is
    a bug report the operator cannot act on.
    """

    layout = stage(tmp_path)
    edited = config_text(EncoreConfig(), layout).replace("page_size: 50", "page_size: 12")
    state = installed(layout, config_text=edited)
    plan = install_plan(options(layout, service=True), state, EncoreConfig())
    assert not [a for a in plan.actions if isinstance(a, WriteConfig)]
    preserved = [a for a in plan.actions if isinstance(a, PreserveConfig)]
    assert len(preserved) == 1
    assert ("search.page_size", 12, 50) in preserved[0].differences


def test_a_byte_identical_configuration_produces_no_configuration_action(tmp_path: Path) -> None:
    """The difference between an idempotent installer and a noisy one (SAPRS 13.7)."""

    plan = install_plan(
        options(stage(tmp_path), service=True), installed(stage(tmp_path)), EncoreConfig()
    )
    assert not [a for a in plan.actions if isinstance(a, WriteConfig | PreserveConfig)]


def test_an_annotated_configuration_produces_no_action(tmp_path: Path) -> None:
    """A comment is not a setting, and an installer that cannot tell is not idempotent.

    The difference from the test above is bytes rather than values. Reformatting, annotating or
    reordering a file are things operators do to the one file they are invited to edit, and a
    second run that reported "keep config.yaml as it is" for each of them would be a run that
    disagreed with the first about whether anything was installed.
    """

    layout = stage(tmp_path)
    written = config_text(EncoreConfig(), layout)
    annotated = "\n".join(["# tuned for this room", *written.splitlines()]).replace(
        "  port: 8080", "  port: 8080  # changed later, see the wiki"
    )
    plan = install_plan(
        options(layout, service=True), installed(layout, config_text=annotated), EncoreConfig()
    )
    assert not [a for a in plan.actions if isinstance(a, WriteConfig | PreserveConfig)]


def test_a_flag_that_the_file_outvotes_is_named(tmp_path: Path) -> None:
    """13.8's preservation, with the honesty clause.

    The file wins — that is the rule, and the previous test proves the installer will not touch
    it. What this proves is that the installer says so. A silently ignored `--port` is how an
    appliance ends up listening on a port its operator believes is free, and the report is the
    only place the two can meet.
    """

    layout = stage(tmp_path)
    edited = config_text(EncoreConfig(), layout).replace("  port: 8080", "  port: 9100")
    dials = Settings(port=8099)
    asked_for = dials.config(audio_output="alsa", mpv_path=Path("/usr/bin/mpv"))
    plan = install_plan(
        replace(options(layout, service=True), settings=dials),
        # The configuration in force is the file's, which is what `main._configuration` returns;
        # `requested` is the flag. Passing the same object to both is the bug this test is for.
        installed(layout, config_text=edited),
        EncoreConfig.model_validate(yaml.safe_load(edited)),
        requested=asked_for,
    )
    preserved = [a for a in plan.actions if isinstance(a, PreserveConfig)]
    assert preserved, "a file that disagrees with the flags must be reported, not respected quietly"
    assert any(name == "server.port" for name, _, _ in preserved[0].differences)


def test_a_broken_configuration_is_kept_and_the_run_fails(tmp_path: Path) -> None:
    """The case where preserving the file hurts, which is the case the rule is really for.

    Overwriting an operator's typo would turn a five-minute edit into a reinstalled appliance, so
    the action is a deliberate non-act and the failure arrives from `verify` instead.
    """

    state = installed(stage(tmp_path), config_text="server:\n  port: not_a_number\n")
    plan = install_plan(options(stage(tmp_path), service=True), state, EncoreConfig())
    kept = [a for a in plan.actions if isinstance(a, KeepExistingConfig)]
    assert kept
    assert kept[0].problems


def test_the_environment_file_is_written_even_without_systemd(tmp_path: Path) -> None:
    """`ENCORE_HOME` and `ENCORE_CONFIG` are how a hand-started server finds its appliance."""

    plan = install_plan(
        options(stage(tmp_path), service=False),
        empty(stage(tmp_path), user_exists=True),
        EncoreConfig(),
    )
    assert any(isinstance(a, EnsureFile) and a.path.name == "environment" for a in plan.actions)


def test_a_generated_unit_names_the_venv_it_built_not_the_one_it_ran_from(tmp_path: Path) -> None:
    """The bug this guards is subtle and fatal: an ExecStart pointing at the installer's own
    environment would work on the development machine and fail on the appliance."""

    plan = install_plan(
        options(stage(tmp_path), service=True), empty(stage(tmp_path)), EncoreConfig()
    )
    unit = next(
        a for a in plan.actions if isinstance(a, EnsureFile) and a.path.name == "encore.service"
    )
    assert str(stage(tmp_path).staged("/opt/encore/venv")) in unit.text


# -- upgrade ----------------------------------------------------------------


def test_an_upgrade_stops_the_service_before_it_changes_anything(tmp_path: Path) -> None:
    """13.9 step 1, first. Replacing files a running process has open is how an upgrade ends up
    with half of one release and half of another loaded in the same interpreter."""

    state = installed(
        stage(tmp_path), service=ServiceState(installed=True, enabled=True, active=True)
    )
    plan = upgrade_plan(options(stage(tmp_path), service=True), state, EncoreConfig())
    assert action_kinds(plan.actions)[0] == StopService.__name__


def test_an_upgrade_backs_up_runtime_before_the_migrations_run(tmp_path: Path) -> None:
    """13.9 step 3, as a copy rather than as a schema this program applies.

    Migrations belong to the Server, which owns them (`runtime/migrations.py`); a second
    implementation in the installer is ADR-010's warning reproduced against an operator's queue.
    The copy is the part only the installer can do, so that is what this step is.
    """

    state = installed(
        stage(tmp_path),
        service=ServiceState(installed=True, enabled=True, active=True),
        present={**{final: True for final, _ in stage(tmp_path).removals}, RUNTIME_DB: True},
    )
    plan = upgrade_plan(options(stage(tmp_path), service=True), state, EncoreConfig())
    backups = [a for a in plan.actions if isinstance(a, BackupRuntimeDatabase)]
    assert len(backups) == 1
    assert backups[0].destination.name == "runtime.db.pre-upgrade"
    started = [
        i for i, a in enumerate(plan.actions) if isinstance(a, StartService | RestartService)
    ]
    assert started
    assert started[0] > plan.actions.index(backups[0])
    assert any("pre-upgrade" in note for note in plan.notes)


def test_an_upgrade_of_a_box_that_never_booted_has_nothing_to_back_up(tmp_path: Path) -> None:
    state = installed(stage(tmp_path), present={RUNTIME_DB: False})
    plan = upgrade_plan(options(stage(tmp_path), service=True), state, EncoreConfig())
    assert not [a for a in plan.actions if isinstance(a, BackupRuntimeDatabase)]


def test_an_uptodate_upgrade_is_a_restart_and_nothing_else(tmp_path: Path) -> None:
    """`start` on a running service would be a no-op that reports success while the old code

    keeps playing — so a running, unchanged appliance gets stopped and restarted instead."""

    plan = upgrade_plan(
        options(stage(tmp_path), service=True),
        installed(stage(tmp_path), present={RUNTIME_DB: False}),
        EncoreConfig(),
    )
    assert action_kinds(plan.actions) == [StopService.__name__, RestartService.__name__]


def test_an_upgrade_reenables_a_service_that_fell_out_of_the_boot_order(tmp_path: Path) -> None:
    """`systemctl disable` during an experiment is invisible until the power comes back on."""

    state = installed(
        stage(tmp_path), service=ServiceState(installed=True, enabled=False, active=False)
    )
    plan = upgrade_plan(options(stage(tmp_path), service=True), state, EncoreConfig())
    assert EnableService.__name__ in action_kinds(plan.actions)


def test_an_uptodate_upgrade_moves_no_files(tmp_path: Path) -> None:
    """The same emptiness as install, arriving by a different route.

    Stated separately because the two plans are written separately: an upgrade that re-copied the
    application on every run would pass every install test above and still restart a party.
    """

    plan = upgrade_plan(
        options(stage(tmp_path), service=True),
        installed(stage(tmp_path)),
        EncoreConfig(),
    )
    assert not [
        a
        for a in plan.actions
        if isinstance(a, InstallApplication | SyncEnvironment | EnsureFile | WriteConfig)
    ]
    assert action_kinds(plan.actions) == [StopService.__name__, RestartService.__name__]


# -- remove -----------------------------------------------------------------


def test_a_default_removal_leaves_the_library_and_the_configuration(tmp_path: Path) -> None:
    """13.10's scopes: removing the application should not cost anybody their music index."""

    plan = remove_plan(options(stage(tmp_path), service=True), installed(stage(tmp_path)))
    deleted = {a.path for a in plan.actions if isinstance(a, DeletePath)}
    staged = stage(tmp_path).staged
    assert staged("/opt/encore/application") in deleted
    assert staged("/etc/systemd/system/encore.service") in deleted
    assert staged("/var/lib/encore") not in deleted
    assert staged("/etc/encore/config.yaml") not in deleted
    assert all("music" not in str(path) for path in deleted)


def test_purge_flags_are_the_only_way_to_widen_the_scope(tmp_path: Path) -> None:
    plan = remove_plan(
        options(stage(tmp_path), service=True, purge_config=True, purge_data=True),
        installed(stage(tmp_path)),
    )
    deleted = {a.path for a in plan.actions if isinstance(a, DeletePath)}
    staged = stage(tmp_path).staged
    assert staged("/etc/encore") in deleted
    assert staged("/var/lib/encore") in deleted
    assert staged("/var/log/encore") in deleted


def test_removal_says_what_it_declined_to_delete(tmp_path: Path) -> None:
    """The note is the feature: an operator needs to know where the databases are, not guess."""

    plan = remove_plan(options(stage(tmp_path)), installed(stage(tmp_path)))
    joined = " ".join(plan.notes)
    assert "--purge-data" in joined
    assert "--purge-config" in joined
    assert "userdel" in joined, "the service account is left alone and must be reported"


def test_removal_stops_and_disables_before_deleting_the_unit(tmp_path: Path) -> None:
    """Deleting the unit file from under a running service leaves a process no tool can stop."""

    state = installed(
        stage(tmp_path), service=ServiceState(installed=True, enabled=True, active=True)
    )
    plan = remove_plan(options(stage(tmp_path), service=True), state)
    order = action_kinds(plan.actions)
    assert order[0] == StopService.__name__
    assert order[1] == DisableService.__name__
    assert order.index("DeletePath") > order.index(DisableService.__name__)


def test_removal_on_an_absent_machine_plans_no_deletions(tmp_path: Path) -> None:
    state = empty(stage(tmp_path))
    plan = remove_plan(options(stage(tmp_path), service=True), state)
    assert not [a for a in plan.actions if isinstance(a, DeletePath)]


# -- reset-credential --------------------------------------------------------


def test_reset_credential_stops_the_service_before_touching_the_database(tmp_path: Path) -> None:
    """A row deleted from a live WAL database is a corruption, not a password reset.

    This is ADR-013's "approved maintenance" in the literal sense: the appliance is stopped, the
    one row is cleared, and it is started again — and the report says so, because the operator who
    runs it at 2am is the person who needs to know the queue was interrupted.
    """

    state = installed(
        stage(tmp_path),
        service=ServiceState(installed=True, enabled=True, active=True),
        present={**{final: True for final, _ in stage(tmp_path).removals}, RUNTIME_DB: True},
    )
    plan = reset_credential_plan(options(stage(tmp_path), service=True), state)
    order = action_kinds(plan.actions)
    assert order[0] == StopService.__name__
    assert order[1] == "ForgetCredential"
    assert order[2] == StartService.__name__


def test_reset_credential_on_a_box_that_never_booted_says_so(tmp_path: Path) -> None:
    plan = reset_credential_plan(options(stage(tmp_path)), empty(stage(tmp_path)))
    assert not plan.actions
    assert any("does not exist" in note for note in plan.notes)


# -- dispatch ----------------------------------------------------------------


def test_plan_for_dispatches_on_the_command(tmp_path: Path) -> None:
    """`main.py` holds no policy, so the dispatch has to be somewhere testable."""

    for command in (Command.UPGRADE, Command.REMOVE, Command.RESET_CREDENTIAL):
        plan = plan_for(
            options(stage(tmp_path), command=command),
            installed(stage(tmp_path), service=ServiceState()),
            EncoreConfig(),
        )
        assert plan.command is command
    verify = plan_for(
        options(stage(tmp_path), command=Command.VERIFY), empty(stage(tmp_path)), EncoreConfig()
    )
    assert not verify.actions
    assert verify.command is Command.VERIFY
