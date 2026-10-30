"""Regression: #57 — MusicBrainz had no configuration home, so it was never on.

Reproduced on the appliance, in a build an operator had every reason to expect would help:

```console
$ encore-builder --music-dir /opt/music
  MusicBrainz        disabled
  Artwork            0    images written to the cache
  [ok     ] artwork
Published /var/lib/encore/library.db

$ sqlite3 /var/lib/encore/library.db 'select count(*) from songs where artwork_id is null'
1989
$ sqlite3 /var/lib/encore/library.db "select name from artists where name glob '[0-9]*'"
01
```

Three defects, one cause. `--musicbrainz` existed only as a command-line flag, so a systemd timer,
a CI job and anyone following the Administrator Guide built with enrichment off. The enrichment
stage asked "is this field empty?", so `artist: 01` — a rip tool writing the track number into the
artist field, 34 files — and `date: 24` (2 files) were neither corrected nor asked about. And
SAPRS 6.7's single artwork rule, prefer the file's own image, was the only rule: a library of FLAC
files, whose container carries no picture, published 463 grey tiles and reported nothing unusual.

The first five tests are the configuration half — what a build decides when nobody typed a flag.
The rest are what those flags were for, run through the pipeline against generated files, because
"enrichment is on" is a claim about an artifact rather than about a boolean. The fix is ADR-016.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

import apps.builder.main as builder_main
from apps.builder.musicbrainz import MusicBrainzUnavailableError, Recording, Release
from apps.builder.pipeline import BuildOptions, run
from apps.builder.report import BuildReport
from encore.config.service import ConfigurationService
from encore.repositories.contract import Table
from tests.support.media import AlbumSpec, MediaSpec, cover_image, generate_albums, generate_track

pytestmark = pytest.mark.regression


class Archive:
    """The whole client protocol, answering from dictionaries and counting the asking."""

    def __init__(
        self,
        *,
        recordings: dict[str, Recording] | None = None,
        releases: dict[str, Sequence[Release]] | None = None,
        covers: dict[str, bytes] | None = None,
        down: bool = False,
    ) -> None:
        self.recordings = recordings or {}
        self.catalog = releases or {}
        self.covers = covers or {}
        self.down = down
        self.searches: list[tuple[str, str, str]] = []
        self.queries: list[tuple[str, str]] = []
        self.asked_for: list[str] = []

    @property
    def available(self) -> bool:
        return not self.down

    def _open(self) -> None:
        if self.down:
            raise MusicBrainzUnavailableError("simulated outage")

    def search(self, *, artist: str, release: str, track: str) -> Recording | None:
        self._open()
        self.searches.append((artist, release, track))
        return self.recordings.get(track.lower())

    def recording(self, recording_id: str) -> Recording | None:  # noqa: ARG002
        self._open()
        return None

    def releases(self, *, artist: str, album: str) -> Sequence[Release]:
        self._open()
        self.queries.append((artist, album))
        return self.catalog.get(album, ())

    def cover(self, release_id: str) -> bytes | None:
        self._open()
        self.asked_for.append(release_id)
        return self.covers.get(release_id)


def _options(home: Path, music: Path, **overrides: object) -> BuildOptions:
    return BuildOptions(
        music_dir=music,
        library_db=home / "library.db",
        artwork_dir=home / "artwork",
        temp_dir=home / "tmp",
        library_version="regression-57",
        **overrides,  # type: ignore[arg-type]
    )


def _build(home: Path, music: Path, **overrides: object) -> BuildReport:
    report = run(_options(home, music, **overrides))
    assert report.published_to is not None, report.errors
    return report


def _artifact(report: BuildReport) -> Path:
    """The published file, asserted rather than assumed.

    `published_to` is Optional because a build that published nothing is a normal outcome, and
    every test in this section is about one that did — which makes the narrowing belong here rather
    than repeated at each query, where a reader would have to work out which of the four copies is
    the one that can be None.
    """

    assert report.published_to is not None
    return report.published_to


def _write_config(home: Path, music: Path, *, builder: str = "") -> Path:
    """A `config.yaml` in the shape the installer writes, with one optional section appended."""

    path = home / "config.yaml"
    path.write_text(
        "paths:\n"
        f"  music_dir: {music}\n"
        f"  library_db: {home / 'library.db'}\n"
        f"  runtime_db: {home / 'runtime.db'}\n"
        f"  artwork_dir: {home / 'artwork'}\n"
        f"  temp_dir: {home / 'tmp'}\n"
        f"{builder}",
        encoding="utf-8",
    )
    return path


def _decide(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, argv: list[str], *, builder: str = ""
) -> BuildOptions:
    """The options one command line resolves to, with the build itself stopped at the door.

    `run` is replaced rather than wrapped: these tests are about the decision made before any file
    is opened, and a build that ran would answer a different question — which is what the tests
    below this one do instead, with the artifact.
    """

    home = tmp_path / "appliance"
    home.mkdir(parents=True, exist_ok=True)
    music = home / "music"
    music.mkdir(parents=True, exist_ok=True)
    captured: dict[str, BuildOptions] = {}

    def fake_run(options: BuildOptions, **_: Any) -> BuildReport:
        captured["options"] = options
        return BuildReport(library_version="regression-57")

    monkeypatch.setattr(builder_main, "run", fake_run)
    argv = [*argv, "-c", str(_write_config(home, music, builder=builder))]
    # 0 is "published", 3 is "there was nothing to build"; the empty fixture directory guarantees
    # the second, and neither is the answer this helper is reading.
    assert builder_main.main(argv) in (builder_main.EXIT_OK, builder_main.EXIT_ABORTED)
    return captured["options"]


# -- where the decision comes from ---------------------------------------------------------


def test_a_configuration_that_says_nothing_asks_musicbrainz(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The issue's headline: an ordinary build, no flags, and enrichment was off.

    Default-on is a decision with a cost stated beside it: one request per second, so about two
    minutes on a 3,000-file library with gaps, and roughly two requests per album that has never
    been identified. What it replaces is a feature that ran only when someone remembered a flag in
    a terminal session, which measured across the appliance's life so far is never.
    """

    options = _decide(monkeypatch, tmp_path, [])
    assert (options.enrich, options.album_art) == (True, True)


