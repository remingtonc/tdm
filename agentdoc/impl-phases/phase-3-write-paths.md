# Phase 3 — Rewrite Write Paths

## Context to start from

Phases 1–2 are done: `web/src/web/db.py` provides pooled Postgres
connections, and every **read-only** query function in `views.py` has been
ported to SQL and returns `data_path_id` instead of Arango's `_key` (Phase
2's table has the full list). A pytest suite exists covering those read
paths against the live Postgres data.

What's left in `views.py` still talking to ArangoDB is the **mutation**
side: creating `DataPathMatch`/`Calculation` records from the matchmaker UI
and the bulk/native import-export endpoints. These currently do
"find-then-insert" against pyArango/`arango` collections with manual
existence checks (`check_collection_fields`, `add_mapping`) instead of
relying on schema constraints.

## What needs to be accomplished

1. **`map_datapath_single`** (creates a `DataPathMatch`) and
   **`map_datapath_single_by_key`** (same, from the matchmaker form) —
   resolve both endpoints (`machine_id` or `human_id` input) to
   `data_path_id`s via a parameterized `SELECT`, then `INSERT INTO
   data_path_match (...)`.

   **Critical constraint interaction — get this right:**
   `data_path_match` has `CHECK (data_path_a_id < data_path_b_id)` plus
   `UNIQUE (data_path_a_id, data_path_b_id)` (see `etl/src/schema.sql`).
   This schema enforces the "undirected pair" invariant that the old code
   emulated manually by checking both `(_from,_to)` and `(_to,_from)`
   existence. **The new insert code must sort the two resolved
   `data_path_id`s ascending before `INSERT`** — do not preserve
   "base"/"match" or "first"/"second" ordering from the form fields.
   Otherwise, any match where the user happens to submit the numerically
   larger id as the first argument will violate the CHECK constraint and
   the request will 500. Use `INSERT ... ON CONFLICT (data_path_a_id,
   data_path_b_id) DO NOTHING` (or a pre-check `SELECT`) to reproduce the
   old "mapping already exists" error message instead of a raw constraint
   violation reaching the user.

2. **`map_datapath_calculation_single`** (creates a `Calculation` plus its
   `InCalculation`/`CalculationResult` edges) — becomes an `INSERT INTO
   calculation (...) RETURNING calculation_id`, followed by batch `INSERT`s
   into `calculation_input`/`calculation_result` using that id. Keep the
   existing "calculation of this name already exists" pre-check
   (`SELECT 1 FROM calculation WHERE name = %s`) since `calculation.name`
   has no `UNIQUE` constraint in the schema to rely on instead.

3. **`check_collection_fields`** — the "look up by `machine_id` OR
   `human_id`, error if ambiguous" helper collapses to one parameterized
   `SELECT ... WHERE machine_id = %s OR human_id = %s` with a row-count
   guard. Note: `machine_id` is `UNIQUE` in the new schema so it can never
   be ambiguous on its own, but `human_id` is **not** unique (same as it
   wasn't in Arango) — keep the "more than one match" error path, don't
   drop it just because one of the two fields is now constrained.

4. **`add_mapping`** — folds into the `INSERT ... ON CONFLICT` pattern from
   item 1; there's no need to keep a separate generic "insert into edge
   collection with bidirectional existence check" helper once
   `data_path_match`'s own constraints do that enforcement.

5. **`api_map_bulk`** (CSV bulk import) and **`api_map_load_native`** (JSON
   native import) — same resolve-then-insert logic as item 1/2, looped over
   rows, collecting per-row success/failure like the current code does.
   **Fix a pre-existing bug while touching this code**: `api_map_bulk`
   currently does `open(mappings_file.save())` — Werkzeug's
   `FileStorage.save()` returns `None`, not a path, so this line already
   raises `TypeError` before ever reaching the database. This means the
   bulk-CSV path has likely never worked in production; fix it properly
   (`mappings_file.save(some_path)` then `open(some_path)`, or read directly
   from the `FileStorage` stream without saving to disk at all) rather than
   silently porting the bug forward.

6. **`fetch_dump_mappings` / `fetch_dump_mappings_native`** (export) were
   already ported to read-only SQL in Phase 2 — nothing left to do here,
   just confirm the export round-trips correctly against data created by
   the new insert paths (i.e. write a mapping via the matchmaker UI, then
   confirm it appears in a CSV/native export).

### Extend the pytest suite

Add tests for each function above against a **disposable
transaction/rollback per test** (open a transaction, run the insert, assert
the expected row/constraint behavior, roll back) so these tests don't
permanently pollute the 871k-row dataset already loaded by `etl/`. Include
at least one test that deliberately submits a pair in descending id order
to confirm the ordering-before-insert logic in item 1 actually kicks in —
this is the one behavior in this phase most likely to regress silently.

## Exit criteria

- No remaining `views.py` function does manual bidirectional existence
  checking that the schema's constraints already handle.
- A same-pair match submitted in either id order succeeds exactly once and
  is rejected as a duplicate on the second attempt, regardless of argument
  order.
- The `api_map_bulk` file-handling bug is fixed, not ported.
- pytest coverage exists for every write path, using transactional rollback
  so the real dataset stays untouched.
