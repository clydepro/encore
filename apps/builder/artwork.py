"""Stage 5 — artwork (SAPRS 6.7, 5.6; ADR-010).

Extracts embedded images, normalizes them, and writes a content-addressed cache
beside the database. SAPRS 5.6 fixes the storage shape — files plus references,
never BLOBs in rows, because a BLOB inflates every copy of a library that is
otherwise mostly integers and text — and ADR-010 rejects the alternative on the
same grounds.

Four things this stage has to get right, in the order they surprise people:

* **"Nothing to write" is normal.** The measured corpus has embedded artwork on
  98% of M4A, 23% of MP3 and 0% of FLAC, so a FLAC-heavy library produces few
  files and no warnings. 6.7's "missing artwork must not make a track unplayable"
  is why this count is not an error count.
* **Content-addressed, not path-addressed.** Two files of one album carry the same
  image and get one cache entry, which is what makes a rebuild cheap and a
  duplicate cover impossible. The address is the digest of the bytes that will be
  *written*, not of the bytes that were found, so re-running a build with the same
  images overwrites nothing and adds nothing — and the two digests are not the same
  number. Normalization decodes and re-encodes, which is many-to-one by construction:
  one album's cover, tagged twice, arrives as two byte strings and leaves as one JPEG.
  Keyed on what was found rather than on what was written, that is two assets naming
  one file, and `artwork.relative_path` is UNIQUE, so the build dies in its own
  transaction over the fifteenth copy of a picture nobody can tell apart (#51).
* **Normalize the pixels, keep the aspect ratio.** A 3000x3000 booklet scan costs a
  guest's phone a second to decode; 640x640 JPEG at quality 82 does not look
  different at the sizes the interface uses (SAPRS 9.2). Decoding is also where a
  corrupt image is found, and that is a missing picture rather than a failed build.
* **An unchanged file is never opened, so it arrives with no payload.** The incremental
  build hands this stage the reference the previous build resolved to (`previous`), because
  the artifact is written from scratch every run and an image this stage does not name is
  absent from the published library — zero rows over a cache of 222 JPEGs, validated green,
  which is #52. Reuse is why `generated` and `len(assets)` are different numbers.
"""

from __future__ import annotations

import hashlib
import io
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from apps.builder.records import ArtworkAsset, ArtworkOutcome, Track

__all__ = ["MAX_EDGE", "QUALITY", "ArtworkLimits", "ArtworkOutcome", "generate", "store_one"]

#: Longest edge of a cached image, in pixels. The interface shows artwork at
#: 48-320 px; 640 leaves room for a retina-density tile without shipping a scan.
MAX_EDGE: Final = 640

#: JPEG quality for the cache — where a cover stops losing detail visibly at the
#: sizes above and starts gaining kilobytes quickly.
QUALITY: Final = 82

#: Distinct images one build will write. A library tagged with a different image
#: per track would otherwise produce 15,000 files on the same flash as the
#: databases, and the run would spend longer on I/O than on the index.
MAX_ASSETS: Final = 20_000


@dataclass(frozen=True, slots=True, kw_only=True)
class ArtworkLimits:
    """Tunables for the cache, as one value object rather than four arguments.

    Defaults are the constants above. They are parameters because the performance
    suite (SAPRS 14.9) builds a 15,000-song library and does not need 15,000
    images to do it; a flag that skips the work in a test and not in production
    would be worth nothing.
    """

    max_edge: int = MAX_EDGE
    quality: int = QUALITY
    max_assets: int = MAX_ASSETS
    subdir: str = "album"


def generate(
    tracks: Sequence[Track],
    cache_dir: Path,
    limits: ArtworkLimits | None = None,
    *,
    previous: Mapping[Path, ArtworkAsset] | None = None,
) -> ArtworkOutcome:
    """Write every embedded image into the cache and map files to references.

    Args:
        tracks: Enriched tracks, in discovery order.
        cache_dir: `paths.artwork_dir`. Created if absent — the Builder owns the
            cache and the Server never writes to it (SAPRS 5.2).
        limits: See `ArtworkLimits`.
        previous: What the last build resolved each file to, keyed by path. A file whose
            incremental key matched was not opened this run, so it arrives here with no
            payload and would have no reference at all unless this said what its picture
            already is (SAPRS 6.9, ADR-010). #52: without it an incremental build published
            a library with **zero** artwork rows over a cache of 222 JPEGs, validated green,
            and renamed it into place — the artifact is written from scratch every run, so a
            reference this stage does not name is a reference that does not exist.

    Returns:
        An `ArtworkOutcome`. A track whose image cannot be decoded is absent from
        `by_file` and counted: corrupt artwork is a missing picture, never a
        missing song (SAPRS 6.7).
    """

    bounds = limits if limits is not None else ArtworkLimits()
    carried = previous if previous is not None else {}
    cache_dir.mkdir(parents=True, exist_ok=True)
    store: dict[str, ArtworkAsset] = {}
    seen: dict[str, ArtworkAsset] = {}
    by_file: dict[Path, ArtworkAsset] = {}
    written = 0
    resolved = 0
    reused = 0
    empty = 0
    failed = 0
    uncached = 0

    for track in tracks:
        payload = track.extracted.embedded_artwork
        if not payload:
            earlier = carried.get(track.path)
            if earlier is None:
                empty += 1
                continue
            # Carry the reference only when the file it addresses is still there. A cache that
            # lost its images is #51's install fault, and writing a row for a missing one would
            # turn a loud report line into a 404 on a guest's phone.
            if not (cache_dir / earlier.relative_path).is_file():
                uncached += 1
                empty += 1
                continue
            store.setdefault(earlier.sha256, earlier)
            by_file[track.path] = earlier
            reused += 1
            continue
        payload_digest = hashlib.sha256(payload).hexdigest()
        asset = seen.get(payload_digest)
        if asset is None:
            # The cap bounds *new* images, which is what `--max-artwork N` promises in the
            # Administrator Guide; the references this run carried over are already on disk
            # and cost this build nothing.
            if resolved >= bounds.max_assets:
                continue
            asset, created = _store(payload, cache_dir, limits=bounds)
            if asset is None:
                # The song is still in the library. SAPRS 6.7 is explicit that bad
                # artwork is a missing picture and never a missing track, and `failed`
                # is where the difference between those two sentences gets recorded.
                failed += 1
                continue
            seen[payload_digest] = asset
            # Two keys, for two different questions: the payload digest is the cheap one
            # that spares a 40-track album 40 decodes, and the stored digest is the
            # address, which decides how many images a library holds. The second bullet
            # above is why they are not the same number (#51).
            if store.setdefault(asset.sha256, asset) is asset:
                resolved += 1
                written += int(created)
        by_file[track.path] = asset

    return ArtworkOutcome(
        assets=tuple(store[digest] for digest in sorted(store)),
        by_file=dict(by_file),
        generated=written,
        reused=reused,
        empty=empty,
        failed=failed,
        uncached=uncached,
    )


