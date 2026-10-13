"""The only module in the installer that changes anything (SAPRS 13.7).

One dispatch over the actions `plan.py` produced, and each arm is the shortest honest way to
do the thing: `mkdir` plus `chown` for a directory, a temporary file and a rename for a text
file, SQLite's own backup API for a copy of a database, `systemctl` for a service.

Three rules hold it together.

**Ownership goes through the runner.** Creating a directory is a filesystem call, which an
unprivileged process can do; `chown` cannot, so it is a command. That split is what lets a test
install into a temporary directory as an ordinary user and still assert that the real
`chown encore:encore /var/lib/encore` was asked for — the mode is checked on disk, the
ownership in the transcript, and neither is faked.

**Anything with a byte of consequence is written atomically.** A unit file half-written by a
full disk is worse than the missing file it replaced, so every write goes to a sibling
temporary, is flushed, and is renamed — ADR-006's publication rule, applied to a 150-line YAML
file because the failure it protects against is the same one.

**A failure stops the run.** There is no "continue anyway" path. The actions are ordered so
that everything after a failure depends on it, and an installer that printed a warning and
carried on is how a machine ends up with a new environment, an old unit file and a service
running code nobody intended.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from apps.installer.layout import Layout
from apps.installer.plan import (
    Action,
    BackupRuntimeDatabase,
    CreateDirectories,
    CreateServiceUser,
    DaemonReload,
    DeletePath,
    DisableService,
    EnableService,
    EnsureFile,
    ForgetCredential,
    InstallApplication,
    KeepExistingConfig,
    PreserveConfig,
    RepairPaths,
    RestartService,
    StartService,
    StopService,
    SyncEnvironment,
    WriteConfig,
)
from apps.installer.system import CommandResult, CommandRunner

__all__ = ["Execution", "Executor", "Result", "Status"]

#: What a copy of the source tree leaves out. Every entry is a build artifact, a cache, or
#: somebody's database — and the last category is the one that matters: carrying a development
#: `library.db` into `/opt/encore/application` would put a second library on a machine that is
#: allowed to have exactly one, in the wrong place, owned by root (ADR-006).
IGNORED = shutil.ignore_patterns(
    ".git",
    ".venv",
    "__pycache__",
    "*.py[cod]",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".coverage",
    "coverage.xml",
    "htmlcov",
    "site",
    "dist",
    "build",
    "*.egg-info",
    ".encore-lock",
    "library.db",
    "runtime.db",
    "artwork",
    "*.db-wal",
    "*.db-shm",
)

#: The same list in the dialect rsync speaks. Duplicated rather than derived because the two
#: glob syntaxes are not the same language, and an exclusion that works on one code path and
#: not the other is worse than an obvious list beside both.
RSYNC_EXCLUDES = (
    ".git/",
    ".venv/",
    "__pycache__/",
    "*.py[cod]",
    ".mypy_cache/",
    ".pytest_cache/",
    ".ruff_cache/",
    ".coverage",
    "coverage.xml",
    "htmlcov/",
    "site/",
    "dist/",
    "build/",
    "*.egg-info/",
    ".encore-lock",
    "library.db",
    "runtime.db",
    "artwork/",
    "*.db-wal",
    "*.db-shm",
)

#: The paths the installer owns, and therefore the only ones it may delete (SAPRS 13.10).
#: The SAPRS 13.3 tree, from `Layout.owned_roots`, is what a deletion may fall inside. Nothing
#: outside it is the installer's to remove, and 13.10's "user data should not be deleted
#: implicitly" is the reason that is enforced at the moment of deletion rather than in the plan.


class Status(StrEnum):
    """What an action became.

    `SKIPPED` is a result rather than an absence of one: an action that decided not to write a
    file it had been asked to write is the evidence that the installer is idempotent, and a
    report that lists only what happened cannot show that.
    """

    DONE = "done"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True, slots=True, kw_only=True)
class Result:
    """One action, attempted.

    Attributes:
        action: What was asked for, kept so the transcript and the plan are the same document.
        status: Whether it happened.
        detail: What the machine said — a command's first line of complaint, a path, a count.
        finding: Something the operator has to act on that the action did not resolve.
    """

    action: Action
    status: Status
    detail: str = ""
    finding: str = ""

    @property
    def ok(self) -> bool:
        return self.status is not Status.FAILED

    def line(self) -> str:
        mark = {Status.DONE: "done ", Status.SKIPPED: "no-op ", Status.FAILED: "FAILED"}[
            self.status
        ]
        text = f"  {mark} {self.action.detail}"
        if self.detail:
            text = f"{text}\n         {self.detail}"
        return f"{text}\n         → {self.finding}" if self.finding else text


@dataclass(frozen=True, slots=True, kw_only=True)
class Execution:
    """Everything that happened, in the order it happened."""

    results: tuple[Result, ...] = ()

    @property
    def succeeded(self) -> bool:
        return all(result.ok for result in self.results)

    @property
    def changed(self) -> int:
        return sum(1 for result in self.results if result.status is Status.DONE)

    @property
    def noops(self) -> int:
        return sum(1 for result in self.results if result.status is Status.SKIPPED)

    @property
    def failures(self) -> tuple[Result, ...]:
        return tuple(result for result in self.results if result.status is Status.FAILED)

    def findings(self) -> tuple[str, ...]:
        return tuple(result.finding for result in self.results if result.finding)


class Executor:
    """Turn actions into effects.

    Args:
        runner: The seam that starts every process.
        layout: For the deletion scope check, which needs to know where the appliance is.
        dry_run: Record every action as skipped and touch nothing. Distinct from an empty plan:
            `--dry-run` describes what would have happened to a machine that is otherwise real.
    """

    def __init__(self, runner: CommandRunner, *, layout: Layout, dry_run: bool = False) -> None:
        self._runner = runner
        self._layout = layout
        self._dry_run = dry_run

    def apply(self, actions: Sequence[Action]) -> Execution:
        """Perform `actions` in order, stopping at the first failure."""

        results: list[Result] = []
        for action in actions:
            if self._dry_run:
                results.append(Result(action=action, status=Status.SKIPPED, detail="--dry-run"))
                continue
            result = self._do(action)
            results.append(result)
            if result.status is Status.FAILED:
                break
        return Execution(results=tuple(results))

    # -- dispatch --------------------------------------------------------

    def _do(self, action: Action) -> Result:  # noqa: PLR0911, PLR0912 - one arm per action type
        match action:
            case CreateServiceUser():
                return self._create_user(action)
            case CreateDirectories(entries=entries):
                return self._paths(action, entries, create=True)
            case RepairPaths(entries=entries):
                return self._paths(action, entries, create=False)
            case EnsureFile(path=path, text=text, owner=owner, group=group, mode=mode):
                return self._write(
                    action, path, text, owner=owner, group=group, mode=mode, keep=False
                )
            case WriteConfig(path=path, text=text, owner=owner, group=group, mode=mode):
                return self._write(
                    action, path, text, owner=owner, group=group, mode=mode, keep=True
                )
            case PreserveConfig(path=path, differences=differences):
                return self._preserved(action, path, differences)
            case KeepExistingConfig(path=path, problems=problems):
                return Result(
                    action=action,
                    status=Status.FAILED,
                    detail=f"{path}: {'; '.join(problems[:3])}",
                    finding="Correct the file and run again. Nothing else was changed.",
                )
            case InstallApplication(source=source, destination=destination):
                return self._install_application(action, source, destination)
            case SyncEnvironment(application=application, venv=venv, offline=offline):
                return self._sync(action, application, venv, offline=offline)
            case DaemonReload():
                return self._command(action, ["systemctl", "daemon-reload"])
            case EnableService(service=service):
                return self._command(action, ["systemctl", "enable", service])
            case DisableService(service=service):
                return self._command(action, ["systemctl", "disable", service])
            case StartService(service=service):
                return self._command(action, ["systemctl", "start", service])
            case StopService(service=service):
                return self._command(action, ["systemctl", "stop", service])
            case RestartService(service=service):
                return self._command(action, ["systemctl", "restart", service])
            case BackupRuntimeDatabase(source=source, destination=destination):
                return self._backup(action, source, destination)
            case DeletePath(path=path, scope=scope):
                return self._delete(action, path, scope=scope)
            case ForgetCredential(database=database):
                return self._forget(action, database)
            case _:
                return Result(
                    action=action,
                    status=Status.FAILED,
                    detail=f"nothing in execute.py knows how to {action.kind}",
                )

    # -- accounts, paths, files ------------------------------------------

    def _create_user(self, action: CreateServiceUser) -> Result:
        """`useradd --system` (SAPRS 13.5), then the group that makes sound possible.

        `--no-create-home` because the state directory is created by the directory step with a
        mode and an owner chosen deliberately. Letting `useradd` make it as a side effect of
        adding an account is how an appliance ends up with `/var/lib/encore` at 0755 and a
        queue anyone on the LAN can read.
        """

        add = self._runner.run(
            [
                "useradd",
                "--system",
                "--home-dir",
                str(action.home),
                "--no-create-home",
                "--shell",
                action.shell,
                "--user-group",
                "--comment",
                "Encore jukebox service account",
                action.user,
            ]
        )
        if not add.ok:
            return Result(action=action, status=Status.FAILED, detail=add.output() or add.stderr)
        if not action.audio_group:
            return Result(
                action=action,
                status=Status.DONE,
                detail=f"{action.user} created; there is no audio group on this machine",
            )
        joined = self._runner.run(["usermod", "--append", "--groups", "audio", action.user])
        if not joined.ok:
            return Result(
                action=action,
                status=Status.FAILED,
                detail=f"created, but not added to audio: {joined.output()}",
                finding=f"sudo usermod -aG audio {action.user}  (SAPRS 7.7 needs the device)",
            )
        return Result(
            action=action, status=Status.DONE, detail=f"{action.user} created, in group audio"
        )

    def _paths(
        self,
        action: Action,
        entries: Sequence[tuple[Path, str, str, int]],
        *,
        create: bool,
    ) -> Result:
        done: list[str] = []
        for path, owner, group, mode in entries:
            if create:
                try:
                    path.mkdir(parents=True, exist_ok=True)
                except OSError as error:
                    return Result(
                        action=action,
                        status=Status.FAILED,
                        detail=f"{path}: {error.strerror or error}",
                    )
            if not path.exists():
                continue
            try:
                path.chmod(mode)
            except OSError as error:  # pragma: no cover - a directory that refuses chmod
                return Result(
                    action=action, status=Status.FAILED, detail=f"{path}: {error.strerror or error}"
                )
            status, detail = self._ownership(path, owner=owner, group=group)
            if status is Status.FAILED:
                return Result(action=action, status=status, detail=detail)
            done.append(str(path))
        verb = "Created" if create else "Repaired"
        return Result(action=action, status=Status.DONE, detail=f"{verb} {len(done)} path(s)")

    def _write(
        self,
        action: Action,
        path: Path,
        text: str,
        *,
        owner: str,
        group: str,
        mode: int,
        keep: bool,
    ) -> Result:
        """Write one rendered file, atomically — unless it is the one file that must not be.

        `keep=True` is SAPRS 13.8 enforced at the last moment. The plan already decided not to
        emit a write over an existing configuration; this is the belt. A bug in the plan should
        end here as a refusal rather than as an operator's `audio.device` reset to `default`.
        """

        if keep and path.exists():
            return Result(
                action=action,
                status=Status.SKIPPED,
                detail="already there; configuration is never overwritten (SAPRS 13.8)",
            )
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(path, text)
            path.chmod(mode)
        except OSError as error:
            return Result(
                action=action, status=Status.FAILED, detail=f"{path}: {error.strerror or error}"
            )
        owned, note = self._ownership(path, owner=owner, group=group)
        return Result(
            action=action,
            status=owned,
            detail=f"{path} at {oct(mode)[2:]}",
            finding=note,
        )

    def _preserved(
        self, action: Action, path: Path, differences: tuple[tuple[str, object, object], ...]
    ) -> Result:
        """Say precisely what was not applied, because "nothing" is the wrong summary.

        The operator who typed `--domain music.example` and got a preserved file needs to know
        which setting their file already disagrees with, in the file's own terms. This is the
        only place the two can be compared while both are still true.
        """

        if not differences:
            return Result(
                action=action, status=Status.SKIPPED, detail=f"{path} kept as it is (SAPRS 13.8)"
            )
        shown = 8  # more than this and the report is a diff, not a summary
        listed = ", ".join(
            f"{name} is {on_disk!r} there, {wanted!r} asked for"
            for name, on_disk, wanted in differences[:shown]
        )
        more = "" if len(differences) <= shown else f" (+{len(differences) - shown} more)"
        return Result(
            action=action,
            status=Status.SKIPPED,
            detail=f"{path} kept as it is (SAPRS 13.8)",
            finding=f"{listed}{more}. Edit the file if you meant those changes.",
        )

    def _ownership(self, path: Path, *, owner: str, group: str) -> tuple[Status, str]:
        """Set who owns a path, and decide whether failing to do so is fatal.

        The split is the whole reason a rehearsal can rehearse. `chown` needs root; `mkdir` and
        `chmod` do not, so a non-root run against a staging root gets a real filesystem with
        real modes and a note saying the ownership is still the operator's. Run as root, the same
        failure is a machine that will not let the service read its own configuration, and
        nothing after it is worth attempting.
        """

        result = self._runner.run(["chown", f"{owner}:{group}", str(path)])
        if result.ok:
            return Status.DONE, ""
        if os.geteuid() == 0:
            return (
                Status.FAILED,
                f"{path}: {result.output()}\n         → sudo chown {owner}:{group} {path}",
            )
        return (
            Status.DONE,
            f"{path}: ownership not set (not root). On the appliance: chown {owner}:{group} {path}",
        )

    # -- the application and its environment -----------------------------

    def _install_application(
        self, action: InstallApplication, source: Path, destination: Path
    ) -> Result:
        """Copy the project in, with the exclusions of `IGNORED`.

        `rsync --delete` when the machine has it, because pruning what a release removed is the
        difference between an upgrade and an accumulation: a module that no longer exists in the
        source but still sits in `application/` is importable, and that is how a half-upgraded
        appliance produces a traceback naming a file nobody can find in the repository.
        """

        if source == destination:
            return Result(
                action=action, status=Status.SKIPPED, detail="already the running application"
            )
        if shutil.which("rsync") is not None:
            argv = [
                "rsync",
                "--archive",
                "--delete",
                "--delete-excluded",
                "--quiet",
                *(f"--exclude={pattern}" for pattern in RSYNC_EXCLUDES),
                f"{source}/",
                f"{destination}/",
            ]
            return self._command(action, argv)
        try:
            shutil.copytree(source, destination, ignore=IGNORED, dirs_exist_ok=True, symlinks=True)
        except OSError as error:
            return Result(action=action, status=Status.FAILED, detail=str(error))
        return Result(
            action=action,
            status=Status.DONE,
            detail=f"{destination} copied; rsync was not found",
            finding="Without rsync, files a release removed are left behind. Nothing breaks, but "
            "a traceback may name a module that is no longer in the repository.",
        )

    def _sync(
        self, action: SyncEnvironment, application: Path, venv: Path, *, offline: bool
    ) -> Result:
        """`uv sync`, and the stamp recording which lockfile it was for.

        The stamp is not bookkeeping for this program's benefit — it is the only way to know
        that the environment matches `uv.lock` without running a resolver, and running a
        resolver on every install is what `--frozen` exists to avoid (SAPRS 15.1).
        """

        argv = ["uv", "sync", "--frozen", "--no-dev", "--project", str(application)]
        if offline:
            argv.append("--offline")
        result = self._command(
            action, argv, env={"UV_PROJECT_ENVIRONMENT": str(venv)}, cwd=application
        )
        if not result.ok:
            return Result(
                action=action,
                status=Status.FAILED,
                detail=result.detail,
                finding=f"cd {application} && uv sync --frozen --no-dev "
                f"(UV_PROJECT_ENVIRONMENT={venv})",
            )
        lock = application / "uv.lock"
        try:
            if lock.is_file():
                (venv / ".encore-lock").write_bytes(lock.read_bytes())
        except OSError as error:  # pragma: no cover - the sync would have failed first
            return Result(
                action=action,
                status=Status.DONE,
                detail=f"{venv} synced, but the stamp could not be written: {error.strerror or error}",
                finding="The next `upgrade` will sync these dependencies again, harmlessly.",
            )
        return Result(action=action, status=Status.DONE, detail=f"{venv} is in sync with {lock}")

    # -- databases -------------------------------------------------------

    def _backup(self, action: BackupRuntimeDatabase, source: Path, destination: Path) -> Result:
        """Copy a database with SQLite's backup API, from a read-only connection.

        `shutil.copy` is the wrong tool and the reason is WAL: a Server that is running keeps
        committed pages in `runtime.db-wal` that are not yet in the main file, so copying the
        file alone captures a moment that never existed and restoring it loses the queue.
        `mode=ro` plus `backup()` produces one consistent file and leaves no root-owned sidecar
        behind for the appliance to trip over on its next start.
        """

        if not source.is_file():
            return Result(action=action, status=Status.SKIPPED, detail=f"{source} is not there")
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            with (
                sqlite3.connect(f"file:{source}?mode=ro", uri=True) as origin,
                sqlite3.connect(destination) as copy,
            ):
                origin.backup(copy)
        except sqlite3.Error as error:
            return Result(
                action=action,
                status=Status.FAILED,
                detail=str(error),
                finding=f"Copy {source} by hand before upgrading. The migration cannot be undone.",
            )
        destination.chmod(0o600)
        self._runner.run(["chown", f"{self._layout.user}:{self._layout.group}", str(destination)])
        return Result(action=action, status=Status.DONE, detail=str(destination))

    def _forget(self, action: ForgetCredential, database: Path) -> Result:
        """Delete the one row in `admin_state`, and nothing else.

        The only write to a database this installer performs, and it is a delete rather than an
        insert for the reason ADR-015 gives: setting a password from here would mean a root-owned
        process hashing a credential into the appliance's own store, and any `-wal` left behind
        owned by root is an appliance that cannot start. So the row goes, the next `/admin` visit
        writes it, and the file never changes hands.
        """

        if not database.is_file():
            return Result(action=action, status=Status.SKIPPED, detail=f"{database} is not there")
        try:
            with sqlite3.connect(database) as connection:
                rows = connection.execute("DELETE FROM admin_state").rowcount
                connection.commit()
        except sqlite3.Error as error:
            return Result(
                action=action,
                status=Status.FAILED,
                detail=str(error),
                finding="Stop the service first (SAPRS 13.9 step 1); the database is in use.",
            )
        self._runner.run(["chown", f"{self._layout.user}:{self._layout.group}", str(database)])
        return Result(
            action=action,
            status=Status.DONE,
            detail=f"{rows} row(s) cleared; /admin will ask for a new password",
        )

    # -- deletion --------------------------------------------------------

    def _delete(self, action: DeletePath, path: Path, *, scope: str) -> Result:
        """Remove one path, having first proved the installer owns it.

        `scope` is echoed in the detail because 13.10's three scopes are the difference between a
        routine uninstall and an operator's library disappearing: a report that lists sixteen
        deleted paths without saying which of them were data forces the reader to know the layout
        to work out what they just lost.
        """

        if not self._owned(path):
            return Result(
                action=action,
                status=Status.FAILED,
                detail=f"{path} is not a path this installer created",
                finding="Refusing to delete something it did not install (SAPRS 13.10).",
            )
        if not path.exists():
            return Result(action=action, status=Status.SKIPPED, detail=f"{scope}: already gone")
        try:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
        except OSError as error:
            return Result(
                action=action,
                status=Status.FAILED,
                detail=f"{scope}: {path}: {error.strerror or error}",
            )
        return Result(action=action, status=Status.DONE, detail=f"{scope}: {path}")

    def _owned(self, path: Path) -> bool:
        """Whether a path is inside something the installer owns.

        Checked at the moment of deletion rather than in the plan, because the plan is a pure
        function a test may call with anything in it, and `DeletePath` is the one action whose
        mistake cannot be undone afterwards. The prefix list comes from `Layout`, not from a
        constant here, so the table that decides what an appliance *is* is the same one that
        decides what may be deleted — the arrangement that makes an out-of-scope path impossible
        to write into a plan without also making it deletable.
        """

        return any(
            path == staged or staged in path.parents
            for staged in (self._layout.staged(final) for final in self._layout.owned_roots)
        )

    # -- processes -------------------------------------------------------

    def _command(
        self,
        action: Action,
        argv: Sequence[str],
        *,
        env: dict[str, str] | None = None,
        cwd: Path | None = None,
    ) -> Result:
        result = self._runner.run(argv, env=env, cwd=cwd)
        if result.ok:
            return Result(action=action, status=Status.DONE, detail=_transcript(argv, result))
        return Result(
            action=action, status=Status.FAILED, detail=result.output() or f"exit {result.code}"
        )


def _transcript(argv: Sequence[str], result: CommandResult) -> str:
    """The command, and what it said, in one line that a report can print."""

    said = result.output()
    return " ".join(argv) if not said else f"{' '.join(argv)}: {said}"


def _atomic_write(path: Path, text: str) -> None:
    """Content, then a rename. Never a partially-written file under the real name."""

    temporary = path.parent / f".{path.name}.tmp"
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)
