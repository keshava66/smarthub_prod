-- Enterprise Agentic RAG metadata and evaluation.
-- Additive only: existing RAG/Data API/assistant tables and contracts remain unchanged.

CREATE TABLE rag_sources (
    id           text        PRIMARY KEY,
    name         text        NOT NULL UNIQUE,
    description  text        NOT NULL DEFAULT '',
    source_type  text        NOT NULL CHECK (source_type IN ('data_api', 'connector', 'rest')),
    config       json        NOT NULL,
    is_active    boolean     NOT NULL DEFAULT true,
    created_by   text        NOT NULL DEFAULT '',
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_by   text        NOT NULL DEFAULT '',
    updated_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX rag_sources_active_name_idx ON rag_sources (is_active, name);

CREATE TABLE rag_profiles (
    id           text        PRIMARY KEY,
    name         text        NOT NULL UNIQUE,
    description  text        NOT NULL DEFAULT '',
    config       json        NOT NULL,
    is_active    boolean     NOT NULL DEFAULT true,
    created_by   text        NOT NULL DEFAULT '',
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_by   text        NOT NULL DEFAULT '',
    updated_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX rag_profiles_active_name_idx ON rag_profiles (is_active, name);

CREATE TABLE rag_feedback (
    id           text        PRIMARY KEY,
    request_id   text        NOT NULL,
    user_email   text        NOT NULL DEFAULT '',
    rating       integer     NOT NULL CHECK (rating IN (-1, 1)),
    reason       text        NOT NULL DEFAULT '',
    comment      text        NOT NULL DEFAULT '',
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX rag_feedback_request_idx ON rag_feedback (request_id);
CREATE INDEX rag_feedback_created_idx ON rag_feedback (created_at DESC);

CREATE TABLE rag_eval_datasets (
    id           text        PRIMARY KEY,
    name         text        NOT NULL,
    version      integer     NOT NULL CHECK (version > 0),
    description  text        NOT NULL DEFAULT '',
    cases        json        NOT NULL,
    created_by   text        NOT NULL DEFAULT '',
    created_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (name, version)
);
CREATE INDEX rag_eval_datasets_name_version_idx ON rag_eval_datasets (name, version DESC);

CREATE TABLE rag_eval_runs (
    id               text        PRIMARY KEY,
    dataset_id       text        NOT NULL REFERENCES rag_eval_datasets (id) ON DELETE RESTRICT,
    profile_id       text        NOT NULL REFERENCES rag_profiles (id) ON DELETE RESTRICT,
    status           text        NOT NULL CHECK (status IN ('running', 'complete', 'failed')),
    config_snapshot  json        NOT NULL,
    summary          json        NOT NULL,
    created_by       text        NOT NULL DEFAULT '',
    started_at       timestamptz NOT NULL DEFAULT now(),
    completed_at     timestamptz
);
CREATE INDEX rag_eval_runs_started_idx ON rag_eval_runs (started_at DESC);
CREATE INDEX rag_eval_runs_dataset_idx ON rag_eval_runs (dataset_id, started_at DESC);
CREATE INDEX rag_eval_runs_profile_idx ON rag_eval_runs (profile_id, started_at DESC);

CREATE TABLE rag_eval_case_results (
    id                  text        PRIMARY KEY,
    run_id              text        NOT NULL REFERENCES rag_eval_runs (id) ON DELETE CASCADE,
    case_id             text        NOT NULL,
    question            text        NOT NULL,
    response            text        NOT NULL DEFAULT '',
    citations           json        NOT NULL,
    retrieved_evidence  json        NOT NULL,
    metrics             json        NOT NULL,
    latency_ms          integer     NOT NULL DEFAULT 0,
    error               text        NOT NULL DEFAULT '',
    UNIQUE (run_id, case_id)
);
CREATE INDEX rag_eval_case_results_run_idx ON rag_eval_case_results (run_id, case_id);
