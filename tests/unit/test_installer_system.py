"""`apps/installer/system.py`, against a machine that is not this one.

The other installer suites test what the installer *decides*. This one tests the module that
asks the questions those decisions are made of, which had no file of its own until #38 — the bug
being an installer that reported "uv is missing" on a machine that had it, in a directory that was
correct and invisible to root. A seam nobody tests is a seam that drifts from the machine it
stands in for, and this seam stands in for the one thing the installer cannot otherwise do: name
where a binary is.

Nothing here runs a real `useradd` or `systemctl`, and nothing depends on what is installed on the
computer running pytest: the environments are literals and the binaries are files in `tmp_path`.
"""

from __future__ import annotations

import os
import pwd
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from apps.installer.system import (
    USER_BIN_DIRS,
    CommandResult,
    RealMachine,
    SubprocessRunner,
    search_path,
)

pytestmark = pytest.mark.unit

#: The `secure_path` sudo installs on Debian and Ubuntu, and the reason #38 exists: it contains
#: no user's home directory, so a per-user `uv` is not on it.
ROOT_SECURE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

#: What `SUDO_USER` looks like to a root process, as opposed to `HOME`, which sudo points at /root.
OPERATOR = "encoreop"


def environ_of(values: Mapping[str, str]) -> property:
    """A `Machine.environ` that answers from a literal.

    `search_path` and `which` take the environment as data — the seam #38 needed to be testable
    at all — so a test replaces one property rather than the OS, and nothing in this file depends
    on what the machine running pytest happens to have installed.
    """

    return property(lambda _: values)


