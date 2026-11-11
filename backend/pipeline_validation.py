"""Centralised StreamSets ingestion validator: pick a pipeline -> extract it -> validate source vs Snowflake ->
show the result, and optionally append the report to Snowflake so the IT Checks page shows it.

Sources: Oracle and MariaDB/MySQL (detected from the pipeline's JDBC URL). Target: Snowflake.
Reads only, on source and target. The one write is opt-in ("Write to Snowflake"): CREATE TABLE IF NOT EXISTS
+ a single INSERT into {snowflake_save.database}.{core_schema}.DQ_IT_VALIDATION_REPORT - no updates, no deletes.

This one file is both:
  - the FastAPI router (imported by app.py)
  - the validation runner (the router starts it as `python pipeline_validation.py --script ... --output ...`)
The runner part sits at the top with stdlib imports only and exits before the web imports,
so the validation Python (scripts.python_path) doesn't need fastapi installed.
"""
import argparse
import importlib.util
import json
import os
import re
import sys
import time
import traceback
from collections import Counter
from datetime import datetime
from decimal import Decimal


# ═══════════════════════════════════════════════════════════════════════════════
# MariaDB / MySQL source support
# The validation script talks to its source only through OracleConnector + conn.cursor().
# For MariaDB pipelines the runner swaps in a connector with the same methods; the script's
# SQL runs as is because the session uses ANSI_QUOTES ("SCHEMA"."TABLE" works) and the
# cursor adds the derived-table aliases MariaDB insists on.
# ═══════════════════════════════════════════════════════════════════════════════
_SQL_KEYWORDS = {"where", "group", "order", "having", "limit", "union", "join", "left", "right",
                 "inner", "outer", "cross", "on", "for", "window", "except", "intersect"}


def _alias_derived(sql: str) -> str:
    """'FROM (subquery)' with no alias -> 'FROM (subquery) AS _dN' (Oracle allows it, MariaDB doesn't)."""
    spots = []
    for m in re.finditer(r"\bFROM\s*\(", sql, re.I):
        depth, i = 0, m.end() - 1
        while i < len(sql):
            depth += {"(": 1, ")": -1}.get(sql[i], 0)
            if depth == 0:
                break
            i += 1
        if depth:
            continue   # unbalanced - leave it alone
        nxt = re.match(r"\s*(\w+)", sql[i + 1:])
        if not nxt or nxt.group(1).lower() in _SQL_KEYWORDS:
            spots.append(i + 1)
    for n, at in enumerate(sorted(spots, reverse=True)):   # back to front keeps earlier offsets valid
        sql = f"{sql[:at]} AS _d{n}{sql[at:]}"
    return sql


def _num(x):
    # MariaDB SUM() comes back as Decimal; the script compares and JSON-dumps plain ints
    return int(x) if isinstance(x, Decimal) and x == x.to_integral_value() else x


class _MariaCursor:
    def __init__(self, cur):
        self._c = cur

    def execute(self, sql, params=None):
        return self._c.execute(_alias_derived(sql), params)

    def fetchone(self):
        row = self._c.fetchone()
        return tuple(_num(x) for x in row) if row else row

    def fetchall(self):
        return [tuple(_num(x) for x in row) for row in self._c.fetchall()]

    @property
    def description(self):
        return self._c.description

    def close(self):
        self._c.close()


class _MariaConn:
    def __init__(self, conn):
        self._conn = conn

    def cursor(self):
        return _MariaCursor(self._conn.cursor())

    def close(self):
        self._conn.close()


def _source_of(pipeline: dict):
    """(kind, jdbc url, username) from the extracted source stage. kind is 'oracle' or 'mariadb' when
    supported, otherwise a name for the error message ('sqlserver', 'Amazon S3', ...)."""
    sources = (pipeline.get("pipeline_flow") or {}).get("sources") or []
    for s in sources:
        cfg = s.get("configuration") or {}
        url = next((v for v in cfg.values() if isinstance(v, str) and v.lower().startswith("jdbc:")), "")
        if url:
            driver = url.split(":")[1].lower()   # jdbc:<driver>:...
            kind = "mariadb" if driver in ("mariadb", "mysql") else driver
            return kind, url, cfg.get("Username")
    if sources and not any(s.get("technology") == "JDBC/Database" for s in sources):
        return sources[0].get("technology") or "unknown", "", None   # files, S3, APIs ...
    return "oracle", "", None   # JDBC source with no URL in the report: keep the original Oracle behaviour


def _mariadb_connector(v):
    """Build the MariaDB connector on top of the loaded script's OracleConnector (keeps its retry/reconnect)."""

    class MariaDBConnector(v.OracleConnector):
        use_ssl = False   # set per pipeline from the JDBC URL (useSSL=true) or config 'ssl'
        def __init__(self, user, password, dsn, schema):
            super().__init__(user, password, dsn, schema)
            host, _, port = dsn.rpartition(":")
            self.host, self.port = host, int(port or 3306)

        def connect(self):
            try:
                import pymysql
            except ImportError:
                v.print_safe("❌ MariaDB source needs PyMySQL: pip install pymysql (in the validation Python)")
                return False
            try:
                raw = pymysql.connect(host=self.host, port=self.port, user=self.user, password=self.password,
                                      database=self.schema, charset="utf8mb4", autocommit=True, connect_timeout=30,
                                      init_command="SET SESSION sql_mode = CONCAT(@@sql_mode, ',ANSI_QUOTES')",
                                      # ssl={} = encrypted, server cert not checked (JDBC trustServerCertificate=true)
                                      ssl={} if self.use_ssl else None)
                with raw.cursor() as c:
                    c.execute("SET SESSION TRANSACTION READ ONLY")   # validation never writes to the source
                self.conn = _MariaConn(raw)
                self.last_activity = time.time()
                return True
            except Exception as e:
                v.log_detailed_error("MariaDB Connection Failed", e,
                                     {"User": self.user, "Host": f"{self.host}:{self.port}", "Database": self.schema})
                return False

        def is_alive(self):
            if not self.conn:
                return False
            try:
                c = self.conn.cursor()
                c.execute("SELECT 1")
                c.fetchone()
                c.close()
                self.last_activity = time.time()
                return True
            except Exception:
                return False

        def _rows(self, sql, params):
            if not self.ensure_connected():
                raise Exception("Failed to establish MariaDB connection")
            c = self.conn.cursor()
            try:
                c.execute(sql, params)
                return c.fetchall()
            finally:
                c.close()
                self.last_activity = time.time()

        def get_columns(self, table_name):
            rows = self._rows("""SELECT column_name, UPPER(data_type), is_nullable FROM information_schema.columns
                                 WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position""",
                              (self.schema, table_name))
            if not rows:
                raise Exception(f"Table {table_name} not found in schema {self.schema}")
            # real MariaDB type. The script skips names containing CLOB/BLOB/LONG (an Oracle limit), so here
            # only LONGTEXT and *BLOB are skipped; TEXT/MEDIUMTEXT get distinct + duplicate checks like VARCHAR
            return [{"name": r[0], "type": r[1], "nullable": r[2] == "YES"} for r in rows]

        def get_columns_from_query(self, query):
            if not self.ensure_connected():
                raise Exception("Failed to establish MariaDB connection")
            c = self.conn.cursor()
            try:
                c.execute(f"SELECT * FROM ({query}) WHERE 1=0")
                c.fetchall()
                from pymysql.constants import FIELD_TYPE   # type codes -> names, so TEXT/BLOB count as LOBs
                names = {code: name for name, code in vars(FIELD_TYPE).items() if name.isupper()}
                # the script calls anything with "LONG" in it a LOB; PyMySQL names INT "LONG" and BIGINT "LONGLONG"
                kind = lambda n: "BLOB" if "BLOB" in n else n.replace("LONGLONG", "BIGINT").replace("LONG", "INT")
                return [{"name": d[0], "type": kind(names.get(d[1], str(d[1]))), "nullable": True} for d in c.description]
            finally:
                c.close()

        def get_all_tables(self):
            return [r[0] for r in self._rows("""SELECT table_name FROM information_schema.tables
                                                WHERE table_schema = %s AND table_type = 'BASE TABLE' ORDER BY table_name""",
                                             (self.schema,))]

        def get_constraints(self, table_name):
            out = {"primary_keys": [], "unique_constraints": [], "check_constraints": [],
                   "not_null_columns": [], "foreign_keys": []}
            p = (self.schema, table_name)
            try:
                # names upper-cased like Oracle's, so they compare with Snowflake's
                out["primary_keys"] = [r[0].upper() for r in self._rows(
                    """SELECT column_name FROM information_schema.key_column_usage
                       WHERE table_schema = %s AND table_name = %s AND constraint_name = 'PRIMARY'
                       ORDER BY ordinal_position""", p)]
                uniq = {}
                # unique indexes from STATISTICS: table_constraints hides rows from SELECT-only users
                for name, col in self._rows(
                        """SELECT index_name, column_name FROM information_schema.statistics
                           WHERE table_schema = %s AND table_name = %s AND non_unique = 0 AND index_name <> 'PRIMARY'
                           ORDER BY index_name, seq_in_index""", p):
                    uniq.setdefault(name, []).append(col.upper())
                out["unique_constraints"] = [{"name": n, "columns": c} for n, c in uniq.items()]
                out["not_null_columns"] = [r[0].upper() for r in self._rows(
                    """SELECT column_name FROM information_schema.columns
                       WHERE table_schema = %s AND table_name = %s AND is_nullable = 'NO'
                       ORDER BY ordinal_position""", p)]
                fks = {}
                for name, col, rt, rc in self._rows(
                        """SELECT constraint_name, column_name, referenced_table_name, referenced_column_name
                           FROM information_schema.key_column_usage
                           WHERE table_schema = %s AND table_name = %s AND referenced_table_name IS NOT NULL
                           ORDER BY constraint_name, ordinal_position""", p):
                    fk = fks.setdefault(name, {"columns": [], "referenced_table": rt, "referenced_columns": []})
                    fk["columns"].append(col.upper())
                    fk["referenced_columns"].append(rc.upper())
                out["foreign_keys"] = [{"name": n, **f} for n, f in fks.items()]
                try:   # CHECK_CONSTRAINTS needs MariaDB 10.2+ / MySQL 8.0.16+
                    out["check_constraints"] = [{"name": n, "condition": c} for n, c in self._rows(
                        """SELECT constraint_name, check_clause FROM information_schema.check_constraints
                           WHERE constraint_schema = %s AND table_name = %s""", p)]
                except Exception:
                    pass
            except Exception as e:
                v.log_detailed_error(f"MariaDB - Failed to get constraints for table {table_name}", e,
                                     {"Table": table_name, "Schema": self.schema})
            return out

    return MariaDBConnector


# ═══════════════════════════════════════════════════════════════════════════════
# Runner: validate an extracted report with Src_to_Trgt_Validation_Script, no Snowflake save
# ═══════════════════════════════════════════════════════════════════════════════
_ORACLE_WORD = re.compile(r"\bOracle\b")


def _neutral(obj):
    """The script names everything 'oracle'; the page shows 'source' (Oracle, MariaDB, ...)."""
    if isinstance(obj, dict):
        own = obj.get("_own_error")   # our own messages say "Oracle" on purpose; only the script's get renamed
        keep = lambda k: (own and k in ("error", "problems")) or k in ("type", "source_type")   # "Oracle" here names the database
        return {k.replace("oracle", "source"): (x if keep(k) else _neutral(x))
                for k, x in obj.items() if k != "_own_error"}
    if isinstance(obj, list):
        return [_neutral(x) for x in obj]
    return _ORACLE_WORD.sub("Source", obj) if isinstance(obj, str) else obj


def _jdbc_database(url):
    """jdbc:mariadb://host:3306/HAQ?x=y -> 'HAQ'."""
    hit = re.match(r"jdbc:(?:mariadb|mysql)://[^/]+/([^?;/]+)", url or "", re.I)
    return hit.group(1) if hit else None


def _prepare(pipeline, database):
    """Fill gaps the script expects, on a copy of the extracted pipeline:
    - blank Tableconfigs schema -> the database from the JDBC URL (MariaDB pipelines leave it blank)
    - Snowflake 'Uppercaseschema' on -> upper-case target table names, as StreamSets created them"""
    p = json.loads(json.dumps(pipeline))
    flow = p.get("pipeline_flow") or {}
    for s in flow.get("sources") or []:
        cfg = s.get("configuration") or {}
        if database and cfg.get("Tableconfigs"):
            tc = json.loads(cfg["Tableconfigs"])
            for t in tc:
                if not (t.get("schema") or "").strip():
                    t["schema"] = database
                    print(f"ℹ️  Source schema blank in pipeline - using database '{database}' from the JDBC URL")
            cfg["Tableconfigs"] = json.dumps(tc)
    for t in flow.get("targets") or []:
        cfg = t.get("configuration") or {}
        if t.get("technology") == "Snowflake" and str(cfg.get("Uppercaseschema")).lower() == "true" \
                and cfg.get("Table(s)") and "${" not in cfg["Table(s)"]:
            cfg["Table(s)"] = cfg["Table(s)"].upper()
    return p


def _like(pattern):
    """SQL LIKE pattern (StreamSets 'Table' field) -> case-insensitive regex."""
    return re.compile("".join(".*" if c == "%" else "." if c == "_" else re.escape(c) for c in pattern) + r"\Z", re.I)


def _follow_table_switch(p, connector_cls, creds):
    """Do what StreamSets does with 'Is Table Pattern List Provided':
    Yes -> validate the list as is.  No -> ignore the list, read every table in the schema matching the
    pattern from the source (read-only), and map each to Snowflake with the pipeline's naming rule."""
    flow = p.get("pipeline_flow") or {}
    src = next((s for s in flow.get("sources") or [] if (s.get("configuration") or {}).get("Tableconfigs")), None)
    if not src:
        return p   # query-based or no table config: nothing to do
    cfg = src["configuration"]
    tcs = json.loads(cfg["Tableconfigs"])
    tc = tcs[0]   # ponytail: the script only reads the first table config; same here
    if tc.get("isTablePatternListProvided") is not False or not creds:
        return p
    pattern, schema = tc.get("tablePattern") or "%", tc.get("schema")
    conn = connector_cls(creds["username"], creds["password"], creds["dsn"], schema)
    if not conn.connect():
        tc["tablePatternList"] = []   # never fall back to the stale list; the script reports the connection error
        cfg["Tableconfigs"] = json.dumps(tcs)
        return p
    try:
        found = conn.get_all_tables()
    finally:
        conn.disconnect()
    like = _like(pattern)
    tables = [t for t in found if like.match(t)]
    if tc.get("tableExclusionPattern"):   # StreamSets exclusion is a Java regex
        tables = [t for t in tables if not re.fullmatch(tc["tableExclusionPattern"], t)]
    print(f"ℹ️  'Is Table Pattern List Provided' is No - ignoring the list of {len(tc.get('tablePatternList') or [])}, "
          f"validating all {len(tables)} table(s) matching '{pattern}' in {schema}, as StreamSets does")

    # Snowflake names: learn the prefix from the pairs the pipeline lists (arctic_x -> RDH_HAQ_ARCTIC_X)
    for t in flow.get("targets") or []:
        tcfg = t.get("configuration") or {}
        names = tcfg.get("Table(s)") or ""
        if t.get("technology") != "Snowflake" or not names or "${" in names:
            continue   # an expression like PREFIX${...} already names every table
        listed = [x.strip() for x in names.split("\n") if x.strip()]
        pairs = [sf[:-len(s)].upper() for s in tc.get("tablePatternList") or [] for sf in listed
                 if sf.upper().endswith(s.upper())]
        prefix, hits = Counter(pairs).most_common(1)[0] if pairs else (None, 0)
        if hits * 2 > len(listed):   # the rule has to explain most of the listed names

            tcfg["Table(s)"] = "\n".join(prefix + x for x in tables)
            print(f"   Snowflake names: '{prefix}' + table name")
        else:
            print("   ⚠️  Couldn't work out the Snowflake naming rule - only tables in the pipeline's Snowflake list will match")
    tc["tablePatternList"] = tables
    cfg["Tableconfigs"] = json.dumps(tcs)
    return p


def _jdbc_host_port(url):
    """jdbc:mariadb://host:3306/db?x=y -> ('host', '3306'); (None, None) if it isn't one."""
    hit = re.match(r"jdbc:(?:mariadb|mysql)://([^:/?,;]+)(?::(\d+))?", url or "", re.I)
    return (hit.group(1), hit.group(2)) if hit else (None, None)


def _oracle_dsn(conn: str):
    """What a user types for Oracle -> an oracledb DSN. Accepts host:port/service, host:port:SID,
    jdbc:oracle:thin:@... (any of those) or a full (DESCRIPTION=...). None if it isn't an Oracle connection."""
    c = (conn or "").strip()
    if c.lower().startswith("jdbc:"):
        if not c.lower().startswith("jdbc:oracle:"):
            return None
        c = c.split("@", 1)[1] if "@" in c else ""
    c = c.lstrip("/")
    sid = re.fullmatch(r"([^:/()\s]+):(\d+):([\w$#.]+)", c)
    if sid:   # SID form isn't valid easy-connect syntax
        return f"(DESCRIPTION=(ADDRESS=(PROTOCOL=TCP)(HOST={sid[1]})(PORT={sid[2]}))(CONNECT_DATA=(SID={sid[3]})))"
    return c or None


def _with_source(config, kind, src):
    """Swap the config's source login for the one the user typed (this run only). Returns (config, error)."""
    conn = (src.get("connection") or "").strip()
    if kind == "oracle":
        dsn = _oracle_dsn(conn) if conn else (config.get("oracle") or {}).get("dsn")
        if not dsn:
            return config, ("Enter the Oracle connection (host:port/service, host:port:SID or jdbc:oracle:thin:@...)"
                            if not conn else "That connection isn't an Oracle one - this pipeline's source is Oracle")
        return {**config, "oracle": {"username": src["user"], "password": src["password"], "dsn": dsn}}, None
    m = {**(config.get("mariadb") or {}), "user": src["user"], "password": src["password"]}
    if conn:
        if not re.match(r"jdbc:(mariadb|mysql)://", conn, re.I):
            return config, "That connection isn't a MariaDB one - use jdbc:mariadb://host:3306/database"
        m["jdbc_url"] = conn
        m.pop("host", None), m.pop("port", None)
    return {**config, "mariadb": m}, None


def _setup(v, pipeline, config, index, oracle_connector, mariadb_connector, source=None):
    """Everything before the comparison: which source, which login, which tables. Shared by the review (--plan)
    and the validation, so what the user confirms is exactly what gets validated.
    Returns {"error": result} or {"p": prepared pipeline, "cfg": config to validate with, "label": "Oracle"/"MariaDB"}."""
    kind, url, report_user = _source_of(pipeline)
    typed = (source or {}).get("connection", "").strip().lower()
    if not url and typed.startswith("jdbc:"):
        kind = "mariadb" if re.match(r"jdbc:(mariadb|mysql):", typed) else typed.split(":")[1]   # user said what it is
    elif not url and "mariadb" in config and "oracle" not in config:
        kind = "mariadb"   # pipeline uses a shared StreamSets connection, so no URL in the extract - trust the config
    fail = lambda msg, st: (print(f"❌ {msg}"), {"error": {"pipeline_name": pipeline.get("pipeline_name"), "pipeline_number": index,
                                                           "status": "ERROR", "error": msg, "validation_results": None,
                                                           "source_type": st, "_own_error": True}})[1]
    if kind not in ("oracle", "mariadb"):
        return fail(f"Source '{kind}' isn't supported yet - only Oracle and MariaDB/MySQL sources can be validated", kind)
    if source:
        config, err = _with_source(config, kind, source)
        if err:
            return fail(err, "Oracle" if kind == "oracle" else "MariaDB")
        print(f"🔑 Using the source login entered for this run ({source['user']})")
    if kind == "oracle":
        v.OracleConnector = oracle_connector
        if not (config.get("oracle") or {}).get("password"):
            return fail("Source is Oracle but there's no Oracle login - the validation config has no 'oracle' section", "Oracle")
        p = _prepare(_follow_table_switch(_prepare(pipeline, None), oracle_connector, config.get("oracle")), None)
        return {"p": p, "cfg": config, "label": "Oracle"}

    m = config.get("mariadb") or {}
    if not m.get("password"):
        return fail("Source is MariaDB but the validation config has no 'mariadb' section (user, password) - pick the MariaDB option", "MariaDB")
    # where to connect, first one set wins: config jdbc_url, config host/port, the pipeline's own JDBC URL
    cfg_host, cfg_port = _jdbc_host_port(m.get("jdbc_url"))
    pipe_host, pipe_port = _jdbc_host_port(url)
    host = cfg_host or m.get("host") or pipe_host
    port = cfg_port or m.get("port") or pipe_port or 3306
    if not host:
        return fail("MariaDB host unknown - add 'jdbc_url' (jdbc:mariadb://host:3306/db) to the 'mariadb' section of the config", "MariaDB")
    print(f"🐬 MariaDB source: {host}:{port}")
    v.OracleConnector = mariadb_connector
    ssl_url = m.get("jdbc_url") or url
    mariadb_connector.use_ssl = bool(m["ssl"]) if "ssl" in m else bool(re.search(r"use_?ssl=true|sslmode=(?!disable)", ssl_url or "", re.I))
    print(f"🔒 SSL {'on' if mariadb_connector.use_ssl else 'off'}")
    cfg = {**config, "oracle": {"username": m.get("user") or report_user, "password": m["password"], "dsn": f"{host}:{port}"}}
    database = _jdbc_database(m.get("jdbc_url")) or _jdbc_database(url)
    # fill the schema first (the table lookup needs it), then upper-case the Snowflake names it adds
    p = _prepare(_follow_table_switch(_prepare(pipeline, database), mariadb_connector, cfg["oracle"]), database)
    return {"p": p, "cfg": cfg, "label": "MariaDB"}


def _validate(v, pipeline, config, index, oracle_connector, mariadb_connector, source=None):
    st = _setup(v, pipeline, config, index, oracle_connector, mariadb_connector, source)
    if "error" in st:
        return st["error"]
    result = v.validate_single_pipeline(st["p"], st["cfg"], index)
    result["source_type"] = st["label"]
    return result


def _first_role(role):
    return role[0] if isinstance(role, list) and role else role


def _plan(v, pipeline, config, index, oracle_connector, mariadb_connector, source=None):
    """What a validation would compare, for the user to confirm: source and Snowflake details, both logins tested,
    and every source table with the Snowflake table it's checked against. Reads only."""
    name = pipeline.get("pipeline_name")
    st = _setup(v, pipeline, config, index, oracle_connector, mariadb_connector, source)
    if "error" in st:
        return {"pipeline_name": name, "ok": False, "error": st["error"]["error"], "problems": [st["error"]["error"]],
                "source_type": st["error"]["source_type"], "_own_error": True}
    p, cfg, label = st["p"], st["cfg"], st["label"]
    cl = v.ConfigLoader(p)
    fmt = cl.get_format_type()
    ora, sfc = cfg.get("oracle") or {}, cfg.get("snowflake") or {}
    sf_report = cl.get_snowflake_config()
    sf_schema, prefix = cl.get_snowflake_schema_and_prefix()
    sf_db = sf_report.get("database") or sfc.get("database")
    schema = cl.get_oracle_schema()
    plan = {"pipeline_name": name, "ok": True, "format": fmt, "excluded_columns": (cfg.get("validation") or {}).get("excluded_columns") or [],
            "source": {"type": label, "connection": ora.get("dsn"), "user": ora.get("username"), "schema": schema},
            "target": {"account": sfc.get("account"), "user": sfc.get("user"), "role": _first_role(sfc.get("role")),
                       "roles": sfc.get("role") if isinstance(sfc.get("role"), list) else None, "warehouse": sfc.get("warehouse"),
                       "database": sf_db, "schema": sf_schema, "login": "user/password" if sfc.get("private_key_path") == PASSWORD_LOGIN else "key-pair"}}

    # test both logins (connect + disconnect, nothing else)
    src = v.OracleConnector(ora.get("username"), ora.get("password"), ora.get("dsn"), schema or "")
    plan["source"]["login_ok"] = bool(src.connect())
    tgt = v.SnowflakeConnector(sfc.get("account"), sfc.get("user"), sfc.get("private_key_path"), sfc.get("private_key_password"),
                               plan["target"]["role"], sfc.get("warehouse"), sf_db, sf_schema)
    plan["target"]["login_ok"] = bool(tgt.connect())
    try:
        if fmt == "query_based":
            plan["query"] = cl.get_sql_query()
            plan["tables"] = [{"source": cl.get_oracle_table_from_query() or "(query)", "target": cl.get_snowflake_target_table()}]
        else:
            tables = cl.get_table_list()
            if not tables and getattr(cl, "table_pattern", "") == "%" and plan["source"]["login_ok"]:
                tables = src.get_all_tables()   # same as the script: empty list + '%' = every table in the schema
            use_prefix = cl.should_use_prefix()
            mapping = cl.create_table_mapping(tables) if cl.has_explicit_snowflake_list() and not use_prefix else {}
            plan["tables"] = [{"source": t, "target": mapping.get(t) or (f"{prefix or ''}{t}" if use_prefix else t)} for t in tables]
    finally:
        src.disconnect()
        tgt.disconnect()
    # every check a run depends on; each failure is listed so the page (and Auto mode) can say exactly why it stopped
    problems = []
    if not plan["source"]["login_ok"]:
        problems.append(f"Source login failed ({plan['source']['type']}, user {plan['source']['user']}) - see the log")
    if not plan["target"]["login_ok"]:
        problems.append(f"Snowflake login failed (user {plan['target']['user']}, role {plan['target']['role']}) - see the log")
    if not plan["target"]["database"] or not plan["target"]["schema"]:
        problems.append("The pipeline doesn't say which Snowflake database/schema it loads")
    if not plan["tables"]:
        problems.append("No tables to validate were found")
    unnamed = [t["source"] for t in plan["tables"] if not t["target"]]
    if unnamed:
        problems.append(f"{len(unnamed)} table(s) have no Snowflake table name: {', '.join(unnamed[:5])}{' ...' if len(unnamed) > 5 else ''}")
    plan["ok"] = not problems
    plan["problems"] = problems
    plan["_own_error"] = True   # every message in a plan is ours
    if problems:
        plan["error"] = problems[0] if len(problems) == 1 else f"{len(problems)} problems - see below"
    return plan


REPORT_TABLE = "DQ_IT_VALIDATION_REPORT"   # the CORE table the IT Checks page reads
PASSWORD_LOGIN = "<password-login>"         # in private_key_path: sign in to Snowflake with user/password instead


def _allow_password_login(v):
    """The script only knows key-pair sign-in. A Snowflake login captured from a pipeline is user/password, so
    connectors whose key path is PASSWORD_LOGIN sign in with the password (kept in private_key_password, which
    the script already leaves out of every log)."""
    key_pair = v.SnowflakeConnector.connect

    def connect(self):
        if self.private_key_path != PASSWORD_LOGIN:
            return key_pair(self)
        try:
            self.conn = v.snowflake.connector.connect(user=self.user, account=self.account, password=self.private_key_password,
                                                      warehouse=self.warehouse, database=self.database, schema=self.schema,
                                                      role=self.role)
            self.last_activity = time.time()
            return True
        except Exception as e:
            v.log_detailed_error("Snowflake Connection Failed", e, {"Account": self.account, "User": self.user, "Role": self.role,
                                                                    "Warehouse": self.warehouse, "Login": "user/password"})
            return False
    v.SnowflakeConnector.connect = connect


def _consolidated(v, results, report_path, seconds):
    """Same report shape save_report_to_snowflake() writes, so the IT Checks page reads it unchanged."""
    ok = [r for r in results if r.get("status") == "SUCCESS" and r.get("validation_results")]
    total = lambda key: sum(r["validation_results"]["summary"].get(key, 0) for r in ok)
    return {
        "report_metadata": {"generated_timestamp": datetime.now(v.EASTERN).isoformat(), "total_pipelines": len(results),
                            "total_execution_time": round(seconds, 2), "source_report": os.path.basename(report_path),
                            "generated_by": "pipeline_validation"},
        "overall_summary": {"successful_pipelines": len(ok), "failed_pipelines": len(results) - len(ok),
                            "total_tables_validated": total("total_validations"), "total_passed": total("passed"),
                            "total_critical": total("critical"), "total_errors": total("errors"),
                            "total_not_found": total("not_found"), "total_faulty": total("faulty")},
        "pipeline_results": results,
        "faulty_tables_summary": [{"pipeline_name": r["pipeline_name"], "pipeline_number": r.get("pipeline_number"),
                                   "faulty_tables": r["validation_results"]["faulty_tables"]}
                                  for r in ok if r["validation_results"].get("faulty_tables")],
    }


def _write_report(v, config, results, database_name, report_path, seconds):
    """Append one report row to CORE for the IT Checks page. Append-only: CREATE TABLE IF NOT EXISTS + one INSERT.
    No STG copy and no trimming (the scheduled IT run still trims its own database as before)."""
    ok = [r for r in results if r.get("status") == "SUCCESS" and r.get("validation_results")]
    if not ok:
        return {"written": False, "reason": "Nothing to write - validation didn't complete for any pipeline"}
    sf = v.load_snowflake_save_config(config)   # raises with the missing key's name
    table = f"{sf['database']}.{sf['core_schema']}.{REPORT_TABLE}"
    report = _consolidated(v, ok, report_path, seconds)
    load_date = datetime.now(v.EASTERN).strftime("%Y-%m-%d %H:%M:%S.%f")   # Eastern, like the script
    conn = v.create_snowflake_connection(sf)
    try:
        cur = conn.cursor()
        cur.execute(f"""CREATE TABLE IF NOT EXISTS {table} (
                            report_data VARIANT NOT NULL, run_timestamp TIMESTAMP_NTZ NOT NULL,
                            load_date TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP(), database VARCHAR(255))""")
        cur.execute(f"""INSERT INTO {table} (report_data, run_timestamp, load_date, database)
                        SELECT PARSE_JSON(%s), TO_TIMESTAMP_NTZ(%s), TO_TIMESTAMP_NTZ(%s), %s""",
                    (json.dumps(report, default=str), report["report_metadata"]["generated_timestamp"], load_date, database_name))
        if cur.rowcount != 1:
            raise RuntimeError(f"Snowflake reported {cur.rowcount} rows inserted, expected 1")
        cur.close()
    finally:
        conn.close()
    print(f"✅ Written to {table} (database '{database_name}', load_date {load_date}) - IT Checks will show it")
    return {"written": True, "table": table, "database": database_name, "load_date": load_date,
            "pipelines": [r["pipeline_name"] for r in ok]}


def _runner_main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--script", required=True)
    ap.add_argument("--config", required=True)
    ap.add_argument("--report", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--write", action="store_true", help="append the report to Snowflake CORE for IT Checks")
    ap.add_argument("--database-name", default="", help="'database' column value (IT Checks groups by it)")
    ap.add_argument("--overrides", help="JSON file with the logins typed for this run; read once, then deleted")
    ap.add_argument("--plan", action="store_true", help="don't validate: work out and test what would be compared")
    a = ap.parse_args()
    overrides = {}
    if a.overrides:
        try:
            with open(a.overrides, encoding="utf-8") as f:
                overrides = json.load(f)
        finally:
            os.remove(a.overrides)   # secrets live on disk only until this process has them
    started = time.time()

    sys.path.insert(0, os.path.dirname(os.path.abspath(a.script)))
    spec = importlib.util.spec_from_file_location("src_to_trgt", a.script)
    v = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(v)   # the script has a __main__ guard, so nothing runs on import
    _print = v.print_safe   # script logs say "Oracle"; the page says "Source"
    v.print_safe = lambda *xs, **kw: _print(*(_ORACLE_WORD.sub("Source", str(x)) for x in xs), **kw)

    config = v.load_config_file(a.config)
    pipelines = v.load_pipeline_report(a.report)
    if not pipelines:
        print("❌ Extracted report has no pipelines")
        return 1

    # ponytail: one at a time (connector swap is module-wide); the page sends one pipeline
    # a Snowflake login typed for this run is for reading only; writing below keeps the config's snowflake_save
    read_config = config
    t = overrides.get("target")
    if t:
        if "password" in t:   # captured from the pipeline: user/password
            _allow_password_login(v)
            t = {**{k: val for k, val in t.items() if k != "password"}, "private_key_path": PASSWORD_LOGIN, "private_key_password": t["password"]}
        read_config = {**config, "snowflake": {**(config.get("snowflake") or {}), **t}}
        print(f"🔑 Using a Snowflake login for this run ({t['user']}, role {read_config['snowflake'].get('role') or '?'})")
    oracle, maria = v.OracleConnector, _mariadb_connector(v)
    if a.plan:
        plans = _neutral([_plan(v, p, read_config, i, oracle, maria, overrides.get("source")) for i, p in enumerate(pipelines, 1)])
        tmp = a.output + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"plan": plans}, f, default=str)
        os.replace(tmp, a.output)
        print(f"✅ Review ready for {len(plans)} pipeline(s)")
        return 0
    raw = [_validate(v, p, read_config, i, oracle, maria, overrides.get("source")) for i, p in enumerate(pipelines, 1)]
    print(f"✅ Validation finished for {len(raw)} pipeline(s)")

    write = None
    if a.write:
        # the saved report keeps the script's own field names (oracle_*): the IT Checks page reads those
        try:
            write = _write_report(v, config, raw, a.database_name or None, a.report, time.time() - started)
        except Exception as e:   # validation results still go back to the page
            print(f"❌ Couldn't write to Snowflake: {type(e).__name__}: {e}")
            write = {"written": False, "reason": f"{type(e).__name__}: {e}"}
        if not write["written"]:
            print(f"⚠️  Not written to Snowflake: {write['reason']}")

    tmp = a.output + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"results": _neutral(raw), "write": write}, f, default=str)
    os.replace(tmp, a.output)
    return 0


if __name__ == "__main__":
    _code = 1
    try:
        _code = _runner_main()
    except SystemExit as e:   # the script calls sys.exit on bad config
        _code = e.code if isinstance(e.code, int) else 1
    except Exception as e:
        print(f"❌ {type(e).__name__}: {e}")
        traceback.print_exc()
    sys.stdout.flush()
    # hard exit like the original script: timed-out query threads would keep the process alive
    os._exit(_code)


# ═══════════════════════════════════════════════════════════════════════════════
# API (imported by app.py)
# ═══════════════════════════════════════════════════════════════════════════════
import asyncio
import logging
import shutil
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

import secrets
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from audit import write_audit_event
from auth import require_permission
import streamsets_extractor as ssx   # reuse its Control Hub client + extractor settings, no edits there

logger = logging.getLogger(__name__)

pipeline_validation_router = APIRouter(prefix="/api/pipeline-validation", tags=["Pipeline Validation (sandbox)"])

RUNNER = os.path.abspath(__file__)
# which validation config to use (it_validation.<id>.config_path); "mariadb" = a config with a 'mariadb' section
DBS = {"insight7": "Insight7", "rimdocs": "RIMDocs", "mariadb": "MariaDB"}
HEARTBEAT = 15

_it: Dict[str, Any] = {}
_scripts: Dict[str, Any] = {}
_runs = threading.BoundedSemaphore(2)   # ponytail: 2 runs per server, raise it if Oracle/Snowflake can take more
_active: set = set()                    # pipeline ids running on this server - one run per pipeline at a time
_active_lock = threading.Lock()
PREPARED_TTL = 30 * 60   # a reviewed extract waits this long for "Confirm & validate"
_prepared: Dict[str, Dict[str, Any]] = {}   # token -> {user, pipeline_id, db, fingerprint, work, expires}
_prepared_lock = threading.Lock()
SAVE_KEYS = ("account", "user", "private_key_path", "private_key_password", "role", "warehouse", "database", "stg_schema", "core_schema")   # what the script's load_snowflake_save_config requires


def set_pipeline_validation_dependencies(it_validation: Optional[Dict[str, Any]], scripts: Optional[Dict[str, Any]],
                                         report_connect=None, report_table: Optional[str] = None):
    """report_connect / report_table: how to read the IT Checks report table (History tab and last results)."""
    global _it, _scripts, _report_connect, _report_table
    _it = dict(it_validation or {})
    _scripts = dict(scripts or {})
    _report_connect, _report_table = report_connect, report_table
    _latest_cache.clear()
    _purge_prepared()   # tidy anything a previous server process left behind


def _sse(ev: Dict[str, Any]) -> str:
    return f"data: {json.dumps(ev, default=str, ensure_ascii=False)}\n\n"


def _validation_settings(db: str) -> Dict[str, Any]:
    if db not in DBS:
        raise HTTPException(400, f"db must be one of: {', '.join(DBS)}")
    script = _it.get("script_path")
    config = (_it.get(db) or {}).get("config_path")
    if not script or not config:
        raise HTTPException(503, f"Set it_validation.script_path and it_validation.{db}.config_path in dq_config.json")
    for p in (script, config):
        if not os.path.isfile(p):
            raise HTTPException(503, f"Not found on server: {p}")
    return {"script": script, "config": config,
            "python": _scripts.get("python_path") or sys.executable,
            "timeout": int(_it.get("run_timeout_seconds") or 3600)}


def _write_target(db: str, config_path: str) -> Dict[str, Any]:
    """Where 'Write to Snowflake' would append, read from the validation config's snowflake_save section
    (the same one the scheduled IT run writes with). Only names go to the browser, never credentials."""
    try:
        with open(config_path, encoding="utf-8-sig") as f:
            cfg = json.load(f)
    except (OSError, ValueError) as e:
        return {"can_write": False, "write_reason": f"Validation config can't be read: {e}"}
    save = cfg.get("snowflake_save") or {}
    missing = [k for k in SAVE_KEYS if not save.get(k)]
    if missing:
        return {"can_write": False, "write_reason": f"Add snowflake_save ({', '.join(missing)}) to the validation config to enable writing"}
    return {"can_write": True, "write_reason": None,
            "table": f"{save['database']}.{save['core_schema']}.{REPORT_TABLE}",
            "database_name": (cfg.get("validation") or {}).get("database_name") or DBS[db]}


@pipeline_validation_router.get("/setup")
def setup(auth: dict = Depends(require_permission("runITChecks"))):
    """Is everything configured? The page shows the reasons if not."""
    dbs = []
    for k, label in DBS.items():
        try:
            val = _validation_settings(k)
            dbs.append({"id": k, "label": label, "ready": True, "reason": None, **_write_target(k, val["config"])})
        except HTTPException as e:
            dbs.append({"id": k, "label": label, "ready": False, "reason": e.detail, "can_write": False, "write_reason": e.detail})
    return {"extractor": ssx._extractor_status(ssx._settings()), "databases": dbs, "can_capture": _can_capture(auth)}


def _audit_sync(action: str, user: str, target: str, details: str):
    try:
        write_audit_event(action=action, performed_by=user, target=target, details=details)
    except Exception:   # noqa: BLE001 - a run must not fail because the audit DB hiccuped; it's still in the app log
        logger.exception("Couldn't write audit event %s for %s", action, target)


async def _audit(action: str, user: str, target: str, details: str):
    await asyncio.to_thread(_audit_sync, action, user, target, details)


# ── past validations, read from the table the IT Checks page reads (scheduled runs and this page's writes) ──
_report_connect = None
_report_table: Optional[str] = None
LATEST_DAYS = 90          # "last result" badges only look this far back
LATEST_TTL = 5 * 60       # ...and are cached this long; a write from this page clears it
_latest_cache: Dict[str, Any] = {}


def _overall(status: Optional[str], summary: Dict[str, Any]) -> str:
    """One word for a pipeline's run, worst first - the same order the IT Checks badges use."""
    if status and status != "SUCCESS":
        return "ERROR"
    for key, word in (("critical", "CRITICAL"), ("errors", "ERROR"), ("faulty", "FAULTY"), ("not_found", "TABLE_NOT_FOUND")):
        if summary.get(key):
            return word
    return "PASSED"


def _variant(v) -> Dict[str, Any]:
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return {}
    return v if isinstance(v, dict) else {}


def _report_rows(sql: str, args: tuple) -> list:
    if not _report_connect or not _report_table:
        raise HTTPException(503, "The IT Checks report table isn't configured (snowflake section in dq_config.json)")
    conn = _report_connect()
    try:
        cur = conn.cursor()
        cur.execute(sql.format(table=_report_table), args)
        return cur.fetchall()
    finally:
        conn.close()


@pipeline_validation_router.get("/history")
def history(pipeline_name: str = Query(..., min_length=1, max_length=400), limit: int = Query(25, ge=1, le=100),
            auth: dict = Depends(require_permission("viewITChecks"))):
    """Every stored validation of one pipeline, newest first."""
    rows = _report_rows("""
        SELECT r.load_date, r.run_timestamp, r.database, r.report_data:report_metadata:generated_by::string,
               p.value:status::string, p.value:validation_results:summary, p.value:error::string
        FROM {table} r, LATERAL FLATTEN(input => r.report_data:pipeline_results) p
        WHERE p.value:pipeline_name::string = %s
        ORDER BY r.load_date DESC
        LIMIT %s""", (pipeline_name, limit))
    fmt = lambda d: d.strftime("%Y-%m-%d %H:%M:%S.%f")[:23] if d else None
    runs = []
    for load_date, run_ts, database, by, status, summary, error in rows:
        sm = _variant(summary)
        runs.append({"load_date": fmt(load_date), "run_timestamp": fmt(run_ts), "database": database,
                     "source": "page" if by == "pipeline_validation" else "scheduled", "status": _overall(status, sm),
                     "tables": sm.get("total_validations", 0), "passed": sm.get("passed", 0), "error": error})
    return {"runs": runs}


@pipeline_validation_router.get("/latest")
def latest(auth: dict = Depends(require_permission("viewITChecks"))):
    """Newest result per pipeline name (last LATEST_DAYS days) for the badges in the pipeline list."""
    hit = _latest_cache.get("v")
    if hit and time.time() - hit[0] < LATEST_TTL:
        return hit[1]
    rows = _report_rows("""
        SELECT p.value:pipeline_name::string, r.load_date, p.value:status::string, p.value:validation_results:summary
        FROM {table} r, LATERAL FLATTEN(input => r.report_data:pipeline_results) p
        WHERE r.load_date >= DATEADD(day, -%s, CURRENT_TIMESTAMP())
        QUALIFY ROW_NUMBER() OVER (PARTITION BY p.value:pipeline_name::string ORDER BY r.load_date DESC) = 1""", (LATEST_DAYS,))
    out = {"pipelines": {name: {"status": _overall(status, _variant(summary)),
                                "load_date": load_date.strftime("%Y-%m-%d %H:%M:%S.%f")[:23] if load_date else None}
                         for name, load_date, status, summary in rows if name}}
    _latest_cache["v"] = (time.time(), out)
    return out


@pipeline_validation_router.get("/pipelines")
def pipelines(refresh: bool = Query(False), auth: dict = Depends(require_permission("runITChecks"))):
    return ssx._pipelines(refresh)


# ── which pipelines can be validated (greyed out on the page otherwise) ──
_support_cache: Dict[str, Dict[str, Any]] = {}   # commit id -> verdict; commits never change


def _verdict(pipeline_id: str, commit_id: Optional[str]) -> Dict[str, Any]:
    """Same rules the run applies, read from the pipeline definition (no extraction):
    Snowflake target + JDBC table/query origin + an oracle/mariadb/mysql URL (or one we can't see)."""
    if not commit_id:
        commit_id = ssx._latest(pipeline_id, False)["commitId"]
    if commit_id in _support_cache:
        return _support_cache[commit_id]
    pdef = ssx._definition(ssx._get_commit(commit_id))
    constants = ssx._constants(pdef)
    stages = [s for s in pdef.get("stages") or [] if isinstance(s, dict)]
    targets = [s for s in stages if ssx._kind(s) == "TARGET"]
    origins = [s for s in stages if ssx._kind(s) == "SOURCE"]

    def no(reason, source=None):
        return {"ok": False, "reason": reason, "source": source}

    frag = lambda st: bool((st.get("uiInfo") or {}).get("fragmentGroupStage"))
    if any(frag(st) for st in targets + origins):
        v = {"ok": True, "reason": None, "source": None}   # source/target inside a fragment: decided at run time
    elif not any(ssx._technology(t) == "Snowflake" for t in targets):
        v = no("Target isn't Snowflake")
    elif not origins:
        v = no("No source stage")
    else:
        o = origins[0]
        name = str(o.get("stageName") or "").lower()
        if "origin_jdbc" not in name or "cdc" in name:   # JDBC Multitable / Query Consumer only
            v = no(f"Source is {ssx._type_name(o.get('stageName') or '')}", ssx._technology(o))
        else:
            cfg = {c.get("name"): c.get("value") for c in o.get("configuration") or [] if isinstance(c, dict)}
            shared = any(key and key.endswith("connectionSelection") and val not in (None, "", "MANUAL") for key, val in cfg.items())
            url = "" if shared else next((str(val) for key, val in cfg.items() if key and key.endswith("connectionString") and val), "")
            ref = re.fullmatch(r"\$\{\s*([A-Za-z_][\w.]*)\s*\}", url.strip())
            if ref:
                url = constants.get(ref.group(1), url)   # ${PARAM} -> pipeline parameter value
            driver = url.split(":")[1].lower() if url.lower().startswith("jdbc:") and ":" in url[5:] else None
            if driver in ("oracle", "mariadb", "mysql"):
                v = {"ok": True, "reason": None, "source": "MariaDB" if driver != "oracle" else "Oracle"}
            elif driver:
                v = no(f"Source '{driver}' isn't supported yet", driver)
            else:   # shared connection or runtime resource: can't see the URL until it runs
                v = {"ok": True, "reason": None, "source": None}
    _support_cache[commit_id] = v
    return v


@pipeline_validation_router.get("/support")
def support(auth: dict = Depends(require_permission("runITChecks"))):
    """{pipeline_id: {ok, reason, source}} for every pipeline. First call reads each definition (in parallel);
    after that only new commits are read."""
    listed = ssx._pipelines()["pipelines"]

    def one(p):
        try:
            return p["id"], _verdict(p["id"], p.get("commit_id"))
        except Exception as e:   # noqa: BLE001 - one bad pipeline mustn't hide the rest; let it be tried
            return p["id"], {"ok": True, "reason": None, "source": None, "warning": f"Couldn't check: {e}"}

    with ThreadPoolExecutor(max_workers=8) as pool:   # ponytail: 8 parallel Control Hub reads; raise if it's slow
        return dict(pool.map(one, listed))


# ── "From pipeline" logins: read the source / Snowflake logins the pipeline itself uses ──
CAPTURE_PERM = "viewStreamsetsSecrets"   # the same people who may already see these secrets in Data Studio
_PARAM_REF = re.compile(r"^\$\{\s*([A-Za-z_][\w.]*)\s*\}$")


def _can_capture(auth: dict) -> bool:
    return (auth.get("permissions") or {}).get(CAPTURE_PERM) is True


def _resolve(value, constants: Dict[str, str], origin: str = "pipeline setting", depth: int = 0):
    """A setting's real value: literal, ${PARAM} (pipeline parameter) or ${runtime:loadResource("f", x)} (file on
    the Data Collectors). Returns (value, where it came from, why not)."""
    v = "" if value is None else str(value).strip()
    if not v:
        return None, origin, "not set in the pipeline"
    if depth > 3:
        return None, origin, "parameters refer to each other too deeply"
    res = ssx.LOAD_RESOURCE_RE.search(v)
    if res and v.startswith("${runtime:"):
        text, host, tried = ssx._read_resource(res.group(1))
        if text is None:
            return None, f"resource file {res.group(1)}", f"not found on the Data Collectors ({', '.join(tried) or 'none registered'})"
        return text, f"resource file {res.group(1)}", None
    ref = _PARAM_REF.match(v)
    if ref:
        name = ref.group(1)
        if name not in constants:
            return None, f"parameter {name}", "the pipeline doesn't define this parameter (set at job level?)"
        return _resolve(constants[name], constants, f"parameter {name}", depth + 1)
    if "${" in v:   # credential:get(...), str: functions...: only StreamSets can work these out at run time
        return None, origin, "uses an expression StreamSets resolves at run time - can't be captured"
    return v, origin, None


def _settings_of(stage: Dict[str, Any]) -> Dict[str, Any]:
    """Stage settings by their last name segment (hikariConfigBean.connectionString -> connectionstring)."""
    out = {}
    for c in stage.get("configuration") or []:
        if isinstance(c, dict) and c.get("name"):
            out.setdefault(str(c["name"]).split(".")[-1].lower(), c.get("value"))
    return out


def _sf_account(account: str, region: Optional[str]) -> str:
    """Snowflake stage keeps the account locator and region apart; the connector wants them together."""
    if not region or "." in account or region.upper() in ("OTHER", "CUSTOM"):
        return account
    cloud, _, rest = region.upper().partition("_")
    r = rest.lower().replace("_", "-")
    if cloud == "AWS":
        return account if r == "us-west-2" else f"{account}.{r}"   # us-west-2 is Snowflake's default
    return f"{account}.{r}.{cloud.lower()}" if cloud in ("AZURE", "GCP") else account


def _capture(pipeline_id: str, commit_id: Optional[str] = None) -> Dict[str, Any]:
    """Logins the pipeline uses, from one exact commit. Returns {"public": shown to the user (no secret values),
    "secret": {"source": {...}, "target": {...}} kept on the server}."""
    latest = ssx._latest(pipeline_id, True)
    commit_id = commit_id or latest["commitId"]
    pdef = ssx._definition(ssx._get_commit(commit_id))
    constants = ssx._constants(pdef)
    stages = list(ssx._raw_stages(pdef))
    public = {"pipeline_id": pipeline_id, "commit_id": commit_id, "version": latest.get("version"),
              "is_latest": commit_id == latest["commitId"]}
    secret: Dict[str, Any] = {}

    def field(cfg, key, constants):
        val, where, why = _resolve(cfg.get(key), constants)
        return val, {"value": val, "from": where, "error": why}

    # source: the JDBC origin
    o = next((st for st in stages if ssx._kind(st) == "SOURCE" and "origin_jdbc" in str(st.get("stageName", "")).lower()
              and "cdc" not in str(st.get("stageName", "")).lower()), None)
    if not o:
        public["source"] = {"ok": False, "reason": "No JDBC source stage in this pipeline"}
    else:
        cfg = _settings_of(o)
        sel = cfg.get("connectionselection")
        if sel and sel != "MANUAL":
            public["source"] = {"ok": False, "reason": f"Signs in through a shared Control Hub connection ({sel}) - its logins aren't in the pipeline"}
        else:
            url, url_f = field(cfg, "connectionstring", constants)
            if url:   # a password can ride in the URL itself (…?password=x / user/pw@host): never show it
                url_f["value"] = re.sub(r"((?:password|pwd)=)[^&;]*", r"\1********", re.sub(r"(//[^/:@]+:)[^@/]+@", r"\1********@", url), flags=re.I)
            user, user_f = field(cfg, "username", constants)
            pw, pw_f = field(cfg, "password", constants)
            kind = ("MariaDB" if re.match(r"jdbc:(mariadb|mysql):", url or "", re.I) else
                    "Oracle" if (url or "").lower().startswith("jdbc:oracle:") else None)
            missing = [n for n, f in (("connection", url_f), ("username", user_f), ("password", pw_f)) if f["error"]]
            reason = "; ".join(f"{n}: {f['error']}" for n, f in (("connection", url_f), ("username", user_f), ("password", pw_f)) if f["error"])
            if url and not kind:
                missing, reason = missing + ["connection"], (reason + "; " if reason else "") + "the connection isn't Oracle or MariaDB/MySQL"
            public["source"] = {"ok": not missing, "reason": reason or None, "kind": kind, "stage": o.get("uiInfo", {}).get("label"),
                                "connection": url_f, "user": user_f,
                                "password": {"found": pw is not None, "from": pw_f["from"], "error": pw_f["error"]}}
            if not missing:
                secret["source"] = {"user": user, "password": pw, "connection": url}

    # target: the Snowflake destination
    t = next((st for st in stages if ssx._kind(st) == "TARGET" and ssx._technology(st) == "Snowflake"), None)
    if not t:
        public["target"] = {"ok": False, "reason": "No Snowflake destination in this pipeline"}
    else:
        cfg = _settings_of(t)
        sel = cfg.get("snowflakeconnectionselection")
        auth_m = str(cfg.get("snowflakeauthenticationmethod") or "CREDENTIALS").upper()
        if sel and sel != "MANUAL":
            public["target"] = {"ok": False, "reason": f"Signs in through a shared Control Hub connection ({sel}) - its logins aren't in the pipeline"}
        elif auth_m != "CREDENTIALS":
            public["target"] = {"ok": False, "reason": f"Signs in with {auth_m} - only user/password logins can be captured"}
        else:
            acc, acc_f = field(cfg, "account", constants)
            user, user_f = field(cfg, "user", constants)
            pw, pw_f = field(cfg, "password", constants)
            wh, wh_f = field(cfg, "warehouse", constants)
            role, role_f = field(cfg, "role", constants)
            region, _, _ = _resolve(cfg.get("snowflakeregion"), constants)
            if acc:
                acc_f["value"] = acc = _sf_account(acc, region)
            req = (("account", acc_f), ("user", user_f), ("password", pw_f), ("warehouse", wh_f))
            missing = [n for n, f in req if f["error"]]
            public["target"] = {"ok": not missing, "reason": "; ".join(f"{n}: {f['error']}" for n, f in req if f["error"]) or None,
                                "stage": t.get("uiInfo", {}).get("label"), "account": acc_f, "user": user_f, "warehouse": wh_f,
                                "role": role_f if role else {"value": None, "from": None, "error": None},
                                "password": {"found": pw is not None, "from": pw_f["from"], "error": pw_f["error"]}}
            if not missing:
                secret["target"] = {"account": acc, "user": user, "password": pw, "warehouse": wh, "role": role}
    return {"public": public, "secret": secret}


class CaptureRequest(BaseModel):
    pipeline_id: str = Field(..., min_length=1, max_length=300)


class RevealRequest(BaseModel):
    pipeline_id: str = Field(..., min_length=1, max_length=300)
    commit_id: str = Field(..., min_length=1, max_length=300)
    side: str = Field(..., pattern="^(source|target)$")


def _need_capture_perm(auth: dict):
    if not _can_capture(auth):
        raise HTTPException(403, f"Using the pipeline's own logins needs the {CAPTURE_PERM} permission")


@pipeline_validation_router.post("/capture")
async def capture(body: CaptureRequest, auth: dict = Depends(require_permission("runITChecks"))):
    """The logins the pipeline uses (latest version), for the user to check before a run. Passwords are only
    reported as found / where from; /capture/reveal shows one, audited."""
    _need_capture_perm(auth)
    ssx._check_id(body.pipeline_id)
    try:
        got = await asyncio.to_thread(_capture, body.pipeline_id)
    except ssx._NotFound:
        raise HTTPException(404, "Pipeline not found in StreamSets")
    await _audit("pipeline_validation_logins_captured", auth["user"]["email"], body.pipeline_id,
                 f"commit={got['public']['commit_id']} source={'ok' if got['public']['source']['ok'] else 'no'} "
                 f"snowflake={'ok' if got['public']['target']['ok'] else 'no'}")
    return got["public"]


@pipeline_validation_router.post("/capture/reveal")
async def capture_reveal(body: RevealRequest, response: Response, auth: dict = Depends(require_permission("runITChecks"))):
    """One captured password, for someone allowed to see pipeline secrets. Fail closed: no audit row, no value."""
    _need_capture_perm(auth)
    ssx._check_id(body.pipeline_id)
    response.headers["Cache-Control"] = "no-store"
    got = await asyncio.to_thread(_capture, body.pipeline_id, body.commit_id)
    value = (got["secret"].get(body.side) or {}).get("password")
    if value is None:
        raise HTTPException(404, "No password was captured for that side")
    try:
        await asyncio.to_thread(write_audit_event, action="pipeline_validation_secret_viewed", performed_by=auth["user"]["email"],
                                target=body.pipeline_id, details=f"Viewed captured {body.side} password (commit {body.commit_id})")
    except Exception:
        logger.exception("Couldn't audit a captured-password reveal")
        raise HTTPException(503, "Couldn't record the audit entry, so the password wasn't shown - try again")
    return {"side": body.side, "value": value}


async def _run_step(cmd: List[str], cwd: Optional[str], env: Dict[str, str], deadline: float, procs: list):
    """Run one subprocess. Yields ('log', line), ('ping', None), ('timeout', None), then ('exit', code)."""
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    procs.append(proc)
    loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue()

    # thread reader, not asyncio subprocess: those break on Windows' SelectorEventLoop (same as the IT runner)
    def pump():
        try:
            for raw in iter(proc.stdout.readline, b""):
                loop.call_soon_threadsafe(q.put_nowait, raw)
        except (ValueError, OSError):
            pass
        finally:
            try:
                loop.call_soon_threadsafe(q.put_nowait, None)
            except RuntimeError:
                pass   # request already gone
    threading.Thread(target=pump, daemon=True).start()

    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            proc.kill()
            yield "timeout", None
            break
        try:
            raw = await asyncio.wait_for(q.get(), timeout=min(HEARTBEAT, left))
        except asyncio.TimeoutError:
            yield "ping", None
            continue
        if raw is None:
            break
        line = raw.decode("utf-8", errors="replace").rstrip()
        if line:
            yield "log", line
    yield "exit", await asyncio.to_thread(proc.wait)


def _scrub(line: str, hidden: List[str]) -> str:
    for x in hidden:
        line = line.replace(x, "********")
    return line


def _kill_later(procs: list, work: str):
    """Off the event loop: stop anything still running, then remove the temp folder."""
    for p in procs:
        if p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
    if work:
        shutil.rmtree(work, ignore_errors=True)


class SourceLogin(BaseModel):
    """Source login typed for one run (Oracle or MariaDB). connection is optional: blank = the config's/pipeline's."""
    user: str = Field(..., min_length=1, max_length=128)
    password: str = Field(..., min_length=1, max_length=1024)
    connection: str = Field("", max_length=2048, description="host:port/service, host:port:SID, jdbc:oracle:thin:@..., jdbc:mariadb://...")


class TargetLogin(BaseModel):
    """Snowflake key-pair login typed for one run. Used for reading only; writes keep the config's service account."""
    account: str = Field(..., min_length=1, max_length=256)
    user: str = Field(..., min_length=1, max_length=256)
    role: str = Field(..., min_length=1, max_length=256)
    warehouse: str = Field(..., min_length=1, max_length=256)
    private_key: str = Field(..., min_length=1, max_length=16384, description="PEM text")
    private_key_password: str = Field("", max_length=1024)


class CapturedLogins(BaseModel):
    """Use the logins captured from the pipeline (the commit the user checked). Resolved again on the server."""
    commit_id: str = Field(..., min_length=1, max_length=300)
    source: bool = False
    target: bool = False
    target_role: str = Field("", max_length=256, description="when the pipeline names no Snowflake role; blank = config's")


class RunRequest(BaseModel):
    pipeline_id: str = Field(..., min_length=1, max_length=300)
    db: str = Field(..., description="insight7 | rimdocs | mariadb - picks the validation config")
    write: bool = Field(False, description="append the report to Snowflake CORE so IT Checks shows it")
    source: Optional[SourceLogin] = None
    target: Optional[TargetLogin] = None
    captured: Optional[CapturedLogins] = None
    token: Optional[str] = Field(None, max_length=100, description="from /prepare: validate the extract the user reviewed")


def _key_for_script(pem: str, passphrase: str):
    """The script loads its key from a file and always passes a passphrase, so: check the pasted key opens,
    then re-encrypt it with a one-off passphrase. Returns (pem bytes, passphrase) or raises 400."""
    from cryptography.hazmat.primitives import serialization
    try:
        key = serialization.load_pem_private_key(pem.strip().encode(), password=passphrase.encode() if passphrase else None)
    except TypeError:   # passphrase given for an unencrypted key, or none for an encrypted one
        raise HTTPException(400, "The Snowflake private key " + ("isn't encrypted - leave the passphrase empty"
                                                                 if passphrase else "is encrypted - enter its passphrase"))
    except Exception:   # bad PEM, wrong passphrase, unsupported key type
        raise HTTPException(400, "The Snowflake private key couldn't be read - paste the whole PEM and check the passphrase")
    one_off = secrets.token_urlsafe(24)
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.BestAvailableEncryption(one_off.encode())), one_off


def _write_private(path: str, data: bytes):
    """Create a file only this account can read (0600 on POSIX; the temp folder is per-user on Windows)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


SSE_HEADERS = {"Cache-Control": "no-cache, no-transform", "Connection": "keep-alive", "X-Accel-Buffering": "no"}


@pipeline_validation_router.post("/run")
async def run(body: RunRequest, auth: dict = Depends(require_permission("runITChecks"))):
    """POST (logins never go in a URL). Streams SSE (data JSON): start, step {step, state}, log {step, message},
    extract {report}, result {result}, write {written, table, database, load_date | reason}, error {message},
    done {ok, seconds}.
    A run refused before it starts (bad key, unknown pipeline, write not allowed...) also comes back as an
    error + done event with HTTP 200, carrying the original status: EventSource-style clients can't read
    the body of a 4xx, so the reason would otherwise be lost."""
    return await _refusable(_start_run(body, auth, "run"))


@pipeline_validation_router.post("/prepare")
async def prepare(body: RunRequest, auth: dict = Depends(require_permission("runITChecks"))):
    """Step 1 of 2: extract the pipeline and work out what a validation would compare - source and Snowflake
    details, both logins tested, every table pair - without comparing anything. Streams SSE like /run and ends
    with plan {token, plan, expires_in}. Pass the token to /run (with the same logins) once the user has checked it."""
    return await _refusable(_start_run(body, auth, "prepare"))


async def _refusable(started):
    try:
        return await started
    except HTTPException as e:
        if e.status_code in (401, 403):
            raise
        events = [_sse({"type": "error", "message": e.detail, "status": e.status_code}), _sse({"type": "done", "ok": False})]
        return StreamingResponse(iter(events), media_type="text/event-stream", headers=SSE_HEADERS)


ORPHAN_AGE = 6 * 3600   # a pv_ folder this old belongs to no live run or review (e.g. left by a server restart)


def _purge_prepared():
    """Drop reviews nobody confirmed in time (and their extract folders), plus folders orphaned by a restart."""
    now = time.time()
    with _prepared_lock:
        old = [k for k, x in _prepared.items() if x["expires"] < now]
        gone = [_prepared.pop(k) for k in old]
        live = {x["work"] for x in _prepared.values()}
    for x in gone:
        shutil.rmtree(x["work"], ignore_errors=True)
    tmp = tempfile.gettempdir()
    try:
        names = [n for n in os.listdir(tmp) if n.startswith("pv_")]
    except OSError:
        return
    for n in names:
        path = os.path.join(tmp, n)
        try:
            if path not in live and now - os.path.getmtime(path) > ORPHAN_AGE:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass


def _fingerprint(body: "RunRequest") -> str:
    """Which logins a run uses, without any secret: a review only holds for the same choice of logins."""
    import hashlib
    t = body.target
    parts = {"db": body.db,
             "source": body.source and {"user": body.source.user.strip(), "connection": body.source.connection.strip()},
             "target": t and {"account": t.account.strip().lower(), "user": t.user.strip(), "role": t.role.strip(), "warehouse": t.warehouse.strip(),
                              "key": hashlib.sha256(t.private_key.strip().encode()).hexdigest()},
             "captured": body.captured and {"commit": body.captured.commit_id, "source": body.captured.source,
                                            "target": body.captured.target, "role": body.captured.target_role.strip()}}
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()


async def _start_run(body: RunRequest, auth: dict, phase: str):
    pipeline_id, db, write = body.pipeline_id, body.db, body.write and phase == "run"
    _purge_prepared()
    ssx._check_id(pipeline_id)
    ext = ssx._settings()
    st = ssx._extractor_status(ext)
    if not st["ready"]:
        raise HTTPException(503, st["reason"])
    val = _validation_settings(db)
    target = _write_target(db, val["config"]) if write else None
    if write and not target["can_write"]:
        raise HTTPException(400, target["write_reason"])
    names = {p["id"]: p["name"] for p in (await asyncio.to_thread(ssx._pipelines))["pipelines"]}
    if pipeline_id not in names:
        names = {p["id"]: p["name"] for p in (await asyncio.to_thread(ssx._pipelines, True))["pipelines"]}
    if pipeline_id not in names:
        raise HTTPException(404, "Pipeline not found in StreamSets")
    user = (auth.get("user") or {}).get("email") or "unknown"

    reviewed = None
    if phase == "run" and body.token:
        with _prepared_lock:
            reviewed = _prepared.get(body.token)
            if reviewed and reviewed["user"] == user:
                _prepared.pop(body.token)   # one validation per review
        if not reviewed or reviewed["user"] != user:
            raise HTTPException(410, "That review has expired or was already used - extract and review again")
        if reviewed["pipeline_id"] != pipeline_id or reviewed["db"] != db or reviewed["fingerprint"] != _fingerprint(body):
            shutil.rmtree(reviewed["work"], ignore_errors=True)
            raise HTTPException(409, "The pipeline, config or logins changed after the review - extract and review again")

    # logins for this run: config (nothing here), typed (body.source / body.target) or captured from the pipeline
    cap = body.captured if body.captured and (body.captured.source or body.captured.target) else None
    if cap and ((cap.source and body.source) or (cap.target and body.target)):
        raise HTTPException(400, "Pick one login per side: the pipeline's or your own")
    src_login = body.source.model_dump() if body.source else None
    sf_password_login = None
    if cap:
        _need_capture_perm(auth)
        got = await asyncio.to_thread(_capture, pipeline_id, cap.commit_id)
        if not got["public"]["is_latest"]:
            raise HTTPException(409, "The pipeline has changed since its logins were checked - capture and confirm them again")
        for side, wanted in (("source", cap.source), ("target", cap.target)):
            if wanted and side not in got["secret"]:
                raise HTTPException(400, f"The pipeline's {'source' if side == 'source' else 'Snowflake'} login can't be used: "
                                         f"{got['public'][side].get('reason') or 'not captured'}")
        if cap.source:
            src_login = dict(got["secret"]["source"])
        if cap.target:
            sf_password_login = {k: v for k, v in got["secret"]["target"].items() if v}
            if cap.target_role.strip() and not sf_password_login.get("role"):
                sf_password_login["role"] = cap.target_role.strip()

    key_pem, key_pass = _key_for_script(body.target.private_key, body.target.private_key_password) if body.target else (None, None)
    # every secret in play is scrubbed from every log line before it reaches the browser (belt and braces: the
    # scripts already leave passwords out of their logs)
    hidden = [x for x in ((src_login or {}).get("password"), body.target and body.target.private_key_password, key_pass,
                          (sf_password_login or {}).get("password")) if x and len(x) >= 3]
    if body.target:
        hidden += [ln.strip() for ln in body.target.private_key.splitlines() if len(ln.strip()) >= 16]
    scrub = (lambda line: _scrub(line, hidden)) if hidden else (lambda line: line)

    def overrides_file(work: str) -> Optional[str]:
        if not (src_login or body.target or sf_password_login):
            return None
        data = {}
        if src_login:
            data["source"] = src_login
        if sf_password_login:
            data["target"] = sf_password_login   # 'password' key -> the runner signs in with user/password
        if body.target:
            key_path = os.path.join(work, "sf_key.p8")
            _write_private(key_path, key_pem)
            t = body.target
            account = re.sub(r"^https?://|\.snowflakecomputing\.com.*$", "", t.account.strip(), flags=re.I)   # pasted URL -> account
            data["target"] = {"account": account, "user": t.user.strip(), "role": t.role.strip(),
                              "warehouse": t.warehouse.strip(), "private_key_path": key_path, "private_key_password": key_pass}
        path = os.path.join(work, "overrides.json")
        _write_private(path, json.dumps(data).encode())
        return path

    async def steps(work: str, procs: list):
        report, result_path = os.path.join(work, "report.json"), os.path.join(work, "result.json")
        yield {"type": "start", "pipeline": {"id": pipeline_id, "name": names[pipeline_id]}, "db": db, "phase": phase}
        if reviewed:   # the extract the user reviewed is already here
            async for ev in validate_steps(work, procs, report, result_path):
                yield ev
            return

        # ── 1. extract ──
        yield {"type": "step", "step": "extract", "state": "running"}
        env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8",
               "STREAMSETS_URL": ext["url"], "STREAMSETS_USERNAME": ext["username"],
               "STREAMSETS_PASSWORD": ext["password"], "STREAMSETS_OUTPUT_DIR": work,
               "STREAMSETS_REQUEST_TIMEOUT": str(ext["timeout"]),
               "STREAMSETS_THREAD_POOL_SIZE": str(ext["thread_pool_size"])}
        cmd = [ext["python_path"], ext["script_path"], "--pipeline", pipeline_id, "--output", report]
        code = None
        async for kind, v in _run_step(cmd, os.path.dirname(ext["script_path"]) or None, env,
                                       time.monotonic() + ext["run_timeout"], procs):
            if kind == "log":
                yield {"type": "log", "step": "extract", "message": scrub(v)}
            elif kind in ("ping", "timeout"):
                yield kind
            else:
                code = v
        if code != 0 or not os.path.isfile(report):
            yield {"type": "step", "step": "extract", "state": "failed"}
            yield {"type": "error", "message": f"Extraction failed (exit {code}) - see the log"}
            return
        with open(report, encoding="utf-8") as f:
            yield {"type": "extract", "report": json.load(f)}
        yield {"type": "step", "step": "extract", "state": "done"}

        if phase == "prepare":
            # ── 2. review: what would be compared, both logins tested; nothing is compared yet ──
            yield {"type": "step", "step": "review", "state": "running"}
            plan_path = os.path.join(work, "plan.json")
            cmd = [val["python"], RUNNER, "--plan", "--script", val["script"], "--config", val["config"],
                   "--report", report, "--output", plan_path]
            ov = overrides_file(work)
            if ov:
                cmd += ["--overrides", ov]
            code = None
            async for kind, v in _run_step(cmd, os.path.dirname(val["config"]) or None,
                                           {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"},
                                           time.monotonic() + val["timeout"], procs):
                if kind == "log":
                    yield {"type": "log", "step": "review", "message": scrub(v)}
                elif kind in ("ping", "timeout"):
                    yield kind
                else:
                    code = v
            if not os.path.isfile(plan_path):
                yield {"type": "step", "step": "review", "state": "failed"}
                yield {"type": "error", "message": f"Couldn't work out what to compare (exit {code}) - see the log"}
                return
            with open(plan_path, encoding="utf-8") as f:
                plan = json.load(f)["plan"]
            ok = bool(plan) and all(x.get("ok") for x in plan)
            token = secrets.token_urlsafe(24) if ok else None   # only a review that checked out can be confirmed
            if token:
                yield {"type": "prepared", "token": token}
            yield {"type": "plan", "token": token, "plan": plan, "expires_in": PREPARED_TTL if token else None}
            yield {"type": "step", "step": "review", "state": "done" if ok else "failed"}
            if ok:
                yield {"type": "ok"}
            return

        async for ev in validate_steps(work, procs, report, result_path):
            yield ev

    async def validate_steps(work: str, procs: list, report: str, result_path: str):
        # ── validate (this same file, run as a script) ──
        yield {"type": "step", "step": "validate", "state": "running"}
        env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
        cmd = [val["python"], RUNNER, "--script", val["script"], "--config", val["config"],
               "--report", report, "--output", result_path]
        ov = overrides_file(work)
        if ov:
            cmd += ["--overrides", ov]
        if write:
            cmd += ["--write", "--database-name", target["database_name"]]
            yield {"type": "step", "step": "write", "state": "pending"}
        code = None
        async for kind, v in _run_step(cmd, os.path.dirname(val["config"]) or None, env,
                                       time.monotonic() + val["timeout"], procs):
            if kind == "log":
                yield {"type": "log", "step": "validate", "message": scrub(v)}
            elif kind in ("ping", "timeout"):
                yield kind
            else:
                code = v
        if not os.path.isfile(result_path):
            yield {"type": "step", "step": "validate", "state": "failed"}
            yield {"type": "error", "message": f"Validation failed (exit {code}) - see the log"}
            return
        with open(result_path, encoding="utf-8") as f:
            out = json.load(f)
        result = out["results"]
        ok = bool(result) and all(r.get("status") == "SUCCESS" for r in result)
        yield {"type": "result", "result": result}
        yield {"type": "step", "step": "validate", "state": "done" if ok else "failed"}
        if write:
            w = out.get("write") or {"written": False, "reason": "The validator didn't report a write"}
            yield {"type": "write", **w}
            if w["written"]:
                _latest_cache.clear()
            yield {"type": "step", "step": "write", "state": "done" if w["written"] else "failed"}
            yield {"type": "audit", "action": "pipeline_validation_written" if w["written"] else "pipeline_validation_write_failed",
                   "details": f"{w.get('table') or target['table']} database={target['database_name']}"
                              + (f" load_date={w['load_date']}" if w["written"] else f" reason={w.get('reason')}")}
            ok = ok and w["written"]
        if ok:
            yield {"type": "ok"}

    async def stream():
        with _active_lock:
            busy = pipeline_id in _active
            if not busy:
                _active.add(pipeline_id)
        if busy:
            yield _sse({"type": "error", "message": "This pipeline is already being validated - wait for that run to finish"})
            yield _sse({"type": "done", "ok": False})
            return
        if not _runs.acquire(blocking=False):
            with _active_lock:
                _active.discard(pipeline_id)
            yield _sse({"type": "error", "message": "Two runs are already going on this server - try again shortly"})
            yield _sse({"type": "done", "ok": False})
            return
        work, procs, started, ok, finished, keep = None, [], time.monotonic(), False, False, False
        login = lambda typed, captured: "pipeline" if captured else "custom" if typed else "config"
        what = (f"{names[pipeline_id]} ({pipeline_id}) phase={'review' if phase == 'prepare' else 'validate'}"
                f" config={db} write={'yes' if write else 'no'}"
                f" source_login={login(body.source, cap and cap.source)} snowflake_login={login(body.target, cap and cap.target)}"
                + (f" commit={cap.commit_id}" if cap else ""))
        logger.info("Pipeline validation by %s: %s", user, what)
        try:
            await _audit("pipeline_validation_started", user, pipeline_id, what)
            work = reviewed["work"] if reviewed else tempfile.mkdtemp(prefix="pv_")
            try:
                async for ev in steps(work, procs):
                    if ev == "ping":
                        yield ": ping\n\n"
                    elif ev == "timeout":
                        yield _sse({"type": "error", "message": "Step timed out"})
                    elif ev.get("type") == "ok":
                        ok = True
                    elif ev.get("type") == "audit":
                        await _audit(ev["action"], user, pipeline_id, ev["details"])
                    elif ev.get("type") == "prepared":   # keep the extract for "Confirm & validate"
                        for f in ("overrides.json", "sf_key.p8", "plan.json"):   # nothing secret waits on disk
                            try:
                                os.remove(os.path.join(work, f))
                            except OSError:
                                pass
                        with _prepared_lock:
                            _prepared[ev["token"]] = {"user": user, "pipeline_id": pipeline_id, "db": db, "work": work,
                                                      "fingerprint": _fingerprint(body), "expires": time.time() + PREPARED_TTL}
                        keep = True
                    else:
                        yield _sse(ev)
            except Exception as e:   # noqa: BLE001 - the stream must always end with done
                logger.exception("Pipeline validation crashed")
                yield _sse({"type": "error", "message": f"{type(e).__name__}: {e}"})
            seconds = round(time.monotonic() - started, 1)
            await _audit("pipeline_validation_finished", user, pipeline_id, f"{what} ok={ok} seconds={seconds}")
            finished = True
            yield _sse({"type": "done", "ok": ok, "seconds": seconds})
        finally:
            if not finished:   # browser closed / Stop pressed: can't await here, so audit from a thread
                threading.Thread(target=lambda: _audit_sync("pipeline_validation_cancelled", user, pipeline_id, what),
                                 daemon=True).start()
            # runs on success, error, and when the browser leaves mid-run (no yield allowed here)
            threading.Thread(target=_kill_later, args=(procs, None if keep else work), daemon=True).start()
            with _active_lock:
                _active.discard(pipeline_id)
            _runs.release()

    return StreamingResponse(stream(), media_type="text/event-stream", headers=SSE_HEADERS)