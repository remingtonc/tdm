# PostgreSQL Conversion — Practical Issues Found in the Live Schema

Review of `agentdoc/existing_schema.aql` and `agentdoc/new_schema.sql` against what the conversion actually produced. Accurate as of 2026-09-01 against `postgres-migration_docs` (`d3296f1`, clean tree).

Unlike `agentdoc/etl_memory_usage.md`, this is **not** a static-analysis pass. Every number below was measured against the running stack: `tdm-postgres-1` (postgres:18-alpine, 982k `data_path` rows, ~6h uptime, post-ETL) and — for the ArangoDB comparisons — `tdm-dbms-1` (arangodb 3.12.4, whose `tdm` database is gone but whose fulltext analyzer was probed directly with a scratch collection). `EXPLAIN ANALYZE` timings are single-run on a warm-ish cache and are indicative, not benchmarks.

`etl/src/schema.sql` is byte-identical to `agentdoc/new_schema.sql` modulo stripped comments, so the proposal *is* the deployed schema — findings below apply to both.

**Headline:** the schema translation decisions in `new_schema.sql` are sound, and the migration hit almost all of the hazards it set out to. Three separate live bugs landed in the search path, one structural collapse produced 5,522 corrupt rows, and the one design decision the doc itself flagged as needing validation (`new_schema.sql:52-57`, full-text search) does not work.

Table snapshot at time of review:

| table | rows |
|---|---|
| `data_path_source` | 3,520,236 |
| `data_path` | 973,133 |
| `release_data_model` | 19,715 |
| `data_model` | 6,735 |
| `release` | 46 |
| `data_path_match` | 4 |
| `calculation` | 1 |

---

## 1. The `FULLTEXT()` → `tsvector` translation is a functional regression

