"""Immutable event types and the in-process Event Bus (SAPRS 11.1, AIG 8).

Events are facts that already happened: typed, timestamped and immutable.
Services publish here instead of calling one another; one failing subscriber
must never prevent unrelated subscribers from receiving an event.
"""
