import oracledb
import snowflake.connector
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from datetime import datetime
import pytz
import json
import sys
import argparse
import re
from typing import Dict, List, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock, Thread
import time
import traceback as tb

# All timestamps use Eastern Time (auto-handles EST/EDT daylight saving)
EASTERN = pytz.timezone('America/New_York')

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
    """Log detailed error information to console (not to output file)"""
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

    # Remove backslashes that escape quotes in JSON
    cleaned = expression.replace('\\"', '"').replace('\\\\', '\\')

    # Find all pattern-replacement pairs: , "pattern", "replacement")
    # This regex correctly captures all pairs in nested replaceAll calls
    pattern = r',\s*"([^"]*)",\s*"([^"]*)"\)'
    matches = re.findall(pattern, cleaned)

    # Return list of (pattern, replacement) tuples in the order they appear
    return matches


def transform_column_name(column_name: str, transformations: List[Tuple[str, str]] = None) -> str:
    # Default transformation if none provided (backward compatibility)
    if transformations is None:
        transformations = [('[^a-zA-Z0-9]', '_'), ('_+', '_')]

    transformed = column_name

    # Apply each transformation in order
    for pattern, replacement in transformations:
        try:
            transformed = re.sub(pattern, replacement, transformed)
        except Exception as e:
            # If regex fails, log and continue
            print_safe(f"⚠️  Regex error - pattern: {pattern}, replacement: {replacement}, error: {e}")
            continue

    return transformed


def load_snowflake_config(config: dict) -> dict:
    """Load Snowflake configuration from config file"""
    if 'snowflake' not in config:
        raise ValueError("Snowflake configuration not found in config file")

    sf_config = config['snowflake']
    required_fields = ['account', 'user', 'private_key_path', 'private_key_password',
                       'role', 'warehouse']

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
        'database': sf_config.get('database')  # Optional - can be None
    }


def load_snowflake_save_config(config: dict) -> dict:
    """Load Snowflake Save configuration from config file (for saving validation reports)"""
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
    """Load private key for Snowflake authentication"""
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
    """Create Snowflake connection"""
    try:
        # Load private key
        private_key = get_private_key(
            sf_config['private_key_path'],
            sf_config['private_key_password']
        )

        # Build connection parameters
        conn_params = {
            'user': sf_config['user'],
            'account': sf_config['account'],
            'private_key': private_key,
            'role': sf_config['role'],
            'warehouse': sf_config['warehouse']
        }

        # Add database if provided
        if sf_config.get('database'):
            conn_params['database'] = sf_config['database']

        # Create connection
        conn = snowflake.connector.connect(**conn_params)
        return conn
    except FileNotFoundError as e:
        print_safe(f"❌ Private key file not found: {e}")
        raise
    except Exception as e:
        print_safe(f"❌ Snowflake connection failed: {e}")
        raise


def create_schemas_if_not_exist(conn, stg_schema: str, core_schema: str):
    """Create STG and CORE schemas if they don't exist in Snowflake"""
    try:
        cur = conn.cursor()

        # Create STG schema
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {stg_schema}")

        # Create CORE schema
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {core_schema}")

        cur.close()
    except Exception as e:
        print_safe(f"❌ Failed to create schemas: {e}")
        raise


