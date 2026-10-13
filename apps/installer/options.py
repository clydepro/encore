"""What the operator asked for, before anything is looked at (SAPRS 13.7, 13.10).

A record rather than a namespace of argparse output, because `plan.py` must be callable from
a test without going through a command line, and because the two places a password can arrive
from — an environment variable and a flag — have to be distinguished *here* rather than
discovered later. `admin_password` is `repr=False` and has exactly one documented source: the
environment. A `--admin-password` flag would put the appliance's only credential into every
user's `ps` output and into shell history, which is SAPRS 12.6's sentence about secrets,
so the CLI refuses to offer it.

`Settings` is the installer's small vocabulary of dials — the four things a person actually
knows about their network before the install (`host`, `port`, `hostname`, `domain`) plus
where the music is. Everything else in the generated `config.yaml` is a default from
`encore/config/models.py`, because a flag for every field is how an installer ends up with a
hundred flags and a configuration file nobody typed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from apps.installer.layout import Layout
from encore.config.models import (
    AdminConfig,
    AudioConfig,
    EncoreConfig,
    LoggingConfig,
    PathsConfig,
    PlaybackConfig,
    QueueConfig,
    SearchConfig,
    ServerConfig,
)

__all__ = ["Command", "Options", "Settings"]


class Command(StrEnum):
    """The lifecycle, as SAPRS 13.7, 13.9 and 13.10 name it.

    `verify` is 13.11's acceptance list as a command rather than as prose, and
    `reset-credential` is the locked-out operator's path — a maintenance operation in the
    sense of 11.6's "approved maintenance", which is why it requires `--yes`. What would
    otherwise be a sixth command, `plan`, is `install --dry-run`: one action list, one flag,
    and nothing that can drift from the install it describes.
    """

    INSTALL = "install"
    UPGRADE = "upgrade"
    REMOVE = "remove"
    VERIFY = "verify"
    RESET_CREDENTIAL = "reset-credential"


@dataclass(frozen=True, slots=True, kw_only=True)
class Settings:
    """The four dials a person has opinions about, plus the music.

    Attributes:
        host: `server.host`. Default is every interface, which is what SAPRS 1.4's room
            full of phones needs.
        port: `server.port`. Kept non-80 by default because 80 needs `CAP_NET_BIND_SERVICE`
            and an appliance that runs as an unprivileged account (13.5) should not need a
            capability to bind a port.
        hostname: `server.hostname`, the mDNS name.
        domain: `server.domain`, the name the QR code will encode. It is the only value here
            that a guest ever sees, which is why `install` reports it twice.
        music_dir: `paths.music_dir`. Read by the Builder alone (ADR-010).
        log_format: `logging.format`. `json` under systemd, `console` on a terminal; the
            installer cannot tell which it is being run from, so it asks.
    """

    host: str = "0.0.0.0"  # noqa: S104 - a LAN appliance binds everything by design (1.4)
    port: int = 8080
    hostname: str = "jukebox"
    domain: str = "jukebox.home.arpa"
    music_dir: Path = Path("/opt/music")
    log_format: str = "json"

    def config(self, *, audio_output: str, mpv_path: Path) -> EncoreConfig:
        """The configuration this installer would write, as the Server's own model.

        A real `EncoreConfig` rather than a dictionary for one reason: constructing it runs
        the validators the Server runs (SAPRS 12.5), so a port out of range or a music path
        that cannot be a path is a failure at the command line, before anything has been
        written to a machine that was previously working.

        Args:
            audio_output: `alsa` or `pipewire`, decided from the machine (SAPRS 7.7).
            mpv_path: The binary the Supervisor will launch, resolved from the machine.
        """

        return EncoreConfig(
            server=ServerConfig(
                host=self.host,
                port=self.port,
                hostname=self.hostname,
                domain=self.domain,
            ),
            paths=PathsConfig(music_dir=self.music_dir),
            audio=AudioConfig(output=audio_output),
            queue=QueueConfig(),
            playback=PlaybackConfig(mpv_path=mpv_path),
            search=SearchConfig(),
            logging=LoggingConfig(format=self.log_format),  # type: ignore[arg-type]
            admin=AdminConfig(),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class Options:
    """One invocation of the installer.

    Attributes:
        command: What to do.
        layout: Where to do it, including any staging root.
        source: The checkout to install from. Defaults to the tree this module is running
            out of, which is what "run the installer from the clone" means in practice.
        config_path: An existing `config.yaml` to adopt instead of generating one (SAPRS
            13.8's "preserve the configuration", taken literally).
        settings: The dials.
        service: Whether to touch systemd. False for a container or a staged image, where
            `install` is a filesystem operation with no PID 1 to register with.
        dry_run: Plan, report, change nothing.
        assume_yes: Answer the confirmation for anything destructive.
        offline: Resolve dependencies from cache only (`uv sync --offline`).
        as_json: The same report, as data.
        purge_config: 13.10's second scope, named.
        purge_data: 13.10's third scope, named, and the only thing in the installer that
            deletes something it did not create.
        admin_password: Seeded on first boot, read from `ENCORE_ADMIN_PASSWORD` by `main`.
        answers: Anything the operator already committed to, recorded so the report can say
            so. Not an input.
    """

    command: Command = Command.INSTALL
    layout: Layout = field(default_factory=Layout)
    source: Path | None = None
    config_path: Path | None = None
    settings: Settings = field(default_factory=Settings)
    service: bool = True
    dry_run: bool = False
    assume_yes: bool = False
    offline: bool = False
    as_json: bool = False
    purge_config: bool = False
    purge_data: bool = False
    admin_password: str | None = field(default=None, repr=False)

    @property
    def root(self) -> Path:
        return self.layout.root

    @property
    def staged(self) -> bool:
        """Whether this is a rehearsal rather than an install.

        Derived from the root rather than from a flag, so there is one answer: a prefix is
        either in the paths or it is not, and an installer that believed a `--staging` flag
        while writing to `/` would be an installer that lied about what it did.
        """

        return self.layout.root != Path("/")
