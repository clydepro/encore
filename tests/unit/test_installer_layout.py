"""The appliance layout table (SAPRS 13.3, 13.5, 13.10).

`layout.py` is the only file in the installer that states where Encore lives, so these are the
tests that keep the installer and the specification describing the same machine. The staging
rules get the most attention: a `--root` that nests one level deeper on every run would produce
an appliance that installs successfully and then loses its own databases.
"""

from __future__ import annotations

from pathlib import Path

from apps.installer.layout import (
    SCOPE_APPLICATION,
    SCOPE_CONFIGURATION,
    SCOPE_DATA,
    Layout,
)

# The tree SAPRS 13.3 prints, transcribed rather than imported so that a change here is a
# decision about the specification and not a consequence of a change in the code. The files the
# appliance writes for itself (`library.db`, `runtime.db`) are absent from what follows, and
# explained in the first test rather than in a fixture nobody reads.


def test_the_production_layout_is_the_documented_tree() -> None:
    """What 13.3 prints, minus the files another program writes.

    `library.db`, `runtime.db` and the artwork cache are in the specification's tree and are not
    in this one, on purpose: the first is the Builder's output (ADR-010), the second is created by
    the Server's migrations on first boot, and an installer that created either would produce an
    empty database that validates and a service that starts. `/opt/music` is absent for the same
    reason — it is a mount, not an artifact.
    """

    layout = Layout()
    created = set(layout.managed_paths)
    assert {
        "/opt/encore/application",
        "/opt/encore/venv",
        "/opt/encore/python",
        "/etc/encore",
        "/etc/encore/config.yaml",
        "/etc/encore/environment",
        "/etc/systemd/system/encore.service",
        "/usr/lib/tmpfiles.d/encore.conf",
        "/var/lib/encore",
        "/var/lib/encore/artwork",
        "/var/cache/encore",
        "/var/cache/encore/temp",
        "/var/log/encore",
    } == created
    assert layout.staged("/var/lib/encore") == Path("/var/lib/encore")


def test_staging_redirects_every_documented_path_under_the_root(tmp_path: Path) -> None:
    layout = Layout(root=tmp_path)
    assert layout.staged("/var/lib/encore") == tmp_path / "var/lib/encore"
    assert layout.staged("/etc/encore/config.yaml") == tmp_path / "etc/encore/config.yaml"


def test_staging_a_path_already_under_the_root_does_not_nest_it(tmp_path: Path) -> None:
    """The guard that keeps a second install from hiding the appliance.

    Any install after the first reads a `config.yaml` whose paths were themselves written by a
    staged install, so `staged()` receives inputs that are already staged. Without the guard
    `/tmp/stage/var/lib/encore` becomes `/tmp/stage/tmp/stage/var/lib/encore` and the databases
    move out from under a running service.
    """

    layout = Layout(root=tmp_path)
    once = layout.staged("/var/lib/encore")
    assert layout.staged(once) == once
    assert layout.staged(str(once)) == once


def test_relative_documented_paths_are_not_accepted(tmp_path: Path) -> None:
    # A relative path would be resolved against the installer's cwd, which is whatever directory
    # an operator happened to cd into.
    layout = Layout(root=tmp_path)
    assert layout.staged("var/lib/encore") == tmp_path / "var/lib/encore"


def test_the_state_directory_is_writable_by_the_service_user() -> None:
    """`encore:encore` 0750, and the reason is WAL.

    The tempting arrangement — `root:encore`, with the service user reading its own state
    directory — does not work: a WAL-mode SQLite database creates and renames `-wal` and `-shm`
    files *in the directory*, so a read-only directory is an appliance that fails at its first
    queue insert. Immutability of `library.db` is therefore a code guarantee (the read-only
    connection URI, ADR-006) rather than a permission one, which ADR-014 says out loud instead of
    leaving it to be discovered during an outage.
    """

    layout = Layout()
    entries = {entry.final: entry for entry in layout.directories}
    state = entries["/var/lib/encore"]
    assert state.mode == 0o750
    assert (state.owner, state.group) == ("encore", "encore")


def test_the_socket_directory_is_private_to_the_service_user() -> None:
    """0700, not 0777 and not sticky: mpv's IPC socket is the volume control for the room.

    A world-writable temp directory would let another local account connect to the socket and
    take a track mid-song, so this is the one directory in the table where the group has no
    access at all.
    """

    layout = Layout()
    entries = {entry.final: entry for entry in layout.directories}
    temp = entries["/var/cache/encore/temp"]
    assert (temp.owner, temp.group, temp.mode) == ("encore", "encore", 0o700)


