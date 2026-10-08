"""Tests for the Party Simulation profile loader (PBK 16, SAPRS 14.12)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.support.party_profiles import (
    PROFILES_DIR,
    InvalidProfileError,
    available_profiles,
    default_profile,
    load_profile,
)


def test_shipped_profiles_all_parse() -> None:
    paths = available_profiles()
    assert paths, "no Party Simulation profiles shipped"
    names = {load_profile(path).name for path in paths}
    assert names == {
        "baseline",
        "house_party",
        "large_party",
        "small_gathering",
        "stress",
    }


def test_guest_counts_match_the_specification() -> None:
    """SAPRS 14.12 requires 5 / 25 / 50 / 100+ profiles."""

    by_name = {load_profile(path).name: load_profile(path) for path in available_profiles()}
    assert by_name["small_gathering"].guests == 5
    assert by_name["house_party"].guests == 25
    assert by_name["large_party"].guests == 50
    assert by_name["stress"].guests >= 100


def test_baseline_profile_matches_saprs_14_11() -> None:
    profile = load_profile(PROFILES_DIR / "baseline.toml")
    assert profile.guests == 40
    assert profile.duration_minutes == 60
    assert profile.library_songs == 15_000
    assert profile.admin_activity is True
    assert profile.guest_minutes == 2_400
    assert profile.queue_adds_per_minute == 80


def test_default_profile_is_loadable_without_options() -> None:
    assert default_profile().name == "baseline"


def test_missing_section_is_rejected(tmp_path: Path) -> None:
    broken = tmp_path / "broken.toml"
    broken.write_text('[profile]\nname = "broken"\n', encoding="utf-8")
    with pytest.raises(InvalidProfileError, match="missing \\[library\\]"):
        load_profile(broken)


def test_wrong_type_is_rejected(tmp_path: Path) -> None:
    broken = tmp_path / "wrong.toml"
    broken.write_text(
        '[profile]\nname = "x"\ndescription = "y"\nguests = "lots"\n\n'
        "[library]\nsongs = 1\n\n"
        "[activity]\nsearches_per_guest_per_minute = 1\nbrowsing_per_guest_per_minute = 1\n"
        "queue_adds_per_guest_per_minute = 1\nduplicates_allowed = true\nadmin_activity = false\n\n"
        "[chaos]\nmpv_terminations = 0\nclient_disconnects = true\nsse_reconnects = true\n",
        encoding="utf-8",
    )
    with pytest.raises(InvalidProfileError, match="expected integer for 'guests'"):
        load_profile(broken)
