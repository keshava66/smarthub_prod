from __future__ import annotations

import os
import re
import logging
import math
import struct
import threading
from contextlib import contextmanager
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class ConnectorError(Exception):
    """Raised for connector configuration or connection failures."""


def _sql_literal(term: str) -> str:
    """Escape a value for safe inline embedding as a SQL string literal
    (doubles single quotes). Used for search terms — table/schema/column
    names throughout this file are validated identifiers, not literals, so
    they don't go through this.

    Only safe on its own for a dialect where the single quote is the ONLY
    thing that can end a literal. Where a backslash escapes too (Snowflake,
    Spark/Databricks), doubling the quote is not enough: \\' arrives as an
    escaped quote followed by a live one, which closes the string and leaves
    the rest as SQL. Those connectors set BACKSLASH_ESCAPES and go through
    BaseConnector._lit instead of calling this directly."""
    return term.replace("'", "''")


# ─────────────────────────────────────────────────────────────────────────────
_jdbc_jvm_lock = threading.Lock()


def ensure_jdbc_jvm(jar_paths: List[str]) -> None:
    try:
        import jpype
    except ImportError:
        return  # let the individual connector's own import raise the clearer error

    if jpype.isJVMStarted():
        return

    with _jdbc_jvm_lock:
        if jpype.isJVMStarted():
            return
        classpath = sorted({p for p in jar_paths if p and os.path.exists(p)})
        if not classpath:
            return
        jpype.startJVM(classpath=classpath)
        logger.info(f"Started shared JDBC JVM with classpath: {classpath}")


def _set_jdbc_login_timeout(seconds: int = 10) -> None:
    """JVM-wide JDBC login timeout so a dead host fails fast instead of the
    driver sitting on a TCP connect forever. Best-effort — if the JVM isn't
    up yet or jpype isn't installed, jaydebeapi will start it and we just
    skip the tightening for that first call."""
    try:
        import jpype
        if not jpype.isJVMStarted():
            return
        from jpype import JClass
        JClass("java.sql.DriverManager").setLoginTimeout(seconds)
    except Exception:
        pass


def _cfg(cfg: dict, key: str, env_var: Optional[str] = None, required: bool = True, default=None):
    """Read `key` from a connector config dict, falling back to an env var."""
    if cfg.get(key) not in (None, ""):
        return cfg[key]
    if env_var and os.environ.get(env_var):
        return os.environ[env_var]
    if required:
        raise ConnectorError(
            f"Missing required setting '{key}' — add it to data_validation_config.ini under "
            f"the relevant section (or set the {env_var} environment variable)."
        )
    return default


# ─────────────────────────────────────────────────────────────────────────────
# Common interface
# ─────────────────────────────────────────────────────────────────────────────
class BaseConnector:
    display_name = "Base"
    # Which of {"database", "schema"} this source lets a user browse.
    # "table" is always required and isn't listed here.
    LEVELS: List[str] = []
    # Column types (uppercase) this database refuses to use in ORDER BY /
    # GROUP BY / comparisons — e.g. SQL Server's XML type. The engine excludes
    # these from key auto-detection, ORDER BY, and GROUP BY, while still
    # selecting and comparing their values in Python (string-based comparison
    # doesn't care about SQL-level orderability).
    NON_SORTABLE_TYPES: set = set()

    # Whether fetch_rows_after below is really implemented. Callers check this
    # rather than hasattr(), which is always true for the stub.
    SUPPORTS_VECTOR_SEARCH = False
    SUPPORTS_KEYSET = False

    # Whether search_rows understands mode="words" — matching whole words via
    # the engine's full-text search rather than a literal substring.
    SUPPORTS_WORD_SEARCH = False

    # Whether search_rows understands mode="exact" — equality instead of a
    # substring match. Every SQL source can do equality, so this defaults on;
    # the flag exists so a connector that genuinely can't opt out.
    SUPPORTS_EXACT_SEARCH = True

    # True where a backslash escapes inside a string literal, so it has to be
    # doubled as well as the quote. Off for the SQL-standard dialects — there a
    # backslash is an ordinary character, and doubling it would turn a search
    # for one backslash into a search for two.
    BACKSLASH_ESCAPES = False

    MAX_AGGREGATE_COLUMNS = 60

    def _lit(self, term: str) -> str:
        """Every user-supplied value that goes inline into SQL comes through
        here, escaped the way this dialect actually parses literals."""
        term = "" if term is None else str(term)
        if self.BACKSLASH_ESCAPES:
            term = term.replace("\\", "\\\\")
        return _sql_literal(term)

    def _table_ref(self, database: Optional[str], schema: Optional[str], table: str) -> str:
        """Fully-qualified, quoted table reference for this dialect."""
        raise NotImplementedError

    def _quote_col(self, column: str) -> str:
        return f'"{column}"'

    def _as_text(self, col_sql: str) -> str:
        """Cast an already-quoted column to this dialect's text type."""
        return f"CAST({col_sql} AS TEXT)"

    def _order_sql(self, order_by) -> str:
        """ORDER BY from a list of column names or (column, "ASC"/"DESC")
        pairs. Plain strings keep the old ascending behaviour."""
        if not order_by:
            return ""
        parts = []
        for item in order_by:
            if isinstance(item, (tuple, list)):
                col, direction = item[0], str(item[1]).upper()
                direction = "DESC" if direction == "DESC" else "ASC"
                parts.append(f"{self._quote_col(col)} {direction}")
            else:
                parts.append(self._quote_col(item))
        return "ORDER BY " + ", ".join(parts)

    @staticmethod
    def _order_cols(order_by) -> List[str]:
        return [i[0] if isinstance(i, (tuple, list)) else i for i in (order_by or [])]

    @staticmethod
    def _and_where(*parts: str) -> str:
        live = [p for p in parts if p]
        if not live:
            return ""
        if len(live) == 1:
            return live[0]
        return " AND ".join(f"({p})" for p in live)

    def _run_sql(self, sql: str) -> List[tuple]:
        cur = self.conn.cursor()
        try:
            cur.execute(sql)
            return cur.fetchall()
        finally:
            cur.close()

    def get_column_aggregates(self, database: Optional[str], schema: Optional[str], table: str,
                              columns: List[str], numeric_columns: List[str]) -> Dict[str, dict]:
        """Full-table COUNT/SUM/MIN/MAX per column, in a single query.

        Returns {column: {non_null, sum?, min?, max?}} plus a "*" entry holding
        the total row count. Numeric columns additionally get SUM, which is the
        aggregate most likely to expose truncation or dropped rows.
        """
        if not columns:
            return {}
        columns = columns[:self.MAX_AGGREGATE_COLUMNS]
        numeric = set(numeric_columns)
        select = ["COUNT(*)"]
        plan: List[tuple] = []
        for c in columns:
            q = self._quote_col(c)
            select.append(f"COUNT({q})")
            plan.append((c, "non_null"))
            if c in numeric:
                select.extend([f"SUM({q})", f"MIN({q})", f"MAX({q})"])
                plan.extend([(c, "sum"), (c, "min"), (c, "max")])
        rows = self._run_sql(f'SELECT {", ".join(select)} FROM {self._table_ref(database, schema, table)}')
        if not rows:
            return {}
        row = rows[0]
        out: Dict[str, dict] = {"*": {"row_count": row[0]}}
        for i, (col, kind) in enumerate(plan, start=1):
            out.setdefault(col, {})[kind] = row[i]
        return out

    def connect(self):
        raise NotImplementedError

    def disconnect(self):
        pass

    def list_databases(self) -> List[str]:
        """Only called if 'database' is in LEVELS."""
        raise NotImplementedError

    def list_schemas(self, database: Optional[str]) -> List[str]:
        """Only called if 'schema' is in LEVELS."""
        raise NotImplementedError

    def list_tables(self, database: Optional[str], schema: Optional[str]) -> List[str]:
        raise NotImplementedError

    def get_columns(self, database: Optional[str], schema: Optional[str], table: str) -> List[Dict[str, str]]:
        """Returns [{'name': 'COL', 'type': 'VARCHAR'}, ...] in ordinal order."""
        raise NotImplementedError

    def filtered_row_count(self, database: Optional[str], schema: Optional[str], table: str,
                            filters: List[tuple]) -> int:
        """COUNT(*) restricted by filters. Separate from get_row_count because
        that one can be answered from cached statistics and this one cannot."""
        raise NotImplementedError

    def get_row_count(self, database: Optional[str], schema: Optional[str], table: str) -> int:
        raise NotImplementedError

    def get_null_counts(self, database: Optional[str], schema: Optional[str], table: str, columns: List[str]) -> Dict[str, int]:
        raise NotImplementedError

    def get_duplicate_count(self, database: Optional[str], schema: Optional[str], table: str, columns: List[str]) -> int:
        """Count of rows that are exact duplicates across `columns`."""
        raise NotImplementedError

    def fetch_rows(self, database: Optional[str], schema: Optional[str], table: str,
                    columns: List[str], limit: int, order_by: Optional[List[str]] = None,
                    offset: int = 0, filters: Optional[List[tuple]] = None) -> List[dict]:
        """Fetch up to `limit` rows (starting at `offset`) as a list of dicts —
        used for missing-record / mismatch analysis and paginated exports.
        `order_by` is a list of columns to sort by (order matters); pass None/[]
        for no explicit ordering. Without it, LIMIT/FETCH FIRST is non-deterministic —
        callers should always pass an order_by when sample_limit could be smaller
        than the table's row count (or when paginating with offset), so results
        stay stable and comparable across calls."""
        raise NotImplementedError

    def search_rows(self, database: Optional[str], schema: Optional[str], table: str,
                     columns: List[str], search_term: str, limit: int,
                     order_by: Optional[List[str]] = None, offset: int = 0,
                     search_columns: Optional[List[str]] = None,
                     mode: str = "substring",
                     filters: Optional[List[tuple]] = None) -> List[dict]:
        """Like fetch_rows (returns all of `columns`), but only rows where
        `search_term` matches in any of `search_columns` OR'd together.
        `search_columns` defaults to `columns` (search everywhere) but can be
        narrowed to a subset — e.g. one column — to skip casting/scanning the
        rest, which matters a lot on wide tables or ones with large text/blob
        columns.

        mode="substring" is a literal case-insensitive substring match, the
        long-standing behaviour. mode="words" matches whole words through the
        engine's full-text search instead, and is only available where
        SUPPORTS_WORD_SEARCH is set. mode="exact" is plain equality, which is
        the only form an ordinary btree index on the column can serve — asking
        for a known id with a substring match reads the whole table to find it.

        An empty `order_by` means no ORDER BY at all. That matters more than it
        looks: on a search the sort is usually the expensive part, because the
        engine has to find every match before it can say which ones sort first,
        while an unordered LIMIT can stop as soon as it has enough rows."""
        raise NotImplementedError

    def search_row_count(self, database: Optional[str], schema: Optional[str], table: str,
                          columns: List[str], search_term: str,
                          mode: str = "substring",
                          filters: Optional[List[tuple]] = None) -> int:
        """Count of rows matching the same condition as search_rows. `columns`
        here are the columns to search (no separate select-list to narrow)."""
        raise NotImplementedError

    # ── fast-path helpers ────────────────────────────────────────────────────
    # Everything below has a safe default so a connector that doesn't implement
    # it just falls back to the old behaviour (order by everything, real
    # COUNT(*), OFFSET paging). Only the sources that can do better override.

    def _exact_where(self, columns: List[str], search_term: str) -> str:
        """Plain equality, quoted the way this connector quotes identifiers.

        This is the only form an ordinary btree index on the column can serve.
        The same lookup written as a substring match cannot use the index at
        all and reads every row: on a ten-million-row table that is the
        difference between a fraction of a millisecond and half a minute."""
        term = self._lit(search_term)
        return " OR ".join(f"{self._quote_col(c)} = '{term}'" for c in columns)

    def _where_for(self, columns: List[str], search_term: str, mode: str) -> str:
        """Maps the API's match mode onto this connector's WHERE clause.
        Subclasses that support more modes override and fall through to here."""
        if mode == "exact":
            return self._exact_where(columns, search_term)
        return self._search_where(columns, search_term)

    # ── column filters ───────────────────────────────────────────────────────
    # A filter is (column, operator, values). The caller has already checked
    # that the column exists and the operator is one of these; what happens
    # here is turning that into SQL with the values safely quoted.

    FILTER_OPS = {"eq", "ne", "gt", "gte", "lt", "lte", "in", "not_in",
                  "is_null", "not_null", "contains", "starts_with"}

    _COMPARISONS = {"eq": "=", "ne": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}

    def _filter_clause(self, column: str, op: str, values: List[str]) -> str:
        """One filter as a SQL predicate. Values are always quoted as string
        literals and left for the engine to coerce: writing a number unquoted
        would be a small optimisation and a large hole, since the value comes
        from a query string."""
        col = self._quote_col(column)
        if op in ("is_null", "not_null"):
            return f"{col} IS {'NOT ' if op == 'not_null' else ''}NULL"
        if op in ("in", "not_in"):
            if not values:
                # An empty IN list is a syntax error, and an empty NOT IN
                # matches everything. Say so explicitly rather than emit
                # something the engine will reject.
                return "1=0" if op == "in" else "1=1"
            joined = ", ".join(f"'{self._lit(v)}'" for v in values)
            return f"{col} {'NOT ' if op == 'not_in' else ''}IN ({joined})"
        value = self._lit(values[0] if values else "")
        if op == "contains":
            return self._search_where([column], values[0] if values else "")
        if op == "starts_with":
            return f"{self._as_text(col)} LIKE '{value}%'"
        return f"{col} {self._COMPARISONS[op]} '{value}'"

    def _filters_sql(self, filters: Optional[List[tuple]]) -> str:
        """All filters AND'd together, or an empty string when there are none."""
        if not filters:
            return ""
        return " AND ".join(f"({self._filter_clause(c, o, v)})" for c, o, v in filters)

    def apply_session_limits(self, statement_timeout_ms: Optional[int] = None,
                              work_mem: Optional[str] = None) -> None:
        """Per-connection guard rails, applied once when the connection is
        opened. Default is a no-op so a source that has no equivalent is
        unaffected."""
        return None

    def get_sort_key(self, database: Optional[str], schema: Optional[str], table: str) -> List[str]:
        """The narrowest set of columns that uniquely identifies a row — the
        primary key, or failing that a unique index. Returning [] means "no
        stable key known", and the caller keeps ordering by every column.

        This is the single biggest lever on a big table: ORDER BY over every
        column forces a sort of the whole heap, while ORDER BY the pkey walks
        an index that is already in that order."""
        return []

    def estimate_row_count(self, database: Optional[str], schema: Optional[str], table: str) -> Optional[int]:
        """Planner's row estimate, if the engine keeps one. Free (a catalog
        lookup) where a real COUNT(*) is a full scan. None if unavailable."""
        return None

    def fetch_rows_after(self, database: Optional[str], schema: Optional[str], table: str,
                          columns: List[str], key_columns: List[str],
                          after_values: Optional[list], limit: int) -> List[dict]:
        """Keyset ("seek") pagination: the page of rows immediately after
        `after_values` in `key_columns` order. Unlike OFFSET, the database
        doesn't read and throw away everything before the page, so page 10,000
        costs the same as page 1. `after_values=None` means the first page."""
        raise NotImplementedError

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.disconnect()


