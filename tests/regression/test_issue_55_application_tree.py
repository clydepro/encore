"""Regression: #55 — the installer decided "already installed" by reading `pyproject.toml`.

`upgrade` reported `nothing to do` on a machine two merged PRs behind. Four things were true at
once, and only the fourth was wrong: the source manifest and the installed manifest matched, the
lockfile matched the stamp inside the venv, `systemctl is-active` said `active`, and sixteen
Python files on disk were not the ones this checkout contains. #51 and #54 had both shipped
changes under `apps/builder/` and `encore/` without touching a manifest, which is the normal
shape of a bug fix here — so the comparison was not misread, it was reading a file a release has
no reason to change.

The rule the fix installs lives in `apps/installer/application.py`: one exclusion list, one tree
walk, one content comparison, used by the copier and the observer alike. That is why every test in
this file copies a tree for real. A fake that agreed to disagree about 16 files would be asserting
on the emulation, and the bug was in the agreement.

See also
    [#55](https://github.com/clydepro/encore/issues/55),
    [docs/adr/ADR-014-installer-as-a-third-application.md](../../docs/adr/ADR-014-installer-as-a-third-application.md),
    [docs/SAPRS/Encore-SAPRS.md](../../docs/SAPRS/Encore-SAPRS.md) §13.9, §13.11
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from apps.installer.application import manifest
from apps.installer.layout import Layout
from apps.installer.main import EXIT_OK, EXIT_VERIFICATION, main
from apps.installer.options import Command, Options, Settings
from apps.installer.plan import InstallApplication, install_plan
from apps.installer.system import ApplianceState, observe
from encore.config.models import EncoreConfig
from tests.support.installer import FakeMachine, RecordingRunner, synced

pytestmark = pytest.mark.regression

#: Copying a project into a staging prefix is a filesystem operation the acceptance list then
#: inspects, so it runs for real — exactly as the lifecycle suite lets it.
_REAL = (("rsync",),)


@pytest.fixture
def source(tmp_path: Path) -> Path:
    """A checkout to install from.

    Small on purpose. The copy is real, and the point of the file is a byte that changes while
    nothing else does; installing the actual repository would turn "add a file upstream" into a
    400-file rsync per test, and would make these tests depend on what is in the tree.
    """

    root = tmp_path / "checkout"
    (root / "encore").mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        "[project]\nname = 'encore'\nversion = '0.5.0'\n", encoding="utf-8"
    )
    (root / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (root / "encore" / "__init__.py").write_text('__version__ = "0.5.0"\n', encoding="utf-8")
    (root / "encore" / "playback.py").write_text("class PlaybackService: ...\n", encoding="utf-8")
    return root


def pi() -> FakeMachine:
    """A host with mpv and uv on it, and a recorder that really copies."""

    return FakeMachine(
        recorder=RecordingRunner(effects=synced(), real=_REAL),
        binaries={"mpv": Path("/usr/bin/mpv"), "uv": Path("/usr/local/bin/uv")},
    )


def invoke(
    root: Path,
    source: Path,
    command: str,
    *arguments: str,
    capsys: pytest.CaptureFixture[str],
    as_json: bool = True,
) -> tuple[int, dict[str, Any]]:
    """One staged invocation and its JSON report."""

    argv = [command, "--root", str(root), "--source", str(source), *arguments, "--no-service"]
    if as_json:
        argv.append("--json")
    code = main(argv, machine=pi())
    output = capsys.readouterr().out
    start = output.find("{")
    return code, (json.loads(output[start:]) if start >= 0 else {"_text": output})


def install(root: Path, source: Path, *, capsys: pytest.CaptureFixture[str]) -> int:
    """A first install, staged, without systemd. Returns the exit code."""

    code, payload = invoke(root, source, "install", capsys=capsys)
    assert code == EXIT_OK, payload
    return code


def application(root: Path, relative: str = "") -> Path:
    """`/opt/encore/application` under a staging root, or a path inside it."""

    return root / "opt/encore/application" / relative


def planned(payload: dict[str, Any], kind: str = "InstallApplication") -> bool:
    return any(step["kind"] == kind for step in payload["plan"])


def check(payload: dict[str, Any], name: str) -> dict[str, Any]:
    return next(entry for entry in payload["checks"] if entry["name"] == name)


# -- the bug --------------------------------------------------------------


def test_a_release_that_changed_only_python_is_still_an_upgrade(
    tmp_path: Path, source: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """#55 as reported: two merged PRs behind, and a plan of zero steps.

    The assertion is on the bytes in `application/`, not on the action list, because an action
    planned and not carried out is the same failure by another route — the issue's symptom was a
    machine that reported itself current and went on running code nobody had deployed.
    """

    root = tmp_path / "appliance"
    assert install(root, source, capsys=capsys) == EXIT_OK
    installed = application(root, "encore/playback.py")
    assert installed.read_text() == "class PlaybackService: ...\n"

    # What a merged bug fix looks like: source files, and no version, lockfile or manifest.
    source.joinpath("encore/playback.py").write_text(
        "class PlaybackService:\n    def recover(self) -> None: ...\n", encoding="utf-8"
    )

    code, payload = invoke(root, source, "upgrade", capsys=capsys)
    assert code == EXIT_OK, payload
    assert planned(payload), payload
    assert "def recover" in installed.read_text()


def test_a_file_the_last_release_added_is_a_step_too(
    tmp_path: Path, source: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The half a manifest comparison cannot see at any level of cleverness.

    `encore/playback/fault.py` is Phase 5's, in a project whose `pyproject.toml` did not change
    when it landed. A new file is not a difference between two equal-length strings; it is a name
    that exists on one side, which is why `Diff` counts `missing` separately and why the
    comparison is of sets rather than of documents.
    """

    root = tmp_path / "appliance"
    assert install(root, source, capsys=capsys) == EXIT_OK

    (source / "encore" / "fault.py").write_text("class Supervisor: ...\n", encoding="utf-8")
    code, payload = invoke(root, source, "upgrade", capsys=capsys)
    assert code == EXIT_OK, payload
    assert application(root, "encore/fault.py").exists()
    assert "1 missing" in json.dumps(payload["plan"])