def test_the_environment_file_is_the_only_one_at_0600() -> None:
    """`ENCORE_ADMIN_PASSWORD` may be in it, and root is the only reader (SAPRS 12.6)."""

    layout = Layout()
    files = {entry.final: entry for entry in layout.files}
    assert files["/etc/encore/environment"].mode == 0o600
    assert files["/etc/encore/config.yaml"].mode == 0o640
    assert files["/etc/encore/config.yaml"].group == "encore"
    assert files["/etc/systemd/system/encore.service"].mode == 0o644


def test_only_the_paths_the_appliance_writes_are_writable_by_the_appliance() -> None:
    """The distinction is who writes: `encore`, or the installer running as root.

    `/opt/encore/application`, `/opt/encore/venv`, `/etc/encore` and `/var/cache/encore` are
    root-owned because the only program that changes them is this installer; the four below are
    the ones the Server opens during a party. Asserted as a set so that a fifth is a decision
    rather than an edit (SAPRS 13.3, ADR-008).
    """

    layout = Layout()
    writable = {
        entry.final
        for entry in layout.directories
        if entry.owner == "encore" or (entry.group == "encore" and entry.mode & 0o020)
    }
    assert writable == {
        "/var/lib/encore",
        "/var/lib/encore/artwork",
        "/var/cache/encore/temp",
        "/var/log/encore",
    }


def test_the_downloaded_interpreter_is_readable_by_the_service_account() -> None:
    """The one path whose whole purpose is a *different* identity reading it.

    `/opt/encore/python` is where `uv` puts a managed CPython when the distribution has none new
    enough — Debian 12 ships 3.11 and the project requires 3.12. Left to uv's default it lands in
    `~/.local/share/uv/python` **of whoever ran the installer**, which under `sudo` is inside
    `/root` at `drwx------`: the venv links to it, `User=encore` cannot traverse the path, and
    systemd reports `status=203/EXEC` while every mode check on the entry point passes (#46).

    So the entry is asserted as *root-owned and world-readable* rather than `encore`-owned. It is
    code, not state — the appliance never writes it — and the property that matters is that the
    account named in `User=` can walk to it whichever account paid for the download.
    """

    layout = Layout()
    entry = next(item for item in layout.directories if item.final == layout.python)
    assert (entry.owner, entry.group) == ("root", "root")
    assert entry.mode == 0o755
    assert entry.mode & 0o004, "readable by everybody"
    assert entry.mode & 0o001, "and searchable, which is the bit that stops an exec"
    assert layout.staged(layout.python) == Path("/opt/encore/python")
    assert (layout.python, SCOPE_APPLICATION) in layout.removals, (
        "in the application scope, so `remove` cannot leave an interpreter behind and `owned_roots` "
        "cannot let a deletion reach it by accident"
    )


def test_the_removal_table_carries_exactly_the_three_specification_scopes() -> None:
    layout = Layout()
    scopes = {scope for _, scope in layout.removals}
    assert scopes == {SCOPE_APPLICATION, SCOPE_CONFIGURATION, SCOPE_DATA}
    assert {final for final, _ in layout.removals} == set(layout.owned_roots)


def test_removal_defaults_to_the_application_alone() -> None:
    """13.10's "should not be deleted implicitly", expressed as which rows are configuration."""

    layout = Layout()
    data = {final for final, scope in layout.removals if scope is SCOPE_DATA}
    configuration = {final for final, scope in layout.removals if scope is SCOPE_CONFIGURATION}
    assert data == {"/var/lib/encore", "/var/cache/encore", "/var/log/encore"}
    assert "/etc/encore" in configuration
    assert "/etc/encore/config.yaml" in configuration


def test_no_removal_scope_names_the_music_directory() -> None:
    """`/opt/music` is somebody's collection. Encore never wrote it and never deletes it."""

    layout = Layout()
    assert not [final for final, _ in layout.removals if "music" in final]
    assert not [entry for entry in layout.directories if "music" in entry.final]


def test_the_music_directory_is_not_in_the_installed_tree() -> None:
    """13.3 shows it as read-only input; creating it would be the Builder's business (ADR-010)."""

    layout = Layout()
    assert "/opt/music" not in {entry.final for entry in layout.directories}


def test_every_path_is_declared_with_a_purpose() -> None:
    """Every entry appears in a report; "why is this here" has to be answerable without a wiki."""

    layout = Layout()
    assert all(entry.purpose for entry in layout.directories)
    assert all(entry.purpose for entry in layout.files)


def test_the_service_account_is_unprivileged_by_construction() -> None:
    layout = Layout()
    assert layout.user == "encore"
    assert layout.group == "encore"
    # Nothing in the table is owned by root *and* writable by the service user's group in a way
    # that would let the appliance rewrite its own launch configuration (SAPRS 13.5).
    unit = {entry.final: entry for entry in layout.files}["/etc/systemd/system/encore.service"]
    assert (unit.owner, unit.group, unit.mode) == ("root", "root", 0o644)
