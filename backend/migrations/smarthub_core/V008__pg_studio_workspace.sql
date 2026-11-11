-- Data Studio Postgres pane: query history and saved queries, same shape as the Snowflake ones.
CREATE TABLE pg_query_history (LIKE sf_query_history INCLUDING ALL);
CREATE TABLE pg_saved_queries (LIKE sf_saved_queries INCLUDING ALL);