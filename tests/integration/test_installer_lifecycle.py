"""The installer, executed against a real tree (SAPRS 13.7, 13.8, 13.9, 13.10, 13.11).

`tests/unit/test_installer_plan.py` proves the plan is right. This proves the plan is *true*: that
the actions, run once, leave files at the modes claimed, and that running them again does nothing.

The second half is the reason the file exists. A pure planner can be unit-tested into an empty plan
by an `ApplianceState` hand-built to say "already installed"; only a real `stat` after a real write
shows that the observer and the executor agree about what a byte is. That agreement is what every
retry, every re-run after a power cut, and every "did it work?" depends on.

Everything here goes through `main()`, with a :class:`~tests.support.installer.FakeMachine`
injected. Not past it: driving `observe`/`plan_for`/`Executor` by hand would reimplement the nine
steps the CLI actually takes, and a hand-assembled pipeline is a pipeline that stops being tested
the moment `main.py` learns a new step. `FakeMachine` is the real filesystem with scripted answers
about users, systemd and ports, so no test here needs root, systemd, or a Pi.

`--root` staging is what makes this possible, and it is also what makes these rehearsals rather
than installs: a staged tree renders its own paths into the files it writes, which is why a staged
appliance is bootable (`tests/e2e/test_installed_appliance.py`) rather than merely present.
"""

from __future__ import annotations

import json
import shutil
import stat
from pathlib import Path
from typing import Any

import pytest

from apps.installer.layout import Layout
from apps.installer.main import EXIT_OK, EXIT_PREREQUISITES, EXIT_USAGE, EXIT_VERIFICATION, main
from apps.installer.system import OsRelease, ServiceState
from tests.support.installer import FakeMachine, RecordingRunner, StubHttpProbe, synced

pytestmark = pytest.mark.integration

BASE = ["--no-service", "--log-format", "console"]

#: The commands this file lets run for real. Copying a project into a staging prefix is a
#: filesystem operation the acceptance list later inspects, and emulating it would mean asserting
#: on the emulation. Everything that changes a *host* — `useradd`, `systemctl` — stays recorded.
_REAL_COMMANDS = (("rsync",),)


def pi(recorder: RecordingRunner | None = None, **answers: Any) -> FakeMachine:
    """A Debian-with-mpv host, which is the machine SAPRS 13.1 says all this is for.

    `uv sync` gets an effect rather than a silence: the command is still recorded, and the
    environment it pretends to build appears, because an acceptance list that found no
    `venv/bin/encore-server` would be reporting a real property of a machine where the resolver
    had never run (see `tests.support.installer.synced`).
    """

    return FakeMachine(
        recorder=(
            recorder
            if recorder is not None
            else RecordingRunner(effects=synced(), real=_REAL_COMMANDS)
        ),
        os_release=OsRelease(id="debian"),
        binaries={"mpv": Path("/usr/bin/mpv"), "uv": Path("/usr/local/bin/uv")},
        **answers,
    )


def invoke(
    root: Path,
    *arguments: str,
    capsys: pytest.CaptureFixture[str],
    machine: FakeMachine | None = None,
    http: StubHttpProbe | None = None,
) -> tuple[int, dict[str, Any]]:
    """One invocation, staged under `root`, and what the machine and the report say afterwards.

    The JSON is returned parsed rather than as text: 13.11's acceptance list and 13.7's "report
    next steps" are both contracts, and a contract read as a string is a contract half-checked.
    """

    host = machine if machine is not None else pi()
    code = main([*arguments, "--root", str(root), "--json"], machine=host, http=http)
    payload = capsys.readouterr().out
    start = payload.find("{")
    return code, (json.loads(payload[start:]) if start >= 0 else {})


def install(
    root: Path,
    capsys: pytest.CaptureFixture[str],
    *,
    machine: FakeMachine | None = None,
    **answers: Any,
) -> int:
    """A first install, staged, without systemd. Returns the exit code."""

    host = machine if machine is not None else pi(**answers)
    code, _ = invoke(
        root,
        "install",
        *BASE,
        "--music-dir",
        str(root / "opt" / "music"),
        capsys=capsys,
        machine=host,
    )
    return code


