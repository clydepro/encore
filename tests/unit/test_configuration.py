"""ConfigurationService: a validated document, not a pile of lookups (SAPRS Ch.12)."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from encore.config import ConfigurationError, ConfigurationService, EncoreConfig
from encore.config.models import QueueConfig

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_CONFIG = REPO_ROOT / "examples" / "config.yaml"


def written(tmp_path: Path, document: dict[str, Any], name: str = "config.yaml") -> Path:
    path = tmp_path / name
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return path


def document(**overrides: dict[str, Any] | None) -> dict[str, Any]:
    """A complete, valid config with named sections replaced or removed."""

    base: dict[str, Any] = yaml.safe_load(EXAMPLE_CONFIG.read_text(encoding="utf-8"))
    for key, value in overrides.items():
        if value is None:
            base.pop(key, None)
        else:
            base[key] = value
    return base


# -- the example is the contract ------------------------------------------


def test_the_shipped_example_loads() -> None:
    """`examples/config.yaml` is documentation an installer copies. It must work."""

    config = ConfigurationService(path=EXAMPLE_CONFIG).load()

    assert config.paths.library_db == Path("/var/lib/encore/library.db")
    assert config.queue.max_items == 200
    assert config.logging.level == "INFO"
    assert config.admin.session_minutes == 60


def test_the_example_states_only_what_the_defaults_already_are() -> None:
    """Two sources of truth, one answer.

    `examples/config.yaml` is what an installer copies and what documentation
    quotes; if it ever says something other than the model defaults, an operator
    following the docs and an operator running the example get different
    appliances. The comparison is on the dump, so a renamed field fails too.
    """

    stated = EncoreConfig(**yaml.safe_load(EXAMPLE_CONFIG.read_text(encoding="utf-8")))
    assert stated.model_dump() == EncoreConfig().model_dump()


def test_an_empty_file_is_the_defaults(tmp_path: Path) -> None:
    assert ConfigurationService(path=written(tmp_path, {})).load() == EncoreConfig()


def test_a_missing_file_is_a_startable_error(tmp_path: Path) -> None:
    path = tmp_path / "nowhere.yaml"
    service = ConfigurationService(path=path)

    with pytest.raises(ConfigurationError) as raised:
        service.load()

    assert raised.value.missing_file
    assert raised.value.missing_file.resolve() == path.resolve()
    assert str(path) in str(raised.value), "the message must name the file it looked for"


def test_a_directory_is_not_a_config_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="not a file"):
        ConfigurationService(path=tmp_path).load()


@pytest.mark.parametrize(
    "text",
    ["server:\n  port: 80\n80", "---\n{[", "key: value\n key: nested", "\tab: 1"],
)
def test_unparseable_yaml_is_a_configuration_error_not_a_traceback(
    tmp_path: Path, text: str
) -> None:
    """SAPRS 12.5 requires actionable output; a `yaml.scanner` traceback is not."""

    path = tmp_path / "broken.yaml"
    path.write_text(text, encoding="utf-8")

    with pytest.raises(ConfigurationError, match="not valid YAML"):
        ConfigurationService(path=path).load()


def test_a_yaml_document_that_is_not_a_mapping_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "list.yaml"
    path.write_text("- one\n- two\n", encoding="utf-8")

    with pytest.raises(ConfigurationError, match="mapping"):
        ConfigurationService(path=path).load()


@pytest.mark.skipif(sys.platform == "linux" and os.geteuid() == 0, reason="root ignores modes")
def test_an_unreadable_file_says_which_one(tmp_path: Path) -> None:
    """SAPRS 13.6 runs Encore as a dedicated service user, so a wrong mode on the
    installed file is a real failure and not a developer's mistake."""

    path = written(tmp_path, {})
    path.chmod(0o000)
    try:
        with pytest.raises(ConfigurationError, match="cannot be read") as raised:
            ConfigurationService(path=path).load()
        assert str(path) in str(raised.value)
    finally:
        path.chmod(0o644)


