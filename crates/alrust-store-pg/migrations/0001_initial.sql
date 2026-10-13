-- alrust initial schema (alpha). Column meanings follow the reference
-- Alerta v9.1.0 schema (reference/alerta/alerta/sql/schema.sql); history is a
-- jsonb array kept newest first instead of a composite-type array.

CREATE TABLE alerts (
    id                text PRIMARY KEY,
    resource          text NOT NULL,
    event             text NOT NULL,
    environment       text NOT NULL,
    severity          text NOT NULL,
    correlate         text[] NOT NULL DEFAULT '{}',
    status            text NOT NULL,
    service           text[] NOT NULL DEFAULT '{}',
    "group"           text NOT NULL,
    value             jsonb,
    text              text NOT NULL DEFAULT '',
    tags              text[] NOT NULL DEFAULT '{}',
    attributes        jsonb NOT NULL DEFAULT '{}',
    origin            text NOT NULL,
    type              text NOT NULL,
    create_time       timestamptz NOT NULL,
    timeout           bigint,            -- NULL only after PUT /status without timeout (spec A1.3)
    raw_data          text,
    customer          text,
    duplicate_count   bigint NOT NULL DEFAULT 0,
    repeat            boolean NOT NULL DEFAULT false,
    previous_severity text NOT NULL,
    trend_indication  text NOT NULL,
    receive_time      timestamptz NOT NULL,
    last_receive_id   text NOT NULL,
    last_receive_time timestamptz NOT NULL,
    update_time       timestamptz NOT NULL,
    history           jsonb NOT NULL DEFAULT '[]'
);

CREATE INDEX alerts_identity_idx ON alerts (environment, resource, COALESCE(customer, ''));
CREATE INDEX alerts_last_receive_time_idx ON alerts (last_receive_time DESC);
CREATE INDEX alerts_status_idx ON alerts (status);

CREATE TABLE heartbeats (
    id           text PRIMARY KEY,
    origin       text NOT NULL,
    tags         text[] NOT NULL DEFAULT '{}',
    attributes   jsonb NOT NULL DEFAULT '{}',
    type         text NOT NULL,
    create_time  timestamptz NOT NULL,
    timeout      bigint NOT NULL,
    receive_time timestamptz NOT NULL,
    customer     text
);

CREATE UNIQUE INDEX heartbeats_origin_customer_idx ON heartbeats (origin, COALESCE(customer, ''));

CREATE TABLE keys (
    id             text PRIMARY KEY,
    key            text NOT NULL UNIQUE,
    "user"         text NOT NULL,
    scopes         text[] NOT NULL DEFAULT '{}',
    text           text NOT NULL DEFAULT '',
    expire_time    timestamptz NOT NULL,
    count          bigint NOT NULL DEFAULT 0,
    last_used_time timestamptz,
    customer       text
);
