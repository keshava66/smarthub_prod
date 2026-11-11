"""Data API metadata store: the data_api_* tables in the portal database
(smarthub_core), reached through their own small pools.

Separate from db.pool() on purpose. Consumer calls arrive in bursts from
outside the portal; a burst should get a fast 503 from its own budget, not
queue behind (or starve) admins and the rest of the portal. Request logging
gets a second, tiny pool so telemetry can never take a request's connection.
Connections, session settings and row handling are the shared ones in db.py.
"""
from __future__ import annotations

import atexit
import math

from fastapi import HTTPException

import db as core_db
from data_api_runtime import BoundedPool, CapacityError, METRICS, env_int, env_bool


class PgConnection(core_db.Connection):
    """A core_db.Connection that gives its socket back to a BoundedPool."""

    def __init__(self, lease, raw):
        super().__init__(raw)
        self._lease = lease
        self._closed = False

    def commit(self):
        self.raw.commit()

    def rollback(self):
        try:
            self.raw.rollback()
        except Exception:
            self.broken = True
            raise

    def close(self):
        if self._closed:
            return
        self._closed = True
        if not self.broken and not self.raw.closed and self.raw.autocommit:
            try:
                self.raw.autocommit = False   # back to the pool's default
            except Exception:
                self.broken = True
        if self.broken or self.raw.closed:
            error = RuntimeError("Discard broken metadata connection")
            self._lease.__exit__(type(error), error, None)
        else:
            self._lease.__exit__(None, None, None)


class MetadataStore:
    backend = "postgresql"

    def __init__(self):
        self.production = env_bool("DATA_API_PRODUCTION")
        self.timeout_ms = env_int("DATA_API_METADATA_TIMEOUT_MS", 3000, maximum=120000)
        wait = env_int("DATA_API_POOL_WAIT_MS", 100, minimum=0, maximum=10000) / 1000
        total = env_int("DATA_API_METADATA_POOL_MAX", 6, maximum=128)
        self.pool = BoundedPool(total, total, total, wait_seconds=wait)
        self.log_pool = BoundedPool(2, 2, 2, wait_seconds=wait)
        atexit.register(self.close)

    @property
    def dsn(self) -> str:
        return core_db.settings().dsn

    def _open_pg(self):
        # Consumer-facing: a short statement timeout and a 1s lock wait, so a
        # slow metadata read turns into a quick 503 instead of a hung caller.
        return core_db.open_connection(statement_timeout_ms=self.timeout_ms, lock_timeout_ms=1000,
                                       application_name="smarthub-data-api")

    def connect(self, purpose="request", autocommit=False):
        """autocommit=True for work that is a single statement (or a few that
        need no shared transaction): no BEGIN/COMMIT round trips."""
        core_db.ensure_ready()
        pool = self.log_pool if purpose == "log" else self.pool
        lease = pool.connection("metadata", self._open_pg)
        try:
            raw = lease.__enter__()
        except CapacityError:
            raise HTTPException(503, "Metadata capacity is busy; retry with backoff.", headers={"Retry-After": "1"}) from None
        except Exception:
            METRICS.add("metadata_connect_errors")
            raise HTTPException(503, "Metadata database unavailable.", headers={"Retry-After": "1"}) from None
        if autocommit:
            raw.autocommit = True
        return PgConnection(lease, raw)

    def verify(self):
        """Fail fast at startup if the tables this module reads aren't there.

        Uses a one-off connection, not the pool: this runs at import, and a
        pooled connection opened then would be inherited by forked workers
        (which BoundedPool refuses to use)."""
        core_db.ensure_ready()
        raw = self._open_pg()
        try:
            connection = core_db.Connection(raw)
            required = {
                "data_apis": "id,slug,name,description,api_key,created_by,created_at,is_active,last_used_at",
                "data_api_tables": "id,api_id,source_system,env,database_name,schema_name,table_name,alias,row_limit,columns_csv",
                "data_api_requests": "id,api_id,alias,endpoint,ts,ip,user_agent,status,row_count,duration_ms",
                "data_api_rate_windows": "bucket,window_start,hits",
            }
            for table, columns in required.items():
                connection.execute(f"SELECT {columns} FROM {table} LIMIT 0")
            raw.rollback()
        finally:
            raw.close()

    def shared_rate_check(self, connection, bucket: str, limit: int, window: int):
        """Atomic fixed-window budget shared by all workers/replicas, per API.

        The database supplies the clock. Fixed windows can admit up to twice the
        configured budget around a boundary; this is not a sliding window.
        Runs on the caller's metadata connection (no nested pool); a single
        statement, so it is atomic even in autocommit.
        """
        result = connection.execute("""
            WITH tick AS MATERIALIZED (
                SELECT floor(extract(epoch FROM clock_timestamp()) / ?)::bigint AS slot
            )
            INSERT INTO data_api_rate_windows AS r (bucket, window_start, hits)
            SELECT ?, slot, 1 FROM tick
            ON CONFLICT (bucket) DO UPDATE SET
                hits = CASE WHEN r.window_start = EXCLUDED.window_start
                       THEN LEAST(r.hits + 1, ?) ELSE 1 END,
                window_start = EXCLUDED.window_start
            RETURNING hits, window_start,
                      extract(epoch FROM clock_timestamp()) AS now_epoch
            """, (window, bucket, limit + 1)).fetchone()
        if result["hits"] <= limit:
            return None
        return max(1, math.ceil((result["window_start"] + 1) * window - float(result["now_epoch"])))

    def close(self):
        self.pool.close()
        self.log_pool.close()

    def snapshot(self):
        return {"backend": self.backend, "pool": self.pool.snapshot(), "log_pool": self.log_pool.snapshot()}


# The semantic-search tables. They are created by the core migration along
# with everything else, so this is only used to confirm they are reachable.
SEARCH_TABLES = ("data_api_embedding_models", "data_api_vector_registrations",
                 "data_api_contracts", "data_api_search_settings")