# -- validation (SAPRS 12.4) --------------------------------------------


def test_unknown_keys_fail_rather_than_go_ignore_wanted(tmp_path: Path) -> None:
    """12.4 lists unknown keys as a startup failure; `extra="forbid"` is the only
    way to catch it."""

    with pytest.raises(ConfigurationError, match="crossfade_second"):
        ConfigurationService(
            path=written(
                tmp_path, document(audio={"crossfade_seconds": 2.0, "crossfade_second": 1})
            )
        ).load()


def test_wrong_types_are_named_with_their_path(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError) as raised:
        ConfigurationService(path=written(tmp_path, document(server={"port": "eighty"}))).load()

    assert raised.value.problems[0].location == "server.port"
    assert "port" in str(raised.value)


def test_a_bad_enum_value_lists_what_is_allowed(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match=r"json|console"):
        ConfigurationService(path=written(tmp_path, document(logging={"format": "yam"}))).load()


def test_the_defaults_are_the_appliance_described_in_chapter_13() -> None:
    """A minimal file must produce the same appliance as the documented defaults."""

    config = EncoreConfig()

    assert config.server.port == 8080
    assert config.audio.gapless is True
    assert config.audio.crossfade_seconds == 0.0, "7.8: crossfade is off unless enabled"
    assert config.playback.restart_backoff_seconds == [1.0, 2.0, 5.0, 10.0, 30.0]
    assert config.queue.allow_duplicates is True


def test_relative_paths_are_refused_because_systemd_sets_the_cwd(tmp_path: Path) -> None:
    """SAPRS 13.6 runs the service under systemd, whose working directory an
    operator does not choose; a relative path resolves somewhere surprising."""

    with pytest.raises(ConfigurationError, match="absolute"):
        ConfigurationService(
            path=written(tmp_path, document(paths={"library_db": "library.db"}))
        ).load()


def test_runtime_and_library_databases_must_be_different_files(tmp_path: Path) -> None:
    """SAPRS 12.6 states the reason: sharing one file breaks immutability."""

    same = written(
        tmp_path,
        document(
            paths={
                "library_db": "/var/lib/encore/one.db",
                "runtime_db": "/var/lib/encore/one.db",
            }
        ),
    )

    with pytest.raises(ConfigurationError, match="must be different files"):
        ConfigurationService(path=same).load()


def test_the_two_paths_may_be_written_to_because_they_are_different() -> None:
    config = EncoreConfig()
    assert config.paths.library_db != config.paths.runtime_db


def test_a_blank_bind_address_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="host"):
        ConfigurationService(path=written(tmp_path, document(server={"host": "   "}))).load()


def test_the_mdns_name_is_stripped_rather_than_rejected(tmp_path: Path) -> None:
    """A trailing space in `hostname:` is a typo, not a decision (SAPRS 1.4)."""

    config = ConfigurationService(
        path=written(tmp_path, document(server={"hostname": " encore-room "}))
    ).load()

    assert config.server.hostname == "encore-room"


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("server", "port", 0),
        ("server", "port", 65_536),
        ("audio", "volume", -1),
        ("audio", "volume", 101),
        ("audio", "crossfade_seconds", -0.5),
        ("audio", "crossfade_seconds", 12.5),
        ("queue", "max_items", 0),
        ("playback", "progress_interval_seconds", 0),
        ("playback", "restart_backoff_seconds", []),
        ("search", "page_size", 0),
        ("admin", "session_minutes", 0),
    ],
)
def test_bounds_are_enforced_where_the_spec_names_a_number(
    tmp_path: Path, section: str, key: str, value: Any
) -> None:
    """SAPRS 12.2 says the file contains these settings; AIG 19 and Chapter 8 say
    what they may be. A value outside them must stop startup (12.5)."""

    section_values: dict[str, Any] = dict(getattr(EncoreConfig(), section).model_dump(mode="json"))
    section_values[key] = value

    with pytest.raises(ConfigurationError, match=key):
        ConfigurationService(path=written(tmp_path, document(**{section: section_values}))).load()


