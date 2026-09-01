# Database
TDM's parsed source-of-truth resides in PostgreSQL. It's a normalized relational schema in a dedicated `tdm` schema (not `public`, so the database can host other apps alongside TDM) — entities are tables with surrogate `BIGINT` primary keys, and relationships between them are foreign key columns or small join tables.

[[toc]]

## Schema
TDM has a relatively simple schema.

![Database Schema Image](/doc/img/tdm_schema.png)

### Tables (Entities)

#### data_path
The most basic representation of a "path" to data which can be transformed and formatted for control protocols to retrieve the data.

* **machine_id**  
The path/identifier which is qualified, unique, and most used by definition in the machine. `machine_id` is `UNIQUE NOT NULL`.
* human_id  
The path/identifier which is colloquially used by humans to communicate the data path.
* description  
The description provided for the data to be returned.
* is_leaf  
Whether the data path returns a leaf-like value such as an integer. Indicates a pointer to data which is unable to be further traversed.
* is_variable  
Whether the data path is unable to be directly indexed.
* is_configurable  
Whether the data path represents something which is able to be configured. Effectively, a "write" property.
* verified  
Whether the data path has been verified and should absolutely be trusted. If it is not verified, there is a potential for it to be in error.
* parent_id  
Self-referencing foreign key to another `data_path` row, for parent/child DataPath relationships.
* data_type_id  
Foreign key to `data_type`.
* search_vector  
A `tsvector` `GENERATED ALWAYS AS ... STORED` column, weighted A/B/C over `human_id`/`machine_id`/`description`, backed by a GIN index.

#### data_model
The data model which provides the definition/schema of available data which we may derive DataPaths to.

* content  
The unparsed content of the data model. Carried forward for fidelity; unused by any current ETL path.
* **name**  
The name or filename of the data model. Combined with `revision`, `UNIQUE (name, revision)`.
* **parsed_checksum**  
The checksum of the data paths parsed from the data model. This is not currently implemented.
* **revision**  
The revision of the data model.
* data_model_language_id  
Foreign key to `data_model_language`.
* parent_id  
Self-referencing foreign key to another `data_model` row, for revision parent/child relationships.

#### data_model_language
The known and defined language of data modeling which a DataModel is written in.

* **name**
* description

#### os
The operating system which DataModels may apply to.

* **name**  
e.g. IOS XE
* description

#### release
The OS Release which DataModels may apply to.

* os_id  
Foreign key to `os`.
* **name**  
e.g. 6.5.1. Combined with `os_id`, `UNIQUE (os_id, name)`.
* description
* previous_release_id  
Self-referencing foreign key to the prior `release` row in the same OS's revision chain; walk it with a recursive CTE if a full chain is needed.

#### control_protocol
The known and defined protocol which is capable of transforming or utilizing DataModels or DataPaths to retrieve data.

* **name**  
e.g. NETCONF
* description

#### transport_protocol
The known and defined protocol which a ControlProtocol may operate over.

* **name**  
e.g. HTTP
* description

#### encoding
The encoding of the data which a DataPath is communicated via a ControlProtocol and over the TransportProtocol.

* **name**  
e.g. JSON
* description

#### data_type
The defined data type that a data point in a DataPath is defined to return.

* data_model_language_id  
Foreign key to `data_model_language`.
* **name**  
Combined with `data_model_language_id`, `UNIQUE (data_model_language_id, name)`.
* description
* is_primitive

#### calculation
A defined calculation which may be used to indicate that a DataPath is calculated via other DataPaths. This does not attempt to maintain order of operations. Order of operations must be maintained in the equation/description and will not automatically apply.

* **name**  
An apt naming for the calculation, for human consumption. `UNIQUE NOT NULL`.
* description
* equation
* author

### Relationships (Foreign Keys & Join Tables)

#### release.os_id
Indicates that it has been validated that an OS does have a specified Release.

#### release_data_model
Indication that, theoretically, a specific Release should have a DataModel. Two-column join table (`release_id`, `data_model_id`).

#### release.previous_release_id
Indicates that a Release is a revision of another Release.