# ─────────────────────────────────────────────────────────────────────────────
# Oracle — single database per DSN, many schemas (owners)
# ─────────────────────────────────────────────────────────────────────────────
class OracleConnector(BaseConnector):
    display_name = "Oracle"
    LEVELS = ["schema"]
    # LOB types can't be used in ORDER BY/GROUP BY/comparisons without an
    # explicit conversion (e.g. DBMS_LOB/TO_CHAR); BLOB can't be compared at all.
    NON_SORTABLE_TYPES = {"CLOB", "NCLOB", "BLOB", "LONG", "LONG RAW", "BFILE"}

    def __init__(self, cfg: dict):
        self.user = _cfg(cfg, "user", "IT_CHECKS_ORACLE_USER")
        self.password = _cfg(cfg, "password", "IT_CHECKS_ORACLE_PASSWORD")
        self.dsn = _cfg(cfg, "dsn", "IT_CHECKS_ORACLE_DSN")
        self.conn = None

    def connect(self):
        import oracledb
        # tcp_connect_timeout keeps an unreachable host from hanging for minutes
        self.conn = oracledb.connect(user=self.user, password=self.password, dsn=self.dsn,
                                     tcp_connect_timeout=10)

    def disconnect(self):
        if self.conn:
            try:
                self.conn.close()
            except Exception:
                pass

    def list_schemas(self, database: Optional[str] = None) -> List[str]:
        cur = self.conn.cursor()
        try:
            cur.execute("SELECT DISTINCT OWNER FROM ALL_TABLES ORDER BY OWNER")
            return [r[0] for r in cur.fetchall()]
        finally:
            cur.close()

    def list_tables(self, database: Optional[str], schema: Optional[str]) -> List[str]:
        if not schema:
            raise ConnectorError("A schema must be selected to list Oracle tables.")
        cur = self.conn.cursor()
        try:
            cur.execute(
                "SELECT TABLE_NAME FROM ALL_TABLES WHERE OWNER = :owner ORDER BY TABLE_NAME",
                {"owner": schema.upper()},
            )
            return [r[0] for r in cur.fetchall()]
        finally:
            cur.close()

    def get_columns(self, database: Optional[str], schema: str, table: str) -> List[Dict[str, str]]:
        cur = self.conn.cursor()
        try:
            cur.execute(
                "SELECT COLUMN_NAME, DATA_TYPE FROM ALL_TAB_COLUMNS "
                "WHERE OWNER = :owner AND TABLE_NAME = :tbl ORDER BY COLUMN_ID",
                {"owner": schema.upper(), "tbl": table.upper()},
            )
            return [{"name": r[0], "type": r[1]} for r in cur.fetchall()]
        finally:
            cur.close()

    def _table_ref(self, database, schema, table) -> str:
        return f'"{schema.upper()}"."{table.upper()}"'

    def get_row_count(self, database: Optional[str], schema: str, table: str) -> int:
        cur = self.conn.cursor()
        try:
            cur.execute(f'SELECT /*+ PARALLEL(4) */ COUNT(*) FROM "{schema.upper()}"."{table.upper()}"')
            return cur.fetchone()[0]
        finally:
            cur.close()

    def get_null_counts(self, database: Optional[str], schema: str, table: str, columns: List[str]) -> Dict[str, int]:
        if not columns:
            return {}
        parts = [f'SUM(CASE WHEN "{c}" IS NULL THEN 1 ELSE 0 END)' for c in columns]
        cur = self.conn.cursor()
        try:
            cur.execute(f'SELECT {", ".join(parts)} FROM "{schema.upper()}"."{table.upper()}"')
            row = cur.fetchone()
            return {c: int(row[i] or 0) for i, c in enumerate(columns)}
        finally:
            cur.close()

    def get_duplicate_count(self, database: Optional[str], schema: str, table: str, columns: List[str]) -> int:
        if not columns:
            return 0
        col_list = ", ".join(f'"{c}"' for c in columns)
        cur = self.conn.cursor()
        try:
            cur.execute(f'''
                SELECT COUNT(*) FROM (
                    SELECT {col_list} FROM "{schema.upper()}"."{table.upper()}"
                    GROUP BY {col_list} HAVING COUNT(*) > 1
                )
            ''')
            return cur.fetchone()[0]
        finally:
            cur.close()

    def fetch_rows(self, database: Optional[str], schema: str, table: str,
                    columns: List[str], limit: int, order_by: Optional[List[str]] = None,
                    offset: int = 0, filters: Optional[List[tuple]] = None) -> List[dict]:
        col_list = ", ".join(f'"{c}"' for c in columns)
        where = self._filters_sql(filters)
        where_clause = f"WHERE {where}" if where else ""
        cur = self.conn.cursor()
        try:
            cur.execute(
                f'SELECT {col_list} FROM "{schema.upper()}"."{table.upper()}" {where_clause} {self._order_sql(order_by)} '
                f'OFFSET {int(offset)} ROWS FETCH NEXT {int(limit)} ROWS ONLY'
            )
            return [dict(zip(columns, r)) for r in cur.fetchall()]
        finally:
            cur.close()

    def filtered_row_count(self, database: Optional[str], schema: str, table: str,
                            filters: List[tuple]) -> int:
        where = self._filters_sql(filters)
        cur = self.conn.cursor()
        try:
            cur.execute(f'SELECT COUNT(*) FROM "{schema.upper()}"."{table.upper()}" '
                        f'{f"WHERE {where}" if where else ""}')
            return cur.fetchone()[0]
        finally:
            cur.close()

    def _as_text(self, col_sql: str) -> str:
        return f"TO_CHAR({col_sql})"

    def _search_where(self, columns: List[str], search_term: str) -> str:
        term = self._lit(search_term).upper()
        return " OR ".join(f'UPPER(TO_CHAR("{c}")) LIKE \'%{term}%\'' for c in columns)

    def search_rows(self, database: Optional[str], schema: str, table: str,
                     columns: List[str], search_term: str, limit: int,
                     order_by: Optional[List[str]] = None, offset: int = 0,
                     search_columns: Optional[List[str]] = None,
                     mode: str = "substring",
                     filters: Optional[List[tuple]] = None) -> List[dict]:
        col_list = ", ".join(f'"{c}"' for c in columns)
        where = self._and_where(self._where_for(search_columns or columns, search_term, mode),
                                self._filters_sql(filters))
        cur = self.conn.cursor()
        try:
            cur.execute(
                f'SELECT {col_list} FROM "{schema.upper()}"."{table.upper()}" WHERE {where} {self._order_sql(order_by)} '
                f'OFFSET {int(offset)} ROWS FETCH NEXT {int(limit)} ROWS ONLY'
            )
            return [dict(zip(columns, r)) for r in cur.fetchall()]
        finally:
            cur.close()

    def search_row_count(self, database: Optional[str], schema: str, table: str,
                          columns: List[str], search_term: str,
                          mode: str = "substring",
                          filters: Optional[List[tuple]] = None) -> int:
        where = self._and_where(self._where_for(columns, search_term, mode), self._filters_sql(filters))
        cur = self.conn.cursor()
        try:
            cur.execute(f'SELECT COUNT(*) FROM "{schema.upper()}"."{table.upper()}" WHERE {where}')
            return cur.fetchone()[0]
        finally:
            cur.close()


# ─────────────────────────────────────────────────────────────────────────────
# Snowflake (used as the target, and optionally as a source)
# ─────────────────────────────────────────────────────────────────────────────
class SnowflakeConnector(BaseConnector):
    display_name = "Snowflake"
    LEVELS = ["database", "schema"]
    # Snowflake honours backslash escape sequences inside a string literal.
    BACKSLASH_ESCAPES = True

    def __init__(self, cfg: dict):
        self.account = _cfg(cfg, "account", "IT_CHECKS_SNOWFLAKE_ACCOUNT")
        self.user = _cfg(cfg, "user", "IT_CHECKS_SNOWFLAKE_USER")
        self.private_key_path = _cfg(cfg, "private_key_path", "IT_CHECKS_SNOWFLAKE_KEY_PATH")
        self.private_key_password = _cfg(
            cfg, "private_key_password", "IT_CHECKS_SNOWFLAKE_KEY_PASSWORD", required=False, default=""
        )
        self.role = _cfg(cfg, "role", "IT_CHECKS_SNOWFLAKE_ROLE")
        self.warehouse = _cfg(cfg, "warehouse", "IT_CHECKS_SNOWFLAKE_WAREHOUSE")
        self.conn = None

    def _load_private_key(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.backends import default_backend

        if not os.path.exists(self.private_key_path):
            raise ConnectorError(f"Snowflake private key not found at {self.private_key_path}")
        with open(self.private_key_path, "rb") as f:
            key = serialization.load_pem_private_key(
                f.read(),
                password=self.private_key_password.encode() if self.private_key_password else None,
                backend=default_backend(),
            )
        return key.private_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )

    def connect(self):
        import snowflake.connector

        private_key = self._load_private_key()
        # No database/schema pinned at connect time — browsed and fully
        # qualified per-query instead.
        self.conn = snowflake.connector.connect(
            user=self.user,
            account=self.account,
            private_key=private_key,
            role=self.role,
            warehouse=self.warehouse,
            login_timeout=10,  # connect handshake only — running queries are never limited
        )

    def disconnect(self):
        if self.conn:
            try:
                self.conn.close()
            except Exception:
                pass

    def _show_name_column(self, sql: str) -> List[str]:
        """Run a SHOW ... command and pull out the 'name' column generically."""
        cur = self.conn.cursor()
        try:
            cur.execute(sql)
            cols = [d[0].upper() for d in cur.description]
            idx = cols.index("NAME")
            return sorted(r[idx] for r in cur.fetchall())
        finally:
            cur.close()

    def list_databases(self) -> List[str]:
        return self._show_name_column("SHOW DATABASES")

    def list_schemas(self, database: Optional[str]) -> List[str]:
        if not database:
            raise ConnectorError("A database must be selected to list Snowflake schemas.")
        return self._show_name_column(f'SHOW SCHEMAS IN DATABASE "{database}"')

    def list_tables(self, database: Optional[str], schema: Optional[str]) -> List[str]:
        if not database or not schema:
            raise ConnectorError("A database and schema must be selected to list Snowflake tables.")
        cur = self.conn.cursor()
        try:
            cur.execute(
                f'SELECT TABLE_NAME FROM "{database}".INFORMATION_SCHEMA.TABLES '
                f"WHERE TABLE_SCHEMA = %s ORDER BY TABLE_NAME",
                (schema.upper(),),
            )
            return [r[0] for r in cur.fetchall()]
        finally:
            cur.close()

    def get_columns(self, database: str, schema: str, table: str) -> List[Dict[str, str]]:
        cur = self.conn.cursor()
        try:
            cur.execute(
                f'SELECT COLUMN_NAME, DATA_TYPE FROM "{database}".INFORMATION_SCHEMA.COLUMNS '
                f"WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s ORDER BY ORDINAL_POSITION",
                (schema.upper(), table.upper()),
            )
            return [{"name": r[0], "type": r[1]} for r in cur.fetchall()]
        finally:
            cur.close()

    def _table_ref(self, database, schema, table) -> str:
        return f'"{database}"."{schema}"."{table.upper()}"'

    def get_row_count(self, database: str, schema: str, table: str) -> int:
        cur = self.conn.cursor()
        try:
            cur.execute(f'SELECT COUNT(*) FROM "{database}"."{schema}"."{table.upper()}"')
            return cur.fetchone()[0]
        finally:
            cur.close()

    def get_null_counts(self, database: str, schema: str, table: str, columns: List[str]) -> Dict[str, int]:
        if not columns:
            return {}
        parts = [f'SUM(CASE WHEN "{c}" IS NULL THEN 1 ELSE 0 END)' for c in columns]
        cur = self.conn.cursor()
        try:
            cur.execute(f'SELECT {", ".join(parts)} FROM "{database}"."{schema}"."{table.upper()}"')
            row = cur.fetchone()
            return {c: int(row[i] or 0) for i, c in enumerate(columns)}
        finally:
            cur.close()

    def get_duplicate_count(self, database: str, schema: str, table: str, columns: List[str]) -> int:
        if not columns:
            return 0
        col_list = ", ".join(f'"{c}"' for c in columns)
        cur = self.conn.cursor()
        try:
            cur.execute(f'''
                SELECT COUNT(*) FROM (
                    SELECT {col_list} FROM "{database}"."{schema}"."{table.upper()}"
                    GROUP BY {col_list} HAVING COUNT(*) > 1
                )
            ''')
            return cur.fetchone()[0]
        finally:
            cur.close()

    def fetch_rows(self, database: str, schema: str, table: str,
                    columns: List[str], limit: int, order_by: Optional[List[str]] = None,
                    offset: int = 0, filters: Optional[List[tuple]] = None) -> List[dict]:
        col_list = ", ".join(f'"{c}"' for c in columns)
        where = self._filters_sql(filters)
        where_clause = f"WHERE {where}" if where else ""
        cur = self.conn.cursor()
        try:
            cur.execute(
                f'SELECT {col_list} FROM "{database}"."{schema}"."{table.upper()}" {where_clause} '
                f'{self._order_sql(order_by)} LIMIT {int(limit)} OFFSET {int(offset)}'
            )
            return [dict(zip(columns, r)) for r in cur.fetchall()]
        finally:
            cur.close()

    def filtered_row_count(self, database: str, schema: str, table: str,
                            filters: List[tuple]) -> int:
        where = self._filters_sql(filters)
        cur = self.conn.cursor()
        try:
            cur.execute(f'SELECT COUNT(*) FROM "{database}"."{schema}"."{table.upper()}" '
                        f'{f"WHERE {where}" if where else ""}')
            return cur.fetchone()[0]
        finally:
            cur.close()

    def _as_text(self, col_sql: str) -> str:
        return f"TO_VARCHAR({col_sql})"

    def estimate_row_count(self, database: str, schema: str, table: str) -> Optional[int]:
        # Snowflake keeps ROW_COUNT in its metadata, so this answers instantly
        # where COUNT(*) has to spin up the warehouse and scan micro-partitions.
        cur = self.conn.cursor()
        try:
            cur.execute(
                f'SELECT ROW_COUNT FROM "{database}".INFORMATION_SCHEMA.TABLES '
                f"WHERE TABLE_SCHEMA = '{schema.upper()}' AND TABLE_NAME = '{table.upper()}'"
            )
            row = cur.fetchone()
            return int(row[0]) if row and row[0] is not None else None
        except Exception:
            return None
        finally:
            cur.close()

    def _search_where(self, columns: List[str], search_term: str) -> str:
        term = self._lit(search_term)
        # TO_VARCHAR handles VARIANT/OBJECT/ARRAY columns too, not just scalars.
        return " OR ".join(f'TO_VARCHAR("{c}") ILIKE \'%{term}%\'' for c in columns)

    def search_rows(self, database: str, schema: str, table: str,
                     columns: List[str], search_term: str, limit: int,
                     order_by: Optional[List[str]] = None, offset: int = 0,
                     search_columns: Optional[List[str]] = None,
                     mode: str = "substring",
                     filters: Optional[List[tuple]] = None) -> List[dict]:
        col_list = ", ".join(f'"{c}"' for c in columns)
        where = self._and_where(self._where_for(search_columns or columns, search_term, mode),
                                self._filters_sql(filters))
        cur = self.conn.cursor()
        try:
            cur.execute(
                f'SELECT {col_list} FROM "{database}"."{schema}"."{table.upper()}" WHERE {where} '
                f'{self._order_sql(order_by)} LIMIT {int(limit)} OFFSET {int(offset)}'
            )
            return [dict(zip(columns, r)) for r in cur.fetchall()]
        finally:
            cur.close()

    def search_row_count(self, database: str, schema: str, table: str,
                          columns: List[str], search_term: str,
                          mode: str = "substring",
                          filters: Optional[List[tuple]] = None) -> int:
        where = self._and_where(self._where_for(columns, search_term, mode), self._filters_sql(filters))
        cur = self.conn.cursor()
        try:
            cur.execute(f'SELECT COUNT(*) FROM "{database}"."{schema}"."{table.upper()}" WHERE {where}')
            return cur.fetchone()[0]
        finally:
            cur.close()


