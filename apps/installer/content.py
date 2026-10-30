"""What the files the installer writes actually say (SAPRS 12.2, 13.3, 13.6).

Three of the artifacts an install produces are text: the configuration, the unit, and the
environment file. They live here rather than in a directory of template files for one reason
— a template on disk is invisible to `mypy`, invisible to the test suite, and invisible to
the configuration models, while the whole point of generating a `config.yaml` is that every
value in it came out of a validated `EncoreConfig`.

The comments are the deliverable. SAPRS 13.2 makes the manual steps authoritative, and 12.2
makes this file the operator's only dial, so the generated file explains itself at the point
of doubt: which lines are worth changing, which one breaks playback silently, and which one
the installer owns and will overwrite.

`config_text` renders a model rather than a dictionary, so the file cannot contain a key the
Server would reject. The failure that guards against is the specific one where an installer
writes a plausible-looking configuration, reports success, and the appliance exits 78 on the
first boot — which is SAPRS 13.11's "passing health checks" arriving three hours late.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from apps.installer.layout import Layout

if TYPE_CHECKING:
    from encore.config.models import EncoreConfig

__all__ = ["config_text", "environment_text", "tmpfiles_text", "unit_text"]

#: What `systemctl status` will show. Kept short: it competes with the unit name.
SERVICE_DESCRIPTION = "Encore jukebox appliance"


def config_text(config: EncoreConfig, layout: Layout) -> str:
    """The `config.yaml` for this appliance, as an operator would want to find it.

    Args:
        config: The validated settings the installer was asked for. Nothing is invented:
            every value below is a field of a model that already accepted it.
        layout: Where the paths really are, including any staging root.

    Returns:
        YAML with the documentation of `examples/config.yaml` and the values of `config`,
        ending in a newline because POSIX text files do.
    """

    server, paths = config.server, config.paths
    audio, playback = config.audio, config.playback
    staged = layout.staged
    return f"""# Encore configuration (SAPRS Chapter 12). Written by encore-install.
#
# Encore reads this file once, at startup, validates it, and never writes it back. Anything
# Encore has to remember belongs in runtime.db, not here (SAPRS 12.4). An unknown key is an
# error rather than a warning, so a typo cannot become a silent default.
#
# To change something: edit it, then `sudo systemctl restart encore`.

server:
  # What to bind. 0.0.0.0 is correct for a LAN appliance (SAPRS 1.4); this is a machine's
  # address, not the name guests use — that is `domain` below.
  host: {server.host}
  port: {server.port}
  # The name a guest's phone resolves and the name printed on the QR code. These are as
  # much documentation as configuration: nothing in Encore answers to a name, the network
  # does. Set them to whatever your DNS or router actually says.
  hostname: {server.hostname}
  domain: {server.domain}

paths:
  # Read-only, published by the Library Builder. The Server never creates this file and
  # never writes to it (ADR-006, ADR-010). If it is missing, run encore-builder.
  library_db: {_scalar(staged(str(paths.library_db)))}
  # Queue, history, statistics and the administrator. Created on first start.
  runtime_db: {_scalar(staged(str(paths.runtime_db)))}
  # The image cache the Builder wrote, served from disk and never generated at runtime.
  artwork_dir: {_scalar(staged(str(paths.artwork_dir)))}
  # mpv's IPC socket and the Builder's scratch space. Must be writable, and must be local:
  # a socket on a network filesystem is a player nobody can tell to stop.
  temp_dir: {_scalar(staged(str(paths.temp_dir)))}
  # Read by the Builder only; the Server never scans it (ADR-010).
  music_dir: {_scalar(staged(str(paths.music_dir)))}

audio:
  # Which kind of sound server to open: "alsa" or "pipewire". mpv accepts either, and no version
  # floor is claimed for this choice (SAPRS 7.7, ADR-005) — the installer picked it from what the
  # machine has, because a silent appliance that reports healthy is the worst failure this file
  # can cause.
  #
  # The one thing it cannot see from there: PipeWire and PulseAudio usually run per-user, inside
  # someone's login session, and the appliance account has no session to be inside. On a box with
  # a desktop logged in and a jukebox running as `encore`, "alsa" is the answer.
  output: {_scalar(audio.output)}
  # Which device inside that output. `mpv --ao={audio.output} --audio-device=help` prints the
  # names mpv accepts, and `aplay -l` prints the cards — they are different spellings of the same
  # hardware, and only the first is what goes here. A name mpv cannot parse is not rejected: it
  # logs "Trying the default audio device..." and fails the way a dead card fails, so a typo in
  # this line looks exactly like a broken output.
  #
  # Read this before leaving it as generated. "default" means "whichever card registered first",
  # and on a Raspberry Pi 4 that is an HDMI port — with nothing plugged into it every file in the
  # library is refused with "audio output initialization failed", mp3 and m4a alike, and nothing
  # else about the appliance looks wrong (#48). Name the card you can actually hear:
  #   device: alsa/plughw:CARD=Headphones,DEV=0    # the 3.5 mm jack
  #   device: alsa/hdmi:CARD=vc4hdmi0,DEV=0         # an HDMI sink that is connected
  device: {_scalar(audio.device)}
  volume: {audio.volume}
  gapless: {_bool(audio.gapless)}
  # 0 ends a track; anything above it holds two decoders open at once, which on a Pi 4 is
  # paid for with a visible seek on the next start. 3 seconds is the polite party value.
  crossfade_seconds: {audio.crossfade_seconds}