#### data_path_source
Indicates that a specific DataPath is derivative of a certain DataModel. Join table with an extra `parse_timestamp TIMESTAMPTZ` column.

#### data_model.data_model_language_id
Indicates that a DataModel is written in the linked DataModelLanguage.

#### data_model_language_control_protocol
Indicates that a DataModelLanguage may be manipulated by the linked ControlProtocol. Join table.

#### control_protocol_encoding
Indicates that a ControlProtocol supports the linked Encoding. Join table.

#### control_protocol_transport_protocol
Indicates that a ControlProtocol supports the linked TransportProtocol. Join table.

#### data_path_match
Indicates that the linked DataPaths are equivalent. Logically undirected — enforced via `data_path_a_id`/`data_path_b_id` plus `CHECK (data_path_a_id < data_path_b_id)` and a `UNIQUE (data_path_a_id, data_path_b_id)` constraint, so `(A,B)` and `(B,A)` can't both be inserted.

* created_at  
Time of match indication. `TIMESTAMPTZ DEFAULT now()`.
* author  
Submitter of match.
* validated  
Whether a match is trustworthy.
* weight  
-1..+inf. -1 indicates incongruent.
* annotation  
Human consumable annotation of match.
* needs_human  
Indicates incompatible for machine consumption.

#### data_path.parent_id
Indicates that a DataPath is a parent/child of another DataPath.

#### data_path.data_type_id
Indicates that a DataPath is of data type DataType.

#### data_model.parent_id
Demonstrates revision parent/child relationships in DataModels.

#### calculation_input
Indicates that a DataPath is an input to the specified Calculation. Join table.

#### calculation_result
Indicates that a DataPath is a result of the specified Calculation. Join table.

## Example SQL
Queries below are drawn directly from `web/src/web/views.py` and `etl/src/search.py` where a matching implementation exists (noted per section), plus a few illustrative queries for common patterns that don't have a single dedicated function today. All examples assume the connection's `search_path` is set to `tdm` (see [Web](Web.html) and [ETL](ETL.html)), so table names are unqualified.

### Filtered DataPath Search
`fetch_search_data_paths` (`web/src/web/views.py:656`) is what backs the primary structured search API (`/api/v1/search`). It filters on `(os, release)` pairs, DataModelLanguage names, leaf/configurable flags, and — when a filter string is given — full-text search over `data_path.search_vector` using `plainto_tsquery`.

```sql
SELECT os.name AS os_name, release.name AS release_name,
       dml.name AS dml_name, dm.name AS dm_name,
       dp.data_path_id, dp.human_id
FROM data_path dp
JOIN data_path_source dps ON dps.data_path_id = dp.data_path_id
JOIN data_model dm ON dm.data_model_id = dps.data_model_id
JOIN data_model_language dml ON dml.data_model_language_id = dm.data_model_language_id
JOIN release_data_model rdm ON rdm.data_model_id = dm.data_model_id
JOIN release ON release.release_id = rdm.release_id
JOIN os ON os.os_id = release.os_id
WHERE (os.name, release.name) IN (('IOS XR', '6.3.1'))
  AND dml.name = ANY(ARRAY['YANG'])
  AND dp.is_leaf = TRUE
  AND dp.search_vector @@ plainto_tsquery('simple', 'openconfig interface')
  AND dp.is_configurable = FALSE
ORDER BY dp.human_id
LIMIT 3 OFFSET 0
```

Rows come back flat; the view groups them into an `OS → Release → DataModelLanguage → DataModel → [DataPath]` nested dict in Python (`views.py:703-709`).

#### Output
```json
{
  "IOS XR": {
    "6.3.1": {
      "YANG": {
        "openconfig-interfaces": [
          {"data_path_id": 937777, "human_id": "openconfig-interfaces:interfaces/interface/aggregation/state/lag-speed"}
        ]
      }
    }
  }
}
```

### OS and Releases
Backs the search form's OS/Release picker. `fetch_os_releases` (`web/src/web/views.py:722`):

```sql
SELECT os.name AS os_name, release.name AS release_name
FROM os
JOIN release USING (os_id)
ORDER BY os.name ASC, release.name DESC
```

