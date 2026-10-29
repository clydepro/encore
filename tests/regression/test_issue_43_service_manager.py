"""Regression: #43, #44 — `sudo encore-install install`, on the machine the guide describes.

Step 15 has now failed three times in the gap between the fake and the host. #38 was a binary the
installer could not see, #41 an account it deliberately had not created, and these two are the same
shape again: #43 is a check that inferred "this machine has no service manager" from "this machine
has no encore.service", which is every machine one minute before its first install; #44 is what
fixing #43 exposes, because a rehearsal that was never reached cannot mutate a host.

The difference between this file and `tests/unit/test_installer_checks.py` is that the unit file
asserts a verdict and this one asserts the run: the exit code, the sentence an operator reads, and
the transcript of what was asked of PID 1. Commands are *executed* against a staging root rather
than rehearsed with `--dry-run`, because #44's claim is about what a run asks, and a dry run asks
nothing of anything.

On `main` this file cannot be imported — `Planning` and `systemd_running` do not exist yet, which is
the honest form of "red": the state that broke was not expressible.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from apps.installer.main import EXIT_OK, EXIT_PREREQUISITES, EXIT_VERIFICATION, main
from apps.installer.options import Command
from apps.installer.plan import Plan, Planning
from apps.installer.system import SYSTEMD_EVIDENCE, RealMachine, ServiceState
from tests.support.installer import FakeMachine, RecordingRunner, StubHttpProbe, synced

pytestmark = pytest.mark.regression

#: The commands a rehearsal may run for real: copying a tree the test then inspects.
_REAL = (("rsync",),)

#: What a machine that the test describes as *running* answers when `verify`/`upgrade` ask it.
#: `observe()` only probes when the unit is active, and an active unit that says nothing is a health
#: failure by design — so a test that claims "the appliance is up" has to let it answer, or it is
#: describing a sick box rather than a rehearsal.
_HEALTHY = {"http://127.0.0.1:8080/api/v1/health": {"status": "healthy", "components": []}}


def host(*, systemd: bool = True, unit: ServiceState | None = None) -> FakeMachine:
    """A Debian box with the tools installed, systemd running, and no Encore on it.

    `unit` defaults to the state a first install observes — `systemctl cat` finds no file,
    `is-active` answers `inactive`. That pair is #43, and before this branch the fake could not say
    it: `systemd=True` always arrived with an empty `detail`, which is exactly the inference the
    check made and the reason a green suite shipped an unusable installer.
    """

    return FakeMachine(
        recorder=RecordingRunner(effects=synced(), real=_REAL),
        root=True,
        systemd=systemd,
        unit=unit if unit is not None else ServiceState(detail="inactive"),
        binaries={"mpv": Path("/usr/bin/mpv"), "uv": Path("/usr/local/bin/uv")},
    )


def run(command: str, root: Path, machine: FakeMachine) -> int:
    """One command, staged under `root`, against the host the test described.

    The staged root is what makes this safe to *execute*: everything the plan writes lands under
    `root`, and the host is reached only through the recorder and the scripted answers — which is
    the property #44 is about, so it is worth having a helper that says it once.
    """

    return main(
        [
            command,
            "--root",
            str(root),
            "--music-dir",
            str(root / "opt" / "music"),
            "--json",
        ],
        machine=machine,
        # Asked and answered from a table: a machine whose service is `active` is probed on
        # 127.0.0.1:8080, and a test must not reach for a socket that might be a real appliance.
        http=StubHttpProbe(_HEALTHY),
    )


# -- #43: systemd is running, and there is no unit --------------------------


def test_a_missing_unit_is_not_a_missing_service_manager(tmp_path: Path, capsys: Any) -> None:
    """#43 as the operator met it: exit 3 on a machine that meets every prerequisite.

    The old check decided the machine was not installable from `state.service.detail` being
    non-empty — and on a first install that string is systemd saying `inactive` about a unit nobody
    has written yet. So it refused to do the one thing it was invoked to do, and advised
    `--no-service`, which leaves an appliance that never comes back after a power cut.
    """

    code = run("install", tmp_path, host())
    payload = json.loads(_json(capsys))
    assert code in (EXIT_OK, EXIT_VERIFICATION), code
    assert _verdict(payload, "service manager") == "ok", payload["checks"]
    # The unit's own absence is still reported, as a warning rather than a prerequisite: `verify`
    # needs the line, and `install` must not mistake one for the other.
    assert _verdict(payload, "unit") == "warn", payload["checks"]


def test_a_real_probe_of_a_missing_unit_answers_both_questions() -> None:
    """The confusion, on the machine running this test rather than on a fake.

    `systemctl is-active` of a unit that never existed prints `inactive` and exits 3. That is why
    `detail` was never empty on a fresh machine, and why a check that inferred the manager from it
    could only ever be wrong about the first install. Asserted against the real binary because it is
    the one fact in #43 no double can carry.
    """

    real = RealMachine()
    if not real.systemd_running():
        pytest.skip("this machine has no systemd, which is the case the check must still fail")
    assert SYSTEMD_EVIDENCE.is_dir()
    assert not real.service("encore-regression-probe-43").installed
    assert real.systemd_running(), "two facts, and they are allowed to disagree"


def test_a_machine_without_systemd_still_refuses(tmp_path: Path, capsys: Any) -> None:
    """The other half, because the fix must not become "install anywhere at all".

    No service manager is a real prerequisite failure and SAPRS 13.2's answer is a manual start. An
    installer that let a container through would exit zero on a box whose appliance never starts.
    """

    assert run("install", tmp_path, host(systemd=False)) is EXIT_PREREQUISITES
    assert _verdict(json.loads(_json(capsys)), "service manager") == "fail"


# -- #43's second sentence: the report must not describe a plan it never made --


def test_a_blocked_run_says_why_it_planned_nothing(tmp_path: Path, capsys: Any) -> None:
    """The line that made #43 unreadable, four lines above its own contradiction.

    `nothing to do — the machine already matches this request (SAPRS 13.7)` is 13.7's idempotence
    result. It was also what an empty `Plan` printed for a run that stopped before planning, and for
    `verify`, which never plans: three meanings and one sentence, so an operator read "already
    installed" and "a prerequisite is unmet" in the same report and neither was a description of
    what had happened.
    """

    main(
        ["install", "--root", str(tmp_path), "--music-dir", str(tmp_path / "opt" / "music")],
        machine=host(systemd=False),
        http=StubHttpProbe(_HEALTHY),
    )
    shown = capsys.readouterr().out
    assert "nothing to do" not in shown, shown
    assert "no plan" in shown, shown
    assert "A prerequisite is unmet" in shown, shown


def test_verify_stops_claiming_the_machine_matches(tmp_path: Path, capsys: Any) -> None:
    """`verify` asks questions. It has never planned, so it never had the right to say this."""

    main(["verify", "--root", str(tmp_path)], machine=host(), http=StubHttpProbe(_HEALTHY))
    shown = capsys.readouterr().out
    assert "nothing to do" not in shown, shown
    assert "no plan" in shown, shown


def test_an_idempotent_install_still_says_the_original_sentence() -> None:
    """The sentence is right where it was written, and keeping it working is the point.

    Deleting "nothing to do" from the report would trade one unreadable message for another: 13.7's
    idempotence is a result and this is the only word for it.
    """

    planned = Plan(command=Command.INSTALL, planning=Planning.PLANNED).describe()
    blocked = Plan(command=Command.INSTALL, planning=Planning.BLOCKED).describe()
    assert planned.startswith("nothing to do"), planned
    assert blocked.startswith("no plan"), blocked
    assert "prerequisite" in blocked, blocked


# -- #44: a rehearsal must not ask this machine to do anything ---------------


def test_a_rehearsal_of_an_upgrade_does_not_stop_the_appliance(tmp_path: Path) -> None:
    """The sharpest form of #44: `upgrade --root` used to silence a party.

    The host here is running Encore and the command is pointed at a prefix. Stop, start and disable
    are verbs of the host's service manager, and `systemctl` cannot be aimed at a directory — so
    what a rehearsal achieved was an interruption with no relationship to its own output, on the one
    machine in the fleet where anyone would notice.

    The tree is installed first: an upgrade is defined against an appliance, and 13.9's plan copies
    the application before it creates directories, so running it against a prefix that never held
    one fails in `rsync` for a reason that has nothing to do with this issue.
    """

    fresh = host()
    assert run("install", tmp_path, fresh) in (EXIT_OK, EXIT_VERIFICATION)
    machine = host(unit=ServiceState(installed=True, enabled=True, active=True))
    assert run("upgrade", tmp_path, machine) in (EXIT_OK, EXIT_VERIFICATION)
    for label, transcript in (
        ("install", fresh.recorder.transcript()),
        ("upgrade", machine.recorder.transcript()),
    ):
        assert "systemctl" not in transcript, f"{label}: {transcript}"
    assert "rsync" in fresh.recorder.transcript(), (
        "the rehearsal did run; only the host effects were withdrawn"
    )


def test_a_rehearsal_says_which_step_it_skipped(tmp_path: Path, capsys: Any) -> None:
    """A skipped step nobody mentions is how 13.6 becomes an optional flag."""

    run("install", tmp_path, host())
    shown = capsys.readouterr().out
    assert "systemctl daemon-reload && systemctl enable --now encore" in shown, shown
    assert (tmp_path / "etc/systemd/system/encore.service").is_file(), shown


def _json(capsys: Any) -> str:
    raw: str = capsys.readouterr().out
    return raw[raw.find("{") :]


def _verdict(payload: dict[str, Any], name: str) -> str:
    for check in payload.get("checks", []):
        if check["name"] == name:
            verdict: str = check["verdict"]
            return verdict
    raise AssertionError(f"{name} was not assessed: {payload.get('checks')}")
