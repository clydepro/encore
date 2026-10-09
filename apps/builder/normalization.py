"""Stage 3 — normalization: the rules, applied once, in their own stage.

SAPRS 6.5 lists what normalization must address; ADR-010 fixes how, and why it is
a stage rather than something done while reading: extraction is per-format, so a
rule applied there would be re-derived in every reader.

The rules, in the order the module applies them:

* **Whitespace and case.** Values are collapsed and stripped for display and
  casefolded into `normalized_*` for comparison. The original bytes stay on the
  record — 6.5 says originals remain available "where appropriate", and ADR-010
  makes that a requirement of the provenance table rather than a courtesy.
* **Placeholders.** `Various Artists`, `Unknown`, `No Artist` and their many
  spellings are *not* artist names. They are the absence of one, written down by
  whoever ripped the CD, and treating them as names is how a library grows a 400
  track artist called "Unknown".
* **Tool prefixes.** `"AlbumWrap - Kenny Chesney"` is AlbumWrap's watermark, not a
  performer. The corpus contains a 53-minute file tagged exactly that.
* **Promo markers.** `No Stranger To Shame-ADVANCE` is a promotion suffix on a real
  album title; `[Clean_Version]` is a variant marker. Both are removed, and the
  original kept, because the album is the same album.
* **Positions.** `5`, `5/12`, `05` and `5 of 12` are the same track number.
* **Album grouping.** Keyed on album artist where present, else primary artist —
  which ADR-010 needs because `albumartist` is on only 19% of the corpus, and
  6.4's "where available" cannot be a requirement.
* **Sort keys.** Casefolded, with a leading article moved to the end
  ("The Beatles" → "Beatles, The"), which is the convention every jukebox before
  this one used and the one a guest scanning an A-list expects.

And the rule that is not a text transformation but a decision about where text
comes from — ADR-010's four-level precedence — is implemented in
`apps.builder.precedence`, because it needs the filesystem layout as an input and
this module should not.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

__all__ = [
    "NORMALIZATION_RULES_VERSION",
    "Normalized",
    "collapse",
    "fold",
    "is_placeholder",
    "parse_position",
    "sort_key",
    "strip_promo_markers",
    "strip_tool_prefix",
]

#: Bumped whenever a rule below changes meaning. ADR-010 puts this in the
#: incremental cache key so that fixing a rule invalidates every entry instead of
#: silently reusing a conclusion the old rule reached: "a normalization fix is a
#: full rebuild" is the cost that sentence accepts, and this constant is where it
#: is paid.
NORMALIZATION_RULES_VERSION: Final = 1

_WHITESPACE: Final = re.compile(r"\s+")
_CONTROL: Final = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

#: Artists that mean "no artist". Matched on the folded value, so `Various
#: Artists`, `VARIOUS`, `various artists ` and `Various-Artists` all land here.
_PLACEHOLDER_ARTISTS: Final[frozenset[str]] = frozenset(
    {
        "various",
        "various artists",
        "various artist",
        "va",
        "v a",
        "unknown",
        "unknown artist",
        "unknown artists",
        "no artist",
        "artist unknown",
        "n a",
        "na",
        "none",
        "null",
        "-1",
        "undefined",
        "not applicable",
    }
)

#: The same idea for releases. `[Unknown Album]` in brackets is the Winamp/iTunes
#: convention and appears in rips of rips.
_PLACEHOLDER_ALBUMS: Final[frozenset[str]] = frozenset(
    {
        "unknown",
        "unknown album",
        "no album",
        "album",
        "nonalbum",
        "non album",
        "unreleased",
        "undefined",
        "null",
        "none",
    }
)

#: Tools that append their own name to an artist or album tag. Matched as a
#: prefix because that is how they write it: `"AlbumWrap - Kenny Chesney"`.
_TOOL_PREFIXES: Final[tuple[str, ...]] = (
    "albumwrap",
    "album art wrapper",
    "artbook",
    " Mp3tag",
    "kwalbumart",
    "tag&rename",
)

#: Promotional and variant suffixes, matched wherever they appear. `-ADVANCE` and
#: `-ANTICIPATION` mark pre-release promo discs; `[Clean_Version]` and
#: `(Explicit)` mark a variant of the same recording. `-ADV` is included because
#: the corpus spells it both ways.
_PROMO_MARKERS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"[-\s]\b(ADVANCE|ADV|ANTICIPATION|PROMO|PROOF)\b\s*$", re.IGNORECASE),
    re.compile(
        r"[\s_-]?[\[(](Clean_Version|Clean|Explicit|Edited|Radio\s*Edit|MIX)[\])]", re.IGNORECASE
    ),
)

_ARTICLE_PREFIX: Final = re.compile(r"^(The|A|An|Le|La|Les|El|Der|Die|Das)\s+", re.IGNORECASE)
_TRACK_PART: Final = re.compile(r"^\s*(\d{1,4})\s*(?:/|of)\s*(\d{1,4})\s*$", re.IGNORECASE)
_LEADING_NUMBER: Final = re.compile(r"^\s*(\d{1,4})\s*[._-]\s*")
#: Everything that is not a letter, a digit, whitespace or one of the two marks a
#: title genuinely needs: `&` because it joins names ("Ritchie Valens & The Daltons")
#: and `'` because it is inside them ("O'Byrne"). Every other punctuation mark is a
#: separator wearing a costume, and the corpus proves it — the same album appears as
#: "Art of the Album", "Art of the Album [Experiment]" and "(Art of the Album)".
_PUNCTUATION: Final = re.compile(r"[^\w\s&']", re.UNICODE)


@dataclass(frozen=True, slots=True, kw_only=True)
class Normalized:
    """One cleaned value: what to store, and whether it changed.

    `changed` exists because ADR-010's provenance records a value as `repaired`
    when a rule corrected it. Returning a bool alongside the text keeps that fact
    from being re-derived by comparing strings that may differ only in case.
    """

    value: str = ""
    changed: bool = False

    @property
    def present(self) -> bool:
        return bool(self.value)


def collapse(value: str | None) -> str:
    """Strip, fold runs of whitespace, and remove control characters.

    Applied to every value before anything else, because a tag written by a
    Windows tool carries `\r\n` inside a field and a comparison that does not know
    that says two identical artists are two artists.
    """

    if not value:
        return ""
    cleaned = _CONTROL.sub(" ", value)
    return _WHITESPACE.sub(" ", cleaned).strip()


def fold(value: str | None) -> str:
    """The comparison form of a value: casefolded, punctuation-light, NFKD.

    Casefold rather than lowercase, because `STRASSE` and `Strasse` are the same
    artist and `casefold()` knows it. Diacritics are kept — `Björk` should not
    compare equal to `Bjork` in a way that merges two artists, but it should match
    a search for either spelling, which is what the FTS tokenizer is for.
    """

    text = unicodedata.normalize("NFKC", collapse(value))
    folded = text.casefold()
    return _WHITESPACE.sub(" ", _PUNCTUATION.sub(" ", folded)).strip()


def is_placeholder(value: str | None, *, kind: str = "artist") -> bool:
    """True when a present value means absence.

    Args:
        value: The tag as written.
        kind: `"artist"` or `"album"`; the lists differ because "Album" is a
            plausible placeholder title and an implausible artist name.
    """

    folded = fold(value)
    if not folded:
        return True
    stripped = folded.strip("[] ().-_'")
    if kind == "album":
        return stripped in _PLACEHOLDER_ALBUMS or stripped.startswith("[unknown")
    return stripped in _PLACEHOLDER_ARTISTS


def sort_key(value: str | None) -> str:
    """The value a list orders on.

    Folds, then moves a leading article to the end, so "The Beatles" sorts under
    B. Only the article moves: a name with a comma already in it is left as the
    artist wrote it, because inventing an inversion would reorder someone's name.
    """

    text = collapse(value)
    if not text:
        return ""
    match = _ARTICLE_PREFIX.match(text)
    folded = text.casefold()
    if match:
        remainder = text[match.end() :].strip()
        if remainder:
            folded = f"{remainder}, {match.group(1)}".casefold()
    return folded


def strip_tool_prefix(value: str | None) -> Normalized:
    """Remove a tagging tool's watermark from an artist or album name.

    `"AlbumWrap - Kenny Chesney"` becomes `"Kenny Chesney"`, marked changed.
    The prefix is only removed when something follows it: a tag whose entire
    content is the tool name is not evidence of an artist, and inventing one from
    it would file a track under a piece of software.
    """

    text = collapse(value)
    if not text:
        return Normalized(value="")
    lowered = text.casefold()
    for prefix in _TOOL_PREFIXES:
        candidate = prefix.casefold()
        if not lowered.startswith(candidate):
            continue
        remainder = text[len(candidate) :].lstrip(" -_:")
        if collapse(remainder):
            return Normalized(value=collapse(remainder), changed=True)
    return Normalized(value=text)


def strip_promo_markers(value: str | None) -> Normalized:
    """Remove promo and variant suffixes, retaining the original on the record.

    Returns the cleaned display value. The original is kept by the caller in the
    provenance table (SAPRS 6.5), which is the only place `No Stranger To
    Shame-ADVANCE` still exists after this function runs.
    """

    text = collapse(value)
    if not text:
        return Normalized(value="")
    cleaned = text
    for pattern in _PROMO_MARKERS:
        cleaned = pattern.sub("", cleaned)
    cleaned = collapse(cleaned)
    if not cleaned:
        # Everything was a marker: "Promo" alone, or "[Clean]". Keeping the
        # original is the lesser loss, since a value that displays as "" cannot
        # even be recognised as the album it was.
        return Normalized(value=text, changed=False)
    return Normalized(value=cleaned, changed=cleaned.casefold() != text.casefold())


def parse_position(value: str | None) -> tuple[int | None, int | None]:
    """`(number, total)` from `5`, `5/12`, `05`, `5 of 12`, or None.

    Track and disc numbering are canonicalised across those three spellings by
    SAPRS 6.5's explicit list. The total is returned and not stored: it tells the
    caller `3/12` is track 3 of a 12-track album, which construction uses to
    decide a file is a single rather than a mis-numbered album track.
    """

    text = collapse(value)
    if not text:
        return None, None
    match = _TRACK_PART.match(text)
    if match:
        return _positive(match.group(1)), _positive(match.group(2))
    leading = _LEADING_NUMBER.match(text)
    digits = _WHITESPACE.sub("", text.split("-")[0])
    if digits.isdigit():
        return _positive(digits), None
    if leading:
        return _positive(leading.group(1)), None
    return None, None


def _positive(text: str) -> int | None:
    try:
        number = int(text)
    except ValueError:
        return None
    return number if number > 0 else None


def any_placeholder(values: Iterable[str | None], *, kind: str = "artist") -> bool:
    """True when every value in `values` is absent or a placeholder.

    Used by the artist-tree check: an album directory whose files all say
    "Various Artists" is a compilation even if one of them also says something
    else, and the distinction decides whether a path hint may be trusted.
    """

    return all(is_placeholder(value, kind=kind) for value in values)
