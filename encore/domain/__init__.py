"""Pure domain model: entities, value objects, events-as-facts and rules.

This package is the innermost layer of the architecture (SAPRS 15.2). It must
not import FastAPI, Jinja, SQLite bindings, mpv bindings or any web framework.
Only the standard library and Pydantic/dataclass primitives belong here.
"""
