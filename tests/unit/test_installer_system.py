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

import grp
import os
import pwd
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import pytest

from apps.installer.system import (
    USER_BIN_DIRS,
    CommandResult,
    Machine,
    PathState,
    RealMachine,
    ServiceAccount,
    SubprocessRunner,
    interpreter_blocker,
    interpreter_of,
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


def test_a_shebang_names_the_program_the_kernel_will_actually_run() -> None:
    """The line that turns "is this file executable" into a question about a different file.

    `#!/bin/sh` at the top of a console script is not a comment: systemd execs it, the kernel reads
    it, and the answer to "can the appliance start" moves to wherever that path leads. An
    `env`-style shebang resolves against the invoking account's `PATH`, which is not a fact about
    this filesystem, so it is answered with None rather than with a guess.
    """

    assert interpreter_of("#!/opt/encore/venv/bin/python\nimport sys\n") == Path(
        "/opt/encore/venv/bin/python"
    )
    assert interpreter_of("#!/bin/sh") == Path("/bin/sh")
    assert interpreter_of("#!/usr/bin/env python3") is None
    assert interpreter_of("#!/opt/encore/venv/bin/python  -E ") == Path(
        "/opt/encore/venv/bin/python"
    )
    assert interpreter_of("no shebang here") is None
    assert interpreter_of(None) is None, "a path root could not read is a question, not an answer"


class _Table:
    """A machine that answers `stat` from a table, because the arithmetic is the point.

    Built out of path facts rather than out of a real filesystem so that a directory can be
    `drwx------` on a machine where the test process is root and could walk through it anyway —
    which is #46: the account that matters is the one in `User=`, never the one running the check.

    Only `stat` is implemented, which is the only question `interpreter_blocker` asks. A full
    `Machine` here would be a double answering questions this function does not pose.
    """

    def __init__(self, **paths: tuple[str, str, int]) -> None:
        self._paths = {
            Path(final): PathState(
                path=Path(final),
                exists=True,
                is_dir=not final.rsplit("/", 1)[-1].count("."),
                owner=owner,
                group=group,
                mode=mode,
            )
            for final, (owner, group, mode) in paths.items()
        }

    def stat(self, path: Path) -> PathState:
        return self._paths.get(path, PathState(path=path))


def table(**paths: tuple[str, str, int]) -> Machine:
    """`_Table` as the protocol asks for it.

    A cast rather than an implementation: `interpreter_blocker` puts one question to a machine, and
    a double that also answered `systemctl` would be claiming more than it knows.
    """

    return cast("Machine", _Table(**paths))


ENCORE = ServiceAccount(
    name="encore", shell="/usr/sbin/nologin", home="/var/lib/encore", group="encore"
)


def test_a_private_directory_blocks_a_service_account_below_it() -> None:
    """#46's exact machine: the interpreter is there, readable, and unreachable.

    Every mode bit on `/opt/encore/venv/bin/python` says executable. What stops the appliance is
    `/root` above it, which is `drwx------` on Debian by default and is where uv put a downloaded
    CPython because root ran the install.
    """

    machine = table(
        **{
            "/": ("root", "root", 0o755),
            "/root": ("root", "root", 0o700),
            "/root/.local": ("root", "root", 0o755),
            "/root/.local/share": ("root", "root", 0o700),
            "/root/.local/share/uv": ("root", "root", 0o755),
            "/root/.local/share/uv/python": ("root", "root", 0o755),
            "/root/.local/share/uv/python/cpython-3.12": ("root", "root", 0o755),
            "/root/.local/share/uv/python/cpython-3.12/bin": ("root", "root", 0o755),
            "/root/.local/share/uv/python/cpython-3.12/bin/python3.12": ("root", "root", 0o755),
        }
    )
    blocked = Path("/root/.local/share/uv/python/cpython-3.12/bin/python3.12")
    assert interpreter_blocker(machine, blocked, account=ENCORE) == Path("/root")


def test_the_same_file_is_reachable_when_everything_above_it_is_searchable() -> None:
    """The fix, as one line of table: same modes on the file, `/opt/encore/python` above it."""

    machine = table(
        **{
            "/": ("root", "root", 0o755),
            "/opt": ("root", "root", 0o755),
            "/opt/encore": ("root", "root", 0o755),
            "/opt/encore/python": ("root", "root", 0o755),
            "/opt/encore/python/cpython-3.12": ("root", "root", 0o755),
            "/opt/encore/python/cpython-3.12/bin": ("root", "root", 0o755),
            "/opt/encore/python/cpython-3.12/bin/python3.12": ("root", "root", 0o755),
        }
    )
    target = Path("/opt/encore/python/cpython-3.12/bin/python3.12")
    assert interpreter_blocker(machine, target, account=ENCORE) is None


def test_the_owner_the_group_and_everybody_are_three_different_answers() -> None:
    """Which triad applies is decided by who the account *is*, not by what the mode looks like.

    Three machines, one bit each, and a wrong choice among them is a check that passes a broken
    appliance or fails a working one:
    """

    def one(mode: int, *, owner: str = "root", group: str = "root") -> Path | None:
        machine = table(
            **{
                "/": ("root", "root", 0o755),
                "/srv": (owner, group, mode),
                "/srv/python": ("root", "root", 0o755),
            }
        )
        return interpreter_blocker(machine, Path("/srv/python"), account=ENCORE)

    assert one(0o750) == Path("/srv"), "0750 root:root says nothing to encore"
    assert one(0o750, group="encore") is None, "the primary group is a real answer"
    assert one(0o710, group="encore") is None, "search without read is enough to exec"
    assert one(0o705) is None, "other-x is enough, and an ACL-free 0705 is a shared directory"
    assert one(0o700) == Path("/srv"), "and 0700 is /root"


def test_a_supplementary_group_counts_because_that_is_how_audio_is_granted() -> None:
    """Membership is asked of the account's groups, not of its primary one.

    The appliance's own privilege model is supplementary (13.5: `encore` joins `audio`), so a check
    that looked only at `pw_gid` would disagree with the machine about half of what 13.5 sets up.
    """

    machine = table(
        **{
            "/": ("root", "root", 0o755),
            "/srv": ("root", "shared", 0o750),
            "/srv/python": ("root", "root", 0o755),
        }
    )
    account = ServiceAccount(name="encore", shell="/usr/sbin/nologin", home="/x", group="encore")
    assert interpreter_blocker(machine, Path("/srv/python"), account=account) == Path("/srv")
    member = ServiceAccount(
        name="encore", shell="/usr/sbin/nologin", home="/x", group="encore", groups=("shared",)
    )
    assert interpreter_blocker(machine, Path("/srv/python"), account=member) is None


def test_a_path_that_stops_existing_blocks_at_the_component_where_it_stops(tmp_path: Path) -> None:
    """A dangling symlink blocks at the link, not at a mode it never got to consult.

    `uv` removes its managed interpreter on `clean`, and an upgrade that relinked a venv to a
    directory mid-removal produces exactly this: the entry point is executable, and there is
    nothing at the other end of it.
    """

    missing = tmp_path / "venv" / "bin" / "python"
    assert interpreter_blocker(RealMachine(), missing, account=ENCORE) is not None


def test_the_arithmetic_agrees_with_the_kernel_on_this_filesystem(tmp_path: Path) -> None:
    """The predicate is a reimplementation of `access(2)`, so it is checked against `access(2)`.

    Not against a table: for the identity that *can* be used here — the one running the test —
    `os.access` is the kernel answering the same question, on real directories with real modes.
    Where they disagree, `_grants` is wrong and this is the file that says so.
    """

    account = ServiceAccount(
        name=pwd.getpwuid(os.getuid()).pw_name,
        shell="/bin/sh",
        home="/",
        group=grp.getgrgid(os.getgid()).gr_name,
    )
    for mode, label in ((0o755, "shared"), (0o700, "private"), (0o750, "group")):
        home = tmp_path / label
        home.mkdir()
        home.chmod(mode)
        interpreter = home / "python"
        interpreter.write_text("#!/bin/sh\n")
        interpreter.chmod(0o755)
        blocked = interpreter_blocker(RealMachine(), interpreter, account=account)
        assert (blocked is None) == os.access(interpreter, os.X_OK), f"{label}: {mode:o}"