def create_core_table_if_not_exists(conn, core_schema: str):
    """Create CORE validation_report table if it doesn't exist in Snowflake"""
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

    STG: Creates new timestamped table for each run (historical archive)
    CORE: Keeps the last 10 runs per database (rolling window).
          On the 11th run the oldest row is automatically removed.
          Only updated when all pipelines pass.
    """
    conn = None
    try:
        # Connect to Snowflake
        conn = create_snowflake_connection(sf_config)

        # Create schemas if needed
        create_schemas_if_not_exist(conn, sf_config['stg_schema'], sf_config['core_schema'])

        # Create CORE table if needed
        create_core_table_if_not_exists(conn, sf_config['core_schema'])

        # Generate timestamp for table naming
        timestamp = datetime.now(EASTERN).strftime('%Y%m%d_%H%M%S')
        stg_table_name = f"{sf_config['stg_schema']}.DQ_IT_VALIDATION_REPORT_{timestamp}"

        # Extract run_timestamp for use in both STG and CORE tables
        run_timestamp = consolidated_report.get('report_metadata', {}).get('generated_timestamp')

        # load_date — always Eastern time, passed explicitly so Snowflake never
        # falls back to CURRENT_TIMESTAMP() which defaults to UTC
        load_date_eastern = datetime.now(EASTERN).strftime('%Y-%m-%d %H:%M:%S.%f')

        # --- SAVE TO STG (Always) ---
        cur = conn.cursor()

        # Create new STG table with timestamp
        cur.execute(f"""
            CREATE TABLE {stg_table_name} (
                report_data VARIANT NOT NULL,
                run_timestamp TIMESTAMP_NTZ NOT NULL,
                load_date TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP(),
                database VARCHAR(255)
            )
        """)

        # Insert report data into STG — load_date supplied explicitly in Eastern time
        report_json = json.dumps(consolidated_report, indent=2)
        cur.execute(f"""
            INSERT INTO {stg_table_name} (report_data, run_timestamp, load_date, database)
            SELECT PARSE_JSON(%s), TO_TIMESTAMP_NTZ(%s), TO_TIMESTAMP_NTZ(%s), %s
        """, (report_json, run_timestamp, load_date_eastern, database_name))

        print_safe(f"✅ Snowflake: Saved to {stg_table_name}")

        # --- UPDATE CORE (Only if validation passed) ---
        failed_pipelines = consolidated_report.get('overall_summary', {}).get('failed_pipelines', 0)

        if failed_pipelines == 0:
            core_table_name = f"{sf_config['core_schema']}.DQ_IT_VALIDATION_REPORT"

            # Step 1 — Insert the new run — load_date supplied explicitly in Eastern time
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


class ConfigLoader:
    """Load configuration from StreamSets JSON report - supports single pipeline object"""

    def __init__(self, pipeline_data: dict):
        """Initialize with a single pipeline data (not the full report array)"""
        self.config = pipeline_data
        self.format_type = None
        self.sql_query = None
        self.oracle_schema_from_query = None
        self.oracle_table_from_query = None
        self._sf_table_list_cache = None
        self.field_name_transformation_detected = False
        self.transformation_rules = None  # Store extracted transformation rules
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
        """Detect if pipeline has Field Mapper that transforms column names and extract transformation rules"""
        try:
            stages = self.config.get('pipeline_stages', {}).get('stages', {})
            processors = stages.get('processors', [])

            for processor in processors:
                stage_name = processor.get('stageName', '')
                # Check if it's a Field Mapper processor
                if 'FieldMapper' in stage_name or 'FieldMapperDProcessor' in stage_name:
                    config_list = processor.get('configuration', [])

                    operate_on = None
                    mapping_expression = None

                    # Extract relevant configuration
                    for config_item in config_list:
                        if config_item.get('name') == 'fieldMapperConfig.operateOn':
                            operate_on = config_item.get('value')
                        elif config_item.get('name') == 'fieldMapperConfig.mappingExpression':
                            mapping_expression = config_item.get('value')

                    # Check if it operates on FIELD_NAMES and has a mapping expression
                    if operate_on == 'FIELD_NAMES' and mapping_expression:
                        # Parse the expression to extract transformation rules
                        transformations = parse_streamsets_replaceall(mapping_expression)

                        if transformations:
                            self.field_name_transformation_detected = True
                            self.transformation_rules = transformations

                            # Show what transformations will be applied
                            print_safe(f"  🔍 Detected Field Mapper transformation:")
                            for pattern, replacement in transformations:
                                repl_display = repr(replacement) if replacement else "''"
                                print_safe(f"     • Pattern: {pattern} → Replacement: {repl_display}")
                            return
        except Exception as e:
            # Silently fail - if we can't detect, we assume no transformation
            pass

    def get_transformation_rules(self) -> List[Tuple[str, str]]:
        """Get the extracted transformation rules"""
        return self.transformation_rules if self.transformation_rules else []

    def has_field_name_transformation(self) -> bool:
        """Check if field name transformation was detected in the pipeline"""
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
            import re
            pattern = r'FROM\s+([A-Za-z_][A-Za-z0-9_]*)\.'
            match = re.search(pattern, self.sql_query, re.IGNORECASE)
            if match:
                self.oracle_schema_from_query = match.group(1).upper()
        except Exception:
            pass

    def _extract_table_from_query(self):
        """Extract Oracle table name from SQL query"""
        if not self.sql_query:
            return
        try:
            import re
            # Pattern to match: FROM schema.table or FROM table
            # Handles both "FROM SCHEMA.TABLE" and "FROM TABLE"
            pattern = r'FROM\s+(?:[A-Za-z_][A-Za-z0-9_]*\.)?([A-Za-z_][A-Za-z0-9_]*)'
            match = re.search(pattern, self.sql_query, re.IGNORECASE)
            if match:
                self.oracle_table_from_query = match.group(1).upper()
        except Exception:
            pass

    def get_oracle_table_from_query(self) -> Optional[str]:
        """Get Oracle table name extracted from query"""
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
        """Extract Snowflake table names from TARGET section - cached for performance"""
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
        """Get Oracle table list from SOURCE section"""
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
        """Create mapping from Oracle to Snowflake table names - O(n) optimized"""
        mapping = {}
        sf_tables = self.get_snowflake_table_list()
        if not sf_tables:
            return mapping

        # Pre-build lookup dictionaries for O(1) access
        sf_upper_to_name = {sf.upper(): sf for sf in sf_tables}

        # Build suffix lookup for endswith matching
        sf_suffix_map = {}
        for sf_table in sf_tables:
            sf_upper = sf_table.upper()
            sf_suffix_map[sf_upper] = sf_table

        # Match Oracle tables - single pass
        for oracle_table in oracle_tables:
            oracle_upper = oracle_table.upper()

            # Strategy 1: Exact match (fastest)
            if oracle_upper in sf_upper_to_name:
                mapping[oracle_table] = sf_upper_to_name[oracle_upper]
                continue

            # Strategy 2: Suffix match - check if any SF table ends with Oracle name
            matched = False
            for sf_upper, sf_name in sf_suffix_map.items():
                if sf_upper.endswith(oracle_upper):
                    mapping[oracle_table] = sf_name
                    matched = True
                    break

            if matched:
                continue

            # Strategy 3: Partial match (fallback)
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
        self.keepalive_interval = 300  # Ping every 5 minutes
        self.keepalive_thread = None
        self.keepalive_running = False
        self._start_keepalive()

    def _start_keepalive(self):
        """Start background keepalive thread"""
        self.keepalive_running = True
        self.keepalive_thread = Thread(target=self._keepalive_worker, daemon=True)
        self.keepalive_thread.start()

    def _keepalive_worker(self):
        """Background worker to keep connections alive"""
        # Use short sleep intervals (1 second) so thread can exit quickly when keepalive_running becomes False
        check_interval = 1  # Check every 1 second for fast shutdown
        elapsed = 0

        while self.keepalive_running:
            time.sleep(check_interval)
            elapsed += check_interval

            # Only ping connections every 5 minutes (300 seconds)
            if elapsed >= self.keepalive_interval:
                elapsed = 0
                with self.lock:
                    # Ping all connections that haven't been used recently
                    for conn in self.connections:
                        try:
                            # Check if connection has been idle for more than 4 minutes
                            if hasattr(conn, 'last_activity') and conn.last_activity:
                                idle_time = time.time() - conn.last_activity
                                if idle_time > 240:  # 4 minutes
                                    # Ping to keep alive
                                    conn.is_alive()
                        except:
                            pass

    def get_connection(self, max_wait_seconds: int = 30):
        """Get an available connection or create a new one - validates connection health with timeout"""
        start_time = time.time()

        while True:
            with self.lock:
                # Try to reuse available connection, but validate it first
                while self.available:
                    conn = self.available.pop()
                    # Validate connection health
                    if conn.is_alive():
                        return conn
                    else:
                        # Connection is dead, try to reconnect
                        if conn.reconnect():
                            return conn
                        else:
                            # Remove dead connection from pool
                            try:
                                self.connections.remove(conn)
                            except ValueError:
                                pass

                # Create new connection if under limit
                if len(self.connections) < self.max_connections:
                    try:
                        conn = self.connector_class(**self.connection_params)
                        if conn.connect():
                            self.connections.append(conn)
                            return conn
                        else:
                            log_detailed_error(
                                f"Connection Pool - Failed to connect",
                                Exception("Connection attempt returned False"),
                                {
                                    "Connector": self.connector_class.__name__,
                                    "Connection Params": {k: v for k, v in self.connection_params.items() if
                                                          k not in ['password', 'private_key_password']}
                                }
                            )
                            return None
                    except Exception as e:
                        log_detailed_error(
                            f"Connection Pool - Exception during connection",
                            e,
                            {
                                "Connector": self.connector_class.__name__,
                                "Connection Params": {k: v for k, v in self.connection_params.items() if
                                                      k not in ['password', 'private_key_password']}
                            }
                        )
                        return None

            # Check if we've exceeded max wait time
            elapsed = time.time() - start_time
            if elapsed >= max_wait_seconds:
                log_detailed_error(
                    "Connection Pool Exhausted",
                    Exception(f"No connections available after {max_wait_seconds}s"),
                    {
                        "Connector": self.connector_class.__name__,
                        "Total Connections": len(self.connections),
                        "Available": len(self.available),
                        "Max Connections": self.max_connections,
                        "Waited": f"{elapsed:.1f}s"
                    }
                )
                return None

            # Wait a bit and retry if pool is exhausted
            time.sleep(0.1)

    def return_connection(self, conn):
        """Return connection to pool"""
        with self.lock:
            if conn and conn not in self.available:
                self.available.append(conn)

    def close_all(self, force_skip_disconnect=False):
        """Close all connections and stop keepalive thread"""
        self.keepalive_running = False
        if self.keepalive_thread:
            # Wait up to 3 seconds for keepalive thread to exit (check_interval is 1s)
            self.keepalive_thread.join(timeout=3)

        with self.lock:
            if force_skip_disconnect:
                # Abandoned threads are still using connections - don't try to close them
                # Just clear the lists and let Python/OS clean up when threads finish
                print_safe("      ⚠️  Skipping connection disconnect (abandoned threads detected)")
                self.connections.clear()
                self.available.clear()
            else:
                # Normal cleanup - disconnect all connections
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

        # Create connection pool for each role
        for role in roles:
            self.pools[role] = ConnectionPool(
                SnowflakeConnector,
                {
                    'account': account,
                    'user': user,
                    'private_key_path': private_key_path,
                    'private_key_password': private_key_password,
                    'role': role,
                    'warehouse': warehouse,
                    'database': database,
                    'schema': schema
                },
                max_connections=max_connections
            )

    def get_connection(self, role: str = None):
        """Get connection for specific role, or use first role if not specified"""
        if role is None:
            role = self.roles[0]
        return self.pools[role].get_connection()

    def return_connection(self, conn, role: str = None):
        """Return connection to appropriate pool"""
        if role is None:
            role = self.roles[0]
        self.pools[role].return_connection(conn)

    def get_all_roles(self) -> List[str]:
        """Get list of all available roles"""
        return self.roles

    def close_all(self, force_skip_disconnect=False):
        """Close all connection pools"""
        for pool in self.pools.values():
            pool.close_all(force_skip_disconnect=force_skip_disconnect)


def is_access_error(error_message: str) -> bool:
    """Check if error is related to access/authorization issues"""
    error_lower = str(error_message).lower()
    access_patterns = [
        'does not exist',
        'not authorized',
        'insufficient privileges',
        'access denied',
        'permission denied',
        'authorization error',
        'not found',
        'object does not exist',
        'table or view does not exist'
    ]
    return any(pattern in error_lower for pattern in access_patterns)


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
        """Establish database connection"""
        try:
            self.conn = oracledb.connect(
                user=self.user,
                password=self.password,
                dsn=self.dsn
            )
            self.last_activity = time.time()
            return True
        except Exception as e:
            log_detailed_error(
                "Oracle Connection Failed",
                e,
                {
                    "User": self.user,
                    "DSN": self.dsn,
                    "Schema": self.schema
                }
            )
            return False

    def is_alive(self) -> bool:
        """Check if connection is still alive"""
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
        """Reconnect to database"""
        try:
            self.disconnect()
        except:
            pass
        return self.connect()

    def ensure_connected(self) -> bool:
        """Ensure connection is alive, reconnect if needed"""
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
        """Get table columns with detailed error logging and retry logic"""
        max_retries = 3
        for attempt in range(max_retries):
            try:
                # Ensure connection is alive
                if not self.ensure_connected():
                    if attempt < max_retries - 1:
                        time.sleep(2 ** attempt)  # Exponential backoff
                        continue
                    else:
                        raise Exception("Failed to establish Oracle connection after retries")

                cursor = self.conn.cursor()
                query = f"""
                    SELECT column_name, data_type, nullable
                    FROM all_tab_columns
                    WHERE table_name = :table_name
                    AND owner = :schema
                    ORDER BY column_id
                """
                cursor.execute(query, {'table_name': table_name.upper(), 'schema': self.schema.upper()})
                columns = []
                for row in cursor.fetchall():
                    columns.append({
                        'name': row[0],
                        'type': row[1],
                        'nullable': row[2] == 'Y'
                    })
                cursor.close()
                self.last_activity = time.time()

                if not columns:
                    error_msg = f"Table {table_name} not found in schema {self.schema}"
                    log_detailed_error(
                        f"Oracle - Table Not Found",
                        Exception(error_msg),
                        {
                            "Table": table_name,
                            "Schema": self.schema,
                            "Possible Reason": "Table does not exist in Oracle source database"
                        }
                    )
                    raise Exception(error_msg)

                return columns
            except Exception as e:
                error_str = str(e)
                # Check if it's a connection error
                if "not connected" in error_str.lower() or "dpy-1001" in error_str.lower():
                    if attempt < max_retries - 1:
                        print_safe(f"      ⚠️  Connection error on attempt {attempt + 1}, retrying...")
                        time.sleep(2 ** attempt)
                        continue

                if "not found" not in error_str.lower():
                    log_detailed_error(
                        f"Oracle - Failed to retrieve columns for table {table_name}",
                        e,
                        {
                            "Table": table_name,
                            "Schema": self.schema,
                            "Attempt": attempt + 1
                        }
                    )
                raise

    def get_columns_from_query(self, query: str) -> List[Dict]:
        """Get columns from a SQL query result with detailed error logging and retry logic"""
        max_retries = 3
        for attempt in range(max_retries):
            try:
                # Ensure connection is alive
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

                log_detailed_error(
                    "Oracle - Failed to retrieve columns from query",
                    e,
                    {
                        "Query": query[:200] + "..." if len(query) > 200 else query,
                        "Possible Reason": "Query syntax error or query references non-existent objects",
                        "Attempt": attempt + 1
                    }
                )
                raise

    def get_all_tables(self) -> List[str]:
        """Get all table names in schema with detailed error logging and retry logic"""
        max_retries = 3
        for attempt in range(max_retries):
            try:
                # Ensure connection is alive
                if not self.ensure_connected():
                    if attempt < max_retries - 1:
                        time.sleep(2 ** attempt)
                        continue
                    else:
                        raise Exception("Failed to establish Oracle connection after retries")

                cursor = self.conn.cursor()
                cursor.execute(f"SELECT table_name FROM all_tables WHERE owner = :schema ORDER BY table_name",
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

                log_detailed_error(
                    "Oracle - Failed to retrieve table list",
                    e,
                    {
                        "Schema": self.schema,
                        "Possible Reason": "Schema does not exist or no access permissions",
                        "Attempt": attempt + 1
                    }
                )
                raise

    def get_constraints(self, table_name: str) -> Dict:
        """Get all constraints for a table"""
        constraints = {
            'primary_keys': [],
            'unique_constraints': [],
            'check_constraints': [],
            'not_null_columns': [],
            'foreign_keys': []
        }

        try:
            cursor = self.conn.cursor()

            # Primary Keys
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

            # Unique Constraints
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
                constraint_name = row[0]
                column_name = row[1]
                if constraint_name not in unique_dict:
                    unique_dict[constraint_name] = []
                unique_dict[constraint_name].append(column_name)

            constraints['unique_constraints'] = [
                {'name': name, 'columns': cols}
                for name, cols in unique_dict.items()
            ]

            # Check Constraints
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
                    constraints['check_constraints'].append({
                        'name': row[0],
                        'condition': condition
                    })

            # NOT NULL columns
            cursor.execute("""
                           SELECT column_name
                           FROM all_tab_columns
                           WHERE table_name = :table_name
                             AND owner = :schema
                             AND nullable = 'N'
                           ORDER BY column_id
                           """, {'table_name': table_name.upper(), 'schema': self.schema.upper()})

            constraints['not_null_columns'] = [row[0] for row in cursor.fetchall()]

            # Foreign Keys
            cursor.execute("""
                           SELECT cons.constraint_name,
                                  cols.column_name,
                                  ref_cons.table_name  as referenced_table,
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
                    fk_dict[fk_name] = {
                        'columns': [],
                        'referenced_table': row[2],
                        'referenced_columns': []
                    }
                fk_dict[fk_name]['columns'].append(row[1])
                fk_dict[fk_name]['referenced_columns'].append(row[3])

            constraints['foreign_keys'] = [
                {
                    'name': name,
                    'columns': fk_info['columns'],
                    'referenced_table': fk_info['referenced_table'],
                    'referenced_columns': fk_info['referenced_columns']
                }
                for name, fk_info in fk_dict.items()
            ]

            cursor.close()
        except Exception as e:
            log_detailed_error(
                f"Oracle - Failed to get constraints for table {table_name}",
                e,
                {
                    "Table": table_name,
                    "Schema": self.schema
                }
            )

        return constraints


