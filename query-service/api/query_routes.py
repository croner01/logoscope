"""
Query Service API 路由 - 统一的查询接口

功能:
1. 日志查询 (支持健康检查过滤、扩展字段)
2. 日志聚合 (智能 Pattern 提取)
3. Metrics/Traces 查询
"""
import json
import logging
import os
import sys
import re
import base64
import binascii
import time
import asyncio
from collections import defaultdict, OrderedDict
from typing import Dict, Any, List, Optional, Tuple, Set
from fastapi import APIRouter, HTTPException, Query, Response
from datetime import datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo

_SHARED_LIB_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "shared_src"))
if os.path.isdir(_SHARED_LIB_PATH) and _SHARED_LIB_PATH not in sys.path:
    # 保持本地模块优先，避免共享库覆盖 query-service 自身包路径。
    sys.path.append(_SHARED_LIB_PATH)

from storage.adapter import StorageAdapter
from api import query_inference_service as inference_query_utils
from api import query_logs_service as logs_query_utils
from api import query_params as query_param_utils
from api import query_observability_service as obs_query_utils
from api import trace_lite_inference as trace_lite_utils
from api import value_kpi_service as value_kpi_utils

try:
    from shared_src.utils.logging_config import get_logger
    logger = get_logger(__name__)
except ImportError:
    try:
        from utils.logging_config import get_logger
        logger = get_logger(__name__)
    except ImportError:
        logger = logging.getLogger(__name__)

try:
    from shared_src.monitoring import increment as metric_increment, gauge as metric_gauge
except ImportError:
    metric_increment = None
    metric_gauge = None

HEALTH_CHECK_REGEX_PATTERNS = [
    r"(?i)\bkube-probe\b",
    r'(?i)"(?:GET|HEAD)\s+/health(?:z)?(?:\?[^"\s]*)?\s+HTTP/1\.[01]"',
    r'(?i)"(?:GET|HEAD)\s+/(?:ready|readiness|live|liveness)(?:\?[^"\s]*)?\s+HTTP/1\.[01]"',
    r"(?i)\b(?:readiness|liveness)[\s_-]*probe\b",
]
HEALTH_CHECK_FAST_TOKENS = [
    "kube-probe",
    "/health",
    "/healthz",
    "/ready",
    "/readiness",
    "/live",
    "/liveness",
    "readiness probe",
    "liveness probe",
]

router = APIRouter(prefix="/api/v1", tags=["query"])

# 全局 storage 实例
_STORAGE_ADAPTER: StorageAdapter = None
_TRACE_COLUMNS_CACHE: Optional[set] = None
_INFERENCE_ALERT_SUPPRESSIONS: Set[str] = set()
_VALUE_KPI_ALERT_SUPPRESSIONS: Set[str] = set()
_TRACE_TIME_COLUMN_CANDIDATES: Tuple[str, ...] = ("timestamp", "start_time")
_TRACE_ATTRS_COLUMN_CANDIDATES: Tuple[str, ...] = ("attributes_json", "tags")
_TRACE_DURATION_COLUMN_CANDIDATES: Tuple[str, ...] = ("duration_ms", "duration", "duration_us", "duration_ns")
_VALUE_KPI_CACHE: "OrderedDict[str, Tuple[Dict[str, Any], float]]" = OrderedDict()
_QUERY_TIME_INPUT_DEFAULT_TZ_ENV = "QUERY_TIME_INPUT_DEFAULT_TZ"
_QUERY_TIME_INPUT_DEFAULT_TZ = str(os.getenv(_QUERY_TIME_INPUT_DEFAULT_TZ_ENV, "UTC") or "UTC").strip() or "UTC"


def _resolve_query_input_tz() -> tzinfo:
    """Resolve default timezone used for naive request timestamps."""
    tz_text = _QUERY_TIME_INPUT_DEFAULT_TZ
    if tz_text.upper() == "UTC":
        return timezone.utc
    offset_match = re.fullmatch(r"([+-])(\d{2}):?(\d{2})", tz_text)
    if offset_match:
        sign, hh, mm = offset_match.groups()
        minutes = int(hh) * 60 + int(mm)
        if sign == "-":
            minutes = -minutes
        return timezone(timedelta(minutes=minutes))
    try:
        return ZoneInfo(tz_text)
    except Exception:
        logger.warning("Invalid %s=%s, fallback to UTC", _QUERY_TIME_INPUT_DEFAULT_TZ_ENV, tz_text)
        return timezone.utc


_QUERY_INPUT_TZINFO = _resolve_query_input_tz()


def _get_expected_preagg_tables() -> Tuple[str, ...]:
    """Resolve expected pre-aggregation tables from observability schema version."""
    try:
        tables = tuple(obs_query_utils.get_expected_preagg_tables())
    except Exception:
        tables = ("obs_counts_1m", "obs_traces_1m")
    return tables


_EXPECTED_PREAGG_TABLES: Tuple[str, ...] = _get_expected_preagg_tables()
_PREAGG_RUNTIME_STATUS: Dict[str, Any] = {
    "checked_at": None,
    "storage_connected": False,
    "expected": list(_EXPECTED_PREAGG_TABLES),
    "available": [],
    "missing": list(_EXPECTED_PREAGG_TABLES),
    "ready": False,
    "error": None,
}
_PREAGG_LAST_LOGGED_STATE: Optional[str] = None


async def _run_blocking(func, *args, **kwargs):
    """Execute blocking storage-heavy logic in thread pool to avoid event-loop stalls."""
    if os.environ.get("PYTEST_CURRENT_TEST") is not None:
        return func(*args, **kwargs)
    return await asyncio.to_thread(func, *args, **kwargs)


def _coerce_int_query_param(
    value: Any,
    *,
    default_value: int,
    minimum: int,
    maximum: Optional[int] = None,
) -> int:
    """
    Normalize integer-like query arguments for direct route calls in tests.

    FastAPI route functions can be invoked directly (without request parsing),
    and in that case default values may still be `Query(...)` objects.
    """
    raw_value = getattr(value, "default", value)
    try:
        parsed = int(raw_value)
    except (TypeError, ValueError):
        parsed = int(default_value)

    normalized = max(int(minimum), parsed)
    if maximum is not None:
        normalized = min(normalized, int(maximum))
    return normalized


def _read_int_env(name: str, default_value: int) -> int:
    """读取整数环境变量，异常时回退默认值。"""
    raw = os.getenv(name, str(default_value))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default_value
    return max(value, 1)


VALUE_KPI_CACHE_TTL_SECONDS = _read_int_env("VALUE_KPI_CACHE_TTL_SECONDS", 45)
VALUE_KPI_CACHE_MAX_ENTRIES = _read_int_env("VALUE_KPI_CACHE_MAX_ENTRIES", 128)
VALUE_KPI_CACHE_METRICS_LOG_EVERY = _read_int_env("VALUE_KPI_CACHE_METRICS_LOG_EVERY", 200)

_VALUE_KPI_CACHE_METRICS: Dict[str, int] = {
    "requests": 0,
    "hits": 0,
    "misses": 0,
    "writes": 0,
    "evictions_expired": 0,
    "evictions_capacity": 0,
    "manual_clears": 0,
    "storage_resets": 0,
    "last_log_request_count": 0,
}


def _emit_cache_counter(metric_name: str, value: int, tags: Dict[str, str]) -> None:
    """向统一监控模块上报计数器（可选能力）。"""
    if not metric_increment:
        return
    try:
        metric_increment(metric_name, value=value, tags=tags)
    except Exception as exc:
        logger.debug("cache metric increment failed: %s", exc)


def _emit_cache_gauge(metric_name: str, value: float, tags: Dict[str, str]) -> None:
    """向统一监控模块上报 gauge（可选能力）。"""
    if not metric_gauge:
        return
    try:
        metric_gauge(metric_name, value=value, tags=tags)
    except Exception as exc:
        logger.debug("cache metric gauge failed: %s", exc)


def _update_value_kpi_cache_gauges() -> None:
    """更新 value KPI 缓存大小与命中率 gauge。"""
    snapshot = _build_value_kpi_cache_metrics_snapshot()
    tags = {"service": "query-service", "cache": "value_kpi"}
    _emit_cache_gauge("cache.size", float(len(_VALUE_KPI_CACHE)), tags=tags)
    _emit_cache_gauge("cache.hit_rate", float(snapshot["hit_rate"]), tags=tags)


def _build_value_kpi_cache_metrics_snapshot() -> Dict[str, Any]:
    """生成 value KPI 缓存指标快照。"""
    requests = int(_VALUE_KPI_CACHE_METRICS.get("requests", 0))
    hits = int(_VALUE_KPI_CACHE_METRICS.get("hits", 0))
    misses = int(_VALUE_KPI_CACHE_METRICS.get("misses", 0))
    hit_rate = round((hits / requests), 4) if requests > 0 else 0.0
    return {
        "requests": requests,
        "hits": hits,
        "misses": misses,
        "hit_rate": hit_rate,
        "writes": int(_VALUE_KPI_CACHE_METRICS.get("writes", 0)),
        "evictions_expired": int(_VALUE_KPI_CACHE_METRICS.get("evictions_expired", 0)),
        "evictions_capacity": int(_VALUE_KPI_CACHE_METRICS.get("evictions_capacity", 0)),
        "manual_clears": int(_VALUE_KPI_CACHE_METRICS.get("manual_clears", 0)),
        "storage_resets": int(_VALUE_KPI_CACHE_METRICS.get("storage_resets", 0)),
    }


def _maybe_log_value_kpi_cache_metrics() -> None:
    """按请求数节流打印缓存摘要，便于日志基线观测。"""
    requests = int(_VALUE_KPI_CACHE_METRICS.get("requests", 0))
    if requests <= 0:
        return
    if requests % VALUE_KPI_CACHE_METRICS_LOG_EVERY != 0:
        return
    if _VALUE_KPI_CACHE_METRICS.get("last_log_request_count") == requests:
        return
    _VALUE_KPI_CACHE_METRICS["last_log_request_count"] = requests
    summary = _build_value_kpi_cache_metrics_snapshot()
    logger.info(
        "value_kpi_cache metrics: requests=%s hits=%s misses=%s hit_rate=%.4f writes=%s evictions_expired=%s evictions_capacity=%s size=%s",
        summary["requests"],
        summary["hits"],
        summary["misses"],
        summary["hit_rate"],
        summary["writes"],
        summary["evictions_expired"],
        summary["evictions_capacity"],
        len(_VALUE_KPI_CACHE),
    )


