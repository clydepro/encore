"""Where the appliance lives (SAPRS 13.3), stated once, under one optional root.

The layout is a table, not a scattering of string literals, because three things read it:
the plan that creates the directories, the report that explains them, and `verify` that
checks whether someone moved one. A fourth reads it in tests: a staging root makes an
install into `tmp_path` a real install — real files, real modes, a real rendered unit —
which is the only way SAPRS 13.7's idempotence and 13.8's non-destruction become assertions
instead of hopes.

Two kinds of entry appear here and the difference matters. A **directory** is created. A
**file** is rendered from `content.py` and written with an action that cannot overwrite.
Anything that is neither — `library.db`, `runtime.db`, the artwork cache, whatever the
operator drops in `/var/log/encore` — is deliberately absent from this table, so the
sentence "the installer preserves user data" is a property of the data structure rather
than a promise about code nobody has read yet.

`root` is a *staging* prefix and nothing else: `/var/lib/encore` under a root of
`/tmp/stage` is `/tmp/stage/var/lib/encore`, while the rendered unit file and the generated
report still say `/var/lib/encore`, because that is where the appliance will look for it.
A staged install is therefore a filesystem image, not a rehearsal with different answers.
"""

from __future__ import annotations

import stat
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "DEFAULT_GROUP",
    "DEFAULT_USER",
    "SCOPE_APPLICATION",
    "SCOPE_CONFIGURATION",
    "SCOPE_DATA",
    "Directory",
    "Layout",
    "ManagedFile",
]

#: The service account (SAPRS 13.5). Created if absent, never renamed, never root.
DEFAULT_USER = "encore"
DEFAULT_GROUP = "encore"

#: The group that owns the sound cards, when the distribution has one. 13.5's unprivileged
#: account cannot play anything without it, which is the difference between an appliance that is
#: healthy and one that is quiet.
AUDIO_GROUP = "audio"

#: The interpreter the appliance runs on, as `uv` names it. Matches `requires-python` in
#: `pyproject.toml`, and is stated here rather than read from it because the installer runs before
#: the project is unpacked in some orderings and because a floor is a decision, not an import.
#: AIG 3.
PYTHON_VERSION = "3.12"

# SAPRS 13.10's three scopes, in the words an operator reads in a removal report.
SCOPE_APPLICATION = "application"
SCOPE_CONFIGURATION = "configuration"
SCOPE_DATA = "data"

#: `/`, named because `staged()` compares against it and a literal `/` in a condition reads
#: like a filesystem operation rather than like the absence of one.
_FILESYSTEM_ROOT = Path("/")


@dataclass(frozen=True, slots=True, kw_only=True)
class Directory:
    """One directory the installer creates, with the ownership it must end up with.

    Attributes:
        final: The absolute path as the appliance sees it.
        owner: Account that must be able to write it.
        group: Group that must be able to write it.
        mode: The exact permission bits, because "0750 with ACLs" and "0755" are different
            answers to a question a party guest can ask by guessing a URL.
        purpose: The line in the report. A directory nobody can explain is a directory
            someone will `chmod 777` at midnight.
    """

    final: str
    owner: str = DEFAULT_USER
    group: str = DEFAULT_GROUP
    mode: int = 0o750
    purpose: str = ""

    @property
    def octal(self) -> str:
        return stat.filemode(stat.S_IFDIR | self.mode)[1:]


@dataclass(frozen=True, slots=True, kw_only=True)
class ManagedFile:
    """One file the installer renders. Ownership is `root` by default on purpose.

    Configuration is read by a process running as `encore` and written by a process running
    as root; `group=encore` is what makes that work without 0644 on a file that names every
    path on the box, and 0600 is what the environment file uses when it holds a password.
    """

    final: str
    owner: str = "root"
    group: str = DEFAULT_USER
    mode: int = 0o644
    purpose: str = ""

    @property
    def octal(self) -> str:
        return stat.filemode(stat.S_IFREG | self.mode)[1:]