class SnowflakeConnector:
    """Snowflake connector with key-pair authentication"""

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
        """Establish Snowflake connection"""
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
                user=self.user,
                account=self.account,
                private_key=pkb,
                warehouse=self.warehouse,
                database=self.database,
                schema=self.schema,
                role=self.role
            )
            self.last_activity = time.time()
            return True
        except Exception as e:
            log_detailed_error(
                "Snowflake Connection Failed",
                e,
                {
                    "Account": self.account,
                    "User": self.user,
                    "Role": self.role,
                    "Warehouse": self.warehouse,
                    "Database": self.database,
                    "Schema": self.schema,
                    "Private Key Path": self.private_key_path
                }
            )
            return False

    def is_alive(self) -> bool:
        """Check if connection is still alive"""
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
        """Reconnect to Snowflake"""
        try:
            self.disconnect()
        except:
            pass
        return self.connect()

    def ensure_connected(self) -> bool:
        """Ensure connection is alive, reconnect if needed"""
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
        """Check if table exists in Snowflake with retry logic"""
        max_retries = 3
        for attempt in range(max_retries):
            try:
                # Ensure connection is alive
                if not self.ensure_connected():
                    if attempt < max_retries - 1:
                        time.sleep(2 ** attempt)
                        continue
                    else:
                        return False

                cursor = self.conn.cursor()
                # Use parameterized query with proper identifier quoting
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
                # If we can't check after all retries, assume it doesn't exist
                return False

    def get_columns(self, table_name: str) -> List[Dict]:
        """Get columns - raises exception on access errors for role fallback, with retry logic"""
        max_retries = 3
        for attempt in range(max_retries):
            try:
                # Ensure connection is alive
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
                    columns.append({
                        'name': row[0],
                        'type': row[1],
                        'nullable': row[3] == 'Y'
                    })
                cursor.close()
                self.last_activity = time.time()

                if not columns:
                    error_msg = f"Table {table_name} not found in Snowflake {self.database}.{self.schema}"
                    log_detailed_error(
                        "Snowflake - Table Not Found",
                        Exception(error_msg),
                        {
                            "Table": table_name,
                            "Database": self.database,
                            "Schema": self.schema,
                            "Role": self.role,
                            "Possible Reason": "Table does not exist in Snowflake target or role lacks permission"
                        }
                    )
                    raise Exception(error_msg)

                return columns
            except Exception as e:
                error_str = str(e)
                # Check if it's a connection error
                if "not connected" in error_str.lower():
                    if attempt < max_retries - 1:
                        print_safe(f"      ⚠️  Connection error on attempt {attempt + 1}, retrying...")
                        time.sleep(2 ** attempt)
                        continue

                if "not found" not in error_str.lower():
                    log_detailed_error(
                        f"Snowflake - Failed to retrieve columns for table {table_name}",
                        e,
                        {
                            "Table": table_name,
                            "Database": self.database,
                            "Schema": self.schema,
                            "Role": self.role,
                            "Attempt": attempt + 1
                        }
                    )
                raise

    def get_constraints(self, table_name: str) -> Dict:
        """Get all constraints for a table using DESCRIBE TABLE - Snowflake"""
        constraints = {
            'primary_keys': [],
            'unique_constraints': [],
            'check_constraints': [],
            'not_null_columns': [],
            'foreign_keys': []
        }

        try:
            cursor = self.conn.cursor()

            # Get all information from DESCRIBE TABLE
            cursor.execute(f'DESCRIBE TABLE "{self.database}"."{self.schema}"."{table_name}"')
            describe_results = cursor.fetchall()

            # Process DESCRIBE TABLE results
            # Columns: name, type, kind, null?, default, primary key, unique key, check, expression, comment, policy name
            for row in describe_results:
                column_name = row[0]
                is_nullable = row[3]  # 'Y' or 'N'
                is_primary_key = row[5]  # 'Y' or 'N'
                is_unique_key = row[6]  # 'Y' or 'N'
                check_constraint = row[7] if len(row) > 7 else None

                # Primary Keys
                if is_primary_key == 'Y':
                    constraints['primary_keys'].append(column_name)

                # Unique Constraints
                if is_unique_key == 'Y':
                    constraints['unique_constraints'].append({
                        'name': f'UNIQUE_{column_name}',
                        'columns': [column_name]
                    })

                # NOT NULL columns
                if is_nullable == 'N':
                    constraints['not_null_columns'].append(column_name)

                # Check Constraints
                if check_constraint:
                    constraints['check_constraints'].append({
                        'name': f'CHECK_{column_name}',
                        'condition': check_constraint
                    })

            # Get Foreign Keys using SHOW commands
            try:
                cursor.execute(f'SHOW IMPORTED KEYS IN TABLE "{self.database}"."{self.schema}"."{table_name}"')
                fk_results = cursor.fetchall()

                fk_dict = {}
                for row in fk_results:
                    # SHOW IMPORTED KEYS columns vary, typically:
                    # pk_database, pk_schema, pk_table, pk_column, fk_database, fk_schema, fk_table, fk_column, key_sequence, fk_name
                    if len(row) >= 10:
                        fk_name = row[9] if row[9] else f"FK_{row[7]}"
                        fk_column = row[7]
                        pk_table = row[2]
                        pk_column = row[3]

                        if fk_name not in fk_dict:
                            fk_dict[fk_name] = {
                                'columns': [],
                                'referenced_table': pk_table,
                                'referenced_columns': []
                            }
                        fk_dict[fk_name]['columns'].append(fk_column)
                        fk_dict[fk_name]['referenced_columns'].append(pk_column)

                constraints['foreign_keys'] = [
                    {
                        'name': name,
                        'columns': fk_info['columns'],
                        'referenced_table': fk_info['referenced_table'],
                        'referenced_columns': fk_info['referenced_columns']
                    }
                    for name, fk_info in fk_dict.items()
                ]
            except Exception:
                # SHOW IMPORTED KEYS might not be supported or table has no FKs
                pass

            cursor.close()
        except Exception as e:
            log_detailed_error(
                f"Snowflake - Failed to get constraints for table {table_name}",
                e,
                {
                    "Table": table_name,
                    "Database": self.database,
                    "Schema": self.schema,
                    "Role": self.role
                }
            )

        return constraints

    def get_table_last_altered(self, table_name: str) -> Optional[str]:
        """Get the last altered timestamp for a Snowflake table"""
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


