"""
Semantic Engine Storage 模块
负责与数据库交互，提供统一的存储接口
使用 ClickHouse HTTP 接口和 Neo4j Bolt 协议

符合标准：
- RFC 3339 时间戳格式（ISO 8601）
- ClickHouse DateTime64(9) 纳秒精度存储
"""
from typing import Dict, Any, List, Callable
import logging
import json
import random
import time
import urllib.parse
import urllib.request
import urllib.error
import threading
from datetime import datetime, timedelta, timezone
from tenacity import retry, stop_after_attempt, wait_exponential
import sys
import os
import re

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

logger = logging.getLogger(__name__)
# clickhouse-driver 在连接空闲被服务端关闭后会输出 WARNING，
# 但后续会自动重连。将连接层日志提高到 ERROR 以减少噪音告警。
logging.getLogger("clickhouse_driver.connection").setLevel(logging.ERROR)
_CH_ASYNC_INSERT_SETTINGS = {
    "async_insert": 1,
    "wait_for_async_insert": 0,
}


def _log_event(level: int, message: str, event_id: str, **kwargs: Any) -> None:
    """统一事件化日志输出。"""
    extra: Dict[str, Any] = {"event_id": event_id}
    for key, value in kwargs.items():
        if value is None:
            continue
        extra[key] = value
    logger.log(level, message, extra=extra)


class _ThreadLocalClickHouseClientProxy:
    """
    ClickHouse Native 客户端线程本地代理。

    目标：
    - 同一线程内复用同一个 Client，减少频繁建连开销；
    - 不同线程隔离 Client，避免单连接并发访问导致异常。
    """

    def __init__(self, client_factory: Callable[[], Any]):
        self._client_factory = client_factory
        self._thread_local = threading.local()
        self._clients: List[Any] = []
        self._clients_lock = threading.Lock()

    def _get_or_create_client(self) -> Any:
        """获取当前线程专属 Client，不存在则懒加载创建。"""
        client = getattr(self._thread_local, "client", None)
        if client is not None:
            return client

        client = self._client_factory()
        self._thread_local.client = client
        with self._clients_lock:
            self._clients.append(client)
        return client

    def execute(self, *args, **kwargs):
        """委托 execute 调用到线程专属 Client。"""
        return self._get_or_create_client().execute(*args, **kwargs)

    def disconnect(self) -> None:
        """关闭所有已创建的线程专属 Client。"""
        with self._clients_lock:
            clients = list(self._clients)
            self._clients.clear()

        for client in clients:
            try:
                client.disconnect()
            except Exception as exc:
                logger.debug("Failed to disconnect ClickHouse native client: %s", exc)

        self._thread_local = threading.local()

    def __getattr__(self, item: str):
        """
        透传其他属性/方法到线程专属 Client，兼容既有调用方式。
        """
        client = self._get_or_create_client()
        return getattr(client, item)


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


def _read_int_env(name: str, default_value: int) -> int:
    """读取整数环境变量，异常时回退默认值。"""
    raw = os.getenv(name, str(default_value))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default_value
    return max(value, 1)


def _read_float_env(name: str, default_value: float) -> float:
    """读取浮点环境变量，限制在 [0, 1] 区间。"""
    raw = os.getenv(name, str(default_value))
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default_value
    return min(max(value, 0.0), 1.0)


def _normalize_trace_status(value: Any) -> str:
    """统一 trace status 到 STATUS_CODE_* 枚举。"""
    normalized = str(value or "").strip().upper()
    if normalized in {"2", "ERROR", "STATUS_CODE_ERROR"}:
        return "STATUS_CODE_ERROR"
    if normalized in {"1", "OK", "STATUS_CODE_OK"}:
        return "STATUS_CODE_OK"
    return "STATUS_CODE_UNSET"


SLOW_QUERY_THRESHOLD_MS = _read_int_env("SLOW_QUERY_THRESHOLD_MS", 500)
AGG_QUERY_LOG_SAMPLE_RATE = _read_float_env("AGG_QUERY_LOG_SAMPLE_RATE", 0.2)
QUERY_LOG_MAX_CHARS = _read_int_env("QUERY_LOG_MAX_CHARS", 1200)
CH_NATIVE_CONNECT_TIMEOUT_SECONDS = _read_int_env("CH_NATIVE_CONNECT_TIMEOUT_SECONDS", 3)
CH_NATIVE_SEND_RECEIVE_TIMEOUT_SECONDS = _read_int_env("CH_NATIVE_SEND_RECEIVE_TIMEOUT_SECONDS", 30)
CH_NATIVE_SYNC_REQUEST_TIMEOUT_SECONDS = _read_int_env("CH_NATIVE_SYNC_REQUEST_TIMEOUT_SECONDS", 30)
CH_HTTP_TIMEOUT_SECONDS = _read_int_env("CH_HTTP_TIMEOUT_SECONDS", 30)
_STATS_DEFAULT_WINDOW = _sanitize_interval(os.getenv("CH_STATS_TIME_WINDOW", "24 HOUR"), default_value="24 HOUR")
_TOPOLOGY_DEFAULT_WINDOW = _sanitize_interval(
    os.getenv("CH_TOPOLOGY_TIME_WINDOW", "24 HOUR"),
    default_value="24 HOUR",
)


def _compact_sql(sql: str) -> str:
    """压缩 SQL 文本，便于日志输出。"""
    return " ".join((sql or "").split())


def _clip_sql(sql: str, max_chars: int = QUERY_LOG_MAX_CHARS) -> str:
    """裁剪 SQL 文本长度，避免日志过长。"""
    compacted = _compact_sql(sql)
    if len(compacted) <= max_chars:
        return compacted
    return f"{compacted[:max_chars]} ...[truncated]"


def _is_aggregation_query(sql: str) -> bool:
    """识别高频聚合查询，用于日志采样。"""
    normalized = f" {_compact_sql(sql).lower()} "
    if " group by " in normalized:
        return True
    aggregation_tokens = (" count(", " sum(", " avg(", " quantile", " uniq(", " max(", " min(")
    return any(token in normalized for token in aggregation_tokens)


def _should_log_query_info(sql: str) -> bool:
    """高频聚合查询采样记录，其他查询全量记录。"""
    if not _is_aggregation_query(sql):
        return True
    return random.random() < AGG_QUERY_LOG_SAMPLE_RATE


