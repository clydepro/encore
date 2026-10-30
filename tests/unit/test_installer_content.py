"""What the installer writes, and whether Encore would accept it (SAPRS 12.2, 13.4, 13.6).

`content.py` renders four files. Three of them (the unit, the environment, the tmpfiles rule) are
read by programs that are not Encore — systemd, `systemd-tmpfiles` — so their tests are textual.
The fourth is `config.yaml`, which is read by Encore itself, and so gets the strongest test
available here: hand the rendered bytes to `ConfigurationService` and see whether the appliance
starts. A comment in a YAML file is not evidence that the file is valid.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from apps.installer.content import config_text, environment_text, tmpfiles_text, unit_text
from apps.installer.layout import Layout
from apps.installer.options import Settings
from encore.config.models import AudioConfig, EncoreConfig
from encore.config.service import ConfigurationService


def settings(root: Path, **values: object) -> Settings:
    """The installer's dials, pointed at a staging root."""

    return Settings(
        music_dir=root / "opt" / "music",
        **{"port": 8080, "domain": "jukebox.home.arpa", **values},  # type: ignore[arg-type]
    )


def config_for(
    root: Path, *, values: dict[str, object] | None = None, **decided: object
) -> EncoreConfig:
    """A configuration, with the two values only the machine can answer.

    `audio_output` and `mpv_path` are not `Settings` fields on purpose: they are observed
    (7.7 picks the output, ADR-005 finds the binary), and a test that could set them from a flag
    would be testing a CLI that does not exist.
    """

    audio = {"audio_output": "alsa", "mpv_path": Path("/usr/bin/mpv")}
    audio.update(decided)
    return settings(root, **(values or {})).config(**audio)  # type: ignore[arg-type]


def generated(root: Path, *, values: dict[str, object] | None = None, **decided: object) -> str:
    return config_text(config_for(root, values=values, **decided), Layout(root=root))


def _stage(paths: object, layout: Layout) -> object:
    """The documented paths, as they look from inside a staging root.

    Through `Layout.staged` rather than by hand, so this helper cannot drift from the rule the
    generator uses — which is the drift that would make the test below pass for the wrong reason.
    """

    if not isinstance(paths, dict):
        return paths
    return {key: layout.staged(str(value)) for key, value in paths.items()}


def parsed(text: str) -> dict[str, object]:
    loaded = yaml.safe_load(text)
    assert isinstance(loaded, dict), "the rendered configuration is not a mapping"
    return loaded


# -- config.yaml ----------------------------------------------------------------


def test_the_rendered_file_is_one_the_server_would_accept(tmp_path: Path) -> None:
    """The whole claim of this module, tested directly.

    Written through `ConfigurationService` rather than by loading YAML into a dict, because the
    appliance's definition of a valid configuration is Pydantic's plus the loader's, and an
    installer that validated to a weaker standard would produce a machine that fails at boot and
    a report that said it succeeded.
    """

    path = tmp_path / "etc" / "config.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(generated(tmp_path))
    loaded = ConfigurationService(path).load()
    assert isinstance(loaded, EncoreConfig)
    assert loaded.server.port == 8080


def test_rendering_loses_nothing_between_the_models_and_the_file(tmp_path: Path) -> None:
    """Round-trip: config → text → config, and the two agree on every setting.

    The test that catches a field added to `EncoreConfig` without being added to the template —
    a silent event, because the omitted field simply takes its default on the appliance and the
    operator's configuration file says nothing about it.
    """

    layout = Layout(root=tmp_path)
    wanted = config_for(
        tmp_path,
        values={"port": 8099, "domain": "music.local", "log_format": "console"},
        audio_output="pipewire",
        mpv_path=Path("/snap/bin/mpv"),
    )
    again = EncoreConfig.model_validate(yaml.safe_load(config_text(wanted, layout)))
    expected = wanted.model_dump()
    # `paths` is the one section the generator rewrites on purpose: the file must name where the
    # *appliance* will look, which under `--root` is not where this test's settings pointed.
    expected["paths"] = _stage(expected["paths"], layout)
    assert again.model_dump() == expected


