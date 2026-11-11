-- SmartHub application schema. Runs with search_path set to the target schema
-- (smarthub_core unless SMARTHUB_DB_SCHEMA says otherwise), so nothing here is
-- schema-qualified. Applied once by db.migrate(); never edit an applied file,
-- add V002__... instead.
--
-- Conventions:
--   ids are text: several are slugs-or-uuids in the same URL segment, and the
--     modules already mint them as strings.
--   every point in time is timestamptz.
--   json (not jsonb) where the app stores and re-reads a document as text; it
--     is validated on the way in and comes back byte-for-byte.
--   flags are boolean.


-- ─── Audit log ──────────────────────────────────────────────────────────────

CREATE TABLE audit_log (
    id            text        PRIMARY KEY,
    "timestamp"   timestamptz NOT NULL DEFAULT now(),
    action        text        NOT NULL,
    performed_by  text        NOT NULL DEFAULT '',
    target        text        NOT NULL DEFAULT '',
    details       text        NOT NULL DEFAULT '',
    ip_address    text        NOT NULL DEFAULT ''
);
CREATE INDEX audit_log_timestamp_idx ON audit_log ("timestamp" DESC);
-- text_pattern_ops so the category filters' prefix LIKEs ('role\_%',
-- 'page\_%') can use it as well as plain equality.
CREATE INDEX audit_log_action_idx    ON audit_log (action text_pattern_ops);
-- Unique-users / known-users group case-insensitively and want the newest row
-- per person; this index hands them rows already in that order.
CREATE INDEX audit_log_actor_idx     ON audit_log (lower(performed_by), "timestamp" DESC);

CREATE TABLE assistant_settings (
    user_email      text        PRIMARY KEY,
    enabled         boolean     NOT NULL DEFAULT false,
    avatar_id       text        NOT NULL DEFAULT '',
    appearance      text        NOT NULL DEFAULT 'standard',
    reduce_motion   boolean     NOT NULL DEFAULT false,
    text_size       text        NOT NULL DEFAULT 'medium',
    avatar_size     text        NOT NULL DEFAULT 'medium',
    auto_send_voice boolean     NOT NULL DEFAULT true,
    updated_at      timestamptz NOT NULL DEFAULT now()
);


-- ─── RBAC ───────────────────────────────────────────────────────────────────

CREATE TABLE rbac_custom_roles (
    role_id     text PRIMARY KEY,
    definition  json NOT NULL
);

CREATE TABLE rbac_user_overrides (
    email      text    PRIMARY KEY,
    role_name  text    NOT NULL,
    roles      json,
    keep_ad    boolean NOT NULL DEFAULT false
);

CREATE TABLE rbac_user_permissions (
    email    text PRIMARY KEY,
    grants   json NOT NULL DEFAULT '[]',
    revokes  json NOT NULL DEFAULT '[]'
);

CREATE TABLE rbac_meta (
    key    text PRIMARY KEY,
    value  text
);


-- ─── Communication Center ───────────────────────────────────────────────────

