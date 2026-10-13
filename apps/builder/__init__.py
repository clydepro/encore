"""The Encore builder: the offline library database, and the process that produces it.

Two applications live under `apps/`, and AIG 5 puts them there rather than in `encore/` so that
neither can import the other by accident. This one runs to completion and exits: it scans a music
directory, repairs its metadata against MusicBrainz, and publishes `library.db` (ADR-010). It has
no opinion about playback, and `encore/playback/` must stay unimported here — a guardrail
`tests/unit/test_architecture_guardrails.py` enforces rather than records.

`main.py` is the CLI and the exit codes; `pipeline.py` is the build, in stages that take a
directory in and hand a database out.
"""