class DataValidator:
    """Optimized data validation with parallel table processing and multi-role support"""

    def __init__(self, oracle_pool: ConnectionPool, snowflake_pool_manager,
                 tables: List[str], sf_table_prefix: str = "", excluded_columns: List[str] = None,
                 format_type: str = 'table_list', sql_query: str = None, sf_target_table: str = None,
                 oracle_table_from_query: str = None,
                 use_prefix: bool = True, table_mapping: Dict[str, str] = None,
                 parallel_execution: bool = True, max_parallel_tables: int = 5,
                 table_timeout: int = 60, max_retries: int = 3,
                 field_name_transformation: bool = False,
                 transformation_rules: List[Tuple[str, str]] = None):
        self.oracle_pool = oracle_pool
        self.snowflake_pool_manager = snowflake_pool_manager  # Can be ConnectionPool or MultiRoleSnowflakePoolManager
        self.is_multi_role = isinstance(snowflake_pool_manager, MultiRoleSnowflakePoolManager)
        self.tables = tables
        self.sf_table_prefix = sf_table_prefix
        self.use_prefix = use_prefix
        self.table_mapping = table_mapping or {}
        self.excluded_columns = [col.upper() for col in (excluded_columns or [])]
        self.format_type = format_type
        self.sql_query = sql_query
        self.sf_target_table = sf_target_table
        self.oracle_table_from_query = oracle_table_from_query  # Oracle source table from query
        self.parallel_execution = parallel_execution
        self.max_parallel_tables = max_parallel_tables
        self.table_timeout = table_timeout  # Timeout per table in seconds
        self.max_retries = max_retries  # Max retry attempts for timeout tables
        self.field_name_transformation = field_name_transformation  # Field name transformation flag
        self.transformation_rules = transformation_rules or []  # Store transformation rules
        self.results_lock = Lock()
        self.has_abandoned_threads = False  # Track if we abandoned any threads due to timeout
        self.results = {
            'metadata': {
                'validation_timestamp': datetime.now(EASTERN).isoformat(),
                'validation_type': 'Deep Validation (Parallel)' if parallel_execution else 'Deep Validation (Sequential)',
                'format_type': format_type,
                'total_tables': len(tables) if format_type == 'table_list' else 1,
                'excluded_columns': self.excluded_columns,
                'parallel_execution': parallel_execution,
                'max_parallel_workers': max_parallel_tables if parallel_execution else 1,
                'multi_role_enabled': self.is_multi_role,
                'table_timeout_seconds': table_timeout,
                'max_retries': max_retries,
                'field_name_transformation_enabled': field_name_transformation  # NEW: Include in metadata
            },
            'summary': {},
            'table_validations': [],
            'faulty_tables': []  # Tables that consistently timeout
        }

    def _create_column_name_mapping(self, oracle_columns: List[Dict]) -> Dict[str, str]:

        if not self.field_name_transformation:
            return {}

        mapping = {}
        for col in oracle_columns:
            original_name = col['name']
            transformed_name = transform_column_name(original_name, self.transformation_rules)
            mapping[transformed_name.upper()] = original_name

        return mapping

    def _filter_excluded_columns(self, columns: List[Dict]) -> List[Dict]:
        if not self.excluded_columns:
            return columns
        return [col for col in columns if col['name'].upper() not in self.excluded_columns]

    def _get_row_counts_parallel(self, oracle_conn, sf_conn, oracle_source: str,
                                 sf_table: str, is_query: bool = False):
        def get_oracle_count():
            try:
                if is_query:
                    # Validate subquery doesn't contain unbound bind variables
                    if ':' in oracle_source and 'TO_DATE' not in oracle_source.upper():
                        # Simple check for bind variables (excluding TO_DATE which uses : for time)
                        import re
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
                    e,
                    {
                        "Source": oracle_source[:200] + "..." if is_query and len(
                            oracle_source) > 200 else oracle_source,
                        "Is Query": is_query,
                        "Schema": oracle_conn.schema if not is_query else "N/A"
                    }
                )
                return None

        def get_sf_count():
            try:
                query = f'SELECT COUNT(*) FROM "{sf_conn.database}"."{sf_conn.schema}"."{sf_table}"'
                cursor = sf_conn.conn.cursor()
                cursor.execute(query)
                count = cursor.fetchone()[0]
                cursor.close()
                return count
            except Exception as e:
                log_detailed_error(
                    f"Snowflake - Failed to get row count for {sf_table}",
                    e,
                    {
                        "Table": sf_table,
                        "Database": sf_conn.database,
                        "Schema": sf_conn.schema
                    }
                )
                return None

        with ThreadPoolExecutor(max_workers=5) as executor:
            future_oracle = executor.submit(get_oracle_count)
            future_sf = executor.submit(get_sf_count)
            return future_oracle.result(), future_sf.result()

    def _analyze_column_statistics(self, oracle_conn, sf_conn, oracle_columns: List[Dict],
                                   snowflake_columns: List[Dict], oracle_source: str,
                                   sf_table: str, is_query: bool = False):
        # Build matching columns list using transformation if enabled
        if self.field_name_transformation:
            # Transform Oracle names for matching with Snowflake
            oracle_col_map = {transform_column_name(col['name'], self.transformation_rules).upper(): col for col in
                              oracle_columns}
            snowflake_col_map = {col['name'].upper(): col for col in snowflake_columns}
            common_col_names = set(oracle_col_map.keys()) & set(snowflake_col_map.keys())

            # Create list of matched pairs (Oracle col object, Snowflake col object, matched name)
            matched_cols = []
            for common_name in common_col_names:
                matched_cols.append({
                    'oracle_col': oracle_col_map[common_name],
                    'sf_col': snowflake_col_map[common_name],
                    'matched_name': common_name
                })
        else:
            # No transformation - direct matching
            oracle_col_names = {col['name'].upper() for col in oracle_columns}
            snowflake_col_names = {col['name'].upper() for col in snowflake_columns}
            common_col_names = oracle_col_names & snowflake_col_names

            matched_cols = []
            for col_name in common_col_names:
                oracle_col = next((c for c in oracle_columns if c['name'].upper() == col_name), None)
                sf_col = next((c for c in snowflake_columns if c['name'].upper() == col_name), None)
                if oracle_col and sf_col:
                    matched_cols.append({
                        'oracle_col': oracle_col,
                        'sf_col': sf_col,
                        'matched_name': col_name
                    })

        if not matched_cols:
            return []

        # Filter out LOB columns that can't be used in COUNT(DISTINCT)
        lob_types = {'CLOB', 'BLOB', 'NCLOB', 'BFILE', 'LONG', 'LONG RAW'}
        filtered_matched_cols = []
        skipped_lobs = []

        for match in matched_cols:
            oracle_col = match['oracle_col']
            col_type = oracle_col.get('type', '').upper()
            is_lob = any(lob in col_type for lob in lob_types)

            if is_lob:
                skipped_lobs.append(f"{oracle_col['name']} ({col_type})")
            else:
                filtered_matched_cols.append(match)

        if skipped_lobs and filtered_matched_cols:
            # Only show message if we have some columns left to process
            print_safe(f"     ℹ️  Skipping LOB columns from statistics: {', '.join(skipped_lobs[:3])}")
            if len(skipped_lobs) > 3:
                print_safe(f"        ... and {len(skipped_lobs) - 3} more")

        if not filtered_matched_cols:
            return []

        matched_cols = filtered_matched_cols
        column_statistics = []

        # Build combined query for Oracle (use ORIGINAL names with proper quoting)
        oracle_stat_parts = []
        for match in matched_cols:
            oracle_col = match['oracle_col']
            # IMPORTANT: Quote Oracle column name to handle spaces and special characters
            col_ref = f'"{oracle_col["name"]}"'
            # Use safe alias for result column
            safe_col_alias = match['matched_name'].replace(' ', '_').replace('-', '_')
            oracle_stat_parts.append(f'COUNT(DISTINCT {col_ref}) as {safe_col_alias}_DISTINCT')
            oracle_stat_parts.append(f'SUM(CASE WHEN {col_ref} IS NULL THEN 1 ELSE 0 END) as {safe_col_alias}_NULLS')

        if is_query:
            combined_oracle_query = f"SELECT /*+ PARALLEL(8) */ {', '.join(oracle_stat_parts)} FROM ({oracle_source})"
        else:
            combined_oracle_query = f'SELECT /*+ PARALLEL(8) */ {", ".join(oracle_stat_parts)} FROM "{oracle_conn.schema}"."{oracle_source}"'

        # Build combined query for Snowflake (use Snowflake names as-is)
        sf_stat_parts = []
        for match in matched_cols:
            sf_col = match['sf_col']
            # Use Snowflake column name (already transformed)
            safe_col_alias = match['matched_name'].replace(' ', '_').replace('-', '_')
            sf_stat_parts.append(f'COUNT(DISTINCT "{sf_col["name"]}") as {safe_col_alias}_DISTINCT')
            sf_stat_parts.append(
                f'SUM(CASE WHEN "{sf_col["name"]}" IS NULL THEN 1 ELSE 0 END) as {safe_col_alias}_NULLS')

        combined_sf_query = f'SELECT {", ".join(sf_stat_parts)} FROM "{sf_conn.database}"."{sf_conn.schema}"."{sf_table}"'

        # Execute in parallel
        def run_oracle_stats():
            try:
                cursor = oracle_conn.conn.cursor()
                cursor.execute(combined_oracle_query)
                result = cursor.fetchone()
                cursor.close()
                return result
            except Exception as e:
                log_detailed_error(
                    f"Oracle - Failed to get column statistics for {oracle_source}",
                    e,
                    {
                        "Source": oracle_source,
                        "Columns": [m['matched_name'] for m in matched_cols[:5]]
                    }
                )
                return None

        def run_sf_stats():
            try:
                cursor = sf_conn.conn.cursor()
                cursor.execute(combined_sf_query)
                result = cursor.fetchone()
                cursor.close()
                return result
            except Exception as e:
                log_detailed_error(
                    f"Snowflake - Failed to get column statistics for {sf_table}",
                    e,
                    {
                        "Table": sf_table,
                        "Columns": [m['matched_name'] for m in matched_cols[:5]]
                    }
                )
                return None

        with ThreadPoolExecutor(max_workers=5) as executor:
            future_oracle = executor.submit(run_oracle_stats)
            future_sf = executor.submit(run_sf_stats)
            oracle_stats_result = future_oracle.result()
            sf_stats_result = future_sf.result()

        if oracle_stats_result and sf_stats_result:
            for i, match in enumerate(matched_cols):
                try:
                    oracle_col = match['oracle_col']
                    sf_col = match['sf_col']
                    matched_name = match['matched_name']

                    # Convert None to 0 to handle NULL results from database
                    oracle_distinct = oracle_stats_result[i * 2] if oracle_stats_result[i * 2] is not None else 0
                    oracle_nulls = oracle_stats_result[i * 2 + 1] if oracle_stats_result[i * 2 + 1] is not None else 0
                    sf_distinct = sf_stats_result[i * 2] if sf_stats_result[i * 2] is not None else 0
                    sf_nulls = sf_stats_result[i * 2 + 1] if sf_stats_result[i * 2 + 1] is not None else 0

                    column_stat = {
                        'column_name': matched_name,
                        'oracle_type': oracle_col['type'] if oracle_col else 'UNKNOWN',
                        'snowflake_type': sf_col['type'] if sf_col else 'UNKNOWN',
                        'oracle_stats': {
                            'distinct_count': oracle_distinct,
                            'null_count': oracle_nulls
                        },
                        'snowflake_stats': {
                            'distinct_count': sf_distinct,
                            'null_count': sf_nulls
                        },
                        'stats_match': {
                            'distinct_count': (oracle_distinct == sf_distinct),
                            'null_count': (oracle_nulls == sf_nulls)
                        }
                    }

                    # Add original names if transformation was applied
                    if self.field_name_transformation:
                        column_stat['oracle_original_name'] = oracle_col['name']
                        column_stat['snowflake_name'] = sf_col['name']

                    column_statistics.append(column_stat)
                except Exception:
                    continue

        return column_statistics

    def _get_overall_null_count_parallel(self, oracle_conn, sf_conn, oracle_source: str,
                                         sf_table: str, oracle_columns: List[Dict], sf_columns: List[Dict],
                                         is_query: bool = False):
        """Count total nulls across all cells in both tables in parallel"""

        def count_oracle_nulls():
            try:
                null_checks = []
                for col in oracle_columns:
                    # IMPORTANT: Quote Oracle column name to handle spaces and special characters
                    col_ref = f'"{col["name"]}"'
                    null_checks.append(f'SUM(CASE WHEN {col_ref} IS NULL THEN 1 ELSE 0 END)')

                if is_query:
                    query = f"SELECT /*+ PARALLEL(8) */ {' + '.join(null_checks)} as TOTAL_NULLS FROM ({oracle_source})"
                else:
                    query = f'SELECT /*+ PARALLEL(8) */ {" + ".join(null_checks)} as TOTAL_NULLS FROM "{oracle_conn.schema}"."{oracle_source}"'

                cursor = oracle_conn.conn.cursor()
                cursor.execute(query)
                result = cursor.fetchone()[0]
                cursor.close()
                return result if result is not None else 0
            except Exception as e:
                log_detailed_error(
                    f"Oracle - Failed to get null count for {oracle_source}",
                    e,
                    {
                        "Source": oracle_source
                    }
                )
                return None

        def count_sf_nulls():
            try:
                null_checks = []
                for col in sf_columns:
                    null_checks.append(f'SUM(CASE WHEN "{col["name"]}" IS NULL THEN 1 ELSE 0 END)')

                query = f'SELECT {" + ".join(null_checks)} as TOTAL_NULLS FROM "{sf_conn.database}"."{sf_conn.schema}"."{sf_table}"'

                cursor = sf_conn.conn.cursor()
                cursor.execute(query)
                result = cursor.fetchone()[0]
                cursor.close()
                return result if result is not None else 0
            except Exception as e:
                log_detailed_error(
                    f"Snowflake - Failed to get null count for {sf_table}",
                    e,
                    {
                        "Table": sf_table
                    }
                )
                return None

        with ThreadPoolExecutor(max_workers=5) as executor:
            future_oracle = executor.submit(count_oracle_nulls)
            future_sf = executor.submit(count_sf_nulls)
            return future_oracle.result(), future_sf.result()

    def _get_duplicate_count_parallel(self, oracle_conn, sf_conn, oracle_source: str,
                                      sf_table: str, oracle_columns: List[Dict], sf_columns: List[Dict],
                                      is_query: bool = False):
        """Count duplicate rows based on all columns in parallel"""

        def count_oracle_duplicates():
            try:
                # Filter out LOB columns that can't be used in GROUP BY
                lob_types = {'CLOB', 'BLOB', 'NCLOB', 'BFILE', 'LONG', 'LONG RAW'}
                filtered_oracle_columns = [
                    col for col in oracle_columns
                    if not any(lob in col.get('type', '').upper() for lob in lob_types)
                ]

                if not filtered_oracle_columns:
                    # If only LOB columns, can't detect duplicates
                    return 0

                # IMPORTANT: Quote Oracle column names to handle spaces and special characters
                col_list = ', '.join([f'"{col["name"]}"' for col in filtered_oracle_columns])

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
                log_detailed_error(
                    f"Oracle - Failed to get duplicate count for {oracle_source}",
                    e,
                    {
                        "Source": oracle_source
                    }
                )
                return None

        def count_sf_duplicates():
            try:
                # Filter out LOB columns that can't be used in GROUP BY
                lob_types = {'CLOB', 'BLOB', 'NCLOB', 'BFILE', 'LONG', 'LONG RAW'}
                filtered_sf_columns = [
                    col for col in sf_columns
                    if not any(lob in col.get('type', '').upper() for lob in lob_types)
                ]

                if not filtered_sf_columns:
                    # If only LOB columns, can't detect duplicates
                    return 0

                col_list = ', '.join([f'"{col["name"]}"' for col in filtered_sf_columns])

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
                log_detailed_error(
                    f"Snowflake - Failed to get duplicate count for {sf_table}",
                    e,
                    {
                        "Table": sf_table
                    }
                )
                return None

        with ThreadPoolExecutor(max_workers=5) as executor:
            future_oracle = executor.submit(count_oracle_duplicates)
            future_sf = executor.submit(count_sf_duplicates)
            return future_oracle.result(), future_sf.result()

    def _analyze_constraints(self, oracle_conn, sf_conn, oracle_source: str,
                             sf_table: str, is_query: bool = False) -> Dict:
        """Analyze and compare constraints between Oracle and Snowflake"""
        # Track counts for warnings (will convert to "Source=X, Snowflake=Y" format)
        constraint_counts = {
            'primary_keys': {'source': 0, 'snowflake': 0},
            'foreign_keys': {'source': 0, 'snowflake': 0},
            'unique_constraints': {'source': 0, 'snowflake': 0},
            'check_constraints': {'source': 0, 'snowflake': 0},
            'not_null_constraints': {'source': 0, 'snowflake': 0}
        }

        constraint_details = {
            'oracle': {},
            'snowflake': {}
        }

        validation_results = {
            'primary_keys': {'status': 'UNKNOWN', 'details': None},
            'unique_constraints': {'status': 'UNKNOWN', 'details': None},
            'check_constraints': {'status': 'UNKNOWN', 'details': None},
            'not_null_constraints': {'status': 'UNKNOWN', 'details': []},
            'foreign_keys': {'status': 'UNKNOWN', 'details': None}
        }

        try:
            # Get constraints from both systems
            if not is_query:
                oracle_constraints = oracle_conn.get_constraints(oracle_source)
                constraint_details['oracle'] = oracle_constraints
            else:
                oracle_constraints = {
                    'primary_keys': [],
                    'unique_constraints': [],
                    'check_constraints': [],
                    'not_null_columns': [],
                    'foreign_keys': []
                }

            sf_constraints = sf_conn.get_constraints(sf_table)
            constraint_details['snowflake'] = sf_constraints

            # ==================== PRIMARY KEYS ====================
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

            # ==================== UNIQUE CONSTRAINTS ====================
            oracle_unique_count = len(oracle_constraints['unique_constraints'])
            sf_unique_count = len(sf_constraints['unique_constraints'])

            if oracle_unique_count == sf_unique_count:
                validation_results['unique_constraints']['status'] = 'MATCH'
                validation_results['unique_constraints'][
                    'details'] = f"Unique constraint count matches: {oracle_unique_count}"
            else:
                validation_results['unique_constraints']['status'] = 'MISMATCH'
                msg = f"Unique constraint count mismatch: Oracle={oracle_unique_count}, Snowflake={sf_unique_count}"
                validation_results['unique_constraints']['details'] = msg
                # Track which constraints exist only in one system
                if oracle_unique_count > sf_unique_count:
                    constraint_counts['unique_constraints']['source'] = oracle_unique_count - sf_unique_count
                if sf_unique_count > oracle_unique_count:
                    constraint_counts['unique_constraints']['snowflake'] = sf_unique_count - oracle_unique_count

            # ==================== NOT NULL CONSTRAINTS ====================
            oracle_nn = set(oracle_constraints['not_null_columns'])
            sf_nn = set(sf_constraints['not_null_columns'])

            nn_only_oracle = oracle_nn - sf_nn
            nn_only_sf = sf_nn - oracle_nn

            # Track NOT NULL constraints only in Oracle
            if nn_only_oracle:
                constraint_counts['not_null_constraints']['source'] = len(nn_only_oracle)

            # For each NOT NULL column in Oracle, validate actual data in Snowflake
            cursor = sf_conn.conn.cursor()

            for col in oracle_nn:
                try:
                    # Check for NULL values in Snowflake - simplified query
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
                    validation_results['not_null_constraints']['details'].append({
                        'column': col,
                        'error': str(e)
                    })

            cursor.close()

            # Track NOT NULL only in Snowflake
            if nn_only_sf:
                constraint_counts['not_null_constraints']['snowflake'] = len(nn_only_sf)

            # Set overall NOT NULL status
            failed_nn = sum(1 for d in validation_results['not_null_constraints']['details']
                            if d.get('validation_status') == 'FAIL')
            if failed_nn > 0:
                validation_results['not_null_constraints']['status'] = 'VALIDATION_FAILED'
            elif nn_only_oracle or nn_only_sf:
                validation_results['not_null_constraints']['status'] = 'DEFINITION_MISMATCH'
            else:
                validation_results['not_null_constraints']['status'] = 'MATCH'

            # ==================== CHECK CONSTRAINTS ====================
            oracle_check_count = len(oracle_constraints['check_constraints'])
            sf_check_count = len(sf_constraints['check_constraints'])

            if oracle_check_count == sf_check_count:
                validation_results['check_constraints']['status'] = 'MATCH'
                validation_results['check_constraints'][
                    'details'] = f"Check constraint count matches: {oracle_check_count}"
            else:
                validation_results['check_constraints']['status'] = 'MISMATCH'
                msg = f"Check constraint count mismatch: Oracle={oracle_check_count}, Snowflake={sf_check_count}"
                validation_results['check_constraints']['details'] = msg
                # Track which check constraints exist only in one system
                if oracle_check_count > sf_check_count:
                    constraint_counts['check_constraints']['source'] = oracle_check_count - sf_check_count
                if sf_check_count > oracle_check_count:
                    constraint_counts['check_constraints']['snowflake'] = sf_check_count - oracle_check_count

            # ==================== FOREIGN KEYS ====================
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
                msg = f"Foreign key count mismatch: Oracle={oracle_fk_count}, Snowflake={sf_fk_count}"
                validation_results['foreign_keys']['details'] = msg

                # Track FKs only in one system
                if oracle_fk_count > sf_fk_count:
                    constraint_counts['foreign_keys']['source'] = oracle_fk_count - sf_fk_count
                if sf_fk_count > oracle_fk_count:
                    constraint_counts['foreign_keys']['snowflake'] = sf_fk_count - oracle_fk_count

        except Exception as e:
            # Return error in constraint_warnings
            constraint_warnings_formatted = {'error': f"Error during constraint analysis: {str(e)}"}
            log_detailed_error(
                "Constraint Analysis Failed",
                e,
                {
                    "Oracle Source": oracle_source,
                    "Snowflake Table": sf_table
                }
            )

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

        # Convert counts to "Source=X, Snowflake=Y" format
        constraint_warnings_formatted = {}

        for constraint_type, counts in constraint_counts.items():
            source_count = counts['source']
            snowflake_count = counts['snowflake']

            # Only add if there's a mismatch (either source or snowflake has constraints the other doesn't)
            if source_count > 0 or snowflake_count > 0:
                # Format the constraint type name nicely
                type_name = constraint_type.replace('_', ' ').title()
                constraint_warnings_formatted[
                    f"{type_name} mismatch"] = f"Source={source_count}, Snowflake={snowflake_count}"

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

    def _validate_single_table(self, table: str) -> Dict:
        """Validate a single table - called in parallel, with role fallback support"""
        start_time = time.time()

        # Get Oracle connection from pool
        oracle_conn = self.oracle_pool.get_connection()
        if not oracle_conn:
            error_msg = 'Failed to get Oracle connection from pool'
            log_detailed_error(
                "Table Validation Failed - Oracle Connection",
                Exception(error_msg),
                {
                    "Table": table,
                    "Reason": "Unable to obtain Oracle connection from connection pool"
                }
            )
            return {
                'table_name': table,
                'status': 'ERROR',
                'error': error_msg,
                'validation_time': time.time() - start_time,
                'Snowflake_Last_Updated': None,
                'Last_Checked': datetime.now(EASTERN).isoformat()
            }

        # Determine table names
        is_query = (self.format_type == 'query_based')
        if is_query:
            oracle_source = self.sql_query
            sf_table = self.sf_target_table
        else:
            oracle_source = table
            if table in self.table_mapping:
                sf_table = self.table_mapping[table]
            elif self.use_prefix:
                sf_table = f"{self.sf_table_prefix}{table}"
            else:
                sf_table = table

        # Try validation with role fallback if multi-role is enabled
        if self.is_multi_role:
            roles = self.snowflake_pool_manager.get_all_roles()
            last_error = None
            last_error_details = {}

            # First, check if table exists in Snowflake with any role
            table_exists = False
            sf_conn_for_check = None
            role_with_access = None

            for role in roles:
                sf_conn_for_check = self.snowflake_pool_manager.get_connection(role)
                if sf_conn_for_check:
                    if sf_conn_for_check.table_exists(sf_table):
                        table_exists = True
                        role_with_access = role
                        print_safe(f"       ✓ Table '{sf_table}' exists in Snowflake")

                        # Get Snowflake table last updated timestamp
                        sf_last_updated = sf_conn_for_check.get_table_last_altered(sf_table)
                        break
                    else:
                        self.snowflake_pool_manager.return_connection(sf_conn_for_check, role)

            if not table_exists:
                # Table doesn't exist in Snowflake
                error_msg = f"Table '{sf_table}' does not exist in Snowflake"
                print_safe(f"       ✗ Table '{sf_table}' does not exist in Snowflake - skipping validation")
                self.oracle_pool.return_connection(oracle_conn)
                return {
                    'table_name': table if not is_query else sf_table,
                    'snowflake_table': sf_table,
                    'status': 'TABLE_NOT_FOUND',
                    'error': error_msg,
                    'validation_time': time.time() - start_time,
                    'Snowflake_Last_Updated': None,
                    'Last_Checked': datetime.now(EASTERN).isoformat()
                }

            # Table exists, proceed with validation
            for role_idx, role in enumerate(roles):
                # Use the connection we already have if it's the right role
                if role == role_with_access and sf_conn_for_check:
                    sf_conn = sf_conn_for_check
                else:
                    sf_conn = self.snowflake_pool_manager.get_connection(role)

                if not sf_conn:
                    last_error = f"Failed to get Snowflake connection for role {role}"
                    last_error_details = {
                        "Role": role,
                        "Reason": "Unable to obtain Snowflake connection from pool"
                    }
                    continue

                try:
                    result = self._perform_validation(oracle_conn, sf_conn, oracle_source, sf_table, is_query, table)

                    # Success! Return connection and result
                    self.oracle_pool.return_connection(oracle_conn)
                    self.snowflake_pool_manager.return_connection(sf_conn, role)
                    result['validation_time'] = time.time() - start_time
                    result['role_used'] = role
                    result['Snowflake_Last_Updated'] = sf_last_updated
                    result['Last_Checked'] = datetime.now(EASTERN).isoformat()
                    return result

                except Exception as e:
                    error_msg = str(e)
                    last_error = error_msg
                    last_error_details = {
                        "Table": table if not is_query else sf_table,
                        "Snowflake Table": sf_table,
                        "Role Attempted": role,
                        "Role Index": f"{role_idx + 1}/{len(roles)}"
                    }

                    # Return connection before checking if we should retry
                    self.snowflake_pool_manager.return_connection(sf_conn, role)

                    # Check if this is an access error and we have more roles to try
                    if is_access_error(error_msg) and role_idx < len(roles) - 1:
                        # Access error - try next role
                        print_safe(f"       ⚠️  Access error with role {role}, trying next role...")
                        continue
                    else:
                        # Not an access error or no more roles - fail
                        log_detailed_error(
                            f"Table Validation Failed - {table}",
                            e,
                            last_error_details
                        )
                        self.oracle_pool.return_connection(oracle_conn)
                        return {
                            'table_name': table if not is_query else sf_table,
                            'snowflake_table': sf_table,
                            'status': 'ERROR',
                            'error': error_msg,
                            'validation_time': time.time() - start_time,
                            'Snowflake_Last_Updated': sf_last_updated,
                            'Last_Checked': datetime.now(EASTERN).isoformat()
                        }

            # All roles failed
            log_detailed_error(
                f"Table Validation Failed - All Roles Exhausted for {table}",
                Exception(f'All {len(roles)} roles failed'),
                {
                    **last_error_details,
                    "Last Error": last_error,
                    "Roles Tried": roles
                }
            )
            self.oracle_pool.return_connection(oracle_conn)
            return {
                'table_name': table if not is_query else sf_table,
                'snowflake_table': sf_table,
                'status': 'ERROR',
                'error': f'All {len(roles)} roles failed. Last error: {last_error}',
                'validation_time': time.time() - start_time,
                'Snowflake_Last_Updated': sf_last_updated,
                'Last_Checked': datetime.now(EASTERN).isoformat()
            }
        else:
            # Single role mode (backward compatibility)
            sf_conn = self.snowflake_pool_manager.get_connection()
            if not sf_conn:
                error_msg = 'Failed to get Snowflake connection from pool'
                log_detailed_error(
                    "Table Validation Failed - Snowflake Connection",
                    Exception(error_msg),
                    {
                        "Table": table,
                        "Reason": "Unable to obtain Snowflake connection from connection pool"
                    }
                )
                self.oracle_pool.return_connection(oracle_conn)
                return {
                    'table_name': table,
                    'status': 'ERROR',
                    'error': error_msg,
                    'validation_time': time.time() - start_time,
                    'Snowflake_Last_Updated': None,
                    'Last_Checked': datetime.now(EASTERN).isoformat()
                }

            # Check if table exists in Snowflake
            if not sf_conn.table_exists(sf_table):
                error_msg = f"Table '{sf_table}' does not exist in Snowflake"
                print_safe(f"       ✗ Table '{sf_table}' does not exist in Snowflake - skipping validation")
                self.oracle_pool.return_connection(oracle_conn)
                self.snowflake_pool_manager.return_connection(sf_conn)
                return {
                    'table_name': table if not is_query else sf_table,
                    'snowflake_table': sf_table,
                    'status': 'TABLE_NOT_FOUND',
                    'error': error_msg,
                    'validation_time': time.time() - start_time,
                    'Snowflake_Last_Updated': None,
                    'Last_Checked': datetime.now(EASTERN).isoformat()
                }

            # Table exists, proceed with validation
            print_safe(f"       ✓ Table '{sf_table}' exists in Snowflake")

            # Get Snowflake table last updated timestamp
            sf_last_updated = sf_conn.get_table_last_altered(sf_table)

            try:
                result = self._perform_validation(oracle_conn, sf_conn, oracle_source, sf_table, is_query, table)
                self.oracle_pool.return_connection(oracle_conn)
                self.snowflake_pool_manager.return_connection(sf_conn)
                result['validation_time'] = time.time() - start_time
                result['Snowflake_Last_Updated'] = sf_last_updated
                result['Last_Checked'] = datetime.now(EASTERN).isoformat()
                return result
            except Exception as e:
                log_detailed_error(
                    f"Table Validation Failed - {table}",
                    e,
                    {
                        "Table": table if not is_query else sf_table,
                        "Snowflake Table": sf_table
                    }
                )
                self.oracle_pool.return_connection(oracle_conn)
                self.snowflake_pool_manager.return_connection(sf_conn)
                return {
                    'table_name': table if not is_query else sf_table,
                    'snowflake_table': sf_table,
                    'status': 'ERROR',
                    'error': str(e),
                    'validation_time': time.time() - start_time,
                    'Snowflake_Last_Updated': sf_last_updated,
                    'Last_Checked': datetime.now(EASTERN).isoformat()
                }

    def _perform_validation(self, oracle_conn, sf_conn, oracle_source: str, sf_table: str,
                            is_query: bool, original_table: str) -> Dict:
        """Perform the actual validation logic - can raise exceptions"""
        # Get columns (keep original names for SQL queries)
        if is_query:
            oracle_columns = oracle_conn.get_columns_from_query(oracle_source)
        else:
            oracle_columns = oracle_conn.get_columns(oracle_source)

        sf_columns = sf_conn.get_columns(sf_table)

        # Filter excluded columns
        oracle_columns = self._filter_excluded_columns(oracle_columns)
        sf_columns = self._filter_excluded_columns(sf_columns)

        if not oracle_columns or not sf_columns:
            raise Exception('Unable to retrieve columns from source or target')

        # Create column name mapping if transformation is enabled
        # This maps transformed Oracle names → original Oracle names for matching
        oracle_name_mapping = self._create_column_name_mapping(oracle_columns)

        # Compare column structures using transformed names for Oracle
        if self.field_name_transformation:
            # Transform Oracle column names for comparison only
            oracle_col_names = {transform_column_name(col['name'], self.transformation_rules).upper() for col in
                                oracle_columns}
        else:
            oracle_col_names = {col['name'].upper() for col in oracle_columns}

        snowflake_col_names = {col['name'].upper() for col in sf_columns}
        common_columns = oracle_col_names & snowflake_col_names
        oracle_only = oracle_col_names - snowflake_col_names
        snowflake_only = snowflake_col_names - oracle_col_names

        # Get common columns for further analysis - separate lists for Oracle and Snowflake
        if self.field_name_transformation:
            # Map back to original Oracle names and get matching Snowflake columns
            oracle_common_cols = []
            sf_common_cols = []

            for col in oracle_columns:
                transformed_name = transform_column_name(col['name'], self.transformation_rules).upper()
                if transformed_name in common_columns:
                    oracle_common_cols.append(col)
                    # Find matching Snowflake column
                    sf_col = next((c for c in sf_columns if c['name'].upper() == transformed_name), None)
                    if sf_col:
                        sf_common_cols.append(sf_col)
        else:
            oracle_common_cols = [col for col in oracle_columns if col['name'].upper() in common_columns]
            sf_common_cols = [col for col in sf_columns if col['name'].upper() in common_columns]

        # Parallel data validation
        oracle_count, sf_count = self._get_row_counts_parallel(
            oracle_conn, sf_conn, oracle_source, sf_table, is_query
        )

        column_statistics = self._analyze_column_statistics(
            oracle_conn, sf_conn, oracle_columns, sf_columns,
            oracle_source, sf_table, is_query
        )

        oracle_nulls, sf_nulls = self._get_overall_null_count_parallel(
            oracle_conn, sf_conn, oracle_source, sf_table, oracle_common_cols, sf_common_cols, is_query
        )

        oracle_dupes, sf_dupes = self._get_duplicate_count_parallel(
            oracle_conn, sf_conn, oracle_source, sf_table, oracle_common_cols, sf_common_cols, is_query
        )

        # Analyze constraints
        constraint_analysis = self._analyze_constraints(
            oracle_conn, sf_conn, oracle_source, sf_table, is_query
        )

        # Determine validation status
        validation_issues = []
        if oracle_count != sf_count:
            validation_issues.append(f"Row count mismatch: Oracle={oracle_count}, Snowflake={sf_count}")
        if oracle_only:
            validation_issues.append(f"Columns only in Oracle: {', '.join(sorted(oracle_only))}")
        if snowflake_only:
            validation_issues.append(f"Columns only in Snowflake: {', '.join(sorted(snowflake_only))}")
        if oracle_nulls != sf_nulls:
            validation_issues.append(f"Null count mismatch: Oracle={oracle_nulls}, Snowflake={sf_nulls}")
        if oracle_dupes != sf_dupes:
            validation_issues.append(f"Duplicate count mismatch: Oracle={oracle_dupes}, Snowflake={sf_dupes}")

        # Check column statistics
        for col_stat in column_statistics:
            if not col_stat['stats_match']['distinct_count']:
                validation_issues.append(
                    f"Column {col_stat['column_name']}: distinct count mismatch "
                    f"(Oracle={col_stat['oracle_stats']['distinct_count']}, "
                    f"Snowflake={col_stat['snowflake_stats']['distinct_count']})"
                )

        status = 'PASSED' if not validation_issues else 'CRITICAL'

        return {
            'table_name': original_table if not is_query else sf_table,
            'snowflake_table': sf_table,
            'status': status,
            'row_counts': {
                'oracle': oracle_count,
                'snowflake': sf_count,
                'match': oracle_count == sf_count
            },
            'column_analysis': {
                'oracle_columns': len(oracle_columns),
                'snowflake_columns': len(sf_columns),
                'common_columns': len(common_columns),
                'oracle_only_columns': list(sorted(oracle_only)),
                'snowflake_only_columns': list(sorted(snowflake_only))
            },
            'data_quality': {
                'oracle_nulls': oracle_nulls,
                'snowflake_nulls': sf_nulls,
                'null_match': oracle_nulls == sf_nulls,
                'oracle_duplicates': oracle_dupes,
                'snowflake_duplicates': sf_dupes,
                'duplicate_match': oracle_dupes == sf_dupes
            },
            'column_statistics': column_statistics,
            'constraint_analysis': constraint_analysis,
            'validation_issues': validation_issues
        }

    def validate_all_tables(self):
        """Validate all tables with parallel execution and timeout handling"""
        start_time = time.time()
        timeout_tables = []  # Track tables that timeout

        if self.format_type == 'query_based':
            # For query-based, we only have one "table" to validate
            print_safe(f"\n  ⏳ Validating query-based data...")
            result = self._validate_single_table_with_timeout("QUERY_RESULT", timeout=self.table_timeout)

            if result['status'] == 'TIMEOUT':
                print_safe(f"    ⏱️  Validation timed out after {self.table_timeout}s")

                # Retry logic for query-based (same as table-list)
                if self.max_retries > 0:
                    print_safe(f"    🔁 Retrying query validation (max {self.max_retries} attempts)...")

                    for retry_attempt in range(1, self.max_retries + 1):
                        print_safe(f"    🔁 Retry attempt {retry_attempt}/{self.max_retries}...")
                        result = self._validate_single_table_with_timeout("QUERY_RESULT", timeout=self.table_timeout)

                        if result['status'] == 'TIMEOUT':
                            print_safe(f"       ⏱️  Still timing out...")
                        else:
                            status_icon = "✅" if result['status'] == 'PASSED' else "🚨" if result[
                                                                                              'status'] == 'CRITICAL' else "⚠️"
                            print_safe(f"       {status_icon} Query validation - {result['status']}")
                            break

                    # If still timeout after all retries, mark as FAULTY
                    if result['status'] == 'TIMEOUT':
                        print_safe(f"    ⚠️  Query validation marked as FAULTY after {self.max_retries} retries")
                        result['status'] = 'FAULTY'
                        result[
                            'error'] = f'Query validation consistently exceeded {self.table_timeout}s timeout after {self.max_retries} retry attempts'

                        with self.results_lock:
                            # Use actual table name from result
                            self.results['faulty_tables'].append({
                                'table_name': result.get('table_name', self.sf_target_table),
                                'snowflake_table': result.get('snowflake_table', self.sf_target_table),
                                'reason': f'Validation exceeded {self.table_timeout}s timeout',
                                'retry_attempts': self.max_retries
                            })

            elif result['status'] == 'TABLE_NOT_FOUND':
                print_safe(f"    ⊘  Target table does not exist in Snowflake")
            else:
                status_icon = "✅" if result['status'] == 'PASSED' else "🚨" if result['status'] == 'CRITICAL' else "⚠️"
                print_safe(f"    {status_icon} Query validation - {result['status']}")

            with self.results_lock:
                self.results['table_validations'].append(result)
        else:
            # Table list validation
            print_safe(f"\n  ⏳ Validating {len(self.tables)} table(s)...")

            if self.parallel_execution and len(self.tables) > 1:
                # Parallel execution with timeout handling
                with ThreadPoolExecutor(max_workers=self.max_parallel_tables) as executor:
                    future_to_table = {}

                    for idx, table in enumerate(self.tables, 1):
                        future = executor.submit(self._validate_single_table_with_timeout, table,
                                                 timeout=self.table_timeout)
                        future_to_table[future] = (idx, table)

                    for future in as_completed(future_to_table):
                        idx, table = future_to_table[future]
                        result = future.result()

                        if result['status'] == 'TIMEOUT':
                            timeout_tables.append(table)
                            print_safe(
                                f"    ⏱️  [{idx}/{len(self.tables)}] {table} - Timed out after {self.table_timeout}s")
                        elif result['status'] == 'TABLE_NOT_FOUND':
                            print_safe(f"    ⊘  [{idx}/{len(self.tables)}] {table} - Table does not exist in Snowflake")
                        else:
                            status_icon = "✅" if result['status'] == 'PASSED' else "🚨" if result[
                                                                                              'status'] == 'CRITICAL' else "⚠️"
                            print_safe(
                                f"    {status_icon} [{idx}/{len(self.tables)}] {result.get('table_name', 'Unknown')} - {result['status']}")

                        with self.results_lock:
                            self.results['table_validations'].append(result)
            else:
                # Sequential execution
                for idx, table in enumerate(self.tables, 1):
                    result = self._validate_single_table_with_timeout(table, timeout=self.table_timeout)

                    if result['status'] == 'TIMEOUT':
                        timeout_tables.append(table)
                        print_safe(
                            f"    ⏱️  [{idx}/{len(self.tables)}] {table} - Timed out after {self.table_timeout}s")
                    elif result['status'] == 'TABLE_NOT_FOUND':
                        print_safe(f"    ⊘  [{idx}/{len(self.tables)}] {table} - Table does not exist in Snowflake")
                    else:
                        status_icon = "✅" if result['status'] == 'PASSED' else "🚨" if result[
                                                                                          'status'] == 'CRITICAL' else "⚠️"
                        print_safe(
                            f"    {status_icon} [{idx}/{len(self.tables)}] {result.get('table_name', 'Unknown')} - {result['status']}")

                    with self.results_lock:
                        self.results['table_validations'].append(result)

            # Retry timeout tables
            if timeout_tables:
                print_safe(
                    f"\n    🔁 Retrying {len(timeout_tables)} timeout tables (max {self.max_retries} attempts)...")

                for retry_attempt in range(1, self.max_retries + 1):
                    if not timeout_tables:
                        break

                    print_safe(f"    🔁 Retry attempt {retry_attempt}/{self.max_retries}...")
                    still_timeout = []

                    for table in timeout_tables:
                        result = self._validate_single_table_with_timeout(table, timeout=self.table_timeout)

                        if result['status'] == 'TIMEOUT':
                            still_timeout.append(table)
                            print_safe(f"       ⏱️  {table} - Still timing out...")
                        else:
                            status_icon = "✅" if result['status'] == 'PASSED' else "🚨" if result[
                                                                                              'status'] == 'CRITICAL' else "⚠️"
                            print_safe(f"       {status_icon} {table} - {result['status']}")

                            # Remove old timeout result and add new result
                            with self.results_lock:
                                self.results['table_validations'] = [
                                    r for r in self.results['table_validations']
                                    if r.get('table_name') != table
                                ]
                                self.results['table_validations'].append(result)

                    timeout_tables = still_timeout

                # Mark remaining timeout tables as FAULTY
                if timeout_tables:
                    print_safe(
                        f"    ⚠️  {len(timeout_tables)} tables marked as FAULTY after {self.max_retries} retries")
                    with self.results_lock:
                        for table in timeout_tables:
                            # Update status from TIMEOUT to FAULTY
                            snowflake_table = None
                            for result in self.results['table_validations']:
                                if result.get('table_name') == table and result['status'] == 'TIMEOUT':
                                    result['status'] = 'FAULTY'
                                    result[
                                        'error'] = f'Table validation consistently exceeded {self.table_timeout}s timeout after {self.max_retries} retry attempts'
                                    # Capture snowflake_table from result if available
                                    snowflake_table = result.get('snowflake_table')

                            faulty_entry = {
                                'table_name': table,
                                'reason': f'Validation exceeded {self.table_timeout}s timeout',
                                'retry_attempts': self.max_retries
                            }
                            if snowflake_table:
                                faulty_entry['snowflake_table'] = snowflake_table

                            self.results['faulty_tables'].append(faulty_entry)

        # Calculate summary
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
            'success_rate': round((passed / len(self.results['table_validations']) * 100), 2) if self.results[
                'table_validations'] else 0
        }

        # Print pipeline summary
        summary_parts = [
            f"✅ {passed}",
            f"🚨 {critical}",
            f"⚠️ {errors}"
        ]
        if not_found > 0:
            summary_parts.append(f"⊘ {not_found} not found")
        if faulty > 0:
            summary_parts.append(f"🔴 {faulty} faulty")

        print_safe(
            f"    ⏱️  Completed in {self.results['summary']['total_execution_time']}s | " + " | ".join(summary_parts))

    def _validate_single_table_with_timeout(self, table: str, timeout: int) -> Dict:
        """Validate a single table with timeout support - abandons thread on timeout"""
        # Don't use 'with' context manager to avoid automatic shutdown(wait=True)
        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(self._validate_single_table, table)

        try:
            result = future.result(timeout=timeout)
            # Success - shutdown normally
            executor.shutdown(wait=True)
            return result
        except TimeoutError:
            # Timeout occurred - abandon the thread immediately
            print_safe(f"       ⚠️  Timeout detected, abandoning validation thread for {table}...")
            self.has_abandoned_threads = True  # Mark that we have abandoned threads
            executor.shutdown(wait=False)  # DON'T wait for thread to finish

            error_msg = f'Validation exceeded {timeout}s timeout'

            # Determine table names based on format type
            is_query = (self.format_type == 'query_based')
            if is_query:
                # For query-based: use Oracle source table name (if available) or target table name
                table_name = self.oracle_table_from_query if self.oracle_table_from_query else self.sf_target_table
                sf_table = self.sf_target_table
            else:
                # For table-list: use Oracle table name
                table_name = table
                # Determine Snowflake table name using same logic as _validate_single_table
                if table in self.table_mapping:
                    sf_table = self.table_mapping[table]
                elif self.use_prefix:
                    sf_table = f"{self.sf_table_prefix}{table}"
                else:
                    sf_table = table

            log_detailed_error(
                f"Table Validation Timeout - {table_name}",
                Exception(error_msg),
                {
                    "Source Table": table_name,
                    "Snowflake Table": sf_table,
                    "Timeout": f"{timeout}s",
                    "Reason": "Table validation took longer than allowed timeout period. Background thread abandoned."
                }
            )
            return {
                'table_name': table_name,
                'snowflake_table': sf_table,
                'status': 'TIMEOUT',
                'error': error_msg,
                'validation_time': timeout,
                'Snowflake_Last_Updated': None,
                'Last_Checked': datetime.now(EASTERN).isoformat()
            }
        except Exception as e:
            # Other exception - shutdown normally
            executor.shutdown(wait=True)
            raise

    def get_results(self) -> Dict:
        """Get validation results"""
        return self.results

    def save_results(self, filename: str):
        """Save results to JSON file"""
        try:
            with open(filename, 'w') as f:
                json.dump(self.results, f, indent=2)
            print_safe(f"\n💾 Results saved to: {filename}")
        except Exception as e:
            print_safe(f"\n❌ Failed to save results: {e}")


