# Phase 6 — Manual Validation Pass

## Context to start from

Phases 1–5 are complete: `web/` is entirely Postgres-backed, dead ArangoDB
code and docs are removed, and the pytest suite built in Phases 2–3 passes.
That suite covers individual query/mutation functions, but this is a
server-rendered UI app with inline JS making its own `fetch()` calls
(`datapath.html`, `matchmaker.html`, `search.html`, `map_backup.html`) —
unit-level coverage of `views.py` functions does not guarantee the
templates render correctly, the JS parses the JSON shapes correctly, or the
end-to-end flows (form submit → redirect → rendered result) actually work.
This phase is a human-in-the-loop browser pass to catch what the pytest
suite structurally cannot.

Run this against the stack with `postgres`/`etl`/`search`/`web` all up
(the `dbms` ArangoDB service can stay running or not — it's inert either
way at this point, and its removal is deliberately deferred to Phase 7, not
because this phase depends on it).

## What needs to be accomplished

Click through every route and confirm expected behavior, not just "doesn't
500":

- **Index (`/`)** — stat counts for DataPath/Release/DataModel render and
  are plausible (compare against `SELECT COUNT(*)` on the real tables).
- **Direct lookup (`/datapath/direct`)** — search by a known `machine_id`
  (e.g. an OID or XPath for `bgpLocalAS`/`ifMTU`/`cdpCacheDeviceId` from the
  top-level `README.md`) and by a known `human_id`; confirm the
  single-match redirect and the multi-match disambiguation table both work.
- **DataPath detail view (`/datapath/view/<id>`)** for at least one
  YANG-sourced path and one SNMP-sourced path:
  - Parent/children links resolve and are navigable.
  - Data type is shown correctly.
  - The OS/Release graph (`fetch_datapath_os_graph`) and DML graph
    (`fetch_datapath_dml_graph`) show correct, non-duplicated results.
  - Existing mappings (if any were created during Phase 3 manual testing)
    display correctly.
- **Matchmaker (`/matchmaker`)**:
  - Paste a list of known paths, confirm `/matches` returns correct
    cross-references.
  - Create a new match via the detail-view form; confirm it round-trips
    (appears in a subsequent `/matches` query and in the CSV/native export).
  - Attempt to create a duplicate match (submit the same pair in **both**
    id orders) and confirm both attempts correctly report "mapping already
    exists" rather than one succeeding and the other 500ing — this is the
    Phase 3 ordered-pair constraint check, verified end-to-end through the
    UI this time, not just via pytest.
- **Both search UIs, same query, side by side**:
  - `/search` (structured, direct Postgres full-text via
    `fetch_search_data_paths`) — confirm OS/DML filters, "exclude config",
    and "only leaves" checkboxes all narrow results as expected, and results
    are relevant (this is the path with a genuine tokenizer behavior change
    from Arango's `FULLTEXT()` — sanity-check relevance, not just "returns
    rows").
  - `/search_es` (deep/ES search, untouched by this migration) — confirm it
    still works as a baseline, and spot-compare a couple of queries against
    `/search`'s results for rough agreement.
- **Calculations** (`/calculations`, `/calculations_as_result`,
  `/calculations_as_factor`) — if any calculations exist or were created
  during Phase 3 testing, confirm they display with correct
  factor/result relationships.
- **Mapping export** (`/api/v1/map/dump/csv`, `/api/v1/map/dump/native`) —
  download both, confirm the matches created during this pass appear.
- **Native mapping import** (`/api/v1/map/load/native`, via
  `/map-backup`) — round-trip: export, then re-import the same file,
  confirm it reports the (now-existing) mappings as failures/duplicates
  rather than erroring unexpectedly.
- **Bulk CSV import** (`/api/v1/map/bulk`, via `/map-bulk`) — exercise this
  specifically since Phase 3 fixed a bug (`mappings_file.save()`) that
  likely meant this path has never actually worked; confirm it now does.

## Exit criteria

- Every route above has been exercised in a real browser against real data
  and behaves as described.
- Any discrepancy found gets fixed in the relevant earlier phase's code
  (don't patch it here) and this checklist gets re-run for the affected
  routes.
- Sign-off here is the gate for Phase 7 — don't remove ArangoDB from the
  stack until this phase is clean.