def _record_value_kpi_cache_request(hit: bool) -> None:
    """记录缓存请求命中/未命中。"""
    _VALUE_KPI_CACHE_METRICS["requests"] += 1
    result = "hit" if hit else "miss"
    if hit:
        _VALUE_KPI_CACHE_METRICS["hits"] += 1
    else:
        _VALUE_KPI_CACHE_METRICS["misses"] += 1
    _emit_cache_counter(
        "cache.requests_total",
        value=1,
        tags={"service": "query-service", "cache": "value_kpi", "result": result},
    )
    _update_value_kpi_cache_gauges()
    _maybe_log_value_kpi_cache_metrics()


def _record_value_kpi_cache_write() -> None:
    """记录缓存写入。"""
    _VALUE_KPI_CACHE_METRICS["writes"] += 1
    _emit_cache_counter(
        "cache.writes_total",
        value=1,
        tags={"service": "query-service", "cache": "value_kpi"},
    )


def _record_value_kpi_cache_eviction(reason: str, count: int) -> None:
    """记录缓存淘汰事件。"""
    if count <= 0:
        return
    metric_key = "evictions_expired" if reason == "expired" else "evictions_capacity"
    _VALUE_KPI_CACHE_METRICS[metric_key] += int(count)
    _emit_cache_counter(
        "cache.evictions_total",
        value=int(count),
        tags={"service": "query-service", "cache": "value_kpi", "reason": reason},
    )
    _update_value_kpi_cache_gauges()


def _record_value_kpi_cache_clear(reason: str, cleared: int) -> None:
    """记录缓存清理事件。"""
    if reason == "manual":
        _VALUE_KPI_CACHE_METRICS["manual_clears"] += 1
    if reason == "storage_reset":
        _VALUE_KPI_CACHE_METRICS["storage_resets"] += 1
    _emit_cache_counter(
        "cache.clears_total",
        value=1,
        tags={"service": "query-service", "cache": "value_kpi", "reason": reason},
    )
    logger.info("value_kpi_cache cleared: reason=%s cleared=%s", reason, cleared)
    _update_value_kpi_cache_gauges()


def _reset_value_kpi_cache_metrics() -> None:
    """重置缓存指标（仅用于测试隔离）。"""
    for key in _VALUE_KPI_CACHE_METRICS.keys():
        _VALUE_KPI_CACHE_METRICS[key] = 0


def _evict_expired_value_kpi_cache(now_ts: Optional[float] = None) -> None:
    """清理过期 KPI 缓存，避免无界增长。"""
    current_ts = now_ts if now_ts is not None else time.time()
    expired_keys = [key for key, (_, expiry) in _VALUE_KPI_CACHE.items() if expiry <= current_ts]
    for key in expired_keys:
        _VALUE_KPI_CACHE.pop(key, None)
    _record_value_kpi_cache_eviction("expired", len(expired_keys))


def _build_value_kpi_cache_key(
    *,
    time_window: str,
    start_time: Optional[str],
    end_time: Optional[str],
) -> str:
    """构建 value KPI 缓存键。"""
    safe_window = _sanitize_interval(time_window, default_value="7 DAY")
    return "|".join(
        [
            f"window={safe_window}",
            f"start={str(start_time or '').strip()}",
            f"end={str(end_time or '').strip()}",
        ]
    )


