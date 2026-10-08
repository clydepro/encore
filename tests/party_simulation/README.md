# tests/party_simulation

The suite required by SAPRS 14.11: a realistic party, replayed automatically.

Baseline profile (SAPRS 14.11):

- 40 simultaneous simulated guests
- 15,000-song library
- continuous searching, artist/album browsing, song queueing
- playback progression and SSE updates
- administrative activity in the background
- one hour of wall-clock duration

## Load profiles

`profiles/*.toml` holds the standard profiles from SAPRS 14.12. A profile is
test data, not appliance configuration — nothing in `apps/` or `encore/` reads
it.

| Profile           | Guests | Intent                |
| ----------------- | ------ | --------------------- |
| `small_gathering` | 5      | smoke, nightly        |
| `house_party`     | 25     | default working set   |
| `large_party`     | 50     | weekly                |
| `stress`          | 100+   | pre-release           |
| `baseline`        | 40     | SAPRS 14.11 reference |

Run a specific profile:

```bash
pytest tests/party_simulation --run-slow \
  --party-profile tests/party_simulation/profiles/stress.toml
```

## Status

The driver arrives with milestone 16 (Party Simulation) in AIG Chapter 21. The
bootstrap phase ships the profiles, the profile loader and the skip-marked
placeholders so that the wiring is testable from day one.