# -- the rule the queue must not lose (SAPRS 8.3) ------------------------


def test_duplicates_may_not_be_turned_off_by_configuration() -> None:
    """8.3 states duplicates may be queued - by different guests or the same one.

    The wording is about behaviour, not about who asked, so a build that removed
    repeat requests would not be Encore whatever its file said. This is SAPRS
    12.3's "configuration is not managed state" applied to a rule that was never
    a preference: of everything in the file, this one key cannot be configured.
    """

    with pytest.raises(ValidationError, match=re.escape("SAPRS 8.3")):
        QueueConfig(allow_duplicates=False)


def test_copying_a_section_does_not_bypass_the_rule() -> None:
    """`model_copy` skips validation by design, so an edited copy could carry a
    disabled-duplicates queue into an otherwise valid document.

    `EncoreConfig` revalidates instances it is given, which is the difference
    between a rule and a convention.
    """

    copied = QueueConfig().model_copy(update={"allow_duplicates": False})

    with pytest.raises(ValidationError, match=re.escape("SAPRS 8.3")):
        EncoreConfig(queue=copied)


def test_the_rule_still_holds_after_a_load(tmp_path: Path) -> None:
    loaded = ConfigurationService(
        path=written(tmp_path, document(queue={"allow_duplicates": False}))
    )

    with pytest.raises(ConfigurationError, match=re.escape("SAPRS 8.3")):
        loaded.load()


# -- access, reload, summary -------------------------------------------


def test_settings_are_read_only_after_loading() -> None:
    """12.4: configuration is read-only at runtime. Runtime state belongs in
    runtime.db, so there is nowhere for a write to be correct."""

    config = ConfigurationService(path=EXAMPLE_CONFIG).load()

    for section in EncoreConfig.model_fields:
        with pytest.raises(ValidationError):
            setattr(config, section, getattr(config, section))


def test_loading_twice_reads_the_file_again(tmp_path: Path) -> None:
    """`config` caches so every reader sees one consistent document (12.4);
    `load()` is the explicit re-read, and it must not merge the old one in."""

    path = written(tmp_path, document(audio={"volume": 70}))
    service = ConfigurationService(path=path)
    assert service.load().audio.volume == 70
    assert service.config.audio.volume == 70

    path.write_text(yaml.safe_dump(document()), encoding="utf-8")
    assert service.load().audio.volume == 85, "a re-read that kept stale keys is half-applied"
    assert service.config.audio.volume == 85


def test_a_failed_load_leaves_the_running_settings_alone(tmp_path: Path) -> None:
    """SAPRS 12.5 makes startup the moment of truth. If a re-read is attempted after
    the file has been broken, the process must keep the configuration it already
    validated rather than run with nothing."""

    path = written(tmp_path, document())
    service = ConfigurationService(path=path)
    service.load()

    path.write_text("server: nope\n", encoding="utf-8")
    with pytest.raises(ConfigurationError):
        service.load()

    assert service.current is not None
    assert service.current.server.port == 8080


def test_a_service_can_be_asked_whether_it_has_loaded(tmp_path: Path) -> None:
    service = ConfigurationService(path=written(tmp_path, {}))

    assert service.current is None
    loaded = service.load()
    assert service.current is loaded


def test_no_service_reads_the_environment() -> None:
    """12.4 lists exactly one input: the YAML file."""

    source = (REPO_ROOT / "encore" / "config" / "service.py").read_text(encoding="utf-8")
    assert "environ" not in source
    assert "getenv" not in source