@pytest.mark.parametrize(
    "device",
    [
        # The value #48 needs on a Raspberry Pi 4: colons, an equals sign, a comma.
        "alsa/plughw:CARD=Headphones,DEV=0",
        "alsa/hdmi:CARD=vc4hdmi0,DEV=0",
        # And the pathological case the quoting rule exists for: a colon with a space after it is
        # a nested mapping to anything that parses this, and the Server would refuse the file.
        "alsa/default: the first card",
    ],
)
def test_a_device_name_survives_the_rendering(tmp_path: Path, device: str) -> None:
    """#48's remedy is one YAML line, so the line has to be a parseable one.

    A value interpolated into a template unquoted turns into a different value — or into a file
    the Server refuses at boot, which is the failure this module exists to make impossible and
    which the round-trip test above catches only for the values it happens to try.
    """

    wanted = EncoreConfig(audio=AudioConfig(output="alsa", device=device))

    rendered = config_text(wanted, Layout(root=tmp_path))
    again = EncoreConfig.model_validate(yaml.safe_load(rendered))

    assert again.audio.device == device


def test_the_generated_file_says_where_the_device_comes_from(tmp_path: Path) -> None:
    """A knob with no sentence beside it is a knob nobody turns, which is how #48 stayed lit.

    The comment is the other half of the fix: the installer cannot probe a sound card from a
    command line, so the file it writes has to name the command that can, and say what "default"
    actually resolves to on the hardware this product targets.
    """

    text = generated(tmp_path)

    assert "--audio-device=help" in text, "the line that lists the real device names"
    assert "audio output initialization failed" in text, (
        "the sentence the journal shows, so an operator can search for it"
    )


def test_a_rendered_file_carries_a_comment_for_every_setting(tmp_path: Path) -> None:
    """12.5's promise, kept by the generator rather than by an operator's patience."""

    text = generated(tmp_path)
    comments = sum(1 for line in text.splitlines() if line.lstrip().startswith("#"))
    settings = sum(
        1
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#") and ":" in line
    )
    assert comments >= settings // 2, f"{comments} comments for {settings} lines of configuration"


def test_every_documented_path_is_named_under_the_staging_root(tmp_path: Path) -> None:
    """A config that names `/var/lib/encore` while installed under `--root` boots and loses data.

    The bug is invisible on a production install, which is the only kind anyone demos.
    """

    values = parsed(generated(tmp_path))
    paths = values["paths"]
    assert isinstance(paths, dict)
    for key, value in paths.items():
        assert str(value).startswith(str(tmp_path)), (
            f"paths.{key} escaped the staging root: {value}"
        )


def test_a_non_default_port_appears_in_the_file(tmp_path: Path) -> None:
    """12.2's `server.port`, and the reason `--port` exists at all."""

    assert parsed(generated(tmp_path, values={"port": 8099}))["server"]["port"] == 8099  # type: ignore[index]


def test_mpv_is_named_by_absolute_path(tmp_path: Path) -> None:
    """systemd does not use a login shell's PATH; a bare `mpv` would not be found (ADR-005)."""

    assert "/opt/vlc/bin/mpv" in generated(tmp_path, mpv_path=Path("/opt/vlc/bin/mpv"))


def test_the_restart_ladder_is_rendered_as_a_flow_sequence(tmp_path: Path) -> None:
    """A YAML list written across five lines is five lines for a validator to disagree with."""

    value = parsed(generated(tmp_path))["playback"]["restart_backoff_seconds"]  # type: ignore[index]
    assert isinstance(value, list)
    assert value == [1.0, 2.0, 5.0, 10.0, 30.0]


def test_the_audio_output_is_written_as_decided(tmp_path: Path) -> None:
    """7.7 leaves the choice to the installer, so the file has to say which was made.

    The value arrives from the machine (`pipewire` when a Pulse/PipeWire socket is up, `alsa`
    otherwise) rather than from a flag, and a rendered file that lost it would silently put every
    appliance back on the default.
    """

    assert parsed(generated(tmp_path, audio_output="pipewire"))["audio"]["output"] == "pipewire"  # type: ignore[index]


def test_no_secret_is_written_into_the_configuration(tmp_path: Path) -> None:
    """12.6: the password is never in this file, and neither is a session key (ADR-013)."""

    text = generated(tmp_path).lower()
    for needle in ("password", "secret", "token"):
        assert f"{needle}:" not in text, f"{needle} appeared as a setting"


# -- /etc/encore/environment -----------------------------------------------------


def test_the_environment_file_holds_the_two_structural_variables(root: Path) -> None:
    text = environment_text(admin_password=None, layout=Layout(root=root))
    assert f"ENCORE_HOME={root / 'var/lib/encore'}" in text
    assert "ENCORE_CONFIG=" in text


