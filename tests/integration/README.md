# tests/integration

Tests that wire two or more real services together, per SAPRS 14.5:

- Queue and Playback
- Playback and Event Bus
- Event Bus and SSE
- Library and Search
- Runtime database and Queue

mpv stays mocked here too. Integration tests run in the `Test` workflow and in
the PR gate, so keep each one under a second.
