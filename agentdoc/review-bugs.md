# Code review findings — postgres-migration_web (phases 3–5)

Reviewed range: `git diff ffbc40a..HEAD` (~848 lines across `web/src/web/views.py`, nginx configs, tests, templates).

Ordered most severe first.

## 1. Silent data loss — `map_datapath_single()` drops `timestamp`

- **File:** `web/src/web/views.py:847`
- **Bug:** `map_datapath_single()` accepts a `timestamp` parameter but never uses it, so `data_path_match.created_at` silently becomes `now()` instead of the caller-supplied value.
- **Failure scenario:** A user exports mappings via `GET /api/v1/map/dump/native` (which includes original `timestamp` values from `created_at`) and later restores them via `POST /api/v1/map/load/native`, which calls `map_datapath_single(**mapping)` including that `timestamp` key. Because `insert_data_path_match()` (views.py:910) has no `timestamp` parameter and the INSERT never references `created_at`, every restored mapping silently gets `created_at = now()`, losing the original provenance data the backup/restore feature is supposed to preserve. The old Arango implementation used `timestamp or time.time()` and actually stored it.

## 2. nginx upstream caching regression breaks single-service redeploy

- **File:** `nginx/nginx.conf:37` (also `nginx/nginx.https.conf:53/91/102`)
- **Bug:** Removing the `resolver 127.0.0.11 valid=30s;` directive and the `set $upstream_web ...; proxy_pass $upstream_web;` indirection means nginx now resolves `web`/`dbms`/`kibana` once at startup and caches the IP for the life of the process, instead of re-resolving per request.
- **Failure scenario:** `update_web.sh` runs `podman compose ... up -d --force-recreate --no-deps web`, which recreates only the web container (new IP on the backend network) while nginx keeps running. With the resolver+variable idiom removed, nginx keeps proxying to the old, now-dead IP, so every request through nginx to the web app fails (502/timeout) until nginx itself is manually restarted.

## 3. Lookup semantics changed in `resolve_data_path_id()`

- **File:** `web/src/web/views.py:899`
- **Bug:** Merges the `machine_id` and `human_id` lookups into a single `WHERE machine_id = %s OR human_id = %s` query, losing the old field-priority order (machine_id checked first, short-circuiting before human_id was ever consulted).
- **Failure scenario:** If a value uniquely identifies one row via `machine_id` but also happens to equal a different row's (non-unique) `human_id`, the old code returned the machine_id match immediately; the new code fetches both rows, sees `len(rows) > 1`, and raises "More than one document exists" for what used to be an unambiguous machine_id lookup.

## 4. TOCTOU race on calculation-name uniqueness

- **File:** `web/src/web/views.py:826`
- **Bug:** `map_datapath_calculation_single()` enforces calculation-name uniqueness with a SELECT-then-INSERT check in application code; the comment itself notes `calculation.name` has no UNIQUE constraint backing it.
- **Failure scenario:** Two concurrent requests (e.g. two overlapping `/api/v1/map/load/native` imports) submit a calculation with the same name; both pass the `SELECT 1 FROM calculation WHERE name = %s` check before either commits, and both INSERTs succeed, producing duplicate calculations the "already exists" guard was meant to prevent.

## 5. N+1 queries in `map_datapath_calculation_single()`

- **File:** `web/src/web/views.py:823`
- **Bug:** Resolves each `InCalculation`/`CalculationResult` entry with a separate `resolve_data_path_id()` query (N+1) instead of a single batched lookup, even though the file already has a bulk-lookup helper (`_fetch_given_data_paths`, views.py:231) using `= ANY(...)`.
- **Failure scenario:** A calculation with many factors/results issues one round trip per data path instead of one; under load or with large `InCalculation`/`CalculationResult` lists this adds unnecessary DB round trips on a write path that already holds one transaction open.

## 6. Leftover `_key` naming after phase-4 rename

- **File:** `web/src/web/views.py:124`
- **Bug:** Internal helper functions (`fetch_datapath_os_graph`, `fetch_datapath_dml_graph`, `fetch_datapath_mappings`, `fetch_datapath_datatype`, `fetch_datapath_parent`, `fetch_datapath_children`, `fetch_datapath_models`, `fetch_datapath`) still use the parameter name `_key` even though phase 4 renamed the route-level identifier to `data_path_id` everywhere else (routes, templates, tests).
- **Failure scenario:** Not a crash, but inconsistent naming left behind by the rename — a reader following the phase-4 `_key` → `data_path_id` migration will find these eight function signatures still calling the same value `_key`, increasing the odds a future edit reintroduces `_key` semantics or causes confusion about whether it's the old Arango key or the new surrogate id.

---

**Recommendation:** Fix #1 (silent data loss on backup/restore) and #2 (nginx redeploy breakage) first — both are concrete, high-confidence bugs rather than style issues.