#### Output
```
IOS XE - 16.7.1
IOS XE - 16.6.2
IOS XR - 6.3.1
IOS XR - 5.3.0
```

### OS/Release Owned DataModels
Illustrative — walks the same join path as the search query above but stops at `data_model`, without touching `data_path`:

```sql
SELECT os.name AS os_name, release.name AS release_name, dm.name, dm.revision
FROM data_model dm
JOIN release_data_model rdm ON rdm.data_model_id = dm.data_model_id
JOIN release ON release.release_id = rdm.release_id
JOIN os ON os.os_id = release.os_id
WHERE os.name = 'IOS XE' AND release.name = '16.7.1'
ORDER BY dm.name
LIMIT 5
```

### Common OS/Release DataPaths
Intersects the DataPaths belonging to two different OS/Release pairs, using `INTERSECT`:

```sql
SELECT dp.human_id
FROM data_path dp
JOIN data_path_source dps ON dps.data_path_id = dp.data_path_id
JOIN release_data_model rdm ON rdm.data_model_id = dps.data_model_id
JOIN release ON release.release_id = rdm.release_id
JOIN os ON os.os_id = release.os_id
WHERE os.name = 'IOS XE' AND release.name = '16.7.1'

INTERSECT

SELECT dp.human_id
FROM data_path dp
JOIN data_path_source dps ON dps.data_path_id = dp.data_path_id
JOIN release_data_model rdm ON rdm.data_model_id = dps.data_model_id
JOIN release ON release.release_id = rdm.release_id
JOIN os ON os.os_id = release.os_id
WHERE os.name = 'IOS XR' AND release.name = '6.3.1'
```

### Retrieve Matching DataPaths
Backs Matchmaker (`/matches`). `fetch_matches` (`web/src/web/views.py:266`) resolves each given `human_id`/`machine_id` to a `data_path_id`, then per DataPath:

```sql
SELECT other.data_path_id, other.human_id, other.machine_id
FROM data_path_match dpm
JOIN data_path other ON other.data_path_id =
    CASE WHEN dpm.data_path_a_id = %(id)s
         THEN dpm.data_path_b_id ELSE dpm.data_path_a_id END
WHERE dpm.data_path_a_id = %(id)s OR dpm.data_path_b_id = %(id)s
ORDER BY other.human_id
```

The `CASE`/`OR` pair reflects that `data_path_match` stores each pair once, undirected (`a_id < b_id`), rather than as two separate directed rows.

### Retrieve DataPath Calculations
Backs `/calculations*`. `_calcs_as_result`/`_calcs_as_factor` (`web/src/web/views.py:319` and `:340`) use two focused queries per direction:

```sql
-- Calculations where this DataPath is the result
SELECT calc.calculation_id, calc.name
FROM calculation_result cr
JOIN calculation calc USING (calculation_id)
WHERE cr.data_path_id = %s;

-- ...then, per calculation, its inputs:
SELECT factor.data_path_id, factor.human_id, factor.machine_id
FROM calculation_input ci
JOIN data_path factor ON factor.data_path_id = ci.data_path_id
WHERE ci.calculation_id = %s;
```

### Retrieve Unmatched DataPaths
Illustrative — DataPaths given by `human_id` that have neither a `data_path_match` row nor a `calculation_result` row:

```sql
SELECT dp.human_id
FROM data_path dp
WHERE dp.human_id = ANY(%(given_dps)s)
  AND NOT EXISTS (
      SELECT 1 FROM data_path_match dpm
      WHERE dpm.data_path_a_id = dp.data_path_id OR dpm.data_path_b_id = dp.data_path_id
  )
  AND NOT EXISTS (
      SELECT 1 FROM calculation_result cr WHERE cr.data_path_id = dp.data_path_id
  )
```

### Retrieve Table Counts
Backs the home page stats. `fetch_collection_counts` (`web/src/web/views.py:446`) issues one `COUNT(*)` per requested table name, resolved through a fixed allow-list (`_COLLECTION_COUNT_TABLES`, `views.py:440`) rather than interpolating client-supplied table names directly:

```sql
SELECT COUNT(*) AS count FROM data_path;
SELECT COUNT(*) AS count FROM data_model;
SELECT COUNT(*) AS count FROM release;
```

