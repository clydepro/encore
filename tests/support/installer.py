"""Test doubles for the installer (SAPRS Chapter 13, issue #32).

The installer is the one part of Encore whose subject is a machine it is not standing on: it
asks whether a user exists, whether a socket is free, whether systemd is PID 1. Those questions
are the reason it had no tests anywhere else in this suite, and the reason it can have them here.

Two seams, deliberately different in kind:

``RecordingRunner``
    Captures argv, environment and working directory instead of spawning — 13.6's "every command
    is logged" made testable. A test asserts on the `useradd --system` the installer planned
    rather than on a machine that has had a user added to it.

``FakeMachine``
    Subclasses :class:`~apps.installer.system.RealMachine` and overrides only the answers about
    the *host*: users, groups, systemd, ports, OS. It keeps the real filesystem, which is the
    half worth being precise about. A fake filesystem that agrees with itself proves only that
    the installer planned what it already knew; a real tree under `--root` proves that the plan,
    executed, leaves files that exist at the modes the plan claimed. Real `stat` and `read` are
    also how idempotence is tested honestly — a second run observes the bytes the first one
    wrote, not a scripted answer.

Neither double makes an unprivileged test process root, and neither needs to: the installer never
has to be root, and the commands that would need it are recorded rather than run.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from apps.installer.layout import PYTHON_VERSION, Layout
from apps.installer.options import Command, Options, Settings
from apps.installer.system import (
    CommandResult,
    OsRelease,
    RealMachine,
    ServiceAccount,
    ServiceState,
    SubprocessRunner,
)

__all__ = ["FakeMachine", "RecordingRunner", "request", "synced"]

#: The service account's own group, which exists exactly when the account does (SAPRS 13.5).
SERVICE_GROUP = "encore"


@dataclass(frozen=True, slots=True)
class Call:
    """One command the installer asked the host to run."""

    argv: tuple[str, ...]
    env: Mapping[str, str] | None = None
    cwd: Path | None = None

    def starts_with(self, *prefix: str) -> bool:
        return self.argv[: len(prefix)] == prefix

    def __str__(self) -> str:
        return " ".join(self.argv)


@dataclass(slots=True)
class RecordingRunner:
    """A `CommandRunner` that records instead of running.

    Args:
        answers: argv-prefix → canned result, for the questions a host answers with a command
            (`systemctl is-enabled` and friends). Anything unmatched answers success silently,
            which from here is what a command that worked looks like.
        effects: argv-prefix → callable, for a command whose *effect* later state depends on.
            `uv sync` is the one that matters: it would resolve a dependency graph over the
            network, so the installer's tests substitute the outcome — an environment with its
            entry points in it — and assert on everything after that. An effect is not an answer:
            the command is still recorded, and a test that expected it not to run would still
            see it.
        real: argv-prefixes to run for real, through the same `SubprocessRunner` production
            uses. `rsync` is the only sensible entry: copying the project into a staging prefix
            is a filesystem operation the acceptance list later inspects, and emulating it would
            mean a test asserting on its own emulation. The dangerous prefixes — `useradd`,
            `systemctl` — are never here.
    """

    answers: Mapping[tuple[str, ...], CommandResult] = field(default_factory=dict)
    effects: Mapping[tuple[str, ...], Callable[[Call], None]] = field(default_factory=dict)
    real: Sequence[tuple[str, ...]] = ()
    accounts: Sequence[str] | None = None
    calls: list[Call] = field(default_factory=list)

    def run(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> CommandResult:
        parts = tuple(str(part) for part in argv)
        call = Call(argv=parts, env=env, cwd=cwd)
        self.calls.append(call)
        for prefix, effect in self.effects.items():
            if parts[: len(prefix)] == prefix:
                effect(call)
        for prefix in self.real:
            if parts[: len(prefix)] == prefix:
                return SubprocessRunner().run(argv, env=env, cwd=cwd)
        if (refused := self._refused_chown(parts)) is not None:
            return refused
        for prefix, answer in self.answers.items():
            if parts[: len(prefix)] == prefix:
                return answer
        return CommandResult(argv=parts, code=0)

    # --- the assertion side ---------------------------------------------------------------

    def _refused_chown(self, parts: tuple[str, ...]) -> CommandResult | None:
        """`chown` to an account this machine does not have, answered the way `chown` answers.

        "Anything unmatched answers success" is the right default for a recorder and the wrong one
        for this command: a rehearsal whose machine has no `encore` group was happily
        `chown root:encore`-ing its way through every test until #41, and the fake was
        accidentally green about exactly the failure that aborts a real install. The account list
        is the same fiction `FakeMachine.user_exists` tells, so the two cannot disagree.
        """

        if self.accounts is None or parts[:1] != ("chown",) or len(parts) < 3:
            return None
        _, _, group = parts[1].partition(":")
        if not group or group in self.accounts:
            return None
        # The shape of the real failure (exit 1, "invalid group"), not its punctuation: the
        # version of chown on the box decides that, and no assertion here reads the string.
        return CommandResult(argv=parts, code=1, stderr=f"chown: invalid group: '{parts[1]}'")

    def ran(self, *prefix: str) -> bool:
        return any(call.starts_with(*prefix) for call in self.calls)

    def times(self, *prefix: str) -> int:
        return sum(call.starts_with(*prefix) for call in self.calls)

    def one(self, *prefix: str) -> Call | None:
        for call in self.calls:
            if call.starts_with(*prefix):
                return call
        return None

    def transcript(self) -> str:
        """What 13.6 wants logged: the commands, as one would have typed them."""

        return "\n".join(str(call) for call in self.calls)


class FakeMachine(RealMachine):
    """Real filesystem, scripted host.

    Args:
        recorder: The command seam. Defaults to a fresh :class:`RecordingRunner`.
        user: Whether 13.5's `encore` account exists.
        groups: Extra groups that exist; `audio` is the one any of this turns on.
        systemd: Whether the machine runs systemd. False is a container, and every service
            question then answers "there is no systemd here".
        unit: What systemd says about `encore.service`, when it is running.
        busy_ports: Ports already claimed; anything absent is free.
        os_release: The platform. Defaults to a supported Debian.
        root: Whether this process may change the machine.
        binaries: `mpv` and `uv` as the machine sees them; None means "not installed".
        disk_free: Bytes free anywhere, or None for "a real filesystem's answer".
        health: What `/api/v1/health` answered, or None for "not asked".
        account: What the service account's record says, where `user=True`. Its primary group
            matters: #46's question is whether that identity can walk to the interpreter, and an
            account that belongs to nothing is the machine where the answer is no.
    """

    def __init__(
        self,
        *,
        recorder: RecordingRunner | None = None,
        user: bool = False,
        groups: Sequence[str] = (),
        systemd: bool = False,
        unit: ServiceState | None = None,
        busy_ports: Sequence[int] = (),
        os_release: OsRelease | None = None,
        root: bool = False,
        hostname: str = "jukebox",
        binaries: Mapping[str, Path | None] | None = None,
        disk_free: int | None = None,
        account: ServiceAccount | None = None,
    ) -> None:
        self._recorder = recorder if recorder is not None else RecordingRunner()
        super().__init__(self._recorder)
        self._user = user
        # The runner has to know the same fiction, because `chown` is the one command whose
        # failure is a fact about accounts rather than about processes.
        self._recorder.accounts = (SERVICE_GROUP,) if user else ()
        self._groups = frozenset(groups) | ({SERVICE_GROUP} if user else frozenset())
        self._systemd = systemd
        self._unit = unit if unit is not None else ServiceState()
        self._busy = frozenset(busy_ports)
        self._os = os_release if os_release is not None else OsRelease(id="debian")
        self._root = root
        self._hostname = hostname
        self._binaries = dict(
            binaries
            if binaries is not None
            else {"mpv": Path("/usr/bin/mpv"), "uv": Path("/usr/bin/uv")}
        )
        self._disk_free = disk_free
        self._account = account

    def user(self, name: str) -> ServiceAccount | None:
        # `user_exists` already tells this fiction, so `user` has to as well: the two disagreeing is
        # how #41's fake could report an account that its own chown then failed to find, and #46's
        # question — can *this identity* execute that path — is asked of this record.
        if not self._user:
            return None
        if self._account is not None:
            return self._account
        return ServiceAccount(
            name=name, shell="/usr/sbin/nologin", home="/var/lib/encore", group=name
        )

    @property
    def recorder(self) -> RecordingRunner:
        """The commands asked of this host, for tests that built the machine, not the runner."""

        return self._recorder

    @property
    def running_as_root(self) -> bool:
        return self._root

    def os_release(self) -> OsRelease:
        return self._os

    def hostname(self) -> str:
        return self._hostname

    def which(self, name: str) -> Path | None:
        # A table rather than `shutil.which`, because the machine under test is the appliance and
        # not the development box running pytest — which has a `uv` on its PATH for reasons of
        # its own. An install that passed because it found the test runner's interpreter is the
        # bug this seam exists to prevent.
        return self._binaries.get(name)

    def user_exists(self, name: str) -> bool:
        return self._user

    def systemd_running(self) -> bool:
        # The same answer `service()` is built from, because #43 was exactly these two facts being
        # confused: a machine can run systemd and have no `encore.service`, and that is a first
        # install rather than a broken prerequisite.
        return self._systemd

    def group_exists(self, name: str) -> bool:
        return name in self._groups

    def service(self, name: str) -> ServiceState:
        if not self._systemd:
            return ServiceState(name=name, detail="systemd is not running on this machine")
        return self._unit

    def port_free(self, host: str, port: int) -> bool:
        return port not in self._busy

    def disk_free(self, path: Path) -> int:
        # 64 GiB by default: enough for 13.2's library plus headroom, so a test that means
        # "disk" has to say so rather than inherit a real partition's luck.
        return 64 * 1024**3 if self._disk_free is None else self._disk_free


class StubHttpProbe:
    """An `HttpProbe` that answers from a table of URLs.

    `verify` is the one command that asks a running appliance about itself, and a test of the
    acceptance list should not need an appliance.
    """

    def __init__(self, answers: Mapping[str, Mapping[str, object] | None] | None = None) -> None:
        self.answers: dict[str, Mapping[str, object] | None] = dict(answers or {})
        self.asked: list[str] = []

    def get_json(self, url: str, *, timeout: float = 3.0) -> Mapping[str, object] | None:
        self.asked.append(url)
        return self.answers.get(url)


def request(
    root: Path,
    *,
    command: Command = Command.INSTALL,
    service: bool = False,
    source: Path | None = None,
    settings: Settings | None = None,
    **values: object,
) -> Options:
    """An operation staged under `root`, with everything else at its default.

    `service=False` by default: these tests run as an ordinary user, and asking for systemd
    integration would make the outcome depend on the machine running pytest. The systemd half is
    covered through :class:`RecordingRunner`, which sees the commands without needing a host that
    would run them.
    """

    return Options(
        command=command,
        layout=Layout(root=root),
        source=source if source is not None else checkout(),
        settings=settings if settings is not None else Settings(music_dir=root / "opt" / "music"),
        service=service,
        **values,  # type: ignore[arg-type]
    )


def checkout() -> Path:
    """The repository this run came from — the tree an install would copy."""

    import apps.installer

    return Path(apps.installer.__file__).resolve().parents[2]


def synced(*commands: str) -> dict[tuple[str, ...], Callable[[Call], None]]:
    """The *effects* of `uv python install` and `uv sync`, without the network.

    A test that ran the real commands would resolve a dependency graph and download a CPython on
    every install; a test that skipped them would leave `venv/bin/encore-server` nonexistent, which
    the acceptance list correctly calls a broken appliance. So the outcomes are substituted and the
    commands are still recorded — `ran("uv", "sync")` stays true, and the environment ends up
    runnable.

    What they leave behind is the venv `uv` leaves behind, not a directory of shell scripts: a
    `bin/python` that is a symlink into the interpreter directory, and console scripts whose shebang
    names it. #46 is the reason. The interpreter's *location* is the fact that decides whether the
    appliance starts, so a fake whose entry point says `#!/bin/sh` cannot express the machine where
    that path runs through a `drwx------` home — and a recorder that answered `uv python install`
    with success and no filesystem effect would let the executor plan a `--python` path that never
    existed, which is #41's accidentally-green fake one command later.

    Args:
        commands: The entry points to leave in the environment. Defaults to the three the
            appliance ships, because an install that produced a venv with only half of them is
            an install that cannot build a library on the box (SAPRS 13.11).

    Returns:
        A mapping for :attr:`RecordingRunner.effects`.
    """

    def create_environment(call: Call) -> None:
        environ = call.env or {}
        environment = environ.get("UV_PROJECT_ENVIRONMENT")
        if environment is None:
            return
        binary = Path(environment) / "bin"
        binary.mkdir(parents=True, exist_ok=True)
        interpreter = _managed_interpreter(environ.get("UV_PYTHON_INSTALL_DIR"))
        launcher = binary / "python"
        if interpreter is not None:
            launcher.unlink(missing_ok=True)
            launcher.symlink_to(interpreter)
        named = f"#!{launcher if interpreter is not None else Path('/bin/sh')}\n"
        for command in commands or ("encore-server", "encore-builder", "encore-install"):
            script = binary / command
            script.write_text(f'{named}exec {command} "$@"\n')
            script.chmod(0o755)

    return {
        ("uv", "sync"): create_environment,
        ("uv", "python", "install"): install_interpreter,
    }


def install_interpreter(call: Call) -> None:
    """`uv python install --install-dir D`, which leaves a CPython under `D`.

    The directory is named in argv rather than in the environment. Registering an effect for it is
    the difference #41 taught about: a recorder that answers `uv python install` with success and
    nothing on disk lets the next command be planned against a path that does not exist.
    """

    argv = call.argv
    if "--install-dir" in argv:
        _managed_interpreter(argv[argv.index("--install-dir") + 1], alias=True)


def _managed_interpreter(install_dir: str | None, *, alias: bool = False) -> Path | None:
    """A stand-in for the CPython `uv` downloads, placed where it said it would put it.

    Nothing in the suite executes it — the entry points are only ever inspected — but it is created
    0755 under a 0755 tree so that the traversal arithmetic in `interpreter_blocker` has something
    true to run against.

    `alias=True` also leaves uv's stable `cpython-3.12-<platform>` symlink beside the versioned
    directory, because that is what the real layout contains and the executor has to pick one.
    """

    if install_dir is None:
        return None
    root = Path(install_dir)
    interpreter = root / f"cpython-{PYTHON_VERSION}.0-test" / "bin" / f"python{PYTHON_VERSION}"
    interpreter.parent.mkdir(parents=True, exist_ok=True)
    interpreter.write_text("#!/bin/sh\nexit 0\n")
    interpreter.chmod(0o755)
    if alias:
        stable = root / f"cpython-{PYTHON_VERSION}-test"
        stable.unlink(missing_ok=True)
        stable.symlink_to(interpreter.parent, target_is_directory=True)
    return interpreter
