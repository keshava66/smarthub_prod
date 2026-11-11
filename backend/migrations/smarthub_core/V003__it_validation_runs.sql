-- IT validation runs and their console output, so every server and every
-- user sees the same run: who started it, its live log, and stop requests.
-- The server that started a run owns the process; it appends log lines,
-- bumps heartbeat_at about once a second and polls stop_requested_by.
-- Any server can read the log or ask for a stop.

CREATE TABLE it_runs (
    id                 text        PRIMARY KEY,
    db                 text        NOT NULL,              -- insight7 | rimdocs
    started_by         text        NOT NULL DEFAULT '',
    started_by_email   text        NOT NULL DEFAULT '',
    started_at         timestamptz NOT NULL DEFAULT now(),
    finished_at        timestamptz,
    status             text        NOT NULL DEFAULT 'running', -- running | success | failed | stopped | lost
    returncode         integer,
    stop_requested_by  text,
    host               text        NOT NULL DEFAULT '',   -- server running the script
    heartbeat_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX it_runs_started_idx ON it_runs (started_at DESC);
-- One running validation per database, across every server.
CREATE UNIQUE INDEX it_runs_one_running_idx ON it_runs (db) WHERE finished_at IS NULL;

CREATE TABLE it_run_logs (
    run_id   text        NOT NULL REFERENCES it_runs (id) ON DELETE CASCADE,
    seq      integer     NOT NULL,
    ts       timestamptz NOT NULL,
    type     text        NOT NULL,   -- output | info | status | success | error | separator
    message  text        NOT NULL DEFAULT '',
    PRIMARY KEY (run_id, seq)
);