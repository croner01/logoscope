"""
Semantic Engine Storage 模块
负责与数据库交互，提供统一的存储接口
使用 ClickHouse HTTP 接口和 Neo4j Bolt 协议

符合标准：
- RFC 3339 时间戳格式（ISO 8601）
- ClickHouse DateTime64(9) 纳秒精度存储
"""
from typing import Dict, Any, List
import logging
import json
import re
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from tenacity import retry, stop_after_attempt, wait_exponential
import sys
import os

# 添加项目根目录到 Python 路径
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.timestamp import rfc3339_to_datetime64, parse_any_timestamp, datetime64_to_rfc3339

try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False

try:
    from clickhouse_driver import Client as ClickHouseClient
    CLICKHOUSE_DRIVER_AVAILABLE = True
except ImportError:
    CLICKHOUSE_DRIVER_AVAILABLE = False

try:
    from neo4j import GraphDatabase
    NEO4J_AVAILABLE = True
except ImportError:
    NEO4J_AVAILABLE = False

# 配置日志系统
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)
_CH_ASYNC_INSERT_SETTINGS = {
    "async_insert": 1,
    "wait_for_async_insert": 0,
}


def _sanitize_interval(time_window: str, default_value: str = "1 HOUR") -> str:
    """规范化 INTERVAL 参数，避免 SQL 注入。"""
    pattern = re.compile(r"^\s*(\d+)\s+([A-Za-z]+)\s*$")
    match = pattern.match(str(time_window or ""))
    if not match:
        return default_value

    amount = int(match.group(1))
    unit_raw = match.group(2).upper()
    valid_units = {
        "MINUTE": "MINUTE",
        "MINUTES": "MINUTE",
        "HOUR": "HOUR",
        "HOURS": "HOUR",
        "DAY": "DAY",
        "DAYS": "DAY",
        "WEEK": "WEEK",
        "WEEKS": "WEEK",
    }
    if amount <= 0 or unit_raw not in valid_units:
        return default_value
    return f"{amount} {valid_units[unit_raw]}"


def _sanitize_limit(value: Any, default_value: int = 1000, max_value: int = 10000) -> int:
    """限制 LIMIT 范围，避免异常值影响查询。"""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default_value
    if parsed < 1:
        return 1
    return min(parsed, max_value)


def _escape_sql_literal(value: str) -> str:
    """转义 SQL 字符串字面量中的单引号。"""
    return str(value).replace("'", "''")


_STATS_DEFAULT_WINDOW = _sanitize_interval(os.getenv("CH_STATS_TIME_WINDOW", "24 HOUR"), default_value="24 HOUR")
_TOPOLOGY_DEFAULT_WINDOW = _sanitize_interval(os.getenv("CH_TOPOLOGY_TIME_WINDOW", "24 HOUR"), default_value="24 HOUR")