# ─────────────────────────────────────────────────────────────────────────────
# PostgreSQL — connections are per-database, so browsing across databases
# means reconnecting; list_databases() uses a "maintenance" database (usually
# the default "postgres" db) to see what else exists.
# ─────────────────────────────────────────────────────────────────────────────
class PostgreSQLConnector(BaseConnector):
    display_name = "PostgreSQL"
    LEVELS = ["database", "schema"]
    SUPPORTS_VECTOR_SEARCH = True
    VECTOR_TYPES = {"vector", "halfvec", "sparsevec"}
    VECTOR_METRICS = {"cosine": "<=>", "l2": "<->", "inner_product": "<#>"}
    MAX_VECTOR_DIMENSIONS = 16_000
    SUPPORTS_KEYSET = True
    SUPPORTS_WORD_SEARCH = True

    # Dictionary used for word search. It has to be the same one the index was
    # built with — an index on to_tsvector('english', col) is invisible to a
    # query using any other config, and the search silently falls back to
    # reading the whole table.
    TEXT_SEARCH_CONFIG = "english"
    # xml and (non-binary) json have no default equality/ordering operator in
    # Postgres — jsonb is fine and intentionally not included here.
    NON_SORTABLE_TYPES = {"XML", "JSON"}

    def __init__(self, cfg: dict):
        self.host = _cfg(cfg, "host", "IT_CHECKS_PG_HOST")
        self.port = int(cfg.get("port") or os.environ.get("IT_CHECKS_PG_PORT") or 5432)
        self.user = _cfg(cfg, "user", "IT_CHECKS_PG_USER")
        self.password = _cfg(cfg, "password", "IT_CHECKS_PG_PASSWORD")
        self.maintenance_database = cfg.get("maintenance_database") or "postgres"
        self.conn = None
        self._current_db = None
        self._session_limits = None
        self._connection_options = dict(cfg)

    def connect(self):
        self._ensure_conn(self.maintenance_database)

    def disconnect(self):
        if self.conn:
            try:
                self.conn.close()
            except Exception:
                pass

    def _ensure_conn(self, database: Optional[str]):
        db = database or self.maintenance_database
        if self.conn and not self.conn.closed and self._current_db == db:
            return
        if self.conn:
            try:
                self.conn.close()
            except Exception:
                pass
        import psycopg2
        cfg = getattr(self, "_connection_options", {})
        extra = {name: cfg[name] for name in ("sslmode", "sslrootcert", "sslcert", "sslkey") if cfg.get(name)}
        self.conn = psycopg2.connect(
            host=self.host, port=self.port, user=self.user, password=self.password,
            dbname=db, connect_timeout=int(cfg.get("connect_timeout") or os.environ.get("DATA_API_CONNECT_TIMEOUT_SECONDS", "5")),
            application_name="smarthub-source", keepalives=1, keepalives_idle=30,
            keepalives_interval=10, keepalives_count=3, **extra,
        )
        # Read-only work. Autocommit keeps a pooled connection from sitting
        # idle in a transaction, and a failed statement (a swallowed estimate,
        # a bad cast in a filter) can't leave it in the aborted state where
        # every later query on it fails.
        self.conn.autocommit = True
        if getattr(self, "_api_read_only", False):
            self.conn.set_session(readonly=True)
        self._current_db = db
        # Switching database opens a new backend, so limits set on the old one
        # are gone and have to be reapplied.
        if self._session_limits:
            self.apply_session_limits(*self._session_limits)

    def apply_session_limits(self, statement_timeout_ms: Optional[int] = None,
                              work_mem: Optional[str] = None) -> None:
        """statement_timeout stops a pathological query from holding a
        connection open indefinitely; the caller gets an error instead of
        waiting forever.

        work_mem matters more than it looks on this workload. A bitmap scan
        that doesn't fit in work_mem degrades to tracking whole pages rather
        than individual rows, and then every row on those pages has to be
        rechecked against the WHERE clause — hundreds of thousands of rows
        that were never candidates. Giving the bitmap room to stay exact is
        the difference between rechecking the matches and rechecking the
        table. It is per-connection, and this pool holds few connections."""
        self._session_limits = (statement_timeout_ms, work_mem)
        if not self.conn:
            return
        cur = self.conn.cursor()
        try:
            if statement_timeout_ms is not None:
                cur.execute(f"SET statement_timeout = {int(statement_timeout_ms)}")
            if work_mem:
                # Whitelisted rather than interpolated — this lands in a SET.
                if not re.fullmatch(r"\d{1,6}(kB|MB|GB)", work_mem):
                    raise ConnectorError(f"Invalid work_mem value: {work_mem!r}")
                cur.execute(f"SET work_mem = '{work_mem}'")
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        finally:
            cur.close()

    def list_databases(self) -> List[str]:
        self._ensure_conn(self.maintenance_database)
        cur = self.conn.cursor()
        try:
            # only databases this login may connect to (rdsadmin and friends have datallowconn = false)
            cur.execute("SELECT datname FROM pg_database WHERE NOT datistemplate AND datallowconn "
                        "AND has_database_privilege(datname, 'CONNECT') ORDER BY datname")
            names = [r[0] for r in cur.fetchall()]
        finally:
            cur.close()
        # CONNECT alone lets in empty ones like "postgres". Keep only databases
        # with at least one table this login can SELECT, same rule as schemas.
        return [db for db in names if self._has_readable_table(db)]

    def _has_readable_table(self, database: str) -> bool:
        import psycopg2
        cfg = getattr(self, "_connection_options", {})
        extra = {name: cfg[name] for name in ("sslmode", "sslrootcert", "sslcert", "sslkey") if cfg.get(name)}
        try:
            conn = psycopg2.connect(
                host=self.host, port=self.port, user=self.user, password=self.password,
                dbname=database, connect_timeout=int(cfg.get("connect_timeout") or os.environ.get("DATA_API_CONNECT_TIMEOUT_SECONDS", "5")),
                application_name="smarthub-source", **extra,
            )
        except Exception:
            return False
        try:
            conn.autocommit = True
            cur = conn.cursor()
            cur.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname NOT IN ('pg_catalog', 'information_schema') "
                "AND n.nspname NOT LIKE 'pg_toast%' AND n.nspname NOT LIKE 'pg_temp%' "
                "AND c.relkind IN ('r', 'p', 'v', 'm', 'f') "
                "AND has_schema_privilege(n.oid, 'USAGE') AND has_table_privilege(c.oid, 'SELECT'))"
            )
            return bool(cur.fetchone()[0])
        except Exception:
            return False
        finally:
            conn.close()

    def list_schemas(self, database: Optional[str]) -> List[str]:
        self._ensure_conn(database)
        cur = self.conn.cursor()
        try:
            # only schemas this login can use and that hold something it can SELECT from
            cur.execute(
                "SELECT n.nspname FROM pg_namespace n "
                "WHERE n.nspname NOT IN ('pg_catalog', 'information_schema') "
                "AND n.nspname NOT LIKE 'pg_toast%' AND n.nspname NOT LIKE 'pg_temp%' "
                "AND has_schema_privilege(n.oid, 'USAGE') "
                "AND EXISTS (SELECT 1 FROM pg_class c WHERE c.relnamespace = n.oid "
                "AND c.relkind IN ('r', 'p', 'v', 'm', 'f') AND has_table_privilege(c.oid, 'SELECT')) "
                "ORDER BY n.nspname"
            )
            return [r[0] for r in cur.fetchall()]
        finally:
            cur.close()

    def list_tables(self, database: Optional[str], schema: Optional[str]) -> List[str]:
        if not schema:
            raise ConnectorError("A schema must be selected to list PostgreSQL tables.")
        self._ensure_conn(database)
        cur = self.conn.cursor()
        try:
            cur.execute(
                # information_schema lists anything with any privilege; keep the ones we can read.
                # Schema matched as named, falling back to lowercase, so quoted names work too.
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = COALESCE((SELECT nspname FROM pg_namespace WHERE nspname = %s), lower(%s)) "
                "AND has_schema_privilege(table_schema, 'USAGE') "
                "AND has_table_privilege(format('%%I.%%I', table_schema, table_name), 'SELECT') "
                "ORDER BY table_name",
                (schema, schema),
            )
            return [r[0] for r in cur.fetchall()]
        finally:
            cur.close()

    def _real_name(self, schema: str, table: str) -> tuple:
        # Tables made with quotes keep their case (RDH_RIMDOCS_...), so lowercasing
        # the name finds nothing. Look up the real name: exact match first, then
        # the lowercase one, then any case match.
        cache = self.__dict__.setdefault("_name_cache", {})
        key = (self._current_db, schema, table)
        if key in cache:
            return cache[key]
        cur = self.conn.cursor()
        try:
            cur.execute(
                "SELECT n.nspname, c.relname FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE lower(n.nspname) = lower(%s) AND lower(c.relname) = lower(%s) "
                "AND c.relkind IN ('r', 'p', 'v', 'm', 'f') "
                "ORDER BY (n.nspname = %s AND c.relname = %s) DESC, "
                "(n.nspname = lower(%s) AND c.relname = lower(%s)) DESC LIMIT 1",
                (schema, table, schema, table, schema, table),
            )
            row = cur.fetchone()
        finally:
            cur.close()
        if not row:
            return schema.lower(), table.lower()
        cache[key] = (row[0], row[1])
        return cache[key]

    def _ref(self, schema: str, table: str) -> str:
        s, t = self._real_name(schema, table)
        return '"' + s.replace('"', '""') + '"."' + t.replace('"', '""') + '"'

    def get_columns(self, database: Optional[str], schema: str, table: str) -> List[Dict[str, str]]:
        self._ensure_conn(database)
        cur = self.conn.cursor()
        try:
            # data_type is useless for anything that isn't a built-in scalar: a
            # pgvector column reports USER-DEFINED and a text[] reports ARRAY,
            # neither of which says what is actually in there. udt_name does —
            # "vector", "_text" — so it comes back alongside rather than
            # replacing data_type, which callers already compare against other
            # dialects.
            cur.execute(
                "SELECT column_name, data_type, udt_name FROM information_schema.columns "
                "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
                self._real_name(schema, table),
            )
            return [{"name": r[0], "type": r[1], "udt": r[2]} for r in cur.fetchall()]
        finally:
            cur.close()

    @staticmethod
    def _vector_identifier(name: str) -> str:
        """Quote a catalog-validated identifier; values are bound separately."""
        if not isinstance(name, str) or not name or "\x00" in name:
            raise ConnectorError("Invalid vector-search identifier.")
        return '"' + name.replace('"', '""') + '"'

    def get_vector_columns(self, database: Optional[str], schema: str, table: str) -> List[dict]:
        """Read pgvector type/dimension metadata, without scanning source rows.

        Verify extension membership instead of trusting a type named 'vector'.
        A bare vector/halfvec/sparsevec has no fixed typmod: dimensions is None.
        Domains and binary bit vectors are deliberately outside this endpoint.
        """
        self._ensure_conn(database)
        cur = self.conn.cursor()
        try:
            cur.execute(
                """
                SELECT a.attname, typ.typname, a.atttypmod, tn.nspname, ext.extversion
                  FROM pg_catalog.pg_attribute a
                  JOIN pg_catalog.pg_class rel ON rel.oid = a.attrelid
                  JOIN pg_catalog.pg_namespace ns ON ns.oid = rel.relnamespace
                  JOIN pg_catalog.pg_type typ ON typ.oid = a.atttypid
                  JOIN pg_catalog.pg_namespace tn ON tn.oid = typ.typnamespace
                  JOIN pg_catalog.pg_depend dep
                    ON dep.classid = 'pg_catalog.pg_type'::regclass
                   AND dep.objid = typ.oid AND dep.deptype = 'e'
                   AND dep.refclassid = 'pg_catalog.pg_extension'::regclass
                  JOIN pg_catalog.pg_extension ext ON ext.oid = dep.refobjid
                 WHERE ns.nspname = %s AND rel.relname = %s
                   AND a.attnum > 0 AND NOT a.attisdropped
                   AND ext.extname = 'vector'
                   AND typ.typname IN ('vector', 'halfvec', 'sparsevec')
                 ORDER BY a.attnum
                """,
                self._real_name(schema, table),
            )
            columns = [
                {"name": name, "type": kind,
                 "dimensions": int(dim) if dim is not None and dim > 0 else None,
                 "type_schema": type_schema, "extension_version": version, "ann_indexes": []}
                for name, kind, dim, type_schema, version in cur.fetchall()
            ]
            cur.execute("""
                SELECT a.attname, ix.relname, am.amname, opc.opcname
                FROM pg_catalog.pg_index i
                JOIN pg_catalog.pg_class rel ON rel.oid = i.indrelid
                JOIN pg_catalog.pg_namespace ns ON ns.oid = rel.relnamespace
                JOIN pg_catalog.pg_class ix ON ix.oid = i.indexrelid
                JOIN pg_catalog.pg_am am ON am.oid = ix.relam
                JOIN pg_catalog.pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = i.indkey[0]
                JOIN pg_catalog.pg_opclass opc ON opc.oid = i.indclass[0]
                WHERE ns.nspname = %s AND rel.relname = %s
                  AND i.indisvalid AND i.indisready AND i.indnkeyatts = 1
                  AND i.indpred IS NULL AND i.indexprs IS NULL
                  AND am.amname IN ('hnsw', 'ivfflat')
            """, self._real_name(schema, table))
            by_name = {c["name"]: c for c in columns}
            for name, index, method, opclass in cur.fetchall():
                c = by_name.get(name)
                if not c:
                    continue
                suffix = opclass.removeprefix(c["type"] + "_")
                metric = {"cosine_ops": "cosine", "l2_ops": "l2", "ip_ops": "inner_product"}.get(suffix)
                if metric:
                    c["ann_indexes"].append({"name": index, "method": method, "metric": metric})
            return columns
        finally:
            cur.close()

    @classmethod
    def _vector_literal(cls, values: list, kind: str, metric: str) -> str:
        """Validate and round exactly as the target storage precision requires.

        No global psycopg2 adapters are registered: existing /rows serialization
        stays unchanged. SQL receives this literal as a bound parameter.
        """
        if kind not in cls.VECTOR_TYPES or metric not in cls.VECTOR_METRICS:
            raise ConnectorError("Unsupported vector type or distance metric.")
        if not isinstance(values, list) or not 1 <= len(values) <= cls.MAX_VECTOR_DIMENSIONS:
            raise ConnectorError(f"query_vector must contain 1 to {cls.MAX_VECTOR_DIMENSIONS} numbers.")
        precision = "e" if kind == "halfvec" else "f"
        rounded = []
        for value in values:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ConnectorError("query_vector must contain JSON numbers, not strings or booleans.")
            try:
                value = float(value)
                if not math.isfinite(value):
                    raise ValueError
                value = struct.unpack(precision, struct.pack(precision, value))[0]
                if not math.isfinite(value):
                    raise ValueError
            except (ValueError, OverflowError, struct.error):
                raise ConnectorError(f"query_vector contains a non-finite or out-of-range {kind} value.") from None
            rounded.append(value)
        if metric == "cosine" and not any(v != 0 for v in rounded):
            raise ConnectorError("Cosine search requires a non-zero query vector at the column's storage precision.")
        if kind == "sparsevec":
            items = ",".join(f"{i}:{v!r}" for i, v in enumerate(rounded, 1) if v != 0)
            return "{" + items + "}/" + str(len(rounded))
        return "[" + ",".join(repr(v) for v in rounded) + "]"

    def _vector_filter_sql(self, filters: Optional[List[tuple]], params: dict) -> str:
        """Same filter operators as /rows, but fully parameterized for this path."""
        parts = []
        for i, (column, op, values) in enumerate(filters or []):
            if op not in self.FILTER_OPS:
                raise ConnectorError("Unsupported vector-search filter operator.")
            col = self._vector_identifier(column)
            if op in ("is_null", "not_null"):
                parts.append(f"{col} IS {'NOT ' if op == 'not_null' else ''}NULL")
                continue
            if not values:
                raise ConnectorError("A vector-search filter requires a value.")
            slots = []
            for j, value in enumerate(values):
                name = f"filter_{i}_{j}"
                params[name] = value
                slots.append(f"%({name})s")
            if op in ("in", "not_in"):
                parts.append(f"{col} {'NOT ' if op == 'not_in' else ''}IN ({', '.join(slots)})")
            elif op == "contains":
                params[f"filter_{i}_0"] = "%" + values[0] + "%"
                parts.append(f"CAST({col} AS TEXT) ILIKE {slots[0]}")
            elif op == "starts_with":
                params[f"filter_{i}_0"] = values[0] + "%"
                parts.append(f"CAST({col} AS TEXT) LIKE {slots[0]}")
            else:
                parts.append(f"{col} {self._COMPARISONS[op]} {slots[0]}")
        return " AND ".join(f"({p})" for p in parts)

    def _vector_statement(self, schema: str, table: str, columns: List[str],
                          vector_meta: dict, query_vector: list, limit: int,
                          metric: str, filters: Optional[List[tuple]],
                          search_mode: str, key_columns: Optional[List[str]]) -> Tuple[str, dict]:
        """SQL + bound params for one nearest-neighbour query.

        auto leaves the direct distance ORDER BY eligible for an existing ANN
        index. exact adds +0 to that expression, avoiding that ANN ordering path
        without changing pooled session settings. The last selected column is
        always the distance.
        """
        if search_mode not in ("auto", "exact"):
            raise ConnectorError("search_mode must be 'auto' or 'exact'.")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ConnectorError("Vector result limit must be a positive integer.")
        kind = vector_meta["type"]
        literal = self._vector_literal(query_vector, kind, metric)
        dims = vector_meta.get("dimensions")
        if dims is not None and dims != len(query_vector):
            raise ConnectorError(
                f"Column '{vector_meta['name']}' needs {dims} dimensions; "
                f"query_vector has {len(query_vector)}."
            )
        q = self._vector_identifier
        ref = self._ref(schema, table)
        col = q(vector_meta["name"])
        ext = q(vector_meta["type_schema"])
        cast = f"{ext}.{q(kind)}"
        op = f"OPERATOR({ext}.{self.VECTOR_METRICS[metric]})"
        params = {"query_vector": literal, "result_limit": limit}
        predicates = [f"{col} IS NOT NULL"]
        operand = col
        if dims is None:
            # CASE protects the distance operator independently of predicate
            # evaluation order, which PostgreSQL is allowed to rearrange.
            dim_expr = (f"pg_catalog.split_part({col}::text, '/', 2)::integer"
                        if kind == "sparsevec" else f"{ext}.vector_dims({col})")
            params["dimensions"] = len(query_vector)
            dim_check = f"{dim_expr} = %(dimensions)s"
            predicates.append(dim_check)
            operand = f"(CASE WHEN {dim_check} THEN {col} ELSE NULL END)"
        if metric == "cosine":
            norm = "vector_norm" if kind == "vector" else "l2_norm"
            predicates.append(f"{ext}.{norm}({col}) > 0")
        filter_sql = self._vector_filter_sql(filters, params)
        if filter_sql:
            predicates.append(filter_sql)
        distance = f"{operand} {op} %(query_vector)s::{cast}"
        order_distance = f"({distance}) + 0" if search_mode == "exact" else distance
        selected = ", ".join([q(c) for c in columns] + [f"{distance} AS __vector_distance"])
        # Exact mode breaks equal-distance ties on a known unique key so the
        # result is repeatable; ANN ordering cannot honour a secondary key.
        tie = ""
        if search_mode == "exact" and key_columns:
            tie = ", " + ", ".join(q(c) + " ASC" for c in key_columns)
        statement = (
            f"SELECT {selected} FROM {ref} "
            f"WHERE {' AND '.join(f'({p})' for p in predicates)} "
            f"ORDER BY {order_distance} ASC{tie} LIMIT %(result_limit)s"
        )
        return statement, params

    @staticmethod
    def _pgvector_version(vector_meta: dict) -> tuple:
        return tuple(int(v) for v in vector_meta.get("extension_version", "0.0.0").split(".")[:3] if v.isdigit())

    def _apply_ann_settings(self, cur, vector_meta: dict, ef_search: int, probes: int,
                            max_scan_tuples: int, max_probes: int) -> None:
        """Transaction-local ANN tuning; reset automatically at transaction end."""
        cur.execute("SELECT set_config('hnsw.ef_search', %s, true), set_config('ivfflat.probes', %s, true)",
                    (str(ef_search), str(probes)))
        if self._pgvector_version(vector_meta) >= (0, 8, 0):
            cur.execute("SELECT set_config('hnsw.iterative_scan', 'strict_order', true), "
                        "set_config('hnsw.max_scan_tuples', %s, true), "
                        "set_config('ivfflat.iterative_scan', 'relaxed_order', true), "
                        "set_config('ivfflat.max_probes', %s, true)",
                        (str(max_scan_tuples), str(max_probes)))

    @staticmethod
    def _set_statement_timeout(cur, timeout_ms: int) -> None:
        cur.execute("SELECT set_config('statement_timeout', %s, true)", (str(max(1, int(timeout_ms))),))

    @contextmanager
    def read_only_transaction(self, database: Optional[str], timeout_ms: int, repeatable_read: bool = False):
        """One READ ONLY transaction on the pooled connection with local limits.

        Every setting made inside uses set_config(..., true), so it ends with
        the transaction and never leaks into the next request on this socket.
        repeatable_read gives multi-statement callers one consistent snapshot;
        a read-only transaction can never fail serialization.
        """
        self._ensure_conn(database)
        cur = self.conn.cursor()
        begun = False
        try:
            cur.execute("BEGIN READ ONLY ISOLATION LEVEL REPEATABLE READ" if repeatable_read else "BEGIN READ ONLY")
            begun = True
            cur.execute("SELECT set_config('statement_timeout', %s, true), set_config('lock_timeout', '1000', true)", (str(timeout_ms),))
            yield cur
            cur.execute("COMMIT")
            begun = False
        finally:
            if begun:
                try:
                    cur.execute("ROLLBACK")
                except Exception:
                    self.disconnect()
            cur.close()

    @staticmethod
    def _vector_matches(results: list, columns: List[str], metric: str) -> List[dict]:
        matches = []
        for result in results:
            value = float(result[-1])
            if not math.isfinite(value):
                raise ConnectorError("Source produced a non-finite distance; check the stored embeddings.")
            item = {"row": dict(zip(columns, result[:-1])), "distance": value}
            if metric == "cosine":
                item["similarity"] = 1.0 - value
            elif metric == "inner_product":
                item["similarity"] = -value
            matches.append(item)
        # Also restores distance order if a DBA enabled relaxed ANN scans.
        matches.sort(key=lambda item: item["distance"])
        return matches

    def search_vector_rows(self, database: Optional[str], schema: str, table: str,
                           columns: List[str], vector_meta: dict, query_vector: list,
                           limit: int, metric: str = "cosine",
                           filters: Optional[List[tuple]] = None,
                           search_mode: str = "auto", key_columns: Optional[List[str]] = None,
                           timeout_ms: int = 5000, ef_search: int = 100,
                           max_scan_tuples: int = 20000, probes: int = 10,
                           max_probes: int = 100) -> List[dict]:
        """Nearest matches on existing columns, using the checked-out connection.
        No DDL, writes, or adapters."""
        statement, params = self._vector_statement(schema, table, columns, vector_meta, query_vector,
                                                   limit, metric, filters, search_mode, key_columns)
        with self.read_only_transaction(database, timeout_ms) as cur:
            if search_mode == "auto":
                self._apply_ann_settings(cur, vector_meta, ef_search, probes, max_scan_tuples, max_probes)
            cur.execute(statement, params)
            results = cur.fetchall()
        matches = self._vector_matches(results, columns, metric)
        for rank, item in enumerate(matches, 1):
            item["rank"] = rank
        return matches

    # ------------------------------------------------------------------
    # Hybrid retrieval primitives (dense + keyword). The caller owns the
    # transaction via read_only_transaction() and the fusion policy; these
    # methods only build and run SQL on the cursor they are given.
    # ------------------------------------------------------------------

    def get_text_search_indexes(self, database: Optional[str], schema: str, table: str) -> List[dict]:
        """Valid, non-partial GIN full-text indexes: either
        to_tsvector('<config>', column) expressions or plain tsvector columns.
        Read from the catalog; no rows are scanned."""
        self._ensure_conn(database)
        cur = self.conn.cursor()
        try:
            cur.execute("""
                SELECT ix.relname, pg_catalog.pg_get_indexdef(i.indexrelid),
                       CASE WHEN i.indexprs IS NULL THEN a.attname END,
                       CASE WHEN i.indexprs IS NULL THEN t.typname END
                  FROM pg_catalog.pg_index i
                  JOIN pg_catalog.pg_class rel ON rel.oid = i.indrelid
                  JOIN pg_catalog.pg_namespace ns ON ns.oid = rel.relnamespace
                  JOIN pg_catalog.pg_class ix ON ix.oid = i.indexrelid
                  JOIN pg_catalog.pg_am am ON am.oid = ix.relam
                  LEFT JOIN pg_catalog.pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = i.indkey[0]
                  LEFT JOIN pg_catalog.pg_type t ON t.oid = a.atttypid
                 WHERE ns.nspname = %s AND rel.relname = %s AND am.amname = 'gin'
                   AND i.indisvalid AND i.indisready AND i.indpred IS NULL AND i.indnkeyatts = 1
            """, self._real_name(schema, table))
            found = []
            for name, definition, column, type_name in cur.fetchall():
                if column and type_name == "tsvector":
                    found.append({"index": name, "column": column, "config": None})
                    continue
                m = self._TSVECTOR_INDEX_RE.search(definition or "")
                if m:
                    found.append({"index": name, "column": m.group(3), "config": m.group(1)})
            return found
        finally:
            cur.close()

    def estimate_rows(self, database: Optional[str], schema: str, table: str) -> Optional[int]:
        """Planner's row estimate from pg_class (catalog only; never counts).
        None when the table has never been analyzed."""
        self._ensure_conn(database)
        cur = self.conn.cursor()
        try:
            cur.execute("""SELECT c.reltuples::bigint FROM pg_catalog.pg_class c
                           JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                           WHERE n.nspname = %s AND c.relname = %s""", self._real_name(schema, table))
            row = cur.fetchone()
            return int(row[0]) if row and row[0] is not None and row[0] >= 0 else None
        finally:
            cur.close()

    def is_unique_column(self, database: Optional[str], schema: str, table: str, column: str) -> bool:
        """True when a valid, non-partial unique index (or primary key) covers
        exactly this one column, so a lookup by it is one index probe."""
        self._ensure_conn(database)
        cur = self.conn.cursor()
        try:
            cur.execute("""
                SELECT EXISTS (
                  SELECT 1 FROM pg_catalog.pg_index i
                    JOIN pg_catalog.pg_class rel ON rel.oid = i.indrelid
                    JOIN pg_catalog.pg_namespace ns ON ns.oid = rel.relnamespace
                    JOIN pg_catalog.pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = i.indkey[0]
                   WHERE ns.nspname = %s AND rel.relname = %s AND a.attname = %s
                     AND i.indisunique AND i.indisvalid AND i.indpred IS NULL
                     AND i.indexprs IS NULL AND i.indnkeyatts = 1)""",
                        (*self._real_name(schema, table), column))
            return bool(cur.fetchone()[0])
        finally:
            cur.close()

    def explain_plan(self, database: Optional[str], statement: str, params: dict, timeout_ms: int = 5000) -> str:
        """Planner-only EXPLAIN (no ANALYZE: nothing is executed or scanned)
        in a read-only transaction, using the table's real statistics."""
        with self.read_only_transaction(database, timeout_ms) as cur:
            cur.execute("EXPLAIN " + statement, params)
            return "\n".join(r[0] for r in cur.fetchall())

    def prefer_index(self, cur) -> tuple:
        """For one nearest-neighbour statement in auto mode: keep the planner
        off sequential scans (as pgvector's docs suggest) so stale statistics
        cannot turn an index lookup into a full-table read and sort, and turn
        JIT off, whose compile time only pays off for long analytic queries.
        Returns the previous values for restore_planner()."""
        cur.execute("SELECT current_setting('enable_seqscan'), current_setting('jit')")
        previous = cur.fetchone()
        cur.execute("SELECT set_config('enable_seqscan', 'off', true), set_config('jit', 'off', true)")
        return previous

    @staticmethod
    def restore_planner(cur, previous: tuple) -> None:
        cur.execute("SELECT set_config('enable_seqscan', %s, true), set_config('jit', %s, true)", previous)

    def statistics_health(self, database: Optional[str], schema: str, table: str, column: str) -> dict:
        """Planner statistics for one column (catalog views only, nothing scanned)."""
        self._ensure_conn(database)
        cur = self.conn.cursor()
        try:
            cur.execute("""
                SELECT s.null_frac, t.n_live_tup, t.n_mod_since_analyze,
                       GREATEST(t.last_analyze, t.last_autoanalyze) IS NOT NULL
                  FROM pg_catalog.pg_stat_user_tables t
                  LEFT JOIN pg_catalog.pg_stats s
                    ON s.schemaname = t.schemaname AND s.tablename = t.relname AND s.attname = %s
                 WHERE t.schemaname = %s AND t.relname = %s""", (column, *self._real_name(schema, table)))
            row = cur.fetchone()
            if not row:
                return {}
            return {"null_frac": None if row[0] is None else float(row[0]), "live_rows": row[1],
                    "modified_since_analyze": row[2], "ever_analyzed": bool(row[3])}
        finally:
            cur.close()

    # Matches pg_get_indexdef output such as
    #   ... USING gin (to_tsvector('english'::regconfig, chunk_text))
    #   ... USING gin (to_tsvector('english'::regconfig, "Chunk Text"))
    _TSVECTOR_INDEX_RE = re.compile(
        r"USING gin \(to_tsvector\('([A-Za-z0-9_.]+)'::regconfig, (\"?)((?:[^\"]|\"\")+?)\2\)\)$")

    def dense_candidates(self, cur, schema: str, table: str, key_columns: List[str],
                         vector_meta: dict, query_vector: list, limit: int, metric: str,
                         filters: Optional[List[tuple]], search_mode: str) -> List[tuple]:
        """[(key_tuple, distance)] nearest first. Selects only the key, so wide
        text columns are fetched once, for the fused winners."""
        statement, params = self._vector_statement(schema, table, key_columns, vector_meta, query_vector,
                                                   limit, metric, filters, search_mode, key_columns)
        cur.execute(statement, params)
        matches = self._vector_matches(cur.fetchall(), key_columns, metric)
        return [(tuple(m["row"][c] for c in key_columns), m["distance"]) for m in matches]

    def _sparse_statement(self, schema: str, table: str, key_columns: List[str],
                          text_column: str, text_config: str, stored_tsvector: bool, query_text: str,
                          limit: int, filters: Optional[List[tuple]]) -> Tuple[str, dict]:
        """SQL + bound params for the keyword leg (see sparse_candidates)."""
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ConnectorError("Keyword result limit must be a positive integer.")
        q = self._vector_identifier
        ref = self._ref(schema, table)
        col = q(text_column)
        if not isinstance(text_config, str) or not re.fullmatch(r"[A-Za-z0-9_.]+", text_config):
            raise ConnectorError("Invalid text search configuration.")
        # A stored tsvector column is used as-is. Otherwise the expression is
        # written exactly like the index expression so the planner matches it.
        # Either way the query must be parsed with the same config.
        tsv = col if stored_tsvector else f"to_tsvector('{text_config}'::regconfig, {col})"
        params = {"query_text": query_text, "result_limit": limit}
        filter_sql = self._vector_filter_sql(filters, params)
        inner_cols = list(dict.fromkeys(list(key_columns) + [f[0] for f in (filters or [])]))
        keys = ", ".join(q(c) for c in key_columns)
        statement = (
            f"WITH matched AS MATERIALIZED ("
            f"SELECT {', '.join(q(c) for c in inner_cols)}, "
            f"ts_rank_cd({tsv}, __q.query, 32) AS __sparse_score "
            f"FROM {ref}, websearch_to_tsquery('{text_config}'::regconfig, %(query_text)s) AS __q(query) "
            f"WHERE {tsv} @@ __q.query) "
            f"SELECT {keys}, __sparse_score FROM matched "
            f"{'WHERE ' + filter_sql + ' ' if filter_sql else ''}"
            f"ORDER BY __sparse_score DESC, {keys} LIMIT %(result_limit)s"
        )
        return statement, params

    def sparse_candidates(self, cur, schema: str, table: str, key_columns: List[str],
                          text_column: str, text_config: str, stored_tsvector: bool, query_text: str,
                          limit: int, filters: Optional[List[tuple]], timeout_ms: int) -> Tuple[List[tuple], bool]:
        """Keyword leg: [(key_tuple, score)] best first, plus timed_out.

        The match runs inside a MATERIALIZED CTE so the GIN index does the
        work and filters apply afterwards (see _fenced_search for why). Ranking
        by ts_rank_cd has to score every matching row, so a very common term on
        a very large table can be slow; this leg runs in a SAVEPOINT with its
        own timeout and, if it trips, the caller gets timed_out=True and keeps
        the dense results instead of failing the whole request.
        """
        statement, params = self._sparse_statement(schema, table, key_columns, text_column, text_config,
                                                   stored_tsvector, query_text, limit, filters)
        import psycopg2
        cur.execute("SAVEPOINT data_api_sparse")
        self._set_statement_timeout(cur, timeout_ms)
        try:
            cur.execute(statement, params)
            rows = cur.fetchall()
        except psycopg2.errors.QueryCanceled:
            cur.execute("ROLLBACK TO SAVEPOINT data_api_sparse")
            return [], True
        cur.execute("RELEASE SAVEPOINT data_api_sparse")
        return [(tuple(r[:-1]), float(r[-1])) for r in rows], False

    def fetch_by_keys(self, cur, schema: str, table: str, key_columns: List[str],
                      columns: List[str], keys: List[tuple]) -> Dict[tuple, dict]:
        """Rows for known keys. Values are bound as untyped literals so uuid,
        numeric and text keys all compare without driver-side type adapters."""
        if not keys:
            return {}
        q = self._vector_identifier
        ref = self._ref(schema, table)
        select = list(dict.fromkeys(list(key_columns) + list(columns)))
        width = len(key_columns)
        tuple_sql = "(" + ", ".join(["%s"] * width) + ")"
        cur.execute(
            f"SELECT {', '.join(q(c) for c in select)} FROM {ref} "
            f"WHERE ({', '.join(q(c) for c in key_columns)}) IN ({', '.join([tuple_sql] * len(keys))})",
            [v for key in keys for v in key],
        )
        out = {}
        for r in cur.fetchall():
            row = dict(zip(select, r))
            out[tuple(row[c] for c in key_columns)] = row
        return out

    def _table_ref(self, database, schema, table) -> str:
        return self._ref(schema, table)

    def get_column_aggregates(self, database, schema, table, columns, numeric_columns):
        # Postgres routes per-database; make sure we are on the right one.
        self._ensure_conn(database)
        return super().get_column_aggregates(database, schema, table, columns, numeric_columns)

    def get_row_count(self, database: Optional[str], schema: str, table: str) -> int:
        self._ensure_conn(database)
        cur = self.conn.cursor()
        try:
            cur.execute(f'SELECT COUNT(*) FROM {self._ref(schema, table)}')
            return cur.fetchone()[0]
        finally:
            cur.close()

    def get_null_counts(self, database: Optional[str], schema: str, table: str, columns: List[str]) -> Dict[str, int]:
        if not columns:
            return {}
        self._ensure_conn(database)
        parts = [f'SUM(CASE WHEN "{c}" IS NULL THEN 1 ELSE 0 END)' for c in columns]
        cur = self.conn.cursor()
        try:
            cur.execute(f'SELECT {", ".join(parts)} FROM {self._ref(schema, table)}')
            row = cur.fetchone()
            return {c: int(row[i] or 0) for i, c in enumerate(columns)}
        finally:
            cur.close()

    def get_duplicate_count(self, database: Optional[str], schema: str, table: str, columns: List[str]) -> int:
        if not columns:
            return 0
        self._ensure_conn(database)
        col_list = ", ".join(f'"{c}"' for c in columns)
        cur = self.conn.cursor()
        try:
            cur.execute(f'''
                SELECT COUNT(*) FROM (
                    SELECT {col_list} FROM {self._ref(schema, table)}
                    GROUP BY {col_list} HAVING COUNT(*) > 1
                ) sub
            ''')
            return cur.fetchone()[0]
        finally:
            cur.close()

    def fetch_rows(self, database: Optional[str], schema: str, table: str,
                    columns: List[str], limit: int, order_by: Optional[List[str]] = None,
                    offset: int = 0, filters: Optional[List[tuple]] = None) -> List[dict]:
        self._ensure_conn(database)
        col_list = ", ".join(f'"{c}"' for c in columns)
        order_clause = self._order_sql(order_by)
        where = self._filters_sql(filters)
        where_clause = f"WHERE {where}" if where else ""
        cur = self.conn.cursor()
        try:
            cur.execute(
                f'SELECT {col_list} FROM {self._ref(schema, table)} {where_clause} {order_clause} '
                f'LIMIT {int(limit)} OFFSET {int(offset)}'
            )
            return [dict(zip(columns, r)) for r in cur.fetchall()]
        finally:
            cur.close()

    def filtered_row_count(self, database: Optional[str], schema: str, table: str,
                            filters: List[tuple]) -> int:
        self._ensure_conn(database)
        where = self._filters_sql(filters)
        cur = self.conn.cursor()
        try:
            cur.execute(f'SELECT COUNT(*) FROM {self._ref(schema, table)} '
                        f'{f"WHERE {where}" if where else ""}')
            return cur.fetchone()[0]
        finally:
            cur.close()

    def get_sort_key(self, database: Optional[str], schema: str, table: str) -> List[str]:
        self._ensure_conn(database)
        ref = self._table_ref(database, schema, table)
        cur = self.conn.cursor()
        try:
            cur.execute("""
                WITH chosen AS (
                    SELECT i.indexrelid, i.indrelid, i.indkey, i.indnkeyatts
                    FROM pg_catalog.pg_index i
                    JOIN pg_catalog.pg_class ix ON ix.oid = i.indexrelid
                    JOIN pg_catalog.pg_am am ON am.oid = ix.relam
                    WHERE i.indrelid = %s::regclass
                      AND i.indisunique AND i.indisvalid AND i.indisready
                      AND i.indpred IS NULL AND i.indexprs IS NULL
                      AND am.amname = 'btree'
                      AND NOT EXISTS (
                        SELECT 1 FROM unnest(i.indkey::smallint[]) WITH ORDINALITY AS k(attnum, ord)
                        LEFT JOIN pg_catalog.pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum
                        WHERE k.ord <= i.indnkeyatts AND (a.attnum IS NULL OR NOT a.attnotnull)
                      )
                    ORDER BY i.indisprimary DESC, i.indnkeyatts, i.indexrelid
                    LIMIT 1
                )
                SELECT a.attname FROM chosen i
                CROSS JOIN LATERAL unnest(i.indkey::smallint[]) WITH ORDINALITY AS k(attnum, ord)
                JOIN pg_catalog.pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum
                WHERE k.ord <= i.indnkeyatts ORDER BY k.ord
            """, (ref,))
            return [row[0] for row in cur.fetchall()]
        finally:
            cur.close()

    def estimate_row_count(self, database: Optional[str], schema: str, table: str) -> Optional[int]:
        self._ensure_conn(database)
        cur = self.conn.cursor()
        try:
            cur.execute(
                "SELECT reltuples::bigint FROM pg_class WHERE oid = %s::regclass",
                (self._ref(schema, table),),
            )
            row = cur.fetchone()
            # -1 is "never analyzed"; a fresh table reports 0 legitimately.
            if not row or row[0] is None or row[0] < 0:
                return None
            return int(row[0])
        except Exception:
            return None
        finally:
            cur.close()

    def fetch_rows_after(self, database: Optional[str], schema: str, table: str,
                          columns: List[str], key_columns: List[str],
                          after_values: Optional[list], limit: int,
                          filters: Optional[List[tuple]] = None) -> List[dict]:
        self._ensure_conn(database)
        col_list = ", ".join(f'"{c}"' for c in columns)
        key_list = ", ".join(f'"{c}"' for c in key_columns)
        clauses, params = [], []
        if after_values:
            # Row-value comparison, not a chain of ORs — Postgres can push this
            # straight into a single index scan on a composite key.
            placeholders = ", ".join(["%s"] * len(after_values))
            clauses.append(f"({key_list}) > ({placeholders})")
            params = list(after_values)
        filter_sql = self._filters_sql(filters)
        if filter_sql:
            clauses.append(filter_sql)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        cur = self.conn.cursor()
        try:
            cur.execute(
                f'SELECT {col_list} FROM {self._ref(schema, table)} '
                f'{where} ORDER BY {key_list} LIMIT {int(limit)}',
                params,
            )
            return [dict(zip(columns, r)) for r in cur.fetchall()]
        finally:
            cur.close()

    def _search_where(self, columns: List[str], search_term: str) -> str:
        term = self._lit(search_term)
        # text::text is a no-op the parser drops, so a trigram index on a text
        # column is still reachable here — but only when the term is the single
        # branch of the WHERE. OR'ing a dozen columns together makes the planner
        # give up on every index and scan; narrowing search_columns to one
        # indexed column (see the API's `column` param) is what keeps it fast.
        return " OR ".join(f'CAST("{c}" AS TEXT) ILIKE \'%{term}%\'' for c in columns)

    def _word_search_where(self, columns: List[str], search_term: str) -> str:
        """Whole-word matching against a GIN index on to_tsvector(config, col).

        The expression has to be written exactly as the index was declared or
        the planner won't match the two. websearch_to_tsquery is what turns a
        plain phrase into a query: bare words become AND, quotes make a phrase,
        and a leading minus excludes — close enough to what people expect from
        a search box, and it never raises on odd punctuation the way
        to_tsquery does."""
        cfg = _sql_literal(self.TEXT_SEARCH_CONFIG)
        term = self._lit(search_term)
        return " OR ".join(
            f"to_tsvector('{cfg}', \"{c}\") @@ websearch_to_tsquery('{cfg}', '{term}')"
            for c in columns
        )

    def _where_for(self, columns: List[str], search_term: str, mode: str) -> str:
        if mode == "words":
            return self._word_search_where(columns, search_term)
        return super()._where_for(columns, search_term, mode)

    def _fenced_search(self, schema: str, table: str, select_cols: str,
                        search_where: str, filter_where: str, tail: str,
                        inner_cols: Optional[str] = None) -> str:
        """Search inside a MATERIALIZED CTE, filters outside it.

        Handing the planner a text-index predicate and a cheap-looking filter
        as equals invites it to drop the index: it assumes the filter plus a
        LIMIT will fill quickly from a sequential scan, then re-evaluates the
        text match on every row it reads. Measured on a ten-million-row table,
        adding `chunk_level = 'split'` to a word search took it from 441 ms to
        40 seconds. MATERIALIZED is an optimisation fence — the planner cannot
        push the filter inside it, so the index does its job and the filter
        applies to what comes out. The CTE still stops early under the outer
        LIMIT, so this does not materialise every match."""
        ref = self._ref(schema, table)
        return (
            f"WITH matched AS MATERIALIZED (SELECT {inner_cols or select_cols} FROM {ref} WHERE {search_where}) "
            f"SELECT {select_cols} FROM matched WHERE {filter_where} {tail}"
        )

    def search_rows(self, database: Optional[str], schema: str, table: str,
                     columns: List[str], search_term: str, limit: int,
                     order_by: Optional[List[str]] = None, offset: int = 0,
                     search_columns: Optional[List[str]] = None,
                     mode: str = "substring",
                     filters: Optional[List[tuple]] = None) -> List[dict]:
        self._ensure_conn(database)
        if order_by:
            rows = self._search_bounded(database, schema, table, columns, search_term, limit,
                                        order_by, offset, search_columns, mode, filters)
            if rows is not None:
                return rows
        col_list = ", ".join(f'"{c}"' for c in columns)
        # No order_by means no ORDER BY clause, which is the whole point of
        # offering the choice: sorting a search by the primary key makes the
        # planner walk the pkey and re-evaluate the match on every row it
        # passes, ignoring the text index entirely.
        order_clause = self._order_sql(order_by)
        tail = f"{order_clause} LIMIT {int(limit)} OFFSET {int(offset)}"
        search_where = self._where_for(search_columns or columns, search_term, mode)
        filter_where = self._filters_sql(filters)

        if filter_where:
            # The outer query filters and sorts on columns the caller may not
            # have asked to see, so the CTE has to carry them too.
            inner = list(columns)
            for c in [f[0] for f in filters] + self._order_cols(order_by):
                if c not in inner:
                    inner.append(c)
            inner_list = ", ".join(self._quote_col(c) for c in inner)
            sql = self._fenced_search(schema, table, col_list, search_where, filter_where, tail,
                                      inner_cols=inner_list)
        else:
            sql = (f'SELECT {col_list} FROM {self._ref(schema, table)} '
                   f'WHERE {search_where} {tail}')
        cur = self.conn.cursor()
        try:
            cur.execute(sql)
            return [dict(zip(columns, r)) for r in cur.fetchall()]
        finally:
            cur.close()

    # How many matches the bounded search will collect before giving up and
    # handing over to the normal plan. See _search_bounded.
    BOUNDED_SEARCH_MIN = 1000
    BOUNDED_SEARCH_MAX = 10000

    def _search_bounded(self, database, schema, table, columns, search_term, limit,
                        order_by, offset, search_columns, mode, filters) -> Optional[List[dict]]:
        """Ordered search that doesn't depend on the planner guessing right.

        With ORDER BY key LIMIT n, Postgres assumes a search term matches ~0.2%
        of rows. Small n then looks cheapest as a walk down the primary key,
        checking every row until n matches turn up. For a rare term that walk
        reads the whole table: measured on 2M rows, 75 s at LIMIT 100 against
        8 ms at LIMIT 1000, same term, same index. Page size shouldn't decide
        whether a search takes milliseconds or times out.

        So: pull at most `cap` matches through the search index, inside a
        MATERIALIZED fence the planner can't reorder, and sort only those. If
        the cap wasn't reached, those are all the matches and the page is
        exact. If it was, the term is common, the key walk finds a page almost
        immediately, and this returns None so the caller runs the normal plan.

        cap ~ sqrt(table rows * rows needed): the probe costs about cap rows
        on a common term, and the key walk after it about rows * needed / cap,
        so this keeps both sides of the trade small."""
        want = int(offset) + int(limit)
        try:
            est = self.estimate_row_count(database, schema, table) or 0
        except Exception:
            est = 0
        cap = int(math.sqrt(est * want)) if est else self.BOUNDED_SEARCH_MIN
        cap = max(want, min(self.BOUNDED_SEARCH_MAX, max(self.BOUNDED_SEARCH_MIN, cap)))

        ref = self._ref(schema, table)
        inner = list(columns)
        for c in [f[0] for f in (filters or [])] + self._order_cols(order_by):
            if c not in inner:
                inner.append(c)
        inner_list = ", ".join(self._quote_col(c) for c in inner)
        col_list = ", ".join(self._quote_col(c) for c in columns)
        search_where = self._where_for(search_columns or columns, search_term, mode)
        filter_where = self._filters_sql(filters)
        page_where = f"WHERE {filter_where}" if filter_where else ""
        order_clause = self._order_sql(order_by)

        # One statement so the fenced set is built once. The LEFT JOIN keeps a
        # row coming back even for an empty page, so the match count is always
        # readable; the row number keeps the page in order through the join.
        sql = (
            f"WITH m AS MATERIALIZED (SELECT {inner_list} FROM {ref} WHERE {search_where} LIMIT {cap + 1}), "
            f"p AS (SELECT {col_list}, row_number() OVER ({order_clause}) AS __dapi_rn FROM m {page_where} "
            f"{order_clause} LIMIT {int(limit)} OFFSET {int(offset)}) "
            f"SELECT (SELECT count(*) FROM m) AS __dapi_n, p.* FROM (SELECT 1) AS one "
            f"LEFT JOIN p ON true ORDER BY p.__dapi_rn"
        )
        cur = self.conn.cursor()
        try:
            cur.execute(sql)
            fetched = cur.fetchall()
        finally:
            cur.close()
        matched = fetched[0][0] if fetched else 0
        if matched > cap:
            return None
        width = len(columns)
        return [dict(zip(columns, r[1:1 + width])) for r in fetched if r[-1] is not None]

    def search_row_count(self, database: Optional[str], schema: str, table: str,
                          columns: List[str], search_term: str,
                          mode: str = "substring",
                          filters: Optional[List[tuple]] = None) -> int:
        self._ensure_conn(database)
        ref = self._ref(schema, table)
        search_where = self._where_for(columns, search_term, mode)
        filter_where = self._filters_sql(filters)
        if filter_where:
            # Same fence as above. Counting has no LIMIT to stop early, so
            # getting the plan right matters more here, not less.
            filter_cols = list(dict.fromkeys(f[0] for f in filters))
            sql = (f"WITH matched AS MATERIALIZED (SELECT 1 AS one, {', '.join(self._quote_col(c) for c in filter_cols)} "
                   f"FROM {ref} WHERE {search_where}) "
                   f"SELECT COUNT(*) FROM matched WHERE {filter_where}")
        else:
            sql = f"SELECT COUNT(*) FROM {ref} WHERE {search_where}"
        cur = self.conn.cursor()
        try:
            cur.execute(sql)
            return cur.fetchone()[0]
        finally:
            cur.close()


