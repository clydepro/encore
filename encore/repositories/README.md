# encore/repositories

Persistence, split by **mutability** rather than by table, because the two
databases have different owners and different guarantees (ADR-009, which supersedes
part of ADR-003).

| Side | Engine | Opened by | Writes? |
| ---- | ------ | --------- | ------- |
| `library/` | raw `sqlite3`, `mode=ro` URI + `PRAGMA query_only=ON` | the Server | never |
| `runtime/` | SQLAlchemy 2.x, WAL, transactional sessions | the Server | always |
| — | `apps/builder/` owns the `library.db` DDL and is its only writer | the Builder | once, offline |

## Layout

- `contract.py` — every table and column of both schemas, named once. This file is
  what keeps the Builder's DDL and the Server's reads from drifting; if you add a
  column, add it here first and both sides fail until they agree.
- `errors.py` — the three failures worth distinguishing: a store that is missing, a
  store that is the wrong shape, and a row that cannot be read.
- `coercion.py` — SQLite's type world → the domain's, in one place. Values that do
  not survive the round trip raise here rather than in a mapper.
- `library/` — `connection.py` (the only opener), `queries.py` (all the SQL, nothing
  else), `mappers.py`, then `songs.py`/`catalogue.py`/`media.py`/`search.py`,
  composed by `store.py`.
- `runtime/` — `models.py` (ORM), `types.py` (the only datetime column type, so a
  naive timestamp cannot be stored), `migrations.py` (numbered, forward-only,
  applied on open), `session.py`, then the queue/history/statistics/admin-state
  repositories, composed by `store.py`.

## Rules that bind this package

- No business rules. A repository answers a question about rows or writes one; a
  `if`/`for` here that is about mapping is fine, and one about jukebox policy is a
  bug that belongs in `encore/services/`.
- No FastAPI, no HTMX, no mpv. Checked by
  `tests/unit/test_architecture_guardrails.py`.
- Domain identifiers in, domain objects out. A `Session`, a `Row` or an integer
  primary key never crosses the boundary.
- Nothing here writes `library.db` — not with a repository, not with a migration,
  not "just this once" (SAPRS 5.1, ADR-010).
- `tests/integration/test_library_contract.py` executes every statement in
  `library/queries.py` against a database the real Builder built. If it stops
  passing, the schemas have diverged; fix the schema, not the test.
