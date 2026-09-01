# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

TDM (Telemetry Data Mapper) maps network telemetry data identifiers (SNMP OIDs, YANG XPaths, etc.) to each other across OS/release/data-model combinations, so operators can tell that e.g. SNMP `bgpLocalAS` and the YANG xpath `.../global-process-info/global/local-as` mean the same thing. It's a Flask web app + a batch Python ETL, backed by PostgreSQL as source of truth and Elasticsearch as a denormalized search cache, all deployed as Podman containers behind NGINX.

## Repo layout

- `web/` — Flask app (API + server-rendered UI). `web/src/web/` is the package; `web/src/runserver.py` is the entrypoint.
- `etl/` — batch ETL: parses Cisco MIBs (via `pysmi`) and YANG models (via `pyang`) and loads them into PostgreSQL, then flattens into Elasticsearch. `etl/src/main.py` is the entrypoint; `etl/src/schema.sql` is the schema DDL executed once on first run.
- `doc/` — VuePress documentation site (source of truth for architecture docs, read these before making structural changes).
- `nginx/` — reverse proxy config (dev HTTP + prod HTTPS) and Goaccess log analytics config.
- `migrate/1_to_2/` — a prior (unrelated) schema migration; not part of the current ArangoDB→Postgres work.
- `agentdoc/` — working notes with AI.
- `compose.yaml` / `compose.https.yaml` — the full Podman Compose stack (postgres, etl, web, nginx, goaccess, search/Elasticsearch, kibana, doc).

## Commands

Always use `podman`/`podman compose`, avoid `docker`/`docker-compose`.

### Running the stack
```bash
./setup.sh              # install podman compose for your user (one-time)
./start.sh [http|https] # build and start the full stack (default: http)
./stop.sh [http|https]  # stop containers
./reset.sh [http|https] # stop AND delete persisted volumes (destructive)
```
ETL takes ~8 hours to fully populate on first run; watch progress with `podman logs -f tdm_etl_1`. Web is only meaningfully useful once ETL has loaded data.

HTTPS mode requires `nginx/tdm.cisco.com.crt` and `nginx/tdm.cisco.com.key` to be placed manually (see `nginx/README.md`).

### Web app — tests
```bash
cd web/src
pipenv install --dev   # first time
pipenv run pytest                          # full suite
pipenv run pytest tests/test_views.py -k test_fetch_datapath_found  # single test
```
Tests require a live PostgreSQL reachable per `web/src/config.json` (default host `postgres:5432`, i.e. inside the compose network — run tests inside the `web` container, or point `config.json`/override the pooled connection at the host-published `localhost:5432` if running outside Podman). `tests/conftest.py`'s `db_rollback` fixture routes `db.cursor()` through one connection and rolls it back after each test, so write-path tests can insert against the real dataset without persisting changes.

### Web app — dev server
```bash
cd web/src
FLASK_ENV=development pipenv run python runserver.py   # debug, auto-reload, port 80
```
Without `FLASK_ENV=development`, `runserver.py` always serves via `gevent`'s production WSGI server.

### ETL
```bash
cd etl/src
pipenv install
pipenv run python main.py
```
Runs the full extract → transform → load → Elasticsearch-flatten pipeline sequentially; it's a one-shot batch process, not a service. `etl/src/schema.sql` is applied automatically on first connect if the `tdm` schema doesn't exist yet. `etl/cache/{extract,transform}` are bind-mounted for inspecting intermediate output.

### Docs site
```bash
cd doc && ./standalone.sh   # VuePress dev server on localhost:8089
```

Both `web/src` and `etl/src` use `Pipfile`s pinned to Python 3.6 (matches the Alpine-based Containerfiles) — don't assume a newer interpreter is available inside containers.

## Architecture

**Data flow:** ETL parses MIBs/YANG models from source repos → writes normalized relational data into PostgreSQL (`tdm` schema) → a separate ETL stage flattens PostgreSQL data into denormalized documents pushed into Elasticsearch for the "deep search" UI. Elasticsearch is a cache, never a source of truth, and is **not** kept live-synced — it only reflects PostgreSQL as of the last ETL run, so writes made through the web UI (new mappings, etc.) won't show up in Elasticsearch search until ETL reruns. The primary structured search API (`/api/v1/search`) instead queries PostgreSQL directly via a generated, weighted `tsvector`/GIN column on `data_path`, independent of Elasticsearch.

**Database (PostgreSQL, schema `tdm`, not `public`):** entities are tables with `BIGINT GENERATED ALWAYS AS IDENTITY` surrogate keys; relationships are FK columns or two-column join tables. Core entity chain: `os → release → release_data_model (join) → data_model → data_path_source (join) → data_path`, with `data_model_language`, `control_protocol`, `transport_protocol`, `encoding`, and `data_type` as supporting/lookup entities, plus `data_path_match` (undirected equivalence between two DataPaths, enforced via `CHECK (a_id < b_id)`) and `calculation`/`calculation_input`/`calculation_result` for derived DataPaths. Every FK and join table has an explicit index (Postgres doesn't auto-index FKs the way ArangoDB auto-indexed edges). Full schema, every column's meaning, and worked example queries live in `doc/docs/dev/architecture/Database.md` — read it before writing new queries against this schema rather than reverse-engineering `schema.sql` from scratch.

**Web (`web/src/web/`):** Flask app, no ORM — plain SQL via `psycopg2`. `web/db.py` centralizes connection handling in one pooled `ThreadedConnectionPool` (`options='-c search_path=tdm'`, so queries elsewhere use unqualified table names), exposing a `cursor()` contextmanager that commits on success / rolls back on exception. All routes and query-building logic live in `web/views.py` (there's no separation yet between HTML view logic and `/api/v1/*` API logic — both are intermixed in the same file, flagged as a known refactor need). Templates are Jinja2 in `web/templates/`, with vanilla JS embedded per-template rather than a separate frontend build.

**ETL (`etl/src/`):** sequential, single-threaded, no ETL framework — `main.py` runs each extract/transform/load stage as plain function calls. `models.py` handles schema creation; `snmp.py`/`yang/` handle the two source formats; `search.py` handles the final Elasticsearch-flattening stage (a single streamed query, `DATAPATH_QUERY`, read through a server-side cursor since result sets run into the millions of rows). ETL connects to Postgres with one blocking `psycopg2.connect()` (retried until Postgres is up) rather than pooling, since it's a one-shot batch job.

**Search (Elasticsearch):** custom index/analyzer setup (synonym expansion like `intf, interface, if, int`; a path-aware tokenizer splitting on `. - / :`) documented with example queries in `doc/docs/dev/architecture/Search.md`. One document per DataPath × OS/Release permutation — deliberately denormalized/duplicated for search relevance, not storage efficiency.

For anything touching the schema, query patterns, or the ArangoDB legacy, `doc/docs/dev/architecture/{Database,ETL,Web,Search}.md` are kept current and are more authoritative than reverse-engineering from code — read the relevant one first.
