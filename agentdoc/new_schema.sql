-- =============================================================================
-- TDM — PostgreSQL Schema Translation (from ArangoDB)
-- =============================================================================
-- Companion to agentdoc/existing_schema.aql and agentdoc/current_db_context.md.
-- Every table below is annotated with the ArangoDB collection/edge it replaces.
-- Target: PostgreSQL 14+ (uses GENERATED ALWAYS AS IDENTITY, generated tsvector
-- columns).
--
-- DESIGN DECISIONS THAT DEPART FROM A LITERAL 1:1 TRANSLATION
-- -----------------------------------------------------------------------------
-- 1. Surrogate keys: every table gets a BIGINT IDENTITY primary key, named
--    after its table (e.g. `os.os_id`, `release.release_id`) so a foreign key
--    column always matches the primary key it points to — no id/os_id
--    juggling at the join site, and it lets joins in the query notes below
--    use `JOIN ... USING (col)` instead of spelling out `ON a.x = b.x`.
--    Arango's human-readable `_key` values (e.g. "IOS_XE", "IOS_XE+16.7.1")
--    are NOT carried forward as a separate `slug` column anywhere — every
--    table already has a column, or combination of columns, that dedupes
--    the same way (see point 8), so a redundant slug would just be a second
--    constraint guarding the same fact. Lookups go through the surrogate id
--    or through the existing UNIQUE column(s).
-- 2. Direction-mirrored edge pairs are collapsed to one self-referencing FK:
--      - DataPathParent + DataPathChild        -> data_path.parent_id
--      - DataModelParent + DataModelChild      -> data_model.parent_id
--      - ReleaseRevision (chain)               -> release.previous_release_id
--    ArangoDB stored the same relationship twice (once per traversal
--    direction); Postgres doesn't need that, a single FK is traversable both
--    ways with a plain join or a recursive CTE.
-- 3. Edges that are functionally 1:1 in every observed write path collapse to
--    a plain FK column instead of a join table:
--      - OfDataType        -> data_path.data_type_id
--    Edges that are genuinely many-to-many keep a join table:
--      - DataPathFromDataModel -> data_path_source (has parse_timestamp)
--      - ReleaseHasDataModel   -> release_data_model
--      - OfDataModelLanguage   -> data_model.data_model_language_id (FK — every
--        DataModel is written in exactly one language in practice)
--      - HasControlProtocol / HasEncoding / HasTransportProtocol -> join tables
--      - InCalculation / CalculationResult -> join tables (kept separate,
--        since a Calculation can have many inputs and — per the ArangoDB
--        model — the schema doesn't prevent multiple result paths either)
--      - DataPathMatch -> data_path_match (undirected pair, enforced via a
--        CHECK constraint ordering the two FKs, see below)
-- 4. Never-populated collections/edges (Device, DeviceHasDataPath,
--    DeviceHasDataModel, DataModelDerivedFrom) are moved to a separate
--    "DEFERRED" section at the bottom — not created by default. Add them back
--    if/when a real use case for device inventory shows up; carrying dead
--    tables into the new system for parity alone isn't worth it.
-- 5. Every FK column has an explicit index. ArangoDB gave every edge
--    collection a free automatic _from/_to index; Postgres does not index FK
--    columns automatically, so skipping this would be a real performance
--    regression on every join the web app currently relies on.
-- 6. The three legacy ArangoDB `FULLTEXT()` indexes (machine_id, human_id,
--    description) become one weighted `tsvector` generated column + GIN index
--    on data_path. This is a genuine behavior change (token/prefix semantics
--    differ from Arango's fulltext analyzer) and should be validated against
--    real search queries — see current_db_context.md §6-7 for why Elasticsearch
--    may remain the better home for the user-facing search UI regardless.
-- 7. `DataPathMatch.timestamp` (Unix epoch float from Python `time.time()`)
--    becomes `TIMESTAMPTZ DEFAULT now()` — same information, native type.
-- 8. No table carries a separate `slug` column — every Arango `_key` is
--    already reproducible from columns the table needs anyway:
--      - `release` ("IOS_XE+16.7.1" = os + name) -> `UNIQUE (os_id, name)`
--      - `data_model` ("<name>+<revision>")       -> `UNIQUE (name, revision)`
--      - `data_type` ("<language>+<type>")        -> `UNIQUE (data_model_language_id, name)`
--      - `os`, `data_model_language`, `control_protocol`,
--        `transport_protocol`, `encoding` (bare keys like "IOS_XE", "YANG",
--        "gRPC") -> `UNIQUE (name)`. These are flat lookup tables with
--        nothing else to compose a key from, but `name` alone already *is*
--        the `_key` value (same string, just not artificially separated
--        into its own column) — so making it UNIQUE captures the identical
--        constraint a `slug` column would, without storing the value twice.
-- 9. Everything lives in a `tdm` schema, not `public`, so the database can
--    host other apps' schemas alongside this one without name collisions.
--    The ETL connects with search_path=tdm, so unqualified table references
--    throughout etl/src/*.py resolve here without further changes.
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS tdm;

-- -----------------------------------------------------------------------------
-- LOOKUP / REFERENCE TABLES
-- (was: OS, Release, DataModelLanguage, ControlProtocol, TransportProtocol,
--  Encoding — all populated by etl/src/static.py from hardcoded Python dicts)
-- -----------------------------------------------------------------------------

CREATE TABLE os (
    os_id       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,   -- was OS._key, e.g. "IOS_XE", "IOS_XR", "NX-OS"
    description TEXT
);

CREATE TABLE release (
    release_id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    os_id                BIGINT NOT NULL REFERENCES os (os_id),
    name                 TEXT NOT NULL,
    description          TEXT,
    -- collapses ReleaseRevision edge (Release -> Release, prev -> next) into
    -- a self-referencing FK; walk the chain with a recursive CTE if needed.
    previous_release_id  BIGINT REFERENCES release (release_id),
    -- was Release._key, e.g. "IOS_XE+16.7.1" — dropped as a separate slug
    -- column; (os_id, name) already deduplicates the same way (see point 8).
    UNIQUE (os_id, name)
);
CREATE INDEX idx_release_os_id ON release (os_id);
CREATE INDEX idx_release_previous_release_id ON release (previous_release_id);

CREATE TABLE data_model_language (
    data_model_language_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name                    TEXT NOT NULL UNIQUE,   -- was DataModelLanguage._key, e.g. "YANG", "SMI"
    description             TEXT
);

CREATE TABLE control_protocol (
    control_protocol_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name                 TEXT NOT NULL UNIQUE,   -- e.g. "gRPC", "NETCONF", "SNMP", "MDT", "RESTCONF"
    description          TEXT
);

CREATE TABLE transport_protocol (
    transport_protocol_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name                    TEXT NOT NULL UNIQUE,   -- e.g. "TCP", "UDP", "SSH", "Telnet", "HTTP"
    description             TEXT
);

CREATE TABLE encoding (
    encoding_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name         TEXT NOT NULL UNIQUE,   -- e.g. "JSON", "XML", "GPB", "KV-GPB", "BER", "Text"
    description  TEXT
);

CREATE TABLE data_type (
    data_type_id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    data_model_language_id BIGINT NOT NULL REFERENCES data_model_language (data_model_language_id),
    name                    TEXT NOT NULL,
    description             TEXT,
    is_primitive            BOOLEAN NOT NULL DEFAULT FALSE,
    -- was DataType._key = "<DataModelLanguage>+<type>" e.g. "YANG+string"
    UNIQUE (data_model_language_id, name)
);
CREATE INDEX idx_data_type_data_model_language_id ON data_type (data_model_language_id);


-- -----------------------------------------------------------------------------
-- M:N EDGE TABLES BETWEEN LOOKUP TABLES
-- (was: HasControlProtocol, HasEncoding, HasTransportProtocol,
--  DataModelLanguageHasDataType — all static, populated by static.py)
-- -----------------------------------------------------------------------------

CREATE TABLE data_model_language_control_protocol (
    data_model_language_id BIGINT NOT NULL REFERENCES data_model_language (data_model_language_id),
    control_protocol_id    BIGINT NOT NULL REFERENCES control_protocol (control_protocol_id),
    PRIMARY KEY (data_model_language_id, control_protocol_id)
);
CREATE INDEX idx_dmlcp_control_protocol_id ON data_model_language_control_protocol (control_protocol_id);

CREATE TABLE control_protocol_encoding (
    control_protocol_id BIGINT NOT NULL REFERENCES control_protocol (control_protocol_id),
    encoding_id          BIGINT NOT NULL REFERENCES encoding (encoding_id),
    PRIMARY KEY (control_protocol_id, encoding_id)
);
CREATE INDEX idx_cpe_encoding_id ON control_protocol_encoding (encoding_id);

CREATE TABLE control_protocol_transport_protocol (
    control_protocol_id   BIGINT NOT NULL REFERENCES control_protocol (control_protocol_id),
    transport_protocol_id BIGINT NOT NULL REFERENCES transport_protocol (transport_protocol_id),
    PRIMARY KEY (control_protocol_id, transport_protocol_id)
);
CREATE INDEX idx_cptp_transport_protocol_id ON control_protocol_transport_protocol (transport_protocol_id);

-- NOTE: DataModelLanguageHasDataType is redundant with data_type's own
-- data_model_language_id FK above (every DataType already belongs to exactly
-- one DataModelLanguage) — intentionally not carried forward as a separate
-- join table.


-- -----------------------------------------------------------------------------
-- CORE ENTITY TABLES: DataModel, DataPath
-- -----------------------------------------------------------------------------

CREATE TABLE data_model (
    data_model_id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    data_model_language_id  BIGINT NOT NULL REFERENCES data_model_language (data_model_language_id), -- was OfDataModelLanguage edge
    name                     TEXT NOT NULL,
    revision                 TEXT NOT NULL,
    content                  TEXT,   -- carried forward for fidelity; unused in every current ETL path
    parsed_checksum          TEXT,   -- carried forward for fidelity; unused in every current ETL path
    -- collapses DataModelParent + DataModelChild (revision chain) into one FK
    parent_id                BIGINT REFERENCES data_model (data_model_id),
    -- was DataModel._key = "<name>+<revision>" (YANG) or bare model name (SNMP)
    -- — no separate slug column; this UNIQUE already provides the same dedup.
    UNIQUE (name, revision)
);
CREATE INDEX idx_data_model_data_model_language_id ON data_model (data_model_language_id);
CREATE INDEX idx_data_model_parent_id ON data_model (parent_id);

CREATE TABLE release_data_model (
    -- was ReleaseHasDataModel edge (Release -> DataModel)
    release_id    BIGINT NOT NULL REFERENCES release (release_id),
    data_model_id BIGINT NOT NULL REFERENCES data_model (data_model_id),
    PRIMARY KEY (release_id, data_model_id)
);
CREATE INDEX idx_rdm_data_model_id ON release_data_model (data_model_id);

CREATE TABLE data_path (
    data_path_id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    machine_id       TEXT NOT NULL UNIQUE,  -- was the unique skiplist index in Arango
    human_id         TEXT,
    description      TEXT,
    is_leaf          BOOLEAN NOT NULL DEFAULT FALSE,
    is_variable      BOOLEAN NOT NULL DEFAULT FALSE,  -- never set true by any current writer
    is_configurable  BOOLEAN NOT NULL DEFAULT FALSE,
    verified         BOOLEAN NOT NULL DEFAULT FALSE,  -- never set true by any current writer
    -- collapses DataPathParent + DataPathChild into one FK
    parent_id        BIGINT REFERENCES data_path (data_path_id),
    -- collapses OfDataType edge (observed as functionally 1:1)
    data_type_id     BIGINT REFERENCES data_type (data_type_id),
    -- replaces the 3 separate ArangoDB FULLTEXT() indexes (machine_id,
    -- human_id, description) with one weighted full-text search column.
    search_vector    tsvector GENERATED ALWAYS AS (
                         setweight(to_tsvector('simple', coalesce(human_id, '')), 'A') ||
                         setweight(to_tsvector('simple', coalesce(machine_id, '')), 'B') ||
                         setweight(to_tsvector('simple', coalesce(description, '')), 'C')
                     ) STORED
);
CREATE INDEX idx_data_path_parent_id ON data_path (parent_id);
CREATE INDEX idx_data_path_data_type_id ON data_path (data_type_id);
CREATE INDEX idx_data_path_search_vector ON data_path USING GIN (search_vector);

CREATE TABLE data_path_source (
    -- was DataPathFromDataModel edge (DataModel -> DataPath)
    data_path_id     BIGINT NOT NULL REFERENCES data_path (data_path_id),
    data_model_id    BIGINT NOT NULL REFERENCES data_model (data_model_id),
    parse_timestamp  TIMESTAMPTZ,  -- field existed in Arango but was never actually populated
    PRIMARY KEY (data_path_id, data_model_id)
);
CREATE INDEX idx_dps_data_model_id ON data_path_source (data_model_id);


-- -----------------------------------------------------------------------------
-- USER-CURATED DATA
-- (was: DataPathMatch, Calculation, InCalculation, CalculationResult —
--  the only data NOT wholesale-regenerated by re-running the ETL; see
--  migrate/1_to_2/ precedent for why this boundary matters)
-- -----------------------------------------------------------------------------

CREATE TABLE data_path_match (
    data_path_match_id  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    data_path_a_id       BIGINT NOT NULL REFERENCES data_path (data_path_id),
    data_path_b_id       BIGINT NOT NULL REFERENCES data_path (data_path_id),
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),  -- was 'timestamp' (Unix epoch float)
    author                 TEXT,
    validated              BOOLEAN NOT NULL DEFAULT FALSE,
    weight                 INTEGER NOT NULL DEFAULT 0,  -- -1..+inf; -1 == "incongruent" per original code comment
    annotation             TEXT,
    needs_human            BOOLEAN NOT NULL DEFAULT TRUE,
    -- DataPathMatch was logically undirected (app code queries it with `ANY`);
    -- enforce a canonical ordering so (A,B) and (B,A) can't both be inserted.
    CONSTRAINT chk_data_path_match_ordered CHECK (data_path_a_id < data_path_b_id),
    UNIQUE (data_path_a_id, data_path_b_id)
);
CREATE INDEX idx_dpm_data_path_b_id ON data_path_match (data_path_b_id);

CREATE TABLE calculation (
    calculation_id  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name             TEXT NOT NULL,
    description      TEXT,
    equation         TEXT,  -- free text; order of operations not parsed/enforced, same as original
    author           TEXT
);

CREATE TABLE calculation_input (
    -- was InCalculation edge (DataPath -> Calculation)
    data_path_id    BIGINT NOT NULL REFERENCES data_path (data_path_id),
    calculation_id  BIGINT NOT NULL REFERENCES calculation (calculation_id),
    PRIMARY KEY (data_path_id, calculation_id)
);
CREATE INDEX idx_ci_calculation_id ON calculation_input (calculation_id);

CREATE TABLE calculation_result (
    -- was CalculationResult edge (Calculation -> DataPath)
    calculation_id  BIGINT NOT NULL REFERENCES calculation (calculation_id),
    data_path_id    BIGINT NOT NULL REFERENCES data_path (data_path_id),
    PRIMARY KEY (calculation_id, data_path_id)
);
CREATE INDEX idx_cr_data_path_id ON calculation_result (data_path_id);


-- =============================================================================
-- DEFERRED — never populated by any current ETL or web code path.
-- Not created by default. Uncomment if/when device-inventory tracking becomes
-- a real requirement; there's no existing data to migrate for these.
-- (was: Device, DeviceHasDataPath, DeviceHasDataModel, DataModelDerivedFrom)
-- =============================================================================

-- CREATE TABLE device (
--     device_id   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
--     name        TEXT NOT NULL,
--     description TEXT
-- );
--
-- CREATE TABLE device_data_path (
--     device_id     BIGINT NOT NULL REFERENCES device (device_id),
--     data_path_id  BIGINT NOT NULL REFERENCES data_path (data_path_id),
--     os_id         BIGINT NOT NULL REFERENCES os (os_id),
--     release_id    BIGINT NOT NULL REFERENCES release (release_id),
--     PRIMARY KEY (device_id, data_path_id)
-- );
--
-- CREATE TABLE device_data_model (
--     device_id      BIGINT NOT NULL REFERENCES device (device_id),
--     data_model_id  BIGINT NOT NULL REFERENCES data_model (data_model_id),
--     os_id          BIGINT NOT NULL REFERENCES os (os_id),
--     release_id     BIGINT NOT NULL REFERENCES release (release_id),
--     PRIMARY KEY (device_id, data_model_id)
-- );
--
-- ALTER TABLE data_model ADD COLUMN derived_from_id BIGINT REFERENCES data_model (data_model_id);


-- =============================================================================
-- QUERY TRANSLATION NOTES (see current_db_context.md §3-4 for the AQL originals)
-- -----------------------------------------------------------------------------
-- Most AQL in web/src/web/views.py is a manual `FOR ... FILTER x._from == y._id`
-- join and translates directly to a SQL JOIN, e.g.:
--   fetch_datapath_parent   -> SELECT * FROM data_path WHERE data_path_id = (SELECT parent_id FROM data_path WHERE data_path_id = $1)
--   fetch_datapath_children -> SELECT * FROM data_path WHERE parent_id = $1
--   fetch_datapath_datatype -> SELECT dt.* FROM data_type dt JOIN data_path dp USING (data_type_id) WHERE dp.data_path_id = $1
--   fetch_datapath_mappings -> SELECT * FROM data_path_match WHERE data_path_a_id = $1 OR data_path_b_id = $1
--
-- The few true multi-hop AQL graph traversals become fixed-depth JOIN chains,
-- since the depth is always known/fixed (3 hops OS->Release->DataModel->DataPath,
-- or 2 hops DataModelLanguage->DataModel->DataPath) — no recursion needed:
--   fetch_datapath_os_graph  ->
--     SELECT os.*, r.*, dm.*
--     FROM data_path dp
--     JOIN data_path_source dps USING (data_path_id)
--     JOIN data_model dm USING (data_model_id)
--     JOIN release_data_model rdm USING (data_model_id)
--     JOIN release r USING (release_id)
--     JOIN os USING (os_id)
--     WHERE dp.data_path_id = $1;
--
-- The revision chains (release.previous_release_id, data_model.parent_id) are
-- the only places an actual recursive CTE is warranted, e.g.:
--   WITH RECURSIVE chain AS (
--     SELECT * FROM data_model WHERE data_model_id = $1
--     UNION ALL
--     SELECT dm.* FROM data_model dm JOIN chain c ON dm.data_model_id = c.parent_id
--   )
--   SELECT * FROM chain;
-- =============================================================================
