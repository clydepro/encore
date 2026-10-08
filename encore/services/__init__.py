"""Application services: library, queue, health, statistics, SSE publishing, logging.

Each service owns exactly one capability (AIG 7, SAPRS 11.6) and receives its
dependencies through its constructor. Services cooperate through the Event Bus
rather than by direct coupling.
"""
