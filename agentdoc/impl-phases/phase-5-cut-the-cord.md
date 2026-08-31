# Phase 5 — Cut the Cord (Remove Dead ArangoDB References)

## Context to start from

Phases 1–4 are done: `web/` is fully functional against Postgres — every
query is SQL, every identifier is `data_path_id`, templates and routes are
aligned, and the app renders end-to-end. The `from arango import
ArangoClient` import and the `ARANGO_PORT = 8529` constant at the top of
`views.py` are now dead code — nothing calls them anymore, they're just
left over from before Phase 2–3 replaced their call sites. This phase is a
pure cleanup pass with no functional change, making the dead-code removal
explicit rather than leaving it to rot.

## What needs to be accomplished

1. **`web/src/web/views.py`**:
   - Remove `from arango import ArangoClient`.
   - Remove `ARANGO_PORT = 8529`.
   - Grep the file for any other stray Arango references (`_id`, `_from`,
     `_to` used as Arango edge fields rather than legitimate variable names,
     leftover comments mentioning AQL/collections) and clean them up.

2. **`web/README.md`**:
   - Under "Libraries", replace the `python-arango` entry with
     `psycopg2` (link: https://www.psycopg.org/).
   - Under "Improvements", the existing stale note — *"DataPath view should
     rely on the Machine ID not the `_key`. `_key` will vary per ETL.
     (???)"* — is now resolved by the Phase 4 rename to `data_path_id`
     (note: `data_path_id` is a Postgres surrogate key, not derived from
     `machine_id` either, so the underlying concern — that the identifier
     in the URL isn't the stable, human-meaningful one — is only partially
     addressed by the rename itself; decide whether to close this note
     outright or rephrase it to reflect that `machine_id`-based routing is
     still a real, separate future improvement rather than something this
     migration solved).

3. Do a final check that nothing in `web/src/Pipfile` or
   `web/Containerfile` still references Arango (should already be clean
   from Phase 1, but confirm rather than assume).

## Exit criteria

- `grep -ri arango web/src/` returns nothing except historical mentions in
  `web/README.md` that have been deliberately updated (not just left as
  dangling references).
- The app's behavior is unchanged from the end of Phase 4 — this phase adds
  no new functionality and fixes no bugs, it only removes dead code and
  stale docs.