`new_schema.sql:52-57` (design decision #6) flags this as "a genuine behavior change (token/prefix semantics differ from Arango's fulltext analyzer) and should be validated against real search queries." It was not validated, and it fails.

### 1a. Postgres tokenizes a whole path as one lexeme

The default text-search parser classifies a slash-delimited string as a **`file` token** ("File or path name") and does not split it:

```
SELECT ts_debug('simple','/Cisco-NX-OS-device:System/bgp-items/adminSt');

 (file,"File or path name",/Cisco-NX-OS-device,{simple},simple,{/cisco-nx-os-device})
 (blank,"Space symbols",:,{},,)
 (file,"File or path name",System/bgp-items/adminSt,{simple},simple,{system/bgp-items/adminst})
```

```
to_tsvector('simple','/interfaces/interface/state/counters/in-octets')
  -> '/interfaces/interface/state/counters/in-octets':1        -- one token

to_tsvector('simple','1.3.6.1.2.1.2.2.1.10')
  -> '1.3.6.1.2.1.2.2.1.10':1                                  -- one token
```

`human_id` carries `setweight(..., 'A')` — the highest weight in `schema.sql:117-122` — and contributes exactly one unsearchable token per row. SNMP OIDs likewise. Only `machine_id` partially splits, and only where the module-prefix half is hyphenated.

### 1b. ArangoDB's analyzer did split on path separators

Probed directly against `tdm-dbms-1` (arangodb 3.12.4) with a scratch collection carrying `ensureIndex({type:"fulltext", fields:["human_id"]})` and three representative rows:

```
interface  -> 1 ["/interfaces/interface/state/counters/in-octets"]
interfaces -> 1 ["/interfaces/interface/state/counters/in-octets"]
counters   -> 1 ["/interfaces/interface/state/counters/in-octets"]
octets     -> 1 ["/interfaces/interface/state/counters/in-octets"]
bgp        -> 1 ["/Cisco-NX-OS-device:System/bgp-items/adminSt"]
items      -> 1 ["/Cisco-NX-OS-device:System/bgp-items/adminSt"]
adminst    -> 1 ["/Cisco-NX-OS-device:System/bgp-items/adminSt"]
cisco      -> 1 ["/Cisco-NX-OS-device:System/bgp-items/adminSt"]
system     -> 0 []
device     -> 0 []
```

Arango split on `/` and `-` (not consistently on `:` — hence `system`/`device` missing, which are the halves of the `device:System` token). Per-segment search worked; in Postgres it does not.

### 1c. Measured impact on real data

Restricted to YANG-style paths (`human_id LIKE '/%'`), counting rows that contain `/interface/` as a genuine path segment:

| | rows |
|---|---|
| `human_id LIKE '%/interface/%'` | 121,507 |
| ... of those, matched by `search_vector @@ plainto_tsquery('simple','interface')` | 33,469 |

**~72% of genuinely relevant rows are invisible to `/api/v1/search`** (`views.py:671`). The 33,469 survivors are matching via `description` (weight `'C'`) or via hyphen-splitting inside `machine_id` — not via the path itself.

### 1d. It is not free, and it is not used

```
 indexrelname                | size   | idx_scan
 data_path_pkey              | 47 MB  | 20,141,518
 data_path_machine_id_key    | 512 MB |  6,140,824
 idx_data_path_parent_id     | 25 MB  |         64
 idx_data_path_data_type_id  | 20 MB  |         39
 idx_data_path_search_vector | 374 MB |          4
```

```
sum(pg_column_size(search_vector)) = 588 MB      -- vs description: 39 MB
pg_total_relation_size('data_path') = 3,373 MB
```

The generated column plus its GIN index are **~960 MB, ~29% of the table**, with four index scans in the table's lifetime. The planner does not even choose it for the production query shape — `EXPLAIN ANALYZE` of `fetch_search_data_paths` (`views.py:656-706`) drives from `release` and applies `@@` as a per-row filter across 100,328 `data_path` primary-key lookups (606 ms total).

### 1e. Fix directions

Either pre-tokenize in the generated expression (`schema.sql:117-122`):

```sql
setweight(to_tsvector('simple',
    regexp_replace(coalesce(human_id, ''), '[/.:_-]+', ' ', 'g')), 'A') || ...
```

**Do not use `translate(human_id, '/.:-', ' ')`** — a 4-character `from` with a 1-character `to` *deletes* the remaining three, so `bgp-items` collapses to `bgpitems`. Verified:

```
translate      -> 'adminst':3 'bgpitems':2 'cisconxosdevicesystem':1
regexp_replace -> 'adminst':8 'bgp':6 'cisco':1 'device':4 'items':7 'nx':2 'os':3 'system':5
```

Or drop `search_vector` and its index entirely and accept what `current_db_context.md` §7 already argues — Elasticsearch owns search, it already has the path-aware tokenizer and synonym expansion (`doc/docs/dev/architecture/Search.md`), and deleting the column reclaims ~960 MB and removes the write amplification in §5 below. Rebuilding the column later is a one-time table rewrite; it is not a one-way door.

---

## 2. `LIMIT` changed meaning wholesale

The pre-migration AQL applied `LIMIT @start_index, @max_return_count` at the **innermost** nesting level (`views.py:784` at `95faaaf^`), inside `FOR dp IN DataPath` under OS → Release → DML → DataModel. "Maximum Return Count" (`forms.py:49`, default 10) meant *up to 10 paths per DataModel, per Release* — a full tree could return hundreds of paths.

`views.py:689` applies it to the flat join instead, and the OS/Release/DML/DataModel tree is rebuilt in Python afterwards (`views.py:703-709`). The limit now bounds permutation rows, not paths per bucket. Measured with three IOS XE releases selected:

```
rows_returned | distinct_paths
           10 |              4
```

A path shipped in multiple selected releases consumes one row per `(data_model, release)` permutation — `data_path_source` alone averages 3.62 models per path (max 15, 681,075 paths with more than one). Two consequences: the user asks for 10 results and gets 4, and `OFFSET` walks permutation rows, so page boundaries slice through a path's permutation group and the same path can appear on two consecutive pages.

**Fix:** bound the distinct path set in a subquery — `WHERE dp.data_path_id IN (SELECT data_path_id FROM ... ORDER BY human_id LIMIT %s OFFSET %s)` — and decide deliberately whether the parameter means per-bucket (Arango's behavior) or global (a defensible change, but it should be a choice).

---

## 4. The `parent_id` collapse produced 5,522 self-parented rows

`new_schema.sql:22-28` (design decision #2) collapses `DataPathParent` + `DataPathChild` into `data_path.parent_id`. The reasoning is right — Arango stored the same relationship twice — but the collapse is lossy in a way the doc doesn't anticipate.

```
SELECT count(*) FROM data_path WHERE parent_id = data_path_id;  -->  5522
SELECT count(*) ... two-cycles (a->b->a) ...                    -->  0
```

Examples:

```
/ned:native/router/ospfv3/address-family/ipv4/vrf/passive-interface
/Cisco-IOS-XE-native:native/controller/Cisco-IOS-XE-controller:vdsl/line-mode
```

**Mechanism.** `add_data_paths_to_dm` (`etl/src/yang/__init__.py:184-244`) recurses with `dp_parent_id=path_id` (`:244`), and the upsert at `:200-209` is last-write-wins on `parent_id = EXCLUDED.parent_id`. When a descendant's `machine_id` dedups onto a row that is already its own ancestor — augments and `uses` re-entry can resolve two tree depths to the same qualified path — the row is updated to point at itself. Postgres FKs do not prevent self-reference, and nothing else checks.

**Consequences.**

- `fetch_datapath_parent` (`views.py:199-205`) returns the node itself, so the detail-page breadcrumb points at itself.
- The `WITH RECURSIVE` ancestor walk recommended by `new_schema.sql:344-350` will not terminate on these rows. `UNION ALL` has no cycle detection; a depth cap or PG14+ `CYCLE` clause is mandatory, not optional.

**The structural version of the same problem.** 681,075 paths (69%) are sourced from more than one `data_model` (avg 3.62, max 15). Each gets exactly one `parent_id` — whichever model was processed last. Arango's `DataPathParent` edge collection could hold several edges per node; a single FK column cannot. This is the same class of issue `agentdoc/data_path_revision_history.md` §2 documents for `description`/`data_type_id`, but it is *structural* rather than descriptive — tree navigation depends on it. If §4 of that note is ever implemented (moving revision-scoped attributes onto `data_path_source`), `parent_id` belongs in the same move.

**Minimum fix:** `ALTER TABLE data_path ADD CONSTRAINT chk_data_path_not_self_parent CHECK (parent_id <> data_path_id);` plus a repair pass over the existing 5,522 rows, and a depth guard in any recursive walk.

---

## 5. 10× write amplification from the unconditional upsert

```
 relname   | n_tup_ins | n_tup_upd | n_tup_hot_upd | total
 data_path |   973,133 | 9,905,267 |     4,891,872 | 3,373 MB
```

Nearly 10M updates for 973k rows. Only 49% were HOT, so roughly 5M updates also had to maintain the 512 MB unique index and the 374 MB GIN index.

Cause: the `ON CONFLICT (machine_id) DO UPDATE SET ...` at `etl/src/yang/__init__.py:200-209` rewrites the row unconditionally, even when every incoming value is byte-identical to what's stored. `search_vector` is `STORED`, so it is recomputed and re-indexed on every one of those writes. The `UPDATE data_path SET data_type_id` at `:240` adds another unconditional write per typed path.

This is genuinely new. `current_db_context.md` §5 concluded that row-by-row inserts in Postgres would be "at parity, not a regression" with Arango's `createDocument().save()` — true for *inserts*, but the DB-side dedup that replaced `dp_cache` (see `etl_memory_usage.md` "Status update") converted ~9M of those into MVCC updates with full index maintenance, which Arango's per-document save did not incur in this shape.

**Fixes, both cheap:**

- Append `WHERE data_path IS DISTINCT FROM EXCLUDED` to the `DO UPDATE` so no-op writes are suppressed. Note `RETURNING` then yields no row for suppressed updates, so the id lookup needs a fallback `SELECT` — the same trap that motivated the no-op `SET machine_id = EXCLUDED.machine_id` originally (`yang/__init__.py:186-192`).
- Create `idx_data_path_search_vector` *after* the load rather than carrying it through 10M writes — or delete it per §1e.

---

## 6. `human_id` has no index

`new_schema.sql:48-51` (design decision #5) audits foreign-key columns — correctly, since Arango's automatic `_from`/`_to` edge index had no Postgres equivalent — and stops there. It never audits non-FK equality-filter columns.

`resolve_data_path_id` (`views.py:925-943`) and `fetch_datapath_arbitrary_id` (`views.py:182`) both filter on `human_id`:

```
EXPLAIN (ANALYZE, BUFFERS) SELECT data_path_id FROM data_path WHERE human_id = '...';

 Gather (actual time=278.846..287.588 rows=0.00)
   ->  Parallel Seq Scan on data_path (Rows Removed by Filter: 324,378)
         Buffers: shared hit=1834 read=303,576
 Execution Time: 306.454 ms
```

~300 ms and 2.4 GB scanned per lookup. `resolve_data_path_id` runs this as the fallback after every `machine_id` miss, so a native-dump restore (`/api/v1/map/load/native`) pays it once per mapping. A plain btree is ~25 MB.

For reference, `human_id` ambiguity is real but small — 1,190 duplicated values covering 2,381 rows — and both callers already handle the multi-match case explicitly, so a non-unique index is the right shape.

---

## 7. No incremental ETL path, and all 23 FKs are `NO ACTION`

```
SELECT count(*) FROM pg_constraint
WHERE contype='f' AND connamespace='tdm'::regnamespace AND confdeltype <> 'a';  -->  0
```

Every foreign key is `ON DELETE NO ACTION`. Combined with `main.py:88` — `created = not schema_exists(conn)`, which refuses to populate when the `tdm` schema already exists — the system has no supported way to add a release or prune a deprecated one.

That gate was necessary under Arango, where writes were not idempotent. It no longer is: every ETL write now goes through `ON CONFLICT` (`yang/__init__.py:200-209`, `:220-223`, and the SNMP equivalents). The gate is the only thing preventing an incremental re-run.

**Practical consequence:** adding a release means `reset.sh`, which runs `podman compose down --volumes` and destroys the Postgres volume — taking `data_path_match` and `calculation` with it, unless someone remembers to `/api/v1/map/dump/native` first. That is the one category of data the ETL cannot regenerate, and `current_db_context.md` §9 identifies keeping it separable as an explicit design goal. The FKs don't encode it.

**Fix direction (not implemented — for discussion):**

- Drop or gate the `schema_exists` check behind a `--refresh` flag and let the idempotent writes do their job.
- `ON DELETE CASCADE` on the ETL-owned join tables (`data_path_source`, `release_data_model`) so a release can actually be pruned.
- Keep `NO ACTION`/`RESTRICT` on `data_path_match` and `calculation_input`/`calculation_result`, so a reload can never silently sweep curated data — a reload that would orphan a mapping should fail loudly and be resolved deliberately.
- There is no migration tooling at all: `schema.sql` is applied once on first connect (`models.py`, `create_schema`). The `ALTER TABLE data_path_source ADD COLUMN ...` proposed in `data_path_revision_history.md` §4, and the `CHECK` in §4 above, currently have nowhere to live.

---

## 8. Smaller and latent issues

- **`UNIQUE (name, revision)` should include the language** (`schema.sql:92`). Today the only blank-revision rows are the 1,571 SMI MIBs (`data_model.revision = ''`, hardcoded at `snmp.py:332`), spanning exactly one language, so nothing collides. But a YANG module with no `revision` statement also lands on `''`, and would then merge with a same-named MIB into a single row carrying one arbitrary `data_model_language_id`. Arango's `_key` scheme (`"name+"` for YANG vs bare `"name"` for SNMP) could not collide this way. Adding `data_model_language_id` to the unique key restores the old separation.
- **`insert_data_path_match` doesn't reject `a == b`** (`views.py:944-965`). It sorts the pair for the `CHECK (data_path_a_id < data_path_b_id)` invariant, correctly, but a self-mapping fails the CHECK as an `IntegrityError` rather than the intended "Mapping already exists!"-style message — the user gets a 500.
- **`calculation.name TEXT NOT NULL UNIQUE`** (`schema.sql:154`) is new; Arango's `Calculation` had an auto-generated `_key` and no name constraint. A restored dump containing two same-named calculations now fails where it previously loaded.
- **`search_path=tdm` excludes `public`** (`web/db.py:41`, `main.py:69`). Any extension installed into `public` — `pg_trgm` being the obvious next step for fuzzy path search — will not resolve unqualified. Worth knowing before it bites.
- **The query-translation note at `new_schema.sql:333-341` suggests `SELECT os.*, r.*, dm.*`.** Under `RealDictCursor` (`web/db.py`, `cursor()`), `name` and `description` each appear three times and the dict silently keeps only the last. `views.py` aliases every column correctly today (`views.py:677-679`), so this is a doc-level landmine rather than a live bug — but it will teach the wrong habit to whoever writes the next query.
- **gevent + psycopg2** (`runserver.py`, `start_prod`). psycopg2 is a blocking C extension, so DB calls serialize the whole worker. Not a regression — python-arango wasn't monkey-patched either, and there is no `gevent.monkey.patch_all()` anywhere in `web/src`. But if throughput ever prompts one, psycopg2 needs `psycogreen.gevent.patch_psycopg()` and `ThreadedConnectionPool` needs re-examining against greenlets.
- **Dead columns kept "for fidelity"** — `content`, `parsed_checksum` (`schema.sql:88-89`), `is_variable`, `verified` (`schema.sql:110`, `:112`) — while `Device` and friends were correctly deferred under design decision #4 (`new_schema.sql:43-47`) with the reasoning "carrying dead tables into the new system for parity alone isn't worth it." The same argument applies to dead columns. Minor, but the two decisions point opposite directions.

---

## What holds up

Checked and correct, recorded so it doesn't get re-litigated:

- **Dropping the `slug` columns** (design decisions #1 and #8) is right. The one place it had a visible consequence — Arango's sanitized `OS._key` of `IOS_XE` becoming the real name `IOS XE` — is already handled in the ETL (`static.py:196`), and the live data confirms it: `os.name` is `IOS XE`, `IOS XR`, `NX-OS`.
- **The generated column correctly uses two-argument `to_tsvector(regconfig, text)`** (`schema.sql:118-122`). The one-argument form is `STABLE`, not `IMMUTABLE`, and Postgres would have rejected the column outright. Easy trap, avoided.
- **The undirected-match invariant is implemented properly.** `CHECK (data_path_a_id < data_path_b_id)` plus `sorted((dp_one_id, dp_two_id))` at the single insert site (`views.py:956`), with `ON CONFLICT DO NOTHING` + `RETURNING` used to reproduce the old duplicate-mapping error rather than surfacing a raw constraint violation.
- **User-curated data stays portable.** `fetch_dump_mappings_native` (`views.py:1014-1023`) keys on `machine_id`, which is `UNIQUE`, so dumps survive a rebuild even though surrogate ids do not — preserving the boundary `current_db_context.md` §9 identified from the `migrate/1_to_2/` precedent.
- **The `release.previous_release_id` chain is clean.** 43 of 46 rows populated, exactly 3 heads for 3 OSes, no orphans — the `ReleaseRevision` edge collapse worked as designed.

---

## Priority

| # | Issue | Shape of fix |
|---|---|---|
| 3 | `only_leaves` hides all leaves | One line |
| 6 | Missing `human_id` index | One line |
| 2 | `LIMIT` semantics | Small query rewrite + a deliberate decision |
| 1 | Full-text tokenization | A decision: fix the expression, or delete the column and lean on ES |
| 4 | Self-parented rows | `CHECK` + data repair + recursion guard; ties into `data_path_revision_history.md` §4 |
| 5 | Write amplification | `IS DISTINCT FROM` guard + index-after-load |
| 7 | No incremental path, `NO ACTION` FKs | Real design work; needs a migration-tooling answer first |
