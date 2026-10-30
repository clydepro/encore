"""The machine, as the installer is allowed to know it (SAPRS 13.1, 13.7).

Two seams, because "install Encore" is two kinds of act: **looking** and **doing**. Both are
deliberately narrow, and the reason is testability rather than tidiness. There is no CI
runner that is a Raspberry Pi with systemd, a sound card and a root shell, so an installer
that asks the machine questions in the middle of its own logic is an installer whose
interesting branches never get run anywhere.

* `CommandRunner` is the only way a process gets started. Production runs `subprocess`; a
  test records what was asked and answers from a table.
* `Machine` is the only way anything is *observed*: the platform, the accounts, the
  binaries, the state of a unit, the mode of a directory, whether a port is free.

`observe()` asks both once, into an `ApplianceState` that `plan.py` can reason about as
data. The rule that keeps this module honest is that it decides nothing: `prerequisites.py`
and `plan.py` are where a fact becomes a problem.
"""

from __future__ import annotations

import grp
import os
import pwd
import shutil
import socket
import stat
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import httpx

from apps.installer.application import Diff, difference, manifest
from apps.installer.layout import AUDIO_GROUP, Layout

__all__ = [
    "USER_BIN_DIRS",
    "ApplianceState",
    "CommandResult",
    "CommandRunner",
    "HttpProbe",
    "LiveHttpProbe",
    "Machine",
    "OsRelease",
    "PathState",
    "RealMachine",
    "ServiceState",
    "SubprocessRunner",
    "observe",
    "search_path",
]

#: What `systemctl` calls the appliance's unit. One constant, because the unit file, the
#: enable/start calls and `verify` have to mean the same service.
SERVICE_NAME = "encore"

#: Where PID 1 leaves the evidence that this is a systemd machine and not something else.
SYSTEMD_EVIDENCE = Path("/run/systemd/system")

#: The operating systems SAPRS 13.1 names. `/etc/os-release`'s `ID`, with `ID_LIKE` as the
#: fallback, because a Raspberry Pi OS image says `raspbian` and inherits `debian`.
SUPPORTED_OS_IDS = frozenset({"raspbian", "debian", "ubuntu"})

#: Binaries the installer needs to find, by the name an operator would type.
REQUIRED_BINARIES = ("mpv", "uv")

#: Where a per-user installation of a tool puts it, relative to a home directory.
#: `~/.local/bin` is what the astral `uv` installer writes (docs/Developer/Getting-Started.md),
#: and `~/.cargo/bin` is where an older uv or a `cargo install` leaves one; `~/bin` is the
#: Debian convention an operator who installs anything by hand follows. These are not guesses
#: at the machine: they are the documented destinations of the documented install step.
USER_BIN_DIRS = (".local/bin", "bin", ".cargo/bin")


def search_path(environ: Mapping[str, str]) -> str:
    """Every directory a host binary could legitimately be in, as a `PATH` string.

    One function, because a check that searches one list and a command that resolves through
    another is how an installer ends up reporting "`uv` is missing" on a machine that has it
    (#38). :meth:`RealMachine.which` and :class:`SubprocessRunner` both ask this, so "is it
    there?" and "run it" cannot disagree, and the answer a report prints is the answer the
    subprocess used.

    The inherited `PATH` comes first and the extra directories are appended. Prepending would
    let a home directory shadow `/usr/bin/mpv`, which turns an appliance into whatever the
    account that ran sudo last installed — and this program runs as root on a machine holding
    somebody's music collection.

    Args:
        environ: The environment to read `PATH`, `HOME` and `SUDO_USER` from. A parameter
            rather than `os.environ` because the machine under test is the appliance, not the
            runner that happens to be executing pytest.

    Returns:
        A colon-joined `PATH`, deduplicated, with empty entries removed — an empty component
        means "the current directory" to a shell, and a root process that consults it is a
        privilege escalation waiting for somebody to `cd` somewhere interesting.
    """

    found: list[str] = [part for part in environ.get("PATH", "").split(os.pathsep) if part]
    for home in _candidate_homes(environ):
        found.extend(str(Path(home) / directory) for directory in USER_BIN_DIRS)
    ordered: list[str] = []
    for directory in found:
        if directory not in ordered:
            ordered.append(directory)
    return os.pathsep.join(ordered)


