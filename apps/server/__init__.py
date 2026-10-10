"""The Encore server: the composition root, and the process it runs in.

Two modules, and the split between them is the split AIG 6 asks for.

* `appliance.py` builds the object — both databases, the bus, the services, the one thread
  that owns them (ADR-012). It has no opinion about ports and can be constructed inside a
  test.
* `main.py` is the process: configuration, logging, the signal handlers, uvicorn, exit
  codes. It has no opinion about music.

Nothing else lives here. The routers are in `encore/api/` and `encore/controllers/`, the
rules are in `encore/services/`, and if either of those statements ever stops being true
the reason will be that someone needed a dependency the other direction — which is the
coupling AIG 5's layout exists to prevent.
"""

__all__: list[str] = []
