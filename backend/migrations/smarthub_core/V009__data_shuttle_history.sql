-- Data Shuttle copy history. One row per job, written whole each time
-- (start, each table done, end), so a retried save just writes the latest
-- picture again. A job still 'running' whose heartbeat_at stops moving was cut
-- off by a backend restart; the page shows it as interrupted.

CREATE TABLE shuttle_jobs (
    id            text        PRIMARY KEY,
    user_email    text        NOT NULL,
    direction     text        NOT NULL,                 -- pg_to_sf | sf_to_pg
    copy_type     text        NOT NULL DEFAULT 'table', -- table | tables | schema
    if_exists     text        NOT NULL DEFAULT 'fail',  -- fail | overwrite | append
    source        text        NOT NULL DEFAULT '',
    target        text        NOT NULL DEFAULT '',
    status        text        NOT NULL,                 -- running | success | partial | failed | cancelled | interrupted
    rows_copied   bigint      NOT NULL DEFAULT 0,
    tables        json        NOT NULL DEFAULT '[]',    -- per table: name, target, status, rows, time per step, error
    logs          json,                                 -- the activity log, saved when the job ends
    host          text        NOT NULL DEFAULT '',
    started_at    timestamptz NOT NULL,
    finished_at   timestamptz,
    heartbeat_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX shuttle_jobs_user_started ON shuttle_jobs (user_email, started_at DESC);
CREATE INDEX shuttle_jobs_running ON shuttle_jobs (heartbeat_at) WHERE status = 'running';