"""HTTP surface: FastAPI app factory, routers and SSE endpoints.

Reserved for milestone 11 (FastAPI), 12 (HTMX) and 13 (SSE) of AIG Chapter 21.

Guardrails (SAPRS 11.10, AIG 4):

* Translates HTTP into application operations; no business rules live here.
* Never touches SQLite directly — always through a service or repository.
"""
