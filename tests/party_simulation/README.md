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

`test_queue_and_playback.py` runs a **bounded** party today: six minutes of the baseline
profile's rates — guests queueing, an administrator skipping, mpv dying twice — against the
real queue, the real playback state machine and both databases. It asserts accounting and
order rather than latency, because those are the failures a queue can produce at any size:
a request that vanished, a song that played twice, a list that reordered itself during a
restart.

What is missing is scale, not assertions: no HTTP, no SSE, no browsing, and the seven-track
test corpus instead of 15,000 songs. The driver that reads `--party-profile` end to end
arrives with milestone 16 (AIG 21); `test_party_simulation.py` keeps its skip until then, and
the day the corpus grows this directory's job is to raise MINUTES, not to start again.