def _candidate_homes(environ: Mapping[str, str]) -> tuple[str, ...]:
    """The home directories whose user-local binaries a root process should be able to see.

    `HOME` alone is not enough, and this is the whole of the bug. `sudo` runs the installer as
    root with `HOME=/root` and its own `secure_path`, so an operator who installed `uv` as
    themselves — which is what the documentation says to do, and what the astral installer
    does without a system-wide option being used — has put it somewhere root cannot see.
    `SUDO_USER`, which sudo sets, names that operator; looking their home up in the password
    database is the stdlib's answer, and reading `/etc/passwd` through `pwd` beats parsing a
    path out of an environment variable somebody could have set to anything.
    """

    homes: list[str] = []
    if home := environ.get("HOME"):
        homes.append(home)
    if user := environ.get("SUDO_USER"):
        try:
            record = pwd.getpwnam(user)
        except KeyError:  # a removed account, or a name sudo inherited from a parent shell
            return tuple(homes)
        if record.pw_dir and record.pw_dir not in homes:
            homes.append(record.pw_dir)
    return tuple(homes)


def interpreter_of(shebang: str | None) -> Path | None:
    """The interpreter a script names for itself, or None where the question has no answer.

    A console script is not a program; it is a one-line instruction to the kernel about which
    program to run. `/opt/encore/venv/bin/encore-server` is executable by everyone and tells the
    kernel to run `venv/bin/python`, which is a symlink, which is where #46 lives. An env-style
    shebang resolves through the invoking account's `PATH`, which is not a question about this
    filesystem, so it is answered with None rather than with a guess.
    """

    if shebang is None or not shebang.startswith("#!"):
        return None
    named = shebang.removeprefix("#!").split(maxsplit=1)[:1]
    if not named or not named[0].startswith("/") or named[0].rsplit("/", 1)[-1] == "env":
        return None
    return Path(named[0])


def interpreter_blocker(machine: Machine, target: Path, account: ServiceAccount) -> Path | None:
    """The first component of `target` that `account` cannot get through, or None if it can.

    This is the kernel's rule, stated as data: a file is executable by an identity only if every
    directory above it is searchable by that identity, and `mode & 0o111` on the file itself is
    silent about both. `/opt/encore/venv/bin/encore-server` carries `0755` and the appliance still
    dies at exec, because the interpreter behind its shebang sits under `/root`, which is
    `drwx------` (#46).

    Args:
        machine: Asked for each component's owner, group and mode — never for a verdict, so the
            arithmetic below is the only place it is written.
        target: The interpreter, still a symlink. `stat` follows it, which is what the kernel does
            at exec time.
        account: Whose permissions matter, because `User=` in the unit names this identity and no
            other. A root-installed process is not the process that runs the appliance.
    """

    for component in _components(target):
        state = machine.stat(component)
        if not state.exists:
            return component
        # A directory has to be searchable; the file itself has to be readable as well, because
        # the kernel reads a script's shebang before it decides what to exec.
        if _grants(state, account=account, readable=component == target, executable=True):
            continue
        return component
    return None


def _components(target: Path) -> tuple[Path, ...]:
    """`/`, each directory above `target`, and `target` — the walk the kernel makes."""

    prefixes = tuple(Path(*target.parts[: index + 1]) for index in range(len(target.parts) - 1))
    return (*prefixes, target)


def _grants(state: PathState, *, account: ServiceAccount, readable: bool, executable: bool) -> bool:
    """Whether one identity holds the requested bits on one path, by the DAC rule.

    Owner, then group, then other — first match wins, which is why a `0750 root:encore` directory
    grants nothing to an `encore` process whose primary group is `encore` and whose supplementary
    groups do not include it. ACLs and capabilities are not consulted; where they would matter this
    returns a false negative, and a false negative here reads as "the appliance cannot start", so
    the cost of being wrong is a warning the operator can dismiss rather than a green line over a
    dead service.
    """

    if state.owner == account.name:
        triad = (state.mode >> 6) & 0o7
    elif state.group == account.group or state.group in account.groups:
        triad = (state.mode >> 3) & 0o7
    else:
        triad = state.mode & 0o7
    return not (readable and not triad & 0o4) and not (executable and not triad & 0o1)