# ─────────────────────────────────────────────────────────────────────────────
# DocuBridge — just another PostgreSQL instance, reached through an NLB DNS
# endpoint (confirmed connection details:
# jdbc:postgresql://<nlb-endpoint>:5432/lnzaws1226), so this reuses all of
# PostgreSQLConnector's logic verbatim and only swaps in its own config
# keys/env vars, so DocuBridge's host/database/credentials are configured
# separately from the generic [postgresql] source under its own
# [docubridge] section in data_validation_config.ini. maintenance_database
# defaults to "lnzaws1226" itself (rather than "postgres") since that's the
# one database this NLB endpoint is known to expose.
# ─────────────────────────────────────────────────────────────────────────────
class DocuBridgeConnector(PostgreSQLConnector):
    display_name = "DocuBridge"

    def __init__(self, cfg: dict):
        self.host = _cfg(cfg, "host", "IT_CHECKS_DOCUBRIDGE_HOST")
        self.port = int(cfg.get("port") or os.environ.get("IT_CHECKS_DOCUBRIDGE_PORT") or 5432)
        self.user = _cfg(cfg, "user", "IT_CHECKS_DOCUBRIDGE_USER")
        self.password = _cfg(cfg, "password", "IT_CHECKS_DOCUBRIDGE_PASSWORD")
        self.maintenance_database = cfg.get("maintenance_database") or "lnzaws1226"
        self.conn = None
        self._current_db = None
        self._session_limits = None
        self._connection_options = dict(cfg)


