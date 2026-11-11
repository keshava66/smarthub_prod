-- Trigram index behind the Audit Log search box, which matches anywhere in
-- the actor, action, details and target. Needs the pg_trgm extension; when it
-- can't be installed (no privilege, not available) this logs a notice and the
-- search keeps working as a scan. The query side is in audit.py
-- (_SEARCH_EXPR) and must match the expression below exactly.
DO $$
DECLARE
    trgm_schema text;
BEGIN
    SELECT n.nspname INTO trgm_schema
      FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace
     WHERE e.extname = 'pg_trgm';

    IF trgm_schema IS NULL THEN
        BEGIN
            CREATE EXTENSION IF NOT EXISTS pg_trgm;
        EXCEPTION WHEN OTHERS THEN
            RAISE NOTICE 'pg_trgm unavailable (%): audit search will scan instead of using an index', SQLERRM;
            RETURN;
        END;
        SELECT n.nspname INTO trgm_schema
          FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace
         WHERE e.extname = 'pg_trgm';
    END IF;

    EXECUTE format(
        'CREATE INDEX audit_log_search_trgm_idx ON audit_log USING gin '
        '((lower(performed_by || chr(1) || action || chr(1) || details || chr(1) || target)) %I.gin_trgm_ops)',
        trgm_schema);
END
$$;