CREATE TABLE email_templates (
    id          text        PRIMARY KEY,
    name        text        NOT NULL UNIQUE,
    category    text        NOT NULL DEFAULT 'custom',
    subject     text        NOT NULL DEFAULT '',
    body        text        NOT NULL DEFAULT '',
    layout      json        NOT NULL DEFAULT '{}',
    created_by  text        NOT NULL DEFAULT '',
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX email_templates_name_ci_idx ON email_templates (lower(name));

CREATE TABLE email_history (
    id              text        PRIMARY KEY,
    sent_at         timestamptz NOT NULL DEFAULT now(),
    sent_by         text        NOT NULL,
    category        text        NOT NULL DEFAULT 'custom',
    subject         text        NOT NULL DEFAULT '',
    body            text        NOT NULL DEFAULT '',
    to_addrs        text        NOT NULL DEFAULT '',
    cc_addrs        text        NOT NULL DEFAULT '',
    bcc_addrs       text        NOT NULL DEFAULT '',
    attachments     json        NOT NULL DEFAULT '[]',
    recipient_count integer     NOT NULL DEFAULT 0 CHECK (recipient_count >= 0),
    status          text        NOT NULL DEFAULT 'sent' CHECK (status IN ('sent', 'failed')),
    layout          json        NOT NULL DEFAULT '{}',
    error           text        NOT NULL DEFAULT '',
    is_test         boolean     NOT NULL DEFAULT false,
    sensitivity     text        NOT NULL DEFAULT '',
    attachment_dir  text        NOT NULL DEFAULT '',
    scheduled_id    text        NOT NULL DEFAULT '',
    resent_from     text        NOT NULL DEFAULT '',
    release_notes   text        NOT NULL DEFAULT ''
);
CREATE INDEX email_history_sent_at_idx ON email_history (sent_at DESC);
-- The drafter's "last few real sends" lookup.
CREATE INDEX email_history_real_sends_idx ON email_history (sent_at DESC)
    WHERE status = 'sent' AND NOT is_test;

CREATE TABLE email_scheduled (
    id              text        PRIMARY KEY,
    created_at      timestamptz NOT NULL DEFAULT now(),
    created_by      text        NOT NULL,
    send_at         timestamptz NOT NULL,
    category        text        NOT NULL DEFAULT 'custom',
    subject         text        NOT NULL DEFAULT '',
    body            text        NOT NULL DEFAULT '',
    layout          json        NOT NULL DEFAULT '{}',
    to_addrs        text        NOT NULL DEFAULT '',
    cc_addrs        text        NOT NULL DEFAULT '',
    bcc_addrs       text        NOT NULL DEFAULT '',
    attachments     json        NOT NULL DEFAULT '[]',
    attachment_dir  text        NOT NULL DEFAULT '',
    recipient_count integer     NOT NULL DEFAULT 0 CHECK (recipient_count >= 0),
    status          text        NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'sending', 'sent', 'failed', 'cancelled')),
    error           text        NOT NULL DEFAULT '',
    history_id      text        NOT NULL DEFAULT '',
    sensitivity     text        NOT NULL DEFAULT '',
    release_notes   text        NOT NULL DEFAULT '',
    -- Which worker took a row to send, and when. With several workers or
    -- replicas polling the same queue, only a claim that has gone quiet for
    -- longer than any send could take is treated as abandoned.
    claimed_by      text,
    claimed_at      timestamptz
);
CREATE INDEX email_scheduled_due_idx     ON email_scheduled (send_at) WHERE status = 'pending';
CREATE INDEX email_scheduled_sending_idx ON email_scheduled (claimed_at) WHERE status = 'sending';
CREATE INDEX email_scheduled_status_idx  ON email_scheduled (status, send_at DESC);


-- ─── Data Studio (Snowflake query runner) ───────────────────────────────────