def executable(path: Path) -> Path:
    """A file that could be run: present, with the execute bit, and echoing its own `PATH`.

    Printing rather than exiting silently is what lets a test assert on the environment a child
    actually received instead than on the one the code assembled — the difference between testing
    a value and testing that a value survived being handed over.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('#!/bin/sh\nprintf "%s" "$PATH"\n')
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


# -- the search path -----------------------------------------------------------


def test_the_inherited_path_comes_first() -> None:
    """System directories win.

    Prepending the candidates would let anything in a home directory shadow `/usr/bin/mpv`, and
    an installer running as root on a machine holding somebody's library would then execute it.
    The order is a security property, not a preference.
    """

    found = search_path({"PATH": "/usr/bin:/bin", "HOME": "/home/anyone"}).split(os.pathsep)

    assert found[:2] == ["/usr/bin", "/bin"]
    assert "/home/anyone/.local/bin" in found


def test_a_root_path_finds_a_per_user_install(monkeypatch: Any) -> None:
    """#38, as one call: sudo's PATH, the operator's home named by `SUDO_USER`.

    This is the whole bug in miniature. Nothing in the environment says `/home/encoreop` —
    `HOME` is `/root` — and the only pointer is `SUDO_USER`, which is why resolving that through
    the password database is the fix rather than widening `PATH` to everything.
    """

    class Entry:
        pw_dir = "/home/encoreop"

    monkeypatch.setattr(pwd, "getpwnam", lambda _: Entry())
    found = search_path({"PATH": ROOT_SECURE_PATH, "HOME": "/root", "SUDO_USER": OPERATOR})

    assert "/home/encoreop/.local/bin" in found.split(os.pathsep)


def test_the_documented_install_locations_are_the_ones_searched() -> None:
    """Not a guess at the machine: these are where the tools' own installers write.

    `USER_BIN_DIRS` is asserted rather than trusted, because the astral installer's default is a
    fact about a third-party program and it can change without our tests noticing.
    """

    assert ".local/bin" in USER_BIN_DIRS, "where https://astral.sh/uv/install.sh puts uv"
    assert ".cargo/bin" in USER_BIN_DIRS, "where an older uv, or `cargo install uv`, leaves it"

    found = search_path({"PATH": "", "HOME": "/home/encoreop"}).split(os.pathsep)
    assert all(f"/home/encoreop/{directory}" in found for directory in USER_BIN_DIRS)


def test_an_empty_component_never_reaches_a_child_path() -> None:
    """An empty `PATH` element means "here" to a shell.

    `env PATH=…` with a stray separator makes a root process consult its working directory, which
    is how a directory someone else can write becomes a privilege escalation. The installer is
    often run from a clone, so "here" is a real place with real files in it.
    """

    found = search_path({"PATH": "/usr/bin::", "HOME": ""}).split(os.pathsep)

    assert "" not in found
    assert found == ["/usr/bin"]


def test_a_directory_is_not_listed_twice() -> None:
    """`HOME=/root` and `SUDO_USER=root` are the same account, and a report prints this string."""

    found = search_path({"PATH": "/root/.local/bin:/usr/bin", "HOME": "/root", "SUDO_USER": "root"})

    assert len(found.split(os.pathsep)) == len(set(found.split(os.pathsep)))


def test_a_sudo_user_that_no_longer_exists_changes_nothing(monkeypatch: Any) -> None:
    """`SUDO_USER` is an environment variable, so it can name anyone or nothing.

    The home is looked up rather than derived, and a name with no passwd entry — a deleted
    account, or an outer shell that set it — contributes nothing instead of raising. A prerequisite
    check that throws is an installer that cannot say what failed.
    """

    def unknown(name: str) -> Any:
        raise KeyError(name)

    monkeypatch.setattr(pwd, "getpwnam", unknown)
    found = search_path(
        {"PATH": "/usr/bin", "HOME": "/root", "SUDO_USER": "no-such-user-anywhere"}
    ).split(os.pathsep)

    # `HOME`'s candidates are still there; it is the unresolvable account that adds nothing.
    assert found == ["/usr/bin", "/root/.local/bin", "/root/bin", "/root/.cargo/bin"]
    assert not [directory for directory in found if "no-such-user" in directory]


# -- `which`, against that path ------------------------------------------------


def test_which_uses_the_environment_it_was_given(tmp_path: Path, monkeypatch: Any) -> None:
    """The observable version of #38: a file that exists, in a directory `PATH` omits.

    `HOME` is pointed at a temporary tree so no test depends on what the computer running pytest
    has installed for its own reasons — the same rule `FakeMachine.which` states about the
    appliance, applied to the machine this one is standing on.
    """

    installed = executable(tmp_path / ".local/bin/uv")
    monkeypatch.setattr(
        RealMachine,
        "environ",
        environ_of({"PATH": "/nonexistent-system-dir", "HOME": str(tmp_path)}),
    )

    assert RealMachine().which("uv") == installed


def test_which_reports_nothing_for_a_binary_that_is_not_there(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setattr(
        RealMachine, "environ", environ_of({"PATH": str(tmp_path), "HOME": str(tmp_path)})
    )

    assert RealMachine().which("uv") is None


def test_a_file_that_exists_but_is_not_executable_is_not_found(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """`shutil.which` checks the mode, and so does this: a downloaded-and-un-tarred uv is not a
    reason to plan a `uv sync` that will fail with EACCES."""

    placeholder = tmp_path / ".local/bin/uv"
    placeholder.parent.mkdir(parents=True)
    placeholder.write_text("not yet executable")

    monkeypatch.setattr(RealMachine, "environ", environ_of({"PATH": "", "HOME": str(tmp_path)}))

    assert RealMachine().which("uv") is None


# -- what a child process inherits --------------------------------------------


def test_a_command_resolves_through_the_same_path_the_check_used(tmp_path: Path) -> None:
    """One rule for "is it there?" and "run it", or the two disagree on the machine that matters.

    A `PATH` handed to a child that the checker never searched is the second half of #38: the
    installer could have gone on to `uv sync` with a bare program name and a root `PATH`, and
    produced a 127 in the middle of the plan rather than a prerequisite message before it.
    """

    asked: list[str] = []

    class Recorder(SubprocessRunner):
        """Wraps the production runner so the environment it builds is the one on trial."""

        def run(self, argv: Any, *, env: Any = None, cwd: Any = None) -> CommandResult:
            result = super().run(argv, env=env, cwd=cwd)
            asked.append(result.stdout.strip())
            return result

    # `printenv` prints its own PATH, and the absolute name keeps this test about what the runner
    # hands over rather than about whether the runner could find a program.
    Recorder().run([str(Path("/usr/bin/printenv")), "PATH"])

    assert asked, "the runner never reported what its child saw"
    assert set(asked[0].split(os.pathsep)) >= set(
        search_path({"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "")}).split(
            os.pathsep
        )
    )


def test_a_caller_supplied_environment_still_gets_the_searched_path(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """`env=` adds, it does not replace — and `PATH` among them is still the augmented one.

    `uv sync` is the caller-supplied case (`UV_PROJECT_ENVIRONMENT`), and `uv` is exactly the
    binary whose location this whole file is about.
    """

    home = executable(tmp_path / "fake-home/.local/bin/probe")
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))

    result = SubprocessRunner().run(
        [str(home), "PATH"], env={"UV_PROJECT_ENVIRONMENT": str(tmp_path / "venv")}
    )

    assert result.ok, result.output()
    assert str(tmp_path / "fake-home/.local/bin") in result.stdout.split(os.pathsep)
    assert str(tmp_path / "venv") not in result.stdout, "the extra variable is not a path entry"


def test_the_real_environment_is_not_reduced_by_a_home_that_points_at_a_binary_dir(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """A `HOME` whose candidates *are* the system directories adds nothing and breaks nothing."""

    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    executable(tmp_path / ".local/bin/whichprobe")

    assert RealMachine().which("whichprobe") == tmp_path / ".local/bin/whichprobe"