def test_an_unchanged_appliance_plans_no_copy(
    tmp_path: Path, source: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The opposite failure, which a content comparison invites: copying every time.

    #55's fix must not turn `upgrade` into `rsync`. An idempotence test is the only thing that
    keeps "we now look at the files" from meaning "we always reinstall", and on a Pi the copy is
    the cheap half of the step it belongs to.
    """

    root = tmp_path / "appliance"
    assert install(root, source, capsys=capsys) == EXIT_OK
    _, payload = invoke(root, source, "upgrade", capsys=capsys)
    assert not planned(payload), payload
    assert check(payload, "application")["verdict"] == "ok"

    # The sentence the issue asked to keep: an unchanged appliance is 13.7's idempotence, and a
    # result rather than an evasion.
    code, text = invoke(root, source, "upgrade", capsys=capsys, as_json=False)
    assert code == EXIT_OK, text
    assert "nothing to do — the machine already matches this request" in text["_text"]


# -- what the report says -------------------------------------------------


def test_verify_fails_a_tree_it_cannot_match_and_upgrade_only_warns_about_one_it_can(
    tmp_path: Path, source: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """#43's rule applied to a new observation rather than a new rule.

    `verify` has no repair in its future, so drift is a FAIL and exit code 4 — the case where
    "everything is fine" would be a lie an operator acts on. `install` and `upgrade` are the
    commands that copy a tree, so the identical state is a WARN: a fact about what the run is
    doing, not a fault to file.
    """

    root = tmp_path / "appliance"
    assert install(root, source, capsys=capsys) == EXIT_OK
    source.joinpath("encore/__init__.py").write_text('__version__ = "0.6.0"\n', encoding="utf-8")

    code, payload = invoke(root, source, "verify", capsys=capsys)
    assert code == EXIT_VERIFICATION
    entry = check(payload, "application")
    assert entry["verdict"] == "fail"
    assert "1 differ" in entry["detail"]
    assert "encore/__init__.py" in entry["detail"]
    assert entry["fix"] == "sudo encore-install upgrade"

    # The repair, and the state afterwards: an upgrade's own report is assembled after the copy,
    # so it ends at OK. That is the honest sequence, not a skipped check.
    code, payload = invoke(root, source, "upgrade", capsys=capsys)
    assert code == EXIT_OK, payload
    assert planned(payload)
    assert check(payload, "application")["verdict"] == "ok"
    code, payload = invoke(root, source, "verify", capsys=capsys)
    assert code == EXIT_OK, payload


def test_a_dry_run_warns_about_the_tree_it_is_about_to_leave_behind(
    tmp_path: Path, source: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Where the WARN actually appears, and the one place it must not claim to have copied.

    A rehearsal is how #55 is diagnosed before anything is written, so the line has to name the
    files and then say, in the same breath, that it did nothing about them.
    """

    root = tmp_path / "appliance"
    assert install(root, source, capsys=capsys) == EXIT_OK
    (source / "encore" / "fault.py").write_text("class Supervisor: ...\n", encoding="utf-8")

    code, payload = invoke(root, source, "upgrade", "--dry-run", capsys=capsys)
    assert code == EXIT_OK, payload
    entry = check(payload, "application")
    assert entry["verdict"] == "warn", entry
    assert "1 missing of 5 files: encore/fault.py" in entry["detail"]
    assert entry["fix"].endswith("a rehearsal; the tree was not copied")
    assert not application(root, "encore/fault.py").exists()
    assert planned(payload)


def test_the_plan_says_what_it_found_rather_than_that_it_found_something(
    tmp_path: Path, source: Path
) -> None:
    """Never an adjective: the count and the names belong in the step, not only in the checks.

    `--dry-run` is how an operator asks what a command would do before typing a password, and
    "install the application" is the same sentence for a first install and for a machine one file
    out of 400 away from this checkout.
    """

    layout = Layout(root=tmp_path / "appliance")
    host = pi()
    state = observe(
        host,
        layout=layout,
        bind_host="127.0.0.1",
        bind_port=8080,
        music_dir=layout.staged("/opt/music"),
        source=source,
    )
    assert not state.application_matches
    assert state.application_drift.missing == 4
    assert not state.application_drift.changed
    options = Options(
        command=Command.UPGRADE,
        layout=layout,
        source=source,
        settings=Settings(music_dir=layout.staged("/opt/music")),
        service=False,
    )
    plan = install_plan(options, state, EncoreConfig())
    step = next(action for action in plan.actions if isinstance(action, InstallApplication))
    assert layout.application in step.detail
    assert "4 missing of 4 files" in step.detail
    assert "encore/playback.py" in step.detail


def test_a_tree_that_was_not_read_is_not_called_a_match(tmp_path: Path, source: Path) -> None:
    """`compared` is what separates "identical" from "I was not able to look".

    An empty destination and an unreadable source produce the same diff — nothing differs — and
    only one of them means the appliance is current. This is the failure #55 would have been
    *after* a naive fix: a plan that never copies because it never found anything to compare.
    """

    layout = Layout(root=tmp_path / "appliance")

    def asked(for_source: Path) -> ApplianceState:
        return observe(
            pi(),
            layout=layout,
            bind_host="127.0.0.1",
            bind_port=8080,
            music_dir=layout.staged("/opt/music"),
            source=for_source,
        )

    elsewhere = asked(source)
    assert elsewhere.application_drift.compared == len(manifest(source))
    assert not elsewhere.application_matches
    nowhere = asked(tmp_path / "not-a-checkout").application_drift.summary()
    assert nowhere == "nothing installed to compare"


# -- the exclusions, through a real copy ----------------------------------


def test_a_development_database_does_not_travel_with_the_code(
    tmp_path: Path, source: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The list has to be the one the copier and the observer share, or #55 comes back sideways.

    A file the copy excludes and the comparison counts is a machine that is never current: every
    upgrade plans a copy forever. That is the shape the duplication had — one exclusion list for
    `rsync`, another for `shutil`, and a drift bug in the space between them.
    """

    root = tmp_path / "appliance"
    for relative, text in (
        ("library.db", "not the runtime database"),
        (".git/config", "[core]"),
        ("encore/__pycache__/playback.cpython-312.pyc", "stale bytecode"),
        ("build/lib/encore.py", "a wheel in progress"),
    ):
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    assert install(root, source, capsys=capsys) == EXIT_OK
    for relative in ("library.db", ".git", "encore/__pycache__", "build"):
        assert not application(root, relative).exists(), relative

    _, payload = invoke(root, source, "upgrade", capsys=capsys)
    assert not planned(payload), payload


def test_a_file_removed_upstream_does_not_stay_importable(
    tmp_path: Path, source: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--delete` is still there, and a content comparison now says *when* to use it.

    #55's fix could have been "always copy", which would have kept this behaviour by accident.
    This test is the reason it is kept on purpose: a module that no longer exists upstream but
    still sits in `application/` is importable, and that is how a half-upgraded appliance ends up
    with a traceback naming a file nobody can find in the repository.
    """

    root = tmp_path / "appliance"
    assert install(root, source, capsys=capsys) == EXIT_OK
    (source / "encore" / "playback.py").unlink()

    code, payload = invoke(root, source, "upgrade", capsys=capsys)
    assert code == EXIT_OK, payload
    assert not application(root, "encore/playback.py").exists()