CREATE TABLE sf_query_history (
    id          text        PRIMARY KEY,
    user_email  text        NOT NULL,
    sql         text        NOT NULL,
    database    text        NOT NULL DEFAULT '',
    schema      text        NOT NULL DEFAULT '',
    status      text        NOT NULL DEFAULT '',
    elapsed_ms  integer     NOT NULL DEFAULT 0,
    row_count   bigint,
    error       text        NOT NULL DEFAULT '',
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX sf_query_history_user_idx ON sf_query_history (user_email, created_at DESC, id DESC);

CREATE TABLE sf_saved_queries (
    id          text        PRIMARY KEY,
    user_email  text        NOT NULL,
    name        text        NOT NULL,
    sql         text        NOT NULL,
    database    text        NOT NULL DEFAULT '',
    schema      text        NOT NULL DEFAULT '',
    shared      boolean     NOT NULL DEFAULT false,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX sf_saved_queries_user_idx   ON sf_saved_queries (user_email, updated_at DESC);
CREATE INDEX sf_saved_queries_shared_idx ON sf_saved_queries (updated_at DESC) WHERE shared;


-- ─── What's New ─────────────────────────────────────────────────────────────

CREATE TABLE release_notes (
    id            text        PRIMARY KEY,
    title         text        NOT NULL,
    summary       text        NOT NULL DEFAULT '',
    detail        text        NOT NULL DEFAULT '',
    area          text        NOT NULL DEFAULT '',
    kind          text        NOT NULL DEFAULT 'feature'
                  CHECK (kind IN ('feature', 'improvement', 'fix', 'removal', 'commit')),
    status        text        NOT NULL DEFAULT 'draft'
                  CHECK (status IN ('draft', 'ready', 'announced', 'hidden')),
    source        text        NOT NULL DEFAULT 'manual',
    fingerprint   text        UNIQUE,
    subject       text        NOT NULL DEFAULT '',
    created_at    timestamptz NOT NULL DEFAULT now(),
    created_by    text        NOT NULL DEFAULT 'system',
    announced_at  timestamptz,
    announced_in  text
);
CREATE INDEX release_notes_status_idx  ON release_notes (status, created_at DESC);
CREATE INDEX release_notes_subject_idx ON release_notes (subject);
CREATE INDEX release_notes_created_idx ON release_notes (created_at DESC);

CREATE TABLE release_note_snapshots (
    key       text        PRIMARY KEY,
    payload   json        NOT NULL,
    taken_at  timestamptz NOT NULL DEFAULT now()
);


-- ─── Data API Export ────────────────────────────────────────────────────────

CREATE TABLE data_apis (
    id            text        PRIMARY KEY,
    slug          text        NOT NULL UNIQUE,
    name          text        NOT NULL,
    description   text        NOT NULL DEFAULT '',
    api_key       text        NOT NULL UNIQUE,
    created_by    text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    is_active     boolean     NOT NULL DEFAULT true,
    last_used_at  timestamptz
);
CREATE INDEX data_apis_created_idx ON data_apis (created_at DESC);

CREATE TABLE data_api_tables (
    id             text    PRIMARY KEY,
    api_id         text    NOT NULL REFERENCES data_apis (id) ON DELETE CASCADE,
    source_system  text    NOT NULL,
    env            text    NOT NULL,
    database_name  text,
    schema_name    text,
    table_name     text    NOT NULL,
    alias          text    NOT NULL,
    row_limit      integer NOT NULL DEFAULT 1000 CHECK (row_limit BETWEEN 1 AND 10000),
    columns_csv    text,
    UNIQUE (api_id, alias)
);

-- One row per consumer call; the busiest table here. A bigint identity keeps
-- inserts appending to the right edge of the index instead of scattering.
CREATE TABLE data_api_requests (
    id           bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    api_id       text        NOT NULL REFERENCES data_apis (id) ON DELETE CASCADE,
    alias        text,
    endpoint     text        NOT NULL,
    ts           timestamptz NOT NULL DEFAULT now(),
    ip           text        NOT NULL DEFAULT '',
    user_agent   text        NOT NULL DEFAULT '',
    status       integer     NOT NULL DEFAULT 200,
    row_count    bigint,
    duration_ms  integer
);
CREATE INDEX data_api_requests_api_ts_idx ON data_api_requests (api_id, ts DESC);
CREATE INDEX data_api_requests_ts_idx     ON data_api_requests (ts);

-- Updated on every consumer call; spare room on each page lets those be HOT
-- updates that never touch the index.
CREATE TABLE data_api_rate_windows (
    bucket        text   PRIMARY KEY,
    window_start  bigint NOT NULL,
    hits          bigint NOT NULL
) WITH (fillfactor = 70);

CREATE TABLE data_api_embedding_models (
    id               text        PRIMARY KEY,
    name             text        NOT NULL UNIQUE,
    deployment       text        NOT NULL,
    dimensions       integer     NOT NULL CHECK (dimensions BETWEEN 1 AND 16000),
    endpoint         text        NOT NULL DEFAULT '',
    api_version      text        NOT NULL DEFAULT '',
    api_key_env      text        NOT NULL,
    query_prefix     text        NOT NULL DEFAULT '',
    send_dimensions  boolean     NOT NULL DEFAULT false,
    created_by       text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_by       text,
    updated_at       timestamptz
);

CREATE TABLE data_api_vector_registrations (
    id             text        PRIMARY KEY,
    source_system  text        NOT NULL,
    env            text        NOT NULL,
    database_name  text        NOT NULL DEFAULT '',
    schema_name    text        NOT NULL,
    table_name     text        NOT NULL,
    column_name    text        NOT NULL,
    model_id       text        NOT NULL REFERENCES data_api_embedding_models (id),
    created_by     text,
    created_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source_system, env, database_name, schema_name, table_name, column_name)
);
CREATE INDEX data_api_vector_registrations_model_idx ON data_api_vector_registrations (model_id);

CREATE TABLE data_api_contracts (
    id           text        PRIMARY KEY,
    name         text        NOT NULL,
    version      integer     NOT NULL CHECK (version >= 1),
    description  text        NOT NULL DEFAULT '',
    definition   json        NOT NULL,
    created_by   text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (name, version)
);

CREATE TABLE data_api_search_settings (
    table_id    text        PRIMARY KEY REFERENCES data_api_tables (id) ON DELETE CASCADE,
    settings    json        NOT NULL,
    updated_by  text,
    updated_at  timestamptz NOT NULL DEFAULT now()
);


-- ─── Assistant long-term memory ─────────────────────────────────────────────

CREATE TABLE assistant_user_memory (
    email           text        PRIMARY KEY,
    preferences     jsonb       NOT NULL DEFAULT '{}'::jsonb,
    recent_summary  text        NOT NULL DEFAULT '',
    updated_at      timestamptz NOT NULL DEFAULT now()
);