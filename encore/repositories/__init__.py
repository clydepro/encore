"""Persistence adapters for `library.db` (read-only) and `runtime.db` (read-write).

Repositories translate between domain objects and SQLite. They contain no
business rules (AIG 4) and are the only modules permitted to speak SQL.
`library.db` is opened read-only and is never written at runtime.
"""
