"""The configuration file: what to write, and the one file that must not be overwritten.

SAPRS 13.8 is a short sentence with a long tail — "installation and upgrade must preserve
configuration" — because the alternative is the failure that ends a party: an operator who
set `audio.device` to the USB DAC in March has it reset to `default` by an upgrade in June,
with the only evidence being a journal line nobody reads until the room is silent.

So the decision is made here, once, as data:

* **no file** → render one from a validated `EncoreConfig`, with the comments of
  `examples/config.yaml`;
* **a file that validates** → keep it, byte for byte, and report the settings the installer
  *would* have changed, because the operator asked for `--domain` and deserves to be told it
  was not applied;
* **a file that does not validate** → keep it and fail. Repairing somebody's YAML by
  overwriting it is the exact thing 13.8 forbids, and a rendered replacement would be an
  appliance configured for a machine that is not theirs.

`configuration_problems` validates text with the Server's own models rather than with a
second schema. That is the whole reason the installer is allowed to import `encore.config`:
two definitions of what a valid `config.yaml` is would disagree, and the disagreement would be
found at boot rather than at install.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from pydantic import ValidationError

from encore.config.models import EncoreConfig
from encore.config.service import ConfigurationError, ConfigurationService

if TYPE_CHECKING:
    from apps.installer.layout import Layout

__all__ = [
    "ConfigDecision",
    "Decision",
    "configuration_problems",
    "decide",
    "load_validated",
    "merge_existing",
]


class Decision:
    """What `decide` concluded about the file on disk."""

    WRITE = "write"
    PRESERVE = "preserve"
    UNCHANGED = "unchanged"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True, kw_only=True)
class ConfigDecision:
    """The configuration step's answer, as data.

    Attributes:
        action: One of `Decision`'s three values, deciding which action `plan.py` emits.
        text: The bytes to write, when the action is `WRITE`.
        path: Where they would go, staged. Included because the report names a file rather
            than a decision, and a decision without a path is not actionable.
        settings: What would have changed, as `(dotted.name, on_disk, asked_for)` triples.
            Empty is the normal, boring case; non-empty with `action=PRESERVE` is the
            operator's `--domain` flag meeting a file they edited last spring.
        problems: Why a file on disk is unusable, in `ConfigurationError`'s words.
    """

    action: str
    path: Path
    text: str = ""
    settings: tuple[tuple[str, Any, Any], ...] = ()
    problems: tuple[str, ...] = field(default=())

    @property
    def changes_anything(self) -> bool:
        return self.action == Decision.WRITE


def decide(
    existing_text: str | None, layout: Layout, *, rendered: str, asked: str | None = None
) -> ConfigDecision:
    """Compare what is on disk with what was asked for, and choose the lesser act.

    Args:
        existing_text: The current file's contents, or None if there is no file.
        layout: For the staging root, and for the paths a generated file must name.
        rendered: The text that *would* be written, from `content.config_text`, which is
            `EncoreConfig` plus the layout rendered as YAML. Passed in rather than built here
            so this module's only job is the comparison, and the question of what the file says
            stays in one place.
        asked: The rendering of what the *flags* asked for, when that is not the same document.
            The difference is 13.8's report: an operator who types `--port 8099` against a file
            that says 9100 is told their file won and named the setting that disagreed. Left out
            when there is nothing to compare against — a first install, or a `verify`.

    `rendered` rather than the model, and the reason is staging: an `EncoreConfig` built from
    `--music-dir` names `/var/lib/encore/library.db`, while the file a `--root` rehearsal writes
    names `<root>/var/lib/encore/library.db`. Compared to the model, every rehearsal would report
    a hundred path differences and preserve a file that agrees with itself.

    """

    path = layout.staged(layout.config_file)
    if existing_text is None:
        return ConfigDecision(action=Decision.WRITE, path=path, text=rendered)

    problems = configuration_problems(existing_text)
    if problems:
        return ConfigDecision(
            action=Decision.INVALID, path=path, text=rendered, problems=tuple(problems)
        )

    on_disk = _flatten(_validated(existing_text).model_dump())
    asked_for = _flatten(_validated(asked if asked is not None else rendered).model_dump())
    differences = tuple(
        (name, on_disk[name], asked_for[name])
        for name in sorted(asked_for)
        if name in on_disk and on_disk[name] != asked_for[name]
    )
    # No differences means nothing to report and nothing to write — including when the bytes
    # differ, because a comment somebody added is not a setting and an installer that emitted an
    # action for it would make a second run look like a first one. The comparison is therefore of
    # parsed values, section by section, which is also what makes an operator's reformatting of
    # the file invisible to the plan: what is *set* is what is compared.
    if not differences:
        return ConfigDecision(action=Decision.UNCHANGED, path=path, text=rendered)
    return ConfigDecision(action=Decision.PRESERVE, path=path, text=rendered, settings=differences)


def load_validated(path: Path) -> EncoreConfig:
    """Read a configuration file the way the Server would, and hand back the result.

    `ConfigurationService` rather than a reimplementation, so the answer an installer gives
    about a file is the answer the appliance will give about the same file. The exceptions are
    translated into `ConfigurationError`'s message text by the caller, which reports all
    problems at once (SAPRS 12.5) instead of one per traceback.
    """

    return ConfigurationService(path).load()


def configuration_problems(text: str) -> list[str]:
    """Why this text is not a usable configuration, or nothing.

    Deliberately shaped like `ConfigurationService`'s own path — YAML first, then a mapping,
    then the models — because an operator who is told a file is invalid by two different code
    paths that disagree is an operator who stops believing either.
    """

    try:
        raw: Any = yaml.safe_load(text)
    except yaml.YAMLError as error:
        return [f"not valid YAML: {error}"]
    if raw is None:
        return []
    if not isinstance(raw, dict):
        return [f"the document is a {type(raw).__name__}, not a mapping of sections"]
    sections = {key: ({} if value is None else value) for key, value in raw.items()}
    try:
        EncoreConfig.model_validate(sections)
    except ValidationError as error:
        return [
            f"{'.'.join(str(part) for part in problem['loc']) or '<root>'}: {problem['msg']}"
            for problem in error.errors()
        ]
    return []


def merge_existing(existing_text: str, wanted: EncoreConfig) -> EncoreConfig:
    """Apply the installer's configuration to a file that is already there.

    Used when `--config` names a file the installer did not write, which is the only path by
    which such a file is read at all. The merge is one-directional: whatever the file says stands,
    and the installer's values fill the sections the file never mentioned. A flag is a narrower
    statement than a file somebody edited on purpose, so the file wins on every key it names —
    which is also why the report lists what was *not* applied rather than pretending otherwise.

    The distinction from `_validated` matters. Validating the text alone would hand back an
    `EncoreConfig` full of Python defaults, and a generated file would then omit a setting whose
    default has since changed; merging against the configuration in force keeps the written file
    the authority and the installer the author of everything else.

    Raises:
        ConfigurationError: The existing text is not valid, in which case there is nothing to
            merge and 12.5's rule applies — say everything, change nothing.
    """

    problems = configuration_problems(existing_text)
    if problems:
        raise ConfigurationError("; ".join(problems))
    written: dict[str, object] = dict(yaml.safe_load(existing_text) or {})
    merged = wanted.model_dump()
    for section, values in written.items():
        if isinstance(values, dict) and isinstance(merged.get(section), dict):
            merged[section].update(values)
        else:
            merged[section] = values
    return EncoreConfig.model_validate(merged)


def _validated(text: str) -> EncoreConfig:
    raw = yaml.safe_load(text) or {}
    if not isinstance(raw, dict):
        return EncoreConfig()
    return EncoreConfig.model_validate({key: ({} if v is None else v) for key, v in raw.items()})


def _flatten(sections: dict[str, Any]) -> dict[str, Any]:
    """`{section: {key: value}}` to `{section.key: value}`, for a diff a human can read.

    Two levels only, because `EncoreConfig` is two levels deep by construction. A nested
    value would appear as the dict it is, which is enough to show that it differs.
    """

    return {
        f"{section}.{key}": value
        for section, body in sections.items()
        if isinstance(body, dict)
        for key, value in body.items()
    }