queue:
  max_items: {config.queue.max_items}
  # Guests are anonymous, so a duplicate request is the same guest asking twice (SAPRS 8.2).
  allow_duplicates: {_bool(config.queue.allow_duplicates)}

playback:
  # The only place the mpv binary is named. A wrong path here looks like a healthy appliance
  # that never plays: the Supervisor restarts it, gets the same answer, backs off.
  mpv_path: {_scalar(playback.mpv_path)}
  # The restart ladder (SAPRS 7.6). The long final step is deliberate — a machine with a
  # dead sound card should stop trying before it fills the journal.
  restart_backoff_seconds: [{", ".join(str(step) for step in playback.restart_backoff_seconds)}]
  progress_interval_seconds: {playback.progress_interval_seconds}

search:
  page_size: {config.search.page_size}

logging:
  # "json" is what the journal wants, "console" is what a terminal wants. Both are
  # structured; the fields do not change between them.
  level: {config.logging.level}
  format: {config.logging.format}

admin:
  # Idle minutes before a session ends. The password is not in this file, and neither is a
  # session secret, because sessions are not kept on disk (SAPRS 12.6, ADR-013).
  session_minutes: {config.admin.session_minutes}

builder:
  # Read by `encore-builder`, and by nothing that serves a guest (ADR-001). The Builder is a
  # separate command that finishes, and this is where its policy lives rather than in a flag:
  # a build run by a timer has no terminal to remember one in (#57).
  musicbrainz: {_bool(config.builder.musicbrainz)}
  # An album whose files carry no embedded picture asks the Cover Art Archive once, and gets
  # no picture at all when the release cannot be named exactly. Needs the line above.
  album_art: {_bool(config.builder.album_art)}
"""


def environment_text(*, admin_password: str | None, layout: Layout) -> str:
    """`/etc/encore/environment` — the unit's `EnvironmentFile`, mode 0600.

    Two variables are structural and go into every install. The third appears only when the
    installer's own environment had it: writing an empty `ENCORE_ADMIN_PASSWORD=` line is how
    an installer creates the impression that a credential was seeded when nothing was, and
    the difference only surfaces when somebody tries to sign in.

    Args:
        admin_password: The value to seed the credential with, or None. Read from the
            environment and never from a flag — a flag is visible in every user's `ps`
            output for as long as the process lives (SAPRS 12.6).
        layout: Where the state directory and the configuration are.
    """

    lines = [
        "# Read by systemd's EnvironmentFile directive, and by nothing else (SAPRS 13.6).",
        "# Mode 0600, owner root: one line of this file may be a password. The service does",
        "# not read this file, it is handed the variables, so the service user is not in the",
        "# access path for a secret it has no use for once the first boot is over.",
        f"ENCORE_HOME={layout.staged(layout.state)}",
        f"ENCORE_CONFIG={layout.staged(layout.config_file)}",
    ]
    if admin_password is not None:
        lines += [
            "",
            "# The credential is seeded from this line the first time the appliance starts",
            "# with no administrator (SAPRS 12.6, ADR-015). It is then spent: once",
            "# `admin_state` has a row the value is ignored, and it is never a way to change",
            "# a password. Delete this line once the box has run — `encore-install verify`",
            "# complains while it is still here.",
            f"ENCORE_ADMIN_PASSWORD={admin_password}",
        ]
    return "\n".join(lines) + "\n"


def unit_text(
    config: EncoreConfig, layout: Layout, *, application: Path, venv: Path, config_path: Path
) -> str:
    """The systemd unit (SAPRS 13.6).

    Each line answers a question an operator asks at 11pm, so the comments cite the
    specification rather than explaining systemd:

    * starts after the network it serves and the sound server it plays through, because a
      jukebox that beats PipeWire to the party has no output;
    * restarts with systemd's own pacing while `PlaybackSupervisor` handles the engine
      (SAPRS 7.6) — two ladders, because the two failures are unrelated;
    * stops in a way that reaches the mpv child as well as the server (SAPRS 8.7);
    * and runs as an unprivileged account that is not root (13.5).

    Args:
        config: The settings, for the bind address, the music directory and the name.
        layout: Ownership, and the staging root.
        application: Where the code is, which becomes `WorkingDirectory`.
        venv: The environment whose `bin/encore-server` is exec'd.
        config_path: The file to point at. Literal rather than `${ENCORE_CONFIG}`: a unit
            whose ExecStart depends on an EnvironmentFile that may not exist is a unit that
            starts with a missing argument, and configuration errors are the thing this
            installer is supposed to catch before the party.
    """

    server = config.server
    state = layout.staged(layout.state)
    cache = layout.staged(layout.cache)
    logs = layout.staged(layout.logs)
    music = layout.staged(str(config.paths.music_dir))
    return f"""# Encore — a headless jukebox for a room full of phones.