def _parse_json_object_payload(
    payload: Any,
    *,
    field_name: str,
    event_id: str,
    **context: Any,
) -> Dict[str, Any]:
    """解析 JSON 对象字段，失败时记录结构化告警并返回空字典。"""
    if payload in (None, "", b"", bytearray()):
        return {}

    if isinstance(payload, dict):
        return payload

    if isinstance(payload, (str, bytes, bytearray)):
        try:
            parsed = json.loads(payload)
        except (json.JSONDecodeError, TypeError) as exc:
            _log_event(
                logging.WARNING,
                "Failed to parse JSON object payload",
                event_id=event_id,
                field=field_name,
                error_type=type(exc).__name__,
                error=str(exc),
                payload_preview=str(payload)[:200],
                **context,
            )
            return {}

        if isinstance(parsed, dict):
            return parsed

        _log_event(
            logging.WARNING,
            "JSON payload is not an object",
            event_id=event_id,
            field=field_name,
            payload_type=type(parsed).__name__,
            payload_preview=str(payload)[:200],
            **context,
        )
        return {}

    _log_event(
        logging.WARNING,
        "Unexpected payload type for JSON object field",
        event_id=event_id,
        field=field_name,
        payload_type=type(payload).__name__,
        **context,
    )
    return {}


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

        self.http_session = requests.Session() if REQUESTS_AVAILABLE else None

        # 初始化 ClickHouse 客户端
        logger.info(f"Attempting to connect to ClickHouse at {self.config['clickhouse']['host']}:{self.config['clickhouse']['port']}")
        
        self.ch_client = None
        self.ch_http_client = None
        
        # 首先尝试原生驱动
        if CLICKHOUSE_DRIVER_AVAILABLE:
            try:
                self.ch_client = _ThreadLocalClickHouseClientProxy(
                    client_factory=self._create_native_clickhouse_client
                )
                # 测试连接
                self.ch_client.execute('SELECT 1')
                logger.info("ClickHouse driver client connected successfully (thread-local)")
            except Exception as e:
                logger.warning(f"ClickHouse driver connection failed: {e}, using HTTP client instead")
                self.ch_client = None
        
        # 如果原生驱动失败或不可用，尝试 HTTP 客户端
        if not self.ch_client:
            logger.info("Initializing ClickHouse HTTP client")
            self._init_http_client()

        self.ch_database = self.config['clickhouse'].get('database', 'logs')

        # 初始化 Neo4j 客户端
        self.neo4j_driver = None
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
        self._traces_namespace_column_exists_cache: Any = None
        self._traces_namespace_column_exists_cache_expires_at: float = 0.0
        self._trace_edges_schema_cache: Any = None
        self._trace_edges_schema_cache_expires_at: float = 0.0

        # 初始化数据库表
        self._init_tables()

    def _has_traces_namespace_column(self) -> bool:
        """探测 logs.traces 是否已存在 traces_namespace 物化列。"""
        if (
            self._traces_namespace_column_exists_cache is not None
            and time.time() < float(self._traces_namespace_column_exists_cache_expires_at or 0.0)
        ):
            return bool(self._traces_namespace_column_exists_cache)

        try:
            rows = self.execute_query(
                """
                SELECT count() AS cnt
                FROM system.columns
                WHERE database = {database:String}
                  AND table = 'traces'
                  AND name = 'traces_namespace'
                """,
                {"database": self.ch_database},
            )
            exists = int(rows[0].get("cnt", 0) or 0) > 0 if rows else False
            self._traces_namespace_column_exists_cache = exists
            self._traces_namespace_column_exists_cache_expires_at = time.time() + 300.0
            return exists
        except Exception as exc:
            logger.debug("Failed to inspect traces_namespace column: %s", exc)
            self._traces_namespace_column_exists_cache = False
            self._traces_namespace_column_exists_cache_expires_at = time.time() + 60.0
            return False

    def _get_trace_edges_schema(self) -> Dict[str, Any]:
        """探测 logs.trace_edges_1m 是否存在及可用列。"""
        cached = self._trace_edges_schema_cache
        if isinstance(cached, dict) and time.time() < float(self._trace_edges_schema_cache_expires_at or 0.0):
            return dict(cached)

        schema: Dict[str, Any] = {
            "table_exists": False,
            "has_namespace": False,
            "has_timeout_count": False,
            "has_retries_sum": False,
            "has_pending_sum": False,
            "has_dlq_sum": False,
            "has_p95_ms": False,
            "has_p99_ms": False,
            "has_duration_sum_ms": False,
        }
        try:
            table_rows = self.execute_query(
                """
                SELECT count() AS cnt
                FROM system.tables
                WHERE database = {database:String}
                  AND name = 'trace_edges_1m'
                """,
                {"database": self.ch_database},
            )
            table_exists = int(table_rows[0].get("cnt", 0) or 0) > 0 if table_rows else False
            schema["table_exists"] = table_exists
            if not table_exists:
                self._trace_edges_schema_cache = schema
                return dict(schema)

            column_rows = self.execute_query(
                """
                SELECT name
                FROM system.columns
                WHERE database = {database:String}
                  AND table = 'trace_edges_1m'
                """,
                {"database": self.ch_database},
            )
            column_set = {
                str(row.get("name") or "").strip()
                for row in column_rows
                if isinstance(row, dict)
            }
            schema["has_namespace"] = "namespace" in column_set
            schema["has_timeout_count"] = "timeout_count" in column_set
            schema["has_retries_sum"] = "retries_sum" in column_set
            schema["has_pending_sum"] = "pending_sum" in column_set
            schema["has_dlq_sum"] = "dlq_sum" in column_set
            schema["has_p95_ms"] = "p95_ms" in column_set
            schema["has_p99_ms"] = "p99_ms" in column_set
            schema["has_duration_sum_ms"] = "duration_sum_ms" in column_set
        except Exception as exc:
            logger.debug("Failed to inspect trace_edges_1m schema: %s", exc)

        self._trace_edges_schema_cache = schema
        self._trace_edges_schema_cache_expires_at = time.time() + 120.0
        return dict(schema)

    def _format_edge_red_metrics_rows(self, rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        metrics: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            source = row.get("source_service")
            target = row.get("target_service")
            if not source or not target:
                continue
            key = f"{source}->{target}"
            metrics[key] = {
                "call_count": int(row.get("call_count") or 0),
                "error_count": int(row.get("error_count") or 0),
                "error_rate": float(row.get("error_rate") or 0.0),
                "p95": float(row.get("p95") or 0.0),
                "p99": float(row.get("p99") or 0.0),
                "timeout_rate": float(row.get("timeout_rate") or 0.0),
                "retries": float(row.get("retries") or 0.0),
                "pending": float(row.get("pending") or 0.0),
                "dlq": float(row.get("dlq") or 0.0),
            }
        return metrics

    def _query_edge_red_metrics_from_trace_edges(
        self,
        safe_time_window: str,
        namespace: str = None,
    ) -> Dict[str, Dict[str, Any]]:
        """优先从预聚合 trace_edges_1m 读取边 RED 指标。"""
        schema = self._get_trace_edges_schema()
        if not bool(schema.get("table_exists")):
            return {}

        params: Dict[str, Any] = {}
        namespace_prewhere_filter = ""
        if namespace:
            if not bool(schema.get("has_namespace")):
                return {}
            namespace_prewhere_filter = " AND namespace = {namespace:String}"
            params["namespace"] = namespace

        # 兼容 release-2 老结构（仅 call/error/avg_duration），以及 release-3 扩展结构。
        if all(
            bool(schema.get(flag))
            for flag in (
                "has_timeout_count",
                "has_retries_sum",
                "has_pending_sum",
                "has_dlq_sum",
                "has_p95_ms",
                "has_p99_ms",
            )
        ):
            query = f"""
            SELECT
                source_service,
                target_service,
                sum(call_count) AS call_count,
                sum(error_count) AS error_count,
                if(sum(call_count) = 0, 0, sum(error_count) / sum(call_count)) AS error_rate,
                max(p95_ms) AS p95,
                max(p99_ms) AS p99,
                if(sum(call_count) = 0, 0, sum(timeout_count) / sum(call_count)) AS timeout_rate,
                if(sum(call_count) = 0, 0, sum(retries_sum) / sum(call_count)) AS retries,
                if(sum(call_count) = 0, 0, sum(pending_sum) / sum(call_count)) AS pending,
                if(sum(call_count) = 0, 0, sum(dlq_sum) / sum(call_count)) AS dlq
            FROM logs.trace_edges_1m
            PREWHERE ts_minute > now() - INTERVAL {safe_time_window}
            {namespace_prewhere_filter}
            WHERE notEmpty(source_service)
              AND notEmpty(target_service)
              AND source_service != target_service
            GROUP BY source_service, target_service
            """
        else:
            query = f"""
            SELECT
                source_service,
                target_service,
                sum(call_count) AS call_count,
                sum(error_count) AS error_count,
                if(sum(call_count) = 0, 0, sum(error_count) / sum(call_count)) AS error_rate,
                if(sum(call_count) = 0, 0, sum(avg_duration_ms * call_count) / sum(call_count)) AS p95,
                if(sum(call_count) = 0, 0, sum(avg_duration_ms * call_count) / sum(call_count)) AS p99,
                toFloat64(0.0) AS timeout_rate,
                toFloat64(0.0) AS retries,
                toFloat64(0.0) AS pending,
                toFloat64(0.0) AS dlq
            FROM logs.trace_edges_1m
            PREWHERE ts_minute > now() - INTERVAL {safe_time_window}
            {namespace_prewhere_filter}
            WHERE notEmpty(source_service)
              AND notEmpty(target_service)
              AND source_service != target_service
            GROUP BY source_service, target_service
            """

        try:
            rows = self.execute_query(query, params=params if params else None)
            return self._format_edge_red_metrics_rows(rows)
        except Exception as exc:
            logger.debug("Failed to query trace_edges_1m aggregation, fallback to traces: %s", exc)
            return {}

    def _query_edge_red_metrics_from_traces_self_join(
        self,
        safe_time_window: str,
        namespace: str = None,
    ) -> Dict[str, Dict[str, Any]]:
        """回退路径：直接基于 traces 父子 span 关系实时聚合。"""
        namespace_prewhere_filter = ""
        params: Dict[str, Any] = {}
        if namespace:
            if self._has_traces_namespace_column():
                namespace_prewhere_filter = " AND child.traces_namespace = {namespace:String}"
            else:
                namespace_prewhere_filter = (
                    " AND ("
                    "JSONExtractString(child.attributes_json, 'k8s.namespace.name') = {namespace:String} "
                    "OR JSONExtractString(child.attributes_json, 'service_namespace') = {namespace:String} "
                    "OR JSONExtractString(child.attributes_json, 'namespace') = {namespace:String}"
                    ")"
                )
            params["namespace"] = namespace

        query = f"""
        SELECT
            source_service,
            target_service,
            sum(calls_per_minute) AS call_count,
            sum(errors_per_minute) AS error_count,
            if(sum(calls_per_minute) = 0, 0, sum(errors_per_minute) / sum(calls_per_minute)) AS error_rate,
            max(p95_per_minute) AS p95,
            max(p99_per_minute) AS p99,
            if(sum(calls_per_minute) = 0, 0, sum(timeouts_per_minute) / sum(calls_per_minute)) AS timeout_rate,
            avg(retries_per_minute) AS retries,
            avg(pending_per_minute) AS pending,
            avg(dlq_per_minute) AS dlq
        FROM (
            SELECT
                source_service,
                target_service,
                ts_minute,
                count() AS calls_per_minute,
                countIf(lower(toString(status)) IN ('error', 'failed', 'status_code_error', '2')) AS errors_per_minute,
                quantileTDigest(0.95)(span_duration_ms) AS p95_per_minute,
                quantileTDigest(0.99)(span_duration_ms) AS p99_per_minute,
                countIf(span_duration_ms >= 1000) AS timeouts_per_minute,
                avg(retries_value) AS retries_per_minute,
                avg(pending_value) AS pending_per_minute,
                avg(dlq_value) AS dlq_per_minute
            FROM (
                SELECT
                    parent.service_name AS source_service,
                    child.service_name AS target_service,
                    child.status AS status,
                    toStartOfMinute(child.timestamp) AS ts_minute,
                    greatest(
                        toFloat64OrZero(toString(child.duration_ms)),
                        toFloat64OrZero(JSONExtractString(child.attributes_json, 'duration_ms')),
                        toFloat64OrZero(JSONExtractString(child.attributes_json, 'duration')),
                        toFloat64OrZero(JSONExtractString(child.attributes_json, 'elapsed_ms'))
                    ) AS span_duration_ms,
                    greatest(
                        toFloat64OrZero(JSONExtractString(child.attributes_json, 'retry_count')),
                        toFloat64OrZero(JSONExtractString(child.attributes_json, 'retries'))
                    ) AS retries_value,
                    greatest(
                        toFloat64OrZero(JSONExtractString(child.attributes_json, 'pending')),
                        toFloat64OrZero(JSONExtractString(child.attributes_json, 'pending_count'))
                    ) AS pending_value,
                    greatest(
                        toFloat64OrZero(JSONExtractString(child.attributes_json, 'dlq')),
                        toFloat64OrZero(JSONExtractString(child.attributes_json, 'dlq_count'))
                    ) AS dlq_value
                FROM logs.traces AS child
                INNER JOIN logs.traces AS parent
                    ON child.trace_id = parent.trace_id
                   AND child.parent_span_id = parent.span_id
                PREWHERE child.timestamp > now() - INTERVAL {safe_time_window}
                {namespace_prewhere_filter}
                WHERE notEmpty(child.parent_span_id)
                  AND notEmpty(child.service_name)
                  AND notEmpty(parent.service_name)
                  AND child.service_name != parent.service_name
            )
            GROUP BY source_service, target_service, ts_minute
        )
        GROUP BY source_service, target_service
        """
        rows = self.execute_query(query, params=params if params else None)
        return self._format_edge_red_metrics_rows(rows)

    def _create_native_clickhouse_client(self):
        """创建一个新的 ClickHouse Native Client 实例。"""
        return ClickHouseClient(
            host=self.config['clickhouse']['host'],
            port=self.config['clickhouse']['port'],
            database=self.config['clickhouse']['database'],
            user=self.config['clickhouse']['user'],
            password=self.config['clickhouse']['password'],
            connect_timeout=CH_NATIVE_CONNECT_TIMEOUT_SECONDS,
            send_receive_timeout=CH_NATIVE_SEND_RECEIVE_TIMEOUT_SECONDS,
            sync_request_timeout=CH_NATIVE_SYNC_REQUEST_TIMEOUT_SECONDS,
            settings={'use_numpy': False}
        )

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
            logger.error(f"ClickHouse HTTP client connection failed: {e}")
            import traceback
            traceback.print_exc()
            self.ch_http_client = None

    def _execute_clickhouse_http_ddl(self, query: str) -> None:
        """通过 HTTP 执行 DDL 语句（不附加 FORMAT JSON）。"""
        if not self.ch_http_client:
            raise Exception("ClickHouse HTTP client not available")

        url = self.ch_http_client['url']
        database = self.ch_http_client['database']
        user = self.ch_http_client['user']
        password = self.ch_http_client['password']

        params = {
            'query': query,
            'database': database,
        }
        auth = (user, password) if password else None
        client = self.http_session if self.http_session is not None else requests
        response = client.post(url, params=params, auth=auth, timeout=CH_HTTP_TIMEOUT_SECONDS)
        if response.status_code != 200:
            raise Exception(f"HTTP {response.status_code}: {response.text}")


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
            if self.ch_client:
                self.ch_client.execute(create_events_table)
            else:
                self._execute_clickhouse_http_ddl(create_events_table)
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
            if self.ch_client:
                self.ch_client.execute(create_logs_table)
            else:
                self._execute_clickhouse_http_ddl(create_logs_table)
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

        def _extract_insert_columns(insert_sql: str) -> List[str]:
            """从 INSERT INTO ... (col1, col2) VALUES 语句中提取列名。"""
            matched = re.search(
                r"INSERT\s+INTO\s+[^(]+\((?P<columns>[^)]+)\)\s*VALUES\s*$",
                str(insert_sql or "").strip(),
                flags=re.IGNORECASE | re.DOTALL,
            )
            if not matched:
                return []
            return [
                col.strip().strip("`").strip('"')
                for col in matched.group("columns").split(",")
                if col.strip()
            ]

        # 构建 SQL 查询
        if data:
            # INSERT 查询 - 使用 FORMAT JSONEachRow
            from datetime import datetime

            # 转换数据为 JSON 格式
            insert_columns = _extract_insert_columns(query)
            formatted_data = []
            for row in data:
                if isinstance(row, dict):
                    row_dict = dict(row)
                else:
                    if not insert_columns:
                        raise ValueError("Cannot parse INSERT columns for HTTP batch insert")
                    row_dict = {}
                    for i, col in enumerate(insert_columns):
                        val = row[i] if i < len(row) else ''
                        # 转换 datetime 为字符串
                        if isinstance(val, datetime):
                            val = val.strftime('%Y-%m-%d %H:%M:%S.%f')
                        row_dict[col] = val
                formatted_data.append(row_dict)

            # 构建 INSERT 查询
            insert_query = re.sub(
                r"\bVALUES\s*$",
                "",
                str(query or "").strip(),
                flags=re.IGNORECASE | re.DOTALL,
            ).strip() + " FORMAT JSONEachRow"
            body = '\n'.join(json.dumps(row) for row in formatted_data)

            params = {
                'query': insert_query,
                'database': database
            }

            auth = (user, password) if password else None
            client = self.http_session if self.http_session is not None else requests
            response = client.post(url, params=params, data=body, auth=auth, timeout=CH_HTTP_TIMEOUT_SECONDS)
        else:
            # SELECT 查询 - 使用 FORMAT JSON
            params = {
                'query': f"{query} FORMAT JSON",
                'database': database
            }

            auth = (user, password) if password else None
            client = self.http_session if self.http_session is not None else requests
            response = client.get(url, params=params, auth=auth, timeout=CH_HTTP_TIMEOUT_SECONDS)

        if response.status_code != 200:
            raise Exception(f"HTTP {response.status_code}: {response.text}")

        # 解析 JSON 响应；非 JSON 时记录告警，避免静默吞异常
        try:
            return json.loads(response.text)
        except json.JSONDecodeError as exc:
            _log_event(
                logging.WARNING,
                "ClickHouse HTTP response is not valid JSON",
                event_id="CH_HTTP_NON_JSON_RESPONSE",
                error_type=type(exc).__name__,
                error=str(exc),
                sql=_clip_sql(query),
                response_preview=str(response.text)[:200],
            )
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
            _log_event(
                logging.ERROR,
                "Error saving event",
                event_id="CH_EVENT_SAVE_ERROR",
                action="clickhouse.insert",
                outcome="failed",
                error_type=type(e).__name__,
                error=str(e),
            )
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

            client = self.http_session if self.http_session is not None else requests
            response = client.post(url, params=params, data=json.dumps(row), auth=auth, timeout=CH_HTTP_TIMEOUT_SECONDS)

            if response.status_code == 200:
                logger.debug("Event saved to ClickHouse (HTTP): %s", event.get("id", "unknown"))
                return True
            else:
                raise Exception(f"HTTP {response.status_code}: {response.text}")

        except Exception as e:
            _log_event(
                logging.ERROR,
                "Error saving event via HTTP",
                event_id="CH_EVENT_SAVE_HTTP_ERROR",
                action="clickhouse.insert",
                outcome="failed",
                error_type=type(e).__name__,
                error=str(e),
            )
            raise  # 重新抛出异常以触发重试

    def _save_event_native(self, event: Dict[str, Any]) -> bool:
        """使用原生驱动保存事件"""
        try:
            # 提取 K8s 上下文信息
            k8s_context = event.get('context', {}).get('k8s', {})
            # ⭐ 调试：打印 k8s_context 和 labels
            logger.debug("[storage] k8s_context keys=%s", list(k8s_context.keys()) if k8s_context else "empty")
            logger.debug("[storage] k8s_context.labels=%s", k8s_context.get("labels", "NOT_FOUND"))
            labels_for_storage = k8s_context.get('labels', {})
            logger.debug(
                "[storage] labels_for_storage type=%s value=%s",
                type(labels_for_storage),
                labels_for_storage,
            )

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
            # ⭐ 调试：打印准备插入的 labels 值
            labels_to_insert = json.dumps(k8s_context.get('labels', {}) or {}, ensure_ascii=False)
            logger.debug("[storage] labels_to_insert=%s", labels_to_insert)
            logger.debug("[storage] labels_to_insert_length=%s", len(labels_to_insert))
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
                _log_event(
                    logging.ERROR,
                    "Error executing INSERT",
                    event_id="CH_EVENT_INSERT_NATIVE_ERROR",
                    action="clickhouse.insert",
                    outcome="failed",
                    error_type=type(insert_error).__name__,
                    error=str(insert_error),
                    sample_id=data[0][0],
                    sample_service=data[0][3],
                    sample_message_len=len(data[0][13]),
                )
                raise

            # ⭐ 同时保存到 events 表（语义化事件）
            self._save_semantic_event(event, k8s_context, host, host_ip)

            logger.debug("Event saved to ClickHouse (Native): %s", event.get("id", "unknown"))
            return True

        except Exception as e:
            _log_event(
                logging.ERROR,
                "Error saving event via native driver",
                event_id="CH_EVENT_SAVE_NATIVE_ERROR",
                action="clickhouse.insert",
                outcome="failed",
                error_type=type(e).__name__,
                error=str(e),
            )
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

                    logger.debug(
                        "Relation saved to Neo4j: %s from %s to %s",
                        relation["type"],
                        relation["source"],
                        relation["target"],
                    )
            else:
                # 使用模拟实现
                logger.debug(
                    "Saving relation (mock): %s from %s to %s",
                    relation["type"],
                    relation["source"],
                    relation["target"],
                )

            return True
        except Exception as e:
            _log_event(
                logging.ERROR,
                "Error saving relation",
                event_id="NEO4J_RELATION_SAVE_ERROR",
                action="neo4j.relation.save",
                outcome="failed",
                error_type=type(e).__name__,
                error=str(e),
            )
            return False

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
    def save_graph(self, graph: Dict[str, Any]) -> bool:
        """
        保存拓扑图到 Neo4j

        使用重试机制，最多重试 3 次

        Args:
            graph: 拓扑图数据，包含节点和边信息

        Returns:
            bool: 是否保存成功
        """
        try:
            if self.neo4j_driver:
                # 使用真实的 Neo4j 客户端
                with self.neo4j_driver.session() as session:
                    # 创建所有节点
                    for node in graph.get('nodes', []):
                        session.run("""
                            MERGE (s:Service {id: $id})
                            SET s.name = $name, s.type = $type
                        """, id=node['id'], name=node['id'], type=node.get('type', 'service'))

                    # 创建所有边（包含 call_count 和 confidence）
                    for edge in graph.get('edges', []):
                        # 从 metrics 提取 call_count 和计算 confidence
                        metrics = edge.get('metrics', {})
                        call_count = metrics.get('call_count', metrics.get('request_count', 0))

                        # 计算 confidence：基于数据源和调用次数
                        data_sources = metrics.get('data_sources', [])
                        if 'traces' in data_sources:
                            base_confidence = 1.0
                        elif 'logs' in data_sources:
                            base_confidence = 0.6
                        elif 'metrics' in data_sources:
                            base_confidence = 0.4
                        else:
                            base_confidence = 0.3

                        # 调用次数越多，置信度越高（对数缩放）
                        if call_count > 0:
                            call_boost = min(0.2, (call_count ** 0.1) * 0.05)
                            confidence = min(1.0, base_confidence + call_boost)
                        else:
                            confidence = base_confidence

                        session.run("""
                            MATCH (s:Service {id: $source})
                            MATCH (t:Service {id: $target})
                            MERGE (s)-[r:CALLS]->(t)
                            SET r.type = $type,
                                r.call_count = $call_count,
                                r.confidence = $confidence,
                                r.avg_duration = $avg_duration,
                                r.error_rate = $error_rate,
                                r.data_sources = $data_sources,
                                r.last_updated = timestamp()
                        """,
                        source=edge['source'],
                        target=edge['target'],
                        type=edge.get('type', 'calls'),
                        call_count=call_count,
                        confidence=round(confidence, 3),
                        avg_duration=metrics.get('avg_duration', 0),
                        error_rate=metrics.get('error_rate', 0),
                        data_sources=','.join(data_sources) if data_sources else 'unknown'
                        )

                    logger.debug(
                        "Graph saved to Neo4j: %s nodes, %s edges",
                        len(graph["nodes"]),
                        len(graph["edges"]),
                    )
            else:
                # 使用模拟实现
                logger.debug(
                    "Saving graph (mock): %s nodes, %s edges",
                    len(graph["nodes"]),
                    len(graph["edges"]),
                )
                self.graphs.append(graph)

            return True
        except Exception as e:
            _log_event(
                logging.ERROR,
                "Error saving graph",
                event_id="NEO4J_GRAPH_SAVE_ERROR",
                action="neo4j.graph.save",
                outcome="failed",
                error_type=type(e).__name__,
                error=str(e),
            )
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

            # 构建 PREWHERE 条件 - 使用参数化查询
            prewhere_conditions = []
            params = {}
            
            # 转换 ISO 8601 时间戳为 ClickHouse 格式
            def convert_timestamp(ts):
                if not ts:
                    return ts
                try:
                    from datetime import datetime
                    dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
                    return dt.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
                except (TypeError, ValueError) as exc:
                    _log_event(
                        logging.WARNING,
                        "Failed to parse ISO timestamp, fallback to raw value",
                        event_id="CH_TIMESTAMP_PARSE_FALLBACK",
                        raw_timestamp=str(ts),
                        error_type=type(exc).__name__,
                        error=str(exc),
                    )
                    return ts
            
            if start_time:
                ch_start_time = convert_timestamp(start_time)
                prewhere_conditions.append("timestamp >= toDateTime64({start_time:String}, 9)")
                params["start_time"] = ch_start_time
            if end_time:
                ch_end_time = convert_timestamp(end_time)
                prewhere_conditions.append("timestamp <= toDateTime64({end_time:String}, 9)")
                params["end_time"] = ch_end_time

            if not start_time and not end_time:
                prewhere_conditions.append(f"timestamp > now() - INTERVAL {_STATS_DEFAULT_WINDOW}")
            prewhere_clause = ""
            if prewhere_conditions:
                prewhere_clause = "PREWHERE " + " AND ".join(prewhere_conditions)

            # 使用 ClickHouse Client 查询 logs.logs 表
            query = f"""
            SELECT id, toString(timestamp) as timestamp_str, service_name, pod_name, namespace, node_name,
                   level, severity_number, message, trace_id, span_id, flags, labels, host_ip,
                   pod_id, container_name, container_id, container_image
            FROM logs.logs
            {prewhere_clause}
            ORDER BY timestamp DESC
            LIMIT {{limit:Int32}}
            """
            params["limit"] = _sanitize_limit(limit, default_value=100, max_value=10000)
            result = self.ch_client.execute(query, params)

            events = []
            for row in result:
                if len(row) >= 10:
                    labels = _parse_json_object_payload(
                        row[12] if len(row) > 12 else None,
                        field_name="labels",
                        event_id="CH_EVENT_LABELS_PARSE_FAILED",
                        event_log_id=row[0],
                    )

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

            logger.debug("Retrieved %s events from ClickHouse logs table", len(events))
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

                    logger.debug("Retrieved graph from Neo4j: %s nodes, %s edges", len(nodes), len(edges))
                    return [{'nodes': nodes, 'edges': edges}]
            else:
                # 使用模拟实现
                logger.debug("Retrieving graphs (mock)")
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

            logger.debug("Saved %s metrics to ClickHouse", len(metrics_data))
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
                status = _normalize_trace_status(span.get('status_code', 'STATUS_CODE_UNSET'))
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
                import json
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

            # 批量插入 - 使用 logs.traces 表结构
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

            logger.debug("Saved %s traces to ClickHouse", len(traces_data))
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
            if not self.ch_client and not self.ch_http_client:
                logger.warning("ClickHouse client not available")
                return []

            # 构建 PREWHERE 条件
            prewhere_conditions: List[str] = [f"timestamp > now() - INTERVAL {_STATS_DEFAULT_WINDOW}"]
            params: Dict[str, Any] = {"limit": _sanitize_limit(limit, default_value=100, max_value=10000)}
            if service_name:
                prewhere_conditions.append("service_name = {service_name:String}")
                params["service_name"] = service_name
            if metric_name:
                prewhere_conditions.append("metric_name = {metric_name:String}")
                params["metric_name"] = metric_name

            prewhere_clause = "PREWHERE " + " AND ".join(prewhere_conditions)

            # 使用 ClickHouse Client 查询 metrics 表
            query = f"""
            SELECT metric_name, toString(timestamp) as timestamp_str, value_float64, attributes_json, service_name
            FROM logs.metrics
            {prewhere_clause}
            ORDER BY timestamp DESC
            LIMIT {{limit:Int32}}
            """
            result = self.execute_query(query, params)

            metrics = []
            for row in result:
                labels = _parse_json_object_payload(
                    row.get("attributes_json"),
                    field_name="attributes_json",
                    event_id="CH_METRIC_ATTRS_PARSE_FAILED",
                    metric_name=row.get("metric_name", ""),
                )

                metrics.append({
                    'metric_name': row.get("metric_name", ""),
                    'timestamp': row.get("timestamp_str", ""),
                    'value': float(row.get("value_float64", 0.0) or 0.0),
                    'labels': labels,
                    'service_name': row.get("service_name", "unknown"),
                })

            logger.debug("Retrieved %s metrics from ClickHouse", len(metrics))
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
            if not self.ch_client and not self.ch_http_client:
                logger.warning("ClickHouse client not available")
                return []

            prewhere_conditions = [f"timestamp > now() - INTERVAL {_STATS_DEFAULT_WINDOW}"]
            params: Dict[str, Any] = {"limit": _sanitize_limit(limit, default_value=100, max_value=10000)}
            if service_name:
                prewhere_conditions.append("service_name = {service_name:String}")
                params["service_name"] = service_name
            if trace_id:
                prewhere_conditions.append("trace_id = {trace_id:String}")
                params["trace_id"] = trace_id

            prewhere_clause = "PREWHERE " + " AND ".join(prewhere_conditions)
            query = f"""
            SELECT trace_id, span_id, parent_span_id, service_name, operation_name,
                   toString(timestamp) as timestamp_str, status, attributes_json
            FROM logs.traces
            {prewhere_clause}
            ORDER BY timestamp DESC
            LIMIT {{limit:Int32}}
            """
            result = self.execute_query(query, params)

            traces = []
            for row in result:
                tags = _parse_json_object_payload(
                    row.get("attributes_json"),
                    field_name="attributes_json",
                    event_id="CH_TRACE_TAGS_PARSE_FAILED",
                    trace_id=row.get("trace_id", ""),
                    span_id=row.get("span_id", ""),
                )

                traces.append({
                    'trace_id': row.get("trace_id", ""),
                    'span_id': row.get("span_id", ""),
                    'parent_span_id': row.get("parent_span_id", ""),
                    'service_name': row.get("service_name", "unknown"),
                    'operation_name': row.get("operation_name", ""),
                    'start_time': row.get("timestamp_str", ""),
                    'duration_ms': 0,  # 兼容当前 traces 表结构（无 end_time）
                    'status': row.get("status", "STATUS_CODE_UNSET"),
                    'tags': tags
                })

            logger.debug("Retrieved %s traces from ClickHouse", len(traces))
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
            if not self.ch_client and not self.ch_http_client:
                logger.warning("ClickHouse client not available")
                return []

            query = """
            SELECT trace_id, span_id, parent_span_id, service_name, operation_name,
                   toString(timestamp) as timestamp_str, status, attributes_json
            FROM logs.traces
            PREWHERE trace_id = {trace_id:String}
            ORDER BY timestamp ASC
            """
            result = self.execute_query(query, {"trace_id": trace_id})

            spans = []
            for row in result:
                tags = _parse_json_object_payload(
                    row.get("attributes_json"),
                    field_name="attributes_json",
                    event_id="CH_SPAN_TAGS_PARSE_FAILED",
                    trace_id=row.get("trace_id", ""),
                    span_id=row.get("span_id", ""),
                )

                spans.append({
                    'trace_id': row.get("trace_id", ""),
                    'span_id': row.get("span_id", ""),
                    'parent_span_id': row.get("parent_span_id", ""),
                    'service_name': row.get("service_name", "unknown"),
                    'operation_name': row.get("operation_name", ""),
                    'start_time': row.get("timestamp_str", ""),
                    'duration_ms': 0,  # 兼容当前 traces 表结构（无 end_time）
                    'status': row.get("status", "STATUS_CODE_UNSET"),
                    'tags': tags
                })

            logger.debug("Retrieved %s spans for trace %s", len(spans), trace_id)
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
            if not self.ch_client and not self.ch_http_client:
                logger.warning("ClickHouse client not available")
                return {"before": [], "after": [], "current": None}

            # 转换时间戳 - 将 ISO 8601 格式转换为 ClickHouse DateTime64 格式
            from datetime import datetime
            try:
                # 解析 ISO 8601 格式 (2026-02-25T15:26:23.354442Z)
                dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                ch_timestamp = dt.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]  # 转换为 ClickHouse 格式
            except (TypeError, ValueError) as exc:
                _log_event(
                    logging.WARNING,
                    "Failed to parse log context timestamp, fallback to raw value",
                    event_id="CH_LOG_CONTEXT_TIMESTAMP_FALLBACK",
                    raw_timestamp=str(timestamp),
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
                ch_timestamp = timestamp

            from utils.timestamp import datetime64_to_rfc3339

            # 查询之前的日志
            before_query = """
            SELECT id, toString(timestamp) as timestamp_str, service_name, level, message, labels, pod_name, namespace
            FROM logs.logs
            PREWHERE pod_name = {pod_name:String}
                 AND timestamp < toDateTime64({timestamp:String}, 9)
            ORDER BY timestamp DESC
            LIMIT {before_count:Int32}
            """
            before_result = self.execute_query(
                before_query,
                {
                    "pod_name": pod_name,
                    "timestamp": ch_timestamp,
                    "before_count": max(1, int(before_count)),
                },
            )
            before_logs = []
            for row in before_result:
                labels = _parse_json_object_payload(
                    row.get("labels"),
                    field_name="labels",
                    event_id="CH_LOG_CONTEXT_LABELS_PARSE_FAILED",
                    log_position="before",
                    log_id=row.get("id", ""),
                )
                before_logs.append({
                    'id': row.get("id", ""),
                    'timestamp': datetime64_to_rfc3339(str(row.get("timestamp_str", ""))),
                    'service_name': row.get("service_name", ""),
                    'level': row.get("level", ""),
                    'message': row.get("message", ""),
                    'labels': labels,
                    'pod_name': row.get("pod_name", pod_name),
                    'namespace': row.get("namespace", ""),
                    'position': 'before',
                    'distance': 0,
                })

            # 查询之后的日志
            after_query = """
            SELECT id, toString(timestamp) as timestamp_str, service_name, level, message, labels, pod_name, namespace
            FROM logs.logs
            PREWHERE pod_name = {pod_name:String}
                 AND timestamp > toDateTime64({timestamp:String}, 9)
            ORDER BY timestamp ASC
            LIMIT {after_count:Int32}
            """
            after_result = self.execute_query(
                after_query,
                {
                    "pod_name": pod_name,
                    "timestamp": ch_timestamp,
                    "after_count": max(1, int(after_count)),
                },
            )
            after_logs = []
            for row in after_result:
                labels = _parse_json_object_payload(
                    row.get("labels"),
                    field_name="labels",
                    event_id="CH_LOG_CONTEXT_LABELS_PARSE_FAILED",
                    log_position="after",
                    log_id=row.get("id", ""),
                )
                after_logs.append({
                    'id': row.get("id", ""),
                    'timestamp': datetime64_to_rfc3339(str(row.get("timestamp_str", ""))),
                    'service_name': row.get("service_name", ""),
                    'level': row.get("level", ""),
                    'message': row.get("message", ""),
                    'labels': labels,
                    'pod_name': row.get("pod_name", pod_name),
                    'namespace': row.get("namespace", ""),
                    'position': 'after',
                    'distance': 0,
                })

            # 查询当前日志
            current_query = """
            SELECT id, toString(timestamp) as timestamp_str, service_name, pod_name, namespace, level, message, labels
            FROM logs.logs
            PREWHERE pod_name = {pod_name:String}
                 AND timestamp = toDateTime64({timestamp:String}, 9)
            LIMIT 1
            """
            current_result = self.execute_query(
                current_query,
                {
                    "pod_name": pod_name,
                    "timestamp": ch_timestamp,
                },
            )
            current_log = None
            if len(current_result) > 0:
                row = current_result[0]
                labels = _parse_json_object_payload(
                    row.get("labels"),
                    field_name="labels",
                    event_id="CH_LOG_CONTEXT_LABELS_PARSE_FAILED",
                    log_position="current",
                    log_id=row.get("id", ""),
                )
                current_log = {
                    'id': row.get("id", ""),
                    'timestamp': datetime64_to_rfc3339(str(row.get("timestamp_str", ""))),
                    'service_name': row.get("service_name", ""),
                    'pod_name': row.get("pod_name", pod_name),
                    'namespace': row.get("namespace", ""),
                    'level': row.get("level", ""),
                    'message': row.get("message", ""),
                    'labels': labels,
                }

            logger.debug("Retrieved log context: %s before, %s after", len(before_logs), len(after_logs))

            return {
                "before": before_logs,
                "after": after_logs,
                "current": current_log
            }

        except Exception as e:
            _log_event(
                logging.ERROR,
                "Error getting log context",
                event_id="CH_LOG_CONTEXT_ERROR",
                action="clickhouse.query",
                outcome="failed",
                error_type=type(e).__name__,
                error=str(e),
            )
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

            # 平均持续时间 (logs.traces表没有duration_ms列，返回0)
            avg_duration = 0

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
                        # parent_span_id 无法直接反查父服务名，避免构造 unknown 假边污染拓扑。
                        continue

            # 计算每个节点的错误率和平均延迟
            for node in nodes.values():
                metrics = node["metrics"]
                if metrics["request_count"] > 0:
                    metrics["error_rate"] = metrics["error_count"] / metrics["request_count"]
                else:
                    metrics["error_rate"] = 0

            # 转换为列表
            nodes_list = list(nodes.values())

            logger.debug("Retrieved topology: %s nodes, %s edges", len(nodes_list), len(edges))
            return {
                "nodes": nodes_list,
                "edges": edges
            }

        except Exception as e:
            _log_event(
                logging.ERROR,
                "Error getting topology",
                event_id="CH_TOPOLOGY_QUERY_ERROR",
                action="clickhouse.query",
                outcome="failed",
                error_type=type(e).__name__,
                error=str(e),
            )
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

            logger.debug("Retrieved topology from logs: %s nodes, %s edges", len(nodes), len(edges))
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
            _log_event(
                logging.ERROR,
                "Error getting topology from logs",
                event_id="CH_TOPOLOGY_LOGS_QUERY_ERROR",
                action="clickhouse.query",
                outcome="failed",
                error_type=type(e).__name__,
                error=str(e),
            )
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
        params: Dict[str, Any] = None
    ) -> List[Dict[str, Any]]:
        """
        执行 ClickHouse 查询并返回结果

        Args:
            query: SQL 查询语句（支持参数化）
            params: 查询参数字典

        Returns:
            List[Dict[str, Any]]: 查询结果字典列表
        """
        template_summary = _compact_sql(query)
        should_log_info = _should_log_query_info(template_summary)
        if should_log_info:
            _log_event(
                logging.DEBUG,
                "Executing query template",
                event_id="CH_QUERY_TEMPLATE",
                action="clickhouse.query",
                sql=_clip_sql(template_summary),
            )

        def _to_clickhouse_literal(value: Any) -> str:
            """将 Python 值转换为 ClickHouse 字面量字符串。"""
            if value is None:
                return "NULL"
            if isinstance(value, bool):
                return "1" if value else "0"
            if isinstance(value, (int, float)):
                return str(value)
            if isinstance(value, datetime):
                escaped_time = value.strftime("%Y-%m-%d %H:%M:%S.%f")
                return f"'{escaped_time}'"

            escaped = str(value)
            escaped = escaped.replace("\\", "\\\\")
            escaped = escaped.replace("'", "\\'")
            escaped = escaped.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
            return f"'{escaped}'"

        # 统一渲染参数化 SQL（支持 {name:type} 和 {name} 占位符）
        final_query = query
        if params:
            for key, value in params.items():
                literal = _to_clickhouse_literal(value)
                # 支持 {key:type} 格式
                typed_pattern = r'\{' + re.escape(key) + r':[^}]+\}'
                final_query = re.sub(typed_pattern, lambda _: literal, final_query)

                # 支持 {key} 格式
                plain_pattern = r'\{' + re.escape(key) + r'\}'
                final_query = re.sub(plain_pattern, lambda _: literal, final_query)

        if should_log_info:
            logger.debug("Rendered query: %s", _clip_sql(final_query))

        query_start = time.perf_counter()
        last_query_error: Any = None
        last_query_error_source = ""
        
        # 优先使用原生驱动
        if self.ch_client:
            try:
                # 使用 with_column_types=True 获取列名信息
                result = self.ch_client.execute(
                    final_query, 
                    with_column_types=True
                )
                if not result or len(result) < 2:
                    elapsed_ms = (time.perf_counter() - query_start) * 1000.0
                    if elapsed_ms >= SLOW_QUERY_THRESHOLD_MS:
                        _log_event(
                            logging.WARNING,
                            "Slow query detected",
                            event_id="CH_QUERY_SLOW",
                            action="clickhouse.query",
                            outcome="slow",
                            source="native",
                            duration_ms=round(elapsed_ms, 2),
                            rows=0,
                            sql=_clip_sql(template_summary),
                        )
                    return []

                rows, columns = result
                column_names = [col[0] for col in columns]

                # 转换为字典列表
                dict_results = []
                for row in rows:
                    dict_results.append(dict(zip(column_names, row)))

                elapsed_ms = (time.perf_counter() - query_start) * 1000.0
                if elapsed_ms >= SLOW_QUERY_THRESHOLD_MS:
                    _log_event(
                        logging.WARNING,
                        "Slow query detected",
                        event_id="CH_QUERY_SLOW",
                        action="clickhouse.query",
                        outcome="slow",
                        source="native",
                        duration_ms=round(elapsed_ms, 2),
                        rows=len(dict_results),
                        sql=_clip_sql(template_summary),
                    )
                elif should_log_info:
                    _log_event(
                        logging.DEBUG,
                        "Query executed",
                        event_id="CH_QUERY_EXECUTED",
                        action="clickhouse.query",
                        outcome="success",
                        source="native",
                        duration_ms=round(elapsed_ms, 2),
                        rows=len(dict_results),
                    )
                return dict_results
            except Exception as e:
                last_query_error = e
                last_query_error_source = "native"
                elapsed_ms = (time.perf_counter() - query_start) * 1000.0
                _log_event(
                    logging.ERROR,
                    "Error executing query with native driver",
                    event_id="CH_QUERY_NATIVE_ERROR",
                    action="clickhouse.query",
                    outcome="failed",
                    source="native",
                    duration_ms=round(elapsed_ms, 2),
                    error_type=type(e).__name__,
                    error=str(e),
                )
                # 继续尝试 HTTP 客户端
        
        # 使用 HTTP 客户端作为备用
        if self.ch_http_client:
            try:
                # 执行 HTTP 查询
                result = self._execute_clickhouse_http(final_query)

                if isinstance(result, dict) and 'data' in result:
                    rows = result["data"]
                    elapsed_ms = (time.perf_counter() - query_start) * 1000.0
                    if elapsed_ms >= SLOW_QUERY_THRESHOLD_MS:
                        _log_event(
                            logging.WARNING,
                            "Slow query detected",
                            event_id="CH_QUERY_SLOW",
                            action="clickhouse.query",
                            outcome="slow",
                            source="http",
                            duration_ms=round(elapsed_ms, 2),
                            rows=len(rows),
                            sql=_clip_sql(template_summary),
                        )
                    elif should_log_info:
                        _log_event(
                            logging.DEBUG,
                            "Query executed",
                            event_id="CH_QUERY_EXECUTED",
                            action="clickhouse.query",
                            outcome="success",
                            source="http",
                            duration_ms=round(elapsed_ms, 2),
                            rows=len(rows),
                        )
                    return result['data']
                elif isinstance(result, list):
                    elapsed_ms = (time.perf_counter() - query_start) * 1000.0
                    if elapsed_ms >= SLOW_QUERY_THRESHOLD_MS:
                        _log_event(
                            logging.WARNING,
                            "Slow query detected",
                            event_id="CH_QUERY_SLOW",
                            action="clickhouse.query",
                            outcome="slow",
                            source="http",
                            duration_ms=round(elapsed_ms, 2),
                            rows=len(result),
                            sql=_clip_sql(template_summary),
                        )
                    elif should_log_info:
                        _log_event(
                            logging.DEBUG,
                            "Query executed",
                            event_id="CH_QUERY_EXECUTED",
                            action="clickhouse.query",
                            outcome="success",
                            source="http",
                            duration_ms=round(elapsed_ms, 2),
                            rows=len(result),
                        )
                    return result
                elapsed_ms = (time.perf_counter() - query_start) * 1000.0
                if elapsed_ms >= SLOW_QUERY_THRESHOLD_MS:
                    _log_event(
                        logging.WARNING,
                        "Slow query detected",
                        event_id="CH_QUERY_SLOW",
                        action="clickhouse.query",
                        outcome="slow",
                        source="http",
                        duration_ms=round(elapsed_ms, 2),
                        rows=0,
                        sql=_clip_sql(template_summary),
                    )
                return []
            except Exception as e:
                last_query_error = e
                last_query_error_source = "http"
                elapsed_ms = (time.perf_counter() - query_start) * 1000.0
                _log_event(
                    logging.ERROR,
                    "Error executing query with HTTP client",
                    event_id="CH_QUERY_HTTP_ERROR",
                    action="clickhouse.query",
                    outcome="failed",
                    source="http",
                    duration_ms=round(elapsed_ms, 2),
                    error_type=type(e).__name__,
                    error=str(e),
                )
                return []

        if last_query_error is not None:
            _log_event(
                logging.WARNING,
                "ClickHouse query failed and no fallback client available",
                event_id="CH_QUERY_NO_FALLBACK",
                action="clickhouse.query",
                outcome="failed",
                source=last_query_error_source or "unknown",
                error_type=type(last_query_error).__name__,
                error=str(last_query_error),
                sql=_clip_sql(template_summary),
            )
            return []

        logger.warning("ClickHouse client not available")
        return []

    def get_edge_red_metrics(
        self,
        time_window: str = "1 HOUR",
        namespace: str = None
    ) -> Dict[str, Dict[str, Any]]:
        """
        获取 1m 粒度 edge RED 聚合指标并汇总到边级别。

        返回格式:
        {
            "source->target": {
                "call_count": int,
                "error_count": int,
                "error_rate": float,
                "p95": float,
                "p99": float,
                "timeout_rate": float,
                "retries": float,
                "pending": float,
                "dlq": float
            }
        }
        """
        if not self.ch_client:
            return {}

        try:
            safe_time_window = _sanitize_interval(time_window, default_value="1 HOUR")
            metrics = self._query_edge_red_metrics_from_trace_edges(
                safe_time_window=safe_time_window,
                namespace=namespace,
            )
            if metrics:
                return metrics

            return self._query_edge_red_metrics_from_traces_self_join(
                safe_time_window=safe_time_window,
                namespace=namespace,
            )
        except Exception as e:
            logger.warning("Failed to build edge RED metrics: %s", e)
            return {}

    def execute_neo4j_query(self, query: str, parameters: Dict = None) -> List[Dict]:
        """执行 Neo4j 查询并返回结果。"""
        if not self.neo4j_driver:
            logger.warning("Neo4j driver not available")
            return []

        try:
            with self.neo4j_driver.session() as session:
                result = session.run(query, parameters or {})
                return [record.data() for record in result]
        except Exception as e:
            logger.error("Error executing Neo4j query: %s", e)
            return []

    def close(self):
        """
        关闭数据库连接

        释放所有数据库连接资源
        """
        if self.ch_client:
            try:
                disconnect = getattr(self.ch_client, "disconnect", None)
                if callable(disconnect):
                    disconnect()
                logger.info("ClickHouse client closed")
            except Exception as e:
                logger.warning("Failed to close ClickHouse client cleanly: %s", e)

        if self.neo4j_driver:
            self.neo4j_driver.close()
            logger.info("Neo4j driver closed")

        if self.http_session:
            try:
                self.http_session.close()
                logger.info("ClickHouse HTTP session closed")
            except Exception as e:
                logger.warning("Failed to close HTTP session cleanly: %s", e)

        logger.info("Storage adapter closed")