# ─────────────────────────────────────────────────────────────────────────────
# Denodo (JDBC via jaydebeapi — requires a JRE on PATH and the Denodo VDP driver
# jar). Denodo has "databases" (VDP databases) but no separate schema level;
# tables/views are addressed as "database.view".
# ─────────────────────────────────────────────────────────────────────────────
class DenodoConnector(BaseConnector):
    display_name = "Denodo"
    LEVELS = ["database"]

    def __init__(self, cfg: dict):
        self.driver_jar = _cfg(cfg, "driver_jar", "IT_CHECKS_DENODO_DRIVER_JAR")
        self.driver_class = cfg.get("driver_class") or "com.denodo.vdp.jdbc.Driver"
        self.jdbc_url = _cfg(cfg, "jdbc_url", "IT_CHECKS_DENODO_URL")
        self.user = _cfg(cfg, "user", "IT_CHECKS_DENODO_USER")
        self.password = _cfg(cfg, "password", "IT_CHECKS_DENODO_PASSWORD")
        # Optional static fallback list, used only if JDBC catalog introspection fails.
        # In data_validation_config.ini this is a comma-separated string; accept a list too
        # in case this connector is ever driven from a JSON config instead.
        known_tables_raw = cfg.get("known_tables") or ""
        if isinstance(known_tables_raw, str):
            self.known_tables = [t.strip() for t in known_tables_raw.split(",") if t.strip()]
        else:
            self.known_tables = list(known_tables_raw)
        self.conn = None

    def connect(self):
        try:
            import jaydebeapi
        except ImportError as e:
            raise ConnectorError(
                "Denodo support requires 'jaydebeapi' and 'JPype1' (pip install jaydebeapi JPype1), "
                "plus a JRE on PATH."
            ) from e

        if not os.path.exists(self.driver_jar):
            raise ConnectorError(f"Denodo JDBC driver jar not found at '{self.driver_jar}'")

        _set_jdbc_login_timeout(10)
        self.conn = jaydebeapi.connect(
            self.driver_class, self.jdbc_url, [self.user, self.password], self.driver_jar
        )

    def disconnect(self):
        if self.conn:
            try:
                self.conn.close()
            except Exception:
                pass

    def _qualify(self, database: Optional[str], table: str) -> str:
        return f'"{database}"."{table}"' if database else f'"{table}"'

    def list_databases(self) -> List[str]:
        try:
            dbmd = self.conn.jconn.getMetaData()
            rs = dbmd.getCatalogs()
            dbs = []
            while rs.next():
                val = rs.getString("TABLE_CAT")
                if val:
                    dbs.append(str(val))
            return sorted(set(dbs))
        except Exception as e:
            raise ConnectorError(f"Could not list Denodo databases ({e}).")

    def list_tables(self, database: Optional[str], schema: Optional[str] = None) -> List[str]:
        try:
            dbmd = self.conn.jconn.getMetaData()
            rs = dbmd.getTables(database, None, None, ["TABLE", "VIEW"])
            tables = []
            while rs.next():
                val = rs.getString("TABLE_NAME")
                if val:
                    tables.append(str(val))
            return sorted(set(tables))
        except Exception as e:
            if self.known_tables:
                logger.warning(f"Denodo catalog introspection failed ({e}); using configured known_tables list")
                return sorted(self.known_tables)
            raise ConnectorError(
                f"Could not list Denodo views ({e}). Add a 'known_tables' fallback list to the "
                "[denodo] section of data_validation_config.ini."
            )

    def get_columns(self, database: Optional[str], schema: Optional[str], table: str) -> List[Dict[str, str]]:
        ref = self._qualify(database, table)
        cur = self.conn.cursor()
        try:
            cur.execute(f"SELECT * FROM {ref} WHERE 1 = 0")
            return [{"name": str(d[0]), "type": str(d[1])} for d in cur.description]
        finally:
            cur.close()

    def _table_ref(self, database, schema, table) -> str:
        return self._qualify(database, table)

    def get_row_count(self, database: Optional[str], schema: Optional[str], table: str) -> int:
        ref = self._qualify(database, table)
        cur = self.conn.cursor()
        try:
            cur.execute(f"SELECT COUNT(*) FROM {ref}")
            return cur.fetchone()[0]
        finally:
            cur.close()

    def get_null_counts(self, database: Optional[str], schema: Optional[str], table: str, columns: List[str]) -> Dict[str, int]:
        if not columns:
            return {}
        ref = self._qualify(database, table)
        parts = [f'SUM(CASE WHEN "{c}" IS NULL THEN 1 ELSE 0 END)' for c in columns]
        cur = self.conn.cursor()
        try:
            cur.execute(f'SELECT {", ".join(parts)} FROM {ref}')
            row = cur.fetchone()
            return {c: int(row[i] or 0) for i, c in enumerate(columns)}
        finally:
            cur.close()

    def get_duplicate_count(self, database: Optional[str], schema: Optional[str], table: str, columns: List[str]) -> int:
        if not columns:
            return 0
        ref = self._qualify(database, table)
        col_list = ", ".join(f'"{c}"' for c in columns)
        cur = self.conn.cursor()
        try:
            cur.execute(f'''
                SELECT COUNT(*) FROM (
                    SELECT {col_list} FROM {ref} GROUP BY {col_list} HAVING COUNT(*) > 1
                ) sub
            ''')
            return cur.fetchone()[0]
        finally:
            cur.close()

    def fetch_rows(self, database: Optional[str], schema: Optional[str], table: str,
                    columns: List[str], limit: int, order_by: Optional[List[str]] = None,
                    offset: int = 0, filters: Optional[List[tuple]] = None) -> List[dict]:
        ref = self._qualify(database, table)
        col_list = ", ".join(f'"{c}"' for c in columns)
        where = self._filters_sql(filters)
        where_clause = f"WHERE {where}" if where else ""
        cur = self.conn.cursor()
        try:
            cur.execute(f"SELECT {col_list} FROM {ref} {where_clause} {self._order_sql(order_by)} "
                        f"LIMIT {int(limit)} OFFSET {int(offset)}")
            return [dict(zip(columns, r)) for r in cur.fetchall()]
        finally:
            cur.close()

    def filtered_row_count(self, database: Optional[str], schema: Optional[str], table: str,
                            filters: List[tuple]) -> int:
        ref = self._qualify(database, table)
        where = self._filters_sql(filters)
        cur = self.conn.cursor()
        try:
            cur.execute(f"SELECT COUNT(*) FROM {ref} {f'WHERE {where}' if where else ''}")
            return cur.fetchone()[0]
        finally:
            cur.close()

    def _as_text(self, col_sql: str) -> str:
        return f"CAST({col_sql} AS VARCHAR)"

    def _search_where(self, columns: List[str], search_term: str) -> str:
        term = self._lit(search_term).upper()
        return " OR ".join(f'UPPER(CAST("{c}" AS VARCHAR)) LIKE \'%{term}%\'' for c in columns)

    def search_rows(self, database: Optional[str], schema: Optional[str], table: str,
                     columns: List[str], search_term: str, limit: int,
                     order_by: Optional[List[str]] = None, offset: int = 0,
                     search_columns: Optional[List[str]] = None,
                     mode: str = "substring",
                     filters: Optional[List[tuple]] = None) -> List[dict]:
        ref = self._qualify(database, table)
        col_list = ", ".join(f'"{c}"' for c in columns)
        where = self._and_where(self._where_for(search_columns or columns, search_term, mode),
                                self._filters_sql(filters))
        cur = self.conn.cursor()
        try:
            cur.execute(f"SELECT {col_list} FROM {ref} WHERE {where} {self._order_sql(order_by)} "
                        f"LIMIT {int(limit)} OFFSET {int(offset)}")
            return [dict(zip(columns, r)) for r in cur.fetchall()]
        finally:
            cur.close()

    def search_row_count(self, database: Optional[str], schema: Optional[str], table: str,
                          columns: List[str], search_term: str,
                          mode: str = "substring",
                          filters: Optional[List[tuple]] = None) -> int:
        ref = self._qualify(database, table)
        where = self._and_where(self._where_for(columns, search_term, mode), self._filters_sql(filters))
        cur = self.conn.cursor()
        try:
            cur.execute(f"SELECT COUNT(*) FROM {ref} WHERE {where}")
            return cur.fetchone()[0]
        finally:
            cur.close()


