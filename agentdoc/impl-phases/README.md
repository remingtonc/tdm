# Web → PostgreSQL: Implementation Phases

Execution breakdown of `agentdoc/web_postgres_migration_plan.md` (full
assessment, inventory, and decision rationale — read that first if you need
the *why*, not just the *what*). These phase files are the *how*, sequenced
so each one is startable on its own with only the "Context" section at the
top, not the whole conversation history.

Phases are meant to land as one branch/PR; templates are intentionally
broken between Phase 2 and Phase 4 (see those files) — that's expected, not
a regression, and gets resolved before Phase 6 validation.

1. [`phase-1-connection-plumbing.md`](phase-1-connection-plumbing.md) — shared `psycopg2` connection helper, dependency swap, no behavior change
2. [`phase-2-read-paths.md`](phase-2-read-paths.md) — rewrite every read-only `views.py` query, build the pytest suite alongside
3. [`phase-3-write-paths.md`](phase-3-write-paths.md) — rewrite the mutation endpoints (matching, calculations, bulk import/export)
4. [`phase-4-template-rename.md`](phase-4-template-rename.md) — align templates/routes with the `data_path_id` rename, app becomes renderable again
5. [`phase-5-cut-the-cord.md`](phase-5-cut-the-cord.md) — remove the now-dead ArangoDB imports/references from `web/`
6. [`phase-6-manual-validation.md`](phase-6-manual-validation.md) — full browser click-through checklist
7. [`phase-7-infra-cleanup.md`](phase-7-infra-cleanup.md) — retire the `dbms` (ArangoDB) service from the stack

Settled decisions referenced throughout (full rationale in the main plan
doc, §3): `_key` is not carried forward (renamed to `data_path_id`
everywhere); `web/` gets its own pooled Postgres connection helper; the ES
deep-search path is untouched; ArangoDB removal is deferred to Phase 7;
there's no existing test suite or live ArangoDB to diff against, so parity
is established via a fresh pytest suite against real Postgres data, not
old-vs-new comparison.
