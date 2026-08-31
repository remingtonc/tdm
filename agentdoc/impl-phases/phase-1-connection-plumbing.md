# Phase 1 — Connection & Dependency Plumbing

## Context to start from

TDM is migrating `web/` (a Flask app) from ArangoDB to PostgreSQL.
`etl/` already made this jump — `etl/src/main.py` connects via `psycopg2`
against a `tdm` schema (see `etl/src/schema.sql` for the live DDL,
`etl/src/config.json` for the connection-config shape), and Postgres is
already populated with real data (871k `data_path` rows at last check).
`web/` has not been touched at all yet: it still imports
`from arango import ArangoClient`, hardcodes `ARANGO_PORT = 8529`, and opens
five independent ad hoc `ArangoClient` connections across
`web/src/web/views.py`. This phase does not touch any of that code — it
only lays the Postgres plumbing next to it so later phases have something
to port into. No user-visible behavior changes in this phase.

Decision already made (don't re-litigate): the connection helper is a
pooled `psycopg2` pool (`ThreadedConnectionPool`), not a bare module-level
connection — Flask/gevent workers see concurrent requests, unlike the
single-threaded ETL. Cursor type is `psycopg2.extras.RealDictCursor` so rows
come back dict-shaped like pyArango's documents did, minimizing churn at
call sites in later phases.

## What needs to be accomplished

1. **`web/src/web/db.py`** (new file):
   - Load connection config from a `web/src/web/config.json`, same shape as
     `etl/src/config.json`'s `"postgres"` block (`host`, `port`, `dbname`,
     `user`, `password`) — mirror the existing convention rather than
     inventing a new one.
   - Initialize a module-level `psycopg2.pool.ThreadedConnectionPool` at
     import time (reasonable min/max connections for a small Flask app —
     e.g. 1–10).
   - Expose a small API other modules will use in Phases 2–3, e.g.
     `get_connection()` / `put_connection(conn)`, or a context-manager
     wrapper that acquires from the pool, yields a `RealDictCursor`, commits
     on success, rolls back on exception, and always returns the connection
     to the pool. A context manager is the safer default — it removes the
     "did every call site remember to release the connection" risk
     entirely.
   - This will become the single place credentials live, replacing the 5
     ad hoc `ArangoClient(hosts=...)` blocks currently scattered through
     `views.py` (`map_datapath_calculation_single`,
     `map_datapath_single_by_key`, `check_collection_fields`,
     `add_mapping`, `query_db`).

2. **`web/src/Pipfile`**:
   - Remove `python-arango = "*"`.
   - Add `psycopg2-binary = "==2.8.6"` — pin to the exact version
     `etl/src/Pipfile` uses, for consistency and because the project's
     `agentdoc/todo.md` already flags unpinned dependencies as a known
     problem to fix, not perpetuate.

3. **`web/Containerfile`**:
   - Add `libpq-dev` to the `apk add` line, matching
     `etl/Containerfile:3` (`apk add --update git gcc libc-dev libxslt-dev
     libpq-dev`) — `psycopg2-binary` doesn't strictly need the dev headers
     since it vendors libpq, but match `etl/`'s pattern for consistency
     unless build testing shows it's unnecessary.

4. **`compose.yaml`**:
   - Add `depends_on: [postgres]` to the `web` service. Not strictly
     required (Flask/the pool can retry), but there's no reason not to.

5. **Prove it works** without touching any real route yet: add one
   throwaway diagnostic endpoint (e.g. `/healthz/db`) that acquires a
   connection via the new `db.py`, runs `SELECT 1`, and returns success —
   just enough to confirm the container builds with the new dependency and
   can actually reach the `postgres` service over the `backend` network.
   Remove this endpoint in Phase 5 once real routes prove connectivity
   anyway (or leave it — it's a harmless, genuinely useful health check to
   keep; call this a judgment call at that point, not a requirement now).

## Exit criteria

- `web` container builds with `psycopg2-binary` installed, no `python-arango`.
- The throwaway health endpoint confirms a live round-trip to Postgres.
- No existing route's behavior has changed — `views.py` is still 100%
  Arango-backed at the end of this phase.
