import snowflake.connector
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
    print_safe(f"\n{char * width}")
    print_safe(f"{text.center(width)}")
    print_safe(f"{char * width}")


def log_detailed_error(context: str, error: Exception, additional_info: Dict = None):
    print_safe(f"\n{'=' * 80}")
    print_safe(f"❌ ERROR: {context}")
    print_safe(f"{'=' * 80}")
    print_safe(f"Error: {str(error)}")
    if additional_info:
        print_safe(f"\nDetails:")
        for key, value in additional_info.items():
            print_safe(f"  • {key}: {value}")
    print_safe(f"{'=' * 80}\n")


def transform_column_name(column_name: str, transformations: List[Tuple[str, str]] = None) -> str:
    if transformations is None:
        transformations = [('[^a-zA-Z0-9]', '_'), ('_+', '_')]
    transformed = column_name
    for pattern, replacement in transformations:
        try:
            transformed = re.sub(pattern, replacement, transformed)
        except Exception as e:
            print_safe(f"⚠️  Regex error - pattern: {pattern}, replacement: {replacement}, error: {e}")
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


def load_snowflake_config(config: dict) -> dict:
    if 'snowflake' not in config:
        raise ValueError("Snowflake configuration not found in config file")
    sf_config = config['snowflake']
    required_fields = ['account', 'user', 'private_key_path', 'private_key_password', 'role', 'warehouse']
    for field in required_fields:
        if field not in sf_config:
            raise ValueError(f"Required Snowflake config field missing: {field}")
    return {
        'account': sf_config['account'],
        'user': sf_config['user'],
        'private_key_path': sf_config['private_key_path'],
        'private_key_password': sf_config['private_key_password'],
        'role': sf_config['role'],
        'warehouse': sf_config['warehouse'],
        'database': sf_config.get('database')
    }


def load_snowflake_save_config(config: dict) -> dict:
    if 'snowflake_save' not in config:
        raise ValueError("Snowflake Save configuration not found in config file")
    sf_config = config['snowflake_save']
    required_fields = ['account', 'user', 'private_key_path', 'private_key_password',
                       'role', 'warehouse', 'database', 'stg_schema', 'core_schema']
    for field in required_fields:
        if field not in sf_config:
            raise ValueError(f"Required Snowflake Save config field missing: {field}")
    return {
        'account': sf_config['account'],
        'user': sf_config['user'],
        'private_key_path': sf_config['private_key_path'],
        'private_key_password': sf_config['private_key_password'],
        'role': sf_config['role'],
        'warehouse': sf_config['warehouse'],
        'database': sf_config['database'],
        'stg_schema': sf_config['stg_schema'],
        'core_schema': sf_config['core_schema']
    }


def get_private_key(path: str, password: str):
    with open(path, "rb") as key_file:
        p_key = serialization.load_pem_private_key(
            key_file.read(),
            password=password.encode(),
            backend=default_backend()
        )
    return p_key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()
    )


def create_snowflake_connection(sf_config: dict):
    try:
        private_key = get_private_key(sf_config['private_key_path'], sf_config['private_key_password'])
        conn_params = {
            'user': sf_config['user'],
            'account': sf_config['account'],
            'private_key': private_key,
            'role': sf_config['role'],
            'warehouse': sf_config['warehouse']
        }
        if sf_config.get('database'):
            conn_params['database'] = sf_config['database']
        return snowflake.connector.connect(**conn_params)
    except FileNotFoundError as e:
        print_safe(f"❌ Private key file not found: {e}")
        raise
    except Exception as e:
        print_safe(f"❌ Snowflake connection failed: {e}")
        raise


def create_schemas_if_not_exist(conn, stg_schema: str, core_schema: str):
    try:
        cur = conn.cursor()
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {stg_schema}")
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {core_schema}")
        cur.close()
    except Exception as e:
        print_safe(f"❌ Failed to create schemas: {e}")
        raise