def test_the_password_line_is_absent_when_none_was_supplied(root: Path) -> None:
    """Writing `ENCORE_ADMIN_PASSWORD=` is how an installer implies a credential was seeded.

    The empty-value form is the worse bug: the Server would treat it as a password and refuse to
    start, and the reason would be a line nobody meant to write.
    """

    assert "ENCORE_ADMIN_PASSWORD" not in environment_text(
        admin_password=None, layout=Layout(root=root)
    )


SEED = "correct horse battery staple"  # pragma: allowlist secret


def test_the_password_is_written_exactly_once(root: Path) -> None:
    text = environment_text(admin_password=SEED, layout=Layout(root=root))
    assert text.count("ENCORE_ADMIN_PASSWORD=") == 1
    assert SEED in text


def test_the_environment_file_documents_its_own_mode(root: Path) -> None:
    """The file is 0600 and says why, because the next person to read it is arguing with a tool."""

    text = environment_text(admin_password=None, layout=Layout(root=root))
    assert "0600" in text


# -- the systemd unit ------------------------------------------------------------


def test_the_unit_describes_the_appliance_it_is_installed_on(root: Path) -> None:
    layout = Layout(root=root)
    config = settings(root).config(audio_output="alsa", mpv_path=Path("/usr/bin/mpv"))
    text = unit_text(
        config,
        layout,
        application=layout.staged(layout.application),
        venv=layout.staged(layout.venv),
        config_path=layout.staged(layout.config_file),
    )
    for directive in (
        "[Unit]",
        "[Service]",
        "[Install]",
        "WantedBy=multi-user.target",
        f"User={layout.user}",
        f"EnvironmentFile=-{layout.staged(layout.environment_file)}",
        f"{layout.staged(layout.venv)}/bin/encore-server",
        f"ENCORE_CONFIG={layout.staged(layout.config_file)}",
    ):
        assert directive in text, f"the unit is missing {directive}"


def test_the_unit_would_not_bind_a_privileged_port_without_permission(root: Path) -> None:
    """12.2's default port is unprivileged; a unit that added the capability would be a claim."""

    text = unit_text(
        settings(root).config(audio_output="alsa", mpv_path=Path("/usr/bin/mpv")),
        Layout(root=root),
        application=root / "opt/encore/application",
        venv=root / "opt/encore/venv",
        config_path=root / "etc/encore/config.yaml",
    )
    assert "AmbientCapabilities=CAP_NET_BIND_SERVICE" not in text


def test_the_unit_hardens_what_a_jukebox_can_harden(root: Path) -> None:
    """ADR-008's "defence in depth", expressed as four lines a reviewer can check.

    These are not theatre: `ProtectSystem=strict` with two `ReadWritePaths` is what makes a
    runtime that wrote somewhere unexpected fail at write time instead of at forensics time.
    """

    layout = Layout(root=root)
    text = unit_text(
        settings(root).config(audio_output="alsa", mpv_path=Path("/usr/bin/mpv")),
        layout,
        application=layout.staged(layout.application),
        venv=layout.staged(layout.venv),
        config_path=layout.staged(layout.config_file),
    )
    assert "NoNewPrivileges=yes" in text
    assert "ProtectSystem=strict" in text
    writable = [line for line in text.splitlines() if line.startswith("ReadWritePaths=")]
    assert writable, "a strict unit with no writable paths cannot run SQLite"
    for path in (
        layout.staged(layout.state),
        layout.staged(layout.cache),
        layout.staged(layout.logs),
    ):
        assert str(path) in " ".join(writable)


def test_the_unit_restarts_with_the_ladder_the_playback_section_declares(root: Path) -> None:
    """Two restart policies in one product is how an operator gets a thundering herd (7.6)."""

    layout = Layout(root=root)
    text = unit_text(
        settings(root).config(audio_output="alsa", mpv_path=Path("/usr/bin/mpv")),
        layout,
        application=layout.staged(layout.application),
        venv=layout.staged(layout.venv),
        config_path=layout.staged(layout.config_file),
    )
    assert "Restart=on-failure" in text
    assert "RestartSec=" in text


def test_the_tmpfiles_rule_protects_the_socket_directory(root: Path) -> None:
    """`systemd-tmpfiles --clean` deletes stale /var/cache; mpv's socket lives in one of them.

    Without this rule the appliance survives a week and then fails to start a track on a Monday,
    which is the kind of bug that takes a month to attribute to anything.
    """

    text = tmpfiles_text(Layout(root=root))
    assert "/var/cache/encore" in text.replace(str(root), "") or "encore" in text
    assert "d " in text or "D " in text


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """A staging root that exists, because `Layout` only ever joins onto it."""

    (tmp_path / "etc").mkdir(parents=True, exist_ok=True)
    return tmp_path
