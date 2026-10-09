"""Free text to FTS5 expression (SAPRS 5.5, AIG 11).

The claim under test is not "the parser is clever" but "SQLite is never handed a
sentence it would refuse", so the adversarial half of these tests binds its own output
to a real FTS5 table. An expression that raises `no such column` or a syntax error
there is a bug here, whatever the unit assertions say.
"""

from __future__ import annotations

import sqlite3

import pytest

from encore.search.query import MAX_TOKENS, MIN_PREFIX_LENGTH, parse

#: Things a guest can type that FTS5's own grammar would reject, or misread.
HOSTILE_INPUTS = (
    "AC/DC",
    "Weird Al' Yankovic",
    '"quoted"',
    "collector's items",
    "AND",
    "OR",
    "NOT",
    "NEAR",
    "title:hello",
    "^boost",
    "-minus",
    "(parenthesised)",
    "star*wildcard",
    "doublequote",
    "one | two",
    "&and",
    "…ellipsis…",
    "日本語",
)


def test_words_become_quoted_prefix_phrases() -> None:
    query = parse("Highway to")

    assert query.tokens == ("highway", "to")
    assert query.match == '"highway"* "to"*'


def test_punctuation_splits_the_way_the_tokenizer_splits() -> None:
    """`unicode61` reads "AC/DC" as two tokens, so the query must too.

    Quoting the whole string as one phrase would find nothing at all, which is the
    silent version of this bug: an empty result page for the band someone drove to the
    party to hear.
    """

    assert parse("AC/DC").tokens == ("ac", "dc")
    assert parse("Weird Al' Yankovic").tokens == ("weird", "al", "yankovic")


def test_repeated_words_are_one_term() -> None:
    """Two of the same word are one condition, not two identical intersections."""

    assert parse("the the The").tokens == ("the",)


def test_case_is_not_part_of_a_term() -> None:
    assert parse("THUNDERSTRUCK").match == parse("thunderstruck").match


def test_a_one_character_word_is_matched_whole() -> None:
    """The index carries `prefix='2 3'`, so a 1-char prefix query is a term scan.

    `MIN_PREFIX_LENGTH` is that fact, and this test is where a change to the Builder's
    `prefix` option has to be noticed.
    """

    assert parse("a song").match == '"a" "song"*'
    assert MIN_PREFIX_LENGTH == 2


def test_a_long_input_becomes_max_tokens_words() -> None:
    """A pasted sentence is not a search; eight words is already an AND no one meant."""

    query = parse(" ".join(f"word{i}" for i in range(200)))

    assert len(query.tokens) == MAX_TOKENS
    assert query.match.count("*") == MAX_TOKENS


def test_blank_input_asks_the_index_nothing() -> None:
    for raw in ("", "   ", "\t\n", '""', "*"):
        query = parse(raw)
        assert query.is_empty, raw
        assert query.match == "", raw


def test_the_original_text_survives_the_parse() -> None:
    """The UI echoes what the guest typed, not what SQLite was given."""

    query = parse("  Light My Fire  ")

    assert query.text == "Light My Fire"
    assert str(query) == "Light My Fire"


def test_exact_mode_drops_the_stem_but_keeps_the_quotes() -> None:
    assert parse("highwa", prefix=False).match == '"highwa"'


@pytest.mark.parametrize("raw", HOSTILE_INPUTS)
def test_nothing_a_guest_can_type_reaches_the_grammar(sqlite_fts5: None, raw: str) -> None:
    """Every parse output is bindable against the real index shape.

    The table below is `apps/builder/schema.py`'s `song_search` in miniature —
    contentless, `unicode61 remove_diacritics 2`, `prefix='2 3'` — because an
    expression that only succeeds against a *contentful* table would prove nothing
    about the traps `queries.py` documents.
    """

    connection = sqlite3.connect(":memory:")
    try:
        connection.execute(
            "CREATE VIRTUAL TABLE song_search USING fts5("
            "title, album, artist, content='', "
            "tokenize='unicode61 remove_diacritics 2', prefix='2 3')"
        )
        connection.execute(
            "INSERT INTO song_search(rowid, title, album, artist) VALUES (1, ?, ?, ?)",
            ("Highway to Hell", "Highway to Hell", "AC/DC"),
        )
        query = parse(raw)
        if query.is_empty:
            return
        rows = connection.execute(
            "SELECT rowid FROM song_search WHERE song_search MATCH ?", (query.match,)
        ).fetchall()
        assert isinstance(rows, list)
    finally:
        connection.close()


def test_the_parse_finds_what_the_index_holds(sqlite_fts5: None) -> None:
    """A positive control: the expression is not merely legal, it matches.

    Without this, the test above passes for a parser that emits `"a" AND NOT "a"`
    against an empty table, which is legal, empty and wrong.
    """

    connection = sqlite3.connect(":memory:")
    try:
        connection.execute(
            "CREATE VIRTUAL TABLE song_search USING fts5("
            "title, album, artist, content='', "
            "tokenize='unicode61 remove_diacritics 2', prefix='2 3')"
        )
        connection.execute(
            "INSERT INTO song_search(rowid, title, album, artist) VALUES"
            " (1, 'Thunderstruck', 'The Razors Edge', 'AC/DC'),"
            " (2, 'Hell Bells', 'Blackball', 'AC/DC'),"
            " (3, 'Strange Brew', 'Beckolace', 'Cream')"
        )
        rowids = {
            int(row[0])
            for row in connection.execute(
                "SELECT rowid FROM song_search WHERE song_search MATCH ?",
                (parse("AC/DC hell").match,),
            )
        }
        assert rowids == {2}, "both terms must hit, by prefix, across columns"
    finally:
        connection.close()