#### Output
```json
{"DataPath": 554322, "DataModel": 1760, "Release": 31}
```

### Retrieve OS/Releases linked to a DataPath
Backs the DataPath detail page. `fetch_datapath_os_graph` (`web/src/web/views.py:125`) is a fixed-depth join chain, since the DataPath → DataModel → Release → OS depth is always known:

```sql
SELECT dm.name AS datamodel_name, dm.revision AS datamodel_revision,
       release.name AS os_release, os.name AS os_name
FROM data_path_source dps
JOIN data_model dm ON dm.data_model_id = dps.data_model_id
JOIN release_data_model rdm ON rdm.data_model_id = dm.data_model_id
JOIN release ON release.release_id = rdm.release_id
JOIN os ON os.os_id = release.os_id
WHERE dps.data_path_id = %s
```

### Retrieve per-DataModelLanguage DataPaths with Matches
Backs the "All Mappings" page. `fetch_all_matches` (`web/src/web/views.py:46`) joins DataModelLanguage down to DataPath, then self-joins `data_path_match`:

```sql
SELECT DISTINCT dml.name AS dml_name, dp.data_path_id, dp.human_id
FROM data_model_language dml
JOIN data_model dm ON dm.data_model_language_id = dml.data_model_language_id
JOIN data_path_source dps ON dps.data_model_id = dm.data_model_id
JOIN data_path dp ON dp.data_path_id = dps.data_path_id
JOIN data_path_match dpm
    ON dpm.data_path_a_id = dp.data_path_id OR dpm.data_path_b_id = dp.data_path_id
ORDER BY dml.name, dp.human_id
```

### Flattening for Elasticsearch
The ETL's search-indexing stage flattens `DataPath` × `OS`/`Release` permutations for Elasticsearch with a single streamed SQL query, `DATAPATH_QUERY` (`etl/src/search.py:23`), read via a named/server-side cursor (`itersize = 1000`) since the result set is on the order of millions of rows:

```sql
SELECT
    dp.data_path_id AS dp_key,
    dp.machine_id AS dp_machine_id,
    dp.human_id AS dp_human_id,
    dp.description AS dp_description,
    dp.is_leaf AS dp_is_leaf,
    dp.is_configurable AS dp_is_configurable,
    dml.data_model_language_id AS dml_key,
    dml.name AS dml_name,
    dm.data_model_id AS dm_key,
    dm.name AS dm_name,
    dm.revision AS dm_revision,
    r.release_id AS release_key,
    r.name AS release_name,
    os.os_id AS os_key,
    os.name AS os_name
FROM data_path dp
JOIN data_path_source dps ON dps.data_path_id = dp.data_path_id
JOIN data_model dm ON dm.data_model_id = dps.data_model_id
JOIN data_model_language dml ON dml.data_model_language_id = dm.data_model_language_id
LEFT JOIN release_data_model rdm ON rdm.data_model_id = dm.data_model_id
LEFT JOIN release r ON r.release_id = rdm.release_id
LEFT JOIN os ON os.os_id = r.os_id
```

See [Search](Search.html) for how this feeds the Elasticsearch index, and how it relates to PostgreSQL's own full-text search via `data_path.search_vector`.