def test_the_summary_describes_the_appliance_without_secrets() -> None:
    """SAPRS 12.3 allows the admin interface to display a read-only summary, and
    12.6 keeps secrets out of configuration entirely - so there is nothing here to
    redact, which is the point of asserting it rather than filtering it."""

    summary = ConfigurationService(path=EXAMPLE_CONFIG).summary()

    assert summary["server"]["port"] == 8080
    assert summary["search"]["page_size"] == 50
    assert summary["paths"]["artwork_dir"] == "/var/lib/encore/artwork"
    assert summary["audio"]["gapless"] is True
    assert summary["queue"]["allow_duplicates"] is True
    rendered = json.dumps(summary).lower()
    assert "password" not in rendered, "12.6 keeps secrets out of this file"
    assert "secret" not in rendered, "a field named here would mean the model grew one"
    assert json.dumps(summary, sort_keys=True), "the admin screen renders it as JSON"


def test_the_summary_reports_both_databases_as_distinct_files() -> None:
    """12.6 makes the separation load-bearing, so the dashboard has to show it."""

    paths = ConfigurationService(path=EXAMPLE_CONFIG).summary()["paths"]

    assert paths["library_db"] == "/var/lib/encore/library.db"
    assert paths["runtime_db"] == "/var/lib/encore/runtime.db"
    assert paths["library_db"] != paths["runtime_db"]


def test_a_missing_file_is_reported_before_the_admin_screen_opens(tmp_path: Path) -> None:
    """The dashboard shows a configuration summary; the failure must surface
    rather than render an empty card."""

    service = ConfigurationService(path=tmp_path / "gone.yaml")

    with pytest.raises(ConfigurationError, match="not found"):
        service.summary()


# -- the paths through the loader that a happy test never takes ----------


def test_the_path_is_recorded_as_given(tmp_path: Path) -> None:
    service = ConfigurationService(path=tmp_path / "config.yaml")

    assert service.path == (tmp_path / "config.yaml").expanduser()
    assert ConfigurationService(config=EncoreConfig()).path is None


def test_a_file_of_only_comments_is_the_defaults(tmp_path: Path) -> None:
    """`safe_load` returns None for a commented-out file. Treating that as `{}` is
    what makes "copy examples/config.yaml and comment it all out" a valid install."""

    path = tmp_path / "config.yaml"
    path.write_text("# nothing to see here\n# encore will use its defaults\n", encoding="utf-8")

    assert ConfigurationService(path=path).load() == EncoreConfig()


def test_a_blank_value_in_a_section_is_not_a_setting(tmp_path: Path) -> None:
    """`audio:` with nothing under it parses to None; the section's defaults are
    the answer, not a crash in the model."""

    path = written(tmp_path, {})
    path.write_text("audio:\n", encoding="utf-8")

    assert ConfigurationService(path=path).load().audio.gapless is True


def test_a_non_numeric_backoff_is_reported_by_name(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="restart_backoff_seconds"):
        ConfigurationService(
            path=written(tmp_path, document(playback={"restart_backoff_seconds": ["one", 2]}))
        ).load()


def test_a_top_level_key_that_is_not_a_section_is_refused(tmp_path: Path) -> None:
    stray = document()
    stray["stray_key"] = "value"

    with pytest.raises(ConfigurationError, match="stray_key"):
        ConfigurationService(path=written(tmp_path, stray)).load()


def test_the_builder_and_runtime_paths_are_the_configured_ones(tmp_path: Path) -> None:
    """SAPRS 12.2 names the library, runtime and artwork locations as configuration;
    a service that reconstructed them from some other key would disagree with what
    the installer wrote (13.3)."""

    path = written(
        tmp_path,
        document(
            paths={
                "library_db": "/mnt/corpus/library.db",
                "runtime_db": "/var/lib/encore/runtime.db",
                "artwork_dir": "/mnt/corpus/art",
                "temp_dir": "/var/cache/encore/temp",
            }
        ),
    )
    config = ConfigurationService(path=path).load()

    assert config.paths.library_db == Path("/mnt/corpus/library.db")
    assert config.paths.artwork_dir == Path("/mnt/corpus/art")
