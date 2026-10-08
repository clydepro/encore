"""Regression: #15 - the release gate grepped for a CHANGELOG shape nothing used.

`Release / verify` ends with a grep for a bracketed heading, `## [X.Y.Z]`; the
repository's only release heading was `## 0.0.0 - 2026-10-07`, unbracketed. The
first tag would have spent five minutes on the slow suite and then reported that
CHANGELOG.md does not mention the version — while it did, in a form the gate
cannot see. Nothing documented the required shape either.

Both halves are pinned here: the file's headings, and the workflow's grep. The
defect returns if either one changes alone.
"""

from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CHANGELOG_FILE = PROJECT_ROOT / "CHANGELOG.md"
RELEASE_WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "release.yml"

#: Keep a Changelog release heading, which is the shape `release.yml` looks for.
#: A `## 1.0.0 - 2026-01-01` heading passes every lint rule and fails a release.
RELEASE_HEADING = re.compile(r"^## \[(\d+\.\d+\.\d+[0-9A-Za-z.+-]*)\] - \d{4}-\d{2}-\d{2}$")


def _release_headings() -> list[str]:
    changelog = CHANGELOG_FILE.read_text(encoding="utf-8")
    return [
        line
        for line in changelog.splitlines()
        if line.startswith("## ") and not line.startswith("## Unreleased")
    ]


def test_changelog_release_headings_have_the_expected_shape() -> None:
    headings = _release_headings()
    assert headings, "CHANGELOG.md should record at least the bootstrap release"
    for heading in headings:
        assert RELEASE_HEADING.match(heading), (
            f"{heading!r} must read '## [X.Y.Z] - YYYY-MM-DD'; "
            "see docs/Developer/Release-Process.md"
        )


def test_release_gate_still_expects_that_shape() -> None:
    """If the workflow stops grepping, the assertions above stop protecting it."""
    gate = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    assert '"^## \\[' in gate, (
        "Release / verify no longer greps for a bracketed heading; update "
        "RELEASE_HEADING and Release-Process.md together, not one alone"
    )
