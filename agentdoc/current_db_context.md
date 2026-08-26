# Current Database Implementation (ArangoDB) — Reference for PostgreSQL Migration

Snapshot of TDM's existing database layer, written as prep material before migrating the primary datastore from ArangoDB to PostgreSQL. Accurate as of 2026-08-25 against the repo at `master` (4eb02d6).

## 1. Where ArangoDB is used

Two subprojects talk to ArangoDB directly, each via a **different, independent client library** — the two were never consolidated:

- **`etl/`** — [pyArango](https://github.com/ArangoDB-Community/pyArango) (`pyArango.connection.Connection`, `pyArango.collection.Collection`, `pyArango.graph`)
  - `etl/src/main.py:28`, `etl/src/models.py:19-20`
  - Config: `etl/src/config.json` (checked into git, no env override):
    ```json
    { "dbms": {"arangoURL": "http://dbms:8529", "username": "root", "password": "tdm"},
      "search": {"searchURL": "http://search:9200"} }
    ```
  - `etl/src/Pipfile:8` — `pyArango = "*"` (unpinned)
- **`web/`** — [python-arango](https://github.com/ArangoDB-Community/python-arango) (`arango.ArangoClient`)
  - `web/src/web/views.py:22,28` — `ARANGO_PORT = 8529` hardcoded constant
  - No shared connection helper — every write path opens its own client ad hoc, e.g. `views.py:1226-1227`:
    ```python
    client = ArangoClient(hosts='http://dbms:{}'.format(ARANGO_PORT))
    db = client.db('tdm', username='root', password='tdm')
    ```
    Repeated independently at ~5 call sites (lines ~964-965, 1037-1038, 1062-1063, 1082-1083, 1226-1227).
  - `web/src/Pipfile:11` — `python-arango = "*"` (unpinned)
- **`migrate/1_to_2/`** — no DB connection; operates offline on an exported JSON file (see §9).
- **`doc/`** — no DB connection; VuePress docs describing the schema/queries (useful as secondary reference, `doc/docs/dev/architecture/Database.md` and `Search.md`).

Database name is always **`tdm`**, created by ETL on first run (`main.py:59-68`) — ETL refuses to re-populate if the DB already exists (`main.py:87-90`).

## 2. Data model / collections

Schema-as-code lives in `etl/src/models.py`. `_key` is ArangoDB's native primary key.

### Vertex (document) collections

| Collection | Fields | Notes |
|---|---|---|
| `DataPath` | `machine_id`, `human_id`, `description`, `is_leaf`, `is_variable`, `is_configurable`, `verified` | Core entity: a YANG XPath or SNMP OID. `machine_id` unique (enforced by index). |
| `DataModel` | `content`, `name`, `parsed_checksum`, `revision` | A YANG module or MIB. `_key` = `"<name>+<revision>"` (YANG) or bare `model_name` (SNMP). `content`/`parsed_checksum` unused, always `None`. |
| `DataModelLanguage` | `name`, `description` | YANG, SMI, DME, CLI. |
| `OS` | `name`, `description` | IOS XE, IOS XR, NX-OS. |
| `Release` | `name`, `description` | Per-OS version, `_key` = `"<OS>+<release>"` e.g. `IOS_XE+16.7.1`. |
| `ControlProtocol` | `name`, `description` | gRPC, NETCONF, SNMP, MDT, CLI, RESTCONF, NX-API. |
| `TransportProtocol` | `name`, `description` | TCP, UDP, SSH, Telnet, HTTP. |
| `Encoding` | `name`, `description` | JSON, XML, GPB, KV-GPB, BER, Text. |
| `DataType` | `name`, `description`, `is_primitive` | `_key` = `"<DML>+<type>"` e.g. `YANG+string`. |
| `Device` | `name`, `description` | **Defined but never populated** by any ETL path. |
| `Calculation` | `name`, `description`, `equation`, `author` | User-submitted derived-value definitions, created via web API. |

### Edge collections

| Edge collection | Extra fields | From → To | Populated by |
|---|---|---|---|
| `DeviceHasDataPath` | `os`, `release` | Device → DataPath | **Never populated** |
| `DeviceHasDataModel` | `os`, `release` | Device → DataModel | **Never populated** |
| `OSHasRelease` | — | OS → Release | `static.py` |
| `ReleaseRevision` | — | Release → Release (prev→next) | `static.py` |
| `ReleaseHasDataModel` | — | Release → DataModel | `yang/__init__.py` |
| `DataPathFromDataModel` | `parse_timestamp` (unused) | DataModel → DataPath | `yang/__init__.py`, `snmp.py` |
| `OfDataModelLanguage` | — | DataModelLanguage → DataModel | `yang/__init__.py`, `snmp.py` |
| `HasControlProtocol` | — | DataModelLanguage → ControlProtocol | `static.py` |
| `HasEncoding` | — | ControlProtocol → Encoding | `static.py` |
| `HasTransportProtocol` | — | ControlProtocol → TransportProtocol | `static.py` |
| `DataPathMatch` | `timestamp`, `author`, `validated`, `weight`, `annotation`, `needs_human` | DataPath ↔ DataPath (queried with `ANY`, logically undirected) | web UI mapping endpoints |
| `DataPathParent` | — | DataPath(child) → DataPath(parent) | `yang/__init__.py` |
| `DataPathChild` | — | DataPath(parent) → DataPath(child) | `yang/__init__.py` |
| `OfDataType` | `parse_timestamp` | DataPath → DataType | `yang/__init__.py` |
| `DataModelChild` | — | DataModel(rev N) → DataModel(rev N+1) | `yang/__init__.py` |
| `DataModelParent` | — | DataModel(rev N+1) → DataModel(rev N) | `yang/__init__.py` |
| `DataModelDerivedFrom` | — | DataModel → DataModel | **Never populated** |
| `InCalculation` | — | DataPath → Calculation | web (`views.py`, calculation endpoints) |
| `CalculationResult` | — | Calculation → DataPath | web (`views.py`, calculation endpoints) |
| `DataModelLanguageHasDataType` | — | DataModelLanguage → DataType | `static.py` |

**Modeling quirk worth flagging for the relational redesign:** `DataPathParent`/`DataPathChild` and `DataModelParent`/`DataModelChild` each store the *same* relationship as two separate edge collections in opposite directions, instead of one edge collection traversed both ways. In Postgres this collapses to a single self-referencing FK (e.g. `data_path.parent_id`) or a single adjacency table.

## 3. Graph structure

No ArangoDB *named graph* is defined (no `db.createGraph`) — only raw edge collections with ad hoc multi-collection AQL traversals (`FOR v, e, p IN n..m DIRECTION start edgeCol1, edgeCol2, ...`). True traversal usage is a small minority of queries:

- `views.py` `fetch_datapath_os_graph` (~117-129) — 3-hop `INBOUND`: `DataPath ← DataModel ← Release ← OS`
- `views.py` `fetch_datapath_dml_graph` (~131-142) — 2-hop `INBOUND`: `DataPath ← DataModel ← DataModelLanguage`
- `views.py` `fetch_all_matches` (~42-57) — 3-hop mixed direction, last hop `ANY DataPathMatch`
- `etl/src/search.py` `query_all_datapaths` (~34-67) — two nested 2-hop `INBOUND` traversals, used to flatten data for Elasticsearch

Everything else — the majority of AQL in the codebase — does manual nested `FOR ... FILTER x._from == y._id` joins rather than native traversal syntax. **Migration implication:** most queries translate directly to SQL joins; only the handful of true traversal queries need to become recursive CTEs or fixed-depth JOIN chains.

## 4. AQL query catalog (web app)

All executed via one generic helper, `query_db()` (`views.py` ~1224-1233): `db.aql.execute(query, bind_vars=bind_vars)`.

- **Detail/browse** (`/datapath/view/<_key>`): `fetch_datapath` (`DOCUMENT()` lookup), plus the two graph traversals above, `fetch_datapath_parent`/`_children` (1-hop via `DataPathParent`/`Child`), `fetch_datapath_datatype` (1-hop via `OfDataType`), `fetch_datapath_mappings` (bidirectional join over `DataPathMatch` via ternary on `_from`/`_to`), `fetch_datapath_models` (reverse join via `DataPathFromDataModel`)
- **Direct lookup** (`/datapath/direct`): `fetch_datapath_arbitrary_id` — `FILTER dp.human_id == @path || dp.machine_id == @path`
- **Matchmaker/bulk match** (`/matches`): `fetch_matches` — full-collection scan + nested `FLATTEN` subquery
- **Calculations** (`/calculations*`): `fetch_calculations*` — multi-level nested subqueries joining `DataPath ↔ CalculationResult/InCalculation ↔ Calculation`; uses explicit `WITH DataPath, Calculation` (required in ArangoDB cluster mode)
- **Stats**: `fetch_collection_counts` — `COLLECTION_COUNT()` with dynamic collection-name binding
- **Search facets**: `fetch_os_releases`, `fetch_dmls`
- **Main structured search** (`/api/v1/search`): `fetch_search_data_paths` (~691-809) — the heavyweight query. Uses `FULLTEXT(DataPath, "human_id"/"machine_id", filter_str)` + `UNION_DISTINCT`, then deeply nested `MERGE` to build a JSON tree grouped OS→Release→DML→DataModel→[DataPath]. Documented (`doc/docs/dev/architecture/Database.md`) as using **up to 32GB RAM and 1-45s latency** — the direct motivation for the Elasticsearch search cache (§7).
- **Export/backup**: `fetch_dump_mappings_native`/`fetch_dump_mappings` — dump `DataPathMatch`/`Calculation` resolved to `machine_id`s, for CSV/JSON backup-restore
- Additional reference queries (OS/Release drilldowns, `INTERSECTION()`, `MINUS()` for unmatched-DataPath detection, etc.) are cataloged in `doc/docs/dev/architecture/Database.md:191-1032` — useful signal for real query patterns beyond what's wired into `views.py`.

## 5. ETL pipeline into the DB

Entry point `etl/src/main.py:70-104`, runs only if DB doesn't already exist:

1. `create_schema(db)` — creates all 11 vertex + 20 edge collections and indexes (§6).
2. `populate_static(db)` — seeds `TransportProtocol`, `Encoding`, `ControlProtocol`, `DataModelLanguage`/`DataType`, `OS`/`Release`, all from hand-maintained hardcoded Python dicts (manually kept in sync, e.g. deprecated releases commented out — see `6c9f980 Deprecate IOS XR < 6.3.x`).
3. `populate_snmp(db)` — FTP-downloads Cisco MIBs, compiles via `pysmi`, creates one `DataModel` per MIB + one `DataPath` per OID. Note: `db.connection.resetSession('root', 'tdm')` in `snmp.py:211` — a pyArango session-timeout workaround that hardcodes credentials again, independent of `config.json`.
4. `populate_yang(db)` — clones `github.com/cisco-ie/yang`, parses YANG modules per OS×release with **pyang**, extracts `machine_id`/`xpath`/`type`/`rw`/`description`/children recursively, upserts `DataModel`/`DataPath` docs and all related edges. Uses module-level in-process Python dict caches (`dm_cache`, `dp_cache`, `dt_cache`, etc.) to dedupe writes instead of DB-side upserts — documented in ETL notes as "extremely non-optimal, ~7-10GB RAM usage."
5. `populate_search(db, search_host)` — flattens the graph via AQL and bulk-loads Elasticsearch (§7); runs automatically on first DB creation or via `python main.py --stage search`.

Writes are per-document (`createDocument(...).save()`, `createEdge().links(...)`) — **no batch/bulk insert API used**. Some edge writes pass `waitForSync=True` for forced durability. Net effect: current ETL already commits row-by-row, so a naive row-by-row INSERT strategy in Postgres would be at parity, not a regression.

## 6. Indexes

All explicit indexes are on `DataPath` only (`etl/src/models.py`, `create_indexes`):

```python
db['DataPath'].ensureSkiplistIndex(fields=['machine_id'], unique=True, sparse=False)
db['DataPath'].ensureFulltextIndex(fields=['machine_id'])
db['DataPath'].ensureFulltextIndex(fields=['human_id'])
db['DataPath'].ensureFulltextIndex(fields=['description'])
```

- The unique skiplist index on `machine_id` is the **only uniqueness constraint in the entire schema** — maps directly to a Postgres unique btree index.
- The fulltext indexes back `FULLTEXT()` calls in `fetch_search_data_paths` and doc-cataloged example queries. Postgres equivalent is `tsvector`/`GIN`, but token/analyzer semantics differ — search relevance behavior will need re-validation, not a drop-in swap.
- No indexes exist on any other vertex collection beyond Arango's automatic primary-key index, and none on edge collections beyond Arango's automatic `_from`/`_to` edge index — every `FILTER x._from == y._id` join in `views.py` relies on that automatic index. **In Postgres, every edge-collection-turned-join-table will need explicit FK indexes to avoid a regression.**

## 7. Search integration (ArangoDB → Elasticsearch)

Per `doc/docs/dev/architecture/Search.md`: **ArangoDB is the sole source of truth; Elasticsearch is a purely derived, denormalized, disposable search cache**, rebuildable from Arango at any time. (Arango's own `FULLTEXT()` search was tried first but was too slow/memory-hungry, hence ES was added.)

- `etl/src/search.py`'s `populate_search` queries Arango with a flattening AQL query producing one row per `(DataPath, DataModel, OS, Release)` permutation (~2M denormalized documents per ETL notes), then `setup_search_db`/`populate_search_db` create a custom-analyzed ES index (`generic_path_analyzer` with network-domain synonyms, e.g. `intf`↔`interface`) and bulk-load via `elasticsearch.helpers.streaming_bulk`.
- The web app's "deep search" UI queries ES directly (`views.py` `search_deep_api`/`fetch_search_data_paths_es`, ~535-689), hardcoded to `Elasticsearch('http://search:9200')` — **no Arango read on this path at all**.
- **No live sync/CDC** exists from Arango writes (e.g. new `DataPathMatch` edges created via the web UI) back into Elasticsearch — ES is only refreshed by re-running the ETL search stage. This is one-way and batch, not streaming.
- Elasticsearch is version **6.4.0** (paired with Kibana 6.4.0) — both quite old, out of scope for the Arango→Postgres migration itself but worth flagging separately.

**Migration implication:** the Postgres migration does not need to preserve any live sync mechanism, since none currently exists. Whether to keep Elasticsearch as the search layer (recommended, since Arango's own fulltext search is already bypassed for the primary UI) or move to Postgres full-text search is a separate decision from the datastore migration.

## 8. Config / connection details

`compose.yaml`:
```yaml
dbms:
  image: arangodb/arangodb:latest
  volumes:
    - dbms_storage:/var/lib/arangodb3   # named volume, nocopy
  expose: ["8529"]
  environment:
    - ARANGO_ROOT_PASSWORD=tdm
  networks: [backend]
```

- No `ARANGO_NO_AUTH`, no separate app-level DB user — both ETL and web authenticate as `root`/`tdm`.
- Port 8529 isn't published directly by the `dbms` service; `nginx` reverse-proxies host port 8529 → `dbms:8529` (same in `compose.https.yaml`, which only adds TLS).
- **No `.env` file, no environment-variable-based configuration anywhere** — connection details are hardcoded literals in three independent places: `etl/src/config.json`, `web/src/web/views.py` (constant + 5 call sites), `etl/src/snmp.py:211`.
- Storage: named Podman volume `dbms_storage` at `/var/lib/arangodb3` (Arango's default data directory).
- `reset.sh`/`stop.sh` run `podman compose down --rmi all --volumes` — a "reset" wipes the Arango data volume entirely; the only backup mechanism is the web app's `/api/v1/map/dump/*` routes, which export only `DataPathMatch`/`Calculation`, not the full DB.

## 9. `migrate/1_to_2/` context

Not a database-engine migration — a one-time offline data-format migration for the `machine_id` scheme, operating on exported JSON with no live DB connection:

- Per its README: TDM v2.0 changed how `machine_id` expresses modules/prefixes, breaking v1.x mapping portability.
- `mappings.py` re-parses the same YANG repos to build an `upgrade_map` from old-style to new-style `machine_id`, then rewrites a previously-exported `tdm_mappings.json` (same shape as `fetch_dump_mappings_native`'s output, i.e. `{"DataPathMatch": [...], "Calculation": [...]}`) into `upgraded_tdm_mappings.json` for re-import via `/api/v1/map/load/native`.
- **Useful signal for the Postgres schema:** this confirms `machine_id` has changed format at least once historically, and that user-curated data (`DataPathMatch`/`Calculation`) is already treated as portable/exportable independent of the bulk YANG/MIB-derived catalog data. The new schema should preserve that boundary — keep user-curated mapping/calculation tables cleanly separable from ETL-regenerated catalog tables, so future format migrations don't require touching both.

## 10. ArangoDB version

- **No version is pinned anywhere in the repo** — `compose.yaml` specifies `arangodb/arangodb:latest`, so the actual deployed version depends on whatever `latest` resolved to at deploy time. Confirm the real version against a running instance (`db._version()` or the web UI About page) before finalizing type/behavior mappings.
- Code targets ArangoDB 3.x specifically:
  - Explicit `WITH <collection>, ...` declarations (required in 3.x cluster mode)
  - 3.x-style multi-collection traversal syntax (`FOR v, e, p IN n..m OUTBOUND/INBOUND/ANY start edgeCol1, edgeCol2, ...`)
  - Uses the legacy `FULLTEXT()` index/function, **not** ArangoSearch views (`SEARCH` keyword, introduced 3.4) — suggests code predates or intentionally ignores ArangoSearch
  - `ensureSkiplistIndex`/`ensureFulltextIndex` (pyArango index API) — index types still supported in current 3.x but largely superseded (`skiplist` by `persistent`, `fulltext` deprecated-but-supported)
- Standard/stable AQL functions used throughout: `MERGE`, `FLATTEN`, `UNION_DISTINCT`, `MINUS`, `INTERSECTION`, `ATTRIBUTES`, `TRANSLATE`, `CONCAT_SEPARATOR`, `COLLECTION_COUNT`, `DOCUMENT()`, `SUBSTITUTE`, `SPLIT`, `UNIQUE` — no exotic version-gated features, nothing that should block a mapping to SQL equivalents.
- Copyright headers (2018 Cisco Systems in etl/web, 2019 in migrate) suggest the schema/query design dates to ArangoDB ~3.2-3.4 era.

## Summary for migration design

The schema is a clean star/graph of 11 vertex "entity" collections and 20 edge "relationship" collections, almost all with small fixed field sets — this maps naturally onto normalized Postgres tables with foreign keys (most edges become plain FK columns or two-column join tables). Two things need the most design care:

1. **The small number of true multi-hop AQL graph traversals** (§3) → recursive CTEs or fixed-depth JOIN chains in Postgres.
2. **`FULLTEXT()`-based search** (§4/§6) → either Postgres `tsvector`/`GIN` full-text, or simply keep relying on Elasticsearch as the search layer (arguably preferable, since Arango's fulltext search is already bypassed by ES for the primary search UI — see §7).

Other concrete migration tasks surfaced by this review:
- Two independent, hardcoded-credential connection code paths (pyArango in ETL, python-arango in web) both need rewriting against a Postgres client (e.g. `psycopg`/SQLAlchemy) — a good opportunity to also introduce env-var-based config and a shared connection helper, neither of which exists today.
- `Device`, `DeviceHasDataPath`, `DeviceHasDataModel`, `DataModelDerivedFrom` are defined but never populated — candidates to drop or defer rather than port.
- `DataPathParent`/`DataPathChild` and `DataModelParent`/`DataModelChild` are redundant direction-mirrored edge collections — collapse each pair into a single self-referencing FK or adjacency table.
- No edge-collection indexes exist beyond Arango's automatic `_from`/`_to` index — every join table in the new schema needs explicit FK indexes to avoid a performance regression.
- ETL currently writes row-by-row with no bulk insert API — Postgres migration can start at parity and later add bulk/COPY-based loading as a real improvement, not a requirement.
- No full-DB backup/restore tooling exists today (only a partial `DataPathMatch`/`Calculation` export) — worth deciding whether the Postgres migration should introduce proper `pg_dump`-based backups as part of the cutover.