# Written by encore-install. Change it with `systemctl edit encore`, not here: this file is
# the installer's, and the next upgrade will overwrite anything added to it.

[Unit]
Description={SERVICE_DESCRIPTION} ({server.domain})
Documentation=https://github.com/clydepro/encore/blob/main/docs/Administrator-Guide.md
# 13.6's "network readiness". `network-online` rather than `network`, because the difference
# is the first ten seconds after a power cut, and `sound.target` because starting before the
# sound server is starting with no card to open (SAPRS 7.7).
After=network-online.target sound.target
Wants=network-online.target
# A crash loop must not be silenced by the start limiter: the appliance's own supervisor
# recovers the engine (SAPRS 7.6) and a unit that gave up quietly is the failure mode this
# line exists to prevent.
StartLimitIntervalSec=0

[Service]
Type=simple
# 13.5: a dedicated account, never root. `Group` is the service group, and the installer
# adds it to `audio` when that group exists — without it, ALSA opens and plays into nothing.
User={layout.user}
Group={layout.group}
WorkingDirectory={application}
Environment=ENCORE_HOME={state}
Environment=ENCORE_CONFIG={config_path}
Environment=XDG_CACHE_HOME={cache}
# Leading dash: an install that seeded no password has no file here, and a unit that names
# a required EnvironmentFile cannot start for its absence.
EnvironmentFile=-{layout.staged(layout.environment_file)}
ExecStart={venv}/bin/encore-server --config {config_path}
# The service owns an mpv child, so stopping has to reach both. `mixed` signals the main
# process first so the lifespan can shut the engine down properly, then takes anything left
# holding the sound card (SAPRS 8.7's step 2).
KillMode=mixed
KillSignal=SIGINT
TimeoutStopSec=30
Restart=on-failure
RestartSec=5
# Sandboxing, limited to what cannot break playback. `PrivateDevices=yes` would hide
# /dev/snd, and an appliance in a sandbox with no sound card reports healthy and plays
# nothing, so the audio nodes stay visible on purpose (SAPRS 7.7 leaves the device to the
# installer, and the installer leaves it to mpv).
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=read-only
ProtectControlGroups=yes
RestrictSUIDSGID=yes
LockPersonality=yes
# 13.3's writable places, and nothing else. Configuration is read-only at runtime (12.4) and
# this is what turns that sentence into a property rather than an intention.
ReadWritePaths={state} {cache} {logs}
# The music directory is read by the Builder and mounted read-only for the Server, which
# never scans it (ADR-010) — the line is here so a misfiled `paths.music_dir` fails loudly.
ReadOnlyPaths={music}
LogsDirectory=encore
LogsDirectoryMode=0750

[Install]
WantedBy=multi-user.target
"""


def tmpfiles_text(layout: Layout) -> str:
    """`/usr/lib/tmpfiles.d/encore.conf`, so a tmpfiles clean cannot strand the socket.

    mpv's IPC socket lives under `/var/cache/encore/temp`, which is disposable by
    definition — and `systemd-tmpfiles --clean` finds it disposable enough to remove the
    directory itself. `socket_path_for` creates it on use, but a Supervisor that cannot bind
    a socket because nobody can make its directory is a crash loop rather than a delay.
    """

    return (
        "# Encore's cache directories, recreated after a systemd-tmpfiles clean.\n"
        f"d {layout.staged(layout.cache)} 0755 {layout.user} {layout.group} -\n"
        f"d {layout.staged(layout.temp)} 0700 {layout.user} {layout.group} -\n"
    )


def _scalar(value: Path | str) -> str:
    """A value for YAML, quoted only when bare would not parse.

    Pydantic happily takes `/opt/music` and equally happily takes `"/opt/music: west"`; the
    second one is not valid YAML unquoted, and a colon in a mount point is not exotic on a
    box with a music share. The same is true of an ALSA device: `alsa/plughw:CARD=Headphones,
    DEV=0` is a value a working appliance writes, and quoting is what keeps it one value. Quoting
    everything would be shorter and would make the file nobody wants to read.
    """

    text = str(value)
    if any(marker in text for marker in (": ", " #")) or text != text.strip() or not text:
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return text


def _bool(value: bool) -> str:
    """YAML's `true`/`false`, not Python's."""

    return "true" if value else "false"
