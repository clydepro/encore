"""Free text to an FTS5 `MATCH` expression (SAPRS 5.5, 9.6, AIG 11).

The library repository takes a *match expression*, not a string a guest typed:
`queries.SONG_SEARCH` binds `?` straight into `MATCH`, and FTS5 reads that argument
as syntax. Handing it `"AC/DC: in * doubt"` would raise a syntax error, and handing
it `OR` would raise one too — the failure this module exists to make impossible.

The rule it applies is the narrow one that keeps the feature honest: **nothing of the
input reaches SQLite**. The text is split into word runs, every run is re-emitted as a
quoted phrase, and the punctuation the tokenizer would have discarded anyway falls
out. `unicode61 remove_diacritics 2` (see `apps/builder/schema.py`) splits on
non-alphanumerics, so splitting the same way on this side mirrors what the index
actually contains.

The two halves of that sentence are why the tests here assert the *expression*, not
merely that a search did not crash.

Two limits, both measured rather than guessed:

* `MIN_PREFIX_LENGTH` — the index carries `prefix='2 3'`, so a prefix query on a
  one-character token is a scan of the term dictionary for no benefit. One-character
  tokens are matched exactly instead.
* `MAX_TOKENS` — a pasted sentence is not a search. Eight words is already
  `"a" "b" … "h"` ANDed; beyond that the answer is "nothing" whatever it matches.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

__all__ = ["MAX_TOKENS", "MIN_PREFIX_LENGTH", "SearchQuery", "parse"]

#: Longest prefix the Builder's `prefix='2 3'` index can serve cheaply, and the
#: shortest token worth a prefix query at all.
MIN_PREFIX_LENGTH: Final = 2

#: Tokens kept from the input. See the module docstring.
MAX_TOKENS: Final = 8

#: Word runs, Unicode-aware. Underscore is excluded because `unicode61` treats it as
#: a separator and matching the index is the whole point.
_WORD = re.compile(r"[^\W_]+", re.UNICODE)


@dataclass(frozen=True, slots=True, kw_only=True)
class SearchQuery:
    """One parsed search: what the guest typed, and what SQLite is given.

    Attributes:
        text: The original input, trimmed. Echoed back into the results so a UI can
            show what was searched rather than what was parsed.
        tokens: The words that survived parsing, in order, case-folded.
        match: The FTS5 expression. Empty when there are no tokens — callers must not
            pass that to SQLite, because an empty match expression is a syntax error
            and "no words typed" is not an error.
        prefix: Whether token starts match (`"highwa"` finding *Highway to Hell*),
            which is what SAPRS 5.5 means by "partial and token-based matching".

    `match` is a *derived* value kept on the object rather than recomputed per index:
    three searches on one word must agree on what the word meant, and a UI that shows
    the query alongside the results should show the same parse.
    """

    text: str
    tokens: tuple[str, ...] = ()
    match: str = ""
    prefix: bool = True

    @property
    def is_empty(self) -> bool:
        """True when nothing was searched, which is not the same as no matches."""

        return not self.tokens

    def __str__(self) -> str:
        return self.text


def parse(raw: str, *, prefix: bool = True) -> SearchQuery:
    """Compile `raw` into a `SearchQuery` that is safe to bind to `MATCH`.

    Args:
        raw: Guest input. Anything; this function does not raise.
        prefix: Turn prefix matching off to require whole words. The service leaves it
            on; a "match exactly" checkbox is where a caller would pass False.

    Returns:
        A query whose `match` is either empty or a conjunction of quoted phrases.
    """

    tokens = _tokens(raw)
    if not tokens:
        return SearchQuery(text=raw.strip(), prefix=prefix)
    return SearchQuery(
        text=raw.strip(),
        tokens=tokens,
        match=" ".join(_phrase(token, prefix=prefix) for token in tokens),
        prefix=prefix,
    )


def _tokens(raw: str) -> tuple[str, ...]:
    """Word runs, lower-cased, de-duplicated in order, capped at `MAX_TOKENS`.

    De-duplication is not cosmetic: `"the the"` is one term to a party guest and two
    ANDed clauses to FTS5, and the second costs another rowset intersection for an
    answer identical to the first.
    """

    seen: dict[str, None] = {}
    for word in _WORD.findall(raw.casefold()):
        seen.setdefault(word, None)
        if len(seen) >= MAX_TOKENS:
            break
    return tuple(seen)


def _phrase(token: str, *, prefix: bool) -> str:
    """One token as an FTS5 phrase.

    Quoting is what makes this safe — the grammar's operators are all outside `"…"` —
    and a quoted phrase cannot contain a `"` because `_tokens` removed every character
    that is not a letter or a digit. The doubling below is therefore unreachable for
    parsed input and stays because a `SearchQuery` built by hand should still work.
    """

    quoted = token.replace('"', '""')
    if prefix and len(quoted) >= MIN_PREFIX_LENGTH:
        return f'"{quoted}"*'
    return f'"{quoted}"'