@dataclass(frozen=True, slots=True, kw_only=True)
class CommandResult:
    """One finished process. Nothing here is still running.

    Attributes:
        argv: The command as it was asked for, so a report can show what it cost and a test
            can assert on it.
        code: Exit status.
        stdout: Captured output, decoded leniently — a tool that prints a Latin-1 path is
            not a reason to fail an install.
        stderr: Where the fixable message usually is.
    """

    argv: tuple[str, ...]
    code: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.code == 0

    def output(self) -> str:
        """The first line a human would have read: stderr if it said anything, else stdout."""

        text = self.stderr.strip() or self.stdout.strip()
        return text.splitlines()[0] if text else ""


class CommandRunner(Protocol):
    """Start a process, wait for it, hand back the transcript. No shell, ever."""

    def run(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> CommandResult:
        """Execute `argv` literally.

        Args:
            argv: Program then arguments. Never one string: a command assembled into a string
                is a command an unquoted path can rewrite, and this installer runs as root on a
                machine holding somebody's music collection.
            env: Extra variables, layered over the installer's own environment.
            cwd: Where to run it. `uv` resolves a project from the working directory as well as
                from its flags, so an installer run from `/root` would otherwise sync whatever
                happened to be there.
        """
        ...


class SubprocessRunner:
    """The production `CommandRunner`."""

    def __init__(self, *, timeout: float = 300.0) -> None:
        self._timeout = timeout

    def run(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> CommandResult:
        parts = [str(part) for part in argv]
        program = parts[0]
        environment = dict(os.environ) | {key: str(value) for key, value in (env or {}).items()}
        # The child resolves a bare program name through `PATH`, so it has to be the same
        # `PATH` `which()` answered with; anything else is two answers to one question, which is
        # the shape of #38. Appended rather than prepended, by the same rule as `search_path`.
        environment["PATH"] = search_path(environment)
        try:
            completed = subprocess.run(  # noqa: S603 - argv is literal by this module's rule
                parts,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=self._timeout,
                env=environment,
                cwd=None if cwd is None else str(cwd),
                check=False,
            )
        except FileNotFoundError:
            return CommandResult(argv=tuple(parts), code=127, stderr=f"{program}: not found")
        except PermissionError:
            return CommandResult(
                argv=tuple(parts), code=126, stderr=f"{program}: permission denied"
            )
        except subprocess.TimeoutExpired:
            return CommandResult(
                argv=tuple(parts), code=124, stderr=f"{program}: no answer in {self._timeout:.0f}s"
            )
        return CommandResult(
            argv=tuple(parts),
            code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class OsRelease:
    """`/etc/os-release`, in the three fields SAPRS 13.1's platform list is expressed in."""

    id: str = ""
    id_like: str = ""
    pretty_name: str = ""

    @property
    def supported(self) -> bool:
        """Whether this is a platform the installer claims, rather than one it survives."""

        names = {self.id.strip('"'), *(part.strip('"') for part in self.id_like.split())}
        return bool(names & SUPPORTED_OS_IDS)

    def describe(self) -> str:
        return self.pretty_name or self.id or "an unrecognised Linux"


@dataclass(frozen=True, slots=True, kw_only=True)
class PathState:
    """What a path *is*, as distinct from what the layout wants it to be.

    Absent is a state rather than an error: half of idempotence is knowing the difference
    between "not there yet" and "there, and wrong", and the two need different actions.
    """

    path: Path
    exists: bool = False
    is_dir: bool = False
    owner: str = ""
    group: str = ""
    mode: int = 0

    def matches(self, *, owner: str, group: str, mode: int, ownership: bool = True) -> bool:
        """Whether this path already is what the layout asked for.

        The mode comparison is masked rather than exact: a directory carrying a setgid bit
        from a group-managed share, or an ACL's execute bit, is not an installation to fail
        over, and the bits that matter are the low ones.

        `ownership=False` is for the case where the account in question does not exist yet.
        Asking who owns `/var/lib/encore` before `encore` has been created finds drift on every
        fresh machine and then "repairs" it into a failure, so the question is only asked once
        there is somebody correct it could be right about.
        """

        return (
            self.exists
            and (not ownership or (self.owner == owner and self.group == group))
            and (self.mode & 0o7777) == (mode & 0o7777)
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ServiceState:
    """systemd's opinion about one unit, as three booleans that can disagree.

    `installed` is the file, `enabled` is the boot hook, `active` is the running process.
    They are separate because the failures are separate and the fixes are not alike: the
    operator asking "why didn't it come back after the power cut" needs the middle one.

    These are facts about *the unit*. Whether the machine has a service manager at all is
    `Machine.systemd_running`, and the two must not be inferred from each other: on the first
    install of an otherwise healthy Pi they are "no" and "yes" at once, and reading one as the
    other is #43 — an installer that refused to install a unit because the unit was not installed.
    """

    name: str = SERVICE_NAME
    installed: bool = False
    enabled: bool = False
    active: bool = False
    detail: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class ServiceAccount:
    """The password-database record, in the four fields 13.5's requirements are stated in.

    Its own type rather than `pwd.struct_passwd` so that a test can describe a machine with an
    `encore` account on it without constructing a C struct — and so that "no login shell" and
    "home is the state directory" arrive as named facts instead of as offsets.
    """

    name: str
    shell: str
    home: str
    groups: tuple[str, ...] = ()
    group: str = ""
    """The account's primary group, by name.

    Needed to answer a traversal question (#46): `drwx------` on a home directory stops everybody
    except the owner, but a `drwx--S---` shared directory does not, and the difference is the group
    the account belongs to. A name rather than a gid because every path fact in this module carries
    group *names*, and comparing a gid to a name is how a check quietly starts answering always-no.
    """

    @property
    def can_log_in(self) -> bool:
        """Whether this shell would open a session.

        A keyword list rather than a comparison against `/usr/sbin/nologin`, because
        distributions disagree about the path and about whether the answer is `/bin/false`; what
        they agree on is that neither of those two programs lets anybody in.
        """

        return self.shell.rsplit("/", maxsplit=1)[-1] not in {"nologin", "false"}


class Machine(Protocol):
    """Everything observable about the host. Implement this and the installer runs anywhere."""

    @property
    def commands(self) -> CommandRunner:
        """The seam that changes the host, exposed so it cannot disagree with the machine.

        Part of the protocol rather than a parameter of `main`, because the two halves of one
        effect — `systemctl` as the host reports it and `systemctl` as the executor runs it —
        must come from the same place. A test that scripted the questions and got real answers
        to the commands would be a test of nothing.
        """
        ...

    @property
    def environ(self) -> Mapping[str, str]: ...

    @property
    def running_as_root(self) -> bool: ...

    def os_release(self) -> OsRelease: ...

    def which(self, name: str) -> Path | None: ...

    def systemd_running(self) -> bool:
        """Whether this machine has the service manager SAPRS 13.6 assumes.

        Asked separately from `service`, because the two answers disagree on every machine that
        has not been installed yet (#43), and a check that infers one from the other fails a
        first install on hardware that meets every prerequisite.
        """
        ...

    def hostname(self) -> str: ...

    def user_exists(self, name: str) -> bool: ...

    def user(self, name: str) -> ServiceAccount | None: ...

    def group_exists(self, name: str) -> bool: ...

    def stat(self, path: Path) -> PathState: ...

    def resolve(self, path: Path) -> Path:
        """Where a path actually ends up, symlinks followed.

        Separate from `stat` because `stat` deliberately does *not* follow them (a symlink planted
        at an owned root must be visible as a symlink, or `remove` would delete what it points at),
        and the kernel executing a shebang does. #46 hides in the difference: `venv/bin/python` is a
        symlink, and the directory that stops the service account is at the other end of it.
        """
        ...

    def read(self, path: Path) -> str | None: ...

    def tree(self, root: Path) -> Mapping[str, str]:
        """Content identities for every file a copy of `root` would carry (#55, SAPRS 13.9).

        Digests rather than timestamps, because the question is "is this the same code", and a
        `git pull` answers it without changing anything a `stat()` reports. The walk, and the
        list of names that are not the code, belong to `application.py`.
        """
        ...

    def disk_free(self, path: Path) -> int: ...

    def port_free(self, host: str, port: int) -> bool: ...

    def service(self, name: str) -> ServiceState: ...


class RealMachine:
    """The `Machine` for the computer this process is standing on."""

    def __init__(self, runner: CommandRunner | None = None) -> None:
        self._runner = runner if runner is not None else SubprocessRunner()

    @property
    def commands(self) -> CommandRunner:
        return self._runner

    @property
    def environ(self) -> Mapping[str, str]:
        return os.environ

    @property
    def running_as_root(self) -> bool:
        return os.geteuid() == 0

    def os_release(self) -> OsRelease:
        fields = _shell_assignments(self.read(Path("/etc/os-release")) or "")
        return OsRelease(
            id=fields.get("ID", ""),
            id_like=fields.get("ID_LIKE", ""),
            pretty_name=fields.get("PRETTY_NAME", ""),
        )

    def which(self, name: str) -> Path | None:
        # `search_path`, not the bare `PATH`: see #38. The failure this avoids is an installer
        # that reports "uv is missing" on a machine with uv in `~/.local/bin`, because sudo gave
        # root a `secure_path` and the tool was installed as somebody else.
        found = shutil.which(name, path=search_path(self.environ))
        return None if found is None else Path(found)

    def hostname(self) -> str:
        return socket.gethostname()

    def user_exists(self, name: str) -> bool:
        try:
            pwd.getpwnam(name)
        except KeyError:
            return False
        return True

    def user(self, name: str) -> ServiceAccount | None:
        """The account record, or None where there is no such user.

        Group membership is read from the group database rather than from the passwd entry, since
        on Linux the shadow file is where a system account's supplementary groups usually are.
        """

        try:
            entry = pwd.getpwnam(name)
        except KeyError:
            return None
        return ServiceAccount(
            name=entry.pw_name,
            shell=entry.pw_shell,
            home=entry.pw_dir,
            group=_name_of_gid(entry.pw_gid),
            groups=tuple(group.gr_name for group in grp.getgrall() if name in group.gr_mem),
        )

    def group_exists(self, name: str) -> bool:
        try:
            grp.getgrnam(name)
        except KeyError:
            return False
        return True

    def stat(self, path: Path) -> PathState:
        try:
            info = path.lstat()
        except OSError:
            return PathState(path=path)
        return PathState(
            path=path,
            exists=True,
            is_dir=stat.S_ISDIR(info.st_mode),
            owner=_name_of_uid(info.st_uid),
            group=_name_of_gid(info.st_gid),
            mode=stat.S_IMODE(info.st_mode),
        )

    def resolve(self, path: Path) -> Path:
        # `os.path.realpath` rather than `Path.resolve`: the latter raises on a broken symlink
        # chain with `strict=True` and, on older CPython, complains about an unreadable directory —
        # and an unreadable directory is the answer this module is looking for.
        return Path(os.path.realpath(path))

    def read(self, path: Path) -> str | None:
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    def tree(self, root: Path) -> Mapping[str, str]:
        """`application.manifest`, which is the whole of the answer on a real filesystem."""

        return manifest(root)

    def disk_free(self, path: Path) -> int:
        """Bytes free at `path`, walking up to the nearest existing ancestor.

        Asking about `/var/lib/encore` before it exists is the normal case on a first
        install, and `disk_usage` answers that with an OSError rather than with the number
        that is true of the parent — which is the same filesystem in every case that matters.
        """

        probe = path
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        try:
            return shutil.disk_usage(probe).free
        except OSError:
            return 0

    def port_free(self, host: str, port: int) -> bool:
        """Whether the bind address is unclaimed, by binding it.

        A probe rather than a parse of `ss` output, because the question is about this
        address and this port and the kernel is the only component that knows what a
        `0.0.0.0` bind collides with. `SO_REUSEADDR` is deliberately not set: on Linux it
        lets a probe succeed over a socket in `TIME_WAIT`, which is precisely the state in
        which the Server's own bind would fail.
        """

        family = socket.AF_INET if _looks_ipv4(host) else socket.AF_INET6
        try:
            with socket.socket(family, socket.SOCK_STREAM) as probe:
                probe.bind((host, port))
        except OSError:
            return False
        return True

    def systemd_running(self) -> bool:
        """`/run/systemd/system` is where PID 1 leaves the evidence that it is systemd.

        A directory test rather than `systemctl is-system-running`, which answers `degraded` on a
        machine whose Encore is perfectly installable and costs a subprocess to say so.
        """

        return SYSTEMD_EVIDENCE.is_dir()

    def service(self, name: str) -> ServiceState:
        if not self.systemd_running():
            return ServiceState(name=name, detail="systemd is not running on this machine")
        installed = self._runner.run(["systemctl", "cat", name])
        enabled = self._runner.run(["systemctl", "is-enabled", name])
        active = self._runner.run(["systemctl", "is-active", name])
        return ServiceState(
            name=name,
            installed=installed.ok,
            enabled=enabled.stdout.strip() == "enabled",
            active=active.stdout.strip() == "active",
            detail=active.output() or enabled.output(),
        )


class HttpProbe(Protocol):
    """A get-and-decode, so `verify` can ask a running appliance about itself.

    Its own seam rather than a `Machine` method because it is the one observation that needs
    the service to be up: every other question can be answered about a machine with Encore
    stopped, and a check that cannot is a check that goes quiet exactly when it is needed.
    """

    def get_json(self, url: str, *, timeout: float = 3.0) -> Mapping[str, object] | None: ...


class LiveHttpProbe:
    """The production `HttpProbe`. Localhost, no retries, short timeout."""

    def get_json(self, url: str, *, timeout: float = 3.0) -> Mapping[str, object] | None:
        try:
            response = httpx.get(url, timeout=timeout, follow_redirects=False)
            if response.is_error:
                return None
            decoded = response.json()
        except Exception:
            return None
        return decoded if isinstance(decoded, dict) else None


@dataclass(frozen=True, slots=True, kw_only=True)
class ApplianceState:
    """Everything the installer knows about a machine at one instant.

    Attributes:
        layout: The paths under observation, including the staging root.
        os_release: The platform, for 13.1's honesty about what is supported.
        service: systemd's three answers about the unit.
        binaries: `mpv` and `uv`, resolved or not.
        systemd_running: Whether the host runs the service manager 13.6 assumes, which is not the
            same question as whether `service` names a unit and must never be answered from it.
        user_exists: Whether 13.5's account is already there.
        audio_group_exists: Whether the sound-card group exists to be joined.
        directories / files: observed state per layout entry, keyed by final path.
        contents: The current text of each managed file, which is the one comparison that
            decides whether writing anything is necessary at all — the difference between an
            idempotent installer and a noisy one (SAPRS 13.7).
        config_text: The current `config.yaml`, if any. Read as text, not parsed: the
            plan's question is "is there a file I must not overwrite", and a malformed file
            is `prerequisites.py`'s business rather than an observation's.
        music_readable: Whether the configured music directory is there at all.
        disk_free: Bytes free where the library will live.
        port_free: Whether the configured bind address is unclaimed.
        hostname: What the machine calls itself, against `server.hostname`.
        root: Whether this process may change any of it.
        health: The appliance's own answer, or None when it was not there to be asked.
    """

    layout: object
    os_release: OsRelease = field(default_factory=OsRelease)
    service: ServiceState = field(default_factory=ServiceState)
    binaries: Mapping[str, Path | None] = field(default_factory=dict)
    # The `PATH` those resolutions used. Reported when one is missing, because "uv is not
    # installed" and "uv is in `/home/someone/.local/bin`, which root was never given" are
    # different problems with different fixes, and the second is unfindable without the list.
    search_path: str = ""
    # Whether the host has the service manager 13.6 assumes, as distinct from whether `service`
    # has a unit in it. #43 is what happens when one stands in for the other.
    systemd_running: bool = True
    user_exists: bool = False
    # The account itself, where it exists: 13.5 asks two questions about it that `id` cannot
    # answer, and an appliance whose service user has a login shell is a smaller attack surface
    # than the box it runs on.
    service_user: ServiceAccount | None = None
    audio_group_exists: bool = False
    directories: Mapping[str, PathState] = field(default_factory=dict)
    files: Mapping[str, PathState] = field(default_factory=dict)
    contents: Mapping[str, str] = field(default_factory=dict)
    config_text: str | None = None
    music_readable: bool | None = None
    disk_free: int = 0
    port_free: bool = True
    hostname: str = ""
    root: bool = True
    health: Mapping[str, object] | None = None
    # Whether `/opt/encore/application` already holds the code being offered, and whether the
    # environment in `/opt/encore/venv` was built from the lockfile now in hand. Both are read
    # rather than remembered, because an operator may have run `git pull` in the application
    # directory themselves — and an installer that assumed it was the only writer would then
    # skip the dependency update that was the whole point of the upgrade.
    application_matches: bool = False
    # How far from matching, when it does not: the counts a report names, so "the application is
    # two releases behind" is a number an operator can act on rather than an adjective (#55).
    application_drift: Diff = field(default_factory=Diff)
    environment_matches: bool = False
    # Whether the environment that is there has an entry point in it. Separate from
    # `environment_matches` because the two fail differently: a stamp that disagrees with the
    # lockfile means the sync was skipped, and a missing `bin/encore-server` means the sync ran
    # and produced nothing — the second is a broken install, the first is an ordinary upgrade.
    environment_runnable: bool = False
    # What the entry point's shebang resolves to, and the first component of that path the service
    # account cannot get through — None for "reachable", None also for "not asked". #46: a venv
    # whose interpreter sits inside `/root` is executable by the account that built it and by nobody
    # named in `User=`, so every mode check passes and the unit dies at exec.
    interpreter: Path | None = None
    interpreter_blocker: Path | None = None
    # Whether each path named by the lifecycle tables is there, keyed by *documented* path.
    # `remove` and `upgrade` need existence, and getting it from `Path.exists()` inside a pure
    # planner would make a plan a function of the moment it ran rather than of the state
    # observed at the start — the difference between a report an operator can trust and a
    # description of whatever the disk happened to be doing (SAPRS 13.7).
    present: Mapping[str, bool] = field(default_factory=dict)

    def directory(self, final: str) -> PathState:
        return self.directories.get(final, PathState(path=Path(final)))

    def file(self, final: str) -> PathState:
        return self.files.get(final, PathState(path=Path(final)))

    @property
    def installed(self) -> bool:
        """Whether there is an Encore here already, by the unit rather than the directory.

        `/opt/encore/application` survives a `rm -rf` of the venv, and a unit file that
        systemd knows about is the state that has to be reconciled on the next run.
        """

        return self.service.installed


def observe(
    machine: Machine,
    *,
    layout: Layout,
    bind_host: str = "0.0.0.0",  # noqa: S104 - the question is what the machine will answer
    bind_port: int = 8080,
    music_dir: Path | None = None,
    source: Path | None = None,
    http: HttpProbe | None = None,
) -> ApplianceState:
    """Ask the machine everything `plan.py` will want, once, in one pass.

    Args:
        machine: The seam being observed.
        layout: Where the appliance lives, including any staging root.
        bind_host: `server.host`, for the port probe.
        bind_port: `server.port`.
        music_dir: `paths.music_dir`, if the configuration names one.
        source: The checkout that would be installed, for the two comparisons that decide
            whether copying files and resolving dependencies are necessary at all.
        http: The probe that asks a running appliance. Left out, nothing reaches for a
            socket: an install on a machine with the service stopped reports "not asked"
            rather than reporting a failure it never observed.
    """

    probe = http if http is not None else LiveHttpProbe()
    staged = layout.staged
    service = machine.service(SERVICE_NAME)
    drift, environment_matches = _source_matches(machine, layout, source)
    runnable = machine.stat(staged(f"{layout.venv}/bin/encore-server")).exists
    interpreter, blocker = _interpreter(machine, layout, staged)
    runtime_db = f"{layout.state}/runtime.db"
    return ApplianceState(
        layout=layout,
        os_release=machine.os_release(),
        service=service,
        binaries={name: machine.which(name) for name in REQUIRED_BINARIES},
        search_path=search_path(machine.environ),
        systemd_running=machine.systemd_running(),
        user_exists=machine.user_exists(layout.user),
        service_user=machine.user(layout.user),
        audio_group_exists=machine.group_exists(AUDIO_GROUP),
        directories={
            entry.final: machine.stat(staged(entry.final)) for entry in layout.directories
        },
        files={entry.final: machine.stat(staged(entry.final)) for entry in layout.files},
        contents={
            entry.final: text
            for entry in layout.files
            if (text := machine.read(staged(entry.final))) is not None
        },
        config_text=machine.read(staged(layout.config_file)),
        music_readable=None if music_dir is None else machine.stat(music_dir).exists,
        disk_free=machine.disk_free(staged(layout.state)),
        port_free=machine.port_free(bind_host, bind_port),
        hostname=machine.hostname(),
        root=machine.running_as_root,
        # Asked only of a service that claims to be running. A first install has nothing
        # listening yet, and a three-second timeout on the way to writing that sentence
        # would be the slowest part of the fastest command.
        health=(
            probe.get_json(f"http://127.0.0.1:{bind_port}/api/v1/health")
            if service.active
            else None
        ),
        application_matches=drift.identical,
        application_drift=drift,
        environment_matches=environment_matches,
        environment_runnable=runnable if source is not None else True,
        interpreter=interpreter,
        interpreter_blocker=blocker,
        present={
            final: machine.stat(staged(final)).exists
            for final in dict.fromkeys([*[entry[0] for entry in layout.removals], runtime_db])
        },
    )


def _interpreter(
    machine: Machine, layout: Layout, staged: Callable[[str | Path], Path]
) -> tuple[Path | None, Path | None]:
    """Where the entry point's interpreter is, and whether the service account can reach it.

    Asked only of a machine that has both halves: an entry point, and the account `User=` names.
    A staged rehearsal has no account (13.5's `useradd` is skipped, #41) and a first install has no
    entry point yet, and answering "unreachable" in either would plan a re-sync on every run — the
    mistake is not asking the question, it is inventing an answer.

    The staging rule is the same one `EnsureOwnership` follows, and for the same reason: a prefix
    under `/tmp` is traversable by whoever made it and by no service account on earth, so the
    question "can `encore` execute this" is not yet about this tree. It becomes a question on the
    appliance, where `root` is `/` and the modes are the ones 13.5 set.
    """

    script = staged(f"{layout.venv}/bin/encore-server")
    named = interpreter_of(machine.read(script))
    if named is None:
        return None, None
    # The name in the shebang is a symlink into wherever `uv` put the download; the walk the kernel
    # makes is the one at the other end of it, which is the part `stat` — deliberately an `lstat` —
    # cannot see.
    interpreter = machine.resolve(named)
    if layout.root != Path("/"):
        return interpreter, None
    account = machine.user(layout.user)
    if account is None:
        return interpreter, None
    return interpreter, interpreter_blocker(machine, interpreter, account)


def _source_matches(machine: Machine, layout: Layout, source: Path | None) -> tuple[Diff, bool]:
    """How far the installed trees are from the ones being offered.

    The code question is asked of the **trees**, file by file, and not of `pyproject.toml`.
    #55 is the whole of the reason: the comparison was one manifest read on each side, and a
    release that changes only Python — every bug fix this project has shipped since the
    installer did — changes no byte of it. Sixteen source files differed on an appliance two
    releases behind, the plan saw four steps and none of them was a copy, and #51 reached that
    machine only because somebody rsynced it by hand.

    The environment question stays a comparison of one file's content, because that is what it
    is about: `uv.lock` is what the stamp inside the venv is matched against, so an operator who
    pulled a new release by hand still gets the dependency sync on the next upgrade.
    """

    if source is None:
        return Diff(), False
    application = layout.staged(layout.application)
    drift = difference(machine.tree(source), machine.tree(application))
    lock = machine.read(source / "uv.lock")
    stamp = machine.read(layout.staged(f"{layout.venv}/.encore-lock"))
    return drift, drift.identical and bool(lock) and stamp == lock


def _looks_ipv4(host: str) -> bool:
    return host != "::" and ":" not in host


def _name_of_uid(uid: int) -> str:
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return str(uid)


def _name_of_gid(gid: int) -> str:
    try:
        return grp.getgrgid(gid).gr_name
    except KeyError:
        return str(gid)


def _shell_assignments(text: str) -> dict[str, str]:
    """`KEY=value` lines from a shell-ish file, with comments and `export` ignored.

    `/etc/os-release` is documented as this format, and the two lines that matter to
    SAPRS 13.1 are `ID=` and `ID_LIKE=`. Values keep their quotes: the caller strips them
    where it compares, which is where the difference between `"ubuntu"` and `ubuntu` is
    actually observable.
    """

    fields: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("export "):
            stripped = stripped[len("export ") :].strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        if key.strip():
            fields[key.strip()] = _unquote(value.strip())
    return fields


def _unquote(value: str) -> str:
    """A shell variable's value, without the quotes that were part of the syntax.

    `/etc/os-release` is allowed to quote and most images do, and a `PRETTY_NAME` reported with
    its quotes still attached is a report line that looks like a bug even though the value is
    correct — which matters because that string is what an operator pastes into an issue.
    """

    if value[:1] in {'"', "'"} and value[:1] == value[-1:]:
        return value[1:-1]
    return value
