# Phase 2 — Rewrite Read Paths + Build the Test Suite

## Context to start from

Phase 1 delivered `web/src/web/db.py` (pooled `psycopg2` connections via a
context manager, `RealDictCursor` rows) and swapped the Postgres dependency
into `web/src/Pipfile`/`web/Containerfile`. `views.py` itself is still
untouched — every query in it is still AQL against ArangoDB collections
(`DataPath`, `DataPathMatch`, `DataPathFromDataModel`, etc.), and every
returned row still carries Arango's `_key`/`_id` fields.

This phase ports the **read-only** query functions to SQL against the
`tdm` Postgres schema (`etl/src/schema.sql` is the authoritative DDL —
tables are `data_path`, `data_model`, `os`, `release`,
`data_model_language`, `data_path_source`, `release_data_model`,
`data_path_match`, `calculation`, `calculation_input`,
`calculation_result`; every FK column already has an index). Mutation
functions (matching, calculations, bulk import/export writes) are Phase 3,
not this phase.

**Decision in force while doing this (§3.1 of the main plan doc): rename
`_key` → `data_path_id` in every dict this phase returns.** Do this as each
function is ported, not as a separate pass. Concretely: this phase will
leave the app **temporarily unable to render templates that reference
`datapath['_key']`** (`datapath.html`, `datapath_direct.html`,
`matches.html`, `matchmaker.html`, `search.html`) — that's expected. Phase 4
updates those templates to match. Don't "fix" this by keeping `_key` around
as a compatibility alias; that was explicitly decided against.

## What needs to be accomplished

Port each function below from AQL to SQL. Keep function names and call
signatures stable (route handlers calling them shouldn't need to change in
this phase) — only the query body and the shape of returned dict keys
change.

| Function | Old AQL shape | New SQL shape |
|---|---|---|
| `fetch_datapath` | `DOCUMENT()` lookup | `SELECT * FROM data_path WHERE data_path_id = %s` |
| `fetch_datapath_arbitrary_id` | `FILTER human_id==@x OR machine_id==@x` | same `WHERE` clause, no traversal needed |
| `fetch_datapath_parent` | 1-hop via `DataPathParent` edge | `WHERE data_path_id = (SELECT parent_id FROM data_path WHERE data_path_id = %s)` |
| `fetch_datapath_children` | 1-hop via `DataPathChild` edge | `WHERE parent_id = %s` |
| `fetch_datapath_datatype` | 1-hop via `OfDataType` edge | `JOIN data_type USING (data_type_id) WHERE data_path_id = %s` |
| `fetch_datapath_models` | reverse join via `DataPathFromDataModel` | `JOIN data_path_source ... JOIN data_model` |
| `fetch_datapath_mappings` | bidirectional `_from`/`_to` ternary | `WHERE data_path_a_id = %s OR data_path_b_id = %s`, then in Python (or SQL `CASE`) pick whichever FK isn't the input id |
| `fetch_datapath_os_graph` | 3-hop `INBOUND` traversal | fixed 5-table JOIN: `data_path → data_path_source → data_model → release_data_model → release → os` (depth is always fixed — no recursion needed) |
| `fetch_datapath_dml_graph` | 2-hop `INBOUND` traversal | `data_path → data_path_source → data_model → data_model_language` |
| `fetch_all_matches` | 3-hop mixed direction incl. `ANY DataPathMatch` | per-DML query joining down to `data_path`, then join `data_path_match` on either FK column |
| `fetch_matches` | full collection scan + `FLATTEN` | `WHERE human_id = ANY(%s) OR machine_id = ANY(%s)`, then per-row join over `data_path_match` both directions |
| `fetch_calculations` / `_as_result` / `_as_factor` | nested subqueries over `InCalculation`/`CalculationResult` | joins over `calculation_input`/`calculation_result` — these tables already exist 1:1, straightforward |
| `fetch_os_releases` | 2-hop join | `os JOIN release USING (os_id)` |
| `fetch_dmls` | collection scan | `SELECT name FROM data_model_language ORDER BY name` |
| `fetch_dump_mappings` / `fetch_dump_mappings_native` | resolve `_from`/`_to` to `machine_id` | join `data_path_match` to `data_path` twice (aliased) |

Two functions need more than mechanical translation:

- **`fetch_collection_counts`** (backs `/collection-counts`, called from
  `index.html` with `["DataPath", "Release", "DataModel"]`) — the old AQL
  passed the collection name as a *bound variable* to `COLLECTION_COUNT()`,
  which AQL allows safely. Postgres has no equivalent for parameterized
  identifiers, and the request body is client-controlled JSON
  (`flask.request.get_json()`) — building `f"SELECT COUNT(*) FROM {name}"`
  is a **SQL injection hole**, not just a style issue. Implement a
  hardcoded allow-list dict mapping the exact frontend-sent names to real
  table names (`"DataPath": "data_path"`, `"Release": "release"`,
  `"DataModel": "data_model"`) and reject/ignore anything not in it.

- **`fetch_search_data_paths`** (backs `/api/v1/search`, the *non-ES*
  structured search) — the old AQL built a deeply nested `MERGE` tree
  (OS → Release → DML → DataModel → [DataPath]) inside the database. Don't
  replicate that with nested `json_build_object`/`json_agg` in SQL — it's
  simpler and more maintainable to run one flattish SQL query (filter by
  OS/release/DML, full-text match against `data_path.search_vector` via
  `plainto_tsquery('simple', filter_str)`, apply the `is_configurable`/
  `is_leaf` flags, `LIMIT`/`OFFSET` for `start_index`/`max_return_count`)
  and build the same nested dict shape in Python from the row set —
  matching how `fetch_search_data_paths_es` already post-processes ES's
  response into `return_dict` today. **This is a genuine behavior change**
  from Arango's fulltext tokenizer (different token/prefix semantics) —
  budget time to eyeball real queries against it, not just confirm it
  returns *something*.

Do **not** touch `fetch_search_data_paths_es` / `search_deep_api` — the
ES-backed deep search was never Arango-dependent and needs zero changes
(`etl/src/search.py`'s ES field names are unchanged across the migration).

### Build the pytest suite alongside (not after)

There is no existing test suite or CI in this repo, and no live ArangoDB
instance to diff responses against (checked: the currently running stack is
`postgres`/`etl`/`search` only, no `dbms` container or volume). So this
isn't a parity port — it's new coverage, and it's the regression baseline
going forward. As each function above is ported, add a pytest test for it
against the live Postgres data already loaded by `etl/` (871k `data_path`
rows). Start with known-good fixtures pulled from the top-level
`README.md`'s worked examples (`bgpLocalAS`, `ifMTU`, `cdpCacheDeviceId` —
look up their real `machine_id`/`human_id` values in the DB and hardcode
those as fixtures), and expand coverage as more functions are ported.

## Exit criteria

- Every function in the table above queries Postgres, not Arango.
- Every returned dict uses `data_path_id`, never `_key`.
- `fetch_collection_counts` only ever executes against its allow-listed
  table names, never a client-supplied string.
- A pytest suite exists covering each ported function against real data.
- Templates that reference `_key` are expected to be broken right now —
  confirmed as expected, tracked for Phase 4, not treated as a regression.
