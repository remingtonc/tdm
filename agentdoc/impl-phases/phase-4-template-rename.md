# Phase 4 — Template + Route Param Rename

## Context to start from

Phases 1–3 finished the Python side: every `views.py` query (read and
write) now targets Postgres, and every dict/row flowing out of those
functions uses `data_path_id` instead of Arango's `_key`. Nothing on the
template side has been updated yet, which means **the app is currently
broken to click through** — any template referencing `datapath['_key']` /
`match['_key']` will hit a `KeyError` (or Jinja will silently render
nothing, depending on the template). This is expected and was called out in
Phase 2 — this phase is what fixes it.

This phase makes no further backend query changes — it's a rename-only
pass across templates and the two Flask route signatures that still use
`_key` as the URL parameter name.

## What needs to be accomplished

1. **Flask route signatures** in `views.py`:
   - `/datapath/view/<int:_key>` → `/datapath/view/<int:data_path_id>`
     (function `datapath_details`)
   - `/datapath/match/<int:_key>` → `/datapath/match/<int:data_path_id>`
     (function `datapath_match`)
   - Update the function bodies' parameter names to match, and every
     internal `flask.url_for(..., _key=...)` call site to
     `flask.url_for(..., data_path_id=...)`.

2. **Templates** — replace every `['_key']` lookup with `['data_path_id']`
   and every `_key=...` kwarg in `url_for(...)` calls with
   `data_path_id=...`:
   - `web/src/web/templates/datapath.html` (multiple occurrences — detail
     view links, the match form's hidden field, parent/children/mapping
     link lists)
   - `web/src/web/templates/datapath_direct.html` (multi-match disambiguation
     table)
   - `web/src/web/templates/matches.html` (bulk match results list)
   - `web/src/web/templates/matchmaker.html` — this one has it in inline
     JS (`dataPathURL.setAttribute("href", endpointMap["datapath_details"]
     + datapath["_key"])`), not just Jinja — check the JSON shape returned
     by whatever endpoint populates `datapath` here (it comes from
     `/matches`, i.e. `fetch_matches`, already renamed in Phase 2) and
     update the JS property access to match.
   - `web/src/web/templates/search.html` — also inline JS
     (`dataPath["_key"]` building a link href) — same treatment, check
     against `fetch_search_data_paths`'s (Phase 2) output shape.

   Do **not** touch `map_backup.html`'s `_from`/`_to` — those are unrelated
   display column labels for the JSON failure report shape returned by
   `api_map_load_native` (Phase 3), not a DataPath identifier, and don't
   change with this rename.

3. **`index.html`**'s `collection-counts` call — update the JS array of
   requested names (`["DataPath", "Release", "DataModel"]`) if Phase 2's
   `fetch_collection_counts` allow-list uses different key names than the
   originals; if the allow-list was implemented keyed on the exact same
   strings (`"DataPath"`, `"Release"`, `"DataModel"`), no change needed here
   — just confirm the names match exactly between `index.html` and the
   Phase 2 allow-list dict.

## Exit criteria

- No `.html` file under `web/src/web/templates/` references `_key`, `_from`,
  or `_to` as a DataPath identifier (the `map_backup.html` display labels
  are the one intentional exception, per item 2 above).
- Both `<int:_key>` route signatures are gone from `views.py`.
- The app renders end-to-end without a `KeyError` or broken link on any
  page that touches a DataPath — this is the point where a first real
  browser click-through becomes possible, ahead of the full Phase 6 pass.