# ─────────────────────────────────────────────────────────────────────────────
# SQL Server (JDBC — same jaydebeapi/JPype1 dependency as Denodo)
#
# Uses SQL Server authentication (explicit username/password) only — this
# works identically whether the backend runs on Windows or Linux, unlike
# NTLM/integrated security which depends on native Windows libraries or a
# Kerberos ticket being available to the JVM. Your jdbc_url should NOT include
# integratedSecurity=true/authenticationScheme=NTLM; user/password below are
# passed to the driver directly instead.
#
# The database is normally baked into the JDBC URL itself (databaseName=...),
# but SQL Server supports cross-database queries within one instance/connection
# via three-part naming ([database].[schema].[table]), so — unlike
# PostgreSQL — we don't need to reconnect to browse other databases on the
# same server; whichever database is in the URL is just the initial one.
# ─────────────────────────────────────────────────────────────────────────────
class SQLServerConnector(BaseConnector):
    display_name = "SQL Server"
    LEVELS = ["database", "schema"]
    # These raise "cannot be compared or sorted, except when using the IS
    # NULL operator" if used in ORDER BY/GROUP BY/comparisons. TEXT/NTEXT/IMAGE
    # are deprecated but still show up in older schemas.
    NON_SORTABLE_TYPES = {"XML", "TEXT", "NTEXT", "IMAGE"}

    def __init__(self, cfg: dict):
        self.driver_jar = _cfg(cfg, "driver_jar", "IT_CHECKS_SQLSERVER_DRIVER_JAR")
        self.driver_class = cfg.get("driver_class") or "com.microsoft.sqlserver.jdbc.SQLServerDriver"
        self.jdbc_url = _cfg(cfg, "jdbc_url", "IT_CHECKS_SQLSERVER_URL")
        self.user = _cfg(cfg, "user", "IT_CHECKS_SQLSERVER_USER")
        self.password = _cfg(cfg, "password", "IT_CHECKS_SQLSERVER_PASSWORD")
        self.conn = None

    def connect(self):
        try:
            import jaydebeapi
        except ImportError as e:
            raise ConnectorError(
                "SQL Server support requires 'jaydebeapi' and 'JPype1' (pip install jaydebeapi JPype1), "
                "plus a JRE on PATH."
            ) from e

        if not os.path.exists(self.driver_jar):
            raise ConnectorError(f"SQL Server JDBC driver jar not found at '{self.driver_jar}'")

        _set_jdbc_login_timeout(10)
        self.conn = jaydebeapi.connect(
            self.driver_class, self.jdbc_url, [self.user, self.password], self.driver_jar
        )

    def disconnect(self):
        if self.conn:
            try:
                self.conn.close()
            except Exception:
                pass

    def list_databases(self) -> List[str]:
        # database_id > 4 excludes the system databases (master, tempdb, model, msdb).
        # sys.databases.name is `sysname` (an NVARCHAR alias) — the Microsoft JDBC
        # driver reports NVARCHAR columns with a type code jaydebeapi doesn't
        # convert by default, so without str(...) here `r[0]` can come back as a
        # raw Java string proxy that silently serializes to `{}` over the API.
        cur = self.conn.cursor()
        try:
            # HAS_DBACCESS: only databases this login can open (offline or no user mapping = 0)
            cur.execute("SELECT name FROM sys.databases WHERE database_id > 4 AND HAS_DBACCESS(name) = 1 ORDER BY name")
            return [str(r[0]) for r in cur.fetchall()]
        finally:
            cur.close()

    def list_schemas(self, database: Optional[str]) -> List[str]:
        if not database:
            raise ConnectorError("A database must be selected to list SQL Server schemas.")
        cur = self.conn.cursor()
        try:
            # only schemas holding at least one table or view this login can SELECT from
            cur.execute(
                f"SELECT DISTINCT TABLE_SCHEMA FROM [{database}].INFORMATION_SCHEMA.TABLES "
                f"WHERE HAS_PERMS_BY_NAME(QUOTENAME('{database}') + '.' + QUOTENAME(TABLE_SCHEMA) + '.' + QUOTENAME(TABLE_NAME), "
                f"'OBJECT', 'SELECT') = 1 ORDER BY TABLE_SCHEMA"
            )
            return [str(r[0]) for r in cur.fetchall()]
        finally:
            cur.close()

    def list_tables(self, database: Optional[str], schema: Optional[str]) -> List[str]:
        if not database or not schema:
            raise ConnectorError("A database and schema must be selected to list SQL Server tables.")
        cur = self.conn.cursor()
        try:
            cur.execute(
                f"SELECT TABLE_NAME FROM [{database}].INFORMATION_SCHEMA.TABLES "
                f"WHERE TABLE_SCHEMA = ? AND HAS_PERMS_BY_NAME(QUOTENAME('{database}') + '.' + QUOTENAME(TABLE_SCHEMA) + '.' "
                f"+ QUOTENAME(TABLE_NAME), 'OBJECT', 'SELECT') = 1 ORDER BY TABLE_NAME",
                [schema],
            )
            return [str(r[0]) for r in cur.fetchall()]
        finally:
            cur.close()

    def get_columns(self, database: str, schema: str, table: str) -> List[Dict[str, str]]:
        cur = self.conn.cursor()
        try:
            cur.execute(
                f"SELECT COLUMN_NAME, DATA_TYPE FROM [{database}].INFORMATION_SCHEMA.COLUMNS "
                f"WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ? ORDER BY ORDINAL_POSITION",
                [schema, table],
            )
            return [{"name": str(r[0]), "type": str(r[1])} for r in cur.fetchall()]
        finally:
            cur.close()

    def _table_ref(self, database, schema, table) -> str:
        return f"[{database}].[{schema}].[{table}]"

    def _quote_col(self, column: str) -> str:
        return f"[{column}]"

    def get_row_count(self, database: str, schema: str, table: str) -> int:
        cur = self.conn.cursor()
        try:
            cur.execute(f"SELECT COUNT(*) FROM [{database}].[{schema}].[{table}]")
            return cur.fetchone()[0]
        finally:
            cur.close()

    def get_null_counts(self, database: str, schema: str, table: str, columns: List[str]) -> Dict[str, int]:
        if not columns:
            return {}
        parts = [f"SUM(CASE WHEN [{c}] IS NULL THEN 1 ELSE 0 END)" for c in columns]
        cur = self.conn.cursor()
        try:
            cur.execute(f'SELECT {", ".join(parts)} FROM [{database}].[{schema}].[{table}]')
            row = cur.fetchone()
            return {c: int(row[i] or 0) for i, c in enumerate(columns)}
        finally:
            cur.close()

    def get_duplicate_count(self, database: str, schema: str, table: str, columns: List[str]) -> int:
        if not columns:
            return 0
        col_list = ", ".join(f"[{c}]" for c in columns)
        cur = self.conn.cursor()
        try:
            cur.execute(f'''
                SELECT COUNT(*) FROM (
                    SELECT {col_list} FROM [{database}].[{schema}].[{table}]
                    GROUP BY {col_list} HAVING COUNT(*) > 1
                ) sub
            ''')
            return cur.fetchone()[0]
        finally:
            cur.close()

    def fetch_rows(self, database: str, schema: str, table: str,
                    columns: List[str], limit: int, order_by: Optional[List[str]] = None,
                    offset: int = 0, filters: Optional[List[tuple]] = None) -> List[dict]:
        where = self._filters_sql(filters)
        return self._select_page(database, schema, table, columns, where, order_by, limit, offset)

    def _select_page(self, database, schema, table, columns, where, order_by, limit, offset) -> List[dict]:
        col_list = ", ".join(f"[{c}]" for c in columns)
        ref = f"[{database}].[{schema}].[{table}]"
        where_clause = f"WHERE {where}" if where else ""
        cur = self.conn.cursor()
        try:
            if offset:
                # OFFSET...FETCH NEXT requires an ORDER BY — fall back to
                # ordering by every selected column so paging still works
                # (and stays stable across pages) even without an explicit key.
                order_clause = self._order_sql(order_by or columns)
                cur.execute(
                    f"SELECT {col_list} FROM {ref} {where_clause} {order_clause} "
                    f"OFFSET {int(offset)} ROWS FETCH NEXT {int(limit)} ROWS ONLY"
                )
            else:
                cur.execute(
                    f"SELECT TOP {int(limit)} {col_list} FROM {ref} {where_clause} {self._order_sql(order_by)}"
                )
            return [dict(zip(columns, r)) for r in cur.fetchall()]
        finally:
            cur.close()

    def filtered_row_count(self, database: str, schema: str, table: str,
                            filters: List[tuple]) -> int:
        where = self._filters_sql(filters)
        cur = self.conn.cursor()
        try:
            cur.execute(f"SELECT COUNT(*) FROM [{database}].[{schema}].[{table}] "
                        f"{f'WHERE {where}' if where else ''}")
            return cur.fetchone()[0]
        finally:
            cur.close()

    def _as_text(self, col_sql: str) -> str:
        return f"CAST({col_sql} AS NVARCHAR(MAX))"

    def _search_where(self, columns: List[str], search_term: str) -> str:
        term = self._lit(search_term).upper()
        return " OR ".join(f"UPPER(CAST([{c}] AS NVARCHAR(MAX))) LIKE '%{term}%'" for c in columns)

    def search_rows(self, database: str, schema: str, table: str,
                     columns: List[str], search_term: str, limit: int,
                     order_by: Optional[List[str]] = None, offset: int = 0,
                     search_columns: Optional[List[str]] = None,
                     mode: str = "substring",
                     filters: Optional[List[tuple]] = None) -> List[dict]:
        where = self._and_where(self._where_for(search_columns or columns, search_term, mode),
                                self._filters_sql(filters))
        return self._select_page(database, schema, table, columns, where, order_by, limit, offset)

    def search_row_count(self, database: str, schema: str, table: str,
                          columns: List[str], search_term: str,
                          mode: str = "substring",
                          filters: Optional[List[tuple]] = None) -> int:
        where = self._and_where(self._where_for(columns, search_term, mode), self._filters_sql(filters))
        cur = self.conn.cursor()
        try:
            cur.execute(f"SELECT COUNT(*) FROM [{database}].[{schema}].[{table}] WHERE {where}")
            return cur.fetchone()[0]
        finally:
            cur.close()