def store_one(
    payload: bytes,
    cache_dir: Path,
    *,
    kind: str = "album",
    limits: ArtworkLimits | None = None,
    source_path: Path | None = None,
) -> ArtworkAsset | None:
    """Normalize one image, write it if the cache does not have it, return the reference.

    Exposed because construction derives an artist's picture from an album cover
    rather than from a file, and because a test that wants one asset should not
    have to build a track to carry it.
    """

    asset, _created = _store(payload, cache_dir, kind=kind, limits=limits, source_path=source_path)
    return asset


def _store(
    payload: bytes,
    cache_dir: Path,
    *,
    kind: str = "album",
    limits: ArtworkLimits | None = None,
    source_path: Path | None = None,
) -> tuple[ArtworkAsset | None, bool]:
    """`store_one`, and whether the cache file is this call's doing.

    The second answer is the one the report needs: "how many images did this build write"
    is a claim about I/O, and a `--full` rebuild over a cache it already filled writes
    none while resolving every one of them again. `generate` counts both, because the
    difference between them is the difference between a rebuild and a re-download.
    """

    bounds = limits if limits is not None else ArtworkLimits()
    normalized = _normalize(payload, bounds)
    if normalized is None:
        return None, False
    data, width, height = normalized
    digest = hashlib.sha256(data).hexdigest()
    relative = Path(bounds.subdir) / digest[:2] / f"{digest}.jpg"
    asset = ArtworkAsset(
        kind=kind,
        relative_path=relative,
        width=width,
        height=height,
        sha256=digest,
        source_path=source_path,
    )
    destination = cache_dir / relative
    if _already_cached(destination, data):
        return asset, False
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".tmp")
    temporary.write_bytes(data)
    temporary.replace(destination)
    return asset, True


def _normalize(payload: bytes, bounds: ArtworkLimits) -> tuple[bytes, int, int] | None:
    """Decode, resize and re-encode one image. None for anything that will not decode.

    Re-encoding is not vanity. Embedded covers arrive as PNG, as baseline JPEG, as
    a 16-bit TIFF from a scanner and as an Apple `hei` file, and the interface
    cannot ask a guest's phone to support all four; one format, one quality, one
    maximum edge is what makes the reference mean something.
    """

    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - Pillow is a declared dependency (AIG 3)
        return None

    try:
        with Image.open(io.BytesIO(payload)) as opened:
            opened.load()
            width, height = opened.size
            if width < 1 or height < 1:
                return None
            scale = min(1.0, bounds.max_edge / max(width, height))
            working: Any = opened
            if scale < 1.0:
                width, height = max(1, round(width * scale)), max(1, round(height * scale))
                working = opened.resize((width, height))
            flattened = working if working.mode in ("RGB", "L") else working.convert("RGB")
            buffer = io.BytesIO()
            flattened.save(buffer, format="JPEG", quality=bounds.quality, optimize=True)
    except Exception:
        return None
    return buffer.getvalue(), width, height


def _already_cached(destination: Path, data: bytes) -> bool:
    """True when the file is there and is these bytes.

    The content address means a name collision is a content equality, so a size
    check would do — but a truncated write from an interrupted build has the right
    size and the wrong bytes, and the fix is to rewrite it rather than to ship a
    half a cover.
    """

    try:
        return destination.stat().st_size == len(data) and destination.read_bytes() == data
    except OSError:
        return False


def _distinct(tracks: Sequence[Track]) -> int:
    """How many different images a set of tracks carries. Report-only."""

    return len(
        {track.extracted.embedded_artwork for track in tracks if track.extracted.embedded_artwork}
    )
