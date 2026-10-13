"""Prerequisites and acceptance, as verdicts an operator can act on (SAPRS 13.7, 13.11).

`checks.py` is the module a failed install is judged by, so these tests are written from the
report outward: for each way a machine can be wrong, what does the line say, is it a failure or a
warning, and does it name the command that fixes it. A FAIL with no fix is an error message with a
period on the end.

The WARN/FAIL boundary gets as much attention as either, because it is the distinction installers
most often get wrong in the direction that annoys people — refusing a reasonable install because
something optional was missing.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from apps.installer.checks import Verdict, assess, blocked
from apps.installer.layout import Layout
from apps.installer.options import Command, Options, Settings
from apps.installer.system import ApplianceState, OsRelease, ServiceAccount, ServiceState
from encore.config.models import EncoreConfig, PathsConfig, ServerConfig

MPV = Path("/usr/bin/mpv")
UV = Path("/usr/bin/uv")

#: For the checks whose verdict depends on whether real paths are involved.
production = Layout()


def layout_of(root: Path) -> Layout:
    return Layout(root=root)


def request(layout: Layout, *, command: Command = Command.INSTALL, **values: Any) -> Options:
    return Options(
        command=command,
        layout=layout,
        settings=Settings(music_dir=layout.staged("/opt/music")),
        **values,
    )


def machine(layout: Layout, **values: Any) -> ApplianceState:
    """A machine that passes, so each test below breaks exactly one thing."""

    account = values.pop(
        "account",
        ServiceAccount(
            name="encore", shell="/usr/sbin/nologin", home="/var/lib/encore", groups=("audio",)
        ),
    )
    base: dict[str, Any] = {
        "layout": layout,
        "os_release": OsRelease(id="raspbian", pretty_name="Raspberry Pi OS (12)"),
        "service": ServiceState(installed=True, enabled=True, active=False),
        "binaries": {"mpv": MPV, "uv": UV},
        "user_exists": account is not None,
        "service_user": account,
        "audio_group_exists": True,
        "disk_free": 64 * 1024**3,
        "port_free": True,
        "music_readable": True,
        "hostname": "jukebox",
        "root": True,
        "environment_runnable": True,
        "present": {final: True for final, _ in layout.removals},
    }
    return ApplianceState(**{**base, **values})


def configuration(layout: Layout) -> EncoreConfig:
    return EncoreConfig(
        server=ServerConfig(port=8080, domain="jukebox.home.arpa"),
        paths=PathsConfig(
            library_db=layout.staged("/var/lib/encore/library.db"),
            runtime_db=layout.staged("/var/lib/encore/runtime.db"),
            artwork_dir=layout.staged("/var/lib/encore/artwork"),
            music_dir=layout.staged("/opt/music"),
        ),
    )


def library(root: Path, *, songs: int = 15_212, indexed: bool = True) -> Path:
    """A `library.db` with the two things the check asks about, written by hand.

    Not the Builder's output: this file tests a verdict, and a fixture that ran the whole
    pipeline would make a Builder change look like an installer bug. The names (`songs`,
    `song_search`) are the contract, which is what makes the check honest.

    `root` is a directory rather than a `Layout` because three tests below deliberately describe
    a *production* appliance; their databases still have to live in `tmp_path`.
    """

    path = root / "var/lib/encore/library.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)  # a test that asks for two libraries means two different ones
    with sqlite3.connect(path) as database:
        database.execute("CREATE TABLE songs (id integer primary key, title text)")
        database.executemany(
            "INSERT INTO songs (title) VALUES (?)", [(f"n{i}",) for i in range(songs)]
        )
        if indexed:
            database.execute("CREATE VIRTUAL TABLE song_search USING fts5(title)")
    return path


def runtime(root: Path, *, administrator: bool = True) -> Path:
    path = root / "var/lib/encore/runtime.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    with sqlite3.connect(path) as database:
        database.execute(
            "CREATE TABLE admin_state (id integer primary key, password_hash text,"
            " iterations integer, created_at text, updated_at text)"
        )
        if administrator:
            database.execute("INSERT INTO admin_state VALUES (1, 'x', 1000, '', '')")
    return path


def layout_of_path(layout: Layout | Path) -> Layout:
    return layout if isinstance(layout, Layout) else Layout(root=layout)


def verdict_of(checks: Any, name: str) -> Verdict:
    found: Verdict = next(check.verdict for check in checks if check.name == name)
    return found


def finding(checks: Any, name: str) -> str:
    check = next(check for check in checks if check.name == name)
    return f"{check.detail} {check.fix}"


def assess_box(
    layout: Layout,
    state: ApplianceState | None = None,
    *,
    tmp_path: Path | None = None,
    command: Command = Command.INSTALL,
    service: bool = False,
    dry_run: bool = False,
    config: EncoreConfig | None = None,
    library_db: Path | None = None,
    runtime_db: Path | None = None,
) -> Any:
    """The real `assess`, with the four arguments every test here does not care about.

    Databases are built rather than stubbed: the two checks that read them ask questions of
    SQLite — is this a library, has anybody set a password — and a fake that answers "yes"
    would make both of those checks untested. `tmp_path` overrides where they go, for the tests
    that describe a production appliance without wanting to write to `/var/lib`.
    """

    options = request(layout, command=command, service=service, dry_run=dry_run)
    databases = tmp_path if tmp_path is not None else layout.root
    return assess(
        state if state is not None else machine(layout),
        options,
        config=config if config is not None else configuration(layout),
        library_db=library_db if library_db is not None else library(databases),
        runtime_db=runtime_db if runtime_db is not None else runtime(databases),
    )


# -- the whole list -----------------------------------------------------------


def test_a_box_that_is_fine_blocks_nothing(tmp_path: Path) -> None:
    checks = assess_box(layout_of(tmp_path))
    assert not blocked(checks), [check.line() for check in checks if check.blocking]


def test_every_check_names_the_question_it_asked(tmp_path: Path) -> None:
    """A verdict without a subject is a log line, and 13.11 is a list to work down."""

    checks = assess_box(layout_of(tmp_path))
    assert all(check.name for check in checks)
    assert len({check.name for check in checks}) == len(checks), "two checks claim one name"


def test_an_ok_verdict_still_carries_evidence(tmp_path: Path) -> None:
    """`ok` is not a detail. The report has to be usable as a bug report."""

    checks = assess_box(layout_of(tmp_path))
    assert all(check.detail for check in checks), [
        check.name for check in checks if not check.detail
    ]


def test_the_list_covers_every_acceptance_item_from_1311(tmp_path: Path) -> None:
    """Chapter 13.11 names six outcomes; each one has a row here or is deliberately absent.

    Asserted by name, so deleting a check is a decision rather than a refactor.
    """

    names = {check.name for check in assess_box(layout_of(tmp_path), service=True)}
    assert {
        "privilege",
        "platform",
        "service manager",
        "binaries",
        "environment",
        "service user",
        "port",
        "disk",
        "music",
        "configuration",
        "library",
        "administrator",
        "unit",
        "at boot",
        "running",
    } <= names


# -- privilege -----------------------------------------------------------------


def test_running_as_an_ordinary_user_fails_only_when_it_costs_something(tmp_path: Path) -> None:
    """13.1's "root is not required" is true, and 13.6's systemd steps still need it.

    Written against the production layout because staging is what makes an unprivileged run
    harmless: the same user, asked to install into `/tmp`, is not going to fail for want of root.
    """

    checks = assess_box(
        production, machine(production, root=False), service=True, tmp_path=tmp_path
    )
    assert verdict_of(checks, "privilege") is Verdict.FAIL
    assert "sudo encore-install" in finding(checks, "privilege")


def test_a_staged_run_is_not_scolded_for_not_being_root(tmp_path: Path) -> None:
    layout = layout_of(tmp_path)
    checks = assess_box(layout, machine(layout, root=False), service=True)
    assert verdict_of(checks, "privilege") is Verdict.OK


def test_a_dry_run_needs_no_privilege_at_all(tmp_path: Path) -> None:
    layout = layout_of(tmp_path)
    checks = assess_box(layout, machine(layout, root=False), service=True, dry_run=True)
    assert verdict_of(checks, "privilege") is Verdict.OK


# -- platform ------------------------------------------------------------------


def test_an_unlisted_distribution_warns_instead_of_refusing(tmp_path: Path) -> None:
    """13.1's "where practical". Anything else would be a claim about our own testing."""

    layout = layout_of(tmp_path)
    checks = assess_box(
        layout, machine(layout, os_release=OsRelease(id="fedora", pretty_name="Fedora 40"))
    )
    assert verdict_of(checks, "platform") is Verdict.WARN
    assert "Fedora 40" in finding(checks, "platform")


