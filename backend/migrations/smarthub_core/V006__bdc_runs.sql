-- Business Defined Checks validation and compilation runs started from
-- SmartHub, for the Superuser validation log. The streaming endpoint that
-- launches the script writes the row when it starts and closes it when the
-- script exits. Runs scheduled outside SmartHub never come through here.

CREATE TABLE bdc_runs (
    id                text        PRIMARY KEY,
    kind              text        NOT NULL,              -- validation | compilation
    mode              text        NOT NULL DEFAULT '',   -- EXECUTE_FULL, EXECUTE_ALLRULES, EXECUTE_<RULE_ID>, ...
    started_by        text        NOT NULL DEFAULT '',
    started_by_email  text        NOT NULL DEFAULT '',
    started_at        timestamptz NOT NULL DEFAULT now(),
    finished_at       timestamptz,
    returncode        integer,
    status            text        NOT NULL DEFAULT 'running', -- running | success | failed | detached
    host              text        NOT NULL DEFAULT ''
);
CREATE INDEX bdc_runs_started_idx ON bdc_runs (started_at DESC);