def load_config_file(config_path: str) -> Dict:
    """Load validation configuration file"""
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
    """Get required configuration value"""
    try:
        value = config[section][key]
        if not value:
            raise ValueError(f"Empty value for {section}.{key}")
        return value
    except KeyError:
        print_safe(f"❌ Missing required config: {section}.{key}")
        sys.exit(1)


def get_optional_config_value(config: Dict, section: str, key: str, default=None):
    """Get optional configuration value with default"""
    try:
        return config[section].get(key, default)
    except KeyError:
        return default


def load_pipeline_report(report_path: str) -> List[Dict]:
    """Load pipeline report and return list of pipelines"""
    try:
        with open(report_path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        # Check if it's an array of pipelines or single pipeline
        if isinstance(data, list):
            return data
        else:
            # Single pipeline - wrap in list for consistency
            return [data]
    except FileNotFoundError:
        print_safe(f"❌ Report file not found: {report_path}")
        sys.exit(1)
    except json.JSONDecodeError as e:
        print_safe(f"❌ Invalid JSON in report file: {e}")
        sys.exit(1)


def validate_single_pipeline(pipeline_data: Dict, config: Dict, pipeline_index: int) -> Dict:
    """Validate a single pipeline and return results"""
    pipeline_name = pipeline_data.get('pipeline_name', f'Pipeline_{pipeline_index}')
    pipeline_number = pipeline_data.get('pipeline_number', pipeline_index)

    print_header(f"PIPELINE {pipeline_number}: {pipeline_name}", "=", 80)

    try:
        config_loader = ConfigLoader(pipeline_data)
        format_type = config_loader.get_format_type()
        use_prefix = True
        table_mapping = {}

        # NEW: Get field name transformation flag
        field_name_transformation = config_loader.has_field_name_transformation()
        transformation_rules = config_loader.get_transformation_rules()

        # Get parallel execution settings from config
        parallel_execution = get_optional_config_value(config, 'validation', 'parallel_execution', True)
        max_parallel_tables = get_optional_config_value(config, 'validation', 'max_parallel_tables', 5)
        connection_pool_size = get_optional_config_value(config, 'validation', 'connection_pool_size',
                                                         max_parallel_tables + 2)
        table_timeout = get_optional_config_value(config, 'validation', 'table_timeout', 60)  # Default 60 seconds
        max_retries = get_optional_config_value(config, 'validation', 'max_timeout_retries', 3)  # Default 3 retries

        if format_type == 'query_based':
            sql_query = config_loader.get_sql_query()
            sf_target_table = config_loader.get_snowflake_target_table()
            oracle_schema = config_loader.get_oracle_schema()
            oracle_table_from_query = config_loader.get_oracle_table_from_query()  # Extract source table name
            if not sql_query or not sf_target_table or not oracle_schema:
                error_msg = 'Missing query-based configuration (sql_query, sf_target_table, or oracle_schema)'
                log_detailed_error(
                    f"Pipeline {pipeline_number} Configuration Error",
                    Exception(error_msg),
                    {
                        "Pipeline": pipeline_name,
                        "SQL Query Present": bool(sql_query),
                        "Target Table Present": bool(sf_target_table),
                        "Oracle Schema Present": bool(oracle_schema)
                    }
                )
                print_safe(f"  ❌ Missing query-based configuration")
                return {
                    'pipeline_name': pipeline_name,
                    'pipeline_number': pipeline_number,
                    'status': 'ERROR',
                    'error': error_msg,
                    'validation_results': None
                }
            tables = []
            table_prefix_from_report = ""
            use_prefix = False
        else:
            sf_config = config_loader.get_snowflake_config()
            tables = config_loader.get_table_list()
            oracle_schema = config_loader.get_oracle_schema()
            sf_schema_from_report, table_prefix_from_report = config_loader.get_snowflake_schema_and_prefix()
            sf_database_from_report = sf_config.get('database')

            if not oracle_schema or not sf_schema_from_report or not sf_database_from_report:
                error_msg = 'Missing table-list configuration (oracle_schema, sf_schema, or sf_database)'
                log_detailed_error(
                    f"Pipeline {pipeline_number} Configuration Error",
                    Exception(error_msg),
                    {
                        "Pipeline": pipeline_name,
                        "Oracle Schema Present": bool(oracle_schema),
                        "Snowflake Schema Present": bool(sf_schema_from_report),
                        "Snowflake Database Present": bool(sf_database_from_report)
                    }
                )
                print_safe(f"  ❌ Missing table-list configuration")
                return {
                    'pipeline_name': pipeline_name,
                    'pipeline_number': pipeline_number,
                    'status': 'ERROR',
                    'error': error_msg,
                    'validation_results': None
                }

            sql_query = None
            sf_target_table = None
            oracle_table_from_query = None

        # Get connection parameters from config
        oracle_user = get_config_value(config, 'oracle', 'username')
        oracle_password = get_config_value(config, 'oracle', 'password')
        oracle_dsn = get_config_value(config, 'oracle', 'dsn')
        sf_account = get_config_value(config, 'snowflake', 'account')
        sf_user = get_config_value(config, 'snowflake', 'user')
        sf_warehouse = get_config_value(config, 'snowflake', 'warehouse')

        # Handle both single role and multiple roles
        sf_role_config = get_config_value(config, 'snowflake', 'role')
        if isinstance(sf_role_config, list):
            sf_roles = sf_role_config
        else:
            sf_roles = [sf_role_config]

        sf_private_key_path = get_config_value(config, 'snowflake', 'private_key_path')
        sf_private_key_password = get_config_value(config, 'snowflake', 'private_key_password')

        if format_type == 'table_list':
            if sf_database_from_report:
                sf_database = sf_database_from_report
            else:
                sf_database = get_config_value(config, 'snowflake', 'database')
            sf_schema = sf_schema_from_report
        else:
            sf_config = config_loader.get_snowflake_config()
            sf_database = sf_config.get('database')
            sf_schema = sf_config.get('schema')

        excluded_columns = []
        if config and 'validation' in config and 'excluded_columns' in config['validation']:
            excluded_cols = config['validation']['excluded_columns']
            if isinstance(excluded_cols, list):
                excluded_columns = [col.strip().upper() for col in excluded_cols if col.strip()]
            elif isinstance(excluded_cols, str):
                excluded_columns = [col.strip().upper() for col in excluded_cols.split(',') if col.strip()]

        # Create connection pools
        print_safe(f"  📊 Configuration:")
        print_safe(f"     Oracle Schema: {oracle_schema}")
        print_safe(f"     Snowflake: {sf_database}.{sf_schema}")
        print_safe(f"     Roles: {', '.join(sf_roles)}")
        print_safe(f"     Format: {format_type}")
        print_safe(f"     Tables: {len(tables) if tables else 'N/A (query-based)'}")
        print_safe(f"     Timeout: {table_timeout}s per table")
        if field_name_transformation:
            print_safe(f"     Column Name Transformation: ENABLED")

        oracle_pool = ConnectionPool(
            OracleConnector,
            {
                'user': oracle_user,
                'password': oracle_password,
                'dsn': oracle_dsn,
                'schema': oracle_schema
            },
            max_connections=connection_pool_size
        )

        # Create Snowflake pool - either single role or multi-role
        if len(sf_roles) > 1:
            snowflake_pool_manager = MultiRoleSnowflakePoolManager(
                account=sf_account,
                user=sf_user,
                private_key_path=sf_private_key_path,
                private_key_password=sf_private_key_password,
                roles=sf_roles,
                warehouse=sf_warehouse,
                database=sf_database,
                schema=sf_schema,
                max_connections=connection_pool_size
            )
        else:
            # Single role - use regular ConnectionPool for backward compatibility
            snowflake_pool_manager = ConnectionPool(
                SnowflakeConnector,
                {
                    'account': sf_account,
                    'user': sf_user,
                    'private_key_path': sf_private_key_path,
                    'private_key_password': sf_private_key_password,
                    'role': sf_roles[0],
                    'warehouse': sf_warehouse,
                    'database': sf_database,
                    'schema': sf_schema
                },
                max_connections=connection_pool_size
            )

        # Use try-finally to ensure cleanup happens no matter what
        try:
            # Test connections
            print_safe(f"  🔌 Testing connections...")
            oracle_test = oracle_pool.get_connection()
            if not oracle_test:
                error_msg = 'Failed to establish Oracle connection - see detailed error log above'
                print_safe(f"  ❌ Failed to connect to Oracle")
                return {
                    'pipeline_name': pipeline_name,
                    'pipeline_number': pipeline_number,
                    'status': 'ERROR',
                    'error': error_msg,
                    'validation_results': None
                }
            oracle_pool.return_connection(oracle_test)

            sf_test = snowflake_pool_manager.get_connection()
            if not sf_test:
                error_msg = 'Failed to establish Snowflake connection - see detailed error log above'
                print_safe(f"  ❌ Failed to connect to Snowflake")
                return {
                    'pipeline_name': pipeline_name,
                    'pipeline_number': pipeline_number,
                    'status': 'ERROR',
                    'error': error_msg,
                    'validation_results': None
                }

            # Return connection based on type
            if isinstance(snowflake_pool_manager, MultiRoleSnowflakePoolManager):
                snowflake_pool_manager.return_connection(sf_test, sf_roles[0])
            else:
                snowflake_pool_manager.return_connection(sf_test)

            print_safe(f"  ✅ Connections established")

            # Handle table list logic
            if format_type == 'table_list':
                should_fetch_all_tables = (not tables and getattr(config_loader, 'table_pattern', '') == '%')
                if should_fetch_all_tables:
                    print_safe(f"  🔍 Fetching all tables from Oracle...")
                    oracle_conn = oracle_pool.get_connection()
                    tables = oracle_conn.get_all_tables()
                    oracle_pool.return_connection(oracle_conn)

                    if not tables:
                        error_msg = 'No tables found in Oracle schema'
                        log_detailed_error(
                            f"Pipeline {pipeline_number} - No Tables Found",
                            Exception(error_msg),
                            {
                                "Pipeline": pipeline_name,
                                "Oracle Schema": oracle_schema,
                                "Reason": "Schema exists but contains no tables, or user lacks permission to view tables"
                            }
                        )
                        print_safe(f"  ❌ No tables found in Oracle schema")
                        return {
                            'pipeline_name': pipeline_name,
                            'pipeline_number': pipeline_number,
                            'status': 'ERROR',
                            'error': error_msg,
                            'validation_results': None
                        }
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

            # Create validator with connection pools
            validator = DataValidator(
                oracle_pool, snowflake_pool_manager, tables,
                table_prefix_from_report if format_type == 'table_list' else "",
                excluded_columns, format_type=format_type,
                sql_query=sql_query, sf_target_table=sf_target_table,
                oracle_table_from_query=oracle_table_from_query,
                use_prefix=use_prefix, table_mapping=table_mapping,
                parallel_execution=parallel_execution,
                max_parallel_tables=max_parallel_tables,
                table_timeout=table_timeout,
                max_retries=max_retries,
                field_name_transformation=field_name_transformation,
                transformation_rules=transformation_rules  # Pass transformation rules
            )

            # Run validation
            validator.validate_all_tables()

            # Get results
            validation_results = validator.get_results()

            print_safe(f"  ✅ Pipeline validation completed")

            return {
                'pipeline_name': pipeline_name,
                'pipeline_number': pipeline_number,
                'status': 'SUCCESS',
                'oracle_schema': oracle_schema,
                'snowflake_database': sf_database,
                'snowflake_schema': sf_schema,
                'validation_results': validation_results
            }

        finally:
            # CRITICAL: Always cleanup connection pools, even on exception
            print_safe(f"  🧹 Cleaning up connection pools...")

            # Check if we need to skip disconnect (due to abandoned threads)
            skip_disconnect = False
            try:
                # Check if validator was created and has abandoned threads
                if 'validator' in locals() and hasattr(validator, 'has_abandoned_threads'):
                    skip_disconnect = validator.has_abandoned_threads
                    if skip_disconnect:
                        print_safe(
                            f"  ⚠️  Abandoned threads detected - will skip connection disconnect to avoid hanging")
            except:
                pass

            try:
                oracle_pool.close_all(force_skip_disconnect=skip_disconnect)
            except Exception as e:
                print_safe(f"  ⚠️  Error closing Oracle pool: {e}")
            try:
                snowflake_pool_manager.close_all(force_skip_disconnect=skip_disconnect)
            except Exception as e:
                print_safe(f"  ⚠️  Error closing Snowflake pool: {e}")

    except Exception as e:
        error_details = tb.format_exc()
        log_detailed_error(
            f"Pipeline {pipeline_number} Validation Failed",
            e,
            {
                "Pipeline": pipeline_name
            }
        )
        print_safe(f"  ❌ Pipeline validation failed - see detailed error log above")
        return {
            'pipeline_name': pipeline_name,
            'pipeline_number': pipeline_number,
            'status': 'ERROR',
            'error': str(e),
            'error_details': error_details,
            'validation_results': None
        }


def main():
    parser = argparse.ArgumentParser(description='Multi-Pipeline Oracle to Snowflake Data Validation')
    parser.add_argument('--config', '-c', help='Config JSON file', default="validation_config.json")
    parser.add_argument('--report', '-r', help='StreamSets report JSON file (with multiple pipelines)', default=None)
    args = parser.parse_args()

    print_header("Multi-Pipeline Oracle to Snowflake Validation", "=", 80)

    # Load config
    config = load_config_file(args.config)

    # Load Snowflake config (for validation queries)
    try:
        sf_config = load_snowflake_config(config)
        db_info = f" / {sf_config['database']}" if sf_config.get('database') else ""
        print_safe(f"✅ Snowflake (validation) config loaded: {sf_config['account']}{db_info}")
    except Exception as e:
        print_safe(f"❌ Snowflake configuration error: {e}")
        sys.exit(1)

    # Load Snowflake Save config (for saving validation reports)
    try:
        sf_save_config = load_snowflake_save_config(config)
        print_safe(f"✅ Snowflake (save) config loaded: {sf_save_config['account']} / {sf_save_config['database']}")
    except Exception as e:
        print_safe(f"❌ Snowflake Save configuration error: {e}")
        sys.exit(1)

    # Get report path
    if args.report:
        report_path = args.report
    elif 'validation' in config and 'report_path' in config['validation']:
        report_path = config['validation']['report_path']
    else:
        print_safe("❌ Report path not specified")
        sys.exit(1)

    # Load pipelines
    pipelines = load_pipeline_report(report_path)

    # Validate all pipelines in parallel
    print_safe(f"\n🚀 Starting validation of {len(pipelines)} pipeline(s)...")
    print_safe(f"📁 Report: {report_path}")
    all_results = []

    overall_start_time = time.time()

    # Process pipelines in parallel
    completed_pipelines = 0
    with ThreadPoolExecutor(max_workers=min(len(pipelines), 3)) as executor:
        future_to_pipeline = {
            executor.submit(validate_single_pipeline, pipeline, config, i + 1): (i, pipeline)
            for i, pipeline in enumerate(pipelines)
        }

        for future in as_completed(future_to_pipeline):
            pipeline_index, pipeline_data = future_to_pipeline[future]
            result = future.result()
            all_results.append(result)
            completed_pipelines += 1

            # Show pipeline completion status
            status_icon = "✅" if result['status'] == 'SUCCESS' else "❌"
            print_safe(
                f"\n{status_icon} Completed pipeline {completed_pipelines}/{len(pipelines)}: {result['pipeline_name']}")

    overall_time = time.time() - overall_start_time

    # Sort results by pipeline number for consistent output
    all_results.sort(key=lambda x: x.get('pipeline_number', 0))

    # Create consolidated report
    consolidated_report = {
        'report_metadata': {
            'generated_timestamp': datetime.now(EASTERN).isoformat(),
            'total_pipelines': len(pipelines),
            'total_execution_time': round(overall_time, 2),
            'source_report': report_path
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

    # Collect all faulty tables across pipelines
    for result in all_results:
        if result['status'] == 'SUCCESS' and result['validation_results']:
            faulty_tables = result['validation_results'].get('faulty_tables', [])
            if faulty_tables:
                consolidated_report['faulty_tables_summary'].append({
                    'pipeline_name': result['pipeline_name'],
                    'pipeline_number': result['pipeline_number'],
                    'faulty_tables': faulty_tables
                })

    # Save consolidated report to Snowflake
    print_safe("\n💾 Saving report to Snowflake...")
    database_name = get_optional_config_value(config, 'validation', 'database_name', None)
    try:
        save_report_to_snowflake(consolidated_report, sf_save_config, database_name)
    except Exception as e:
        print_safe(f"\n❌ Failed to save report to Snowflake: {e}")
        print_safe("❌ Validation aborted due to database save failure")
        sys.exit(1)

    # Print overall summary
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

    # Print per-pipeline summary table
    print_header("PER-PIPELINE SUMMARY", "-", 80)
    print_safe(f"\n{'#':<4} {'Pipeline Name':<40} {'Status':<10} {'Tables':<8} {'Passed':<8} {'Critical':<10}")
    print_safe("-" * 80)

    for result in all_results:
        pipeline_num = str(result['pipeline_number'])
        pipeline_name = result['pipeline_name'][:38] + '..' if len(result['pipeline_name']) > 40 else result[
            'pipeline_name']
        status = result['status']

        if result['status'] == 'SUCCESS' and result['validation_results']:
            summary = result['validation_results']['summary']
            tables = str(summary['total_validations'])
            passed = str(summary['passed'])
            critical = str(summary['critical'])
            errors = str(summary['errors'])
        else:
            tables = passed = critical = errors = 'N/A'

        status_icon = "✅" if status == 'SUCCESS' else "❌"
        print_safe(
            f"{pipeline_num:<4} {pipeline_name:<40} {status_icon} {status:<8} {tables:<8} {passed:<8} {critical:<10}")

    # Print faulty tables summary if any exist
    if consolidated_report['faulty_tables_summary']:
        print_header("FAULTY TABLES (TIMEOUT)", "-", 80)
        print_safe(
            f"\n⚠️  The following tables consistently exceeded the {get_optional_config_value(config, 'validation', 'table_timeout', 60)}s timeout:\n")

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