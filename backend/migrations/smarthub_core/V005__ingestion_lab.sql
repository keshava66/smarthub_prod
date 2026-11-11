-- Ingestion Lab runs and their console output. Same model as it_runs: the
-- portal process that started a run owns it, writes its output here in small
-- batches, bumps heartbeat_at and watches stop_requested_by. Any portal process
-- can stream the log or ask for a stop.
--
-- Remote steps run over SSH and write their output to a file on that server;
-- the portal follows the file, so a dropped connection or a portal restart
-- doesn't stop the step. remote_offset is how far into that file the saved log
-- goes, so whoever picks the run back up carries on without losing or
-- repeating a line.

CREATE TABLE ingestion_runs (
    id                 text        PRIMARY KEY,
    step_id            text        NOT NULL,
    step_name          text        NOT NULL DEFAULT '',
    inputs             json        NOT NULL DEFAULT '{}',  -- secret inputs masked
    command            text        NOT NULL DEFAULT '',    -- what ran, secrets masked
    workdir            text        NOT NULL DEFAULT '',
    lock_key           text,                               -- step id when the step runs one at a time
    started_by         text        NOT NULL DEFAULT '',
    started_by_email   text        NOT NULL DEFAULT '',
    started_at         timestamptz NOT NULL DEFAULT now(),
    finished_at        timestamptz,
    status             text        NOT NULL DEFAULT 'running', -- running | success | failed | stopped | timed_out | lost
    returncode         integer,
    stop_requested_by  text,
    force_stop_requested boolean NOT NULL DEFAULT false,  -- kill now instead of waiting out a graceful stop
    host               text        NOT NULL DEFAULT '',
    pid                integer,
    heartbeat_at       timestamptz NOT NULL DEFAULT now(),
    log_lines          integer     NOT NULL DEFAULT 0,
    log_truncated      boolean     NOT NULL DEFAULT false,
    server_id          text        NOT NULL DEFAULT 'local',
    server_name        text        NOT NULL DEFAULT '',
    remote             boolean     NOT NULL DEFAULT false,
    remote_spool       text,                               -- output file on the server
    remote_dir         text,                               -- where the run's exit-code file lives
    remote_offset      bigint      NOT NULL DEFAULT 0,
    owner              text                                -- portal process following the run; only it may write its log
);
CREATE INDEX ingestion_runs_started_idx ON ingestion_runs (started_at DESC);
CREATE INDEX ingestion_runs_step_idx ON ingestion_runs (step_id, started_at DESC);
CREATE INDEX ingestion_runs_server_idx ON ingestion_runs (server_id, started_at DESC);
CREATE INDEX ingestion_runs_open_idx ON ingestion_runs (heartbeat_at) WHERE finished_at IS NULL;
-- One running run per exclusive step, across every server.
CREATE UNIQUE INDEX ingestion_runs_one_running_idx ON ingestion_runs (lock_key)
    WHERE finished_at IS NULL AND lock_key IS NOT NULL;

CREATE TABLE ingestion_run_logs (
    run_id   text        NOT NULL REFERENCES ingestion_runs (id) ON DELETE CASCADE,
    seq      integer     NOT NULL,
    ts       timestamptz NOT NULL,
    type     text        NOT NULL,   -- output | info | status | success | error | separator
    message  text        NOT NULL DEFAULT '',
    PRIMARY KEY (run_id, seq)
);