@pytest.mark.parametrize("identifier", ["raspbian", "debian", "ubuntu"])
def test_the_named_platforms_pass(tmp_path: Path, identifier: str) -> None:
    layout = layout_of(tmp_path)
    checks = assess_box(layout, machine(layout, os_release=OsRelease(id=identifier)))
    assert verdict_of(checks, "platform") is Verdict.OK


def test_raspbian_inherits_debian(tmp_path: Path) -> None:
    """The image says `raspbian`; a check that only matched `debian` would call it unsupported."""

    layout = layout_of(tmp_path)
    checks = assess_box(
        layout, machine(layout, os_release=OsRelease(id="raspbian", id_like="debian"))
    )
    assert verdict_of(checks, "platform") is Verdict.OK


# -- service manager and binaries -----------------------------------------------


def test_no_systemd_fails_only_when_the_unit_was_asked_for(tmp_path: Path) -> None:
    layout = layout_of(tmp_path)
    absent = machine(layout, service=ServiceState(detail="systemd is not running on this machine"))
    assert verdict_of(assess_box(layout, absent, service=True), "service manager") is Verdict.FAIL
    assert verdict_of(assess_box(layout, absent, service=False), "service manager") is Verdict.OK


def test_a_missing_mpv_fails_and_names_the_package(tmp_path: Path) -> None:
    layout = layout_of(tmp_path)
    checks = assess_box(layout, machine(layout, binaries={"mpv": None, "uv": UV}))
    assert verdict_of(checks, "binaries") is Verdict.FAIL
    assert "mpv" in finding(checks, "binaries")


