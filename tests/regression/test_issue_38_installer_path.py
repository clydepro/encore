"""Regression: #38, #41 — the documented install command, on a machine like the guide describes.

Two defects in the same sentence — `sudo encore-install install --root /tmp/rehearsal` — found one
after the other, and the second was invisible until the first was fixed. That ordering is why they
share a file: read apart, #38 looks like a PATH problem and #41 like a design decision, and what
they are together is an installer whose only contact with a root shell was a `FakeMachine` that
agreed with whatever it was asked.

`tests/unit/test_installer_system.py` covers the resolution as a function and
`tests/integration/test_installer_lifecycle.py::TestOwnership` covers the executor's decision. What
lives here is the part that cannot be asserted from inside the process — bash finding the tool —
and the part that only a real filesystem answers.
"""

from __future__ import annotations

import os
import pwd
import subprocess
from pathlib import Path
from typing import Any

import pytest

from apps.installer.execute import Executor
from apps.installer.layout import Layout
from apps.installer.main import EXIT_OK, EXIT_PREREQUISITES, main
from apps.installer.plan import CreateDirectories
from apps.installer.system import RealMachine
from tests.support.installer import FakeMachine, RecordingRunner, synced

pytestmark = pytest.mark.regression

PROJECT_ROOT = Path(__file__).resolve().parents[2]
WRAPPER = PROJECT_ROOT / "scripts" / "encore-install"

#: sudo's default `secure_path` on Debian and Raspberry Pi OS. No user's home anywhere in it,
#: which is the entire content of #38: the guide says `curl … | sh` (installs into
#: `~/.local/bin`) and then says `sudo` (loses `~/.local/bin`).
SECURE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


def sudo_environment(*, path: str, home: str, as_user: str | None = None) -> dict[str, str]:
    """An environment with nothing in it but what a `sudo` run actually carries.

    A stripped environment rather than this process's with `PATH` edited: pytest's own environment
    already contains the user-local directory the bug is about, so an inherited one would make
    every test in this file pass by accident on the machine that reported #38.
    """

    environ = {"PATH": path, "HOME": home, "LANG": os.environ.get("LANG", "C.UTF-8")}
    if as_user is not None:
        environ["SUDO_USER"] = as_user
    return environ


# -- #38: uv is installed, and root cannot see it ---------------------------


def test_a_root_process_sees_a_per_user_install(tmp_path: Path, monkeypatch: Any) -> None:
    """#38's mechanism, on the real `RealMachine`: `SUDO_USER` reaches the operator's home.

    The operator's home is a temporary directory and `pwd.getpwnam` is the only element stubbed,
    because inventing a passwd entry for a name that may or may not exist on the runner would make
    the test assert its own fixture. Everything after that answer — the path assembly, the lookup,
    the mode check — is production code, and the file found is a file that exists.
    """

    operator_home = tmp_path / "home"
    installed = operator_home / ".local/bin/uv"
    installed.parent.mkdir(parents=True)
    installed.write_text("#!/bin/sh\nexit 0\n")
    installed.chmod(0o755)

    class Operator:
        pw_dir = str(operator_home)

    monkeypatch.setattr(pwd, "getpwnam", lambda _: Operator())
    monkeypatch.setattr(
        RealMachine,
        "environ",
        property(lambda _: sudo_environment(path=SECURE_PATH, home="/root", as_user="encoreop")),
    )

    assert RealMachine().which("uv") == installed


def test_the_wrapper_finds_uv_outside_path(tmp_path: Path) -> None:
    """#38 end to end, in the one place no Python test can reach: bash.

    `scripts/encore-install` is the command the Administrator Guide prints, so it is the first
    thing that goes wrong and the last thing a unit test can see. A script that only reports its
    arguments stands in for uv, which asserts both halves at once: that the tool was found where
    `PATH` does not point, and that the directory it was found in was handed down, so the
    installer and the bootstrap resolve uv the same way round.
    """

    fake = tmp_path / "home/.local/bin/uv"
    fake.parent.mkdir(parents=True)
    fake.write_text('#!/bin/sh\necho "uv $*"\n')
    fake.chmod(0o755)

    answer = subprocess.run(
        [str(WRAPPER), "install", "--root", str(tmp_path / "staged"), "--dry-run"],
        cwd=PROJECT_ROOT,
        env=sudo_environment(path=SECURE_PATH, home=str(tmp_path / "home")),
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )

    assert "uv run --frozen --no-dev python -m apps.installer.main" in answer.stdout, (
        answer.stdout + answer.stderr
    )