def test_the_file_can_still_say_no(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Default-on is not mandatory-on.

    A host with no route to musicbrainz.org, and a school network where the Builder would be the
    only thing talking, are both reasons to write `musicbrainz: false` once and have every later
    build — including the timer's — mean it.
    """

    options = _decide(
        monkeypatch,
        tmp_path,
        [],
        builder="builder:\n  musicbrainz: false\n  album_art: false\n",
    )
    assert (options.enrich, options.album_art) == (False, False)


def test_a_flag_beats_the_file_for_one_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Three states on the command line, two in the file, and "not given" is not "false".

    `BooleanOptionalAction` is what makes that possible. The bug it prevents is the shape where
    `--musicbrainz` defaults to False and so silently turns the configuration off on every build
    that did not mention it; both directions are asserted because a flag that works one way is a
    flag that gets worked around with a text editor.
    """

    off = "builder:\n  musicbrainz: false\n  album_art: false\n"
    assert _decide(monkeypatch, tmp_path, ["--musicbrainz"], builder=off).enrich is True
    both = _decide(monkeypatch, tmp_path, ["--musicbrainz", "--album-art"], builder=off)
    assert (both.enrich, both.album_art) == (True, True)
    on = "builder:\n  musicbrainz: true\n  album_art: true\n"
    assert _decide(monkeypatch, tmp_path, ["--no-musicbrainz"], builder=on).enrich is False
    assert _decide(monkeypatch, tmp_path, ["--no-album-art"], builder=on).album_art is False


def test_offline_asks_nothing_and_is_not_a_setting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`--offline` is a fact about this afternoon's network, so it outranks the file.

    It is also the flag that must not lose information, which is the test after this one: turning
    the asking off is easy, and the hard half is not letting an answer you already had be
    overwritten by a question you did not ask.
    """

    options = _decide(
        monkeypatch, tmp_path, ["--offline"], builder="builder:\n  musicbrainz: true\n"
    )
    assert (options.enrich, options.album_art) == (False, False)


def test_the_server_starts_on_a_file_that_configures_the_builder(tmp_path: Path) -> None:
    """Why `builder:` lives in `encore/config/models.py` rather than in `apps/builder/`.

    12.5's `extra="forbid"` has a consequence: a section the Server does not model is an unknown
    key, and an unknown key is a startup failure. Without the model, the Administrator Guide would
    document a file that stops an appliance booting — correct configuration, dead jukebox.

    Neither value is read by a request handler. ADR-001's boundary is enforced by
    `test_architecture_guardrails`, not by a default; this is the narrower claim that the Server
    can at least read past the section.
    """

    home, music = tmp_path, tmp_path / "music"
    assert ConfigurationService(_write_config(home, music)).config.builder.musicbrainz is True
    quiet = _write_config(home, music, builder="builder:\n  musicbrainz: false\n")
    assert ConfigurationService(quiet).config.builder.album_art is True


# -- what the flags were for ---------------------------------------------------------------


def test_a_build_with_no_flags_corrects_a_value_that_is_present_and_wrong(tmp_path: Path) -> None:
    """`artist: 01` and `date: 24` used to be trusted, because present read as correct.

    SAPRS 6.6's second level says "fill gaps **or correct implausible values**" and the stage
    implemented only the first clause. Both halves are asserted in the artifact rather than in a
    stage's return value, because the artifact is what the guest sees: an artist named "01" was a
    row in `artists`, and a release year of 24 was printed on an album page.

    The counter file is written flat in the music root, not in an artist directory: ADR-010's level
    3 fixes a bad artist from the path it was found in, and a fixture the path could repair would
    pass with the enrichment stage doing nothing.
    """

    music = tmp_path / "music"
    generate_albums(
        [AlbumSpec.with_tracks("Real Artist", "True Album", count=3, fmt="flac")], music
    )
    generate_track(MediaSpec(title="Morning", artist="01", album="True Album", date="24"), music)
    answer = Recording(artist="Daft Punk", album="True Album", year="1974")
    report = _build(
        tmp_path / "home",
        music,
        enrich=True,
        album_art=False,
        client=Archive(recordings={"morning": answer}),
    )
    with sqlite3.connect(_artifact(report)) as connection:
        names = {str(row[0]) for row in connection.execute(f"SELECT name FROM {Table.ARTISTS}")}
        dates = {
            str(row[0])
            for row in connection.execute(f"SELECT release_date FROM {Table.ALBUMS}")
            if row[0]
        }
    assert "01" not in names, "a counter is a gap now, not an artist"
    assert "Daft Punk" in names
    assert "24" not in dates, "a fragment of a year is not a year"
    assert "1974" in dates


def test_a_build_that_asks_nothing_keeps_what_the_last_build_learned(tmp_path: Path) -> None:
    """SAPRS 6.6's "shall not be degraded to un-enriched data", with the network off on purpose.

    The first build repairs an album; the second is what `--offline`, `--no-musicbrainz` and a
    machine with no route produce. The reuse path used to sit inside the `if options.enrich` gate,
    so an offline rebuild of unchanged files rewrote the enriched value back to the tag and the
    library quietly got worse every time someone was careful about the network. Asking nothing is
    allowed to be free; forgetting is not.
    """

    music = tmp_path / "music"
    generate_albums(
        [
            AlbumSpec(
                artist="Tagged",
                album="Ghost Album",
                tracks=tuple(
                    MediaSpec(
                        title=f"Song {index}", artist="Tagged", album=None, track_number=index
                    )
                    for index in (1, 2)
                ),
            )
        ],
        music,
    )
    home = tmp_path / "home"
    first = _build(
        home,
        music,
        enrich=True,
        album_art=False,
        client=Archive(recordings={"song 1": Recording(album="Found Album", artist="Tagged")}),
    )
    with sqlite3.connect(_artifact(first)) as connection:
        repaired = {str(row[0]) for row in connection.execute(f"SELECT title FROM {Table.ALBUMS}")}
    assert "Found Album" in repaired

    offline = _build(home, music, enrich=False, album_art=False, client=Archive(down=True))
    with sqlite3.connect(_artifact(offline)) as connection:
        kept = {str(row[0]) for row in connection.execute(f"SELECT title FROM {Table.ALBUMS}")}
    assert "Found Album" in kept, "an offline rebuild is not an amnesiac rebuild"


def test_an_album_with_no_picture_in_it_stops_being_grey(tmp_path: Path) -> None:
    """SAPRS 6.7's second precedence level end to end, which is ADR-016 in one build.

    Three albums: one whose files carry their own art, and two FLAC albums that cannot. `artwork`
    is the number #52 taught the report to tell the truth about; `artwork_held` is the number this
    issue is about — 1,060 covered songs and 1,989 that were not, with nothing in the build that
    could have made one.
    """

    music = tmp_path / "music"
    generate_albums(
        [
            AlbumSpec.with_tracks(
                "Covered Artist", "Has Art", count=2, artwork=cover_image(color=(10, 60, 140))
            ),
            AlbumSpec.with_tracks("Covered Artist", "No Art", count=2, fmt="flac"),
            AlbumSpec.with_tracks("Covered Artist", "Also None", count=2, fmt="flac"),
        ],
        music,
    )
    home = tmp_path / "home"
    report = _build(
        home,
        music,
        enrich=True,
        album_art=True,
        client=Archive(
            releases={
                "No Art": (Release(release_id="rel-2", title="No Art", artist="Covered Artist"),),
                "Also None": (
                    Release(release_id="rel-3", title="Also None", artist="Covered Artist"),
                ),
            },
            covers={
                "rel-2": cover_image(color=(0, 0, 0)),
                "rel-3": cover_image(color=(255, 0, 0)),
            },
        ),
    )
    assert report.album_art.startswith("2 of 2 albums"), report.album_art
    with sqlite3.connect(_artifact(report)) as connection:
        covered = int(
            connection.execute(
                f"SELECT COUNT(*) FROM {Table.SONGS} WHERE artwork_id IS NOT NULL"
            ).fetchone()[0]
        )
        total = int(connection.execute(f"SELECT COUNT(*) FROM {Table.SONGS}").fetchone()[0])
    assert (covered, total) == (6, 6), "no album in this library is grey"
    assert report.artwork_held == 3, "one embedded, two from the archive, no duplicates"


def test_a_release_identified_once_is_not_searched_for_again(tmp_path: Path) -> None:
    """The identifier is written down, and the next build reads it.

    Two requests per newly identified album is the price of naming a release; charging it every
    night forever is a bug. `albums.musicbrainz_release_id` existed in the schema and was never
    written, and `PreviousEnrichment.release_id` existed in a dataclass and was never loaded: a
    documented cache with no reader, which is what #52 called a defect rather than a TODO.

    The fixture is an album the archive has no picture of, which is the case the discount is for.
    Where #52 applies instead — an unchanged file whose artwork was carried over — the album is not
    asked about at all, and the identifier is what keeps *this* case down to one request.
    """

    music = tmp_path / "music"
    generate_albums(
        [AlbumSpec.with_tracks("Quiet Artist", "Silent Album", count=2, fmt="flac")], music
    )
    home = tmp_path / "home"
    archive = Archive(
        releases={
            "Silent Album": (
                Release(release_id="rel-7", title="Silent Album", artist="Quiet Artist"),
            ),
        },
        covers={},
    )
    first = _build(home, music, enrich=True, album_art=True, client=archive)
    assert (len(archive.queries), len(archive.asked_for)) == (1, 1)
    assert first.album_art == "0 of 1 albums without embedded art, 2 requests, 1 with no image"

    _build(home, music, enrich=True, album_art=True, client=archive)
    assert len(archive.queries) == 1, "the search happened once, in the first build"
    assert len(archive.asked_for) == 2, "and only the picture was asked for twice"


def test_the_report_says_what_was_asked(tmp_path: Path) -> None:
    """A default-on feature has to be legible, or an operator cannot turn it off for a reason.

    `MusicBrainz: disabled` was the whole of what a build said about the most expensive thing it
    can do, and the cases it folded together are genuinely different: off, unreachable, and asked.
    Each gets its own sentence now, because each has its own next action.

    One file has no album tag, so there is a gap for the enrichment stage to be *about*. Without it
    the middle build would truthfully say "0 lookups" and this test would prove nothing: a question
    the service refused to answer and a question nobody needed to ask have to read differently.
    """

    music = tmp_path / "music"
    generate_albums(
        [AlbumSpec.with_tracks("Loud Artist", "Loud Album", count=2, fmt="flac")], music
    )
    generate_track(
        MediaSpec(title="Quiet Song", artist="Loud Artist", album=None),
        music / "loud artist" / "loud-album",
    )
    off = _build(tmp_path / "a", music, enrich=False, album_art=False)
    assert off.musicbrainz == "off"
    assert off.album_art == "off"

    unreachable = _build(
        tmp_path / "b", music, enrich=True, album_art=True, client=Archive(down=True)
    )
    assert unreachable.musicbrainz.startswith("unavailable"), unreachable.musicbrainz
    assert unreachable.album_art == "archive unavailable"

    asked = _build(
        tmp_path / "c",
        music,
        enrich=True,
        album_art=True,
        client=Archive(
            releases={
                "Loud Album": (
                    Release(release_id="rel-8", title="Loud Album", artist="Loud Artist"),
                )
            },
            covers={"rel-8": cover_image(color=(9, 90, 9))},
        ),
    )
    assert asked.musicbrainz == "enabled, 0 lookups, 0 fields repaired", asked.musicbrainz
    assert asked.album_art == "1 of 1 albums without embedded art, 2 requests", asked.album_art