def test_a_missing_uv_fails_an_install_and_only_warns_a_verify(tmp_path: Path) -> None:
    """An install cannot build an environment; a verify is describing one that is already built."""

    layout = layout_of(tmp_path)
    state = machine(layout, binaries={"mpv": MPV, "uv": None})
    assert verdict_of(assess_box(layout, state), "binaries") is Verdict.FAIL
    assert verdict_of(assess_box(layout, state, command=Command.VERIFY), "binaries") is Verdict.WARN


# -- the Python environment ------------------------------------------------------


def test_an_environment_with_no_entry_point_fails_acceptance(tmp_path: Path) -> None:
    """A venv that exists and cannot start is 13.4 broken, and the appliance will not say so."""

    layout = layout_of(tmp_path)
    checks = assess_box(layout, machine(layout, environment_runnable=False))
    assert verdict_of(checks, "environment") is Verdict.FAIL
    assert "encore-server" in finding(checks, "environment")


def test_an_unbuilt_machine_is_a_warning_not_a_failure(tmp_path: Path) -> None:
    layout = layout_of(tmp_path)
    checks = assess_box(
        layout,
        machine(layout, environment_runnable=False, present={}),
    )
    assert verdict_of(checks, "environment") is Verdict.WARN


# -- the service account ---------------------------------------------------------


def test_a_login_shell_on_the_service_account_fails(tmp_path: Path) -> None:
    """13.5's "cannot log in" is a requirement, and this is the only place it is checked."""

    layout = layout_of(tmp_path)
    checks = assess_box(
        layout,
        machine(
            layout,
            account=ServiceAccount(name="encore", shell="/bin/bash", home="/var/lib/encore"),
        ),
    )
    assert verdict_of(checks, "service user") is Verdict.FAIL
    assert "usermod" in finding(checks, "service user")


