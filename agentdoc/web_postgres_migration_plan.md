# Web Implementation Assessment & Migration Plan (ArangoDB → PostgreSQL)

Written 2026-08-31. Companion to `agentdoc/current_db_context.md` (pre-migration
ArangoDB survey) and `agentdoc/new_schema.sql` / `etl/src/schema.sql` (the
Postgres schema, already live). Scope: `web/` only. `etl/` is done — this is
the other half.

## 1. Current state

**`etl/` is fully cut over.** `etl/src/main.py` connects with `psycopg2`,
creates `tdm.*` tables from `etl/src/schema.sql`, and every populate stage
(`static.py`, `snmp.py`, `yang/`, `search.py`) writes/reads Postgres. The
`search.py` flatten query (`DATAPATH_QUERY`) already targets the new schema
and feeds Elasticsearch with the same document shape as before
(`dp_key`, `dp_machine_id`, `dp_human_id`, `os_name`, `release_name`,
`dml_name`, ...).

**`web/` has not been touched at all.** Every fact in
`current_db_context.md` is still true of `web/` today:

- `web/src/web/views.py` imports `from arango import ArangoClient`, hardcodes
  `ARANGO_PORT = 8529`, and opens a fresh `ArangoClient` ad hoc in 5 places
  (`map_datapath_calculation_single`, `map_datapath_single_by_key`,
  `check_collection_fields`, `add_mapping`, `query_db`).
- Every query in `views.py` (~30 AQL strings) targets the old vertex/edge
  collection names (`DataPath`, `DataPathMatch`, `DataPathFromDataModel`, ...)
  and Arango's `_key`/`_id`/`DOCUMENT()`/`FULLTEXT()`.
- Templates thread Arango's `_key` through as the routing identifier —
  `datapath.html`, `datapath_direct.html`, `matches.html`, `matchmaker.html`,
  `search.html` all read `datapath['_key']` / `match['_key']` and build URLs
  from it (`url_for('datapath_details', _key=...)`).
- `web/src/Pipfile` still pins `python-arango`; `web/Containerfile` has no
  `libpq-dev`/`psycopg2` (compare `etl/Containerfile`, which added
  `libpq-dev` when it cut over).
- `compose.yaml` still runs a `dbms` (ArangoDB) service alongside `postgres`
  — nothing currently reads it except `web`, so it's the last thing keeping
  ArangoDB in the stack. `nginx/goaccess_dbms.conf` and the `/goaccess_db.html`
  stat page are also Arango-specific and will be dead once `web` cuts over.

**One thing that needs no backend change:** the "deep search" path
(`search_es.html` → `/api/v1/search/es` → `fetch_search_data_paths_es`) talks
to Elasticsearch directly and was never Arango-dependent. Since `search.py`'s
ES field names are unchanged across the migration, this route is already
compatible with the new pipeline as-is. The *other* search
(`search.html` → `/api/v1/search` → `fetch_search_data_paths`) is the one
that runs AQL `FULLTEXT()` directly against the primary DB and is the
heaviest query in the file (documented in `current_db_context.md` as the
original motivation for adding ES at all) — this one needs a full rewrite.

## 2. Inventory: every touch point that needs to change

### `views.py` — query functions (read paths)

