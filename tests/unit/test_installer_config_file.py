"""The four outcomes of meeting an existing `config.yaml` (SAPRS 13.8, 12.5).

`config_file.py` holds the installer's one genuinely hard rule — the operator's file is not
ours to overwrite — and the four branches are where that rule becomes testable. Written against
`decide()` rather than against `install_plan()` because the interesting cases (a valid file that
disagrees with the flags; an invalid file that must survive) are decisions about text, and a plan
test would only re-state them as actions.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest
import yaml

from apps.installer.config_file import (
    Decision,
    configuration_problems,
    decide,
    load_validated,
    merge_existing,
)
from apps.installer.content import config_text
from apps.installer.layout import Layout
from encore.config.models import EncoreConfig
from encore.config.service import ConfigurationError, ConfigurationService


def rendered(root: Path) -> str:
    return config_text(EncoreConfig(), Layout(root=root))


def names(differences: Sequence[tuple[str, object, object]]) -> set[str]:
    return {str(triple[0]) for triple in differences}


def test_a_missing_file_is_written(tmp_path: Path) -> None:
    """The one case where the installer may create `/etc/encore/config.yaml` (13.8)."""

    decision = decide(None, Layout(root=tmp_path), rendered=rendered(tmp_path))
    assert decision.action == Decision.WRITE
    assert decision.path == tmp_path / "etc/encore/config.yaml"
    assert decision.text == rendered(tmp_path)


def test_an_identical_file_produces_no_write(tmp_path: Path) -> None:
    decision = decide(rendered(tmp_path), Layout(root=tmp_path), rendered=rendered(tmp_path))
    assert decision.action == Decision.UNCHANGED
    assert not decision.changes_anything


def test_an_operators_comment_changes_nothing(tmp_path: Path) -> None:
    """A file differing only in comments is the same file, and plans no action.

    The comparison is of parsed settings rather than of bytes, which is the arrangement that keeps
    a second install run silent after an operator annotated their configuration last month. A byte
    comparison here would report "preserved" for a file that agrees, and an installer that emits
    actions for files it agrees with is an installer whose reports nobody can read.
    """

    text = rendered(tmp_path)
    annotated = "\n".join(["# added by me, while debugging the volume", *text.splitlines()])
    decision = decide(annotated, Layout(root=tmp_path), rendered=text)
    assert decision.action == Decision.UNCHANGED
    assert not decision.settings


def test_a_different_file_is_kept_and_the_differences_are_named(tmp_path: Path) -> None:
    """13.8's whole clause, and the reason "preserved" has to come with evidence."""

    text = rendered(tmp_path).replace("page_size: 50", "page_size: 25")
    decision = decide(text, Layout(root=tmp_path), rendered=rendered(tmp_path))
    assert decision.action == Decision.PRESERVE
    assert not decision.changes_anything
    assert ("search.page_size", 25, 50) in decision.settings


def test_a_file_with_an_unknown_key_is_invalid_rather_than_stripped(tmp_path: Path) -> None:
    """An unknown setting is a typo, and silent acceptance is how 12.5's rule dies."""

    text = rendered(tmp_path) + "\nqueue:\n  max_length: 4\n"
    decision = decide(text, Layout(root=tmp_path), rendered=rendered(tmp_path))
    assert decision.action == Decision.INVALID
    assert any("queue" in problem for problem in decision.problems)


def test_the_invalid_problems_are_the_ones_the_server_would_report(tmp_path: Path) -> None:
    """One definition of validity, in the service that loads it at boot.

    The installer's `configuration_problems` and the Server's `ConfigurationService` have to
    agree, or a machine can pass an install and fail a start. Asserted against the same bad file
    through both doors rather than against strings, because the strings are allowed to differ in
    punctuation and the *count* of problems is not.
    """

    text = rendered(tmp_path).replace("volume: 85", "volume: 200")
    problems = configuration_problems(text)
    assert problems, "an out-of-range volume should not validate"
    path = tmp_path / "etc" / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    with pytest.raises(ConfigurationError) as raised:
        ConfigurationService(path).load()
    assert {problem.location for problem in raised.value.problems} == {
        line.split(":", 1)[0] for line in problems
    }


def test_problems_are_empty_for_a_valid_file(tmp_path: Path) -> None:
    assert configuration_problems(rendered(tmp_path)) == []


def test_malformed_yaml_is_a_problem_rather_than_a_crash() -> None:
    problems = configuration_problems("server:\n\tport: 8080\n")
    assert problems
    assert "YAML" in problems[0]


def test_an_empty_file_is_not_a_problem() -> None:
    """An empty configuration is "everything by default", which is a sentence, not a bug.

    Asserted because the temptation is to reject it, and the consequence of rejecting it is an
    appliance that will not start after a `truncate` — a worse outcome than the one prevented.
    """

    assert configuration_problems("") == []


def test_a_document_that_is_not_a_mapping_is_a_problem() -> None:
    assert configuration_problems("- just a list\n")


# -- adoption -------------------------------------------------------------------


def test_adopting_a_file_keeps_what_the_operator_wrote(tmp_path: Path) -> None:
    """`--config` adopts; it does not re-render (13.8's other half).

    The merge keeps on-disk values and takes only the sections the file does not mention, so a
    hand-written configuration that sets three keys still gets the other eleven from the model
    rather than from a second copy of the defaults inside this installer.
    """

    partial = "server:\n  port: 8099\npaths:\n  music_dir: /srv/music\n"
    merged = merge_existing(partial, EncoreConfig())
    assert merged.server.port == 8099
    assert merged.paths.music_dir == Path("/srv/music")
    assert merged.queue.max_items == EncoreConfig().queue.max_items


def test_adopting_a_file_does_not_invent_paths(tmp_path: Path) -> None:
    """A partial file with no `paths` section still has to point at the staged root."""

    merged = merge_existing("server:\n  port: 8099\n", EncoreConfig())
    assert merged.paths.library_db == EncoreConfig().paths.library_db


def test_load_validated_reports_the_same_errors_as_the_server(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(rendered(tmp_path))
    assert load_validated(path).server.port == 8080
    path.write_text("server:\n  port: 99999999\n")
    with pytest.raises(ConfigurationError):
        load_validated(path)


def test_load_validated_on_an_absent_file_says_what_it_missed(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError) as raised:
        load_validated(tmp_path / "nope.yaml")
    assert "nope.yaml" in str(raised.value)


# -- staging, again, because it bites ---------------------------------------------


def test_a_configuration_written_for_a_staged_root_stays_there(tmp_path: Path) -> None:
    """The nesting bug: `staged()` applied twice moves the appliance one level deeper.

    Reproduced here because the second install run is the case — the file on disk already names
    staged paths, and a generator that staged them again would relocate `library.db` out from
    under a running service.
    """

    layout = Layout(root=tmp_path)
    text = config_text(EncoreConfig(), layout)
    again = config_text(EncoreConfig.model_validate(yaml.safe_load(text)), layout)
    assert yaml.safe_load(text) == yaml.safe_load(again)
    assert str(tmp_path) * 2 not in text