# ─────────────────────────────────────────────────────────────────────────────
class GrailConnector(SQLServerConnector):
    display_name = "Grail"

    def __init__(self, cfg: dict):
        self.driver_jar = _cfg(cfg, "driver_jar", "IT_CHECKS_GRAIL_DRIVER_JAR")
        self.driver_class = cfg.get("driver_class") or "com.microsoft.sqlserver.jdbc.SQLServerDriver"
        self.jdbc_url = _cfg(cfg, "jdbc_url", "IT_CHECKS_GRAIL_URL")
        self.user = _cfg(cfg, "user", "IT_CHECKS_GRAIL_USER")
        self.password = _cfg(cfg, "password", "IT_CHECKS_GRAIL_PASSWORD")
        self.conn = None


# ─────────────────────────────────────────────────────────────────────────────
# Databricks / Spark SQL (JDBC — same jaydebeapi/JPype1 dependency as Denodo)
#
# Auth: Databricks' JDBC driver (AuthMech=3 in the URL) expects username
# "token" and the personal access token as the password — configure the PAT
# under `token` below, not `password`.
#
# "database" here means Unity Catalog catalog (SHOW CATALOGS). On older
# workspaces without Unity Catalog this just returns a single catalog
# (typically hive_metastore or spark_catalog) — harmless, just one option
# to pick instead of several.
# ─────────────────────────────────────────────────────────────────────────────
class DatabricksConnector(BaseConnector):
    display_name = "Databricks"
    LEVELS = ["database", "schema"]
    # Spark SQL honours backslash escape sequences inside a string literal.
    BACKSLASH_ESCAPES = True

    def __init__(self, cfg: dict):
        self.driver_jar = _cfg(cfg, "driver_jar", "IT_CHECKS_DATABRICKS_DRIVER_JAR")
        # Two driver variants exist with different URL prefixes and class names:
        #   jdbc:spark://...      -> older Simba Spark driver -> com.simba.spark.jdbc.Driver (default below)
        #   jdbc:databricks://... -> newer rebranded driver    -> com.databricks.client.jdbc.Driver
        # Check which URL prefix your jdbc_url actually uses and override driver_class if needed.
        self.driver_class = cfg.get("driver_class") or "com.simba.spark.jdbc.Driver"
        self.jdbc_url = _cfg(cfg, "jdbc_url", "IT_CHECKS_DATABRICKS_URL")
        self.token = _cfg(cfg, "token", "IT_CHECKS_DATABRICKS_TOKEN")
        self.conn = None

    def connect(self):
        try:
            import jaydebeapi
        except ImportError as e:
            raise ConnectorError(
                "Databricks support requires 'jaydebeapi' and 'JPype1' (pip install jaydebeapi JPype1), "
                "plus a JRE on PATH."
            ) from e

        if not os.path.exists(self.driver_jar):
            raise ConnectorError(f"Databricks JDBC driver jar not found at '{self.driver_jar}'")

        _set_jdbc_login_timeout(10)
        self.conn = jaydebeapi.connect(self.driver_class, self.jdbc_url, ["token", self.token], self.driver_jar)

    def disconnect(self):
        if self.conn:
            try:
                self.conn.close()
            except Exception:
                pass

    def _rows(self, sql: str) -> List[tuple]:
        cur = self.conn.cursor()
        try:
            cur.execute(sql)
            return cur.fetchall()
        finally:
            cur.close()

    def list_databases(self) -> List[str]:
        return sorted(r[0] for r in self._rows("SHOW CATALOGS") if r[0])

    def list_schemas(self, database: Optional[str]) -> List[str]:
        if not database:
            raise ConnectorError("A database (catalog) must be selected to list Databricks schemas.")
        return sorted(r[0] for r in self._rows(f"SHOW SCHEMAS IN `{database}`") if r[0])

    def list_tables(self, database: Optional[str], schema: Optional[str]) -> List[str]:
        if not database or not schema:
            raise ConnectorError("A database (catalog) and schema must be selected to list Databricks tables.")
        return sorted(r[1] if len(r) > 1 else r[0] for r in self._rows(f"SHOW TABLES IN `{database}`.`{schema}`"))

    def get_columns(self, database: str, schema: str, table: str) -> List[Dict[str, str]]:
        rows = self._rows(f"DESCRIBE TABLE `{database}`.`{schema}`.`{table}`")
        columns = []
        for r in rows:
            name = str(r[0] or "").strip()
            if not name or name.startswith("#"):
                # Spark's DESCRIBE appends a blank line + partition-info
                # section after the real columns — stop there.
                break
            columns.append({"name": name, "type": str(r[1])})
        return columns

    def _table_ref(self, database, schema, table) -> str:
        return f"`{database}`.`{schema}`.`{table}`"

    def _quote_col(self, column: str) -> str:
        return f"`{column}`"

    def _run_sql(self, sql: str):
        return self._rows(sql)

    def get_row_count(self, database: str, schema: str, table: str) -> int:
        return self._rows(f"SELECT COUNT(*) FROM `{database}`.`{schema}`.`{table}`")[0][0]

    def get_null_counts(self, database: str, schema: str, table: str, columns: List[str]) -> Dict[str, int]:
        if not columns:
            return {}
        parts = [f"SUM(CASE WHEN `{c}` IS NULL THEN 1 ELSE 0 END)" for c in columns]
        row = self._rows(f'SELECT {", ".join(parts)} FROM `{database}`.`{schema}`.`{table}`')[0]
        return {c: int(row[i] or 0) for i, c in enumerate(columns)}

    def get_duplicate_count(self, database: str, schema: str, table: str, columns: List[str]) -> int:
        if not columns:
            return 0
        col_list = ", ".join(f"`{c}`" for c in columns)
        return self._rows(f'''
            SELECT COUNT(*) FROM (
                SELECT {col_list} FROM `{database}`.`{schema}`.`{table}`
                GROUP BY {col_list} HAVING COUNT(*) > 1
            ) sub
        ''')[0][0]

    def fetch_rows(self, database: str, schema: str, table: str,
                    columns: List[str], limit: int, order_by: Optional[List[str]] = None,
                    offset: int = 0, filters: Optional[List[tuple]] = None) -> List[dict]:
        col_list = ", ".join(f"`{c}`" for c in columns)
        where = self._filters_sql(filters)
        where_clause = f"WHERE {where}" if where else ""
        rows = self._rows(
            f'SELECT {col_list} FROM `{database}`.`{schema}`.`{table}` {where_clause} {self._order_sql(order_by)} '
            f'LIMIT {int(limit)} OFFSET {int(offset)}'
        )
        return [dict(zip(columns, r)) for r in rows]

    def filtered_row_count(self, database: str, schema: str, table: str,
                            filters: List[tuple]) -> int:
        where = self._filters_sql(filters)
        return self._rows(f"SELECT COUNT(*) FROM `{database}`.`{schema}`.`{table}` "
                          f"{f'WHERE {where}' if where else ''}")[0][0]

    def _as_text(self, col_sql: str) -> str:
        return f"CAST({col_sql} AS STRING)"

    def _search_where(self, columns: List[str], search_term: str) -> str:
        term = self._lit(search_term).lower()
        return " OR ".join(f"LOWER(CAST(`{c}` AS STRING)) LIKE '%{term}%'" for c in columns)

    def search_rows(self, database: str, schema: str, table: str,
                     columns: List[str], search_term: str, limit: int,
                     order_by: Optional[List[str]] = None, offset: int = 0,
                     search_columns: Optional[List[str]] = None,
                     mode: str = "substring",
                     filters: Optional[List[tuple]] = None) -> List[dict]:
        col_list = ", ".join(f"`{c}`" for c in columns)
        where = self._and_where(self._where_for(search_columns or columns, search_term, mode),
                                self._filters_sql(filters))
        rows = self._rows(
            f'SELECT {col_list} FROM `{database}`.`{schema}`.`{table}` WHERE {where} {self._order_sql(order_by)} '
            f'LIMIT {int(limit)} OFFSET {int(offset)}'
        )
        return [dict(zip(columns, r)) for r in rows]

    def search_row_count(self, database: str, schema: str, table: str,
                          columns: List[str], search_term: str,
                          mode: str = "substring",
                          filters: Optional[List[tuple]] = None) -> int:
        where = self._and_where(self._where_for(columns, search_term, mode), self._filters_sql(filters))
        return self._rows(f'SELECT COUNT(*) FROM `{database}`.`{schema}`.`{table}` WHERE {where}')[0][0]