@dataclass(frozen=True, slots=True, kw_only=True)
class Layout:
    """The SAPRS 13.3 filesystem, optionally staged under `root`.

    Attributes:
        root: Staging prefix. `/` in production — the default exists so a call site that
            means a real install says nothing, and a call site that means a test says so.
        user: Service account. A parameter rather than a constant because an operator may
            already run Encore under a name that is not `encore` (13.5 says "such as"), and
            the installer's job then is to notice, not to rename.
        group: Service group, which is where the audio-device membership question lands.
        audio_group: The distribution's sound-card group (SAPRS 13.5). A field rather than a
            constant because an operator may have renamed it, and the question that matters — can
            this account reach the device — is answered by the name on this machine, not by ours.
    """

    root: Path = Path("/")
    user: str = DEFAULT_USER
    group: str = DEFAULT_GROUP
    audio_group: str = AUDIO_GROUP

    # -- the named places ------------------------------------------------

    @property
    def application(self) -> str:
        """`/opt/encore/application` — the code. Read-only to the service (SAPRS 13.3)."""
        return "/opt/encore/application"

    @property
    def venv(self) -> str:
        """`/opt/encore/venv` — the dedicated environment (13.4, nothing in system Python)."""
        return "/opt/encore/venv"

    @property
    def python(self) -> str:
        """`/opt/encore/python` — the interpreter the venv links to, when the machine has none.

        Not in SAPRS 13.3's table because 13.3 assumes a system Python, and Debian 12 does not have
        one new enough (`requires-python` is 3.12, bookworm ships 3.11), so `uv` downloads a managed
        CPython. Where it puts that download is `UV_PYTHON_INSTALL_DIR`, whose default is the
        *invoking account's* `~/.local/share/uv/python` — under `sudo`, inside `/root`, which is
        `drwx------`. The venv then links to an interpreter `User=encore` can never reach and the
        unit dies at exec (#46). Giving it a place of its own, world-readable and next to the venv,
        is what makes the download survive the account that triggered it.
        """
        return "/opt/encore/python"

    @property
    def config_file(self) -> str:
        """`/etc/encore/config.yaml` — the file the Server reads and never writes (12.3)."""
        return "/etc/encore/config.yaml"

    @property
    def environment_file(self) -> str:
        """`/etc/encore/environment` — the unit's `EnvironmentFile`, mode 0600."""
        return "/etc/encore/environment"

    @property
    def unit_file(self) -> str:
        """`/etc/systemd/system/encore.service` — 13.6's answer."""
        return "/etc/systemd/system/encore.service"

    @property
    def tmpfiles_file(self) -> str:
        """`/usr/lib/tmpfiles.d/encore.conf` — so `systemd-tmpfiles --clean` cannot strand
        the mpv socket directory that playback startup depends on."""
        return "/usr/lib/tmpfiles.d/encore.conf"

    @property
    def state(self) -> str:
        """`/var/lib/encore` — library.db, runtime.db, artwork."""
        return "/var/lib/encore"

    @property
    def artwork(self) -> str:
        return "/var/lib/encore/artwork"

    @property
    def cache(self) -> str:
        """`/var/cache/encore` — scratch, disposable."""
        return "/var/cache/encore"

    @property
    def temp(self) -> str:
        return "/var/cache/encore/temp"

    @property
    def logs(self) -> str:
        """`/var/log/encore` — created for the layout's sake. Encore logs to stderr and the
        journal owns the record (SAPRS 11.5); this is where an operator who redirects
        `StandardError=` points it, which is why it is encore-writable and not root-owned."""
        return "/var/log/encore"

    # -- the table -------------------------------------------------------

    @property
    def directories(self) -> tuple[Directory, ...]:
        """Everything that gets created, in the order an operator would want it reported.

        Parent directories come from `mkdir -p` semantics rather than from this table, so
        the entries are the ones with an ownership or mode opinion — `/opt/encore` itself
        has neither, which is why it is absent.
        """

        return (
            Directory(
                final=self.application,
                owner="root",
                group=self.group,
                mode=0o755,
                purpose="the Encore code the service runs",
            ),
            Directory(
                final=self.venv,
                owner="root",
                group=self.group,
                mode=0o755,
                purpose="the dedicated virtual environment (SAPRS 13.4)",
            ),
            Directory(
                final=self.python,
                owner="root",
                group="root",
                mode=0o755,
                purpose="the downloaded interpreter the venv links to, readable by the service "
                "account (#46)",
            ),
            Directory(
                final="/etc/encore",
                owner="root",
                group=self.group,
                mode=0o755,
                purpose="installation-time configuration (Chapter 12)",
            ),
            Directory(
                final=self.state,
                mode=0o750,
                purpose="library.db, runtime.db and the artwork cache",
            ),
            Directory(
                final=self.artwork,
                mode=0o750,
                purpose="the image cache the Builder published",
            ),
            Directory(
                final=self.cache,
                owner="root",
                group=self.group,
                mode=0o755,
                purpose="scratch space, disposable by definition",
            ),
            Directory(
                final=self.temp,
                mode=0o700,
                purpose="mpv's IPC socket; private because the socket is a command channel",
            ),
            Directory(
                final=self.logs,
                mode=0o750,
                purpose="for an operator's own redirection; the journal holds the log",
            ),
        )

    @property
    def files(self) -> tuple[ManagedFile, ...]:
        """Everything that gets rendered. See `content.py` for what they say."""

        return (
            ManagedFile(
                final=self.config_file,
                mode=0o640,
                purpose="the configuration the Server validates at startup",
            ),
            ManagedFile(
                final=self.environment_file,
                mode=0o600,
                purpose="ENCORE_HOME, ENCORE_CONFIG and any ENCORE_ADMIN_PASSWORD",
            ),
            ManagedFile(
                final=self.unit_file,
                owner="root",
                group="root",
                mode=0o644,
                purpose="the systemd service (SAPRS 13.6)",
            ),
            ManagedFile(
                final=self.tmpfiles_file,
                owner="root",
                group="root",
                mode=0o644,
                purpose="keeps the cache and socket directories after a tmpfiles clean",
            ),
        )

    # -- staging ---------------------------------------------------------

    def staged(self, final: str | Path) -> Path:
        """The real path of a documented one, under `root`.

        `"/"` and an absolute path concatenate by replacement in pathlib, which is most of the
        trick: a production layout is a no-op and a staging prefix is a redirect, and no call
        site needs to know which it is standing in.

        The other half is the guard. A path already inside the root is returned unchanged,
        because a configuration file that names `/tmp/stage/var/lib/encore` is already staged,
        and staging it a second time would produce `/tmp/stage/tmp/stage/...` — which is not a
        test-only worry. On any install after the first, the configuration in force was itself
        written by a staged install, so `staged()` is applied to paths that are already staged
        and an unguarded version would relocate the appliance one level deeper on every run.
        """

        path = Path(final)
        if not path.is_absolute():
            # Never resolved against the cwd: a relative documented path under the staging root
            # is the answer in both cases, and `Path.cwd()` would make the install depend on the
            # directory an operator happened to type it from.
            return self.root / path
        if self.root == _FILESYSTEM_ROOT:
            return path
        if path.parts[: len(self.root.parts)] == self.root.parts:
            return path
        return self.root / path.relative_to("/")

    @property
    def managed_paths(self) -> tuple[str, ...]:
        """Every path this installer has an opinion about, dirs then files.

        The modes and owners in this table are what `install` creates and what `verify`
        compares against, so the two cannot disagree about what a correct appliance looks
        like — which is the same argument ADR-010 makes about a schema.
        """

        return tuple(entry.final for entry in self.directories) + tuple(
            entry.final for entry in self.files
        )

    @property
    def removals(self) -> tuple[tuple[str, str], ...]:
        """Every path `remove` may delete, each with its SAPRS 13.10 scope.

        One table rather than three lists in the planner, because the scopes *are* 13.10's
        removal section, and because `owned_roots` — the last line of defence before a deletion
        — is derived from here. Two lists, one for planning and one for refusing, is a setup
        where a library directory appears in the first and not the second, and the failure is
        silent until somebody restores a backup.

        Order matters as little here as it does nowhere else in this file, with one exception:
        `/etc/encore` and `/opt/encore` come after the files inside them, so a report lists what
        was lost before it lists the directories that held them.
        """

        return (
            (self.config_file, SCOPE_CONFIGURATION),
            (self.environment_file, SCOPE_CONFIGURATION),
            (self.unit_file, SCOPE_APPLICATION),
            (self.tmpfiles_file, SCOPE_APPLICATION),
            (self.application, SCOPE_APPLICATION),
            (self.venv, SCOPE_APPLICATION),
            (self.python, SCOPE_APPLICATION),
            ("/opt/encore", SCOPE_APPLICATION),
            ("/etc/encore", SCOPE_CONFIGURATION),
            (self.state, SCOPE_DATA),
            (self.cache, SCOPE_DATA),
            (self.logs, SCOPE_DATA),
        )

    @property
    def owned_roots(self) -> tuple[str, ...]:
        """The prefixes a deletion may fall inside. Everything else is not ours to remove."""

        return tuple(dict.fromkeys(final for final, _ in self.removals))
