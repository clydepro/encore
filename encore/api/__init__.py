"""The HTTP face of Encore: an app factory, and the interfaces it serves.

Layering, stated here because it is the first thing a reader of this directory needs:

* `app.py` assembles a FastAPI application around a composed appliance. It decides which
  routers exist and what happens to an exception; it decides nothing about music.
* `deps.py` is the seam between a request and the appliance thread (ADR-012). `read()` is
  the only way a route reaches a service.
* `v1.py` is the versioned JSON interface SAPRS 10.2 asks for, and `sse.py` the event
  stream. `encore/controllers/` holds the human one.
* `views.py` reads the appliance once and `rows.py` labels what it found. Every interface
  in this package is built from those two, which is how a fragment, a JSON response and a
  snapshot frame can be trusted to describe one queue rather than three.
* `schemas.py`, `html.py`, `fragments.py` and `errors.py` are the shapes, the templates,
  the SSE-to-region bridge, and the one place a failure becomes a status code.

Nothing in here is a domain module. The guardrail is the import test in
`tests/unit/test_architecture_guardrails.py`; the reason is SAPRS 4.8 — a service must not
need a request to be testable, and this package is the half that does.

The submodules are imported by path, not re-exported here, because `app.py` imports the
routers: an eager import in this file would make `import encore.api.v1` run the app
factory that imports `encore.api.v1`.
"""

__all__: list[str] = []