## ArangoDB → PostgreSQL Migration
TDM originally stored its source-of-truth in [ArangoDB](https://arangodb.com/), a graph database, and moved to PostgreSQL as part of the v3 release. This section is a reference for anyone working with data, backups, or code that predates the migration; it isn't needed to understand the current schema above.

### Why
ArangoDB's schema was a graph of 11 vertex ("entity") collections and 20 edge ("relationship") collections. Almost all of TDM's actual queries were manual `FOR ... FILTER x._from == y._id` joins rather than native graph traversals — only a handful of queries (the OS/Release/DataModel graph lookups) did true multi-hop traversal — so the schema didn't need ArangoDB's graph capabilities in practice, and a normalized relational schema maps onto the same structure with plain foreign keys and join tables.

### Collection/Edge → Table Mapping

| ArangoDB collection/edge | PostgreSQL table | Notes |
|---|---|---|
| `OS` | `os` | |
| `Release` | `release` | |
| `OSHasRelease` (edge) | `release.os_id` | Collapsed to a foreign key column. |
| `ReleaseRevision` (edge) | `release.previous_release_id` | Collapsed a chain of edges into one self-referencing FK. |
| `DataModelLanguage` | `data_model_language` | |
| `ControlProtocol` | `control_protocol` | |
| `TransportProtocol` | `transport_protocol` | |
| `Encoding` | `encoding` | |
| `HasControlProtocol` (edge) | `data_model_language_control_protocol` | Join table. |
| `HasEncoding` (edge) | `control_protocol_encoding` | Join table. |
| `HasTransportProtocol` (edge) | `control_protocol_transport_protocol` | Join table. |
| `DataType` | `data_type` | |
| `DataModelLanguageHasDataType` (edge) | *(dropped)* | Redundant with `data_type.data_model_language_id`, which already ties every DataType to exactly one DataModelLanguage. |
| `DataModel` | `data_model` | |
| `OfDataModelLanguage` (edge) | `data_model.data_model_language_id` | Collapsed to a FK — every DataModel is written in exactly one language in practice. |
| `DataModelParent` + `DataModelChild` (edges) | `data_model.parent_id` | These stored the same revision-chain relationship twice, once per direction; collapsed into one self-referencing FK. |
| `DataModelDerivedFrom` (edge) | *(not created)* | Never populated by any ETL path in the old schema either. A commented-out `ALTER TABLE data_model ADD COLUMN derived_from_id ...` is ready in `etl/src/schema.sql` if a real need shows up. |
| `ReleaseHasDataModel` (edge) | `release_data_model` | Join table. |
| `DataPath` | `data_path` | |
| `DataPathFromDataModel` (edge) | `data_path_source` | Join table; its `parse_timestamp` field was never actually populated then, and still isn't now. |
| `DataPathParent` + `DataPathChild` (edges) | `data_path.parent_id` | Same direction-mirrored collapse as DataModel's parent/child. |
| `OfDataType` (edge) | `data_path.data_type_id` | Collapsed to a FK — observed to be functionally 1:1 in every write path. |
| `DataPathMatch` (edge) | `data_path_match` | Was queried with `ANY` in AQL (logically undirected); PostgreSQL enforces that directly with `CHECK (data_path_a_id < data_path_b_id)` plus a `UNIQUE` pair, instead of allowing both directions to be inserted. Its `timestamp` (a Unix epoch float) became `created_at TIMESTAMPTZ`. |
| `Calculation` | `calculation` | |
| `InCalculation` (edge) | `calculation_input` | Join table. |
| `CalculationResult` (edge) | `calculation_result` | Join table. |
| `Device`, `DeviceHasDataPath` (edge), `DeviceHasDataModel` (edge) | *(not created)* | Defined in ArangoDB but never populated by any ETL path. Commented out at the bottom of `etl/src/schema.sql`; uncomment if device-inventory tracking becomes a real requirement — there's no existing data to migrate for it either way. |

### Other Notable Changes
* **Full-text search**: ArangoDB's three separate `FULLTEXT()` indexes (on `machine_id`, `human_id`, `description`) became one weighted, generated `tsvector` column plus a GIN index on `data_path`.
* **No more separate `_key`/slug column**: ArangoDB's human-readable `_key` values (e.g. `"IOS_XE+16.7.1"`) aren't carried forward as their own column — every table already has a `UNIQUE` column or column combination (e.g. `release`'s `UNIQUE (os_id, name)`) that dedupes the same way.
* **Explicit indexes on every foreign key**: ArangoDB gave every edge collection a free automatic `_from`/`_to` index. PostgreSQL doesn't index FK columns automatically, so every join table and FK column in the new schema has an explicit `CREATE INDEX` to avoid a performance regression.
* **One database client instead of two**: ETL previously used `pyArango` and Web used `python-arango` — two independent, never-consolidated client libraries with separate hardcoded-credential connection code. Both now use `psycopg2`, and Web centralizes connections through a single pooled helper (`web/src/web/db.py`) instead of opening ad hoc clients at each call site.