def test_the_wrapper_says_where_it_looked_when_uv_is_really_missing(tmp_path: Path) -> None:
    """A missing tool and an invisible tool need different sentences; #38 had the wrong one.

    The original message told the reporter to install uv, which they had done. Exit 3 was right,
    the prose was the bug, and a report that misdiagnoses is worse than one that stays quiet.
    """

    answer = subprocess.run(
        [str(WRAPPER), "install", "--root", str(tmp_path / "staged"), "--dry-run"],
        cwd=PROJECT_ROOT,
        env=sudo_environment(path=SECURE_PATH, home=str(tmp_path / "nowhere")),
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )

    printed = answer.stdout + answer.stderr
    assert answer.returncode == EXIT_PREREQUISITES, printed
    assert "where this command" in printed, printed
    assert "~/.local/bin" in printed, "the hint that would have answered #38 without an issue"


# -- #41: ownership of an account the rehearsal never created --------------


def test_a_rehearsal_run_as_root_does_not_abort_on_ownership(tmp_path: Path) -> None:
    """#41, on a real tree: staged, root, no `encore` account, five actions to finish.

    The lifecycle suite's `TestOwnership` proves this through the CLI; this proves the narrower
    thing that actually broke, in one call: an `Executor` that decides fatality from `geteuid()`
    aborts, and one that decides it from the observed account does not. `RecordingRunner` refuses
    a `chown` naming a group the machine says is absent, so the fake cannot be accidentally green
    about the exact failure that ended a real install.
    """

    layout = Layout(root=tmp_path)
    action = CreateDirectories(
        detail="create the appliance's directories (SAPRS 13.3)",
        entries=tuple(
            (layout.staged(entry.final), entry.owner, entry.group, entry.mode)
            for entry in layout.directories
        ),
    )
    for path, _, _, _ in action.entries:
        Path(path).mkdir(parents=True, exist_ok=True)

    runner = RecordingRunner()
    execution = Executor(runner, layout=layout, privileged=True, account_exists=False).apply(
        (action,)
    )

    assert execution.succeeded, execution.results[0].detail
    assert runner.ran("chown"), "an executor that never tried ownership rehearsed nothing"


def test_a_real_root_rehearsal_completes(tmp_path: Path) -> None:
    """The same claim without a recorder in the loop, for whoever is debugging on hardware.

    Skipped unless this is actually root: the point is a `chown` that fails for the reason real
    ones fail, and no fake can be trusted to fail that way — the fake failing is the bug this file
    is about. The lifecycle test runs everywhere; this one runs where it can lie the least.

    It is also the only test in this file that no CI run has ever executed, and it was wrong: the
    recorder had no `effects`, so `uv sync` was recorded and produced nothing, and the acceptance
    list's `environment` check failed the rehearsal for having no `encore-server` in its venv. A
    skipped test is a test nobody has run — which is the lesson #41's own handoff note, applied to
    the test that #41 added.
    """

    if os.geteuid() != 0:
        pytest.skip("only a root process can fail a chown the way #41 did")
    try:
        pwd.getpwnam("encore")
    except KeyError:
        pass
    else:  # pragma: no cover - an appliance, not a build runner
        pytest.skip("this machine has an 'encore' account, so there is nothing here to fail")

    staged = tmp_path / "staged"
    code = main(
        [
            "install",
            "--root",
            str(staged),
            "--no-service",
            "--music-dir",
            str(staged / "opt" / "music"),
            "--log-format",
            "json",
        ],
        machine=FakeMachine(
            root=True,
            user=False,
            recorder=RecordingRunner(effects=synced(), real=(("rsync",),)),
        ),
    )

    assert code == EXIT_OK, "the rehearsal aborted on an ownership it was never going to set"
    assert Path(staged / "etc/encore/config.yaml").is_file()