class StorageAdapter:
    """
    存储适配器

    提供统一的接口与不同数据库交互，支持 ClickHouse HTTP 和 Neo4j
    集成真实的数据库客户端，支持重试机制和日志记录
    """

    def __init__(self, config: Dict[str, Any] = None):
        """
        初始化存储适配器

        Args:
            config: 数据库配置，包含 ClickHouse 和 Neo4j 的连接信息
                    如果为 None，则使用默认配置
        """
        # 设置默认配置
        self.config = config or {
                "clickhouse": {
                        "host": "localhost",
                        "port": 9000,
                        "database": "logs",
                        "user": "default",
                        "password": ""
                },
                "neo4j": {
                        "host": "localhost",
                        "port": 7687,
                        "user": "neo4j",
                        "password": "password",
                        "database": "neo4j"
                }
        }

        # 初始化 ClickHouse 客户端
        if CLICKHOUSE_DRIVER_AVAILABLE:
            try:
                self.ch_client = ClickHouseClient(
                    host=self.config['clickhouse']['host'],
                    port=self.config['clickhouse']['port'],
                    database=self.config['clickhouse']['database'],
                    user=self.config['clickhouse']['user'],
                    password=self.config['clickhouse']['password'],
                    settings={'use_numpy': False}
                )
                # 测试连接
                self.ch_client.execute('SELECT 1')
                logger.info("ClickHouse driver client connected successfully")
            except Exception as e:
                logger.warning(f"ClickHouse driver connection failed: {e}, using HTTP client instead")
                self.ch_client = None
                # 初始化 HTTP 客户端
                self._init_http_client()
        else:
            logger.warning("ClickHouse driver not available")
            self.ch_client = None
            # 初始化 HTTP 客户端
            self._init_http_client()

        # 如果驱动可用，HTTP 客户端作为备用；否则 HTTP 客户端作为主要客户端
        if self.ch_client:
            self.ch_http_client = None  # 不需要 HTTP 客户端
        else:
            # HTTP 客户端已在上面的 _init_http_client() 中初始化
            pass

        self.ch_database = self.config['clickhouse'].get('database', 'logs')

        # 初始化 Neo4j 客户端
        if NEO4J_AVAILABLE:
            try:
                self.neo4j_driver = GraphDatabase.driver(
                    f"bolt://{self.config['neo4j']['host']}:{self.config['neo4j']['port']}",
                    auth=(self.config['neo4j']['user'], self.config['neo4j']['password'])
                )
                # 测试连接
                self.neo4j_driver.verify_connectivity()
                logger.info("Neo4j driver connected successfully")
            except Exception as e:
                logger.warning(f"Neo4j connection failed: {e}")
                self.neo4j_driver = None
        else:
            logger.warning("Neo4j driver not available")

        # ⭐ 初始化数据去重器（P1 优化）
        from storage.deduplication import get_deduplicator
        self.deduplicator = None  # 延迟初始化，避免循环依赖

        # 初始化存储（用于模拟实现）
        self.events = []
        self.graphs = []

        # 初始化数据库表
        self._init_tables()

    def _init_http_client(self):
        """初始化 ClickHouse HTTP 客户端"""
        try:
            ch_host = self.config['clickhouse'].get('http_host', self.config['clickhouse'].get('host', 'localhost'))
            ch_port = self.config['clickhouse'].get('http_port', 8123)
            database = self.config['clickhouse'].get('database', 'logs')
            user = self.config['clickhouse'].get('user', 'default')
            password = self.config['clickhouse'].get('password', '')

            self.ch_http_client = {
                'url': f'http://{ch_host}:{ch_port}',
                'database': database,
                'user': user,
                'password': password
            }

            # 测试连接
            self._execute_clickhouse_http('SELECT 1')
            logger.info("ClickHouse HTTP client connected successfully")
        except Exception as e:
            logger.warning(f"ClickHouse HTTP client connection failed: {e}")
            self.ch_http_client = None


    def _init_tables(self):
        """
        初始化数据库表

        创建 ClickHouse 和 Neo4j 的表结构
        """
        # 初始化 ClickHouse 表
        try:
            self._init_clickhouse_tables()
        except Exception as e:
            logger.error(f"Failed to initialize ClickHouse tables: {e}")

        # 初始化 Neo4j 约束和索引
        if self.neo4j_driver:
            try:
                self._init_neo4j_constraints()
            except Exception as e:
                logger.error(f"Failed to initialize Neo4j constraints: {e}")

    def _init_clickhouse_tables(self):
        """
        初始化 ClickHouse 表结构

        创建 events、logs、traces、metrics 表
        """
        try:
            if not self.ch_client and not self.ch_http_client:
                logger.warning("ClickHouse client not available, skipping table initialization")
                return

            # 创建 events 表（升级为 DateTime64(9) 纳秒精度）
            create_events_table = """
            CREATE TABLE IF NOT EXISTS events (
                id String,
                timestamp DateTime64(9, 'UTC'),
                entity_type String,
                entity_name String,
                event_type String,
                level String,
                content String,
                trace_id String,
                span_id String,
                labels String,
                host_ip String
            ) ENGINE = MergeTree()
            PARTITION BY toDate(timestamp)
            ORDER BY (timestamp, entity_name, event_type)
            TTL toDateTime(timestamp) + INTERVAL 30 DAY DELETE
            SETTINGS ttl_only_drop_parts = 1;
            """
            self.ch_client.execute(create_events_table)
            logger.info("ClickHouse events table created")

            # 创建 logs 表（升级为 DateTime64(9) 纳秒精度）
            create_logs_table = """
            CREATE TABLE IF NOT EXISTS logs (
                id String,
                timestamp DateTime64(9, 'UTC'),
                service_name String,
                pod_name String,
                namespace String,
                node_name String,
                level String,
                message String,
                trace_id String,
                span_id String,
                labels String,
                host_ip String
            ) ENGINE = MergeTree()
            PARTITION BY toDate(timestamp)
            ORDER BY (timestamp, service_name, level)
            TTL toDateTime(timestamp) + INTERVAL 30 DAY DELETE
            SETTINGS ttl_only_drop_parts = 1;
            """
            self.ch_client.execute(create_logs_table)
            logger.info("ClickHouse logs table created")
        except Exception as e:
            logger.error(f"Failed to create ClickHouse tables: {e}")

    def _init_neo4j_constraints(self):
        """
        初始化 Neo4j 约束和索引

        创建 Service 节点的唯一约束和索引
        """
        with self.neo4j_driver.session() as session:
            # 创建唯一约束
            session.run("""
                CREATE CONSTRAINT service_id_unique IF NOT EXISTS
                FOR (s:Service) REQUIRE s.id IS UNIQUE
            """)
            logger.info("Neo4j service_id_unique constraint created")

            # 创建索引
            session.run("""
                CREATE INDEX service_name_idx IF NOT EXISTS
                FOR (s:Service) ON (s.name)
            """)
            logger.info("Neo4j service_name_idx index created")

    def _execute_clickhouse_http(self, query: str, data=None) -> Any:
        """
        使用 HTTP 接口执行 ClickHouse 查询

        Args:
            query: SQL 查询语句
            data: 查询参数（用于 INSERT）

        Returns:
            查询结果
        """
        if not self.ch_http_client:
            raise Exception("ClickHouse HTTP client not available")

        url = self.ch_http_client['url']
        database = self.ch_http_client['database']
        user = self.ch_http_client['user']
        password = self.ch_http_client['password']

        # 构建 SQL 查询
        if data:
            # INSERT 查询 - 使用 FORMAT JSONEachRow
            import json
            from datetime import datetime

            # 转换数据为 JSON 格式
            formatted_data = []
            for row in data:
                row_dict = {}
                # 获取列名（从 INSERT 语句中解析）
                if 'INSERT INTO logs' in query.upper():
                    columns = ['id', 'timestamp', 'service_name', 'pod_name', 'namespace', 'node_name', 'level', 'message', 'trace_id', 'span_id', 'labels', 'host_ip']
                    for i, col in enumerate(columns):
                        val = row[i] if i < len(row) else ''
                        # 转换 datetime 为字符串
                        if isinstance(val, datetime):
                            val = val.strftime('%Y-%m-%d %H:%M:%S.%f')
                        row_dict[col] = val
                formatted_data.append(row_dict)

            # 构建 INSERT 查询
            insert_query = query.replace('VALUES', '') + ' FORMAT JSONEachRow'
            body = '\n'.join(json.dumps(row) for row in formatted_data)

            params = {
                'query': insert_query,
                'database': database
            }

            auth = (user, password) if password else None
            response = requests.post(url, params=params, data=body, auth=auth, timeout=30)
        else:
            # SELECT 查询
            params = {
                'query': f"{query} FORMAT JSONEachRow",
                'database': database
            }

            auth = (user, password) if password else None
            response = requests.get(url, params=params, auth=auth, timeout=30)

        if response.status_code != 200:
            raise Exception(f"HTTP {response.status_code}: {response.text}")

        return response.text

    def save_event(self, event: Dict[str, Any]) -> bool:
        """
        保存事件到 ClickHouse logs 表

        支持 HTTP 和 Native 两种接口
        ⭐ P1 优化：集成数据去重

        Args:
            event: 事件数据，包含标准化后的事件信息

        Returns:
            bool: 是否保存成功
        """
        # ⭐ P1 优化：数据去重检查
        if self.deduplicator is None:
            from storage.deduplication import get_deduplicator
            self.deduplicator = get_deduplicator(self)

        is_duplicate, reason = self.deduplicator.is_duplicate_event(event, check_existing=False)
        if is_duplicate:
            logger.debug(f"Skipping duplicate event: {reason}")
            return True  # 返回 True 表示"处理成功"，但实际没有写入

        # Mock 模式：直接返回成功
        if not self.ch_http_client and not self.ch_client:
            logger.debug(f"[Mock] Event would be saved: {event.get('event', {}).get('level', 'info')} - {event.get('event', {}).get('raw', '')[:100]}")
            return True

        try:
            # 优先使用 HTTP 接口
            if self.ch_http_client:
                return self._save_event_http(event)
            elif self.ch_client:
                return self._save_event_native(event)
            else:
                raise Exception("ClickHouse client not available")

        except Exception as e:
            logger.error(f"Error saving event: {e}")
            return False

    def _save_event_http(self, event: Dict[str, Any]) -> bool:
        """使用 HTTP 接口保存事件"""
        try:
            # 提取 K8s 上下文信息
            k8s_context = event.get('context', {}).get('k8s', {})

            # 转换 timestamp
            ts_input = event.get('timestamp')
            if ts_input:
                ts_rfc3339 = parse_any_timestamp(ts_input)
                # 转换为 ClickHouse HTTP 格式
                ts_http = ts_rfc3339.replace('T', ' ').replace('Z', '')
            else:
                from datetime import datetime
                ts_http = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]

            # 提取主机信息
            host = k8s_context.get('host', k8s_context.get('node', 'unknown'))
            host_ip = k8s_context.get('host_ip', '')

            # 节点名称映射
            node_ip_map = {'ren': '192.168.56.129'}
            if not host_ip and host in node_ip_map:
                host_ip = node_ip_map[host]

            # 构建 INSERT 查询（使用 SQL 格式）
            query = f"""
                INSERT INTO logs.logs (
                    id, timestamp, service_name, pod_name, namespace,
                    node_name, level, message, trace_id, span_id, labels, host_ip,
                    cpu_limit, cpu_request, memory_limit, memory_request
                ) SETTINGS async_insert = 1, wait_for_async_insert = 0 FORMAT JSONEachRow
            """

            # ⭐ P1优化：提取资源指标
            resources = k8s_context.get('resources', {})

            # 准备 JSON 数据
            import json
            row = {
                'id': event.get('id', ''),
                'timestamp': ts_http,
                'service_name': event.get('entity', {}).get('name', 'unknown'),
                'pod_name': k8s_context.get('pod', 'unknown'),
                'namespace': k8s_context.get('namespace', 'islap'),
                'node_name': k8s_context.get('node', host),
                'level': event.get('event', {}).get('level', 'info'),
                'message': str(event.get('event', {}).get('raw', ''))[:5000],
                'trace_id': event.get('context', {}).get('trace_id', ''),
                'span_id': event.get('context', {}).get('span_id', ''),
                'labels': json.dumps(k8s_context.get('labels', {}), ensure_ascii=False),
                'host_ip': host_ip,
                'cpu_limit': resources.get('cpu_limit', ''),
                'cpu_request': resources.get('cpu_request', ''),
                'memory_limit': resources.get('memory_limit', ''),
                'memory_request': resources.get('memory_request', '')
            }

            # 执行 HTTP 请求
            url = self.ch_http_client['url']
            database = self.ch_http_client['database']
            user = self.ch_http_client['user']
            password = self.ch_http_client['password']

            params = {'query': query, 'database': database}
            auth = (user, password) if password else None

            response = requests.post(url, params=params, data=json.dumps(row), auth=auth, timeout=30)

            if response.status_code == 200:
                logger.info(f"Event saved to ClickHouse (HTTP): {event.get('id', 'unknown')}")
                return True
            else:
                raise Exception(f"HTTP {response.status_code}: {response.text}")

        except Exception as e:
            logger.error(f"Error saving event via HTTP: {type(e).__name__}: {e}")
            raise  # 重新抛出异常以触发重试

    def _save_event_native(self, event: Dict[str, Any]) -> bool:
        """使用原生驱动保存事件"""
        try:
            # 提取 K8s 上下文信息
            k8s_context = event.get('context', {}).get('k8s', {})
            # 转换 timestamp 为 Python datetime 对象
            ts_input = event.get('timestamp')
            ts_datetime = None

            if ts_input:
                ts_rfc3339 = parse_any_timestamp(ts_input)
                if 'T' in ts_rfc3339:
                    ts_clean = ts_rfc3339.replace('T', ' ').replace('Z', '')
                    if '.' in ts_clean:
                        dt_str, ns_str = ts_clean.split('.')
                        ns_str = ns_str[:6]  # 截断到 6 位微秒
                        ts_clean = f"{dt_str}.{ns_str}"

                    from datetime import datetime
                    try:
                        if '.' in ts_clean:
                            ts_datetime = datetime.strptime(ts_clean, '%Y-%m-%d %H:%M:%S.%f')
                        else:
                            ts_datetime = datetime.strptime(ts_clean, '%Y-%m-%d %H:%M:%S')
                    except ValueError as e:
                        logger.warning(f"无法解析时间戳 '{ts_rfc3339}': {e}")
                        ts_datetime = datetime.utcnow()
                else:
                    ts_datetime = datetime.utcnow()
            else:
                from datetime import datetime
                ts_datetime = datetime.utcnow()

            # 提取主机信息
            host = k8s_context.get('host', k8s_context.get('node', 'unknown'))
            host_ip = k8s_context.get('host_ip', '')

            # 节点名称映射
            node_ip_map = {'ren': '192.168.56.129'}
            if not host_ip and host in node_ip_map:
                host_ip = node_ip_map[host]

            # ⭐ P1优化：提取完整的kubernetes metadata
            pod_id = k8s_context.get('pod_id', '')
            container_name = k8s_context.get('container_name', '')
            container_id = k8s_context.get('container_id', '')
            container_image = k8s_context.get('container_image', '')

            # ⭐ P0优化：提取OTLP标准字段
            severity_number = event.get('severity_number', 0)
            flags = event.get('flags', 0)

            # ⭐ P0优化：序列化所有attributes
            raw_attributes = event.get('_raw_attributes', {}) or {}
            attributes_json = json.dumps(raw_attributes, ensure_ascii=False) if raw_attributes else '{}'

            # ⭐ P1优化：提取资源指标
            resources = k8s_context.get('resources', {})
            cpu_limit = resources.get('cpu_limit', '')
            cpu_request = resources.get('cpu_request', '')
            memory_limit = resources.get('memory_limit', '')
            memory_request = resources.get('memory_request', '')

            # 准备数据（按照新表结构顺序）
            labels_to_insert = json.dumps(k8s_context.get('labels', {}) or {}, ensure_ascii=False)
            data = [[
                event.get('id', '') or '',                        # id
                ts_datetime,                                         # timestamp
                ts_datetime,                                         # observed_timestamp (暂时使用相同值)
                event.get('entity', {}).get('name', 'unknown') or 'unknown',  # service_name
                k8s_context.get('pod', 'unknown') or 'unknown',    # pod_name
                k8s_context.get('namespace', 'islap') or 'islap',  # namespace
                host or 'unknown',                                  # node_name
                pod_id or '',                                       # ⭐ pod_id
                container_name or '',                               # ⭐ container_name
                container_id or '',                                 # ⭐ container_id
                container_image or '',                              # ⭐ container_image
                event.get('event', {}).get('level', 'info') or 'info',  # level
                severity_number or 0,                               # ⭐ severity_number
                str(event.get('event', {}).get('raw', '') or '')[:5000],  # message
                event.get('context', {}).get('trace_id', '') or '',  # trace_id
                event.get('context', {}).get('span_id', '') or '',   # span_id
                flags or 0,                                         # ⭐ flags
                labels_to_insert,                                   # labels
                attributes_json,                                    # ⭐ attributes_json
                host_ip,                                            # host_ip
                cpu_limit,                                          # ⭐ P1: cpu_limit
                cpu_request,                                        # ⭐ P1: cpu_request
                memory_limit,                                       # ⭐ P1: memory_limit
                memory_request                                      # ⭐ P1: memory_request
            ]]

            # 执行 INSERT（更新列名以匹配新表结构）
            try:
                self.ch_client.execute(
                    'INSERT INTO logs.logs (id, timestamp, observed_timestamp, service_name, pod_name, namespace, node_name, pod_id, container_name, container_id, container_image, level, severity_number, message, trace_id, span_id, flags, labels, attributes_json, host_ip, cpu_limit, cpu_request, memory_limit, memory_request) VALUES',
                    data,
                    settings=_CH_ASYNC_INSERT_SETTINGS,
                )
            except Exception as insert_error:
                logger.error(f"Error executing INSERT: {insert_error}")
                logger.error(f"Data sample: id={data[0][0]}, service={data[0][3]}, message_len={len(data[0][12])}")
                raise

            # ⭐ 同时保存到 events 表（语义化事件）
            self._save_semantic_event(event, k8s_context, host, host_ip)

            logger.info(f"Event saved to ClickHouse (Native): {event.get('id', 'unknown')}")
            return True

        except Exception as e:
            logger.error(f"Error saving event via Native: {type(e).__name__}: {e}")
            raise  # 重新抛出异常以触发重试

    def _extract_event_type(self, event: Dict[str, Any]) -> str:
        """
        从事件中提取事件类型

        Args:
            event: 标准化的事件数据

        Returns:
            str: 事件类型（error, warning, info, startup, shutdown, etc.）
        """
        level = event.get('event', {}).get('level', 'info')
        message = str(event.get('event', {}).get('raw', '')).lower()

        # 根据日志级别和内容推断事件类型
        if level in ['error', 'fatal', 'critical']:
            return 'error'
        elif level == 'warn':
            return 'warning'
        elif any(keyword in message for keyword in ['started', 'starting', 'initialized', 'ready']):
            return 'startup'
        elif any(keyword in message for keyword in ['stopped', 'stopping', 'shutdown', 'terminated']):
            return 'shutdown'
        elif any(keyword in message for keyword in ['health', 'heartbeat', 'ping']):
            return 'health_check'
        elif any(keyword in message for keyword in ['deployment', 'deploying', 'rolled']):
            return 'deployment'
        elif any(keyword in message for keyword in ['config', 'configuration', 'reloading']):
            return 'configuration'
        else:
            return 'info'

    def _save_semantic_event(self, event: Dict[str, Any], k8s_context: Dict[str, Any], host: str, host_ip: str) -> bool:
        """
        保存语义化事件到 events 表

        Args:
            event: 标准化的事件数据
            k8s_context: Kubernetes 上下文信息
            host: 主机名
            host_ip: 主机IP

        Returns:
            bool: 是否保存成功
        """
        try:
            # 只保存重要事件（error/warning/startup/shutdown）
            event_type = self._extract_event_type(event)
            if event_type == 'info':
                # 跳过普通 info 事件，只保留重要事件
                return True

            # 解析时间戳
            timestamp_str = event.get('timestamp', '')
            ts_datetime = parse_any_timestamp(timestamp_str)

            # 如果返回的是字符串，转换为 datetime 对象
            if isinstance(ts_datetime, str):
                try:
                    # 尝试解析 RFC 3339/ISO 8601 格式
                    # 处理 'Z' 后缀（UTC 时区）
                    if ts_datetime.endswith('Z'):
                        ts_datetime = ts_datetime.rstrip('Z')
                        ts_datetime = datetime.fromisoformat(ts_datetime + '+00:00')
                    else:
                        ts_datetime = datetime.fromisoformat(ts_datetime)
                except Exception as parse_error:
                    logger.warning(f"Failed to parse timestamp string '{ts_datetime}': {parse_error}")
                    # 使用当前时间作为后备
                    ts_datetime = datetime.now(timezone.utc)

            # 提取实体信息
            entity = event.get('entity', {})
            entity_type = entity.get('type', 'service')
            entity_name = entity.get('name', 'unknown')

            # 提取事件内容
            event_data = event.get('event', {})
            level = event_data.get('level', 'info')
            content = str(event_data.get('raw', ''))[:5000]  # 限制长度

            # 提取 trace/span 信息
            trace_id = event.get('context', {}).get('trace_id', '')
            span_id = event.get('context', {}).get('span_id', '')

            # 序列化 labels
            labels_json = json.dumps(k8s_context.get('labels', {}), ensure_ascii=False)

            # 准备数据
            data = [[
                event.get('id', ''),           # id
                ts_datetime,                   # timestamp
                entity_type,                   # entity_type
                entity_name,                   # entity_name
                event_type,                    # event_type
                level,                         # level
                content,                       # content
                trace_id,                      # trace_id
                span_id,                       # span_id
                labels_json,                   # labels
                host_ip                        # host_ip
            ]]

            # 执行 INSERT
            self.ch_client.execute(
                'INSERT INTO logs.events (id, timestamp, entity_type, entity_name, event_type, level, content, trace_id, span_id, labels, host_ip) VALUES',
                data,
                settings=_CH_ASYNC_INSERT_SETTINGS,
            )

            logger.debug(f"Semantic event saved: {event_type} for {entity_name}")
            return True

        except Exception as e:
            # 不抛出异常，避免影响主流程
            logger.warning(f"Failed to save semantic event: {e}")
            return False

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
    def save_relation(self, relation: Dict[str, Any]) -> bool:
        """
        保存关系到 Neo4j

        使用重试机制，最多重试 3 次

        Args:
            relation: 关系数据，包含 type、source、target 等信息

        Returns:
            bool: 是否保存成功
        """
        try:
            if self.neo4j_driver:
                # 使用真实的 Neo4j 客户端
                with self.neo4j_driver.session() as session:
                    # 创建或更新源服务节点
                    session.run("""
                        MERGE (s:Service {id: $source_id})
                        SET s.name = $source_name, s.type = 'service'
                    """, source_id=relation['source'], source_name=relation['source'])

                    # 创建或更新目标服务节点
                    session.run("""
                        MERGE (t:Service {id: $target_id})
                        SET t.name = $target_name, t.type = 'service'
                    """, target_id=relation['target'], target_name=relation['target'])

                    # 创建关系
                    session.run("""
                        MATCH (s:Service {id: $source_id})
                        MATCH (t:Service {id: $target_id})
                        MERGE (s)-[r:DEPENDS_ON]->(t)
                        SET r.timestamp = $timestamp
                    """, source_id=relation['source'], target_id=relation['target'], timestamp=relation.get('timestamp'))

                    logger.info(f"Relation saved to Neo4j: {relation['type']} from {relation['source']} to {relation['target']}")
            else:
                # 使用模拟实现
                logger.info(f"Saving relation (mock): {relation['type']} from {relation['source']} to {relation['target']}")

            return True
        except Exception as e:
            logger.error(f"Error saving relation: {e}")
            return False

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
    def save_graph(self, graph: Dict[str, Any]) -> bool:
        """
        保存拓扑图到 Neo4j（⭐ P0优化：使用 UNWIND 批量操作）

        使用重试机制，最多重试 3 次

        Args:
            graph: 拓扑图数据，包含节点和边信息

        Returns:
            bool: 是否保存成功
        """
        try:
            if self.neo4j_driver:
                nodes = graph.get('nodes', [])
                edges = graph.get('edges', [])
                
                if not nodes and not edges:
                    return True
                
                with self.neo4j_driver.session() as session:
                    # ⭐ P0优化：使用 UNWIND 批量创建节点
                    if nodes:
                        node_data = [
                            {
                                'id': node['id'],
                                'name': node.get('name', node['id']),
                                'type': node.get('type', 'service'),
                                'log_count': node.get('log_count', 0),
                                'health_status': node.get('health_status', 'unknown')
                            }
                            for node in nodes
                        ]
                        
                        session.run("""
                            UNWIND $nodes AS node
                            MERGE (s:Service {id: node.id})
                            SET s.name = node.name,
                                s.type = node.type,
                                s.log_count = node.log_count,
                                s.health_status = node.health_status,
                                s.last_updated = timestamp()
                        """, nodes=node_data)
                    
                    # ⭐ P0优化：使用 UNWIND 批量创建边
                    if edges:
                        edge_data = []
                        for edge in edges:
                            metrics = edge.get('metrics', {})
                            call_count = metrics.get('call_count', metrics.get('request_count', 0))
                            
                            # 计算 confidence
                            data_sources = metrics.get('data_sources', [])
                            if 'traces' in data_sources:
                                base_confidence = 1.0
                            elif 'logs' in data_sources:
                                base_confidence = 0.6
                            elif 'metrics' in data_sources:
                                base_confidence = 0.4
                            else:
                                base_confidence = 0.3
                            
                            if call_count > 0:
                                call_boost = min(0.2, (call_count ** 0.1) * 0.05)
                                confidence = min(1.0, base_confidence + call_boost)
                            else:
                                confidence = base_confidence
                            
                            edge_data.append({
                                'source': edge['source'],
                                'target': edge['target'],
                                'type': edge.get('type', 'calls'),
                                'call_count': call_count,
                                'confidence': round(confidence, 3),
                                'avg_duration': metrics.get('avg_duration', 0),
                                'error_rate': metrics.get('error_rate', 0),
                                'data_sources': ','.join(data_sources) if data_sources else 'unknown'
                            })
                        
                        session.run("""
                            UNWIND $edges AS edge
                            MATCH (s:Service {id: edge.source})
                            MATCH (t:Service {id: edge.target})
                            MERGE (s)-[r:CALLS]->(t)
                            SET r.type = edge.type,
                                r.call_count = edge.call_count,
                                r.confidence = edge.confidence,
                                r.avg_duration = edge.avg_duration,
                                r.error_rate = edge.error_rate,
                                r.data_sources = edge.data_sources,
                                r.last_updated = timestamp()
                        """, edges=edge_data)
                    
                    logger.info(f"Graph saved to Neo4j (batch): {len(nodes)} nodes, {len(edges)} edges")
            else:
                # 使用模拟实现
                logger.info(f"Saving graph (mock): {len(graph.get('nodes', []))} nodes, {len(graph.get('edges', []))} edges")
                self.graphs.append(graph)

            return True
        except Exception as e:
            logger.error(f"Error saving graph: {e}")
            return False

    def get_events(self, limit: int = 100, start_time: str = None, end_time: str = None) -> List[Dict[str, Any]]:
        """
        获取事件列表（从logs表）

        Args:
            limit: 返回数量限制，默认为 100
            start_time: 开始时间（ISO 8601格式）
            end_time: 结束时间（ISO 8601格式）

        Returns:
            List[Dict[str, Any]]: 事件列表，按时间倒序排列
        """
        try:
            if not self.ch_client:
                logger.warning("ClickHouse client not available")
                return []

            # 构建 PREWHERE 条件
            prewhere_conditions = []
            if start_time:
                prewhere_conditions.append(f"timestamp >= parseDateTime64BestEffort('{start_time}')")
            if end_time:
                prewhere_conditions.append(f"timestamp <= parseDateTime64BestEffort('{end_time}')")
            if not start_time and not end_time:
                prewhere_conditions.append(f"timestamp > now() - INTERVAL {_STATS_DEFAULT_WINDOW}")

            prewhere_clause = ""
            if prewhere_conditions:
                prewhere_clause = "PREWHERE " + " AND ".join(prewhere_conditions)

            safe_limit = _sanitize_limit(limit, default_value=100, max_value=10000)

            # 使用 ClickHouse Client 查询 logs 表
            query = f"""
            SELECT id, toString(timestamp) as timestamp_str, service_name, pod_name, namespace, node_name,
                   level, severity_number, message, trace_id, span_id, flags, labels, host_ip,
                   pod_id, container_name, container_id, container_image
            FROM logs.logs
            {prewhere_clause}
            ORDER BY timestamp DESC
            LIMIT {safe_limit}
            """
            result = self.ch_client.execute(query)

            events = []
            for row in result:
                if len(row) >= 10:
                    # 解析labels JSON
                    labels = {}
                    try:
                        if len(row) > 12 and row[12]:  # labels字段 (索引12)
                            labels = json.loads(row[12])
                    except:
                        pass

                    # 将 ClickHouse DateTime64 格式转换为 RFC 3339
                    ts_datetime64 = row[1]  # 已经是字符串格式
                    ts_rfc3339 = datetime64_to_rfc3339(ts_datetime64)

                    # ⭐ P0优化：从 severity_number 映射回 severity_text
                    severity_number = int(row[7]) if len(row) > 7 and row[7] is not None else 0
                    level_from_db = row[6]  # level字段

                    # v3.8.2: 如果 severity_number 为 0（未设置），使用 level 字段作为 severity
                    if severity_number == 0:
                        severity = level_from_db.upper() if level_from_db else 'INFO'
                    else:
                        SEVERITY_MAP = {
                            1: 'TRACE', 2: 'DEBUG', 3: 'INFO',
                            4: 'WARN', 5: 'ERROR', 9: 'FATAL',
                            10: 'TRACE', 11: 'TRACE', 12: 'DEBUG', 13: 'DEBUG',
                            14: 'INFO', 15: 'INFO', 16: 'WARN', 17: 'ERROR',
                            18: 'ERROR', 19: 'FATAL', 20: 'FATAL',
                            21: 'FATAL', 22: 'FATAL', 23: 'FATAL', 24: 'FATAL'
                        }
                        severity = SEVERITY_MAP.get(severity_number, 'INFO')

                    events.append({
                        'id': row[0],
                        'timestamp': ts_rfc3339,  # RFC 3339 格式
                        'entity': {
                            'type': 'service',
                            'name': row[2],
                            'instance': row[3]
                        },
                        'event': {
                            'type': 'log',
                            'level': row[6],
                            'name': 'log',
                            'raw': row[8]  # message is now at index 8
                        },
                        'context': {
                            'trace_id': row[9] if len(row) > 9 else '',
                            'span_id': row[10] if len(row) > 10 else '',
                            'host': row[5],
                            'k8s': {
                                'namespace': row[4],
                                'pod': row[3],
                                'node': row[5],
                                'host': row[5],
                                'host_ip': row[13] if len(row) > 13 else '',
                                'labels': labels,
                                'pod_id': row[14] if len(row) > 14 else '',
                                'container_name': row[15] if len(row) > 15 else '',
                                'container_id': row[16] if len(row) > 16 else '',
                                'container_image': row[17] if len(row) > 17 else ''
                            }
                        },
                        # ⭐ P0/P1优化：添加OTLP标准字段
                        'severity': severity,
                        'severity_number': severity_number,
                        'flags': int(row[11]) if len(row) > 11 and row[11] is not None else 0,
                        'relations': []
                    })

            logger.info(f"Retrieved {len(events)} events from ClickHouse logs table")
            return events

        except Exception as e:
            logger.error(f"Error getting events: {e}")
            import traceback
            traceback.print_exc()
            return []
    def get_graphs(self) -> List[Dict[str, Any]]:
        """
        获取拓扑图列表

        Returns:
            List[Dict[str, Any]]: 拓扑图列表
        """
        try:
            if self.neo4j_driver:
                # 使用真实的 Neo4j 客户端
                with self.neo4j_driver.session() as session:
                    # 获取所有节点
                    nodes_result = session.run("""
                        MATCH (s:Service)
                        RETURN s.id as id, s.name as name, s.type as type
                    """)
                    nodes = [
                        {
                            'id': record['id'],
                            'name': record['name'],
                            'type': record['type']
                        }
                        for record in nodes_result
                    ]

                    # 获取所有边
                    edges_result = session.run("""
                        MATCH (s:Service)-[r]->(t:Service)
                        RETURN s.id as source, t.id as target, type(r) as type
                    """)
                    edges = [
                        {
                            'source': record['source'],
                            'target': record['target'],
                            'type': record['type']
                        }
                        for record in edges_result
                    ]

                    logger.info(f"Retrieved graph from Neo4j: {len(nodes)} nodes, {len(edges)} edges")
                    return [{'nodes': nodes, 'edges': edges}]
            else:
                # 使用模拟实现
                logger.info("Retrieving graphs (mock)")
                return self.graphs
        except Exception as e:
            logger.error(f"Error getting graphs: {e}")
            return []

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
    def save_metrics(self, metrics_data: List[Dict[str, Any]]) -> bool:
        """
        保存 Metrics 数据到 ClickHouse

        Args:
            metrics_data: Metrics 数据点列表

        Returns:
            bool: 是否保存成功
        """
        try:
            if not self.ch_client and not self.ch_http_client:
                logger.warning("ClickHouse client not available, skipping metrics save")
                return False

            if not metrics_data:
                logger.debug("No metrics data to save")
                return True

            # 准备数据
            data = []
            for metric in metrics_data:
                metric_name = metric.get('metric_name', '')
                metric_type = metric.get('metric_type', 'unknown')
                timestamp_str = metric.get('timestamp', '')
                value = metric.get('value', 0.0)
                attributes = metric.get('attributes', {})
                service_name = metric.get('service_name', 'unknown')

                # 解析时间戳 - parse_any_timestamp 返回字符串，需要转换为 datetime
                ts_rfc3339 = parse_any_timestamp(timestamp_str)
                # 将 RFC 3339 字符串转换为 datetime 对象
                from datetime import datetime
                if 'T' in ts_rfc3339:
                    ts_clean = ts_rfc3339.replace('T', ' ').replace('Z', '')
                    if '.' in ts_clean:
                        # 保留微秒精度（6位）
                        parts = ts_clean.split('.')
                        dt_str = parts[0]
                        us_str = parts[1][:6] if len(parts) > 1 else '000000'
                        ts_datetime = datetime.strptime(f"{dt_str}.{us_str}", '%Y-%m-%d %H:%M:%S.%f')
                    else:
                        ts_datetime = datetime.strptime(ts_clean, '%Y-%m-%d %H:%M:%S')
                else:
                    ts_datetime = datetime.strptime(ts_rfc3339, '%Y-%m-%d %H:%M:%S')

                # 序列化 attributes
                attributes_json = json.dumps(attributes, ensure_ascii=False)

                data.append([
                    metric_name,           # metric_name
                    ts_datetime,           # timestamp
                    value,                 # value
                    attributes_json,       # labels
                    service_name           # service_name
                ])

            # 批量插入
            if self.ch_client:
                self.ch_client.execute(
                    'INSERT INTO logs.metrics (metric_name, timestamp, value_float64, attributes_json, service_name) VALUES',
                    data,
                    settings=_CH_ASYNC_INSERT_SETTINGS,
                )
            else:
                # 使用 HTTP 客户端
                query = 'INSERT INTO logs.metrics (metric_name, timestamp, value_float64, attributes_json, service_name) VALUES'
                self._execute_clickhouse_http(query, data)

            logger.info(f"Saved {len(metrics_data)} metrics to ClickHouse")
            return True

        except Exception as e:
            logger.error(f"Error saving metrics: {type(e).__name__}: {e}")
            return False

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
    def save_traces(self, traces_data: List[Dict[str, Any]]) -> bool:
        """
        保存 Traces 数据到 ClickHouse

        Args:
            traces_data: Spans 数据列表

        Returns:
            bool: 是否保存成功
        """
        try:
            if not self.ch_client and not self.ch_http_client:
                logger.warning("ClickHouse client not available, skipping traces save")
                return False

            if not traces_data:
                logger.debug("No traces data to save")
                return True

            # 准备数据 - 匹配 logs.traces 表结构
            data = []
            for span in traces_data:
                trace_id = span.get('trace_id', '')
                span_id = span.get('span_id', '')
                parent_span_id = span.get('parent_span_id', '')
                service_name = span.get('service_name', 'unknown')
                operation_name = span.get('operation_name', '')
                start_time_str = span.get('start_time', '')
                span_kind = span.get('span_kind', '')
                status = span.get('status_code', 'UNSET')
                tags = span.get('tags', '{}')
                duration_ms = span.get('duration_ms')
                if duration_ms in (None, ""):
                    duration_ns = span.get('duration_ns')
                    if duration_ns not in (None, ""):
                        try:
                            duration_ms = float(duration_ns) / 1_000_000.0
                        except (TypeError, ValueError):
                            duration_ms = 0.0
                if duration_ms in (None, ""):
                    tag_map: Dict[str, Any] = {}
                    if isinstance(tags, dict):
                        tag_map = tags
                    elif isinstance(tags, str):
                        try:
                            tag_map = json.loads(tags) if tags else {}
                        except Exception:
                            tag_map = {}
                    for candidate in ("duration_ms", "span.duration_ms"):
                        if candidate in tag_map:
                            try:
                                duration_ms = float(tag_map[candidate])
                                break
                            except (TypeError, ValueError):
                                continue
                    if duration_ms in (None, ""):
                        for candidate in ("duration_ns", "span.duration_ns"):
                            if candidate in tag_map:
                                try:
                                    duration_ms = float(tag_map[candidate]) / 1_000_000.0
                                    break
                                except (TypeError, ValueError):
                                    continue
                if duration_ms in (None, ""):
                    duration_ms = 0.0

                # 解析时间戳 - parse_any_timestamp 返回字符串，需要转换为 datetime
                ts_rfc3339 = parse_any_timestamp(start_time_str)
                # 将 RFC 3339 字符串转换为 datetime 对象
                from datetime import datetime
                if 'T' in ts_rfc3339:
                    ts_clean = ts_rfc3339.replace('T', ' ').replace('Z', '')
                    if '.' in ts_clean:
                        # 保留微秒精度（6位）
                        parts = ts_clean.split('.')
                        dt_str = parts[0]
                        us_str = parts[1][:6] if len(parts) > 1 else '000000'
                        timestamp = datetime.strptime(f"{dt_str}.{us_str}", '%Y-%m-%d %H:%M:%S.%f')
                    else:
                        timestamp = datetime.strptime(ts_clean, '%Y-%m-%d %H:%M:%S')
                else:
                    timestamp = datetime.strptime(ts_rfc3339, '%Y-%m-%d %H:%M:%S')

                # 将 tags 转换为 attributes_json
                attributes_json = tags if isinstance(tags, str) else json.dumps(tags)
                events_json = '{}'
                links_json = '{}'

                data.append([
                    timestamp,         # timestamp
                    trace_id,          # trace_id
                    span_id,           # span_id
                    parent_span_id,    # parent_span_id
                    service_name,      # service_name
                    operation_name,    # operation_name
                    span_kind,         # span_kind
                    status,            # status
                    float(duration_ms), # duration_ms
                    attributes_json,   # attributes_json
                    events_json,       # events_json
                    links_json         # links_json
                ])

            # 批量插入
            if self.ch_client:
                self.ch_client.execute(
                    'INSERT INTO logs.traces (timestamp, trace_id, span_id, parent_span_id, service_name, operation_name, span_kind, status, duration_ms, attributes_json, events_json, links_json) VALUES',
                    data,
                    settings=_CH_ASYNC_INSERT_SETTINGS,
                )
            else:
                # 使用 HTTP 客户端
                query = 'INSERT INTO logs.traces (timestamp, trace_id, span_id, parent_span_id, service_name, operation_name, span_kind, status, duration_ms, attributes_json, events_json, links_json) VALUES'
                self._execute_clickhouse_http(query, data)

            logger.info(f"Saved {len(traces_data)} traces to ClickHouse")
            return True

        except Exception as e:
            logger.error(f"Error saving traces: {type(e).__name__}: {e}")
            return False

    def get_metrics(self, limit: int = 100, service_name: str = None, metric_name: str = None) -> List[Dict[str, Any]]:
        """
        获取指标列表（从metrics表）

        Args:
            limit: 返回数量限制，默认为 100
            service_name: 服务名过滤
            metric_name: 指标名过滤

        Returns:
            List[Dict[str, Any]]: 指标列表，按时间倒序排列
        """
        try:
            if not self.ch_client:
                logger.warning("ClickHouse client not available")
                return []

            # 构建 PREWHERE 条件
            prewhere_conditions = [f"timestamp > now() - INTERVAL {_STATS_DEFAULT_WINDOW}"]
            if service_name:
                prewhere_conditions.append(f"service_name = '{service_name}'")
            if metric_name:
                prewhere_conditions.append(f"metric_name = '{metric_name}'")

            prewhere_clause = "PREWHERE " + " AND ".join(prewhere_conditions)
            safe_limit = _sanitize_limit(limit, default_value=100, max_value=10000)

            # 使用 ClickHouse Client 查询 metrics 表
            query = f"""
            SELECT metric_name, toString(timestamp) as timestamp_str, value_float64, attributes_json, service_name
            FROM logs.metrics
            {prewhere_clause}
            ORDER BY timestamp DESC
            LIMIT {safe_limit}
            """
            result = self.ch_client.execute(query)

            metrics = []
            for row in result:
                if len(row) >= 5:
                    # 解析attributes_json作为labels
                    labels = {}
                    try:
                        if row[3]:  # attributes_json字段
                            labels = json.loads(row[3])
                    except:
                        pass

                    metrics.append({
                        'metric_name': row[0],
                        'timestamp': row[1],
                        'value': float(row[2]) if row[2] is not None else 0.0,
                        'labels': labels,
                        'service_name': row[4]
                    })

            logger.info(f"Retrieved {len(metrics)} metrics from ClickHouse")
            return metrics

        except Exception as e:
            logger.error(f"Error getting metrics: {e}")
            return []

    def get_traces(self, limit: int = 100, service_name: str = None, trace_id: str = None) -> List[Dict[str, Any]]:
        """
        获取追踪列表（从traces表）

        Args:
            limit: 返回数量限制，默认为 100
            service_name: 服务名过滤
            trace_id: 追踪ID过滤

        Returns:
            List[Dict[str, Any]]: 追踪列表，按时间倒序排列
        """
        try:
            if not self.ch_client:
                logger.warning("ClickHouse client not available")
                return []

            # 构建 PREWHERE 条件
            prewhere_conditions = [f"timestamp > now() - INTERVAL {_STATS_DEFAULT_WINDOW}"]
            if service_name:
                prewhere_conditions.append(f"service_name = '{service_name}'")
            if trace_id:
                prewhere_conditions.append(f"trace_id = '{trace_id}'")

            prewhere_clause = "PREWHERE " + " AND ".join(prewhere_conditions)
            safe_limit = _sanitize_limit(limit, default_value=100, max_value=10000)

            # 使用 ClickHouse Client 查询 traces 表
            query = f"""
            SELECT trace_id, span_id, parent_span_id, service_name, operation_name,
                   toString(timestamp) as start_time_str, duration_ms, status, attributes_json AS tags
            FROM logs.traces
            {prewhere_clause}
            ORDER BY timestamp DESC
            LIMIT {safe_limit}
            """
            result = self.ch_client.execute(query)

            traces = []
            for row in result:
                if len(row) >= 9:
                    # 解析tags JSON
                    tags = {}
                    try:
                        if row[8]:  # tags字段
                            tags = json.loads(row[8])
                    except:
                        pass

                    traces.append({
                        'trace_id': row[0],
                        'span_id': row[1],
                        'parent_span_id': row[2],
                        'service_name': row[3],
                        'operation_name': row[4],
                        'start_time': row[5],
                        'duration_ms': int(row[6]),
                        'status': row[7],
                        'tags': tags
                    })

            logger.info(f"Retrieved {len(traces)} traces from ClickHouse")
            return traces

        except Exception as e:
            logger.error(f"Error getting traces: {e}")
            return []

    def get_trace_spans(self, trace_id: str) -> List[Dict[str, Any]]:
        """
        获取某个 trace 下的所有 spans（用于时间线可视化）

        Args:
            trace_id: Trace ID

        Returns:
            List[Dict[str, Any]]: Span 列表，按 start_time 排序
        """
        try:
            if not self.ch_client:
                logger.warning("ClickHouse client not available")
                return []

            query = f"""
            SELECT trace_id, span_id, parent_span_id, service_name, operation_name,
                   toString(timestamp) as start_time_str, duration_ms, status, attributes_json AS tags
            FROM logs.traces
            PREWHERE trace_id = '{trace_id}'
            ORDER BY timestamp ASC
            """
            result = self.ch_client.execute(query)

            spans = []
            for row in result:
                if len(row) >= 9:
                    # 解析 tags JSON
                    tags = {}
                    try:
                        if row[8]:  # tags 字段
                            tags = json.loads(row[8])
                    except:
                        pass

                    spans.append({
                        'trace_id': row[0],
                        'span_id': row[1],
                        'parent_span_id': row[2],
                        'service_name': row[3],
                        'operation_name': row[4],
                        'start_time': row[5],
                        'duration_ms': int(row[6]),
                        'status': row[7],
                        'tags': tags
                    })

            logger.info(f"Retrieved {len(spans)} spans for trace {trace_id}")
            return spans

        except Exception as e:
            logger.error(f"Error getting trace spans: {e}")
            return []

    def get_log_context(self, pod_name: str, timestamp: str, before_count: int = 5, after_count: int = 5) -> Dict[str, Any]:
        """
        获取日志上下文（前后N条日志）

        Args:
            pod_name: Pod 名称
            timestamp: 当前日志时间戳
            before_count: 前面的日志数量
            after_count: 后面的日志数量

        Returns:
            Dict[str, Any]: 上下文日志数据，包含 before, after, current
        """
        try:
            if not self.ch_client:
                logger.warning("ClickHouse client not available")
                return {"before": [], "after": [], "current": None}

            # 转换时间戳
            from utils.timestamp import rfc3339_to_datetime64, datetime64_to_rfc3339
            ts_datetime64 = rfc3339_to_datetime64(timestamp)

            # 查询之前的日志
            before_query = f"""
            SELECT id, toString(timestamp) as timestamp_str, service_name, level, message
            FROM logs.logs
            PREWHERE pod_name = '{pod_name}'
                AND timestamp < '{ts_datetime64}'
            ORDER BY timestamp DESC
            LIMIT {before_count}
            """
            before_result = self.ch_client.execute(before_query)
            before_logs = []
            for row in before_result:
                before_logs.append({
                    'id': row[0],
                    'timestamp': datetime64_to_rfc3339(row[1]),
                    'service_name': row[2],
                    'level': row[3],
                    'message': row[4],
                    'position': 'before',
                    'distance': 0,
                })

            # 查询之后的日志
            after_query = f"""
            SELECT id, toString(timestamp) as timestamp_str, service_name, level, message
            FROM logs.logs
            PREWHERE pod_name = '{pod_name}'
                AND timestamp > '{ts_datetime64}'
            ORDER BY timestamp ASC
            LIMIT {after_count}
            """
            after_result = self.ch_client.execute(after_query)
            after_logs = []
            for row in after_result:
                after_logs.append({
                    'id': row[0],
                    'timestamp': datetime64_to_rfc3339(row[1]),
                    'service_name': row[2],
                    'level': row[3],
                    'message': row[4],
                    'position': 'after',
                    'distance': 0,
                })

            # 查询当前日志
            current_query = f"""
            SELECT id, toString(timestamp) as timestamp_str, service_name, pod_name, namespace, level, message
            FROM logs.logs
            PREWHERE pod_name = '{pod_name}'
                AND timestamp = '{ts_datetime64}'
            LIMIT 1
            """
            current_result = self.ch_client.execute(current_query)
            current_log = None
            if len(current_result) > 0:
                row = current_result[0]
                current_log = {
                    'id': row[0],
                    'timestamp': datetime64_to_rfc3339(row[1]),
                    'service_name': row[2],
                    'pod_name': row[3],
                    'namespace': row[4],
                    'level': row[5],
                    'message': row[6],
                }

            logger.info(f"Retrieved log context: {len(before_logs)} before, {len(after_logs)} after")

            return {
                "before": before_logs,
                "after": after_logs,
                "current": current_log
            }

        except Exception as e:
            logger.error(f"Error getting log context: {e}")
            return {"before": [], "after": [], "current": None}

    def get_metrics_stats(self) -> Dict[str, Any]:
        """
        获取 Metrics 统计信息

        Returns:
            Dict[str, Any]: 统计数据
        """
        try:
            if not self.ch_client:
                return {"total": 0, "byService": {}, "byMetricName": {}}

            safe_time_window = _STATS_DEFAULT_WINDOW
            # 总数统计
            total_query = f"""
            SELECT COUNT(*) as total
            FROM logs.metrics
            PREWHERE timestamp > now() - INTERVAL {safe_time_window}
            """
            result = self.ch_client.execute(total_query)
            total = result[0][0] if result else 0

            # 按服务统计
            service_query = f"""
            SELECT service_name, COUNT(*) as count
            FROM logs.metrics
            PREWHERE timestamp > now() - INTERVAL {safe_time_window}
            GROUP BY service_name
            ORDER BY count DESC
            """
            service_result = self.ch_client.execute(service_query)
            by_service = {row[0]: row[1] for row in service_result}

            # 按指标名统计
            metric_query = f"""
            SELECT metric_name, COUNT(*) as count
            FROM logs.metrics
            PREWHERE timestamp > now() - INTERVAL {safe_time_window}
            GROUP BY metric_name
            ORDER BY count DESC
            """
            metric_result = self.ch_client.execute(metric_query)
            by_metric = {row[0]: row[1] for row in metric_result}

            return {
                "total": total,
                "byService": by_service,
                "byMetricName": by_metric,
            }
        except Exception as e:
            logger.error(f"Error getting metrics stats: {e}")
            return {"total": 0, "byService": {}, "byMetricName": {}}

    def get_traces_stats(self) -> Dict[str, Any]:
        """
        获取 Traces 统计信息

        Returns:
            Dict[str, Any]: 统计数据
        """
        try:
            if not self.ch_client:
                return {"total": 0, "byService": {}, "byOperation": {}, "avgDuration": 0}

            safe_time_window = _STATS_DEFAULT_WINDOW
            # 总数统计
            total_query = f"""
            SELECT COUNT(*) as total
            FROM logs.traces
            PREWHERE timestamp > now() - INTERVAL {safe_time_window}
            """
            result = self.ch_client.execute(total_query)
            total = result[0][0] if result else 0

            # 按服务统计
            service_query = f"""
            SELECT service_name, COUNT(*) as count
            FROM logs.traces
            PREWHERE timestamp > now() - INTERVAL {safe_time_window}
            GROUP BY service_name
            ORDER BY count DESC
            """
            service_result = self.ch_client.execute(service_query)
            by_service = {row[0]: row[1] for row in service_result}

            # 按操作统计
            operation_query = f"""
            SELECT operation_name, COUNT(*) as count
            FROM logs.traces
            PREWHERE timestamp > now() - INTERVAL {safe_time_window}
            GROUP BY operation_name
            ORDER BY count DESC
            """
            operation_result = self.ch_client.execute(operation_query)
            by_operation = {row[0]: row[1] for row in operation_result}

            # 平均持续时间
            duration_query = f"""
            SELECT AVG(duration_ms) as avg_duration
            FROM logs.traces
            PREWHERE timestamp > now() - INTERVAL {safe_time_window}
            """
            duration_result = self.ch_client.execute(duration_query)
            avg_duration = duration_result[0][0] if duration_result else 0

            return {
                "total": total,
                "byService": by_service,
                "byOperation": by_operation,
                "avgDuration": round(avg_duration, 2) if avg_duration else 0,
            }
        except Exception as e:
            logger.error(f"Error getting traces stats: {e}")
            return {"total": 0, "byService": {}, "byOperation": {}, "avgDuration": 0}

    def get_topology(self, limit: int = 1000, namespace: str = None) -> Dict[str, Any]:
        """
        获取服务拓扑图数据

        从 traces 表查询服务调用关系，构建拓扑图

        Args:
            limit: 查询的 traces 数量限制
            namespace: 命名空间过滤（可选）

        Returns:
            Dict[str, Any]: 拓扑图数据，包含 nodes 和 edges
            {
                "nodes": [
                    {
                        "id": "service-a",
                        "label": "Service A",
                        "type": "service",
                        "metrics": {"request_count": 100, "error_rate": 0.05}
                    }
                ],
                "edges": [
                    {
                        "source": "service-a",
                        "target": "service-b",
                        "label": "http",
                        "metrics": {"request_count": 50, "avg_duration": 120}
                    }
                ]
            }
        """
        try:
            if not self.ch_client:
                logger.warning("ClickHouse client not available")
                return {"nodes": [], "edges": []}
            safe_limit = _sanitize_limit(limit, default_value=1000, max_value=10000)
            safe_namespace = _escape_sql_literal(namespace) if namespace else None
            safe_time_window = _TOPOLOGY_DEFAULT_WINDOW

            # 构建查询条件
            where_conditions = []
            if safe_namespace:
                where_conditions.append(f"namespace = '{safe_namespace}'")

            where_clause = ""
            if where_conditions:
                where_clause = "WHERE " + " AND ".join(where_conditions)

            # 查询服务调用关系（从 traces 表）
            query = f"""
            SELECT
                service_name,
                parent_span_id,
                COUNT(*) as call_count,
                AVG(duration_ms) as avg_duration,
                SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) as error_count
            FROM logs.traces
            PREWHERE timestamp > now() - INTERVAL {safe_time_window}
            {where_clause}
            GROUP BY service_name, parent_span_id
            ORDER BY call_count DESC
            LIMIT {safe_limit}
            """

            result = self.ch_client.execute(query)

            # 构建节点和边
            nodes = {}  # {service_name: node_data}
            edges = []  # list of edge_data

            for row in result:
                if len(row) >= 5:
                    service_name = row[0]
                    parent_span_id = row[1]
                    call_count = int(row[2])
                    avg_duration = float(row[3]) if row[3] else 0
                    error_count = int(row[4])

                    # 添加服务节点
                    if service_name not in nodes:
                        nodes[service_name] = {
                            "id": service_name,
                            "label": service_name,
                            "type": "service",
                            "metrics": {
                                "request_count": 0,
                                "error_count": 0,
                                "avg_duration": 0,
                                "error_rate": 0
                            }
                        }

                    # 更新节点指标
                    nodes[service_name]["metrics"]["request_count"] += call_count
                    nodes[service_name]["metrics"]["error_count"] += error_count

                    # 如果有 parent_span_id，说明这是子服务，存在调用关系
                    if parent_span_id and parent_span_id != "":
                        # 这里简化处理：假设 parent_span_id 对应的服务
                        # 实际可能需要额外的查询来获取父服务名
                        # 暂时使用 "unknown" 作为占位
                        parent_service = "unknown"
                        if parent_service not in nodes:
                            nodes[parent_service] = {
                                "id": parent_service,
                                "label": parent_service,
                                "type": "service",
                                "metrics": {
                                    "request_count": 0,
                                    "error_count": 0,
                                    "avg_duration": 0,
                                    "error_rate": 0
                                }
                            }

                        # 添加边
                        edge_id = f"{parent_service}-{service_name}"
                        edges.append({
                            "id": edge_id,
                            "source": parent_service,
                            "target": service_name,
                            "label": "calls",
                            "metrics": {
                                "request_count": call_count,
                                "avg_duration": avg_duration,
                                "error_rate": error_count / call_count if call_count > 0 else 0
                            }
                        })

            # 计算每个节点的错误率和平均延迟
            for node in nodes.values():
                metrics = node["metrics"]
                if metrics["request_count"] > 0:
                    metrics["error_rate"] = metrics["error_count"] / metrics["request_count"]
                else:
                    metrics["error_rate"] = 0

            # 转换为列表
            nodes_list = list(nodes.values())

            logger.info(f"Retrieved topology: {len(nodes_list)} nodes, {len(edges)} edges")
            return {
                "nodes": nodes_list,
                "edges": edges
            }

        except Exception as e:
            logger.error(f"Error getting topology: {e}")
            return {"nodes": [], "edges": []}

    def get_topology_from_logs(
        self,
        limit: int = 1000,
        time_window: str = "1 HOUR"
    ) -> Dict[str, Any]:
        """
        ⭐ P0新增：从logs表构建服务拓扑图（降级方案）

        当traces表为空时，基于logs表中的service_name构建基础拓扑

        Args:
            limit: 查询数量限制
            time_window: 时间窗口（默认1小时）

        Returns:
            Dict[str, Any]: 拓扑图数据，包含 nodes 和 edges
        """
        try:
            if not self.ch_client:
                logger.warning("ClickHouse client not available")
                return {"nodes": [], "edges": []}

            safe_limit = _sanitize_limit(limit, default_value=1000, max_value=10000)
            safe_time_window = _sanitize_interval(time_window, default_value=_TOPOLOGY_DEFAULT_WINDOW)
            # 查询服务统计信息
            query = f"""
            SELECT
                service_name,
                COUNT(*) as log_count,
                COUNT(DISTINCT pod_name) as pod_count,
                COUNT(DISTINCT namespace) as namespace_count,
                SUM(CASE WHEN level = 'error' THEN 1 ELSE 0 END) as error_count,
                MAX(timestamp) as last_seen
            FROM logs.logs
            PREWHERE timestamp > now() - INTERVAL {safe_time_window}
            GROUP BY service_name
            ORDER BY log_count DESC
            LIMIT {safe_limit}
            """

            result = self.ch_client.execute(query)

            # 构建节点
            nodes = []
            for row in result:
                service_name, log_count, pod_count, namespace_count, error_count, last_seen = row

                node = {
                    "id": service_name,
                    "label": service_name,
                    "type": "service",
                    "name": service_name,
                    "metrics": {
                        "log_count": log_count,
                        "pod_count": pod_count,
                        "namespace_count": namespace_count,
                        "error_count": error_count,
                        "error_rate": error_count / log_count if log_count > 0 else 0,
                        "last_seen": str(last_seen)
                    },
                    "metadata": {
                        "type": "service",
                        "label": service_name,
                        "data_source": "logs"
                    }
                }
                nodes.append(node)

            # 构建边（基于服务名推测的调用关系）
            # 简单策略：如果服务名包含常见模式（如frontend-backend），则创建边
            edges = []
            service_names = [node["id"] for node in nodes]

            # 查找潜在的服务调用关系（基于日志内容或命名模式）
            for i, source in enumerate(service_names):
                for target in service_names[i+1:]:
                    # 如果两个服务名在同一命名空间，可能存在调用关系
                    # 这里使用简单的启发式规则
                    if self._is_service_pair_related(source, target):
                        edge = {
                            "id": f"{source}-{target}",
                            "source": source,
                            "target": target,
                            "label": "potential-calls",
                            "type": "calls",
                            "metrics": {
                                "confidence": "low",
                                "data_source": "logs_heuristic"
                            }
                        }
                        edges.append(edge)

            logger.info(f"Retrieved topology from logs: {len(nodes)} nodes, {len(edges)} edges")
            return {
                "nodes": nodes,
                "edges": edges,
                "metadata": {
                    "data_source": "logs",
                    "time_window": safe_time_window,
                    "total_log_count": sum(node["metrics"]["log_count"] for node in nodes)
                }
            }

        except Exception as e:
            logger.error(f"Error getting topology from logs: {e}")
            return {"nodes": [], "edges": []}

    def _is_service_pair_related(self, service1: str, service2: str) -> bool:
        """
        判断两个服务是否可能存在调用关系（启发式规则）

        Args:
            service1: 服务名1
            service2: 服务名2

        Returns:
            bool: 是否可能存在调用关系
        """
        # 规则1: frontend -> backend模式
        if "frontend" in service1.lower() and "backend" in service2.lower():
            return True
        if "frontend" in service2.lower() and "backend" in service1.lower():
            return True

        # 规则2: 服务 -> 数据库模式
        db_keywords = ["database", "db", "mysql", "postgres", "mongodb", "redis"]
        if any(keyword in service2.lower() for keyword in db_keywords):
            return True

        # 规则3: 服务 -> 缓存模式
        cache_keywords = ["cache", "redis", "memcached"]
        if any(keyword in service2.lower() for keyword in cache_keywords):
            return True

        # 规则4: registry常见被其他服务调用
        if "registry" in service2.lower() and service1.lower() not in ["registry"]:
            return True

        return False

    def execute_query(
        self, 
        query: str, 
        params: Dict[str, Any] = None,
        as_dict: bool = True
    ) -> List[Any]:
        """
        执行 ClickHouse 查询并返回结果

        Args:
            query: SQL 查询语句（支持参数化）
            params: 查询参数字典
            as_dict: 是否返回字典列表（默认 True）

        Returns:
            List[Any] 或 List[Dict[str, Any]]: 查询结果
        """
        if not self.ch_client:
            logger.warning("ClickHouse client not available")
            return []

        try:
            if as_dict:
                result = self.ch_client.execute(
                    query, 
                    params or {},
                    with_column_types=True
                )
                if not result or len(result) < 2:
                    return []
                rows, columns = result
                column_names = [col[0] for col in columns]
                return [dict(zip(column_names, row)) for row in rows]
            else:
                return self.ch_client.execute(query, params or {})
        except Exception as e:
            logger.error(f"Error executing query: {e}")
            return []

    def execute_neo4j_query(self, query: str, parameters: Dict = None) -> List[Dict]:
        """
        执行 Neo4j 查询并返回结果

        Args:
            query: Cypher 查询语句
            parameters: 查询参数

        Returns:
            List[Dict]: 查询结果列表
        """
        if not self.neo4j_driver:
            logger.warning("Neo4j driver not available")
            return []

        try:
            with self.neo4j_driver.session() as session:
                result = session.run(query, parameters or {})
                return [record.data() for record in result]
        except Exception as e:
            logger.error(f"Error executing Neo4j query: {e}")
            return []

    def close(self):
        """
        关闭数据库连接

        释放所有数据库连接资源
        """
        if self.neo4j_driver:
            self.neo4j_driver.close()
            logger.info("Neo4j driver closed")

        logger.info("Storage adapter closed")