def test_an_account_outside_the_sound_group_warns_because_sound_is_the_product(
    tmp_path: Path,
) -> None:
    """The difference between healthy and quiet, which is the worst pair to confuse."""

    layout = layout_of(tmp_path)
    checks = assess_box(
        layout,
        machine(
            layout,
            account=ServiceAccount(
                name="encore", shell="/usr/sbin/nologin", home="/var/lib/encore", groups=()
            ),
        ),
    )
    assert verdict_of(checks, "service user") is Verdict.WARN
    assert "audio" in finding(checks, "service user")


def test_a_fresh_machine_without_the_account_is_fine(tmp_path: Path) -> None:
    """The plan creates it; a prerequisite that failed for its absence would be circular."""

    checks = assess_box(
        production, machine(production, account=None, user_exists=False), tmp_path=tmp_path
    )
    assert verdict_of(checks, "service user") is Verdict.OK


# -- ports, disk, music -----------------------------------------------------------


def test_a_port_held_by_a_stranger_fails(tmp_path: Path) -> None:
    layout = layout_of(tmp_path)
    checks = assess_box(layout, machine(layout, port_free=False))
    assert verdict_of(checks, "port") is Verdict.FAIL
    assert "--port" in finding(checks, "port")


def test_a_port_held_by_the_appliance_itself_is_fine(tmp_path: Path) -> None:
    """`verify` on a running box finds its own socket, and would otherwise always fail."""

    layout = layout_of(tmp_path)
    checks = assess_box(
        layout,
        machine(layout, port_free=False, service=ServiceState(installed=True, active=True)),
        command=Command.VERIFY,
    )
    assert verdict_of(checks, "port") is Verdict.OK
    assert "appliance itself" in finding(checks, "port")


def test_a_full_disk_warns_with_the_number(tmp_path: Path) -> None:
    """2 GiB is the floor for the environment; a library is a different conversation (13.2).

    A warning rather than a failure because "full" depends on what else is on the card, and 13.1
    does not promise a minimum — it promises an install that stops rather than corrupts.
    """

    checks = assess_box(
        production, machine(production, disk_free=512 * 1024 * 1024), tmp_path=tmp_path
    )
    assert verdict_of(checks, "disk") is Verdict.WARN
    assert "free" in finding(checks, "disk")


def test_an_absent_music_directory_warns_because_the_server_never_scans_it(tmp_path: Path) -> None:
    """ADR-010 in checklist form: the party continues, the next build does not."""

    layout = layout_of(tmp_path)
    checks = assess_box(layout, machine(layout, music_readable=False))
    assert verdict_of(checks, "music") is Verdict.WARN
    assert "encore-builder" in finding(checks, "music")


# -- configuration, library, administrator -------------------------------------------


def test_a_configuration_that_does_not_validate_fails_and_keeps_the_file(tmp_path: Path) -> None:
    layout = layout_of(tmp_path)
    checks = assess_box(layout, machine(layout, config_text="server:\n  port: eighty\n"))
    assert verdict_of(checks, "configuration") is Verdict.FAIL
    assert "will not overwrite" in finding(checks, "configuration")


def test_an_existing_valid_configuration_says_it_was_preserved(tmp_path: Path) -> None:
    layout = layout_of(tmp_path)
    from apps.installer.content import config_text

    checks = assess_box(
        layout, machine(layout, config_text=config_text(configuration(layout), layout))
    )
    assert verdict_of(checks, "configuration") is Verdict.OK
    assert "Preserved" in finding(checks, "configuration")


def test_an_absent_library_warns_and_names_the_builder(tmp_path: Path) -> None:
    """No library is an appliance that works and plays nothing: 13.11 passes, the party does not."""

    layout = layout_of(tmp_path)
    database = library(layout.root)
    database.unlink()
    checks = assess_box(
        layout,
        machine(layout),
        config=configuration(layout),
        library_db=database,
        runtime_db=runtime(layout.root),
    )
    assert verdict_of(checks, "library") is Verdict.WARN
    assert "encore-builder" in finding(checks, "library")