Straight-line JOIN translations (already sketched in
`agentdoc/new_schema.sql`'s "QUERY TRANSLATION NOTES" section):

| Current function | AQL pattern | Postgres shape |
|---|---|---|
| `fetch_datapath` | `DOCUMENT()` | `SELECT * FROM data_path WHERE data_path_id = %s` |
| `fetch_datapath_arbitrary_id` | `FILTER human_id==@x OR machine_id==@x` | same `WHERE` clause, no traversal |
| `fetch_datapath_parent` | 1-hop via `DataPathParent` | `WHERE data_path_id = (SELECT parent_id FROM data_path WHERE data_path_id=%s)` |
| `fetch_datapath_children` | 1-hop via `DataPathChild` | `WHERE parent_id = %s` |
| `fetch_datapath_datatype` | 1-hop via `OfDataType` | `JOIN data_type USING (data_type_id) WHERE data_path_id=%s` |
| `fetch_datapath_models` | reverse join via `DataPathFromDataModel` | `JOIN data_path_source ... JOIN data_model` |
| `fetch_datapath_mappings` | bidirectional `_from`/`_to` ternary | `WHERE data_path_a_id=%s OR data_path_b_id=%s` (no ternary needed — join both `data_path` sides and pick whichever isn't `%s`) |
| `fetch_datapath_os_graph` | 3-hop `INBOUND` | fixed 5-table JOIN chain (`data_path → data_path_source → data_model → release_data_model → release → os`) — spelled out in `new_schema.sql` |
| `fetch_datapath_dml_graph` | 2-hop `INBOUND` | `data_path → data_path_source → data_model → data_model_language` |
| `fetch_all_matches` | 3-hop mixed direction incl. `ANY DataPathMatch` | per-DML query joining down to `data_path`, then `LEFT JOIN data_path_match` on either FK column |
| `fetch_matches` | full scan + `FLATTEN` | `WHERE human_id = ANY(%s) OR machine_id = ANY(%s)`, then per-row subquery/join over `data_path_match` both directions |
| `fetch_calculations` / `_as_result` / `_as_factor` | nested subqueries over `InCalculation`/`CalculationResult` | joins over `calculation_input`/`calculation_result` — straightforward, these tables exist 1:1 already |
| `fetch_os_releases` | 2-hop join | `os JOIN release USING (os_id)` |
| `fetch_dmls` | scan | `SELECT name FROM data_model_language ORDER BY name` |
| `fetch_dump_mappings(_native)` | resolve `_from`/`_to` to `machine_id` | join `data_path_match` to `data_path` twice (aliased) |

Two functions need real design decisions, not just translation:

- **`fetch_collection_counts`** — AQL used `COLLECTION_COUNT(collection)`
  with the collection name as a *bound variable*, which AQL allows safely.
  Postgres has no equivalent for parameterized identifiers — table names
  can't be bind params. The request body is client-controlled JSON
  (`flask.request.get_json()`), so building `f"SELECT COUNT(*) FROM {name}"`
  is a SQL-injection hole. **Needs a hardcoded allow-list** mapping the
  three names the frontend actually sends (`"DataPath"`, `"Release"`,
  `"DataModel"`, per `index.html`) to real table names
  (`data_path`, `release`, `data_model`), rejecting anything else.

- **`fetch_search_data_paths`** (`/api/v1/search`, non-ES) — the AQL builds
  a deeply nested `MERGE` tree (OS → Release → DML → DataModel → [DataPath])
  in the database itself. Postgres could replicate that with
  `json_build_object`/`json_agg`, but it's simpler and more maintainable to
  run one flattish SQL query (filtered by OS/release/DML, full-text match
  against `data_path.search_vector`, `is_configurable`/`is_leaf` flags,
  `LIMIT`/`OFFSET` for `start_index`/`max_return_count`) and build the same
  nested dict shape in Python from the row set — matching how
  `fetch_search_data_paths_es` already post-processes ES's response into
  `return_dict` today. Recommend `plainto_tsquery('simple', filter_str)`
  against `search_vector` as the FTS entry point; this is a genuine behavior
  change from Arango's fulltext tokenizer (also flagged in
  `new_schema.sql` point 6) and needs eyeballing against real queries before
  calling it equivalent.

### `views.py` — mutation functions (write paths)

- `map_datapath_single`, `map_datapath_single_by_key`,
  `map_datapath_calculation_single`, `check_collection_fields`,
  `add_mapping` all currently do "find-then-insert" against pyArango/`arango`
  collections with manual existence checks. In Postgres these become
  `INSERT ... ON CONFLICT DO NOTHING/UPDATE` or explicit
  `SELECT ... FOR UPDATE` + `INSERT` inside a transaction.
- **Constraint interaction to get right:** `data_path_match` has
  `CHECK (data_path_a_id < data_path_b_id)` plus
  `UNIQUE (data_path_a_id, data_path_b_id)` — the schema enforces the
  "undirected pair" invariant that the old code emulated manually by
  checking both `(_from,_to)` and `(_to,_from)`. The new insert code must
  sort the two resolved `data_path_id`s ascending before `INSERT`, not
  preserve "base"/"match" or "first"/"second" ordering from the form —
  otherwise every match where the user happens to submit the larger key
  first will violate the CHECK constraint and 500.
- `check_collection_fields`'s "look up by machine_id OR human_id, error if
  ambiguous" logic collapses to one parameterized `SELECT` with a
  `COUNT(*)` guard — no schema-level ambiguity is possible for `machine_id`
  (it's `UNIQUE`) but `human_id` is not unique in the new schema either (nor
  was it in Arango), so the "more than one document" guard still matters and
  needs to be kept.
- `api_map_bulk` has a pre-existing bug worth fixing while this code is
  being rewritten anyway: it does `open(mappings_file.save())` — a Werkzeug
  `FileStorage.save()` returns `None`, not a path, so this line already
  raises `TypeError` before ever reaching the DB. Not a migration blocker,
  but it means the bulk-CSV mapping path is unlikely to be exercised /
  regression-tested today; flag rather than silently port.

### Templates — identifier surface

Decided (§3.1): `_key` is not carried forward. Every place that reads
`datapath['_key']` / `match['_key']` / `dp['_key']` gets renamed to
`data_path_id`. Affected files: `datapath.html` (×4), `datapath_direct.html`,
`matches.html`, `matchmaker.html`, `search.html`, plus the two Flask route
signatures (`/datapath/view/<int:_key>` → `<int:data_path_id>`,
`/datapath/match/<int:_key>` → `<int:data_path_id>`). `map_backup.html`'s
`_from`/`_to` are unrelated — just display column labels for the JSON
failure report shape returned by `api_map_load_native` — those stay whatever
`map_datapath_single`'s exception payload calls them, independent of the DB
layer.

### Dependencies / infra

- `web/src/Pipfile`: drop `python-arango`, add `psycopg2-binary` (pin to the
  same `2.8.6` `etl/src/Pipfile` uses, per the existing "lock it down"
  version-pinning intent in `agentdoc/todo.md`).
- `web/Containerfile`: add `libpq-dev` to the apk install line, matching
  `etl/Containerfile:3`.
- `compose.yaml`: `web` service needs a dependency on `postgres` (currently
  has none on `dbms` either — Flask just retries per-request, but a
  `depends_on` doesn't hurt). Once `web` no longer talks to Arango, the
  `dbms` service + `dbms_storage` volume become dead weight — remove them,
  and drop the `nginx` port-8529 forward (`compose.yaml:84-87` /
  `compose.https.yaml` equivalent) and `goaccess`'s `dbms.conf` stage
  (`nginx/goaccess_dbms.conf`, referenced at `compose.yaml:126`).
- `web/README.md` already lists `python-arango` under Libraries and has a
  stale "(???)" note under Improvements about `_key` vs Machine ID pathing —
  worth resolving both as part of this change rather than leaving stale.

## 3. Decisions (settled 2026-08-31)

### 3.1 `_key` is not carried forward

Rename to `data_path_id` everywhere — routes, templates, JS, JSON payloads.
No aliasing shim. This is a bounded, mechanical diff (route param name + 5
templates) and it happens to close the exact "(???)" ambiguity
`web/README.md` already flags under Improvements about `_key` vs Machine ID
pathing. See §2 "Templates — identifier surface" for the full file list.

### 3.2 Shared Postgres connection helper — build it

`web/` gets its own `db.py`, replacing `query_db()` and the 5 ad hoc
`ArangoClient(...)` blocks with one place to hold credentials and hand out
connections. Use `psycopg2.pool` (a `ThreadedConnectionPool`, not a bare
module-level connection) since Flask/gevent workers see concurrent requests,
unlike the single-threaded ETL. Cursor type: `psycopg2.extras.RealDictCursor`,
so rows come back dict-shaped like pyArango's documents did — minimizes
churn at every call site currently doing `for element in cursor`.
Config shape: same JSON-file pattern `etl/src/config.json` already
established (`{"postgres": {"host": ..., "port": ..., "dbname": ...,
"user": ..., "password": ...}}`), for consistency between the two services
rather than inventing a second config convention — env-var-based config is
still a good idea eventually but is a separate, cross-cutting change
affecting `etl/` too, not scoped to this web rewrite.

### 3.3 Elasticsearch search path — untouched

No change — `search_es.html` / `fetch_search_data_paths_es` stay exactly as
they are; already validated compatible in §1.

### 3.4 ArangoDB removal — deferred to the end

Keep `dbms` in `compose.yaml` (unused) through the whole rewrite, and only
remove `dbms`/`dbms_storage`/port 8529 forwarding/`goaccess_dbms.conf` as
the final cleanup step, after the Postgres-backed `web` is merged and
manually verified end-to-end. This keeps a trivial rollback path
(`git revert` the `web` change) available while validation is in progress,
without having also ripped out the database it'd need to roll back to.
(Moot in this environment specifically — there's no `dbms` container or
volume running here at all right now, see the parity-testing note below —
but the decision still governs what ships to environments where ArangoDB is
still live.)

### 3.5 Parity testing — nothing to reuse, build fresh

Checked for existing coverage before finalizing this plan: there is no test
suite anywhere in the repo (no `test*` files, no CI config for `etl/` or
`web/`), and no live ArangoDB instance to diff against either — the stack
currently running here is `tdm-etl-1` / `tdm-search-1` / `tdm-postgres-1`
only, no `dbms` container, and no `dbms_storage` volume exists to resurrect
one from. Reconstructing a comparison baseline would mean checking out the
pre-migration `etl`/`web` commits and re-running the ~8-hour Arango ETL from
scratch — not worthwhile just to diff responses.

Decision: don't chase old-vs-new diffing. Build a small pytest suite against
the live Postgres data (871k real `data_path` rows already loaded) as part
of this rewrite, asserting each rewritten `views.py` function against
known-good spot checks — e.g. the `bgpLocalAS` / `ifMTU` / `cdpCacheDeviceId`
examples already called out in the top-level `README.md` — and treat that
suite as the new regression baseline going forward, not a parity artifact.
Add this as an explicit sub-step under Phase 2 below.

## 4. Phased plan

1. **Connection & dependency plumbing** — add `web/src/web/db.py`
   (§3.2, pooled `psycopg2` connections + `RealDictCursor`), update
   `web/src/Pipfile` and `web/Containerfile`, no behavior change yet (old
   `views.py` still imports arango — this phase just makes the new plumbing
   buildable/testable in isolation, e.g. a throwaway `SELECT 1` route).
2. **Rewrite read paths** in `views.py` per the table in §2, one function at
   a time, applying the §3.1 `_key` → `data_path_id` rename as each function
   is touched. These are all safe to test individually against the
   already-populated Postgres instance — no data mutation risk.
   **Sub-step (§3.5): write the pytest suite alongside this phase**, one
   test per rewritten function, asserting against known-good `data_path`
   rows (start with the README's `bgpLocalAS`/`ifMTU`/`cdpCacheDeviceId`
   examples, expand as more functions are ported) — this is the new
   regression baseline, not a port of anything that existed before.
3. **Rewrite write paths** (`map_datapath_single*`, `add_mapping`,
   `check_collection_fields`, calculation creation, bulk CSV/native
   import/export) with the ordered-pair constraint handling from §2 baked
   in. Fix the `mappings_file.save()` bug in `api_map_bulk` while touching
   this code. Extend the pytest suite from step 2 to cover these against a
   disposable transaction/rollback per test, since they mutate data.
4. **Template + route param rename** (§3.1) across `datapath.html`,
   `datapath_direct.html`, `matches.html`, `matchmaker.html`, `search.html`,
   and the two `<int:_key>` route signatures. Update `index.html`'s
   `collection-counts` call sites to the §2 allow-list naming.
5. **Cut the cord**: remove the `arango`/`python-arango` import and the
   `ARANGO_PORT` constant from `views.py`; update `web/README.md`'s
   Libraries section and resolve its stale `_key` Improvements note.
6. **Manual validation pass** (this is a UI app — needs a browser, not just
   unit tests): click through every route — index stat counts, direct
   lookup, datapath detail view (parent/children/datatype/mappings/OS+DML
   graphs), matchmaker paste-a-list flow, both search UIs side-by-side
   (structured vs deep/ES) for the same query, calculation views, CSV/native
   mapping export, native mapping import, bulk CSV import.
7. **Infra cleanup** — remove `dbms` service, `dbms_storage` volume, port
   8529 forwarding, `goaccess_dbms.conf`/`/goaccess_db.html` (§3.4), once
   step 6 is signed off.

## 5. Out of scope / explicitly not doing

- No change to the ES-backed deep search — already compatible.
- No change to `forms.py` — it has zero Arango dependency, pure WTForms.
- Not adding the deferred `Device`/`DeviceHasDataPath`/etc. tables — they
  were dead in Arango and stay dead (per `new_schema.sql` §4).
- Not fixing the pre-existing `needs_human` operator-precedence bug in
  `map_datapath_single`/`map_datapath_calculation_single`
  (`needs_human or True if annotation else False` parses as
  `needs_human or (True if annotation else False)`, i.e. `needs_human` is
  never actually `False` when `annotation` is truthy) — real bug, predates
  this migration, orthogonal to it; worth a separate ticket, not bundled in
  here to keep this diff reviewable as "same behavior, new database."