def create_core_table_if_not_exists(conn, core_schema: str):
    try:
        cur = conn.cursor()
        table_name = f"{core_schema}.DQ_IT_VALIDATION_REPORT"
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {table_name} (
                report_data VARIANT NOT NULL,
                run_timestamp TIMESTAMP_NTZ NOT NULL,
                load_date TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP(),
                database VARCHAR(255)
            )
        """)
        cur.close()
    except Exception as e:
        print_safe(f"❌ Failed to create CORE table: {e}")
        raise


def save_report_to_snowflake(consolidated_report: dict, sf_config: dict, database_name: str = None):
    """
    Save validation report to Snowflake with STG and CORE pattern.
    STG: Creates new timestamped table for each run (historical archive).
    CORE: Keeps the last 10 runs per database (rolling window).
          On the 11th run the oldest row is automatically removed.
          Only updated when all pipelines pass.
    All timestamps stored in Eastern Time (EST/EDT).
    """
    conn = None
    try:
        conn = create_snowflake_connection(sf_config)
        create_schemas_if_not_exist(conn, sf_config['stg_schema'], sf_config['core_schema'])
        create_core_table_if_not_exists(conn, sf_config['core_schema'])

        timestamp = datetime.now(EASTERN).strftime('%Y%m%d_%H%M%S')
        stg_table_name = f"{sf_config['stg_schema']}.DQ_IT_VALIDATION_REPORT_{timestamp}"
        run_timestamp = consolidated_report.get('report_metadata', {}).get('generated_timestamp')

        # load_date — always Eastern time, passed explicitly so Snowflake never
        # falls back to CURRENT_TIMESTAMP() which defaults to UTC
        load_date_eastern = datetime.now(EASTERN).strftime('%Y-%m-%d %H:%M:%S.%f')

        # --- SAVE TO STG (Always) ---
        cur = conn.cursor()
        cur.execute(f"""
            CREATE TABLE {stg_table_name} (
                report_data VARIANT NOT NULL,
                run_timestamp TIMESTAMP_NTZ NOT NULL,
                load_date TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP(),
                database VARCHAR(255)
            )
        """)
        report_json = json.dumps(consolidated_report, indent=2)
        # load_date supplied explicitly in Eastern time
        cur.execute(f"""
            INSERT INTO {stg_table_name} (report_data, run_timestamp, load_date, database)
            SELECT PARSE_JSON(%s), TO_TIMESTAMP_NTZ(%s), TO_TIMESTAMP_NTZ(%s), %s
        """, (report_json, run_timestamp, load_date_eastern, database_name))
        print_safe(f"✅ Snowflake: Saved to {stg_table_name}")

        # --- UPDATE CORE (Only if validation passed) ---
        failed_pipelines = consolidated_report.get('overall_summary', {}).get('failed_pipelines', 0)
        if failed_pipelines == 0:
            core_table_name = f"{sf_config['core_schema']}.DQ_IT_VALIDATION_REPORT"

            # Step 1 — Insert the new run with explicit Eastern load_date
            cur.execute(f"""
                INSERT INTO {core_table_name} (report_data, run_timestamp, load_date, database)
                SELECT PARSE_JSON(%s), TO_TIMESTAMP_NTZ(%s), TO_TIMESTAMP_NTZ(%s), %s
            """, (report_json, run_timestamp, load_date_eastern, database_name))

            # Step 2 — Trim to the last 10 runs for this database.
            # Any row whose load_date ranks 11th or older (by load_date DESC) is deleted.
            cur.execute(f"""
                DELETE FROM {core_table_name}
                WHERE database = %s
                AND load_date NOT IN (
                    SELECT load_date
                    FROM (
                        SELECT load_date,
                               ROW_NUMBER() OVER (ORDER BY load_date DESC) AS rn
                        FROM {core_table_name}
                        WHERE database = %s
                    ) ranked
                    WHERE rn <= 10
                )
            """, (database_name, database_name))

            # Report how many rows now exist for this database
            cur.execute(
                f"SELECT COUNT(*) FROM {core_table_name} WHERE database = %s",
                (database_name,)
            )
            row_count = cur.fetchone()[0]
            print_safe(f"✅ Snowflake: Updated {core_table_name} ({row_count}/10 runs stored for '{database_name}')")
        else:
            print_safe(f"⚠️  Snowflake: CORE not updated (validation had {failed_pipelines} failed pipeline(s))")

        cur.close()
    except Exception as e:
        print_safe(f"❌ Snowflake save failed: {e}")
        raise
    finally:
        if conn:
            conn.close()


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


class MultiRoleSnowflakePoolManager:
    """Manages multiple Snowflake connection pools for different roles with automatic fallback"""

    def __init__(self, account: str, user: str, private_key_path: str, private_key_password: str,
                 roles: List[str], warehouse: str, database: str, schema: str, max_connections: int = 5):
        self.roles = roles
        self.pools = {}
        self.lock = Lock()
        for role in roles:
            self.pools[role] = ConnectionPool(
                SnowflakeConnector,
                {
                    'account': account, 'user': user,
                    'private_key_path': private_key_path,
                    'private_key_password': private_key_password,
                    'role': role, 'warehouse': warehouse,
                    'database': database, 'schema': schema
                },
                max_connections=max_connections
            )

    def get_connection(self, role: str = None):
        if role is None:
            role = self.roles[0]
        return self.pools[role].get_connection()

    def return_connection(self, conn, role: str = None):
        if role is None:
            role = self.roles[0]
        self.pools[role].return_connection(conn)

    def get_all_roles(self) -> List[str]:
        return self.roles

    def close_all(self, force_skip_disconnect=False):
        for pool in self.pools.values():
            pool.close_all(force_skip_disconnect=force_skip_disconnect)


def is_access_error(error_message: str) -> bool:
    error_lower = str(error_message).lower()
    access_patterns = [
        'does not exist', 'not authorized', 'insufficient privileges',
        'access denied', 'permission denied', 'authorization error',
        'not found', 'object does not exist', 'table or view does not exist'
    ]
    return any(pattern in error_lower for pattern in access_patterns)


class SnowflakeConnector:
    """Snowflake database connector with connection pooling support"""

    def __init__(self, account: str, user: str, private_key_path: str, private_key_password: str,
                 role: str, warehouse: str, database: str, schema: str):
        self.account = account
        self.user = user
        self.private_key_path = private_key_path
        self.private_key_password = private_key_password
        self.role = role
        self.warehouse = warehouse
        self.database = database
        self.schema = schema
        self.conn = None
        self.last_activity = None

    def connect(self):
        try:
            with open(self.private_key_path, 'rb') as key_file:
                p_key = serialization.load_pem_private_key(
                    key_file.read(),
                    password=self.private_key_password.encode(),
                    backend=default_backend()
                )
            pkb = p_key.private_bytes(
                encoding=serialization.Encoding.DER,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption()
            )
            self.conn = snowflake.connector.connect(
                user=self.user, account=self.account, private_key=pkb,
                warehouse=self.warehouse, database=self.database,
                schema=self.schema, role=self.role
            )
            self.last_activity = time.time()
            return True
        except Exception as e:
            log_detailed_error(
                "Snowflake Connection Failed", e,
                {"Account": self.account, "User": self.user, "Role": self.role,
                 "Warehouse": self.warehouse, "Database": self.database,
                 "Schema": self.schema, "Private Key Path": self.private_key_path}
            )
            return False

    def is_alive(self) -> bool:
        if not self.conn:
            return False
        try:
            cursor = self.conn.cursor()
            cursor.execute("SELECT 1")
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
            print_safe(f"      ⚠️  Snowflake connection lost, reconnecting...")
            if self.reconnect():
                print_safe(f"      ✓ Snowflake reconnected successfully")
                return True
            else:
                print_safe(f"      ✗ Snowflake reconnection failed")
                return False
        return True

    def disconnect(self):
        if self.conn:
            try:
                self.conn.close()
            except:
                pass
        self.conn = None

    def table_exists(self, table_name: str) -> bool:
        max_retries = 3
        for attempt in range(max_retries):
            try:
                if not self.ensure_connected():
                    if attempt < max_retries - 1:
                        time.sleep(2 ** attempt)
                        continue
                    else:
                        return False
                cursor = self.conn.cursor()
                query = f"""
                    SELECT COUNT(*) 
                    FROM "{self.database}".INFORMATION_SCHEMA.TABLES 
                    WHERE UPPER(TABLE_SCHEMA) = %s 
                    AND UPPER(TABLE_NAME) = %s
                """
                cursor.execute(query, (self.schema.upper(), table_name.upper()))
                count = cursor.fetchone()[0]
                cursor.close()
                self.last_activity = time.time()
                return count > 0
            except Exception as e:
                error_str = str(e)
                if "not connected" in error_str.lower() or attempt < max_retries - 1:
                    time.sleep(2 ** attempt)
                    continue
                return False

    def get_columns(self, table_name: str) -> List[Dict]:
        max_retries = 3
        for attempt in range(max_retries):
            try:
                if not self.ensure_connected():
                    if attempt < max_retries - 1:
                        time.sleep(2 ** attempt)
                        continue
                    else:
                        raise Exception("Failed to establish Snowflake connection after retries")
                cursor = self.conn.cursor()
                cursor.execute(f'DESCRIBE TABLE "{self.database}"."{self.schema}"."{table_name}"')
                columns = []
                for row in cursor.fetchall():
                    columns.append({'name': row[0], 'type': row[1], 'nullable': row[3] == 'Y'})
                cursor.close()
                self.last_activity = time.time()
                if not columns:
                    error_msg = f"Table {table_name} not found in Snowflake {self.database}.{self.schema}"
                    log_detailed_error(
                        "Snowflake - Table Not Found", Exception(error_msg),
                        {"Table": table_name, "Database": self.database, "Schema": self.schema, "Role": self.role,
                         "Possible Reason": "Table does not exist in Snowflake target or role lacks permission"}
                    )
                    raise Exception(error_msg)
                return columns
            except Exception as e:
                error_str = str(e)
                if "not connected" in error_str.lower():
                    if attempt < max_retries - 1:
                        print_safe(f"      ⚠️  Connection error on attempt {attempt + 1}, retrying...")
                        time.sleep(2 ** attempt)
                        continue
                if "not found" not in error_str.lower():
                    log_detailed_error(
                        f"Snowflake - Failed to retrieve columns for table {table_name}", e,
                        {"Table": table_name, "Database": self.database, "Schema": self.schema,
                         "Role": self.role, "Attempt": attempt + 1}
                    )
                raise

    def get_constraints(self, table_name: str) -> Dict:
        constraints = {
            'primary_keys': [], 'unique_constraints': [],
            'check_constraints': [], 'not_null_columns': [], 'foreign_keys': []
        }
        try:
            cursor = self.conn.cursor()
            cursor.execute(f'DESCRIBE TABLE "{self.database}"."{self.schema}"."{table_name}"')
            describe_results = cursor.fetchall()
            for row in describe_results:
                column_name = row[0]
                is_nullable = row[3]
                is_primary_key = row[5]
                is_unique_key = row[6]
                check_constraint = row[7] if len(row) > 7 else None
                if is_primary_key == 'Y':
                    constraints['primary_keys'].append(column_name)
                if is_unique_key == 'Y':
                    constraints['unique_constraints'].append({'name': f'UNIQUE_{column_name}', 'columns': [column_name]})
                if is_nullable == 'N':
                    constraints['not_null_columns'].append(column_name)
                if check_constraint:
                    constraints['check_constraints'].append({'name': f'CHECK_{column_name}', 'condition': check_constraint})
            try:
                cursor.execute(f'SHOW IMPORTED KEYS IN TABLE "{self.database}"."{self.schema}"."{table_name}"')
                fk_results = cursor.fetchall()
                fk_dict = {}
                for row in fk_results:
                    if len(row) >= 10:
                        fk_name = row[9] if row[9] else f"FK_{row[7]}"
                        fk_column = row[7]
                        pk_table = row[2]
                        pk_column = row[3]
                        if fk_name not in fk_dict:
                            fk_dict[fk_name] = {'columns': [], 'referenced_table': pk_table, 'referenced_columns': []}
                        fk_dict[fk_name]['columns'].append(fk_column)
                        fk_dict[fk_name]['referenced_columns'].append(pk_column)
                constraints['foreign_keys'] = [
                    {'name': name, 'columns': fk['columns'],
                     'referenced_table': fk['referenced_table'],
                     'referenced_columns': fk['referenced_columns']}
                    for name, fk in fk_dict.items()
                ]
            except Exception:
                pass
            cursor.close()
        except Exception as e:
            log_detailed_error(
                f"Snowflake - Failed to get constraints for table {table_name}", e,
                {"Table": table_name, "Database": self.database, "Schema": self.schema, "Role": self.role}
            )
        return constraints

    def get_table_last_altered(self, table_name: str) -> Optional[str]:
        try:
            query = f"""
                SELECT LAST_ALTERED 
                FROM "{self.database}".INFORMATION_SCHEMA.TABLES 
                WHERE UPPER(TABLE_SCHEMA) = %s 
                AND UPPER(TABLE_NAME) = %s
            """
            cursor = self.conn.cursor()
            cursor.execute(query, (self.schema.upper(), table_name.upper()))
            result = cursor.fetchone()
            cursor.close()
            if result and result[0]:
                return result[0].isoformat()
            return None
        except Exception as e:
            print_safe(f"⚠️  Failed to get last altered time for {table_name}: {e}")
            return None


# LOB types that don't support COUNT(DISTINCT)
LOB_TYPES = {'CLOB', 'BLOB', 'NCLOB', 'BFILE', 'LONG', 'LONG RAW'}


class TargetCollector:
    """
    Collects Snowflake target data and merges with Oracle source snapshot.
    Produces the exact same result structure as the original DataValidator.
    """

    def __init__(self, sf_pool_manager, pipeline_snapshot: Dict,
                 parallel_execution: bool = True, max_parallel_tables: int = 5,
                 table_timeout: int = 60, max_retries: int = 3):
        self.sf_pool_manager = sf_pool_manager
        self.is_multi_role = isinstance(sf_pool_manager, MultiRoleSnowflakePoolManager)
        self.pipeline_snapshot = pipeline_snapshot
        self.source_data = pipeline_snapshot['source_data']
        self.table_data_list = self.source_data['table_data']
        self.format_type = pipeline_snapshot['format_type']
        self.excluded_columns = [c.upper() for c in pipeline_snapshot.get('excluded_columns', [])]
        self.field_name_transformation = pipeline_snapshot.get('field_name_transformation', False)
        self.transformation_rules = pipeline_snapshot.get('transformation_rules', [])
        self.parallel_execution = parallel_execution
        self.max_parallel_tables = max_parallel_tables
        self.table_timeout = table_timeout
        self.max_retries = max_retries
        self.results_lock = Lock()
        self.has_abandoned_threads = False
        self.results = {
            'metadata': {
                'validation_timestamp': datetime.now(EASTERN).isoformat(),
                'validation_type': 'Deep Validation (Parallel)' if parallel_execution else 'Deep Validation (Sequential)',
                'format_type': self.format_type,
                'total_tables': len(self.table_data_list),
                'excluded_columns': self.excluded_columns,
                'parallel_execution': parallel_execution,
                'max_parallel_workers': max_parallel_tables if parallel_execution else 1,
                'multi_role_enabled': self.is_multi_role,
                'table_timeout_seconds': table_timeout,
                'max_retries': max_retries,
                'field_name_transformation_enabled': self.field_name_transformation
            },
            'summary': {},
            'table_validations': [],
            'faulty_tables': list(self.source_data.get('faulty_tables', []))
        }

    def _filter_excluded_columns(self, columns: List[Dict]) -> List[Dict]:
        if not self.excluded_columns:
            return columns
        return [col for col in columns if col['name'].upper() not in self.excluded_columns]

    def _get_sf_column_stats(self, sf_conn, sf_table: str, sf_columns: List[Dict]) -> Dict:
        """
        Collect per-column stats from Snowflake.
        Non-LOB columns: distinct_count + null_count.
        LOB columns: null_count only.
        Returns dict keyed by column_name_upper: {distinct_count (None for LOBs), null_count}.
        """
        non_lob_cols = [col for col in sf_columns
                        if not any(lob in col.get('type', '').upper() for lob in LOB_TYPES)]
        lob_cols = [col for col in sf_columns
                    if any(lob in col.get('type', '').upper() for lob in LOB_TYPES)]

        result_lookup = {}

        # Non-LOB: distinct + null in one combined query
        if non_lob_cols:
            stat_parts = []
            for col in non_lob_cols:
                safe_alias = re.sub(r'[^A-Za-z0-9_]', '_', col['name'].upper())
                stat_parts.append(f'COUNT(DISTINCT "{col["name"]}") as {safe_alias}_DISTINCT')
                stat_parts.append(f'SUM(CASE WHEN "{col["name"]}" IS NULL THEN 1 ELSE 0 END) as {safe_alias}_NULLS')
            query = (f'SELECT {", ".join(stat_parts)} FROM '
                     f'"{sf_conn.database}"."{sf_conn.schema}"."{sf_table}"')
            try:
                cursor = sf_conn.conn.cursor()
                cursor.execute(query)
                row = cursor.fetchone()
                cursor.close()
                if row:
                    for i, col in enumerate(non_lob_cols):
                        col_name_upper = col['name'].upper()
                        result_lookup[col_name_upper] = {
                            'distinct_count': row[i * 2] if row[i * 2] is not None else 0,
                            'null_count': row[i * 2 + 1] if row[i * 2 + 1] is not None else 0
                        }
            except Exception as e:
                log_detailed_error(f"Snowflake - Failed to get non-LOB column stats for {sf_table}", e,
                                   {"Table": sf_table})

        # LOB: null only
        if lob_cols:
            lob_parts = []
            for col in lob_cols:
                safe_alias = re.sub(r'[^A-Za-z0-9_]', '_', col['name'].upper())
                lob_parts.append(f'SUM(CASE WHEN "{col["name"]}" IS NULL THEN 1 ELSE 0 END) as {safe_alias}_NULLS')
            lob_query = (f'SELECT {", ".join(lob_parts)} FROM '
                         f'"{sf_conn.database}"."{sf_conn.schema}"."{sf_table}"')
            try:
                cursor = sf_conn.conn.cursor()
                cursor.execute(lob_query)
                row = cursor.fetchone()
                cursor.close()
                if row:
                    for i, col in enumerate(lob_cols):
                        col_name_upper = col['name'].upper()
                        result_lookup[col_name_upper] = {
                            'distinct_count': None,
                            'null_count': row[i] if row[i] is not None else 0
                        }
            except Exception as e:
                log_detailed_error(f"Snowflake - Failed to get LOB column null stats for {sf_table}", e,
                                   {"Table": sf_table})

        return result_lookup

    def _get_sf_row_count(self, sf_conn, sf_table: str) -> Optional[int]:
        try:
            query = f'SELECT COUNT(*) FROM "{sf_conn.database}"."{sf_conn.schema}"."{sf_table}"'
            cursor = sf_conn.conn.cursor()
            cursor.execute(query)
            count = cursor.fetchone()[0]
            cursor.close()
            return count
        except Exception as e:
            log_detailed_error(f"Snowflake - Failed to get row count for {sf_table}", e, {"Table": sf_table})
            return None

    def _get_sf_duplicate_count(self, sf_conn, sf_table: str, sf_columns: List[Dict],
                                 common_col_names: set) -> Optional[int]:
        """Count SF duplicate rows using the common non-LOB columns (mirrors original logic)"""
        try:
            # Use common non-LOB columns for duplicate count (same as original which uses sf_common_cols)
            sf_common_non_lob = [
                col for col in sf_columns
                if col['name'].upper() in common_col_names
                and not any(lob in col.get('type', '').upper() for lob in LOB_TYPES)
            ]
            if not sf_common_non_lob:
                return 0
            col_list = ', '.join([f'"{col["name"]}"' for col in sf_common_non_lob])
            query = f"""
                SELECT COUNT(*) FROM (
                    SELECT {col_list}, COUNT(*) as cnt
                    FROM "{sf_conn.database}"."{sf_conn.schema}"."{sf_table}"
                    GROUP BY {col_list}
                    HAVING COUNT(*) > 1
                ) AS duplicates
            """
            cursor = sf_conn.conn.cursor()
            cursor.execute(query)
            result = cursor.fetchone()[0]
            cursor.close()
            return result if result is not None else 0
        except Exception as e:
            log_detailed_error(f"Snowflake - Failed to get duplicate count for {sf_table}", e, {"Table": sf_table})
            return None

    def _merge_constraints(self, oracle_constraints: Dict, sf_conn, sf_table: str) -> Dict:
        """
        Compare pre-collected Oracle constraints (from snapshot) with fresh Snowflake constraints.
        Returns identical structure to _analyze_constraints in the original script.
        """
        constraint_counts = {
            'primary_keys': {'source': 0, 'snowflake': 0},
            'foreign_keys': {'source': 0, 'snowflake': 0},
            'unique_constraints': {'source': 0, 'snowflake': 0},
            'check_constraints': {'source': 0, 'snowflake': 0},
            'not_null_constraints': {'source': 0, 'snowflake': 0}
        }
        constraint_details = {'oracle': oracle_constraints, 'snowflake': {}}
        validation_results = {
            'primary_keys': {'status': 'UNKNOWN', 'details': None},
            'unique_constraints': {'status': 'UNKNOWN', 'details': None},
            'check_constraints': {'status': 'UNKNOWN', 'details': None},
            'not_null_constraints': {'status': 'UNKNOWN', 'details': []},
            'foreign_keys': {'status': 'UNKNOWN', 'details': None}
        }

        try:
            sf_constraints = sf_conn.get_constraints(sf_table)
            constraint_details['snowflake'] = sf_constraints

            # Primary Keys
            oracle_pk = set(oracle_constraints['primary_keys'])
            sf_pk = set(sf_constraints['primary_keys'])
            if oracle_pk == sf_pk:
                validation_results['primary_keys']['status'] = 'MATCH'
                validation_results['primary_keys']['details'] = f"Primary keys match: {list(oracle_pk)}"
            else:
                validation_results['primary_keys']['status'] = 'MISMATCH'
                pk_only_oracle = oracle_pk - sf_pk
                pk_only_sf = sf_pk - oracle_pk
                details = []
                if pk_only_oracle:
                    constraint_counts['primary_keys']['source'] = len(pk_only_oracle)
                    details.append(f"Primary keys only in Oracle: {list(pk_only_oracle)}")
                if pk_only_sf:
                    constraint_counts['primary_keys']['snowflake'] = len(pk_only_sf)
                    details.append(f"Primary keys only in Snowflake: {list(pk_only_sf)}")
                validation_results['primary_keys']['details'] = '; '.join(details)

            # Unique Constraints
            oracle_unique_count = len(oracle_constraints['unique_constraints'])
            sf_unique_count = len(sf_constraints['unique_constraints'])
            if oracle_unique_count == sf_unique_count:
                validation_results['unique_constraints']['status'] = 'MATCH'
                validation_results['unique_constraints']['details'] = f"Unique constraint count matches: {oracle_unique_count}"
            else:
                validation_results['unique_constraints']['status'] = 'MISMATCH'
                validation_results['unique_constraints']['details'] = (
                    f"Unique constraint count mismatch: Oracle={oracle_unique_count}, Snowflake={sf_unique_count}")
                if oracle_unique_count > sf_unique_count:
                    constraint_counts['unique_constraints']['source'] = oracle_unique_count - sf_unique_count
                if sf_unique_count > oracle_unique_count:
                    constraint_counts['unique_constraints']['snowflake'] = sf_unique_count - oracle_unique_count

            # NOT NULL Constraints — validate actual Snowflake data for each Oracle NOT NULL column
            oracle_nn = set(oracle_constraints['not_null_columns'])
            sf_nn = set(sf_constraints['not_null_columns'])
            nn_only_oracle = oracle_nn - sf_nn
            nn_only_sf = sf_nn - oracle_nn
            if nn_only_oracle:
                constraint_counts['not_null_constraints']['source'] = len(nn_only_oracle)

            cursor = sf_conn.conn.cursor()
            for col in oracle_nn:
                try:
                    null_check_query = f"""
                        SELECT 
                            COUNT(*) as total_rows,
                            COUNT(*) - COUNT("{col}") as null_count
                        FROM "{sf_conn.database}"."{sf_conn.schema}"."{sf_table}"
                    """
                    cursor.execute(null_check_query)
                    result = cursor.fetchone()
                    total_rows = result[0]
                    null_count = result[1]
                    exists_in_sf = col in sf_nn
                    constraint_status = {
                        'column': col,
                        'exists_in_oracle': True,
                        'exists_in_snowflake': exists_in_sf,
                        'snowflake_total_rows': total_rows,
                        'snowflake_null_count': null_count,
                        'validation_status': 'PASS' if null_count == 0 else 'FAIL'
                    }
                    validation_results['not_null_constraints']['details'].append(constraint_status)
                except Exception as e:
                    validation_results['not_null_constraints']['details'].append({'column': col, 'error': str(e)})
            cursor.close()

            if nn_only_sf:
                constraint_counts['not_null_constraints']['snowflake'] = len(nn_only_sf)

            failed_nn = sum(1 for d in validation_results['not_null_constraints']['details']
                            if d.get('validation_status') == 'FAIL')
            if failed_nn > 0:
                validation_results['not_null_constraints']['status'] = 'VALIDATION_FAILED'
            elif nn_only_oracle or nn_only_sf:
                validation_results['not_null_constraints']['status'] = 'DEFINITION_MISMATCH'
            else:
                validation_results['not_null_constraints']['status'] = 'MATCH'

            # Check Constraints
            oracle_check_count = len(oracle_constraints['check_constraints'])
            sf_check_count = len(sf_constraints['check_constraints'])
            if oracle_check_count == sf_check_count:
                validation_results['check_constraints']['status'] = 'MATCH'
                validation_results['check_constraints']['details'] = f"Check constraint count matches: {oracle_check_count}"
            else:
                validation_results['check_constraints']['status'] = 'MISMATCH'
                validation_results['check_constraints']['details'] = (
                    f"Check constraint count mismatch: Oracle={oracle_check_count}, Snowflake={sf_check_count}")
                if oracle_check_count > sf_check_count:
                    constraint_counts['check_constraints']['source'] = oracle_check_count - sf_check_count
                if sf_check_count > oracle_check_count:
                    constraint_counts['check_constraints']['snowflake'] = sf_check_count - oracle_check_count

            # Foreign Keys
            oracle_fk_count = len(oracle_constraints['foreign_keys'])
            sf_fk_count = len(sf_constraints['foreign_keys'])
            if oracle_fk_count == sf_fk_count == 0:
                validation_results['foreign_keys']['status'] = 'NONE'
                validation_results['foreign_keys']['details'] = "No foreign keys in either system"
            elif oracle_fk_count == sf_fk_count:
                validation_results['foreign_keys']['status'] = 'MATCH'
                validation_results['foreign_keys']['details'] = f"Foreign key count matches: {oracle_fk_count}"
            else:
                validation_results['foreign_keys']['status'] = 'MISMATCH'
                validation_results['foreign_keys']['details'] = (
                    f"Foreign key count mismatch: Oracle={oracle_fk_count}, Snowflake={sf_fk_count}")
                if oracle_fk_count > sf_fk_count:
                    constraint_counts['foreign_keys']['source'] = oracle_fk_count - sf_fk_count
                if sf_fk_count > oracle_fk_count:
                    constraint_counts['foreign_keys']['snowflake'] = sf_fk_count - oracle_fk_count

        except Exception as e:
            constraint_warnings_formatted = {'error': f"Error during constraint analysis: {str(e)}"}
            log_detailed_error("Constraint Analysis Failed", e, {"Snowflake Table": sf_table})
            return {
                'constraint_analysis_performed': True,
                'constraint_warnings': constraint_warnings_formatted,
                'constraint_details': constraint_details,
                'validation_results': validation_results,
                'summary': {
                    'primary_keys': validation_results['primary_keys']['status'],
                    'unique_constraints': validation_results['unique_constraints']['status'],
                    'check_constraints': validation_results['check_constraints']['status'],
                    'not_null_constraints': validation_results['not_null_constraints']['status'],
                    'foreign_keys': validation_results['foreign_keys']['status']
                }
            }

        constraint_warnings_formatted = {}
        for constraint_type, counts in constraint_counts.items():
            if counts['source'] > 0 or counts['snowflake'] > 0:
                type_name = constraint_type.replace('_', ' ').title()
                constraint_warnings_formatted[f"{type_name} mismatch"] = (
                    f"Source={counts['source']}, Snowflake={counts['snowflake']}")

        return {
            'constraint_analysis_performed': True,
            'constraint_warnings': constraint_warnings_formatted,
            'constraint_details': constraint_details,
            'validation_results': validation_results,
            'summary': {
                'primary_keys': validation_results['primary_keys']['status'],
                'unique_constraints': validation_results['unique_constraints']['status'],
                'check_constraints': validation_results['check_constraints']['status'],
                'not_null_constraints': validation_results['not_null_constraints']['status'],
                'foreign_keys': validation_results['foreign_keys']['status']
            }
        }

    def _perform_merge(self, sf_conn, table_snapshot: Dict) -> Dict:
        """
        Collect Snowflake data for one table and merge with Oracle snapshot data.
        Produces the exact same dict structure as the original _perform_validation.
        """
        sf_table = table_snapshot['snowflake_table']
        oracle_table = table_snapshot['oracle_table']
        oracle_columns = table_snapshot['oracle_columns']
        oracle_row_count = table_snapshot['oracle_row_count']
        oracle_col_stats_list = table_snapshot.get('oracle_column_stats', [])
        oracle_dup_count = table_snapshot.get('oracle_duplicate_count')
        oracle_constraints = table_snapshot.get('oracle_constraints', {
            'primary_keys': [], 'unique_constraints': [],
            'check_constraints': [], 'not_null_columns': [], 'foreign_keys': []
        })

        # Build oracle column stats lookup by matched_name (upper)
        oracle_stats_lookup = {stat['column_name']: stat for stat in oracle_col_stats_list}

        # --- Snowflake columns ---
        sf_columns = sf_conn.get_columns(sf_table)
        sf_columns = self._filter_excluded_columns(sf_columns)

        if not sf_columns:
            raise Exception(f'Unable to retrieve columns from Snowflake for {sf_table}')

        # --- Column analysis ---
        if self.field_name_transformation:
            oracle_col_names = {
                transform_column_name(col['name'], self.transformation_rules).upper()
                for col in oracle_columns
            }
        else:
            oracle_col_names = {col['name'].upper() for col in oracle_columns}

        sf_col_names = {col['name'].upper() for col in sf_columns}
        common_col_names = oracle_col_names & sf_col_names
        oracle_only = oracle_col_names - sf_col_names
        sf_only = sf_col_names - oracle_col_names

        # --- Snowflake row count ---
        sf_row_count = self._get_sf_row_count(sf_conn, sf_table)

        # --- Snowflake column stats (non-LOB: distinct+null, LOB: null only) ---
        sf_col_stats_lookup = self._get_sf_column_stats(sf_conn, sf_table, sf_columns)

        # --- Snowflake duplicate count (using common non-LOB columns, same as original) ---
        sf_dup_count = self._get_sf_duplicate_count(sf_conn, sf_table, sf_columns, common_col_names)

        # --- Oracle null count: sum snapshot null_counts for common columns ---
        # This includes LOB columns (oracle_col_stats_list has both LOB and non-LOB null counts)
        oracle_nulls = sum(
            stat['null_count']
            for stat in oracle_col_stats_list
            if stat['column_name'] in common_col_names
        )

        # --- SF null count: sum sf_col_stats null_counts for common SF columns ---
        sf_nulls = sum(
            sf_col_stats_lookup.get(col_name, {}).get('null_count', 0)
            for col_name in common_col_names
        )

        # --- Column statistics (non-LOB common columns only — same as original) ---
        column_statistics = []
        for col_name in sorted(common_col_names):
            oracle_stat = oracle_stats_lookup.get(col_name)
            sf_stat = sf_col_stats_lookup.get(col_name)

            # Skip if either side has no stats (LOB columns — no distinct count)
            if oracle_stat is None or sf_stat is None:
                continue
            if oracle_stat.get('is_lob', False) or sf_stat.get('distinct_count') is None:
                continue

            sf_col_obj = next((c for c in sf_columns if c['name'].upper() == col_name), None)
            oracle_col_obj = next(
                (c for c in oracle_columns
                 if (transform_column_name(c['name'], self.transformation_rules).upper()
                     if self.field_name_transformation else c['name'].upper()) == col_name),
                None
            )

            col_stat = {
                'column_name': col_name,
                'oracle_type': oracle_stat.get('oracle_type', 'UNKNOWN'),
                'snowflake_type': sf_col_obj['type'] if sf_col_obj else 'UNKNOWN',
                'oracle_stats': {
                    'distinct_count': oracle_stat['distinct_count'],
                    'null_count': oracle_stat['null_count']
                },
                'snowflake_stats': {
                    'distinct_count': sf_stat['distinct_count'],
                    'null_count': sf_stat['null_count']
                },
                'stats_match': {
                    'distinct_count': oracle_stat['distinct_count'] == sf_stat['distinct_count'],
                    'null_count': oracle_stat['null_count'] == sf_stat['null_count']
                }
            }
            if self.field_name_transformation:
                col_stat['oracle_original_name'] = oracle_stat.get('oracle_original_name', col_name)
                col_stat['snowflake_name'] = sf_col_obj['name'] if sf_col_obj else col_name

            column_statistics.append(col_stat)

        # --- Constraint analysis ---
        constraint_analysis = self._merge_constraints(oracle_constraints, sf_conn, sf_table)

        # --- Validation issues (same logic as original _perform_validation) ---
        validation_issues = []
        if oracle_row_count != sf_row_count:
            validation_issues.append(f"Row count mismatch: Oracle={oracle_row_count}, Snowflake={sf_row_count}")
        if oracle_only:
            validation_issues.append(f"Columns only in Oracle: {', '.join(sorted(oracle_only))}")
        if sf_only:
            validation_issues.append(f"Columns only in Snowflake: {', '.join(sorted(sf_only))}")
        if oracle_nulls != sf_nulls:
            validation_issues.append(f"Null count mismatch: Oracle={oracle_nulls}, Snowflake={sf_nulls}")
        if oracle_dup_count != sf_dup_count:
            validation_issues.append(f"Duplicate count mismatch: Oracle={oracle_dup_count}, Snowflake={sf_dup_count}")
        for col_stat in column_statistics:
            if not col_stat['stats_match']['distinct_count']:
                validation_issues.append(
                    f"Column {col_stat['column_name']}: distinct count mismatch "
                    f"(Oracle={col_stat['oracle_stats']['distinct_count']}, "
                    f"Snowflake={col_stat['snowflake_stats']['distinct_count']})"
                )

        status = 'PASSED' if not validation_issues else 'CRITICAL'

        return {
            'table_name': oracle_table,
            'snowflake_table': sf_table,
            'status': status,
            'row_counts': {
                'oracle': oracle_row_count,
                'snowflake': sf_row_count,
                'match': oracle_row_count == sf_row_count
            },
            'column_analysis': {
                'oracle_columns': len(oracle_columns),
                'snowflake_columns': len(sf_columns),
                'common_columns': len(common_col_names),
                'oracle_only_columns': list(sorted(oracle_only)),
                'snowflake_only_columns': list(sorted(sf_only))
            },
            'data_quality': {
                'oracle_nulls': oracle_nulls,
                'snowflake_nulls': sf_nulls,
                'null_match': oracle_nulls == sf_nulls,
                'oracle_duplicates': oracle_dup_count,
                'snowflake_duplicates': sf_dup_count,
                'duplicate_match': oracle_dup_count == sf_dup_count
            },
            'column_statistics': column_statistics,
            'constraint_analysis': constraint_analysis,
            'validation_issues': validation_issues
        }

    def _collect_and_merge_table(self, table_snapshot: Dict) -> Dict:
        """
        Collect Snowflake data for one table and merge with Oracle snapshot.
        Handles multi-role fallback, TABLE_NOT_FOUND, and errors.
        Produces identical result structure to the original _validate_single_table.
        """
        start_time = time.time()
        oracle_table = table_snapshot.get('oracle_table', 'UNKNOWN')
        sf_table = table_snapshot.get('snowflake_table', 'UNKNOWN')

        # If source collection failed, propagate error — don't hit Snowflake
        if table_snapshot.get('status') not in ('COLLECTED',):
            src_status = table_snapshot.get('status', 'ERROR')
            src_error = table_snapshot.get('error', 'Source collection did not succeed')
            return {
                'table_name': oracle_table,
                'snowflake_table': sf_table,
                'status': 'ERROR',
                'error': f"Source collection {src_status}: {src_error}",
                'validation_time': 0,
                'Snowflake_Last_Updated': None,
                'Last_Checked': datetime.now(EASTERN).isoformat()
            }

        if self.is_multi_role:
            roles = self.sf_pool_manager.get_all_roles()
            last_error = None
            last_error_details = {}

            # Find a role that can see the table
            table_exists = False
            sf_conn_for_check = None
            role_with_access = None
            sf_last_updated = None

            for role in roles:
                sf_conn_for_check = self.sf_pool_manager.get_connection(role)
                if sf_conn_for_check:
                    if sf_conn_for_check.table_exists(sf_table):
                        table_exists = True
                        role_with_access = role
                        sf_last_updated = sf_conn_for_check.get_table_last_altered(sf_table)
                        print_safe(f"       ✓ Table '{sf_table}' exists in Snowflake")
                        break
                    else:
                        self.sf_pool_manager.return_connection(sf_conn_for_check, role)

            if not table_exists:
                error_msg = f"Table '{sf_table}' does not exist in Snowflake"
                print_safe(f"       ✗ Table '{sf_table}' does not exist in Snowflake - skipping validation")
                return {
                    'table_name': oracle_table,
                    'snowflake_table': sf_table,
                    'status': 'TABLE_NOT_FOUND',
                    'error': error_msg,
                    'validation_time': time.time() - start_time,
                    'Snowflake_Last_Updated': None,
                    'Last_Checked': datetime.now(EASTERN).isoformat()
                }

            for role_idx, role in enumerate(roles):
                if role == role_with_access and sf_conn_for_check:
                    sf_conn = sf_conn_for_check
                    sf_conn_for_check = None  # consumed
                else:
                    sf_conn = self.sf_pool_manager.get_connection(role)

                if not sf_conn:
                    last_error = f"Failed to get Snowflake connection for role {role}"
                    last_error_details = {"Role": role, "Reason": "Unable to obtain Snowflake connection from pool"}
                    continue

                try:
                    result = self._perform_merge(sf_conn, table_snapshot)
                    self.sf_pool_manager.return_connection(sf_conn, role)
                    result['validation_time'] = round(time.time() - start_time, 2)
                    result['role_used'] = role
                    result['Snowflake_Last_Updated'] = sf_last_updated
                    result['Last_Checked'] = datetime.now(EASTERN).isoformat()
                    return result
                except Exception as e:
                    error_msg = str(e)
                    last_error = error_msg
                    last_error_details = {
                        "Table": oracle_table,
                        "Snowflake Table": sf_table,
                        "Role Attempted": role,
                        "Role Index": f"{role_idx + 1}/{len(roles)}"
                    }
                    self.sf_pool_manager.return_connection(sf_conn, role)
                    if is_access_error(error_msg) and role_idx < len(roles) - 1:
                        print_safe(f"       ⚠️  Access error with role {role}, trying next role...")
                        continue
                    else:
                        log_detailed_error(f"Table Validation Failed - {oracle_table}", e, last_error_details)
                        return {
                            'table_name': oracle_table,
                            'snowflake_table': sf_table,
                            'status': 'ERROR',
                            'error': error_msg,
                            'validation_time': round(time.time() - start_time, 2),
                            'Snowflake_Last_Updated': sf_last_updated,
                            'Last_Checked': datetime.now(EASTERN).isoformat()
                        }

            # All roles failed
            log_detailed_error(
                f"Table Validation Failed - All Roles Exhausted for {oracle_table}",
                Exception(f'All {len(roles)} roles failed'),
                {**last_error_details, "Last Error": last_error, "Roles Tried": roles}
            )
            return {
                'table_name': oracle_table,
                'snowflake_table': sf_table,
                'status': 'ERROR',
                'error': f'All {len(roles)} roles failed. Last error: {last_error}',
                'validation_time': round(time.time() - start_time, 2),
                'Snowflake_Last_Updated': None,
                'Last_Checked': datetime.now(EASTERN).isoformat()
            }

        else:
            # Single role mode
            sf_conn = self.sf_pool_manager.get_connection()
            if not sf_conn:
                error_msg = 'Failed to get Snowflake connection from pool'
                log_detailed_error("Table Validation Failed - Snowflake Connection",
                                   Exception(error_msg),
                                   {"Table": oracle_table, "Reason": "Unable to obtain Snowflake connection"})
                return {
                    'table_name': oracle_table,
                    'status': 'ERROR',
                    'error': error_msg,
                    'validation_time': round(time.time() - start_time, 2),
                    'Snowflake_Last_Updated': None,
                    'Last_Checked': datetime.now(EASTERN).isoformat()
                }

            if not sf_conn.table_exists(sf_table):
                error_msg = f"Table '{sf_table}' does not exist in Snowflake"
                print_safe(f"       ✗ Table '{sf_table}' does not exist in Snowflake - skipping validation")
                self.sf_pool_manager.return_connection(sf_conn)
                return {
                    'table_name': oracle_table,
                    'snowflake_table': sf_table,
                    'status': 'TABLE_NOT_FOUND',
                    'error': error_msg,
                    'validation_time': round(time.time() - start_time, 2),
                    'Snowflake_Last_Updated': None,
                    'Last_Checked': datetime.now(EASTERN).isoformat()
                }

            print_safe(f"       ✓ Table '{sf_table}' exists in Snowflake")
            sf_last_updated = sf_conn.get_table_last_altered(sf_table)

            try:
                result = self._perform_merge(sf_conn, table_snapshot)
                self.sf_pool_manager.return_connection(sf_conn)
                result['validation_time'] = round(time.time() - start_time, 2)
                result['Snowflake_Last_Updated'] = sf_last_updated
                result['Last_Checked'] = datetime.now(EASTERN).isoformat()
                return result
            except Exception as e:
                log_detailed_error(
                    f"Table Validation Failed - {oracle_table}", e,
                    {"Table": oracle_table, "Snowflake Table": sf_table}
                )
                self.sf_pool_manager.return_connection(sf_conn)
                return {
                    'table_name': oracle_table,
                    'snowflake_table': sf_table,
                    'status': 'ERROR',
                    'error': str(e),
                    'validation_time': round(time.time() - start_time, 2),
                    'Snowflake_Last_Updated': sf_last_updated,
                    'Last_Checked': datetime.now(EASTERN).isoformat()
                }

    def _collect_and_merge_with_timeout(self, table_snapshot: Dict, timeout: int) -> Dict:
        """Process a single table with timeout support — abandons thread on timeout"""
        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(self._collect_and_merge_table, table_snapshot)
        oracle_table = table_snapshot.get('oracle_table', 'UNKNOWN')
        sf_table = table_snapshot.get('snowflake_table', 'UNKNOWN')
        try:
            result = future.result(timeout=timeout)
            executor.shutdown(wait=True)
            return result
        except TimeoutError:
            print_safe(f"       ⚠️  Timeout detected, abandoning validation thread for {oracle_table}...")
            self.has_abandoned_threads = True
            executor.shutdown(wait=False)
            error_msg = f'Validation exceeded {timeout}s timeout'
            log_detailed_error(
                f"Table Validation Timeout - {oracle_table}", Exception(error_msg),
                {"Source Table": oracle_table, "Snowflake Table": sf_table,
                 "Timeout": f"{timeout}s",
                 "Reason": "Table validation took longer than allowed timeout period. Background thread abandoned."}
            )
            return {
                'table_name': oracle_table,
                'snowflake_table': sf_table,
                'status': 'TIMEOUT',
                'error': error_msg,
                'validation_time': timeout,
                'Snowflake_Last_Updated': None,
                'Last_Checked': datetime.now(EASTERN).isoformat()
            }
        except Exception as e:
            executor.shutdown(wait=True)
            raise

    def collect_and_merge_all(self):
        """Process all tables with parallel execution and timeout/retry handling"""
        start_time = time.time()
        timeout_snapshots = []

        if self.format_type == 'query_based':
            table_snapshot = self.table_data_list[0] if self.table_data_list else None
            if not table_snapshot:
                print_safe(f"  ⚠️  No table data in snapshot for query-based pipeline")
                return

            print_safe(f"\n  ⏳ Validating query-based target data...")
            result = self._collect_and_merge_with_timeout(table_snapshot, timeout=self.table_timeout)

            if result['status'] == 'TIMEOUT' and self.max_retries > 0:
                print_safe(f"    ⏱️  Timed out after {self.table_timeout}s — retrying...")
                for retry_attempt in range(1, self.max_retries + 1):
                    print_safe(f"    🔁 Retry attempt {retry_attempt}/{self.max_retries}...")
                    result = self._collect_and_merge_with_timeout(table_snapshot, timeout=self.table_timeout)
                    if result['status'] != 'TIMEOUT':
                        break
                if result['status'] == 'TIMEOUT':
                    result['status'] = 'FAULTY'
                    result['error'] = f'Validation consistently exceeded {self.table_timeout}s timeout after {self.max_retries} retries'
                    with self.results_lock:
                        self.results['faulty_tables'].append({
                            'table_name': result.get('table_name'),
                            'snowflake_table': result.get('snowflake_table'),
                            'reason': f'Validation exceeded {self.table_timeout}s timeout',
                            'retry_attempts': self.max_retries
                        })

            status_icon = "✅" if result['status'] == 'PASSED' else ("🚨" if result['status'] == 'CRITICAL' else "❌")
            print_safe(f"    {status_icon} Query validation - {result['status']}")
            with self.results_lock:
                self.results['table_validations'].append(result)
        else:
            total = len(self.table_data_list)
            print_safe(f"\n  ⏳ Validating {total} table(s) against Snowflake target...")

            if self.parallel_execution and total > 1:
                with ThreadPoolExecutor(max_workers=self.max_parallel_tables) as executor:
                    future_to_snap = {
                        executor.submit(self._collect_and_merge_with_timeout, snap,
                                        timeout=self.table_timeout): (idx, snap)
                        for idx, snap in enumerate(self.table_data_list, 1)
                    }
                    for future in as_completed(future_to_snap):
                        idx, snap = future_to_snap[future]
                        result = future.result()
                        if result['status'] == 'TIMEOUT':
                            timeout_snapshots.append(snap)
                            print_safe(f"    ⏱️  [{idx}/{total}] {result.get('table_name', 'Unknown')} - Timed out")
                        else:
                            icon = "✅" if result['status'] == 'PASSED' else ("🚨" if result['status'] == 'CRITICAL' else "❌")
                            print_safe(f"    {icon} [{idx}/{total}] {result.get('table_name', 'Unknown')} - {result['status']}")
                        with self.results_lock:
                            self.results['table_validations'].append(result)
            else:
                for idx, snap in enumerate(self.table_data_list, 1):
                    result = self._collect_and_merge_with_timeout(snap, timeout=self.table_timeout)
                    if result['status'] == 'TIMEOUT':
                        timeout_snapshots.append(snap)
                        print_safe(f"    ⏱️  [{idx}/{total}] {result.get('table_name', 'Unknown')} - Timed out")
                    else:
                        icon = "✅" if result['status'] == 'PASSED' else ("🚨" if result['status'] == 'CRITICAL' else "❌")
                        print_safe(f"    {icon} [{idx}/{total}] {result.get('table_name', 'Unknown')} - {result['status']}")
                    with self.results_lock:
                        self.results['table_validations'].append(result)

            # Retry timeout tables
            if timeout_snapshots:
                print_safe(f"\n    🔁 Retrying {len(timeout_snapshots)} timeout tables (max {self.max_retries} attempts)...")
                for retry_attempt in range(1, self.max_retries + 1):
                    if not timeout_snapshots:
                        break
                    print_safe(f"    🔁 Retry attempt {retry_attempt}/{self.max_retries}...")
                    still_timeout = []
                    for snap in timeout_snapshots:
                        result = self._collect_and_merge_with_timeout(snap, timeout=self.table_timeout)
                        if result['status'] == 'TIMEOUT':
                            still_timeout.append(snap)
                            print_safe(f"       ⏱️  {result.get('table_name', 'Unknown')} - Still timing out...")
                        else:
                            icon = "✅" if result['status'] == 'PASSED' else ("🚨" if result['status'] == 'CRITICAL' else "❌")
                            print_safe(f"       {icon} {result.get('table_name', 'Unknown')} - {result['status']}")
                            with self.results_lock:
                                self.results['table_validations'] = [
                                    r for r in self.results['table_validations']
                                    if r.get('table_name') != result.get('table_name')
                                ]
                                self.results['table_validations'].append(result)
                    timeout_snapshots = still_timeout

                if timeout_snapshots:
                    print_safe(f"    ⚠️  {len(timeout_snapshots)} tables marked as FAULTY after {self.max_retries} retries")
                    with self.results_lock:
                        for snap in timeout_snapshots:
                            table_name = snap.get('oracle_table', 'UNKNOWN')
                            sf_table = snap.get('snowflake_table')
                            for r in self.results['table_validations']:
                                if r.get('table_name') == table_name and r.get('status') == 'TIMEOUT':
                                    r['status'] = 'FAULTY'
                                    r['error'] = f'Validation consistently exceeded {self.table_timeout}s timeout after {self.max_retries} retry attempts'
                            faulty_entry = {
                                'table_name': table_name,
                                'reason': f'Validation exceeded {self.table_timeout}s timeout',
                                'retry_attempts': self.max_retries
                            }
                            if sf_table:
                                faulty_entry['snowflake_table'] = sf_table
                            self.results['faulty_tables'].append(faulty_entry)

        # Build summary — same structure as DataValidator.validate_all_tables
        total_time = time.time() - start_time
        passed = sum(1 for r in self.results['table_validations'] if r['status'] == 'PASSED')
        critical = sum(1 for r in self.results['table_validations'] if r['status'] == 'CRITICAL')
        errors = sum(1 for r in self.results['table_validations'] if r['status'] == 'ERROR')
        faulty = sum(1 for r in self.results['table_validations'] if r['status'] == 'FAULTY')
        not_found = sum(1 for r in self.results['table_validations'] if r['status'] == 'TABLE_NOT_FOUND')

        self.results['summary'] = {
            'total_validations': len(self.results['table_validations']),
            'passed': passed,
            'critical': critical,
            'errors': errors,
            'faulty': faulty,
            'not_found': not_found,
            'total_execution_time': round(total_time, 2),
            'success_rate': round((passed / len(self.results['table_validations']) * 100), 2)
                            if self.results['table_validations'] else 0
        }

        summary_parts = [f"✅ {passed}", f"🚨 {critical}", f"⚠️ {errors}"]
        if not_found > 0:
            summary_parts.append(f"⊘ {not_found} not found")
        if faulty > 0:
            summary_parts.append(f"🔴 {faulty} faulty")
        print_safe(f"    ⏱️  Completed in {self.results['summary']['total_execution_time']}s | " +
                   " | ".join(summary_parts))

    def get_results(self) -> Dict:
        return self.results


def process_single_pipeline_target(pipeline_snapshot: Dict, config: Dict, pipeline_index: int) -> Dict:
    """Process target collection and merge for a single pipeline from snapshot"""
    pipeline_name = pipeline_snapshot.get('pipeline_name', f'Pipeline_{pipeline_index}')
    pipeline_number = pipeline_snapshot.get('pipeline_number', pipeline_index)

    print_header(f"PIPELINE {pipeline_number}: {pipeline_name}", "=", 80)

    # If source collection failed for this pipeline, return error immediately
    if pipeline_snapshot.get('status') != 'SUCCESS':
        error_msg = pipeline_snapshot.get('error', 'Source collection failed')
        print_safe(f"  ❌ Source collection failed for this pipeline: {error_msg}")
        return {
            'pipeline_name': pipeline_name,
            'pipeline_number': pipeline_number,
            'status': 'ERROR',
            'error': f'Source collection failed: {error_msg}',
            'oracle_schema': pipeline_snapshot.get('oracle_schema'),
            'snowflake_database': pipeline_snapshot.get('snowflake_database'),
            'snowflake_schema': pipeline_snapshot.get('snowflake_schema'),
            'validation_results': None
        }

    sf_database = pipeline_snapshot['snowflake_database']
    sf_schema = pipeline_snapshot['snowflake_schema']

    try:
        parallel_execution = get_optional_config_value(config, 'validation', 'parallel_execution', True)
        max_parallel_tables = get_optional_config_value(config, 'validation', 'max_parallel_tables', 5)
        connection_pool_size = get_optional_config_value(config, 'validation', 'connection_pool_size',
                                                         max_parallel_tables + 2)
        table_timeout = get_optional_config_value(config, 'validation', 'table_timeout', 60)
        max_retries = get_optional_config_value(config, 'validation', 'max_timeout_retries', 3)

        # Load Snowflake connection config
        sf_config = load_snowflake_config(config)
        sf_account = sf_config['account']
        sf_user = sf_config['user']
        sf_warehouse = sf_config['warehouse']
        sf_private_key_path = sf_config['private_key_path']
        sf_private_key_password = sf_config['private_key_password']

        sf_role_config = sf_config['role']
        if isinstance(sf_role_config, list):
            sf_roles = sf_role_config
        else:
            sf_roles = [sf_role_config]

        print_safe(f"  📊 Configuration:")
        print_safe(f"     Oracle Schema (from snapshot): {pipeline_snapshot.get('oracle_schema')}")
        print_safe(f"     Snowflake Target: {sf_database}.{sf_schema}")
        print_safe(f"     Roles: {', '.join(sf_roles)}")
        print_safe(f"     Format: {pipeline_snapshot.get('format_type')}")
        source_data = pipeline_snapshot.get('source_data', {})
        table_count = len(source_data.get('table_data', []))
        print_safe(f"     Tables in snapshot: {table_count}")
        print_safe(f"     Timeout: {table_timeout}s per table")
        if pipeline_snapshot.get('field_name_transformation'):
            print_safe(f"     Column Name Transformation: ENABLED")

        # Create Snowflake pool manager
        if len(sf_roles) > 1:
            sf_pool_manager = MultiRoleSnowflakePoolManager(
                account=sf_account, user=sf_user,
                private_key_path=sf_private_key_path,
                private_key_password=sf_private_key_password,
                roles=sf_roles, warehouse=sf_warehouse,
                database=sf_database, schema=sf_schema,
                max_connections=connection_pool_size
            )
        else:
            sf_pool_manager = ConnectionPool(
                SnowflakeConnector,
                {
                    'account': sf_account, 'user': sf_user,
                    'private_key_path': sf_private_key_path,
                    'private_key_password': sf_private_key_password,
                    'role': sf_roles[0], 'warehouse': sf_warehouse,
                    'database': sf_database, 'schema': sf_schema
                },
                max_connections=connection_pool_size
            )

        try:
            # Test Snowflake connection
            print_safe(f"  🔌 Testing Snowflake connection...")
            sf_test = sf_pool_manager.get_connection()
            if not sf_test:
                error_msg = 'Failed to establish Snowflake connection'
                print_safe(f"  ❌ Failed to connect to Snowflake")
                return {
                    'pipeline_name': pipeline_name, 'pipeline_number': pipeline_number,
                    'status': 'ERROR', 'error': error_msg,
                    'oracle_schema': pipeline_snapshot.get('oracle_schema'),
                    'snowflake_database': sf_database, 'snowflake_schema': sf_schema,
                    'validation_results': None
                }
            if isinstance(sf_pool_manager, MultiRoleSnowflakePoolManager):
                sf_pool_manager.return_connection(sf_test, sf_roles[0])
            else:
                sf_pool_manager.return_connection(sf_test)
            print_safe(f"  ✅ Snowflake connection established")

            collector = TargetCollector(
                sf_pool_manager, pipeline_snapshot,
                parallel_execution=parallel_execution,
                max_parallel_tables=max_parallel_tables,
                table_timeout=table_timeout,
                max_retries=max_retries
            )
            collector.collect_and_merge_all()
            validation_results = collector.get_results()
            print_safe(f"  ✅ Pipeline target collection and merge completed")

            return {
                'pipeline_name': pipeline_name,
                'pipeline_number': pipeline_number,
                'status': 'SUCCESS',
                'oracle_schema': pipeline_snapshot.get('oracle_schema'),
                'snowflake_database': sf_database,
                'snowflake_schema': sf_schema,
                'validation_results': validation_results
            }

        finally:
            print_safe(f"  🧹 Cleaning up Snowflake connection pool...")
            skip_disconnect = False
            try:
                if 'collector' in locals() and hasattr(collector, 'has_abandoned_threads'):
                    skip_disconnect = collector.has_abandoned_threads
                    if skip_disconnect:
                        print_safe(f"  ⚠️  Abandoned threads detected - skipping disconnect")
            except:
                pass
            try:
                sf_pool_manager.close_all(force_skip_disconnect=skip_disconnect)
            except Exception as e:
                print_safe(f"  ⚠️  Error closing Snowflake pool: {e}")

    except Exception as e:
        error_details = tb.format_exc()
        log_detailed_error(f"Pipeline {pipeline_number} Target Processing Failed", e,
                           {"Pipeline": pipeline_name})
        return {
            'pipeline_name': pipeline_name, 'pipeline_number': pipeline_number,
            'status': 'ERROR', 'error': str(e), 'error_details': error_details,
            'oracle_schema': pipeline_snapshot.get('oracle_schema'),
            'snowflake_database': pipeline_snapshot.get('snowflake_database'),
            'snowflake_schema': pipeline_snapshot.get('snowflake_schema'),
            'validation_results': None
        }


def main():
    parser = argparse.ArgumentParser(description='Snowflake Target Collector & Validator - Step 2 of 2')
    parser.add_argument('--config', '-c', help='Config JSON file', default="validation_config.json")
    parser.add_argument('--snapshot', '-s', help='Source snapshot JSON file from step 1', default=None)
    args = parser.parse_args()

    print_header("Snowflake Target Collection & Validation (Step 2 of 2)", "=", 80)

    config = load_config_file(args.config)

    # Resolve snapshot path
    if args.snapshot:
        snapshot_path = args.snapshot
    elif 'validation' in config and 'snapshot_path' in config['validation']:
        snapshot_path = config['validation']['snapshot_path']
    else:
        snapshot_path = 'source_snapshot.json'

    # Load source snapshot
    try:
        with open(snapshot_path, 'r') as f:
            snapshot = json.load(f)
        print_safe(f"✅ Loaded source snapshot: {snapshot_path}")
        metadata = snapshot.get('snapshot_metadata', {})
        print_safe(f"   Generated: {metadata.get('generated_timestamp', 'unknown')}")
        print_safe(f"   Pipelines: {metadata.get('total_pipelines', 'unknown')}")
        print_safe(f"   Source report: {metadata.get('source_report', 'unknown')}")
    except FileNotFoundError:
        print_safe(f"❌ Snapshot file not found: {snapshot_path}")
        print_safe(f"   Run Validation_Script_Source.py first to generate the snapshot.")
        sys.exit(1)
    except json.JSONDecodeError as e:
        print_safe(f"❌ Invalid JSON in snapshot file: {e}")
        sys.exit(1)

    # Load Snowflake save config
    try:
        sf_save_config = load_snowflake_save_config(config)
        print_safe(f"✅ Snowflake (save) config loaded: {sf_save_config['account']} / {sf_save_config['database']}")
    except Exception as e:
        print_safe(f"❌ Snowflake Save configuration error: {e}")
        sys.exit(1)

    pipeline_snapshots = snapshot.get('pipelines', [])
    if not pipeline_snapshots:
        print_safe("❌ No pipelines found in snapshot")
        sys.exit(1)

    print_safe(f"\n🚀 Starting target validation for {len(pipeline_snapshots)} pipeline(s)...")

    all_results = []
    overall_start_time = time.time()

    with ThreadPoolExecutor(max_workers=min(len(pipeline_snapshots), 3)) as executor:
        future_to_pipeline = {
            executor.submit(process_single_pipeline_target, pipe_snap, config, i + 1): (i, pipe_snap)
            for i, pipe_snap in enumerate(pipeline_snapshots)
        }
        completed = 0
        for future in as_completed(future_to_pipeline):
            result = future.result()
            all_results.append(result)
            completed += 1
            status_icon = "✅" if result['status'] == 'SUCCESS' else "❌"
            print_safe(f"\n{status_icon} Completed pipeline {completed}/{len(pipeline_snapshots)}: {result['pipeline_name']}")

    overall_time = time.time() - overall_start_time
    all_results.sort(key=lambda x: x.get('pipeline_number', 0))

    # Build consolidated_report — IDENTICAL structure to the original script
    source_report_path = snapshot.get('snapshot_metadata', {}).get('source_report', 'unknown')
    consolidated_report = {
        'report_metadata': {
            'generated_timestamp': datetime.now(EASTERN).isoformat(),
            'total_pipelines': len(pipeline_snapshots),
            'total_execution_time': round(overall_time, 2),
            'source_report': source_report_path
        },
        'overall_summary': {
            'successful_pipelines': sum(1 for r in all_results if r['status'] == 'SUCCESS'),
            'failed_pipelines': sum(1 for r in all_results if r['status'] == 'ERROR'),
            'total_tables_validated': sum(
                r['validation_results']['summary']['total_validations']
                for r in all_results if r['status'] == 'SUCCESS' and r['validation_results']
            ),
            'total_passed': sum(
                r['validation_results']['summary']['passed']
                for r in all_results if r['status'] == 'SUCCESS' and r['validation_results']
            ),
            'total_critical': sum(
                r['validation_results']['summary']['critical']
                for r in all_results if r['status'] == 'SUCCESS' and r['validation_results']
            ),
            'total_errors': sum(
                r['validation_results']['summary']['errors']
                for r in all_results if r['status'] == 'SUCCESS' and r['validation_results']
            ),
            'total_not_found': sum(
                r['validation_results']['summary'].get('not_found', 0)
                for r in all_results if r['status'] == 'SUCCESS' and r['validation_results']
            ),
            'total_faulty': sum(
                r['validation_results']['summary'].get('faulty', 0)
                for r in all_results if r['status'] == 'SUCCESS' and r['validation_results']
            )
        },
        'pipeline_results': all_results,
        'faulty_tables_summary': []
    }

    for result in all_results:
        if result['status'] == 'SUCCESS' and result['validation_results']:
            faulty_tables = result['validation_results'].get('faulty_tables', [])
            if faulty_tables:
                consolidated_report['faulty_tables_summary'].append({
                    'pipeline_name': result['pipeline_name'],
                    'pipeline_number': result['pipeline_number'],
                    'faulty_tables': faulty_tables
                })

    # Save to Snowflake (STG + CORE)
    print_safe("\n💾 Saving report to Snowflake...")
    database_name = get_optional_config_value(config, 'validation', 'database_name', None)
    try:
        save_report_to_snowflake(consolidated_report, sf_save_config, database_name)
    except Exception as e:
        print_safe(f"\n❌ Failed to save report to Snowflake: {e}")
        print_safe("❌ Validation aborted due to database save failure")
        sys.exit(1)

    # Print overall summary — same as original
    print_header("OVERALL VALIDATION SUMMARY", "=", 80)
    print_safe(f"\n📋 PIPELINES:")
    print_safe(f"   Total: {consolidated_report['report_metadata']['total_pipelines']}")
    print_safe(f"   ✅ Successful: {consolidated_report['overall_summary']['successful_pipelines']}")
    print_safe(f"   ❌ Failed: {consolidated_report['overall_summary']['failed_pipelines']}")
    print_safe(f"\n📊 TABLES:")
    print_safe(f"   Total Validated: {consolidated_report['overall_summary']['total_tables_validated']}")
    print_safe(f"   ✅ Passed: {consolidated_report['overall_summary']['total_passed']}")
    print_safe(f"   🚨 Critical: {consolidated_report['overall_summary']['total_critical']}")
    print_safe(f"   ⚠️  Errors: {consolidated_report['overall_summary']['total_errors']}")
    if consolidated_report['overall_summary']['total_not_found'] > 0:
        print_safe(f"   ⊘  Not Found in Snowflake: {consolidated_report['overall_summary']['total_not_found']}")
    if consolidated_report['overall_summary']['total_faulty'] > 0:
        print_safe(f"   🔴 Faulty (Timeout): {consolidated_report['overall_summary']['total_faulty']}")
    print_safe(f"\n⏱️  EXECUTION TIME: {consolidated_report['report_metadata']['total_execution_time']}s")

    print_header("PER-PIPELINE SUMMARY", "-", 80)
    print_safe(f"\n{'#':<4} {'Pipeline Name':<40} {'Status':<10} {'Tables':<8} {'Passed':<8} {'Critical':<10}")
    print_safe("-" * 80)
    for result in all_results:
        pipeline_num = str(result['pipeline_number'])
        pipeline_name_display = result['pipeline_name'][:38] + '..' if len(result['pipeline_name']) > 40 else result['pipeline_name']
        status = result['status']
        if result['status'] == 'SUCCESS' and result['validation_results']:
            summary = result['validation_results']['summary']
            tables = str(summary['total_validations'])
            passed = str(summary['passed'])
            critical = str(summary['critical'])
        else:
            tables = passed = critical = 'N/A'
        status_icon = "✅" if status == 'SUCCESS' else "❌"
        print_safe(f"{pipeline_num:<4} {pipeline_name_display:<40} {status_icon} {status:<8} {tables:<8} {passed:<8} {critical:<10}")

    if consolidated_report['faulty_tables_summary']:
        print_header("FAULTY TABLES (TIMEOUT)", "-", 80)
        timeout_val = get_optional_config_value(config, 'validation', 'table_timeout', 60)
        print_safe(f"\n⚠️  The following tables consistently exceeded the {timeout_val}s timeout:\n")
        for pipeline_faulty in consolidated_report['faulty_tables_summary']:
            print_safe(f"Pipeline {pipeline_faulty['pipeline_number']}: {pipeline_faulty['pipeline_name']}")
            for faulty in pipeline_faulty['faulty_tables']:
                print_safe(f"   🔴 {faulty['table_name']} - {faulty['reason']}")
            print_safe("")

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