def test_a_library_that_cannot_be_opened_fails(tmp_path: Path) -> None:
    layout = layout_of(tmp_path)
    path = layout.staged("/var/lib/encore/library.db")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not a database, a poem about one")
    checks = assess_box(layout, library_db=path)
    assert verdict_of(checks, "library") is Verdict.FAIL


def test_a_library_with_no_fts_index_warns_about_search(tmp_path: Path) -> None:
    """Search would answer with LIKE, slowly, and 11.2's 100 ms target would be missed silently."""

    layout = layout_of(tmp_path)
    checks = assess_box(layout, library_db=library(layout.root, indexed=False))
    assert verdict_of(checks, "library") is Verdict.WARN
    assert "search" in finding(checks, "library").lower()


def test_the_song_count_is_reported_as_evidence(tmp_path: Path) -> None:
    layout = layout_of(tmp_path)
    checks = assess_box(layout, library_db=library(layout.root, songs=42))
    assert "42 songs" in finding(checks, "library")


def test_an_absent_runtime_database_warns_about_the_first_boot(tmp_path: Path) -> None:
    layout = layout_of(tmp_path)
    path = runtime(layout.root)
    path.unlink()
    checks = assess_box(layout, runtime_db=path)
    assert verdict_of(checks, "administrator") is Verdict.WARN
    assert "ENCORE_ADMIN_PASSWORD" in finding(checks, "administrator")


def test_an_empty_admin_state_warns_rather_than_fails(tmp_path: Path) -> None:
    """The guest interface works and `/admin` will ask for a password on first use (ADR-015)."""

    layout = layout_of(tmp_path)
    checks = assess_box(layout, runtime_db=runtime(layout.root, administrator=False))
    assert verdict_of(checks, "administrator") is Verdict.WARN


# -- service and health --------------------------------------------------------------


def test_a_service_that_is_installed_but_disabled_is_named_exactly(tmp_path: Path) -> None:
    """The 11pm question is "why didn't it come back after the power cut", and `enabled` is it."""

    layout = layout_of(tmp_path)
    checks = assess_box(
        layout,
        machine(layout, service=ServiceState(installed=True, enabled=False, active=False)),
        service=True,
    )
    assert verdict_of(checks, "at boot") is Verdict.WARN
    assert "systemctl enable" in finding(checks, "at boot")


def test_a_running_appliance_that_does_not_answer_fails(tmp_path: Path) -> None:
    """systemd says active and the socket says otherwise; 13.11 is about the second."""

    layout = layout_of(tmp_path)
    checks = assess_box(
        layout,
        machine(
            layout, service=ServiceState(installed=True, enabled=True, active=True), health=None
        ),
        command=Command.VERIFY,
        service=True,
    )
    assert verdict_of(checks, "health") is Verdict.FAIL


def test_a_degraded_component_is_listed_by_name(tmp_path: Path) -> None:
    """ "degraded" alone sends an operator to read four things; the component name does not."""

    layout = layout_of(tmp_path)
    checks = assess_box(
        layout,
        machine(
            layout,
            service=ServiceState(installed=True, enabled=True, active=True),
            health={
                "status": "degraded",
                "components": [
                    {"component": "mpv", "status": "unhealthy", "detail": "no ipc socket"},
                    {"component": "library", "status": "healthy", "detail": ""},
                ],
            },
        ),
        command=Command.VERIFY,
        service=True,
    )
    assert verdict_of(checks, "health") is Verdict.WARN
    assert any(check.name == "health: mpv" for check in checks)
    assert not any(check.name == "health: library" for check in checks)


def test_a_stopped_appliance_is_not_probed(tmp_path: Path) -> None:
    """Nothing is listening on a first install, and a timeout there is the slowest step."""

    layout = layout_of(tmp_path)
    checks = assess_box(
        layout, machine(layout, service=ServiceState(), health=None), command=Command.VERIFY
    )
    assert not [check for check in checks if check.name.startswith("health")]
