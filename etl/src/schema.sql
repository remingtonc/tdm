-- TDM PostgreSQL schema. Executed once by models.create_schema() on first run.
-- Full design rationale for every decision below lives in agentdoc/new_schema.sql
-- (this file is the runtime copy the ETL actually executes).
-- Lives outside `public` so it can coexist with other apps in the same database;
-- the connection's search_path (set in main.connect()) points unqualified
-- table references here.

CREATE SCHEMA IF NOT EXISTS tdm;

CREATE TABLE os (
    os_id       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    description TEXT
);

CREATE TABLE release (
    release_id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    os_id                BIGINT NOT NULL REFERENCES os (os_id),
    name                 TEXT NOT NULL,
    description          TEXT,
    -- collapses the ReleaseRevision edge chain into one self-referencing FK
    previous_release_id  BIGINT REFERENCES release (release_id),
    UNIQUE (os_id, name)
);
CREATE INDEX idx_release_os_id ON release (os_id);
CREATE INDEX idx_release_previous_release_id ON release (previous_release_id);

CREATE TABLE data_model_language (
    data_model_language_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name                    TEXT NOT NULL UNIQUE,
    description             TEXT
);

CREATE TABLE control_protocol (
    control_protocol_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name                 TEXT NOT NULL UNIQUE,
    description          TEXT
);

CREATE TABLE transport_protocol (
    transport_protocol_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name                    TEXT NOT NULL UNIQUE,
    description             TEXT
);

CREATE TABLE encoding (
    encoding_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name         TEXT NOT NULL UNIQUE,
    description  TEXT
);

CREATE TABLE data_type (
    data_type_id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    data_model_language_id BIGINT NOT NULL REFERENCES data_model_language (data_model_language_id),
    name                    TEXT NOT NULL,
    description             TEXT,
    is_primitive            BOOLEAN NOT NULL DEFAULT FALSE,
    UNIQUE (data_model_language_id, name)
);
CREATE INDEX idx_data_type_data_model_language_id ON data_type (data_model_language_id);

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

CREATE TABLE data_model (
    data_model_id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    data_model_language_id  BIGINT NOT NULL REFERENCES data_model_language (data_model_language_id),
    name                     TEXT NOT NULL,
    revision                 TEXT NOT NULL,
    content                  TEXT,
    parsed_checksum          TEXT,
    -- collapses DataModelParent + DataModelChild (revision chain) into one FK
    parent_id                BIGINT REFERENCES data_model (data_model_id),
    UNIQUE (name, revision)
);
CREATE INDEX idx_data_model_data_model_language_id ON data_model (data_model_language_id);
CREATE INDEX idx_data_model_parent_id ON data_model (parent_id);

CREATE TABLE release_data_model (
    release_id    BIGINT NOT NULL REFERENCES release (release_id),
    data_model_id BIGINT NOT NULL REFERENCES data_model (data_model_id),
    PRIMARY KEY (release_id, data_model_id)
);
CREATE INDEX idx_rdm_data_model_id ON release_data_model (data_model_id);

CREATE TABLE data_path (
    data_path_id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    machine_id       TEXT NOT NULL UNIQUE,
    human_id         TEXT,
    description      TEXT,
    is_leaf          BOOLEAN NOT NULL DEFAULT FALSE,
    is_variable      BOOLEAN NOT NULL DEFAULT FALSE,
    is_configurable  BOOLEAN NOT NULL DEFAULT FALSE,
    verified         BOOLEAN NOT NULL DEFAULT FALSE,
    -- collapses DataPathParent + DataPathChild into one FK
    parent_id        BIGINT REFERENCES data_path (data_path_id),
    -- collapses the OfDataType edge (observed as functionally 1:1)
    data_type_id     BIGINT REFERENCES data_type (data_type_id),
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
    data_path_id     BIGINT NOT NULL REFERENCES data_path (data_path_id),
    data_model_id    BIGINT NOT NULL REFERENCES data_model (data_model_id),
    parse_timestamp  TIMESTAMPTZ,
    PRIMARY KEY (data_path_id, data_model_id)
);
CREATE INDEX idx_dps_data_model_id ON data_path_source (data_model_id);

CREATE TABLE data_path_match (
    data_path_match_id  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    data_path_a_id       BIGINT NOT NULL REFERENCES data_path (data_path_id),
    data_path_b_id       BIGINT NOT NULL REFERENCES data_path (data_path_id),
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    author                 TEXT,
    validated              BOOLEAN NOT NULL DEFAULT FALSE,
    weight                 INTEGER NOT NULL DEFAULT 0,
    annotation             TEXT,
    needs_human            BOOLEAN NOT NULL DEFAULT TRUE,
    -- DataPathMatch was logically undirected; a canonical ordering keeps
    -- (A,B) and (B,A) from both being inserted.
    CONSTRAINT chk_data_path_match_ordered CHECK (data_path_a_id < data_path_b_id),
    UNIQUE (data_path_a_id, data_path_b_id)
);
CREATE INDEX idx_dpm_data_path_b_id ON data_path_match (data_path_b_id);

CREATE TABLE calculation (
    calculation_id  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name             TEXT NOT NULL UNIQUE,
    description      TEXT,
    equation         TEXT,
    author           TEXT
);

CREATE TABLE calculation_input (
    data_path_id    BIGINT NOT NULL REFERENCES data_path (data_path_id),
    calculation_id  BIGINT NOT NULL REFERENCES calculation (calculation_id),
    PRIMARY KEY (data_path_id, calculation_id)
);
CREATE INDEX idx_ci_calculation_id ON calculation_input (calculation_id);

CREATE TABLE calculation_result (
    calculation_id  BIGINT NOT NULL REFERENCES calculation (calculation_id),
    data_path_id    BIGINT NOT NULL REFERENCES data_path (data_path_id),
    PRIMARY KEY (calculation_id, data_path_id)
);
CREATE INDEX idx_cr_data_path_id ON calculation_result (data_path_id);
