"""The report: what happened, what it means, and what to do next (SAPRS 13.7, 13.12).

13.12 asks the installer to *tell the operator what to do next* — the requirement most installers
satisfy with a cursor and nothing else. These tests treat the report as an interface: the exit
codes are what a provisioning system branches on, the text is what a human reads, and the two must
not disagree about what happened.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from apps.installer.checks import Check, Verdict
from apps.installer.execute import Execution, Result, Status
from apps.installer.layout import Layout
from apps.installer.options import Command, Options, Settings
from apps.installer.plan import CreateDirectories, EnsureFile, Plan
from apps.installer.report import Report, next_steps
from encore.config.models import EncoreConfig

SECRET = "correct horse battery staple"  # pragma: allowlist secret


def options(
    root: Path, *, command: Command = Command.INSTALL, service: bool = False, **values: Any
) -> Options:
    return Options(
        command=command,
        layout=Layout(root=root),
        settings=Settings(music_dir=root / "opt" / "music"),
        service=service,
        **values,
    )


def directories(root: Path) -> CreateDirectories:
    layout = Layout(root=root)
    return CreateDirectories(
        entries=((layout.staged(layout.state), "encore", "encore", 0o750),),
        detail="create the appliance's directories (SAPRS 13.3)",
    )


def report(
    root: Path,
    *,
    command: Command = Command.INSTALL,
    service: bool = False,
    status: Status = Status.DONE,
    checks: tuple[Check, ...] = (),
    notes: tuple[str, ...] = (),
    actions: tuple[Any, ...] | None = None,
    exit_code: int = 0,
    **values: Any,
) -> Report:
    one = directories(root) if actions is None else actions[0]
    return Report(
        options=options(root, command=command, service=service, **values),
        plan=Plan(command=command, actions=tuple(actions or (one,)), notes=notes),
        execution=Execution(results=(Result(action=one, status=status, detail="1 path(s)"),)),
        checks=checks or (ok("privilege", "running as root"),),
        notes=notes,
        exit_code=exit_code,
    )


def ok(name: str, detail: str = "fine") -> Check:
    return Check(name=name, verdict=Verdict.OK, detail=detail)


def warn(name: str, detail: str = "hmm", fix: str = "") -> Check:
    return Check(name=name, verdict=Verdict.WARN, detail=detail, fix=fix)


def fail(name: str, detail: str = "broken", fix: str = "") -> Check:
    return Check(name=name, verdict=Verdict.FAIL, detail=detail, fix=fix)


def index_of(items: list[str], needle: str) -> int:
    return next(index for index, item in enumerate(items) if needle in item)


# -- the text --------------------------------------------------------------------


def test_the_report_names_the_command_it_answers(tmp_path: Path) -> None:
    assert "encore-install install" in report(tmp_path).text(config=EncoreConfig())


def test_a_staged_report_says_where_the_appliance_went(tmp_path: Path) -> None:
    """Without this line, the first thing an operator does is look for it on the wrong machine."""

    assert str(tmp_path) in report(tmp_path).text(config=EncoreConfig())


def test_every_action_appears_numbered(tmp_path: Path) -> None:
    """The plan section is what `--dry-run` exists to show; a summary would not do."""

    text = report(tmp_path).text(config=EncoreConfig())
    assert "Plan:" in text
    assert " 1. create the appliance's directories" in text


def test_the_result_says_how_many_things_changed(tmp_path: Path) -> None:
    assert "1 action(s) performed" in report(tmp_path).text(config=EncoreConfig())


def test_a_failure_is_visible_in_the_verdict_as_well_as_the_flow(tmp_path: Path) -> None:
    """A reader skims the last line; a reader audits the middle. Both have to be right."""

    text = report(tmp_path, status=Status.FAILED).text(config=EncoreConfig())
    assert "failed" in text.lower()
    assert "did not" in text.lower() or "FAILED" in text


def test_warnings_are_counted_and_listed(tmp_path: Path) -> None:
    text = report(tmp_path, checks=(ok("privilege"), warn("music", "not mounted"))).text(
        config=EncoreConfig()
    )
    assert "1 warning(s)" in text
    assert "music: not mounted" in text


def test_a_failed_check_carries_the_command_that_fixes_it(tmp_path: Path) -> None:
    """13.12 as a formatting rule: the fix travels with the finding, not to a wiki."""

    text = report(
        tmp_path, checks=(fail("binaries", "mpv is missing", "sudo apt-get install -y mpv"),)
    ).text(config=EncoreConfig())
    assert "sudo apt-get install -y mpv" in text


def test_an_empty_plan_reads_as_a_result_rather_than_as_silence(tmp_path: Path) -> None:
    shown = Report(
        options=options(tmp_path),
        plan=Plan(command=Command.INSTALL),
        execution=Execution(),
        checks=(),
        notes=(),
        exit_code=0,
    )
    assert "nothing to do" in shown.text(config=EncoreConfig())


def test_notes_are_not_numbered(tmp_path: Path) -> None:
    """A note is not a step; numbering one would have an operator hunting for a change."""

    text = report(tmp_path, notes=("the music directory is not touched",)).text(
        config=EncoreConfig()
    )
    assert "note  the music directory is not touched" in text
    assert "2. the music" not in text


# -- next steps -------------------------------------------------------------------


def test_the_first_suggestion_is_the_one_that_proves_it_worked(tmp_path: Path) -> None:
    steps = [title for title, _ in next_steps(options(tmp_path), EncoreConfig())]
    assert any("http://" in step for step in steps[:2]), steps


def test_the_library_is_the_second_thing_anybody_needs(tmp_path: Path) -> None:
    """An appliance with no library is a jukebox with nothing on it (ADR-010).

    Searched in the commands as well as the sentences, because `encore-builder` appears where an
    operator copies it from — the command line under the step.
    """

    steps = [" ".join(step) for step in next_steps(options(tmp_path), EncoreConfig())]
    assert index_of(steps, "encore-builder") < index_of(steps, "/admin"), steps


def test_an_upgrade_does_not_tell_anybody_to_build_a_library(tmp_path: Path) -> None:
    steps = [
        title for title, _ in next_steps(options(tmp_path, command=Command.UPGRADE), EncoreConfig())
    ]
    assert not any("encore-builder" in step for step in steps), steps


def test_a_removal_says_what_survived_it(tmp_path: Path) -> None:
    steps = [
        title for title, _ in next_steps(options(tmp_path, command=Command.REMOVE), EncoreConfig())
    ]
    assert any("left" in step.lower() or "where they were" in step.lower() for step in steps), steps


def test_the_no_service_case_says_how_to_start_the_thing(tmp_path: Path) -> None:
    steps = [title for title, _ in next_steps(options(tmp_path, service=False), EncoreConfig())]
    assert any("run-server" in step or "start" in step.lower() for step in steps), steps


def test_the_guest_url_in_the_report_matches_the_one_the_qr_encodes(tmp_path: Path) -> None:
    """Two sentences about the address are two chances to disagree (issue #32)."""

    config = EncoreConfig()
    steps = " ".join(
        f"{title} {command}"
        for title, command in next_steps(options(tmp_path, service=False), config)
    )
    from encore.utilities.qr import target

    assert target(config.server.domain, config.server.port) in steps


# -- the JSON -----------------------------------------------------------------------


def test_the_json_carries_the_same_facts_as_the_text(tmp_path: Path) -> None:
    payload = json.loads(
        report(tmp_path, checks=(ok("privilege"), fail("port", "in use"))).json(
            config=EncoreConfig()
        )
    )
    assert payload["command"] == "install"
    assert payload["performed"] == 1
    assert payload["plan"][0]["kind"] == "CreateDirectories"
    assert payload["results"][0]["status"] == "done"
    assert any(
        check["name"] == "port" and check["verdict"] == "fail" for check in payload["checks"]
    )


def test_the_json_reports_the_exit_code_the_process_will_use(tmp_path: Path) -> None:
    """The field a script branches on has to be the number `main` returns."""

    shown = report(tmp_path, exit_code=4)
    assert json.loads(shown.json(config=EncoreConfig()))["exit_code"] == 4
    assert shown.ok is False


def test_the_json_carries_the_address_the_appliance_answers_on(tmp_path: Path) -> None:
    """A provisioning system that prints a QR code needs the URL from the same source."""

    from encore.utilities.qr import target

    config = EncoreConfig()
    payload = json.loads(report(tmp_path).json(config=config))
    assert payload["guest_url"] == target(config.server.domain, config.server.port)


def test_no_password_appears_in_either_rendering(tmp_path: Path) -> None:
    """The environment file holds one; the report describes the file, never its contents.

    `EnsureFile` carries the rendered text — which is what lets `--dry-run` show what would be
    written — so the redaction has to happen on the way out rather than on the way in.
    """

    layout = Layout(root=tmp_path)
    written = EnsureFile(
        path=layout.staged(layout.environment_file),
        text=f"ENCORE_ADMIN_PASSWORD={SECRET}\n",
        owner="root",
        group="encore",
        mode=0o600,
        purpose="the environment",
        detail="write /etc/encore/environment",
    )
    shown = report(tmp_path, actions=(written,))
    assert SECRET not in shown.text(config=EncoreConfig())
    assert SECRET not in shown.json(config=EncoreConfig())


# -- verdicts -------------------------------------------------------------------------


def test_a_report_that_changed_nothing_is_not_a_failure(tmp_path: Path) -> None:
    shown = Report(
        options=options(tmp_path),
        plan=Plan(command=Command.INSTALL, actions=(directories(tmp_path),)),
        execution=Execution(
            results=(Result(action=directories(tmp_path), status=Status.SKIPPED, detail="there"),)
        ),
        checks=(ok("privilege"),),
        notes=(),
        exit_code=0,
    )
    assert shown.did_nothing


def test_a_failing_check_and_a_failing_action_produce_different_verdicts(tmp_path: Path) -> None:
    """ "nothing was changed" and "something is wrong with this machine" are different advice."""

    action_failed = report(tmp_path, status=Status.FAILED).text(config=EncoreConfig())
    check_failed = report(tmp_path, checks=(fail("disk", "no space"),)).text(config=EncoreConfig())
    assert action_failed != check_failed
    assert "disk: no space" in check_failed
