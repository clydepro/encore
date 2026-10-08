"""Party Simulation wiring (SAPRS 14.11, 14.12).

The driver that actually simulates guests arrives with milestone 9. What lands
here during bootstrap is the contract the driver will consume: profiles that
parse, agree with the specification and are selected from the command line.
"""

from __future__ import annotations

import pytest

from tests.support.party_profiles import available_profiles, load_profile


def test_every_profile_describes_a_representative_library() -> None:
    for path in available_profiles():
        profile = load_profile(path)
        assert profile.guests > 0, profile.name
        assert profile.duration_minutes > 0, profile.name
        assert profile.library_songs == 15_000, (
            f"{profile.name} must use the ~15,000-song library (SAPRS 14.9)"
        )
        assert profile.duplicates_allowed is True, "duplicates are a v1 rule (SAPRS 8)"


def test_every_profile_declares_client_churn() -> None:
    """SSE reconnects and disconnects are part of every profile (SAPRS 14.13)."""

    for path in available_profiles():
        profile = load_profile(path)
        assert profile.client_disconnects, f"{profile.name} drops client connections"
        assert profile.sse_reconnects, f"{profile.name} reconnects SSE clients"


@pytest.mark.slow
def test_simulated_party() -> None:
    pytest.skip("Party Simulation driver lands in milestone 16 (AIG 21)")
