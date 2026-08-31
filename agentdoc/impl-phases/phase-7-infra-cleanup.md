# Phase 7 — Infra Cleanup (Retire ArangoDB)

## Context to start from

Phase 6 signed off: `web/` is fully functional against Postgres, verified
by hand in a browser across every route. ArangoDB (the `dbms` service in
`compose.yaml`) has had zero readers since Phase 5 removed the last code
path that touched it, but it's been deliberately left running through
Phases 1–6 as a rollback safety net — if validation had turned up a
blocking issue, `git revert` on the `web` changes would still have had a
database to fall back to. That safety net is no longer needed once this
phase starts.

Note: in the environment this plan was written against, `dbms` isn't even
running (checked — no `dbms` container, no `dbms_storage`/equivalent volume
exists) and never was during this migration's development. This phase's
changes matter for any environment (e.g. production) where an ArangoDB
instance from before the migration is still live.

## What needs to be accomplished

1. **`compose.yaml`**:
   - Remove the `dbms` service block entirely.
   - Remove the `dbms_storage` volume declaration.
   - Remove the `nginx` service's port-8529 forward (currently proxies host
     `8529` → `dbms:8529`).

2. **`compose.https.yaml`**: apply the same removals if it duplicates any
   of the above (per `README.md`, this file only adds TLS on top of
   `compose.yaml` — confirm it doesn't independently redeclare the `dbms`
   service or port forward before assuming there's nothing to do here).

3. **`nginx/` config**:
   - Remove `nginx/goaccess_dbms.conf` (the GoAccess config that parsed
     ArangoDB access logs).
   - Remove the corresponding volume mount / stage reference in
     `compose.yaml`'s `goaccess` service entrypoint script (it currently
     round-robins `web.conf` → `dbms.conf` → `kibana.conf`; drop the
     `dbms.conf` stage from that loop).
   - Remove any `/goaccess_db.html` routing in `nginx/nginx.conf` /
     `nginx/nginx.https.conf`.

4. **Top-level `README.md`**:
   - Remove the `/goaccess_db.html` bullet under "Access" (ArangoDB access
     statistics no longer exist).
   - Remove the "Port `8529` exposes the ArangoDB Web UI and API" bullet.
   - Check the "System Requirements" and "Installation" sections for any
     remaining ArangoDB-specific mentions (e.g. the ETL "~8 hours" runtime
     note may be worth revisiting too, but that's an ETL-doc concern, not
     part of this cleanup unless it explicitly mentions ArangoDB).

5. **Double-check for stragglers**: `grep -ril "arango\|8529\|dbms" .`
   across the repo root (excluding `.git/` and any `agentdoc/` historical
   notes, which should stay as a record of the migration, not be scrubbed)
   to catch anything not listed above — e.g. `doc/` (the VuePress docs)
   may still describe the old ArangoDB-based architecture and query
   catalog (`doc/docs/dev/architecture/Database.md`, `Search.md`) and could
   use an update or an explicit "historical, pre-Postgres-migration" note,
   depending on how much documentation upkeep is in scope for this effort.

## Exit criteria

- `podman compose config` (or `docker compose config`) parses cleanly with
  no dangling references to the removed `dbms` service/volume.
- Starting the stack fresh (`./start.sh`) never pulls or starts an
  ArangoDB image.
- No live route in `web/` or `nginx/` 404s or dangles because of a removed
  ArangoDB-only path (the `/goaccess_db.html` removal in particular —
  confirm nothing still links to it).