def _get_cached_value_kpis(cache_key: str, now_ts: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """获取 value KPI 缓存，命中后刷新 LRU 顺序。"""
    current_ts = now_ts if now_ts is not None else time.time()
    _evict_expired_value_kpi_cache(current_ts)

    entry = _VALUE_KPI_CACHE.get(cache_key)
    if not entry:
        _record_value_kpi_cache_request(hit=False)
        return None
    value, expiry_ts = entry
    if expiry_ts <= current_ts:
        _VALUE_KPI_CACHE.pop(cache_key, None)
        _record_value_kpi_cache_eviction("expired", 1)
        _record_value_kpi_cache_request(hit=False)
        return None
    _VALUE_KPI_CACHE.move_to_end(cache_key)
    _record_value_kpi_cache_request(hit=True)
    return value


def _set_cached_value_kpis(
    cache_key: str,
    value: Dict[str, Any],
    ttl_seconds: int = VALUE_KPI_CACHE_TTL_SECONDS,
) -> None:
    """写入 value KPI 缓存并执行容量淘汰。"""
    expiry_ts = time.time() + max(int(ttl_seconds), 1)
    _VALUE_KPI_CACHE[cache_key] = (value, expiry_ts)
    _VALUE_KPI_CACHE.move_to_end(cache_key)
    _record_value_kpi_cache_write()
    _evict_expired_value_kpi_cache()
    capacity_evicted = 0
    while len(_VALUE_KPI_CACHE) > VALUE_KPI_CACHE_MAX_ENTRIES:
        _VALUE_KPI_CACHE.popitem(last=False)
        capacity_evicted += 1
    _record_value_kpi_cache_eviction("capacity", capacity_evicted)
    _update_value_kpi_cache_gauges()


def set_storage_adapter(adapter: StorageAdapter):
    """设置 storage adapter 实例"""
    global _STORAGE_ADAPTER, _TRACE_COLUMNS_CACHE
    _STORAGE_ADAPTER = adapter
    _TRACE_COLUMNS_CACHE = None
    cleared = len(_VALUE_KPI_CACHE)
    _VALUE_KPI_CACHE.clear()
    _record_value_kpi_cache_clear(reason="storage_reset", cleared=cleared)
    try:
        refresh_preagg_runtime_status(force_reload=True)
    except Exception as exc:
        logger.warning("preagg runtime status refresh failed during storage reset: %s", exc)


def _build_preagg_state_key(status: Dict[str, Any]) -> str:
    """构建 preagg 状态键，用于状态变化检测。"""
    storage_connected = bool(status.get("storage_connected"))
    ready = bool(status.get("ready"))
    missing = ",".join(sorted(str(item) for item in (status.get("missing") or [])))
    error = str(status.get("error") or "")
    return f"storage={storage_connected}|ready={ready}|missing={missing}|error={error}"


def _log_preagg_state_change(status: Dict[str, Any], force: bool = False) -> None:
    """仅在 preagg 状态变化时记录日志，避免健康检查刷屏。"""
    global _PREAGG_LAST_LOGGED_STATE

    state_key = _build_preagg_state_key(status)
    if not force and state_key == _PREAGG_LAST_LOGGED_STATE:
        return
    _PREAGG_LAST_LOGGED_STATE = state_key

    missing_tables = list(status.get("missing") or [])
    if not status.get("storage_connected"):
        logger.warning("Pre-aggregation status unavailable: storage adapter not initialized")
        return

    if status.get("error"):
        logger.warning("Failed to refresh pre-aggregation status: %s", status.get("error"))
        return

    if status.get("ready"):
        available = ", ".join(list(status.get("available") or []))
        logger.info("Pre-aggregation tables ready: %s", available)
        return

    logger.warning(
        "Pre-aggregation tables missing (%s), query-service will degrade to base-table scans",
        ", ".join(missing_tables),
    )


def refresh_preagg_runtime_status(force_reload: bool = False) -> Dict[str, Any]:
    """刷新预聚合表运行时状态，避免静默回退到全表统计。"""
    global _PREAGG_RUNTIME_STATUS, _PREAGG_LAST_LOGGED_STATE

    expected = list(_get_expected_preagg_tables())
    checked_at = datetime.now(timezone.utc).isoformat()

    if not _STORAGE_ADAPTER:
        _PREAGG_RUNTIME_STATUS = {
            "checked_at": checked_at,
            "storage_connected": False,
            "expected": expected,
            "available": [],
            "missing": expected,
            "ready": False,
            "error": "storage adapter not initialized",
        }
        _log_preagg_state_change(_PREAGG_RUNTIME_STATUS, force=force_reload)
        return get_preagg_runtime_status()

    try:
        if force_reload and isinstance(getattr(obs_query_utils, "_PREAGG_TABLE_CACHE", None), dict):
            obs_query_utils._PREAGG_TABLE_CACHE["expires_at"] = 0.0
            obs_query_utils._PREAGG_TABLE_CACHE["tables"] = set()

        available_tables = sorted(set(obs_query_utils._load_preagg_tables(_STORAGE_ADAPTER)))
        missing_tables = [table for table in expected if table not in set(available_tables)]
        ready = len(missing_tables) == 0

        _PREAGG_RUNTIME_STATUS = {
            "checked_at": checked_at,
            "storage_connected": True,
            "expected": expected,
            "available": available_tables,
            "missing": missing_tables,
            "ready": ready,
            "error": None,
        }
        _log_preagg_state_change(_PREAGG_RUNTIME_STATUS, force=force_reload)
    except Exception as exc:
        _PREAGG_RUNTIME_STATUS = {
            "checked_at": checked_at,
            "storage_connected": True,
            "expected": expected,
            "available": [],
            "missing": expected,
            "ready": False,
            "error": str(exc),
        }
        _log_preagg_state_change(_PREAGG_RUNTIME_STATUS, force=force_reload)

    return get_preagg_runtime_status()


def get_preagg_runtime_status() -> Dict[str, Any]:
    """返回预聚合表状态快照。"""
    snapshot = dict(_PREAGG_RUNTIME_STATUS)
    snapshot["expected"] = list(_PREAGG_RUNTIME_STATUS.get("expected") or [])
    snapshot["available"] = list(_PREAGG_RUNTIME_STATUS.get("available") or [])
    snapshot["missing"] = list(_PREAGG_RUNTIME_STATUS.get("missing") or [])
    snapshot["ready"] = bool(_PREAGG_RUNTIME_STATUS.get("ready"))
    snapshot["storage_connected"] = bool(_PREAGG_RUNTIME_STATUS.get("storage_connected"))
    return snapshot


def _select_first_allowed_column(
    available_columns: Set[str],
    candidates: Tuple[str, ...],
) -> Optional[str]:
    """从候选列表中返回首个存在于 schema 的安全列名。"""
    for column in candidates:
        if column in available_columns:
            return column
    return None


def _append_health_check_exclusion(conditions: List[str], params: Dict[str, Any]) -> None:
    """追加健康检查日志过滤条件（大小写不敏感匹配，避免 lowerUTF8 整列开销）。"""
    _ = params
    normalized_tokens = [str(token or "").strip().lower() for token in HEALTH_CHECK_FAST_TOKENS if str(token or "").strip()]
    escaped = [token.replace("\\", "\\\\").replace("'", "\\'") for token in normalized_tokens]
    tokens_literal = ", ".join(f"'{token}'" for token in escaped)
    conditions.append(f"multiSearchAnyCaseInsensitiveUTF8(message, [{tokens_literal}]) = 0")


def _encode_logs_cursor(timestamp: Any, row_id: Any) -> str:
    """将分页位置编码为不透明游标。"""
    payload = {
        "timestamp": str(timestamp or ""),
        "id": str(row_id or ""),
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).decode("ascii")
    return encoded.rstrip("=")


def _decode_logs_cursor(cursor: str) -> Tuple[str, str]:
    """解析日志分页游标。"""
    raw = str(cursor or "").strip()
    if not raw:
        raise ValueError("cursor is empty")

    try:
        padded = raw + "=" * (-len(raw) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        payload = json.loads(decoded)
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid cursor format") from exc

    timestamp = str(payload.get("timestamp", "")).strip() if isinstance(payload, dict) else ""
    row_id = str(payload.get("id", "")).strip() if isinstance(payload, dict) else ""
    if not timestamp or not row_id:
        raise ValueError("cursor payload missing timestamp/id")
    return timestamp, row_id


def _normalize_optional_str(value: Any) -> Optional[str]:
    """归一化可选字符串参数。"""
    return query_param_utils.normalize_optional_str(value)


def _normalize_optional_str_list(value: Any) -> List[str]:
    """归一化可选字符串列表。"""
    return query_param_utils.normalize_optional_str_list(value)


def _normalize_level_values(value: Any) -> List[str]:
    """归一化日志级别列表。"""
    return query_param_utils.normalize_level_values(value)


def _expand_level_match_values(levels: List[str]) -> List[str]:
    """展开日志级别匹配值，兼容 WARN/WARNING 存储差异。"""
    return query_param_utils.expand_level_match_values(levels)


def _normalize_int_param(
    value: Any,
    *,
    default: int,
    minimum: Optional[int] = None,
    maximum: Optional[int] = None,
) -> int:
    """归一化整数参数，兼容直接函数调用时传入的 Query 对象。"""
    try:
        if isinstance(value, bool):
            raise ValueError("bool is not treated as int param")
        resolved = int(str(value).strip())
    except Exception:
        resolved = default
    if minimum is not None:
        resolved = max(resolved, minimum)
    if maximum is not None:
        resolved = min(resolved, maximum)
    return resolved


def _normalize_bool_param(value: Any, *, default: bool) -> bool:
    """归一化布尔参数，兼容直接函数调用时传入的 Query 对象。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y", "on"}:
            return True
        if normalized in {"0", "false", "no", "n", "off"}:
            return False
    return default


def _append_exact_match_filter(
    *,
    conditions: List[str],
    params: Dict[str, Any],
    column_name: str,
    param_prefix: str,
    values: List[str],
) -> List[str]:
    """将精确匹配条件拼接为安全参数化 WHERE 子句。"""
    return query_param_utils.append_exact_match_filter(
        conditions=conditions,
        params=params,
        column_name=column_name,
        param_prefix=param_prefix,
        values=values,
    )


def _convert_timestamp(ts: Optional[str]) -> Optional[str]:
    """转换 ISO 8601 时间戳为 ClickHouse 字符串格式。"""
    if not ts:
        return ts

    try:
        if isinstance(ts, datetime):
            dt = ts
        else:
            text = str(ts).strip()
            if not text:
                return text
            if text.replace(".", "", 1).isdigit():
                numeric_value = float(text)
                absolute_value = abs(numeric_value)
                if absolute_value >= 1e17:  # nanoseconds
                    dt = datetime.fromtimestamp(numeric_value / 1_000_000_000, tz=timezone.utc)
                elif absolute_value >= 1e14:  # microseconds
                    dt = datetime.fromtimestamp(numeric_value / 1_000_000, tz=timezone.utc)
                elif absolute_value >= 1e11:  # milliseconds
                    dt = datetime.fromtimestamp(numeric_value / 1_000, tz=timezone.utc)
                else:  # seconds
                    dt = datetime.fromtimestamp(numeric_value, tz=timezone.utc)
            else:
                normalized = text
                if " " in normalized and "T" not in normalized:
                    normalized = normalized.replace(" ", "T")
                if normalized.endswith("Z"):
                    normalized = f"{normalized[:-1]}+00:00"
                if len(normalized) >= 5 and normalized[-5] in {"+", "-"} and normalized[-3] != ":":
                    normalized = f"{normalized[:-2]}:{normalized[-2:]}"
                dt = datetime.fromisoformat(normalized)

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_QUERY_INPUT_TZINFO)
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt.strftime("%Y-%m-%d %H:%M:%S.%f")
    except Exception:
        return ts


def _normalize_trace_status(status: Any) -> str:
    """统一 trace/spans 状态值为 STATUS_CODE_*。"""
    value = str(status or "").strip().upper()
    if value in {"2", "ERROR", "STATUS_CODE_ERROR"}:
        return "STATUS_CODE_ERROR"
    if value in {"1", "OK", "STATUS_CODE_OK"}:
        return "STATUS_CODE_OK"
    return "STATUS_CODE_UNSET"


def _parse_json_dict(raw: Any) -> Dict[str, Any]:
    """将 JSON 字符串安全解析为字典。"""
    return trace_lite_utils.parse_json_dict(raw)


def _extract_duration_ms(row: Dict[str, Any], tags: Dict[str, Any]) -> float:
    """优先使用显式 duration 字段，不存在时尝试从 tags 提取。"""
    raw_duration = row.get("duration_ms")
    if raw_duration is not None:
        try:
            duration_value = max(float(raw_duration), 0.0)
            # 当显式字段为 0（旧数据/无该列回填值）时，继续尝试 tags 回退
            if duration_value > 0:
                return duration_value
        except Exception as exc:
            logger.debug("Failed to parse duration_ms from row: %r (%s)", raw_duration, exc)

    candidate_keys = [
        "duration_ms",
        "span.duration_ms",
        "duration",
        "latency_ms",
        "elapsed_ms",
    ]
    for key in candidate_keys:
        if key in tags:
            try:
                return max(float(tags[key]), 0.0)
            except Exception:
                continue

    us_keys = ["duration_us", "span.duration_us", "latency_us", "elapsed_us"]
    for key in us_keys:
        if key in tags:
            try:
                return max(float(tags[key]) / 1000.0, 0.0)
            except Exception:
                continue

    # 兼容常见纳秒字段
    ns_keys = ["duration_ns", "span.duration_ns", "latency_ns", "elapsed_ns"]
    for key in ns_keys:
        if key in tags:
            try:
                return max(float(tags[key]) / 1_000_000.0, 0.0)
            except Exception:
                continue
    return 0.0


def _resolve_trace_schema() -> Dict[str, Optional[str]]:
    """
    动态识别 traces 表字段，兼容旧/新 schema。

    Returns:
        {
            "time_col": "timestamp" | "start_time" | None,
            "attrs_col": "attributes_json" | "tags" | None,
            "duration_col": "duration_ms" | None,
        }
    """
    global _TRACE_COLUMNS_CACHE

    if _TRACE_COLUMNS_CACHE is None:
        columns_result = _STORAGE_ADAPTER.execute_query(
            """
            SELECT name
            FROM system.columns
            WHERE database = 'logs' AND table = 'traces'
            """
        )
        _TRACE_COLUMNS_CACHE = {row.get("name") for row in columns_result if row.get("name")}

    columns = _TRACE_COLUMNS_CACHE or set()
    return {
        "time_col": _select_first_allowed_column(columns, _TRACE_TIME_COLUMN_CANDIDATES),
        "attrs_col": _select_first_allowed_column(columns, _TRACE_ATTRS_COLUMN_CANDIDATES),
        "duration_col": _select_first_allowed_column(columns, _TRACE_DURATION_COLUMN_CANDIDATES),
    }


def _duration_column_to_ms_expr(duration_col: Optional[str]) -> Optional[str]:
    """将 traces 表中的 duration 列统一转换为毫秒表达式。"""
    if not duration_col:
        return None
    base_expr = f"toFloat64OrZero(toString({duration_col}))"
    lowered = str(duration_col).lower()
    if lowered.endswith("_ns"):
        return f"({base_expr} / 1000000.0)"
    if lowered.endswith("_us"):
        return f"({base_expr} / 1000.0)"
    return base_expr


def _build_grouped_trace_duration_expr(schema: Dict[str, Optional[str]]) -> str:
    """
    构建 trace 级别时长聚合表达式。

    优先顺序：
    1. duration_ms 列
    2. 时间范围差（max-min）
    3. attributes/tags 中 duration_ms / duration_ns 回退值
    """
    time_col = schema.get("time_col")
    attrs_col = schema.get("attrs_col")
    duration_col = schema.get("duration_col")

    base_expr = (
        f"toFloat64(greatest(dateDiff('millisecond', min({time_col}), max({time_col})), 0))"
        if time_col
        else "0.0"
    )
    attrs_expr = (
        f"greatest("
        f"max(toFloat64OrZero(JSONExtractString({attrs_col}, 'duration_ms'))),"
        f"max(toFloat64OrZero(JSONExtractString({attrs_col}, 'span.duration_ms'))),"
        f"max(toFloat64OrZero(JSONExtractString({attrs_col}, 'duration_ns')) / 1000000.0),"
        f"max(toFloat64OrZero(JSONExtractString({attrs_col}, 'span.duration_ns')) / 1000000.0),"
        f"max(JSONExtractFloat({attrs_col}, 'duration_ms')),"
        f"max(JSONExtractFloat({attrs_col}, 'span.duration_ms')),"
        f"max(JSONExtractFloat({attrs_col}, 'duration_ns') / 1000000.0),"
        f"max(JSONExtractFloat({attrs_col}, 'span.duration_ns') / 1000000.0)"
        f")"
    ) if attrs_col else ""

    fallback_expr = base_expr
    if attrs_expr:
        fallback_expr = f"greatest({base_expr}, {attrs_expr})"

    if duration_col:
        duration_ms_expr = _duration_column_to_ms_expr(duration_col) or "0.0"
        # 兼容旧数据：当 duration_ms 列存在但为 0 时，回退到时间差/attrs 推导值。
        return f"greatest(max({duration_ms_expr}), {fallback_expr})"

    return fallback_expr


def _build_grouped_trace_duration_expr_light(schema: Dict[str, Optional[str]]) -> str:
    """
    构建轻量 trace 时长聚合表达式（用于 traces 列表分页）。

    与完整表达式相比，避免读取 attributes_json/tags 大字段，
    以降低 ClickHouse 在高并发分页查询下的内存峰值。
    """
    time_col = schema.get("time_col")
    duration_col = schema.get("duration_col")

    base_expr = (
        f"toFloat64(greatest(dateDiff('millisecond', min({time_col}), max({time_col})), 0))"
        if time_col
        else "0.0"
    )
    if duration_col:
        duration_ms_expr = _duration_column_to_ms_expr(duration_col) or "0.0"
        return f"greatest(max({duration_ms_expr}), {base_expr})"
    return base_expr


def _extract_request_id(attrs: Dict[str, Any], message: str = "") -> str:
    """从 attributes/message 提取 request_id。"""
    return trace_lite_utils.extract_request_id(attrs, message=message)


def _to_datetime(value: Any) -> datetime:
    return trace_lite_utils.to_datetime(value)


def _infer_trace_lite_fragments_from_logs(
    log_rows: List[Dict[str, Any]],
    fallback_window_sec: float = 2.0
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """从 logs 生成 Trace-Lite inferred 调用片段。"""
    return trace_lite_utils.infer_trace_lite_fragments_from_logs(
        log_rows=log_rows,
        fallback_window_sec=fallback_window_sec,
    )


def _sanitize_interval(time_window: str, default_value: str = "7 DAY") -> str:
    """规范化 INTERVAL 参数，避免注入并统一格式。"""
    return query_param_utils.sanitize_interval(time_window, default_value=default_value)


def _normalize_topology_context(
    service_name: Optional[str],
    search: Optional[str],
    start_time: Optional[str],
    end_time: Optional[str],
    source_service: Optional[str],
    target_service: Optional[str],
    time_window: Optional[str],
) -> Dict[str, Optional[str]]:
    """
    统一处理拓扑跳转上下文参数，返回可直接用于 logs 查询的过滤条件。
    优先级：
    1. 显式参数（service_name/search/start_time/end_time）
    2. 拓扑上下文（source_service/target_service/time_window）
    """
    return query_param_utils.normalize_topology_context(
        service_name=service_name,
        search=search,
        start_time=start_time,
        end_time=end_time,
        source_service=source_service,
        target_service=target_service,
        time_window=time_window,
    )


def _build_time_filter_clause(
    column_name: str,
    time_window: str,
    start_time: Optional[str],
    end_time: Optional[str],
    param_prefix: str = "kpi"
) -> Tuple[str, Dict[str, Any]]:
    """
    构造时间过滤表达式。
    - 若提供 start/end，则优先使用绝对时间。
    - 否则回退为 now()-INTERVAL。
    """
    return query_param_utils.build_time_filter_clause(
        column_name=column_name,
        time_window=time_window,
        start_time=start_time,
        end_time=end_time,
        param_prefix=param_prefix,
    )


def _safe_ratio(numerator: float, denominator: float) -> float:
    return query_param_utils.safe_ratio(numerator, denominator)


def _interval_to_timedelta(time_window: str, default_value: str = "7 DAY") -> timedelta:
    """将 INTERVAL 字符串转换为 timedelta。"""
    return query_param_utils.interval_to_timedelta(time_window, default_value=default_value)


def _evaluate_value_kpi_alerts(
    metrics: Dict[str, Any],
    max_mttd_minutes: float,
    max_mttr_minutes: float,
    min_trace_log_correlation_rate: float,
    min_topology_coverage_rate: float,
    min_release_regression_pass_rate: float,
) -> List[Dict[str, Any]]:
    """根据阈值评估 value KPI 告警。"""
    return value_kpi_utils.evaluate_value_kpi_alerts(
        metrics=metrics,
        max_mttd_minutes=max_mttd_minutes,
        max_mttr_minutes=max_mttr_minutes,
        min_trace_log_correlation_rate=min_trace_log_correlation_rate,
        min_topology_coverage_rate=min_topology_coverage_rate,
        min_release_regression_pass_rate=min_release_regression_pass_rate,
        suppressed_metrics=_VALUE_KPI_ALERT_SUPPRESSIONS,
    )


def _ensure_value_kpi_snapshot_table() -> None:
    """确保 value KPI 快照表存在。"""
    value_kpi_utils.ensure_value_kpi_snapshot_table(_STORAGE_ADAPTER)


def _store_value_kpi_snapshot(
    computed: Dict[str, Any],
    time_window: str,
    source: str,
    window_start: datetime,
    window_end: datetime,
) -> Dict[str, Any]:
    """落盘 value KPI 快照到 ClickHouse。"""
    if not _STORAGE_ADAPTER:
        raise HTTPException(status_code=503, detail="Storage adapter not initialized")
    return value_kpi_utils.store_value_kpi_snapshot(
        storage_adapter=_STORAGE_ADAPTER,
        computed=computed,
        time_window=time_window,
        source=source,
        window_start=window_start,
        window_end=window_end,
        sanitize_interval_fn=_sanitize_interval,
    )


def _list_value_kpi_snapshots(limit: int, source: Optional[str] = None) -> List[Dict[str, Any]]:
    """查询 value KPI 快照历史。"""
    return value_kpi_utils.list_value_kpi_snapshots(
        storage_adapter=_STORAGE_ADAPTER,
        limit=limit,
        source=source,
    )


def _normalize_datetime(value: Any) -> datetime:
    return value_kpi_utils.normalize_datetime(value, to_datetime_fn=_to_datetime)


def _estimate_mttd_mttr_minutes(
    time_window: str,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None
) -> Dict[str, Any]:
    """
    粗粒度估算 MTTD / MTTR（基于日志推断）。
    说明：
    - incident_start: 首个 ERROR/FATAL
    - detected_at: 首个 WARN/ERROR/FATAL（通常接近 start）
    - recovered_at: 包含恢复关键词或 ERROR 后稳定 INFO
    """
    return value_kpi_utils.estimate_mttd_mttr_minutes(
        storage_adapter=_STORAGE_ADAPTER,
        time_window=time_window,
        start_time=start_time,
        end_time=end_time,
        build_time_filter_clause_fn=_build_time_filter_clause,
        normalize_datetime_fn=_normalize_datetime,
    )


def _query_release_gate_summary(
    time_window: str,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None
) -> Dict[str, Any]:
    """
    读取 release gate 报告汇总。
    若表不存在或查询失败，返回降级结果。
    """
    return value_kpi_utils.query_release_gate_summary(
        storage_adapter=_STORAGE_ADAPTER,
        time_window=time_window,
        start_time=start_time,
        end_time=end_time,
        build_time_filter_clause_fn=_build_time_filter_clause,
        safe_ratio_fn=_safe_ratio,
        logger=logger,
    )


def _compute_inference_coverage_rate(
    time_window: str,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
) -> float:
    """
    基于 trace-lite 推断链路估算拓扑覆盖率。
    口径：参与 inferred 边的服务数 / 日志中出现的服务数。
    """
    return value_kpi_utils.compute_inference_coverage_rate(
        storage_adapter=_STORAGE_ADAPTER,
        time_window=time_window,
        start_time=start_time,
        end_time=end_time,
        build_time_filter_clause_fn=_build_time_filter_clause,
        infer_trace_lite_fragments_fn=_infer_trace_lite_fragments_from_logs,
        safe_ratio_fn=_safe_ratio,
        logger=logger,
    )


def _compute_value_kpis(
    time_window: str = "7 DAY",
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    use_cache: bool = True,
) -> Dict[str, Any]:
    """计算 M4 价值指标看板核心 KPI。"""
    return value_kpi_utils.compute_value_kpis(
        storage_adapter=_STORAGE_ADAPTER,
        time_window=time_window,
        start_time=start_time,
        end_time=end_time,
        use_cache=use_cache,
        sanitize_interval_fn=_sanitize_interval,
        build_cache_key_fn=_build_value_kpi_cache_key,
        get_cached_fn=_get_cached_value_kpis,
        set_cached_fn=_set_cached_value_kpis,
        build_time_filter_clause_fn=_build_time_filter_clause,
        safe_ratio_fn=_safe_ratio,
        compute_inference_coverage_rate_fn=_compute_inference_coverage_rate,
        estimate_mttd_mttr_minutes_fn=_estimate_mttd_mttr_minutes,
        query_release_gate_summary_fn=_query_release_gate_summary,
    )


@router.get("/logs")
async def query_logs(
    limit: int = Query(100, ge=1, le=10000),
    service_name: Optional[str] = Query(None),
    service_names: Optional[List[str]] = Query(None, description="服务名多选过滤"),
    namespace: Optional[str] = Query(None, description="命名空间过滤"),
    namespaces: Optional[List[str]] = Query(None, description="命名空间多选过滤"),
    trace_id: Optional[str] = Query(None),
    trace_ids: Optional[List[str]] = Query(None, description="Trace ID 多值精确过滤"),
    correlation_mode: Optional[str] = Query("and", description="trace/request 组合模式: and|or"),
    request_id: Optional[str] = Query(None, description="Request ID 精确过滤"),
    request_ids: Optional[List[str]] = Query(None, description="Request ID 多值精确过滤"),
    pod_name: Optional[str] = Query(None),
    container_name: Optional[str] = Query(None, description="容器名称过滤"),
    level: Optional[str] = Query(None),
    levels: Optional[List[str]] = Query(None, description="日志级别多选过滤"),
    start_time: Optional[str] = Query(None),
    end_time: Optional[str] = Query(None),
    exclude_health_check: bool = Query(False, description="过滤健康检查日志"),
    search: Optional[str] = Query(None, description="搜索关键词"),
    source_service: Optional[str] = Query(None, description="拓扑上下文: 源服务"),
    target_service: Optional[str] = Query(None, description="拓扑上下文: 目标服务"),
    source_namespace: Optional[str] = Query(None, description="拓扑上下文: 源命名空间"),
    target_namespace: Optional[str] = Query(None, description="拓扑上下文: 目标命名空间"),
    time_window: Optional[str] = Query(None, description="拓扑上下文: 时间窗口（如 1 HOUR）"),
    cursor: Optional[str] = Query(None, description="分页游标（用于加载下一页）"),
    anchor_time: Optional[str] = Query(None, description="查询锚点时间，分页期间保持稳定"),
) -> Dict[str, Any]:
    """
    查询日志数据
    
    Args:
        limit: 返回数量限制
        service_name: 服务名过滤
        trace_id: Trace ID 过滤
        request_id: Request ID 精确过滤
        pod_name: Pod 名称过滤
        container_name: 容器名称过滤
        level: 日志级别过滤
        start_time: 开始时间
        end_time: 结束时间
        exclude_health_check: 过滤健康检查日志
        search: 搜索关键词
    
    Returns:
        Dict[str, Any]: 查询结果
    """
    if not _STORAGE_ADAPTER:
        raise HTTPException(status_code=503, detail="Storage adapter not initialized")

    try:
        normalized_limit = _normalize_int_param(limit, default=100, minimum=1, maximum=10000)
        normalized_exclude_health_check = _normalize_bool_param(exclude_health_check, default=False)
        normalized_service_name = _normalize_optional_str(service_name)
        normalized_namespace = _normalize_optional_str(namespace)
        normalized_trace_id = _normalize_optional_str(trace_id)
        normalized_correlation_mode = _normalize_optional_str(correlation_mode)
        normalized_request_id = _normalize_optional_str(request_id)
        normalized_pod_name = _normalize_optional_str(pod_name)
        normalized_container_name = _normalize_optional_str(container_name)
        normalized_level = _normalize_optional_str(level)
        normalized_start_time = _normalize_optional_str(start_time)
        normalized_end_time = _normalize_optional_str(end_time)
        normalized_search = _normalize_optional_str(search)
        normalized_source_service = _normalize_optional_str(source_service)
        normalized_target_service = _normalize_optional_str(target_service)
        normalized_source_namespace = _normalize_optional_str(source_namespace)
        normalized_target_namespace = _normalize_optional_str(target_namespace)
        normalized_time_window = _normalize_optional_str(time_window)
        normalized_cursor = _normalize_optional_str(cursor)
        normalized_anchor_time = _normalize_optional_str(anchor_time)
        return await _run_blocking(
            logs_query_utils.query_logs,
            storage_adapter=_STORAGE_ADAPTER,
            limit=normalized_limit,
            service_name=normalized_service_name,
            service_names=service_names,
            namespace=normalized_namespace,
            namespaces=namespaces,
            trace_id=normalized_trace_id,
            correlation_mode=normalized_correlation_mode,
            request_id=normalized_request_id,
            pod_name=normalized_pod_name,
            trace_ids=_normalize_optional_str_list(trace_ids),
            request_ids=_normalize_optional_str_list(request_ids),
            container_name=normalized_container_name,
            level=normalized_level,
            levels=levels,
            start_time=normalized_start_time,
            end_time=normalized_end_time,
            exclude_health_check=normalized_exclude_health_check,
            search=normalized_search,
            source_service=normalized_source_service,
            target_service=normalized_target_service,
            source_namespace=normalized_source_namespace,
            target_namespace=normalized_target_namespace,
            time_window=normalized_time_window,
            cursor=normalized_cursor,
            anchor_time=normalized_anchor_time,
            normalize_optional_str_fn=_normalize_optional_str,
            normalize_topology_context_fn=_normalize_topology_context,
            normalize_optional_str_list_fn=_normalize_optional_str_list,
            normalize_level_values_fn=_normalize_level_values,
            expand_level_match_values_fn=_expand_level_match_values,
            append_exact_match_filter_fn=_append_exact_match_filter,
            append_health_check_exclusion_fn=_append_health_check_exclusion,
            convert_timestamp_fn=_convert_timestamp,
            decode_logs_cursor_fn=_decode_logs_cursor,
            encode_logs_cursor_fn=_encode_logs_cursor,
            logger=logger,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"查询日志数据时出错: {e}", extra={"error": str(e)})
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/logs/facets")
async def query_logs_facets(
    service_name: Optional[str] = Query(None),
    service_names: Optional[List[str]] = Query(None, description="服务名多选过滤"),
    namespace: Optional[str] = Query(None, description="命名空间过滤"),
    namespaces: Optional[List[str]] = Query(None, description="命名空间多选过滤"),
    trace_id: Optional[str] = Query(None),
    trace_ids: Optional[List[str]] = Query(None, description="Trace ID 多值精确过滤"),
    correlation_mode: Optional[str] = Query("and", description="trace/request 组合模式: and|or"),
    request_id: Optional[str] = Query(None, description="Request ID 精确过滤"),
    request_ids: Optional[List[str]] = Query(None, description="Request ID 多值精确过滤"),
    pod_name: Optional[str] = Query(None),
    container_name: Optional[str] = Query(None, description="容器名称过滤"),
    level: Optional[str] = Query(None),
    levels: Optional[List[str]] = Query(None, description="日志级别多选过滤"),
    start_time: Optional[str] = Query(None),
    end_time: Optional[str] = Query(None),
    exclude_health_check: bool = Query(False, description="过滤健康检查日志"),
    search: Optional[str] = Query(None, description="搜索关键词"),
    source_service: Optional[str] = Query(None, description="拓扑上下文: 源服务"),
    target_service: Optional[str] = Query(None, description="拓扑上下文: 目标服务"),
    time_window: Optional[str] = Query(None, description="拓扑上下文: 时间窗口（如 1 HOUR）"),
    anchor_time: Optional[str] = Query(None, description="查询锚点时间，保持 Facet 与日志列表一致"),
    limit_services: int = Query(200, ge=1, le=1000, description="服务 Facet 返回数量"),
    limit_namespaces: int = Query(200, ge=1, le=1000, description="命名空间 Facet 返回数量"),
    limit_levels: int = Query(20, ge=1, le=50, description="级别 Facet 返回数量"),
) -> Dict[str, Any]:
    """返回日志筛选 Facet（服务、级别）统计。"""
    if not _STORAGE_ADAPTER:
        raise HTTPException(status_code=503, detail="Storage adapter not initialized")

    try:
        normalized_limit_services = _normalize_int_param(limit_services, default=200, minimum=1, maximum=1000)
        normalized_limit_namespaces = _normalize_int_param(limit_namespaces, default=200, minimum=1, maximum=1000)
        normalized_limit_levels = _normalize_int_param(limit_levels, default=20, minimum=1, maximum=50)
        normalized_exclude_health_check = _normalize_bool_param(exclude_health_check, default=False)
        normalized_service_name = _normalize_optional_str(service_name)
        normalized_namespace = _normalize_optional_str(namespace)
        normalized_trace_id = _normalize_optional_str(trace_id)
        normalized_correlation_mode = _normalize_optional_str(correlation_mode)
        normalized_request_id = _normalize_optional_str(request_id)
        normalized_pod_name = _normalize_optional_str(pod_name)
        normalized_container_name = _normalize_optional_str(container_name)
        normalized_level = _normalize_optional_str(level)
        normalized_start_time = _normalize_optional_str(start_time)
        normalized_end_time = _normalize_optional_str(end_time)
        normalized_search = _normalize_optional_str(search)
        normalized_source_service = _normalize_optional_str(source_service)
        normalized_target_service = _normalize_optional_str(target_service)
        normalized_time_window = _normalize_optional_str(time_window)
        normalized_anchor_time = _normalize_optional_str(anchor_time)
        return await _run_blocking(
            logs_query_utils.query_logs_facets,
            storage_adapter=_STORAGE_ADAPTER,
            service_name=normalized_service_name,
            service_names=service_names,
            namespace=normalized_namespace,
            namespaces=namespaces,
            trace_id=normalized_trace_id,
            correlation_mode=normalized_correlation_mode,
            request_id=normalized_request_id,
            pod_name=normalized_pod_name,
            container_name=normalized_container_name,
            trace_ids=_normalize_optional_str_list(trace_ids),
            request_ids=_normalize_optional_str_list(request_ids),
            level=normalized_level,
            levels=levels,
            start_time=normalized_start_time,
            end_time=normalized_end_time,
            exclude_health_check=normalized_exclude_health_check,
            search=normalized_search,
            source_service=normalized_source_service,
            target_service=normalized_target_service,
            time_window=normalized_time_window,
            anchor_time=normalized_anchor_time,
            limit_services=normalized_limit_services,
            limit_namespaces=normalized_limit_namespaces,
            limit_levels=normalized_limit_levels,
            normalize_topology_context_fn=_normalize_topology_context,
            normalize_optional_str_list_fn=_normalize_optional_str_list,
            normalize_level_values_fn=_normalize_level_values,
            expand_level_match_values_fn=_expand_level_match_values,
            append_exact_match_filter_fn=_append_exact_match_filter,
            append_health_check_exclusion_fn=_append_health_check_exclusion,
            convert_timestamp_fn=_convert_timestamp,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"查询日志 Facet 时出错: {exc}", extra={"error": str(exc)})
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/logs/preview/topology-edge")
async def query_topology_edge_logs_preview(
    source_service: str = Query(..., min_length=1, description="拓扑链路源服务"),
    target_service: str = Query(..., min_length=1, description="拓扑链路目标服务"),
    namespace: Optional[str] = Query(None, description="命名空间过滤"),
    source_namespace: Optional[str] = Query(None, description="拓扑链路源命名空间"),
    target_namespace: Optional[str] = Query(None, description="拓扑链路目标命名空间"),
    time_window: str = Query("1 HOUR", description="查询窗口"),
    anchor_time: Optional[str] = Query(None, description="查询锚点时间"),
    limit: int = Query(20, ge=1, le=200),
    exclude_health_check: bool = Query(True, description="是否过滤健康检查日志"),
) -> Dict[str, Any]:
    """
    链路问题日志预览（QS-01）。
    按 source/target/time_window 返回最近关联日志，并按关联度评分排序。
    """
    if not _STORAGE_ADAPTER:
        raise HTTPException(status_code=503, detail="Storage adapter not initialized")

    try:
        normalized_limit = _normalize_int_param(limit, default=20, minimum=1, maximum=200)
        normalized_exclude_health_check = _normalize_bool_param(exclude_health_check, default=True)
        normalized_source_service = _normalize_optional_str(source_service)
        normalized_target_service = _normalize_optional_str(target_service)
        normalized_namespace = _normalize_optional_str(namespace)
        normalized_source_namespace = _normalize_optional_str(source_namespace)
        normalized_target_namespace = _normalize_optional_str(target_namespace)
        normalized_time_window = _normalize_optional_str(time_window) or "1 HOUR"
        normalized_anchor_time = _normalize_optional_str(anchor_time)
        return await _run_blocking(
            logs_query_utils.query_topology_edge_logs_preview,
            storage_adapter=_STORAGE_ADAPTER,
            source_service=normalized_source_service,
            target_service=normalized_target_service,
            time_window=normalized_time_window,
            limit=normalized_limit,
            exclude_health_check=normalized_exclude_health_check,
            namespace=normalized_namespace,
            source_namespace=normalized_source_namespace,
            target_namespace=normalized_target_namespace,
            anchor_time=normalized_anchor_time,
            sanitize_interval_fn=_sanitize_interval,
            append_health_check_exclusion_fn=_append_health_check_exclusion,
            convert_timestamp_fn=_convert_timestamp,
            to_datetime_fn=_to_datetime,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"查询拓扑链路日志预览时出错: {exc}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/logs/aggregated")
async def query_logs_aggregated(
    limit: int = Query(500, ge=10, le=5000, description="查询日志数量"),
    min_pattern_count: int = Query(2, ge=1, le=100, description="最小聚合数量"),
    max_patterns: int = Query(50, ge=1, le=200, description="返回最大 pattern 数"),
    max_samples: int = Query(3, ge=1, le=10, description="每个 pattern 保留示例数"),
    service_name: Optional[str] = Query(None, description="服务名过滤"),
    service_names: Optional[List[str]] = Query(None, description="服务名多选过滤"),
    namespace: Optional[str] = Query(None, description="命名空间过滤"),
    namespaces: Optional[List[str]] = Query(None, description="命名空间多选过滤"),
    trace_id: Optional[str] = Query(None, description="Trace ID 过滤"),
    trace_ids: Optional[List[str]] = Query(None, description="Trace ID 多值精确过滤"),
    correlation_mode: Optional[str] = Query("and", description="trace/request 组合模式: and|or"),
    request_id: Optional[str] = Query(None, description="Request ID 精确过滤"),
    request_ids: Optional[List[str]] = Query(None, description="Request ID 多值精确过滤"),
    pod_name: Optional[str] = Query(None, description="Pod 名称过滤"),
    container_name: Optional[str] = Query(None, description="容器名称过滤"),
    level: Optional[str] = Query(None, description="日志级别过滤"),
    levels: Optional[List[str]] = Query(None, description="日志级别多选过滤"),
    start_time: Optional[str] = Query(None, description="开始时间"),
    end_time: Optional[str] = Query(None, description="结束时间"),
    exclude_health_check: bool = Query(True, description="过滤健康检查日志"),
    search: Optional[str] = Query(None, description="搜索关键词"),
    source_service: Optional[str] = Query(None, description="拓扑上下文: 源服务"),
    target_service: Optional[str] = Query(None, description="拓扑上下文: 目标服务"),
    time_window: Optional[str] = Query(None, description="拓扑上下文: 时间窗口（如 1 HOUR）"),
    anchor_time: Optional[str] = Query(None, description="查询锚点时间，保持聚合与日志列表一致"),
) -> Dict[str, Any]:
    """
    智能 Pattern 聚合查询
    
    将相似日志聚合为 Pattern 模板，减少日志噪音
    
    Args:
        limit: 查询的日志数量 (聚合前的原始日志数)
        min_pattern_count: 最小聚合数量，低于此值的 pattern 不返回
        max_patterns: 返回的最大 pattern 数量
        max_samples: 每个 pattern 保留的示例日志数量
        service_name: 服务名过滤
        trace_id: Trace ID 过滤
        pod_name: Pod 名称过滤
        level: 日志级别过滤
        start_time: 开始时间
        end_time: 结束时间
        exclude_health_check: 过滤健康检查日志 (默认 True)
        search: 搜索关键词
    
    Returns:
        Dict[str, Any]: 聚合结果
        {
            "patterns": [
                {
                    "pattern": "Request {method} {path} completed in {duration}",
                    "count": 100,
                    "level": "INFO",
                    "samples": [...],
                    "variables": ["method", "path", "duration"]
                }
            ],
            "total_logs": 1000,
            "total_patterns": 10,
            "aggregation_ratio": 0.99
        }
    """
    if not _STORAGE_ADAPTER:
        raise HTTPException(status_code=503, detail="Storage adapter not initialized")

    try:
        normalized_limit = _normalize_int_param(limit, default=500, minimum=10, maximum=5000)
        normalized_min_pattern_count = _normalize_int_param(min_pattern_count, default=2, minimum=1, maximum=100)
        normalized_max_patterns = _normalize_int_param(max_patterns, default=50, minimum=1, maximum=200)
        normalized_max_samples = _normalize_int_param(max_samples, default=3, minimum=1, maximum=10)
        normalized_exclude_health_check = _normalize_bool_param(exclude_health_check, default=True)
        normalized_service_names = _normalize_optional_str_list(service_names)
        normalized_namespaces = _normalize_optional_str_list(namespaces)
        normalized_levels = _normalize_optional_str_list(levels)
        normalized_service_name = _normalize_optional_str(service_name)
        normalized_namespace = _normalize_optional_str(namespace)
        normalized_trace_id = _normalize_optional_str(trace_id)
        normalized_correlation_mode = _normalize_optional_str(correlation_mode)
        normalized_request_id = _normalize_optional_str(request_id)
        normalized_pod_name = _normalize_optional_str(pod_name)
        normalized_container_name = _normalize_optional_str(container_name)
        normalized_level = _normalize_optional_str(level)
        normalized_start_time = _normalize_optional_str(start_time)
        normalized_end_time = _normalize_optional_str(end_time)
        normalized_search = _normalize_optional_str(search)
        normalized_source_service = _normalize_optional_str(source_service)
        normalized_target_service = _normalize_optional_str(target_service)
        normalized_time_window = _normalize_optional_str(time_window)
        normalized_anchor_time = _normalize_optional_str(anchor_time)
        return await _run_blocking(
            logs_query_utils.query_logs_aggregated,
            storage_adapter=_STORAGE_ADAPTER,
            limit=normalized_limit,
            min_pattern_count=normalized_min_pattern_count,
            max_patterns=normalized_max_patterns,
            max_samples=normalized_max_samples,
            service_name=normalized_service_name,
            service_names=normalized_service_names,
            namespace=normalized_namespace,
            namespaces=normalized_namespaces,
            trace_id=normalized_trace_id,
            correlation_mode=normalized_correlation_mode,
            request_id=normalized_request_id,
            pod_name=normalized_pod_name,
            trace_ids=_normalize_optional_str_list(trace_ids),
            request_ids=_normalize_optional_str_list(request_ids),
            container_name=normalized_container_name,
            level=normalized_level,
            levels=normalized_levels,
            start_time=normalized_start_time,
            end_time=normalized_end_time,
            exclude_health_check=normalized_exclude_health_check,
            search=normalized_search,
            source_service=normalized_source_service,
            target_service=normalized_target_service,
            time_window=normalized_time_window,
            anchor_time=normalized_anchor_time,
            normalize_topology_context_fn=_normalize_topology_context,
            normalize_optional_str_list_fn=_normalize_optional_str_list,
            normalize_level_values_fn=_normalize_level_values,
            expand_level_match_values_fn=_expand_level_match_values,
            append_exact_match_filter_fn=_append_exact_match_filter,
            append_health_check_exclusion_fn=_append_health_check_exclusion,
            convert_timestamp_fn=_convert_timestamp,
            logger=logger,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"聚合查询日志时出错: {e}", extra={"error": str(e)})
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/logs/stats")
async def query_logs_stats(
    time_window: str = Query("24 HOUR", description="统计时间窗口"),
) -> Dict[str, Any]:
    """
    获取日志统计信息
    """
    if not _STORAGE_ADAPTER:
        raise HTTPException(status_code=503, detail="Storage adapter not initialized")

    try:
        normalized_time_window = _normalize_optional_str(time_window) or "24 HOUR"
        return await _run_blocking(obs_query_utils.query_logs_stats, _STORAGE_ADAPTER, time_window=normalized_time_window)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"获取日志统计信息时出错: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/metrics")
async def query_metrics(
    limit: int = Query(100, ge=1, le=10000),
    service_name: Optional[str] = Query(None),
    metric_name: Optional[str] = Query(None),
    start_time: Optional[str] = Query(None),
    end_time: Optional[str] = Query(None),
) -> Dict[str, Any]:
    """
    查询指标数据
    """
    if not _STORAGE_ADAPTER:
        raise HTTPException(status_code=503, detail="Storage adapter not initialized")

    try:
        safe_limit = _coerce_int_query_param(limit, default_value=100, minimum=1, maximum=10000)
        return await _run_blocking(
            obs_query_utils.query_metrics,
            storage_adapter=_STORAGE_ADAPTER,
            limit=safe_limit,
            service_name=service_name,
            metric_name=metric_name,
            start_time=start_time,
            end_time=end_time,
            convert_timestamp_fn=_convert_timestamp,
        )

    except Exception as e:
        logger.error(f"查询 Metrics 数据时出错: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/metrics/stats")
async def query_metrics_stats(
    time_window: str = Query("24 HOUR", description="统计时间窗口"),
) -> Dict[str, Any]:
    """
    获取指标统计信息
    """
    if not _STORAGE_ADAPTER:
        raise HTTPException(status_code=503, detail="Storage adapter not initialized")

    try:
        return await _run_blocking(obs_query_utils.query_metrics_stats, _STORAGE_ADAPTER, time_window=time_window)

    except Exception as e:
        logger.error(f"获取 Metrics 统计信息时出错: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/traces")
async def query_traces(
    limit: int = Query(100, ge=1, le=10000),
    offset: int = Query(0, ge=0, le=200000),
    service_name: Optional[str] = Query(None),
    trace_id: Optional[str] = Query(None),
    start_time: Optional[str] = Query(None),
    end_time: Optional[str] = Query(None),
    time_window: Optional[str] = Query(None, description="默认时间窗口（未传 start/end 时生效）"),
) -> Dict[str, Any]:
    """
    查询追踪数据
    """
    if not _STORAGE_ADAPTER:
        raise HTTPException(status_code=503, detail="Storage adapter not initialized")

    try:
        safe_limit = _coerce_int_query_param(limit, default_value=100, minimum=1, maximum=10000)
        safe_offset = _coerce_int_query_param(offset, default_value=0, minimum=0, maximum=200000)
        return await _run_blocking(
            obs_query_utils.query_traces,
            storage_adapter=_STORAGE_ADAPTER,
            limit=safe_limit,
            offset=safe_offset,
            service_name=service_name,
            trace_id=trace_id,
            start_time=start_time,
            end_time=end_time,
            time_window=time_window,
            resolve_trace_schema_fn=_resolve_trace_schema,
            build_grouped_trace_duration_expr_fn=_build_grouped_trace_duration_expr_light,
            normalize_trace_status_fn=_normalize_trace_status,
            convert_timestamp_fn=_convert_timestamp,
        )
    except ValueError as exc:
        raise HTTPException(status_code=500, detail="Internal server error")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"查询 Traces 数据时出错: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/traces/{trace_id}/spans")
async def query_trace_spans(
    trace_id: str,
    limit: int = Query(5000, ge=1, le=20000),
) -> List[Dict[str, Any]]:
    """查询某个 trace 的 spans 列表（用于前端时间线）。"""
    if not _STORAGE_ADAPTER:
        raise HTTPException(status_code=503, detail="Storage adapter not initialized")

    try:
        safe_limit = _coerce_int_query_param(limit, default_value=5000, minimum=1, maximum=20000)
        return await _run_blocking(
            obs_query_utils.query_trace_spans,
            storage_adapter=_STORAGE_ADAPTER,
            trace_id=trace_id,
            limit=safe_limit,
            resolve_trace_schema_fn=_resolve_trace_schema,
            parse_json_dict_fn=_parse_json_dict,
            extract_duration_ms_fn=_extract_duration_ms,
            normalize_trace_status_fn=_normalize_trace_status,
        )
    except ValueError as exc:
        raise HTTPException(status_code=500, detail="Internal server error")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"查询 Trace spans 时出错: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/traces/stats")
async def query_traces_stats(
    time_window: str = Query("24 HOUR", description="统计时间窗口"),
    start_time: Optional[str] = Query(None),
    end_time: Optional[str] = Query(None),
) -> Dict[str, Any]:
    """
    获取追踪统计信息
    """
    if not _STORAGE_ADAPTER:
        raise HTTPException(status_code=503, detail="Storage adapter not initialized")

    try:
        return await _run_blocking(
            obs_query_utils.query_traces_stats,
            storage_adapter=_STORAGE_ADAPTER,
            resolve_trace_schema_fn=_resolve_trace_schema,
            build_grouped_trace_duration_expr_fn=_build_grouped_trace_duration_expr,
            time_window=time_window,
            start_time=start_time,
            end_time=end_time,
            convert_timestamp_fn=_convert_timestamp,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"获取 Traces 统计信息时出错: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/logs/context")
async def query_logs_context(
    log_id: Optional[str] = Query(None, description="日志ID（优先锚定）"),
    trace_id: Optional[str] = Query(None, description="追踪ID"),
    pod_name: Optional[str] = Query(None, description="Pod名称"),
    namespace: Optional[str] = Query(None, description="命名空间"),
    container_name: Optional[str] = Query(None, description="容器名称"),
    timestamp: Optional[str] = Query(None, description="时间戳（ISO 8601格式）"),
    before_count: int = Query(5, ge=0, le=50, description="当前日志之前的条数"),
    after_count: int = Query(5, ge=0, le=50, description="当前日志之后的条数"),
    limit: int = Query(100, ge=1, le=1000),
) -> Dict[str, Any]:
    """
    查询日志上下文（支持 log_id / trace_id / pod_name + timestamp 模式）
    
    模式1: 通过 log_id 精确锚定日志，并返回同 Pod/命名空间/容器（若可用）的前后文
    模式2: 通过 trace_id 查询关联日志
    模式3: 通过 pod_name + timestamp 查询前后日志
    
    Args:
        log_id: 日志ID（模式1）
        trace_id: 追踪ID（模式2）
        pod_name: Pod名称（模式3）
        namespace: 命名空间（模式3，可选）
        container_name: 容器名称（模式3，可选）
        timestamp: 时间戳（模式3）
        before_count: 当前日志之前的条数（模式1/3）
        after_count: 当前日志之后的条数（模式1/3）
        limit: 返回数量限制
    
    Returns:
        Dict[str, Any]: 关联日志列表
    """
    if not _STORAGE_ADAPTER:
        raise HTTPException(status_code=503, detail="Storage adapter not initialized")

    try:
        normalized_before_count = _normalize_int_param(before_count, default=5, minimum=0, maximum=50)
        normalized_after_count = _normalize_int_param(after_count, default=5, minimum=0, maximum=50)
        normalized_limit = _normalize_int_param(limit, default=100, minimum=1, maximum=1000)
        normalized_log_id = _normalize_optional_str(log_id)
        normalized_trace_id = _normalize_optional_str(trace_id)
        normalized_pod_name = _normalize_optional_str(pod_name)
        normalized_namespace = _normalize_optional_str(namespace)
        normalized_container_name = _normalize_optional_str(container_name)
        normalized_timestamp = _normalize_optional_str(timestamp)
        return await _run_blocking(
            logs_query_utils.query_logs_context,
            storage_adapter=_STORAGE_ADAPTER,
            log_id=normalized_log_id,
            trace_id=normalized_trace_id,
            pod_name=normalized_pod_name,
            namespace=normalized_namespace,
            container_name=normalized_container_name,
            timestamp=normalized_timestamp,
            before_count=normalized_before_count,
            after_count=normalized_after_count,
            limit=normalized_limit,
            convert_timestamp_fn=_convert_timestamp,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"查询日志上下文时出错: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/logs/{log_id}")
async def query_log_detail(log_id: str) -> Dict[str, Any]:
    """
    获取日志详情（包含节点名、容器名等完整元数据）
    
    Args:
        log_id: 日志ID
    
    Returns:
        Dict[str, Any]: 日志详情
    """
    if not _STORAGE_ADAPTER:
        raise HTTPException(status_code=503, detail="Storage adapter not initialized")

    try:
        return await _run_blocking(
            logs_query_utils.query_log_detail,
            storage_adapter=_STORAGE_ADAPTER,
            log_id=log_id,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"查询日志详情时出错: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/trace-lite/inferred")
async def query_trace_lite_inferred(
    time_window: str = Query("1 HOUR", description="时间窗口"),
    source_service: Optional[str] = Query(None, description="源服务过滤"),
    target_service: Optional[str] = Query(None, description="目标服务过滤"),
    namespace: Optional[str] = Query(None, description="命名空间过滤"),
    limit: int = Query(100, ge=1, le=1000),
) -> Dict[str, Any]:
    """
    Trace-Lite 推断调用片段查询（M2-03）。
    """
    if not _STORAGE_ADAPTER:
        raise HTTPException(status_code=503, detail="Storage adapter not initialized")

    try:
        return await _run_blocking(
            inference_query_utils.query_trace_lite_inferred,
            storage_adapter=_STORAGE_ADAPTER,
            time_window=time_window,
            source_service=source_service,
            target_service=target_service,
            namespace=namespace,
            limit=limit,
            sanitize_interval_fn=_sanitize_interval,
            infer_trace_lite_fragments_fn=_infer_trace_lite_fragments_from_logs,
        )
    except Exception as e:
        logger.error(f"查询 trace-lite inferred 失败: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/trace-lite/pilot/readiness")
async def trace_lite_pilot_readiness(
    time_window: str = Query("24 HOUR", description="时间窗口"),
    min_services: int = Query(2, ge=1, le=20, description="验收最小服务数")
) -> Dict[str, Any]:
    """
    老业务接入试点验收辅助接口（M2-06）。
    """
    if not _STORAGE_ADAPTER:
        raise HTTPException(status_code=503, detail="Storage adapter not initialized")

    try:
        return await _run_blocking(
            inference_query_utils.trace_lite_pilot_readiness,
            storage_adapter=_STORAGE_ADAPTER,
            time_window=time_window,
            min_services=min_services,
            sanitize_interval_fn=_sanitize_interval,
            infer_trace_lite_fragments_fn=_infer_trace_lite_fragments_from_logs,
        )
    except Exception as e:
        logger.error(f"trace-lite pilot readiness 计算失败: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/quality/inference")
async def inference_quality_metrics(
    time_window: str = Query("1 HOUR", description="时间窗口"),
) -> Dict[str, Any]:
    """
    推断质量指标（M2-05）：coverage / inferred_ratio / false_positive_rate。
    """
    if not _STORAGE_ADAPTER:
        raise HTTPException(status_code=503, detail="Storage adapter not initialized")

    try:
        return await _run_blocking(
            inference_query_utils.inference_quality_metrics,
            storage_adapter=_STORAGE_ADAPTER,
            time_window=time_window,
            sanitize_interval_fn=_sanitize_interval,
            infer_trace_lite_fragments_fn=_infer_trace_lite_fragments_from_logs,
        )
    except Exception as e:
        logger.error(f"推断质量指标计算失败: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/quality/inference/alerts")
async def inference_quality_alerts(
    time_window: str = Query("1 HOUR", description="时间窗口"),
    min_coverage: float = Query(0.20, ge=0.0, le=1.0),
    max_inferred_ratio: float = Query(0.80, ge=0.0, le=1.0),
    max_false_positive_rate: float = Query(0.30, ge=0.0, le=1.0),
) -> Dict[str, Any]:
    """推断质量告警评估（支持 suppression 标记）。"""
    result = await inference_quality_metrics(time_window=time_window)
    metrics = result.get("metrics", {})

    return inference_query_utils.inference_quality_alerts(
        metrics=metrics,
        time_window=time_window,
        min_coverage=min_coverage,
        max_inferred_ratio=max_inferred_ratio,
        max_false_positive_rate=max_false_positive_rate,
        suppressed_metrics=_INFERENCE_ALERT_SUPPRESSIONS,
    )


@router.post("/quality/inference/alerts/suppress")
async def suppress_inference_alert(
    metric: str = Query(..., description="metric 名称: coverage|inferred_ratio|false_positive_rate"),
    enabled: bool = Query(True, description="true=抑制, false=取消抑制"),
) -> Dict[str, Any]:
    """设置或取消推断质量告警抑制。"""
    valid_metrics = {"coverage", "inferred_ratio", "false_positive_rate"}
    if metric not in valid_metrics:
        raise HTTPException(status_code=400, detail=f"Unsupported metric: {metric}")

    if enabled:
        _INFERENCE_ALERT_SUPPRESSIONS.add(metric)
    else:
        _INFERENCE_ALERT_SUPPRESSIONS.discard(metric)

    return {
        "status": "ok",
        "metric": metric,
        "suppressed": metric in _INFERENCE_ALERT_SUPPRESSIONS,
        "suppressed_metrics": sorted(_INFERENCE_ALERT_SUPPRESSIONS),
    }


@router.get("/value/kpi")
async def value_kpi_dashboard(
    time_window: str = Query("7 DAY", description="统计时间窗口"),
    force_refresh: bool = Query(False, description="是否强制绕过缓存"),
) -> Dict[str, Any]:
    """
    M4-02 价值指标看板：
    - MTTD / MTTR
    - trace-log 关联率
    - 拓扑覆盖率
    - 发布回归通过率
    """
    if not _STORAGE_ADAPTER:
        raise HTTPException(status_code=503, detail="Storage adapter not initialized")

    try:
        computed = _compute_value_kpis(time_window=time_window, use_cache=not force_refresh)
        return {
            "status": "ok",
            "time_window": _sanitize_interval(time_window, default_value="7 DAY"),
            "metrics": computed["metrics"],
            "incident_summary": computed["incident_summary"],
            "release_gate_summary": computed["release_gate_summary"],
            "cache": {
                "enabled": not force_refresh,
                "ttl_seconds": VALUE_KPI_CACHE_TTL_SECONDS,
                "size": len(_VALUE_KPI_CACHE),
            },
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as exc:
        logger.error(f"计算 value KPI 失败: {exc}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/value/kpi/weekly-export")
async def value_kpi_weekly_export(
    weeks: int = Query(8, ge=1, le=12, description="导出最近 N 周"),
) -> Response:
    """
    M4-02 周报导出（CSV）。
    """
    if not _STORAGE_ADAPTER:
        raise HTTPException(status_code=503, detail="Storage adapter not initialized")

    try:
        csv_text, filename = await _run_blocking(
            value_kpi_utils.build_value_kpi_weekly_csv,
            weeks=weeks,
            compute_value_kpis_fn=lambda start_dt, end_dt: _compute_value_kpis(
                start_time=start_dt.isoformat(),
                end_time=end_dt.isoformat(),
                time_window="7 DAY",
                use_cache=False,
            ),
        )
        return Response(
            content=csv_text,
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": f"attachment; filename={filename}"
            },
        )
    except Exception as exc:
        logger.error(f"导出 value KPI 周报失败: {exc}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/value/kpi/alerts")
async def value_kpi_alerts(
    time_window: str = Query("7 DAY", description="统计时间窗口"),
    force_refresh: bool = Query(False, description="是否强制绕过缓存"),
    max_mttd_minutes: float = Query(15.0, ge=0.0, description="MTTD 上限（分钟）"),
    max_mttr_minutes: float = Query(30.0, ge=0.0, description="MTTR 上限（分钟）"),
    min_trace_log_correlation_rate: float = Query(0.50, ge=0.0, le=1.0, description="trace-log 关联率下限"),
    min_topology_coverage_rate: float = Query(0.50, ge=0.0, le=1.0, description="拓扑覆盖率下限"),
    min_release_regression_pass_rate: float = Query(0.95, ge=0.0, le=1.0, description="发布回归通过率下限"),
) -> Dict[str, Any]:
    """
    M4-05 价值 KPI 告警评估（支持 suppression）。
    """
    if not _STORAGE_ADAPTER:
        raise HTTPException(status_code=503, detail="Storage adapter not initialized")

    computed = _compute_value_kpis(time_window=time_window, use_cache=not force_refresh)
    metrics = computed.get("metrics", {})
    alerts = _evaluate_value_kpi_alerts(
        metrics=metrics,
        max_mttd_minutes=max_mttd_minutes,
        max_mttr_minutes=max_mttr_minutes,
        min_trace_log_correlation_rate=min_trace_log_correlation_rate,
        min_topology_coverage_rate=min_topology_coverage_rate,
        min_release_regression_pass_rate=min_release_regression_pass_rate,
    )
    return {
        "status": "ok",
        "time_window": _sanitize_interval(time_window, default_value="7 DAY"),
        "metrics": metrics,
        "alerts": alerts,
        "active_alerts": sum(1 for item in alerts if not item.get("suppressed")),
        "suppressed_metrics": sorted(_VALUE_KPI_ALERT_SUPPRESSIONS),
        "thresholds": {
            "max_mttd_minutes": max_mttd_minutes,
            "max_mttr_minutes": max_mttr_minutes,
            "min_trace_log_correlation_rate": min_trace_log_correlation_rate,
            "min_topology_coverage_rate": min_topology_coverage_rate,
            "min_release_regression_pass_rate": min_release_regression_pass_rate,
        },
        "cache": {
            "enabled": not force_refresh,
            "ttl_seconds": VALUE_KPI_CACHE_TTL_SECONDS,
            "size": len(_VALUE_KPI_CACHE),
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


@router.post("/value/kpi/alerts/suppress")
async def suppress_value_kpi_alert(
    metric: str = Query(
        ...,
        description="metric 名称: mttd_minutes|mttr_minutes|trace_log_correlation_rate|topology_coverage_rate|release_regression_pass_rate",
    ),
    enabled: bool = Query(True, description="true=抑制, false=取消抑制"),
) -> Dict[str, Any]:
    """设置或取消 value KPI 告警抑制。"""
    valid_metrics = {
        "mttd_minutes",
        "mttr_minutes",
        "trace_log_correlation_rate",
        "topology_coverage_rate",
        "release_regression_pass_rate",
    }
    if metric not in valid_metrics:
        raise HTTPException(status_code=400, detail=f"Unsupported metric: {metric}")

    if enabled:
        _VALUE_KPI_ALERT_SUPPRESSIONS.add(metric)
    else:
        _VALUE_KPI_ALERT_SUPPRESSIONS.discard(metric)

    return {
        "status": "ok",
        "metric": metric,
        "suppressed": metric in _VALUE_KPI_ALERT_SUPPRESSIONS,
        "suppressed_metrics": sorted(_VALUE_KPI_ALERT_SUPPRESSIONS),
    }


@router.get("/value/kpi/cache/stats")
async def value_kpi_cache_stats() -> Dict[str, Any]:
    """查询 value KPI 缓存状态。"""
    now_ts = time.time()
    _evict_expired_value_kpi_cache(now_ts)
    live_entries = [
        {
            "key": key,
            "expires_in_seconds": max(0, int(expiry - now_ts)),
        }
        for key, (_, expiry) in _VALUE_KPI_CACHE.items()
    ]
    return {
        "status": "ok",
        "size": len(_VALUE_KPI_CACHE),
        "ttl_seconds": VALUE_KPI_CACHE_TTL_SECONDS,
        "max_entries": VALUE_KPI_CACHE_MAX_ENTRIES,
        "metrics": _build_value_kpi_cache_metrics_snapshot(),
        "entries": live_entries,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


@router.delete("/value/kpi/cache")
async def clear_value_kpi_cache() -> Dict[str, Any]:
    """手动清理 value KPI 缓存。"""
    cleared = len(_VALUE_KPI_CACHE)
    _VALUE_KPI_CACHE.clear()
    _record_value_kpi_cache_clear(reason="manual", cleared=cleared)
    return {
        "status": "ok",
        "cleared": cleared,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


@router.post("/value/kpi/snapshots")
async def capture_value_kpi_snapshot(
    time_window: str = Query("7 DAY", description="统计时间窗口"),
    source: str = Query("manual", min_length=1, max_length=64, description="快照来源标识"),
) -> Dict[str, Any]:
    """
    M4-06 生成并持久化 KPI 快照。
    """
    if not _STORAGE_ADAPTER:
        raise HTTPException(status_code=503, detail="Storage adapter not initialized")

    try:
        safe_window = _sanitize_interval(time_window, default_value="7 DAY")
        window_end = datetime.now(timezone.utc)
        window_start = window_end - _interval_to_timedelta(safe_window, default_value="7 DAY")
        computed = await _run_blocking(
            _compute_value_kpis,
            time_window=safe_window,
            start_time=window_start.isoformat(),
            end_time=window_end.isoformat(),
            use_cache=False,
        )
        snapshot = await _run_blocking(
            _store_value_kpi_snapshot,
            computed=computed,
            time_window=safe_window,
            source=str(source).strip() or "manual",
            window_start=window_start,
            window_end=window_end,
        )
        return {
            "status": "ok",
            **snapshot,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as exc:
        logger.error(f"写入 value KPI 快照失败: {exc}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/value/kpi/snapshots")
async def list_value_kpi_snapshots(
    limit: int = Query(12, ge=1, le=200),
    source: Optional[str] = Query(None, description="按来源过滤"),
) -> Dict[str, Any]:
    """
    M4-06 查询 KPI 快照历史。
    """
    if not _STORAGE_ADAPTER:
        raise HTTPException(status_code=503, detail="Storage adapter not initialized")

    try:
        rows = await _run_blocking(_list_value_kpi_snapshots, limit=limit, source=source)
        return {
            "status": "ok",
            "count": len(rows),
            "data": rows,
            "limit": limit,
            "source": source,
        }
    except Exception as exc:
        logger.error(f"查询 value KPI 快照失败: {exc}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/metrics/performance")
async def get_performance_metrics() -> Dict[str, Any]:
    """
    获取 query-service 性能指标（QS-03）。

    与业务 Metrics 查询接口（/metrics）解耦，避免路由冲突。
    """
    try:
        from shared_src.monitoring import get_metrics, get_request_tracker
        
        metrics = get_metrics()
        tracker = get_request_tracker()
        
        return {
            "performance": metrics.get_all_stats(),
            "requests": {
                "active": tracker.get_active_count(),
            },
            "timestamp": datetime.now().isoformat(),
        }
    except ImportError:
        return {
            "error": "Monitoring module not available",
            "timestamp": datetime.now().isoformat(),
        }
    except Exception as e:
        logger.error(f"获取性能指标时出错: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
