"""Search over artists, albums and songs using SQLite FTS5.

Target: sub-100 ms on a 15,000-song library (SAPRS 1.8, AIG 11). Search reads
through the library repositories and publishes results as events; it does not
own queue or playback behaviour.
"""