class TestFilesLanded:
    def test_an_install_ends_in_a_zero_exit(self, tmp_path: Path, capsys: Any) -> None:
        assert install(tmp_path, capsys) == EXIT_OK

    def test_the_tree_contains_every_directory_the_layout_promises(
        self, tmp_path: Path, capsys: Any
    ) -> None:
        """Checked against `Layout` rather than against a list written here, so that adding a
        directory to the table without creating it is a failure rather than a surprise."""

        layout = Layout(root=tmp_path)
        install(tmp_path, capsys)
        for entry in layout.directories:
            assert layout.staged(entry.final).is_dir(), f"{entry.final} was promised and absent"

    def test_every_managed_file_exists_and_says_something(
        self, tmp_path: Path, capsys: Any
    ) -> None:
        """The two files this host runs, and the two `--no-service` declines to write.

        The unit and the tmpfiles rule are skipped together (`plan._file_actions`), because both
        are the machine's boot-time contract rather than its runtime one, and `--no-service` means
        "there is no service". Asserting them absent here is what makes their presence in
        `TestServiceIntegration` mean something: without it, a plan that wrote them anyway would
        still pass.
        """

        layout = Layout(root=tmp_path)
        install(tmp_path, capsys)
        skipped = {layout.unit_file, layout.tmpfiles_file}
        for entry in layout.files:
            written = layout.staged(entry.final)
            if entry.final in skipped:
                assert not written.exists(), f"--no-service wrote {entry.final} anyway"
                continue
            assert written.is_file(), f"{entry.final} was promised and absent"
            assert written.read_text().strip(), f"{written} is empty"

    def test_the_configuration_survives_the_servers_own_loader(
        self, tmp_path: Path, capsys: Any
    ) -> None:
        """YAML that parses and a configuration the Server accepts are different claims.

        Read through `ConfigurationService`, the code path an appliance takes at startup, because
        the interesting failure is a file that is valid YAML and invalid Encore.
        """

        from encore.config.service import ConfigurationService

        layout = Layout(root=tmp_path)
        install(tmp_path, capsys)
        loaded = ConfigurationService(layout.staged(layout.config_file)).load()
        assert loaded.server.port == 8080
        assert loaded.paths.music_dir == layout.staged("/opt/music")

    def test_the_service_is_told_where_the_staged_files_are(
        self, tmp_path: Path, capsys: Any
    ) -> None:
        """The unit's `ExecStart` and `EnvironmentFile` have to name the prefix.

        An un-staged path in the unit is the one way a rehearsal could touch a production
        database, which would make this whole file unsafe rather than merely slow.
        """

        layout = Layout(root=tmp_path)
        invoke(
            tmp_path,
            "install",
            "--music-dir",
            str(tmp_path / "opt" / "music"),
            "--log-format",
            "console",
            capsys=capsys,
            machine=pi(systemd=True, user=True, unit=ServiceState(installed=True, enabled=True)),
        )
        unit = layout.staged(layout.unit_file).read_text()
        assert str(tmp_path) in unit
        for pattern in ("=/var/lib/encore", '"/var/lib/encore', " /var/lib/encore/"):
            assert pattern not in unit, f"the unit still points at {pattern.strip()}"

    def test_the_directories_are_owner_accessible_at_least(
        self, tmp_path: Path, capsys: Any
    ) -> None:
        """13.5's modes, read back off the disk.

        The installer plans a mode and `umask` decides one; this is the assertion that notices
        when the two disagree, which is silent until the service cannot write its socket.
        """

        layout = Layout(root=tmp_path)
        install(tmp_path, capsys)
        for entry in layout.directories:
            mode = stat.S_IMODE(layout.staged(entry.final).stat().st_mode)
            assert mode & 0o700, f"{entry.final} is {mode:o}"

    def test_the_ipc_socket_directory_is_private(self, tmp_path: Path, capsys: Any) -> None:
        """0700, because the mpv socket is a command channel (SAPRS 7.6)."""

        layout = Layout(root=tmp_path)
        install(tmp_path, capsys)
        mode = stat.S_IMODE(layout.staged(layout.temp).stat().st_mode)
        assert mode & 0o077 == 0, f"{layout.temp} is {mode:o}"

    def test_the_environment_file_holds_the_credential_at_0600(
        self, tmp_path: Path, capsys: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The one file on the appliance that contains a secret, and its mode.

        0644 here would put the operator's password in the hands of every account on a machine
        whose whole security model is one shared password and a LAN.
        """

        secret = "correct horse battery staple"  # pragma: allowlist secret
        monkeypatch.setenv("ENCORE_ADMIN_PASSWORD", secret)
        layout = Layout(root=tmp_path)
        install(tmp_path, capsys)
        environment = layout.staged(layout.environment_file)
        assert stat.S_IMODE(environment.stat().st_mode) == 0o600
        assert secret in environment.read_text()


class TestIdempotence:
    def test_the_second_install_changes_nothing(self, tmp_path: Path, capsys: Any) -> None:
        """SAPRS 13.7's requirement, asserted on the filesystem rather than on intentions.

        Timestamps included: an executor that rewrote a file with identical bytes would pass a
        contents-only comparison and still restart a service that had no reason to restart.
        """

        assert install(tmp_path, capsys) == EXIT_OK
        before = _fingerprint(tmp_path)
        assert install(tmp_path, capsys) == EXIT_OK
        assert _fingerprint(tmp_path) == before

    def test_the_second_install_reports_no_work(self, tmp_path: Path, capsys: Any) -> None:
        install(tmp_path, capsys)
        _, payload = invoke(
            tmp_path,
            "install",
            *BASE,
            "--music-dir",
            str(tmp_path / "opt" / "music"),
            capsys=capsys,
        )
        assert payload["performed"] == 0
        assert payload["plan"] == []

    def test_an_interrupted_install_is_completed_rather_than_repeated(
        self, tmp_path: Path, capsys: Any
    ) -> None:
        """The half-machine case: one artifact deleted, and the plan narrows to that one.

        Modeled by deleting a file rather than by scripting state, so the observer has to find the
        hole from the filesystem — which is how an interrupted install actually presents.
        """

        layout = Layout(root=tmp_path)
        install(tmp_path, capsys)
        layout.staged(layout.environment_file).unlink()
        _, payload = invoke(
            tmp_path,
            "install",
            *BASE,
            "--music-dir",
            str(tmp_path / "opt" / "music"),
            capsys=capsys,
        )
        assert payload["performed"] == 1, payload["plan"]
        assert layout.staged(layout.environment_file).is_file()

    def test_a_file_written_by_hand_is_not_overwritten(self, tmp_path: Path, capsys: Any) -> None:
        """The configuration is the exception, and it is worth being precise about why.

        Everything else the installer manages is generated, and therefore safe to rewrite; the
        file an operator edits is not, whatever its bytes currently say (SAPRS 13.8).
        """

        layout = Layout(root=tmp_path)
        install(tmp_path, capsys)
        edited = layout.staged(layout.config_file)
        edited.write_text(edited.read_text().replace("  port: 8080", "  port: 9100"))
        assert install(tmp_path, capsys) == EXIT_OK
        assert "  port: 9100" in edited.read_text()

    def test_the_disagreement_between_file_and_flag_is_reported(
        self, tmp_path: Path, capsys: Any
    ) -> None:
        """Preservation without a report is a silently ignored flag.

        The file wins, and the test above proves it. This proves the installer says so: "your
        `--port` did not apply" is the difference between an appliance on the wrong port and an
        outage nobody can explain to the room.
        """

        layout = Layout(root=tmp_path)
        install(tmp_path, capsys)
        edited = layout.staged(layout.config_file)
        edited.write_text(edited.read_text().replace("  port: 8080", "  port: 9100"))
        code, payload = invoke(
            tmp_path,
            "install",
            *BASE,
            "--port",
            "8099",
            "--music-dir",
            str(tmp_path / "opt" / "music"),
            capsys=capsys,
        )
        assert code == EXIT_OK
        named = json.dumps(payload["plan"])
        assert "config" in named, "a preserved configuration must appear in the plan"
        assert "server.port" in named, "and the setting that disagreed must be named"


class TestServiceIntegration:
    def test_the_commands_are_the_ones_the_guide_prints(self, tmp_path: Path, capsys: Any) -> None:
        """13.6's "every command is logged", read off the transcript.

        Asserted as exact argv rather than as "systemctl was mentioned": the difference between
        `systemctl enable encore.service` and `systemctl enable --now` is invisible to the loose
        version and decides whether the box comes back after a power cut.
        """

        recorder = RecordingRunner(effects=synced(), real=_REAL_COMMANDS)
        host = pi(systemd=True, user=True, unit=ServiceState(), recorder=recorder)
        code, _ = invoke(
            tmp_path, "install", "--music-dir", str(tmp_path / "m"), capsys=capsys, machine=host
        )
        transcript = recorder.transcript()
        assert "systemctl daemon-reload" in transcript, transcript
        assert "systemctl enable encore" in transcript, transcript
        assert code in (EXIT_OK, EXIT_VERIFICATION)

    def test_no_service_means_no_systemd_at_all(self, tmp_path: Path, capsys: Any) -> None:
        recorder = RecordingRunner(effects=synced(), real=_REAL_COMMANDS)
        invoke(
            tmp_path,
            "install",
            *BASE,
            "--music-dir",
            str(tmp_path / "m"),
            capsys=capsys,
            machine=pi(systemd=True, recorder=recorder),
        )
        assert not recorder.ran("systemctl"), recorder.transcript()

    def test_a_machine_without_systemd_is_named_not_rescued(
        self, tmp_path: Path, capsys: Any
    ) -> None:
        """`--no-service` is a decision; systemd missing is a fact about the machine.

        Only the first is allowed to install successfully, and the difference matters to anyone
        automating installs: a container that quietly skipped the unit file would exit zero.
        """

        code, _ = invoke(
            tmp_path,
            "install",
            "--music-dir",
            str(tmp_path / "opt" / "music"),
            "--log-format",
            "console",
            capsys=capsys,
            machine=pi(systemd=False),
        )
        assert code == EXIT_PREREQUISITES

    def test_the_service_user_is_created_once(self, tmp_path: Path, capsys: Any) -> None:
        recorder = RecordingRunner(effects=synced(), real=_REAL_COMMANDS)
        host = pi(systemd=True, user=False, recorder=recorder)
        invoke(tmp_path, "install", "--music-dir", str(tmp_path / "m"), capsys=capsys, machine=host)
        assert recorder.times("useradd") <= 1, recorder.transcript()


class TestRemoval:
    def test_a_default_removal_keeps_the_configuration_and_the_music(
        self, tmp_path: Path, capsys: Any
    ) -> None:
        """13.10's first rule: what is hard to reproduce is what does not get deleted."""

        layout = Layout(root=tmp_path)
        install(tmp_path, capsys)
        music = layout.staged("/opt/music")
        music.mkdir(parents=True, exist_ok=True)
        (music / "keep.flac").write_text("three hours of somebody's life")
        assert _remove(tmp_path, capsys) == EXIT_OK
        assert layout.staged(layout.config_file).is_file(), "the configuration was removed"
        assert (music / "keep.flac").is_file(), "the music was removed"
        assert not layout.staged(layout.application).exists(), "the application was not removed"

    def test_purging_is_two_separate_decisions(self, tmp_path: Path, capsys: Any) -> None:
        """`--purge-config` and `--purge-data` are different flags because they are different acts.

        All four combinations, because a plan that conflated them would pass a test that named
        only the last one.
        """

        for purge_config in (False, True):
            for purge_data in (False, True):
                root = tmp_path / f"{int(purge_config)}{int(purge_data)}"
                root.mkdir()
                layout = Layout(root=root)
                install(root, capsys)
                database = layout.staged(layout.state) / "runtime.db"
                database.parent.mkdir(parents=True, exist_ok=True)
                database.write_bytes(b"the party's history")
                _remove(root, capsys, purge_config=purge_config, purge_data=purge_data)
                assert layout.staged(layout.config_file).exists() is not purge_config
                assert database.exists() is not purge_data

    def test_a_removal_needs_an_explicit_yes(self, tmp_path: Path, capsys: Any) -> None:
        """13.10's guard, and the reason there is no interactive prompt.

        A program that read "yes" from a pipe is a program one `yes | encore-install remove
        --purge-data` away from destroying a library index, and a prompt is exactly that.
        """

        install(tmp_path, capsys)
        code, _ = invoke(tmp_path, "remove", capsys=capsys)
        assert code == EXIT_USAGE

    def test_removal_does_not_follow_a_link_out_of_the_root(
        self, tmp_path: Path, capsys: Any
    ) -> None:
        """The check that makes `--purge-data` survivable.

        Every deletion is inside a root the layout owns. A symlink planted at one of them is the
        accident and the attack at once, and the executor refuses it rather than resolving it.
        """

        layout = Layout(root=tmp_path)
        install(tmp_path, capsys)
        victim = tmp_path / "somebody_elses_things"
        victim.mkdir()
        (victim / "precious").write_text("do not delete me")
        state = layout.staged(layout.state)
        shutil.rmtree(state)
        state.symlink_to(victim, target_is_directory=True)
        _remove(tmp_path, capsys, purge_data=True)
        assert (victim / "precious").is_file(), "the installer followed a symlink out of the root"


class TestVerify:
    def test_a_box_with_nothing_installed_is_reported_as_such(
        self, tmp_path: Path, capsys: Any
    ) -> None:
        code, _ = invoke(tmp_path, "verify", capsys=capsys)
        assert code == EXIT_VERIFICATION

    def test_an_installed_box_verifies_cleanly_or_with_warnings(
        self, tmp_path: Path, capsys: Any
    ) -> None:
        """Warnings rather than failures: no library and no `encore` account are true of a
        rehearsal. The distinction is 13.11's, and the exit code is where it is observable — an
        appliance with work left to do is not an appliance that is broken.
        """

        machine = pi(systemd=True, user=True, unit=ServiceState(installed=True, enabled=True))
        install(tmp_path, capsys, machine=machine)
        code, payload = invoke(
            tmp_path,
            "verify",
            capsys=capsys,
            machine=pi(systemd=True, user=True, unit=ServiceState(installed=True, enabled=True)),
            http=StubHttpProbe({"http://127.0.0.1:8080/api/v1/health": {"status": "ok"}}),
        )
        assert code == EXIT_OK, [(c["name"], c["verdict"], c["detail"]) for c in payload["checks"]]
        assert all(item["verdict"] != "fail" for item in payload["checks"]), payload["checks"]

    def test_verify_names_the_library_that_is_missing(self, tmp_path: Path, capsys: Any) -> None:
        install(tmp_path, capsys)
        _, payload = invoke(tmp_path, "verify", capsys=capsys)
        checks = {str(item["name"]): item for item in payload["checks"]}
        assert checks["library"]["verdict"] == "warn"
        assert str(tmp_path) in checks["library"]["detail"]

    def test_a_configuration_that_is_invalid_fails_the_check(
        self, tmp_path: Path, capsys: Any
    ) -> None:
        """The one case where preserving the file hurts, which is the case the rule is for."""

        layout = Layout(root=tmp_path)
        install(tmp_path, capsys)
        layout.staged(layout.config_file).write_text("server:\n  port: purple\n")
        code, _ = invoke(tmp_path, "verify", capsys=capsys)
        assert code == EXIT_VERIFICATION

    def test_verify_changes_nothing(self, tmp_path: Path, capsys: Any) -> None:
        """An acceptance list that fixed what it found would not be an acceptance list."""

        install(tmp_path, capsys)
        before = _fingerprint(tmp_path)
        invoke(tmp_path, "verify", capsys=capsys)
        assert _fingerprint(tmp_path) == before


class TestReports:
    def test_the_json_report_is_data(self, tmp_path: Path, capsys: Any) -> None:
        """`json.loads` is the assertion; a report that only printed would pass a text test."""

        install(tmp_path, capsys)
        _, payload = invoke(
            tmp_path,
            "install",
            *BASE,
            "--music-dir",
            str(tmp_path / "opt" / "music"),
            capsys=capsys,
        )
        assert payload["command"] == "install"
        assert isinstance(payload["plan"], list)
        assert isinstance(payload["checks"], list)
        assert payload["performed"] <= len(payload["plan"])

    def test_the_password_appears_in_the_report_nowhere(
        self, tmp_path: Path, capsys: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """12.6, applied to the installer's own output.

        The environment file legitimately contains the credential — that is where an appliance
        reads it from. The report must not, because the report is what gets pasted into an issue.
        """

        monkeypatch.setenv(
            "ENCORE_ADMIN_PASSWORD", "correct horse battery staple"
        )  # pragma: allowlist secret
        install(tmp_path, capsys)
        _, payload = invoke(
            tmp_path,
            "install",
            *BASE,
            "--music-dir",
            str(tmp_path / "opt" / "music"),
            capsys=capsys,
        )
        assert "correct horse battery staple" not in json.dumps(payload)

    def test_the_next_steps_describe_this_machine(self, tmp_path: Path, capsys: Any) -> None:
        """A report that ends without "what now" ends with a search.

        The staged case matters most: the generic step is `systemctl start encore`, which does not
        exist here, and an operator who followed it would conclude the installer had lied to them.
        """

        install(tmp_path, capsys)
        _, payload = invoke(
            tmp_path,
            "install",
            *BASE,
            "--music-dir",
            str(tmp_path / "opt" / "music"),
            capsys=capsys,
        )
        assert "systemctl start encore" not in json.dumps(payload)


# -- the plumbing this file needs and nothing else owns ------------------------------------


def _remove(
    root: Path,
    capsys: pytest.CaptureFixture[str],
    *,
    purge_config: bool = False,
    purge_data: bool = False,
) -> int:
    # `--no-service`, because a removal that tried to talk to systemd would be testing the
    # machine rather than the plan, and `FakeMachine` would answer that it has none.
    arguments = ["remove", "--yes", "--no-service"]
    arguments += ["--purge-config"] * purge_config + ["--purge-data"] * purge_data
    code, _ = invoke(root, *arguments, capsys=capsys)
    return code


def _fingerprint(root: Path) -> dict[str, int]:
    """Every file under the staging prefix, with its modification time in nanoseconds."""

    return {
        str(path.relative_to(root)): path.stat().st_mtime_ns
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }
