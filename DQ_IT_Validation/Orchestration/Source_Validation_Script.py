import oracledb
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from datetime import datetime
import pytz
import json
import sys

# All timestamps use Eastern Time (auto-handles EST/EDT daylight saving)
EASTERN = pytz.timezone('America/New_York')
import argparse
import re
from typing import Dict, List, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError
from threading import Lock, Thread
import time
import traceback as tb

# Global console lock for clean output
console_lock = Lock()


def print_safe(*args, **kwargs):
    """Thread-safe print function"""
    with console_lock:
        print(*args, **kwargs)


def print_header(text: str, char: str = "=", width: int = 80):
    """Print formatted header"""
    print_safe(f"\n{char * width}")
    print_safe(f"{text.center(width)}")
    print_safe(f"{char * width}")


def log_detailed_error(context: str, error: Exception, additional_info: Dict = None):
    """Log detailed error information to console"""
    print_safe(f"\n{'=' * 80}")
    print_safe(f"❌ ERROR: {context}")
    print_safe(f"{'=' * 80}")
    print_safe(f"Error: {str(error)}")
    if additional_info:
        print_safe(f"\nDetails:")
        for key, value in additional_info.items():
            print_safe(f"  • {key}: {value}")
    print_safe(f"{'=' * 80}\n")


def parse_streamsets_replaceall(expression: str) -> List[Tuple[str, str]]:
    cleaned = expression.replace('\\"', '"').replace('\\\\', '\\')
    pattern = r',\s*"([^"]*)",\s*"([^"]*)"\)'
    matches = re.findall(pattern, cleaned)
    return matches


def transform_column_name(column_name: str, transformations: List[Tuple[str, str]] = None) -> str:
    if transformations is None:
        transformations = [('[^a-zA-Z0-9]', '_'), ('_+', '_')]
    transformed = column_name
    for pattern, replacement in transformations:
        try:
            transformed = re.sub(pattern, replacement, transformed)
        except Exception as e:
            print_safe(f"⚠️  Regex error - pattern: {pattern}, replacement: {replacement}, error: {e}")
            continue
    return transformed


def load_config_file(config_path: str) -> Dict:
    try:
        with open(config_path, 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        print_safe(f"❌ Config file not found: {config_path}")
        sys.exit(1)
    except json.JSONDecodeError as e:
        print_safe(f"❌ Invalid JSON in config file: {e}")
        sys.exit(1)


def get_config_value(config: Dict, section: str, key: str) -> str:
    try:
        value = config[section][key]
        if not value:
            raise ValueError(f"Empty value for {section}.{key}")
        return value
    except KeyError:
        print_safe(f"❌ Missing required config: {section}.{key}")
        sys.exit(1)


def get_optional_config_value(config: Dict, section: str, key: str, default=None):
    try:
        return config[section].get(key, default)
    except KeyError:
        return default


def load_pipeline_report(report_path: str) -> List[Dict]:
    try:
        with open(report_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        else:
            return [data]
    except FileNotFoundError:
        print_safe(f"❌ Report file not found: {report_path}")
        sys.exit(1)
    except json.JSONDecodeError as e:
        print_safe(f"❌ Invalid JSON in report file: {e}")
        sys.exit(1)


class ConfigLoader:
    """Load configuration from StreamSets JSON report - supports single pipeline object"""

    def __init__(self, pipeline_data: dict):
        self.config = pipeline_data
        self.format_type = None
        self.sql_query = None
        self.oracle_schema_from_query = None
        self.oracle_table_from_query = None
        self._sf_table_list_cache = None
        self.field_name_transformation_detected = False
        self.transformation_rules = None
        self._detect_format_type()
        self._detect_field_name_transformation()

    def _detect_format_type(self):
        try:
            sources = self.config.get('pipeline_flow', {}).get('sources', [])
            for source in sources:
                if source.get('technology') == 'JDBC/Database':
                    config = source.get('configuration', {})
                    sql_query = config.get('SQL Query/Statement') or config.get('Query')
                    if sql_query and 'SELECT' in sql_query.upper():
                        self.format_type = 'query_based'
                        self.sql_query = self._clean_sql_query(sql_query)
                        self._extract_schema_from_query()
                        self._extract_table_from_query()
                        return
                    table_configs = config.get('Tableconfigs', '')
                    if table_configs:
                        self.format_type = 'table_list'
                        return
            self.format_type = 'table_list'
        except Exception:
            self.format_type = 'table_list'

    def _detect_field_name_transformation(self):
        try:
            stages = self.config.get('pipeline_stages', {}).get('stages', {})
            processors = stages.get('processors', [])
            for processor in processors:
                stage_name = processor.get('stageName', '')
                if 'FieldMapper' in stage_name or 'FieldMapperDProcessor' in stage_name:
                    config_list = processor.get('configuration', [])
                    operate_on = None
                    mapping_expression = None
                    for config_item in config_list:
                        if config_item.get('name') == 'fieldMapperConfig.operateOn':
                            operate_on = config_item.get('value')
                        elif config_item.get('name') == 'fieldMapperConfig.mappingExpression':
                            mapping_expression = config_item.get('value')
                    if operate_on == 'FIELD_NAMES' and mapping_expression:
                        transformations = parse_streamsets_replaceall(mapping_expression)
                        if transformations:
                            self.field_name_transformation_detected = True
                            self.transformation_rules = transformations
                            print_safe(f"  🔍 Detected Field Mapper transformation:")
                            for pattern, replacement in transformations:
                                repl_display = repr(replacement) if replacement else "''"
                                print_safe(f"     • Pattern: {pattern} → Replacement: {repl_display}")
                            return
        except Exception:
            pass

    def get_transformation_rules(self) -> List[Tuple[str, str]]:
        return self.transformation_rules if self.transformation_rules else []

    def has_field_name_transformation(self) -> bool:
        return self.field_name_transformation_detected

    def _clean_sql_query(self, query: str) -> str:
        if not query:
            return query
        cleaned = query.replace('\\n', ' ').replace('\n', ' ')
        cleaned = cleaned.replace('\\t', '').replace('\t', '')
        cleaned = ' '.join(cleaned.split())
        return cleaned

    def _extract_schema_from_query(self):
        if not self.sql_query:
            return
        try:
            pattern = r'FROM\s+([A-Za-z_][A-Za-z0-9_]*)\.'
            match = re.search(pattern, self.sql_query, re.IGNORECASE)
            if match:
                self.oracle_schema_from_query = match.group(1).upper()
        except Exception:
            pass

    def _extract_table_from_query(self):
        if not self.sql_query:
            return
        try:
            pattern = r'FROM\s+(?:[A-Za-z_][A-Za-z0-9_]*\.)?([A-Za-z_][A-Za-z0-9_]*)'
            match = re.search(pattern, self.sql_query, re.IGNORECASE)
            if match:
                self.oracle_table_from_query = match.group(1).upper()
        except Exception:
            pass

    def get_oracle_table_from_query(self) -> Optional[str]:
        return self.oracle_table_from_query

    def get_format_type(self) -> str:
        return self.format_type

    def get_sql_query(self) -> Optional[str]:
        return self.sql_query

    def get_snowflake_config(self) -> Dict:
        sf_config = {}
        try:
            targets = self.config.get('pipeline_flow', {}).get('targets', [])
            for target in targets:
                if target.get('technology') == 'Snowflake':
                    config = target.get('configuration', {})
                    sf_config['database'] = config.get('Database')
                    sf_config['schema'] = config.get('Schema')
                    sf_config['warehouse'] = config.get('Warehouse')
                    sf_config['user'] = config.get('User')
                    sf_config['account'] = config.get('Account')
                    break
        except Exception:
            pass
        return sf_config

    def get_oracle_schema(self) -> str:
        if self.format_type == 'query_based' and self.oracle_schema_from_query:
            return self.oracle_schema_from_query
        try:
            sources = self.config.get('pipeline_flow', {}).get('sources', [])
            for source in sources:
                if source.get('technology') == 'JDBC/Database':
                    config = source.get('configuration', {})
                    table_configs = config.get('Tableconfigs', '')
                    if table_configs:
                        table_config_obj = json.loads(table_configs)
                        if isinstance(table_config_obj, list) and len(table_config_obj) > 0:
                            return table_config_obj[0].get('schema', '')
        except Exception:
            pass
        return None

    def get_snowflake_schema_and_prefix(self) -> tuple:
        sf_schema = None
        table_prefix = None
        try:
            targets = self.config.get('pipeline_flow', {}).get('targets', [])
            for target in targets:
                if target.get('technology') == 'Snowflake':
                    config = target.get('configuration', {})
                    sf_schema = config.get('Schema')
                    table_config = config.get('Table(s)', '')
                    if self.format_type == 'query_based':
                        table_prefix = ''
                    else:
                        if table_config and '${' in table_config and 'tableName' in table_config:
                            table_prefix = table_config.split('${')[0]
                        elif table_config and '${' in table_config:
                            table_prefix = table_config.split('${')[0]
                        else:
                            table_prefix = ''
                    break
        except Exception:
            pass
        return sf_schema, table_prefix

    def get_snowflake_target_table(self) -> Optional[str]:
        if self.format_type != 'query_based':
            return None
        try:
            targets = self.config.get('pipeline_flow', {}).get('targets', [])
            for target in targets:
                if target.get('technology') == 'Snowflake':
                    config = target.get('configuration', {})
                    table_name = config.get('Table(s)', '')
                    if table_name and '${' not in table_name:
                        return table_name
        except Exception:
            pass
        return None

    def get_snowflake_table_list(self) -> List[str]:
        if self._sf_table_list_cache is not None:
            return self._sf_table_list_cache
        if self.format_type == 'query_based':
            self._sf_table_list_cache = []
            return self._sf_table_list_cache
        sf_tables = []
        try:
            targets = self.config.get('pipeline_flow', {}).get('targets', [])
            for target in targets:
                if target.get('technology') == 'Snowflake':
                    config = target.get('configuration', {})
                    table_config = config.get('Table(s)', '')
                    if table_config and '${' not in table_config:
                        sf_tables = [t.strip() for t in table_config.split('\n') if t.strip()]
                        if sf_tables:
                            self._sf_table_list_cache = sf_tables
                            return sf_tables
                    table_configs = config.get('Tableconfigs', '')
                    if table_configs:
                        table_config_obj = json.loads(table_configs)
                        if isinstance(table_config_obj, list) and len(table_config_obj) > 0:
                            sf_tables = table_config_obj[0].get('tablePatternList', [])
                            if sf_tables:
                                self._sf_table_list_cache = sf_tables
                                return sf_tables
                    table_list = config.get('TableList', [])
                    if table_list:
                        self._sf_table_list_cache = table_list
                        return table_list
                    break
        except Exception:
            pass
        self._sf_table_list_cache = sf_tables
        return sf_tables

    def get_table_list(self) -> List[str]:
        if self.format_type == 'query_based':
            return []
        oracle_tables = []
        try:
            sources = self.config.get('pipeline_flow', {}).get('sources', [])
            for source in sources:
                if source.get('technology') == 'JDBC/Database':
                    config = source.get('configuration', {})
                    table_configs = config.get('Tableconfigs', '')
                    if table_configs:
                        table_config_obj = json.loads(table_configs)
                        if isinstance(table_config_obj, list) and len(table_config_obj) > 0:
                            oracle_tables = table_config_obj[0].get('tablePatternList', [])
                            self.table_pattern = table_config_obj[0].get('tablePattern', '')
                            break
        except Exception:
            pass
        return oracle_tables

    def has_explicit_snowflake_list(self) -> bool:
        return len(self.get_snowflake_table_list()) > 0

    def should_use_prefix(self) -> bool:
        return not self.has_explicit_snowflake_list()

    def create_table_mapping(self, oracle_tables: List[str]) -> Dict[str, str]:
        mapping = {}
        sf_tables = self.get_snowflake_table_list()
        if not sf_tables:
            return mapping
        sf_upper_to_name = {sf.upper(): sf for sf in sf_tables}
        sf_suffix_map = {sf.upper(): sf for sf in sf_tables}
        for oracle_table in oracle_tables:
            oracle_upper = oracle_table.upper()
            if oracle_upper in sf_upper_to_name:
                mapping[oracle_table] = sf_upper_to_name[oracle_upper]
                continue
            matched = False
            for sf_upper, sf_name in sf_suffix_map.items():
                if sf_upper.endswith(oracle_upper):
                    mapping[oracle_table] = sf_name
                    matched = True
                    break
            if matched:
                continue
            for sf_upper, sf_name in sf_suffix_map.items():
                if oracle_upper in sf_upper:
                    mapping[oracle_table] = sf_name
                    break
        return mapping


class ConnectionPool:
    """Thread-safe connection pool for parallel execution"""

    def __init__(self, connector_class, connection_params: dict, max_connections: int = 5):
        self.connector_class = connector_class
        self.connection_params = connection_params
        self.max_connections = max_connections
        self.connections = []
        self.available = []
        self.lock = Lock()
        self.keepalive_interval = 300
        self.keepalive_thread = None
        self.keepalive_running = False
        self._start_keepalive()

    def _start_keepalive(self):
        self.keepalive_running = True
        self.keepalive_thread = Thread(target=self._keepalive_worker, daemon=True)
        self.keepalive_thread.start()

    def _keepalive_worker(self):
        check_interval = 1
        elapsed = 0
        while self.keepalive_running:
            time.sleep(check_interval)
            elapsed += check_interval
            if elapsed >= self.keepalive_interval:
                elapsed = 0
                with self.lock:
                    for conn in self.connections:
                        try:
                            if hasattr(conn, 'last_activity') and conn.last_activity:
                                idle_time = time.time() - conn.last_activity
                                if idle_time > 240:
                                    conn.is_alive()
                        except:
                            pass

    def get_connection(self, max_wait_seconds: int = 30):
        start_time = time.time()
        while True:
            with self.lock:
                while self.available:
                    conn = self.available.pop()
                    if conn.is_alive():
                        return conn
                    else:
                        if conn.reconnect():
                            return conn
                        else:
                            try:
                                self.connections.remove(conn)
                            except ValueError:
                                pass
                if len(self.connections) < self.max_connections:
                    try:
                        conn = self.connector_class(**self.connection_params)
                        if conn.connect():
                            self.connections.append(conn)
                            return conn
                        else:
                            log_detailed_error(
                                "Connection Pool - Failed to connect",
                                Exception("Connection attempt returned False"),
                                {"Connector": self.connector_class.__name__,
                                 "Connection Params": {k: v for k, v in self.connection_params.items()
                                                       if k not in ['password', 'private_key_password']}}
                            )
                            return None
                    except Exception as e:
                        log_detailed_error(
                            "Connection Pool - Exception during connection",
                            e,
                            {"Connector": self.connector_class.__name__,
                             "Connection Params": {k: v for k, v in self.connection_params.items()
                                                   if k not in ['password', 'private_key_password']}}
                        )
                        return None
            elapsed = time.time() - start_time
            if elapsed >= max_wait_seconds:
                log_detailed_error(
                    "Connection Pool Exhausted",
                    Exception(f"No connections available after {max_wait_seconds}s"),
                    {"Connector": self.connector_class.__name__,
                     "Total Connections": len(self.connections),
                     "Available": len(self.available),
                     "Max Connections": self.max_connections,
                     "Waited": f"{elapsed:.1f}s"}
                )
                return None
            time.sleep(0.1)

    def return_connection(self, conn):
        with self.lock:
            if conn and conn not in self.available:
                self.available.append(conn)

    def close_all(self, force_skip_disconnect=False):
        self.keepalive_running = False
        if self.keepalive_thread:
            self.keepalive_thread.join(timeout=3)
        with self.lock:
            if force_skip_disconnect:
                print_safe("      ⚠️  Skipping connection disconnect (abandoned threads detected)")
                self.connections.clear()
                self.available.clear()
            else:
                for conn in self.connections:
                    try:
                        conn.disconnect()
                    except:
                        pass
                self.connections.clear()
                self.available.clear()


class OracleConnector:
    """Oracle database connector with connection pooling support"""

    def __init__(self, user: str, password: str, dsn: str, schema: str):
        self.user = user
        self.password = password
        self.dsn = dsn
        self.schema = schema
        self.conn = None
        self.last_activity = None

    def connect(self):
        try:
            self.conn = oracledb.connect(user=self.user, password=self.password, dsn=self.dsn)
            self.last_activity = time.time()
            return True
        except Exception as e:
            log_detailed_error("Oracle Connection Failed", e,
                               {"User": self.user, "DSN": self.dsn, "Schema": self.schema})
            return False

    def is_alive(self) -> bool:
        if not self.conn:
            return False
        try:
            cursor = self.conn.cursor()
            cursor.execute("SELECT 1 FROM DUAL")
            cursor.fetchone()
            cursor.close()
            self.last_activity = time.time()
            return True
        except:
            return False

    def reconnect(self) -> bool:
        try:
            self.disconnect()
        except:
            pass
        return self.connect()

    def ensure_connected(self) -> bool:
        if not self.is_alive():
            print_safe(f"      ⚠️  Oracle connection lost, reconnecting...")
            if self.reconnect():
                print_safe(f"      ✓ Oracle reconnected successfully")
                return True
            else:
                print_safe(f"      ✗ Oracle reconnection failed")
                return False
        return True

    def disconnect(self):
        if self.conn:
            try:
                self.conn.close()
            except:
                pass
        self.conn = None

    def get_columns(self, table_name: str) -> List[Dict]:
        max_retries = 3
        for attempt in range(max_retries):
            try:
                if not self.ensure_connected():
                    if attempt < max_retries - 1:
                        time.sleep(2 ** attempt)
                        continue
                    else:
                        raise Exception("Failed to establish Oracle connection after retries")
                cursor = self.conn.cursor()
                query = """
                    SELECT column_name, data_type, nullable
                    FROM all_tab_columns
                    WHERE table_name = :table_name
                    AND owner = :schema
                    ORDER BY column_id
                """
                cursor.execute(query, {'table_name': table_name.upper(), 'schema': self.schema.upper()})
                columns = []
                for row in cursor.fetchall():
                    columns.append({'name': row[0], 'type': row[1], 'nullable': row[2] == 'Y'})
                cursor.close()
                self.last_activity = time.time()
                if not columns:
                    error_msg = f"Table {table_name} not found in schema {self.schema}"
                    log_detailed_error("Oracle - Table Not Found", Exception(error_msg),
                                       {"Table": table_name, "Schema": self.schema,
                                        "Possible Reason": "Table does not exist in Oracle source database"})
                    raise Exception(error_msg)
                return columns
            except Exception as e:
                error_str = str(e)
                if "not connected" in error_str.lower() or "dpy-1001" in error_str.lower():
                    if attempt < max_retries - 1:
                        print_safe(f"      ⚠️  Connection error on attempt {attempt + 1}, retrying...")
                        time.sleep(2 ** attempt)
                        continue
                if "not found" not in error_str.lower():
                    log_detailed_error(f"Oracle - Failed to retrieve columns for table {table_name}", e,
                                       {"Table": table_name, "Schema": self.schema, "Attempt": attempt + 1})
                raise

    def get_columns_from_query(self, query: str) -> List[Dict]:
        max_retries = 3
        for attempt in range(max_retries):
            try:
                if not self.ensure_connected():
                    if attempt < max_retries - 1:
                        time.sleep(2 ** attempt)
                        continue
                    else:
                        raise Exception("Failed to establish Oracle connection after retries")
                cursor = self.conn.cursor()
                test_query = f"SELECT * FROM ({query}) WHERE 1=0"
                cursor.execute(test_query)
                columns = []
                for desc in cursor.description:
                    columns.append({
                        'name': desc[0],
                        'type': desc[1].__name__ if hasattr(desc[1], '__name__') else str(desc[1]),
                        'nullable': True
                    })
                cursor.close()
                self.last_activity = time.time()
                return columns
            except Exception as e:
                error_str = str(e)
                if "not connected" in error_str.lower() or "dpy-1001" in error_str.lower():
                    if attempt < max_retries - 1:
                        print_safe(f"      ⚠️  Connection error on attempt {attempt + 1}, retrying...")
                        time.sleep(2 ** attempt)
                        continue
                log_detailed_error("Oracle - Failed to retrieve columns from query", e,
                                   {"Query": query[:200] + "..." if len(query) > 200 else query,
                                    "Possible Reason": "Query syntax error or query references non-existent objects",
                                    "Attempt": attempt + 1})
                raise

    def get_all_tables(self) -> List[str]:
        max_retries = 3
        for attempt in range(max_retries):
            try:
                if not self.ensure_connected():
                    if attempt < max_retries - 1:
                        time.sleep(2 ** attempt)
                        continue
                    else:
                        raise Exception("Failed to establish Oracle connection after retries")
                cursor = self.conn.cursor()
                cursor.execute("SELECT table_name FROM all_tables WHERE owner = :schema ORDER BY table_name",
                               {'schema': self.schema.upper()})
                tables = [row[0] for row in cursor.fetchall()]
                cursor.close()
                self.last_activity = time.time()
                if not tables:
                    print_safe(f"⚠️  WARNING: No tables found in Oracle schema {self.schema}")
                return tables
            except Exception as e:
                error_str = str(e)
                if "not connected" in error_str.lower() or "dpy-1001" in error_str.lower():
                    if attempt < max_retries - 1:
                        print_safe(f"      ⚠️  Connection error on attempt {attempt + 1}, retrying...")
                        time.sleep(2 ** attempt)
                        continue
                log_detailed_error("Oracle - Failed to retrieve table list", e,
                                   {"Schema": self.schema,
                                    "Possible Reason": "Schema does not exist or no access permissions",
                                    "Attempt": attempt + 1})
                raise

    def get_constraints(self, table_name: str) -> Dict:
        constraints = {
            'primary_keys': [], 'unique_constraints': [], 'check_constraints': [],
            'not_null_columns': [], 'foreign_keys': []
        }
        try:
            cursor = self.conn.cursor()
            cursor.execute("""
                SELECT cols.column_name
                FROM all_constraints cons
                JOIN all_cons_columns cols ON cons.constraint_name = cols.constraint_name
                WHERE cons.constraint_type = 'P'
                AND cons.table_name = :table_name
                AND cons.owner = :schema
                ORDER BY cols.position
            """, {'table_name': table_name.upper(), 'schema': self.schema.upper()})
            constraints['primary_keys'] = [row[0] for row in cursor.fetchall()]

            cursor.execute("""
                SELECT cons.constraint_name, cols.column_name
                FROM all_constraints cons
                JOIN all_cons_columns cols ON cons.constraint_name = cols.constraint_name
                WHERE cons.constraint_type = 'U'
                AND cons.table_name = :table_name
                AND cons.owner = :schema
                ORDER BY cons.constraint_name, cols.position
            """, {'table_name': table_name.upper(), 'schema': self.schema.upper()})
            unique_dict = {}
            for row in cursor.fetchall():
                if row[0] not in unique_dict:
                    unique_dict[row[0]] = []
                unique_dict[row[0]].append(row[1])
            constraints['unique_constraints'] = [{'name': name, 'columns': cols}
                                                  for name, cols in unique_dict.items()]

            cursor.execute("""
                SELECT constraint_name, search_condition
                FROM all_constraints
                WHERE constraint_type = 'C'
                AND table_name = :table_name
                AND owner = :schema
                AND constraint_name NOT LIKE 'SYS_%'
            """, {'table_name': table_name.upper(), 'schema': self.schema.upper()})
            for row in cursor.fetchall():
                condition = row[1].read() if hasattr(row[1], 'read') else row[1]
                if condition and 'IS NOT NULL' not in condition.upper():
                    constraints['check_constraints'].append({'name': row[0], 'condition': condition})

            cursor.execute("""
                SELECT column_name
                FROM all_tab_columns
                WHERE table_name = :table_name
                AND owner = :schema
                AND nullable = 'N'
                ORDER BY column_id
            """, {'table_name': table_name.upper(), 'schema': self.schema.upper()})
            constraints['not_null_columns'] = [row[0] for row in cursor.fetchall()]

            cursor.execute("""
                SELECT cons.constraint_name, cols.column_name,
                       ref_cons.table_name as referenced_table,
                       ref_cols.column_name as referenced_column
                FROM all_constraints cons
                JOIN all_cons_columns cols ON cons.constraint_name = cols.constraint_name
                JOIN all_constraints ref_cons ON cons.r_constraint_name = ref_cons.constraint_name
                JOIN all_cons_columns ref_cols
                     ON ref_cons.constraint_name = ref_cols.constraint_name
                     AND cols.position = ref_cols.position
                WHERE cons.constraint_type = 'R'
                AND cons.table_name = :table_name
                AND cons.owner = :schema
                ORDER BY cons.constraint_name, cols.position
            """, {'table_name': table_name.upper(), 'schema': self.schema.upper()})
            fk_dict = {}
            for row in cursor.fetchall():
                fk_name = row[0]
                if fk_name not in fk_dict:
                    fk_dict[fk_name] = {'columns': [], 'referenced_table': row[2], 'referenced_columns': []}
                fk_dict[fk_name]['columns'].append(row[1])
                fk_dict[fk_name]['referenced_columns'].append(row[3])
            constraints['foreign_keys'] = [
                {'name': name, 'columns': fk_info['columns'],
                 'referenced_table': fk_info['referenced_table'],
                 'referenced_columns': fk_info['referenced_columns']}
                for name, fk_info in fk_dict.items()
            ]
            cursor.close()
        except Exception as e:
            log_detailed_error(f"Oracle - Failed to get constraints for table {table_name}", e,
                               {"Table": table_name, "Schema": self.schema})
        return constraints


class SourceDataCollector:
    """Collects Oracle source data snapshot for later validation against Snowflake"""

    LOB_TYPES = {'CLOB', 'BLOB', 'NCLOB', 'BFILE', 'LONG', 'LONG RAW'}

    def __init__(self, oracle_pool: ConnectionPool, tables: List[str],
                 sf_table_prefix: str = "", excluded_columns: List[str] = None,
                 format_type: str = 'table_list', sql_query: str = None,
                 sf_target_table: str = None, oracle_table_from_query: str = None,
                 use_prefix: bool = True, table_mapping: Dict[str, str] = None,
                 parallel_execution: bool = True, max_parallel_tables: int = 5,
                 table_timeout: int = 60, max_retries: int = 3,
                 field_name_transformation: bool = False,
                 transformation_rules: List[Tuple[str, str]] = None):
        self.oracle_pool = oracle_pool
        self.tables = tables
        self.sf_table_prefix = sf_table_prefix
        self.use_prefix = use_prefix
        self.table_mapping = table_mapping or {}
        self.excluded_columns = [col.upper() for col in (excluded_columns or [])]
        self.format_type = format_type
        self.sql_query = sql_query
        self.sf_target_table = sf_target_table
        self.oracle_table_from_query = oracle_table_from_query
        self.parallel_execution = parallel_execution
        self.max_parallel_tables = max_parallel_tables
        self.table_timeout = table_timeout
        self.max_retries = max_retries
        self.field_name_transformation = field_name_transformation
        self.transformation_rules = transformation_rules or []
        self.results_lock = Lock()
        self.has_abandoned_threads = False
        self.results = {
            'metadata': {
                'collection_timestamp': datetime.now(EASTERN).isoformat(),
                'format_type': format_type,
                'total_tables': len(tables) if format_type == 'table_list' else 1,
                'excluded_columns': self.excluded_columns,
                'parallel_execution': parallel_execution,
                'max_parallel_workers': max_parallel_tables if parallel_execution else 1,
                'table_timeout_seconds': table_timeout,
                'max_retries': max_retries,
                'field_name_transformation_enabled': field_name_transformation
            },
            'summary': {},
            'table_data': [],
            'faulty_tables': []
        }

    def _filter_excluded_columns(self, columns: List[Dict]) -> List[Dict]:
        if not self.excluded_columns:
            return columns
        return [col for col in columns if col['name'].upper() not in self.excluded_columns]

    def _get_oracle_row_count(self, oracle_conn, oracle_source: str, is_query: bool) -> Optional[int]:
        try:
            if is_query:
                if ':' in oracle_source and 'TO_DATE' not in oracle_source.upper():
                    bind_vars = re.findall(r':\w+', oracle_source)
                    if bind_vars:
                        raise Exception(f"Query contains bind variables that cannot be wrapped: {bind_vars}")
                query = f"SELECT /*+ PARALLEL(8) */ COUNT(*) FROM ({oracle_source})"
            else:
                query = f'SELECT /*+ PARALLEL(8) */ COUNT(*) FROM "{oracle_conn.schema}"."{oracle_source}"'
            cursor = oracle_conn.conn.cursor()
            cursor.execute(query)
            count = cursor.fetchone()[0]
            cursor.close()
            return count
        except Exception as e:
            log_detailed_error(
                f"Oracle - Failed to get row count for {oracle_source[:100] if is_query else oracle_source}",
                e, {"Source": oracle_source[:200] if is_query else oracle_source, "Is Query": is_query})
            return None

    def _get_oracle_column_stats(self, oracle_conn, oracle_columns: List[Dict],
                                  oracle_source: str, is_query: bool) -> List[Dict]:
        """Collect per-column stats from Oracle.
        Non-LOB columns: distinct_count + null_count.
        LOB columns: null_count only (distinct not supported on LOBs).
        Stored with matched_name (transformed if applicable) as the lookup key for Script 2.
        """
        non_lob_cols = []
        lob_cols = []
        skipped_lobs_display = []

        for col in oracle_columns:
            col_type = col.get('type', '').upper()
            if any(lob in col_type for lob in self.LOB_TYPES):
                lob_cols.append(col)
                skipped_lobs_display.append(f"{col['name']} ({col_type})")
            else:
                non_lob_cols.append(col)

        if skipped_lobs_display:
            print_safe(f"     ℹ️  LOB columns (null counts only): {', '.join(skipped_lobs_display[:3])}")
            if len(skipped_lobs_display) > 3:
                print_safe(f"        ... and {len(skipped_lobs_display) - 3} more")

        column_stats = []

        # --- Non-LOB columns: distinct + null ---
        if non_lob_cols:
            stat_parts = []
            for col in non_lob_cols:
                col_ref = f'"{col["name"]}"'
                if self.field_name_transformation:
                    matched_name = transform_column_name(col['name'], self.transformation_rules).upper()
                else:
                    matched_name = col['name'].upper()
                safe_alias = re.sub(r'[^A-Za-z0-9_]', '_', matched_name)
                stat_parts.append(f'COUNT(DISTINCT {col_ref}) as {safe_alias}_DISTINCT')
                stat_parts.append(f'SUM(CASE WHEN {col_ref} IS NULL THEN 1 ELSE 0 END) as {safe_alias}_NULLS')

            if is_query:
                query = f"SELECT /*+ PARALLEL(8) */ {', '.join(stat_parts)} FROM ({oracle_source})"
            else:
                query = f'SELECT /*+ PARALLEL(8) */ {", ".join(stat_parts)} FROM "{oracle_conn.schema}"."{oracle_source}"'

            try:
                cursor = oracle_conn.conn.cursor()
                cursor.execute(query)
                result = cursor.fetchone()
                cursor.close()
                if result:
                    for i, col in enumerate(non_lob_cols):
                        if self.field_name_transformation:
                            matched_name = transform_column_name(col['name'], self.transformation_rules).upper()
                        else:
                            matched_name = col['name'].upper()
                        distinct_val = result[i * 2] if result[i * 2] is not None else 0
                        null_val = result[i * 2 + 1] if result[i * 2 + 1] is not None else 0
                        column_stats.append({
                            'column_name': matched_name,
                            'oracle_original_name': col['name'],
                            'oracle_type': col.get('type', 'UNKNOWN'),
                            'is_lob': False,
                            'distinct_count': distinct_val,
                            'null_count': null_val
                        })
            except Exception as e:
                log_detailed_error("Oracle - Failed to get non-LOB column statistics", e,
                                   {"Source": oracle_source[:200] if is_query else oracle_source})

        # --- LOB columns: null count only ---
        if lob_cols:
            lob_parts = []
            for col in lob_cols:
                col_ref = f'"{col["name"]}"'
                if self.field_name_transformation:
                    matched_name = transform_column_name(col['name'], self.transformation_rules).upper()
                else:
                    matched_name = col['name'].upper()
                safe_alias = re.sub(r'[^A-Za-z0-9_]', '_', matched_name)
                lob_parts.append(f'SUM(CASE WHEN {col_ref} IS NULL THEN 1 ELSE 0 END) as {safe_alias}_NULLS')

            if is_query:
                lob_query = f"SELECT /*+ PARALLEL(8) */ {', '.join(lob_parts)} FROM ({oracle_source})"
            else:
                lob_query = f'SELECT /*+ PARALLEL(8) */ {", ".join(lob_parts)} FROM "{oracle_conn.schema}"."{oracle_source}"'

            try:
                cursor = oracle_conn.conn.cursor()
                cursor.execute(lob_query)
                result = cursor.fetchone()
                cursor.close()
                if result:
                    for i, col in enumerate(lob_cols):
                        if self.field_name_transformation:
                            matched_name = transform_column_name(col['name'], self.transformation_rules).upper()
                        else:
                            matched_name = col['name'].upper()
                        null_val = result[i] if result[i] is not None else 0
                        column_stats.append({
                            'column_name': matched_name,
                            'oracle_original_name': col['name'],
                            'oracle_type': col.get('type', 'UNKNOWN'),
                            'is_lob': True,
                            'distinct_count': None,
                            'null_count': null_val
                        })
            except Exception as e:
                log_detailed_error("Oracle - Failed to get LOB column null statistics", e,
                                   {"Source": oracle_source[:200] if is_query else oracle_source})

        return column_stats

    def _get_oracle_duplicate_count(self, oracle_conn, oracle_columns: List[Dict],
                                     oracle_source: str, is_query: bool) -> Optional[int]:
        """Count duplicate rows using all non-LOB Oracle columns"""
        try:
            filtered = [col for col in oracle_columns
                        if not any(lob in col.get('type', '').upper() for lob in self.LOB_TYPES)]
            if not filtered:
                return 0
            col_list = ', '.join([f'"{col["name"]}"' for col in filtered])
            if is_query:
                query = f"""
                    SELECT /*+ PARALLEL(8) */ COUNT(*) FROM (
                        SELECT {col_list}, COUNT(*) as cnt
                        FROM ({oracle_source})
                        GROUP BY {col_list}
                        HAVING COUNT(*) > 1
                    ) duplicates
                """
            else:
                query = f"""
                    SELECT /*+ PARALLEL(8) */ COUNT(*) FROM (
                        SELECT {col_list}, COUNT(*) as cnt
                        FROM "{oracle_conn.schema}"."{oracle_source}"
                        GROUP BY {col_list}
                        HAVING COUNT(*) > 1
                    ) duplicates
                """
            cursor = oracle_conn.conn.cursor()
            cursor.execute(query)
            result = cursor.fetchone()[0]
            cursor.close()
            return result if result is not None else 0
        except Exception as e:
            log_detailed_error("Oracle - Failed to get duplicate count", e,
                               {"Source": oracle_source[:200] if is_query else oracle_source})
            return None

    def _collect_single_table(self, table: str) -> Dict:
        """Collect all Oracle data for a single table"""
        start_time = time.time()

        oracle_conn = self.oracle_pool.get_connection()
        if not oracle_conn:
            error_msg = 'Failed to get Oracle connection from pool'
            log_detailed_error("Source Collection Failed - Oracle Connection",
                               Exception(error_msg), {"Table": table})
            return {
                'oracle_table': table, 'snowflake_table': None,
                'status': 'ERROR', 'error': error_msg,
                'collection_time': round(time.time() - start_time, 2),
                'Last_Checked': datetime.now(EASTERN).isoformat()
            }

        is_query = (self.format_type == 'query_based')
        if is_query:
            oracle_source = self.sql_query
            sf_table = self.sf_target_table
            oracle_table_display = self.oracle_table_from_query or sf_table
        else:
            oracle_source = table
            oracle_table_display = table
            if table in self.table_mapping:
                sf_table = self.table_mapping[table]
            elif self.use_prefix:
                sf_table = f"{self.sf_table_prefix}{table}"
            else:
                sf_table = table

        try:
            # Get Oracle columns
            if is_query:
                oracle_columns = oracle_conn.get_columns_from_query(oracle_source)
            else:
                oracle_columns = oracle_conn.get_columns(oracle_source)

            oracle_columns = self._filter_excluded_columns(oracle_columns)

            if not oracle_columns:
                raise Exception('Unable to retrieve columns from Oracle source')

            # Collect all Oracle-side metrics
            oracle_row_count = self._get_oracle_row_count(oracle_conn, oracle_source, is_query)
            oracle_column_stats = self._get_oracle_column_stats(oracle_conn, oracle_columns, oracle_source, is_query)
            oracle_duplicate_count = self._get_oracle_duplicate_count(oracle_conn, oracle_columns, oracle_source, is_query)

            if not is_query:
                oracle_constraints = oracle_conn.get_constraints(oracle_source)
            else:
                oracle_constraints = {
                    'primary_keys': [], 'unique_constraints': [],
                    'check_constraints': [], 'not_null_columns': [], 'foreign_keys': []
                }

            self.oracle_pool.return_connection(oracle_conn)

            return {
                'oracle_table': oracle_table_display,
                'snowflake_table': sf_table,
                'status': 'COLLECTED',
                'error': None,
                'oracle_columns': oracle_columns,
                'oracle_row_count': oracle_row_count,
                'oracle_column_stats': oracle_column_stats,
                'oracle_duplicate_count': oracle_duplicate_count,
                'oracle_constraints': oracle_constraints,
                'collection_time': round(time.time() - start_time, 2),
                'Last_Checked': datetime.now(EASTERN).isoformat()
            }

        except Exception as e:
            log_detailed_error(f"Source Collection Failed - {oracle_table_display}", e,
                               {"Table": oracle_table_display, "Snowflake Table": sf_table})
            self.oracle_pool.return_connection(oracle_conn)
            return {
                'oracle_table': oracle_table_display,
                'snowflake_table': sf_table,
                'status': 'ERROR',
                'error': str(e),
                'collection_time': round(time.time() - start_time, 2),
                'Last_Checked': datetime.now(EASTERN).isoformat()
            }

    def _collect_single_table_with_timeout(self, table: str, timeout: int) -> Dict:
        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(self._collect_single_table, table)
        try:
            result = future.result(timeout=timeout)
            executor.shutdown(wait=True)
            return result
        except TimeoutError:
            print_safe(f"       ⚠️  Timeout detected, abandoning collection thread for {table}...")
            self.has_abandoned_threads = True
            executor.shutdown(wait=False)
            is_query = (self.format_type == 'query_based')
            if is_query:
                table_name = self.oracle_table_from_query or self.sf_target_table
                sf_table = self.sf_target_table
            else:
                table_name = table
                if table in self.table_mapping:
                    sf_table = self.table_mapping[table]
                elif self.use_prefix:
                    sf_table = f"{self.sf_table_prefix}{table}"
                else:
                    sf_table = table
            error_msg = f'Collection exceeded {timeout}s timeout'
            log_detailed_error(f"Source Collection Timeout - {table_name}", Exception(error_msg),
                               {"Source Table": table_name, "Snowflake Table": sf_table, "Timeout": f"{timeout}s"})
            return {
                'oracle_table': table_name, 'snowflake_table': sf_table,
                'status': 'TIMEOUT', 'error': error_msg,
                'collection_time': timeout, 'Last_Checked': datetime.now(EASTERN).isoformat()
            }
        except Exception as e:
            executor.shutdown(wait=True)
            raise

    def collect_all_tables(self):
        """Collect all tables with parallel execution and timeout handling"""
        start_time = time.time()
        timeout_tables = []

        if self.format_type == 'query_based':
            print_safe(f"\n  ⏳ Collecting source data for query-based pipeline...")
            result = self._collect_single_table_with_timeout("QUERY_RESULT", timeout=self.table_timeout)
            if result['status'] == 'TIMEOUT':
                print_safe(f"    ⏱️  Collection timed out after {self.table_timeout}s")
                if self.max_retries > 0:
                    print_safe(f"    🔁 Retrying (max {self.max_retries} attempts)...")
                    for retry_attempt in range(1, self.max_retries + 1):
                        print_safe(f"    🔁 Retry attempt {retry_attempt}/{self.max_retries}...")
                        result = self._collect_single_table_with_timeout("QUERY_RESULT", timeout=self.table_timeout)
                        if result['status'] != 'TIMEOUT':
                            status_icon = "✅" if result['status'] == 'COLLECTED' else "❌"
                            print_safe(f"       {status_icon} Query collection - {result['status']}")
                            break
                    if result['status'] == 'TIMEOUT':
                        result['status'] = 'FAULTY'
                        result['error'] = f'Collection consistently exceeded {self.table_timeout}s timeout after {self.max_retries} retries'
                        with self.results_lock:
                            self.results['faulty_tables'].append({
                                'table_name': result.get('oracle_table', self.sf_target_table),
                                'snowflake_table': result.get('snowflake_table', self.sf_target_table),
                                'reason': f'Collection exceeded {self.table_timeout}s timeout',
                                'retry_attempts': self.max_retries
                            })
            else:
                status_icon = "✅" if result['status'] == 'COLLECTED' else "❌"
                print_safe(f"    {status_icon} Query collection - {result['status']}")
            with self.results_lock:
                self.results['table_data'].append(result)
        else:
            print_safe(f"\n  ⏳ Collecting source data for {len(self.tables)} table(s)...")
            if self.parallel_execution and len(self.tables) > 1:
                with ThreadPoolExecutor(max_workers=self.max_parallel_tables) as executor:
                    future_to_table = {
                        executor.submit(self._collect_single_table_with_timeout, table,
                                        timeout=self.table_timeout): (idx, table)
                        for idx, table in enumerate(self.tables, 1)
                    }
                    for future in as_completed(future_to_table):
                        idx, table = future_to_table[future]
                        result = future.result()
                        if result['status'] == 'TIMEOUT':
                            timeout_tables.append(table)
                            print_safe(f"    ⏱️  [{idx}/{len(self.tables)}] {table} - Timed out after {self.table_timeout}s")
                        else:
                            status_icon = "✅" if result['status'] == 'COLLECTED' else "❌"
                            print_safe(f"    {status_icon} [{idx}/{len(self.tables)}] {result.get('oracle_table', 'Unknown')} - {result['status']}")
                        with self.results_lock:
                            self.results['table_data'].append(result)
            else:
                for idx, table in enumerate(self.tables, 1):
                    result = self._collect_single_table_with_timeout(table, timeout=self.table_timeout)
                    if result['status'] == 'TIMEOUT':
                        timeout_tables.append(table)
                        print_safe(f"    ⏱️  [{idx}/{len(self.tables)}] {table} - Timed out after {self.table_timeout}s")
                    else:
                        status_icon = "✅" if result['status'] == 'COLLECTED' else "❌"
                        print_safe(f"    {status_icon} [{idx}/{len(self.tables)}] {result.get('oracle_table', 'Unknown')} - {result['status']}")
                    with self.results_lock:
                        self.results['table_data'].append(result)

            if timeout_tables:
                print_safe(f"\n    🔁 Retrying {len(timeout_tables)} timeout tables (max {self.max_retries} attempts)...")
                for retry_attempt in range(1, self.max_retries + 1):
                    if not timeout_tables:
                        break
                    print_safe(f"    🔁 Retry attempt {retry_attempt}/{self.max_retries}...")
                    still_timeout = []
                    for table in timeout_tables:
                        result = self._collect_single_table_with_timeout(table, timeout=self.table_timeout)
                        if result['status'] == 'TIMEOUT':
                            still_timeout.append(table)
                            print_safe(f"       ⏱️  {table} - Still timing out...")
                        else:
                            status_icon = "✅" if result['status'] == 'COLLECTED' else "❌"
                            print_safe(f"       {status_icon} {table} - {result['status']}")
                            with self.results_lock:
                                self.results['table_data'] = [
                                    r for r in self.results['table_data']
                                    if r.get('oracle_table') != result.get('oracle_table')
                                ]
                                self.results['table_data'].append(result)
                    timeout_tables = still_timeout

                if timeout_tables:
                    print_safe(f"    ⚠️  {len(timeout_tables)} tables marked as FAULTY after {self.max_retries} retries")
                    with self.results_lock:
                        for table in timeout_tables:
                            snowflake_table = None
                            for r in self.results['table_data']:
                                if r.get('oracle_table') == table and r['status'] == 'TIMEOUT':
                                    r['status'] = 'FAULTY'
                                    r['error'] = f'Collection consistently exceeded {self.table_timeout}s timeout after {self.max_retries} retry attempts'
                                    snowflake_table = r.get('snowflake_table')
                            faulty_entry = {
                                'table_name': table,
                                'reason': f'Collection exceeded {self.table_timeout}s timeout',
                                'retry_attempts': self.max_retries
                            }
                            if snowflake_table:
                                faulty_entry['snowflake_table'] = snowflake_table
                            self.results['faulty_tables'].append(faulty_entry)

        total_time = time.time() - start_time
        collected = sum(1 for r in self.results['table_data'] if r['status'] == 'COLLECTED')
        errors = sum(1 for r in self.results['table_data'] if r['status'] == 'ERROR')
        faulty = sum(1 for r in self.results['table_data'] if r['status'] == 'FAULTY')
        self.results['summary'] = {
            'total_tables': len(self.results['table_data']),
            'collected': collected,
            'errors': errors,
            'faulty': faulty,
            'total_collection_time': round(total_time, 2)
        }
        print_safe(f"    ⏱️  Completed in {self.results['summary']['total_collection_time']}s | "
                   f"✅ {collected} collected | ❌ {errors} errors | 🔴 {faulty} faulty")

    def get_results(self) -> Dict:
        return self.results


def collect_single_pipeline_source(pipeline_data: Dict, config: Dict, pipeline_index: int) -> Dict:
    """Collect Oracle source data for a single pipeline"""
    pipeline_name = pipeline_data.get('pipeline_name', f'Pipeline_{pipeline_index}')
    pipeline_number = pipeline_data.get('pipeline_number', pipeline_index)

    print_header(f"PIPELINE {pipeline_number}: {pipeline_name}", "=", 80)

    try:
        config_loader = ConfigLoader(pipeline_data)
        format_type = config_loader.get_format_type()
        use_prefix = True
        table_mapping = {}

        field_name_transformation = config_loader.has_field_name_transformation()
        transformation_rules = config_loader.get_transformation_rules()

        parallel_execution = get_optional_config_value(config, 'validation', 'parallel_execution', True)
        max_parallel_tables = get_optional_config_value(config, 'validation', 'max_parallel_tables', 5)
        connection_pool_size = get_optional_config_value(config, 'validation', 'connection_pool_size',
                                                         max_parallel_tables + 2)
        table_timeout = get_optional_config_value(config, 'validation', 'table_timeout', 60)
        max_retries = get_optional_config_value(config, 'validation', 'max_timeout_retries', 3)

        if format_type == 'query_based':
            sql_query = config_loader.get_sql_query()
            sf_target_table = config_loader.get_snowflake_target_table()
            oracle_schema = config_loader.get_oracle_schema()
            oracle_table_from_query = config_loader.get_oracle_table_from_query()
            sf_cfg = config_loader.get_snowflake_config()
            sf_database = sf_cfg.get('database')
            sf_schema = sf_cfg.get('schema')
            if not sql_query or not sf_target_table or not oracle_schema:
                error_msg = 'Missing query-based configuration (sql_query, sf_target_table, or oracle_schema)'
                log_detailed_error(f"Pipeline {pipeline_number} Configuration Error", Exception(error_msg),
                                   {"Pipeline": pipeline_name})
                return {'pipeline_name': pipeline_name, 'pipeline_number': pipeline_number,
                        'status': 'ERROR', 'error': error_msg, 'source_data': None}
            tables = []
            table_prefix_from_report = ""
            use_prefix = False
        else:
            sf_cfg = config_loader.get_snowflake_config()
            tables = config_loader.get_table_list()
            oracle_schema = config_loader.get_oracle_schema()
            sf_schema_from_report, table_prefix_from_report = config_loader.get_snowflake_schema_and_prefix()
            sf_database_from_report = sf_cfg.get('database')
            sf_database = sf_database_from_report
            sf_schema = sf_schema_from_report
            if not oracle_schema or not sf_schema or not sf_database:
                error_msg = 'Missing table-list configuration (oracle_schema, sf_schema, or sf_database)'
                log_detailed_error(f"Pipeline {pipeline_number} Configuration Error", Exception(error_msg),
                                   {"Pipeline": pipeline_name})
                return {'pipeline_name': pipeline_name, 'pipeline_number': pipeline_number,
                        'status': 'ERROR', 'error': error_msg, 'source_data': None}
            sql_query = None
            sf_target_table = None
            oracle_table_from_query = None

        oracle_user = get_config_value(config, 'oracle', 'username')
        oracle_password = get_config_value(config, 'oracle', 'password')
        oracle_dsn = get_config_value(config, 'oracle', 'dsn')

        excluded_columns = []
        if config and 'validation' in config and 'excluded_columns' in config['validation']:
            excluded_cols = config['validation']['excluded_columns']
            if isinstance(excluded_cols, list):
                excluded_columns = [col.strip().upper() for col in excluded_cols if col.strip()]
            elif isinstance(excluded_cols, str):
                excluded_columns = [col.strip().upper() for col in excluded_cols.split(',') if col.strip()]

        print_safe(f"  📊 Configuration:")
        print_safe(f"     Oracle Schema: {oracle_schema}")
        print_safe(f"     Snowflake Target: {sf_database}.{sf_schema}")
        print_safe(f"     Format: {format_type}")
        print_safe(f"     Tables: {len(tables) if tables else 'N/A (query-based)'}")
        print_safe(f"     Timeout: {table_timeout}s per table")
        if field_name_transformation:
            print_safe(f"     Column Name Transformation: ENABLED")

        oracle_pool = ConnectionPool(
            OracleConnector,
            {'user': oracle_user, 'password': oracle_password, 'dsn': oracle_dsn, 'schema': oracle_schema},
            max_connections=connection_pool_size
        )

        try:
            print_safe(f"  🔌 Testing Oracle connection...")
            oracle_test = oracle_pool.get_connection()
            if not oracle_test:
                return {'pipeline_name': pipeline_name, 'pipeline_number': pipeline_number,
                        'status': 'ERROR', 'error': 'Failed to establish Oracle connection', 'source_data': None}
            oracle_pool.return_connection(oracle_test)
            print_safe(f"  ✅ Oracle connection established")

            if format_type == 'table_list':
                should_fetch_all_tables = (not tables and getattr(config_loader, 'table_pattern', '') == '%')
                if should_fetch_all_tables:
                    print_safe(f"  🔍 Fetching all tables from Oracle...")
                    oracle_conn = oracle_pool.get_connection()
                    tables = oracle_conn.get_all_tables()
                    oracle_pool.return_connection(oracle_conn)
                    if not tables:
                        return {'pipeline_name': pipeline_name, 'pipeline_number': pipeline_number,
                                'status': 'ERROR', 'error': 'No tables found in Oracle schema', 'source_data': None}
                    use_prefix = True
                else:
                    use_prefix = config_loader.should_use_prefix()

                has_sf_list = config_loader.has_explicit_snowflake_list()
                if has_sf_list and not use_prefix:
                    table_mapping = config_loader.create_table_mapping(tables)
                    if table_mapping:
                        print_safe(f"  🗺️  Mapped {len(table_mapping)}/{len(tables)} tables")

                if tables and config and 'validation' in config:
                    validate_all = config['validation'].get('validate_all_tables', True)
                    if not validate_all and 'max_tables_to_validate' in config['validation']:
                        max_tables = config['validation']['max_tables_to_validate']
                        if max_tables and isinstance(max_tables, int) and max_tables > 0:
                            tables = tables[:max_tables]

            collector = SourceDataCollector(
                oracle_pool, tables,
                table_prefix_from_report if format_type == 'table_list' else "",
                excluded_columns, format_type=format_type,
                sql_query=sql_query, sf_target_table=sf_target_table,
                oracle_table_from_query=oracle_table_from_query,
                use_prefix=use_prefix, table_mapping=table_mapping,
                parallel_execution=parallel_execution,
                max_parallel_tables=max_parallel_tables,
                table_timeout=table_timeout, max_retries=max_retries,
                field_name_transformation=field_name_transformation,
                transformation_rules=transformation_rules
            )

            collector.collect_all_tables()
            source_data = collector.get_results()
            print_safe(f"  ✅ Pipeline source collection completed")

            return {
                'pipeline_name': pipeline_name,
                'pipeline_number': pipeline_number,
                'status': 'SUCCESS',
                'oracle_schema': oracle_schema,
                'snowflake_database': sf_database,
                'snowflake_schema': sf_schema,
                'format_type': format_type,
                'sql_query': sql_query,
                'sf_target_table': sf_target_table,
                'oracle_table_from_query': oracle_table_from_query,
                'table_prefix': table_prefix_from_report if format_type == 'table_list' else "",
                'use_prefix': use_prefix,
                'table_mapping': table_mapping,
                'excluded_columns': excluded_columns,
                'field_name_transformation': field_name_transformation,
                'transformation_rules': transformation_rules,
                'source_data': source_data
            }

        finally:
            print_safe(f"  🧹 Cleaning up Oracle connection pool...")
            skip_disconnect = False
            try:
                if 'collector' in locals() and hasattr(collector, 'has_abandoned_threads'):
                    skip_disconnect = collector.has_abandoned_threads
                    if skip_disconnect:
                        print_safe(f"  ⚠️  Abandoned threads detected - skipping disconnect")
            except:
                pass
            try:
                oracle_pool.close_all(force_skip_disconnect=skip_disconnect)
            except Exception as e:
                print_safe(f"  ⚠️  Error closing Oracle pool: {e}")

    except Exception as e:
        error_details = tb.format_exc()
        log_detailed_error(f"Pipeline {pipeline_number} Source Collection Failed", e,
                           {"Pipeline": pipeline_name})
        return {
            'pipeline_name': pipeline_name, 'pipeline_number': pipeline_number,
            'status': 'ERROR', 'error': str(e), 'error_details': error_details, 'source_data': None
        }


def main():
    parser = argparse.ArgumentParser(description='Oracle Source Data Collector - Step 1 of 2')
    parser.add_argument('--config', '-c', help='Config JSON file', default="validation_config.json")
    parser.add_argument('--report', '-r', help='StreamSets report JSON file', default=None)
    parser.add_argument('--output', '-o', help='Output snapshot file path', default=None)
    args = parser.parse_args()

    print_header("Oracle Source Data Collection (Step 1 of 2)", "=", 80)

    config = load_config_file(args.config)

    if args.report:
        report_path = args.report
    elif 'validation' in config and 'report_path' in config['validation']:
        report_path = config['validation']['report_path']
    else:
        print_safe("❌ Report path not specified")
        sys.exit(1)

    if args.output:
        snapshot_path = args.output
    elif 'validation' in config and 'snapshot_path' in config['validation']:
        snapshot_path = config['validation']['snapshot_path']
    else:
        snapshot_path = 'source_snapshot.json'

    pipelines = load_pipeline_report(report_path)
    print_safe(f"\n🚀 Starting source data collection for {len(pipelines)} pipeline(s)...")
    print_safe(f"📁 Report: {report_path}")
    print_safe(f"💾 Snapshot will be saved to: {snapshot_path}")

    all_results = []
    overall_start_time = time.time()

    with ThreadPoolExecutor(max_workers=min(len(pipelines), 3)) as executor:
        future_to_pipeline = {
            executor.submit(collect_single_pipeline_source, pipeline, config, i + 1): (i, pipeline)
            for i, pipeline in enumerate(pipelines)
        }
        completed_pipelines = 0
        for future in as_completed(future_to_pipeline):
            result = future.result()
            all_results.append(result)
            completed_pipelines += 1
            status_icon = "✅" if result['status'] == 'SUCCESS' else "❌"
            print_safe(f"\n{status_icon} Completed pipeline {completed_pipelines}/{len(pipelines)}: {result['pipeline_name']}")

    overall_time = time.time() - overall_start_time
    all_results.sort(key=lambda x: x.get('pipeline_number', 0))

    snapshot = {
        'snapshot_metadata': {
            'generated_timestamp': datetime.now(EASTERN).isoformat(),
            'source_report': report_path,
            'total_pipelines': len(pipelines),
            'total_collection_time': round(overall_time, 2)
        },
        'pipelines': all_results
    }

    try:
        with open(snapshot_path, 'w') as f:
            json.dump(snapshot, f, indent=2)
        print_safe(f"\n✅ Source snapshot saved to: {snapshot_path}")
    except Exception as e:
        print_safe(f"\n❌ Failed to save snapshot: {e}")
        sys.exit(1)

    print_header("SOURCE COLLECTION SUMMARY", "=", 80)
    successful = sum(1 for r in all_results if r['status'] == 'SUCCESS')
    failed = sum(1 for r in all_results if r['status'] == 'ERROR')
    print_safe(f"\n📋 PIPELINES: Total={len(pipelines)} | ✅ Successful={successful} | ❌ Failed={failed}")
    print_safe(f"⏱️  COLLECTION TIME: {round(overall_time, 2)}s")
    print_safe(f"💾 Snapshot saved to: {snapshot_path}")
    print_safe("=" * 80)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print_safe("\n⚠️  Interrupted by user")
        sys.exit(0)
    except Exception as e:
        print_safe(f"\n❌ Error: {e}")
        tb.print_exc()
        sys.exit(1)