# ─────────────────────────────────────────────────────────────────────────────
# Factory
# ─────────────────────────────────────────────────────────────────────────────
CONNECTOR_REGISTRY = {
    "insight7": OracleConnector,
    "rimdocs": OracleConnector,
    "snowflake": SnowflakeConnector,
    "postgresql": PostgreSQLConnector,
    "docubridge": DocuBridgeConnector,
    "denodo": DenodoConnector,
    "sqlserver": SQLServerConnector,
    "grail": GrailConnector,
    "databricks": DatabricksConnector,
}

JDBC_CONNECTOR_JAR_SOURCES = {
    "denodo": ("driver_jar", "IT_CHECKS_DENODO_DRIVER_JAR"),
    "sqlserver": ("driver_jar", "IT_CHECKS_SQLSERVER_DRIVER_JAR"),
    "grail": ("driver_jar", "IT_CHECKS_GRAIL_DRIVER_JAR"),
    "databricks": ("driver_jar", "IT_CHECKS_DATABRICKS_DRIVER_JAR"),
}


def create_connector(source_system: str, cfg: dict) -> BaseConnector:
    key = (source_system or "").lower()
    cls = CONNECTOR_REGISTRY.get(key)
    if not cls:
        raise ConnectorError(
            f"Unknown source system '{source_system}'. Supported: {', '.join(CONNECTOR_REGISTRY)}"
        )
    return cls(cfg)