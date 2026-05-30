"""Observability query domain services extracted from query_routes."""

from __future__ import annotations

import logging
import math
import os
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from api.query_params import sanitize_interval

logger = logging.getLogger(__name__)

_PREAGG_TABLE_CACHE: Dict[str, Any] = {
    "expires_at": 0.0,
    "tables": set(),
}
_PREAGG_TABLE_CACHE_TTL_SECONDS = 60
_TRACE_TIME_COLUMN_WHITELIST = {"timestamp", "start_time"}
_PREAGG_SCHEMA_VERSIONS = {"legacy", "v2", "auto"}
_LEGACY_PREAGG_TABLES = ("logs_stats_1m", "metrics_stats_1m", "traces_stats_1m")
_V2_PREAGG_TABLES = ("obs_counts_1m", "obs_traces_1m")
_KNOWN_PREAGG_TABLES = tuple(sorted(set(_LEGACY_PREAGG_TABLES + _V2_PREAGG_TABLES)))
_LOG_LEVEL_BUCKET_LIMIT = 8


def _read_positive_int_env(name: str, default_value: int, min_value: int = 1, max_value: int = 200000) -> int:
    raw = os.getenv(name, str(default_value))
    try:
        parsed = int(str(raw).strip())
    except Exception:
        parsed = default_value
    return max(min_value, min(parsed, max_value))


_QUERY_TRACES_DEFAULT_TIME_WINDOW = sanitize_interval(
    os.getenv("QUERY_TRACES_DEFAULT_TIME_WINDOW", "24 HOUR"),
    default_value="24 HOUR",
)
_QUERY_LOGS_STATS_DEFAULT_TIME_WINDOW = sanitize_interval(
    os.getenv("QUERY_LOGS_STATS_DEFAULT_TIME_WINDOW", "24 HOUR"),
    default_value="24 HOUR",
)
_QUERY_METRICS_DEFAULT_TIME_WINDOW = sanitize_interval(
    os.getenv("QUERY_METRICS_DEFAULT_TIME_WINDOW", "24 HOUR"),
    default_value="24 HOUR",
)
_QUERY_METRICS_STATS_DEFAULT_TIME_WINDOW = sanitize_interval(
    os.getenv("QUERY_METRICS_STATS_DEFAULT_TIME_WINDOW", "24 HOUR"),
    default_value="24 HOUR",
)
_QUERY_TRACES_STATS_DEFAULT_TIME_WINDOW = sanitize_interval(
    os.getenv("QUERY_TRACES_STATS_DEFAULT_TIME_WINDOW", "24 HOUR"),
    default_value="24 HOUR",
)
_TRACE_STATS_DURATION_SAMPLE_LIMIT = _read_positive_int_env("TRACE_STATS_DURATION_SAMPLE_LIMIT", 8000)
_TRACE_QUERY_RECENT_SPAN_SCAN_LIMIT = _read_positive_int_env(
    "TRACE_QUERY_RECENT_SPAN_SCAN_LIMIT",
    8000,
    min_value=1000,
    max_value=500000,
)
_TRACE_QUERY_RECENT_SPAN_HARD_LIMIT = _read_positive_int_env(
    "TRACE_QUERY_RECENT_SPAN_HARD_LIMIT",
    16000,
    min_value=5000,
    max_value=1000000,
)
_TRACE_QUERY_RECENT_SPAN_SCAN_FACTOR = _read_positive_int_env(
    "TRACE_QUERY_RECENT_SPAN_SCAN_FACTOR",
    12,
    min_value=2,
    max_value=200,
)
_TRACE_QUERY_RECENT_SPAN_SHORT_WINDOW_CAP = _read_positive_int_env(
    "TRACE_QUERY_RECENT_SPAN_SHORT_WINDOW_CAP",
    12000,
    min_value=2000,
    max_value=500000,
)
_TRACE_QUERY_RECENT_SPAN_MEDIUM_WINDOW_CAP = _read_positive_int_env(
    "TRACE_QUERY_RECENT_SPAN_MEDIUM_WINDOW_CAP",
    16000,
    min_value=5000,
    max_value=800000,
)
_TRACE_STATS_DURATION_SCAN_FACTOR = _read_positive_int_env(
    "TRACE_STATS_DURATION_SCAN_FACTOR",
    8,
    min_value=2,
    max_value=200,
)
_TRACE_STATS_DURATION_SCAN_HARD_LIMIT = _read_positive_int_env(
    "TRACE_STATS_DURATION_SCAN_HARD_LIMIT",
    80000,
    min_value=20000,
    max_value=2000000,
)


def _sanitize_json_float(value: Any, default: float = 0.0) -> float:
    """Return a finite float for JSON responses."""
    try:
        numeric = float(value)
    except Exception:
        return default
    if not math.isfinite(numeric):
        return default
    return numeric


def _read_preagg_schema_version() -> str:
    raw = str(os.getenv("PREAGG_SCHEMA_VERSION", "auto") or "auto").strip().lower()
    if raw not in _PREAGG_SCHEMA_VERSIONS:
        return "auto"
    return raw


def get_expected_preagg_tables() -> List[str]:
    version = _read_preagg_schema_version()
    if version == "legacy":
        return list(_LEGACY_PREAGG_TABLES)
    return list(_V2_PREAGG_TABLES)


def _should_try_v2_preagg() -> bool:
    return _read_preagg_schema_version() in {"v2", "auto"}


def _should_try_legacy_preagg() -> bool:
    return _read_preagg_schema_version() in {"legacy", "auto"}


def _load_preagg_tables(storage_adapter: Any) -> set:
    now = time.time()
    expires_at = float(_PREAGG_TABLE_CACHE.get("expires_at", 0.0) or 0.0)
    cached = _PREAGG_TABLE_CACHE.get("tables")
    if isinstance(cached, set) and now < expires_at:
        return cached

    try:
        known_tables = ", ".join([f"'{name}'" for name in _KNOWN_PREAGG_TABLES])
        rows = storage_adapter.execute_query(
            f"""
            SELECT name
            FROM system.tables
            WHERE database = 'logs'
              AND name IN ({known_tables})
            """
        )
        tables = {str(row.get("name") or "").strip() for row in rows if str(row.get("name") or "").strip()}
    except Exception:
        tables = set()

    _PREAGG_TABLE_CACHE["tables"] = tables
    _PREAGG_TABLE_CACHE["expires_at"] = now + _PREAGG_TABLE_CACHE_TTL_SECONDS
    return tables


def _has_preagg_table(storage_adapter: Any, table_name: str) -> bool:
    return table_name in _load_preagg_tables(storage_adapter)


def _resolve_safe_trace_time_column(schema: Dict[str, Optional[str]]) -> Optional[str]:
    raw = str((schema or {}).get("time_col") or "").strip()
    if not raw:
        return None
    if raw not in _TRACE_TIME_COLUMN_WHITELIST:
        return None
    return raw


def _build_level_bucket_expr(column_name: str) -> str:
    normalized = f"upperUTF8(trim(BOTH ' ' FROM toString(ifNull({column_name}, ''))))"
    return (
        "multiIf("
        f"{normalized} = '', 'OTHER', "
        f"{normalized} = 'WARNING', 'WARN', "
        f"{normalized}"
        ")"
    )


def _row_level_bucket(row: Dict[str, Any]) -> str:
    """Read grouped level bucket from query rows with backward-compatible aliases."""
    value = str(row.get("level_bucket") or row.get("level") or "OTHER").strip().upper()
    if not value:
        return "OTHER"
    if value == "WARNING":
        return "WARN"
    return value


def _interval_to_minutes(interval_text: str) -> int:
    """Convert sanitized interval text (e.g. '1 HOUR') into minutes."""
    try:
        amount_text, unit_text = str(interval_text or "").strip().split(maxsplit=1)
        amount = max(int(amount_text), 1)
    except Exception:
        return 24 * 60

    unit = unit_text.strip().upper()
    if unit == "MINUTE":
        return amount
    if unit == "HOUR":
        return amount * 60
    if unit == "DAY":
        return amount * 24 * 60
    if unit == "WEEK":
        return amount * 7 * 24 * 60
    return 24 * 60


def _compute_recent_span_scan_limit(
    limit: int,
    safe_window: str,
    has_trace_id: bool,
    has_service_name: bool,
) -> int:
    """Compute bounded recent span scan size for trace listing."""
    safe_limit = max(int(limit), 1)
    floor_limit = int(_TRACE_QUERY_RECENT_SPAN_SCAN_LIMIT)
    hard_limit = int(_TRACE_QUERY_RECENT_SPAN_HARD_LIMIT)
    factor = int(_TRACE_QUERY_RECENT_SPAN_SCAN_FACTOR)

    # Trace-id query is already selective; reduce scan aggressively.
    if has_trace_id:
        floor_limit = min(floor_limit, 4000)
        hard_limit = min(hard_limit, 12000)
        factor = min(factor, 12)

    # Service filter narrows dataset; cap scan tighter than global hard cap.
    if has_service_name:
        hard_limit = min(hard_limit, int(_TRACE_QUERY_RECENT_SPAN_MEDIUM_WINDOW_CAP))

    # 小页查询采用自适应 floor，避免固定 floor 导致过扫。
    adaptive_floor = min(floor_limit, max(3000, safe_limit * 12))
    dynamic_limit = max(safe_limit * factor, adaptive_floor)

    window_minutes = _interval_to_minutes(safe_window)
    if window_minutes <= 60:
        dynamic_limit = min(dynamic_limit, int(_TRACE_QUERY_RECENT_SPAN_SHORT_WINDOW_CAP))
    elif window_minutes <= 24 * 60:
        dynamic_limit = min(dynamic_limit, int(_TRACE_QUERY_RECENT_SPAN_MEDIUM_WINDOW_CAP))

    return min(dynamic_limit, hard_limit)


def _parse_start_time_epoch_ms(raw_value: Any) -> float:
    """Parse span start time text to epoch milliseconds."""
    text = str(raw_value or "").strip()
    if not text:
        return 0.0

    numeric_text = text
    if numeric_text and numeric_text[0] in {"+", "-"}:
        numeric_text = numeric_text[1:]
    if numeric_text.replace(".", "", 1).isdigit():
        try:
            value = float(text)
        except Exception:
            value = 0.0
        if value <= 0:
            return 0.0
        absolute_value = abs(value)
        if absolute_value < 1e11:  # seconds
            return value * 1000.0
        if absolute_value < 1e14:  # milliseconds
            return value
        if absolute_value < 1e17:  # microseconds
            return value / 1000.0
        return value / 1_000_000.0  # nanoseconds

    normalized = text
    if " " in normalized and "T" not in normalized:
        normalized = normalized.replace(" ", "T")
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"

    try:
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp() * 1000.0
    except Exception as exc:
        logger.debug("Failed to parse start_time as ISO8601: %r (%s)", raw_value, exc)

    for pattern in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(text, pattern).replace(tzinfo=timezone.utc)
            return dt.timestamp() * 1000.0
        except Exception:
            continue
    return 0.0


def _infer_all_zero_span_durations(spans: List[Dict[str, Any]]) -> None:
    """
    Infer approximate span durations when all returned durations are zero.

    This is a read-time fallback for legacy data quality issues and does not
    replace real duration instrumentation.
    """
    if not spans:
        return

    annotated: List[Dict[str, Any]] = []
    for index, span in enumerate(spans):
        annotated.append(
            {
                "index": index,
                "start_ms": _parse_start_time_epoch_ms(span.get("start_time")),
                "span_id": str(span.get("span_id") or "").strip(),
                "parent_span_id": str(span.get("parent_span_id") or "").strip(),
            }
        )

    valid_starts = [item["start_ms"] for item in annotated if item["start_ms"] > 0]
    if not valid_starts:
        return
    trace_end_ms = max(valid_starts)

    children_by_parent: Dict[str, List[float]] = {}
    for item in annotated:
        parent_id = item["parent_span_id"]
        if parent_id and item["start_ms"] > 0:
            children_by_parent.setdefault(parent_id, []).append(item["start_ms"])

    sorted_items = sorted(
        annotated,
        key=lambda item: (item["start_ms"] if item["start_ms"] > 0 else float("inf"), item["index"]),
    )
    next_start_by_index: Dict[int, float] = {}
    for pos, item in enumerate(sorted_items):
        start_ms = item["start_ms"]
        if start_ms <= 0:
            continue
        for next_pos in range(pos + 1, len(sorted_items)):
            candidate = sorted_items[next_pos]["start_ms"]
            if candidate > start_ms:
                next_start_by_index[item["index"]] = candidate
                break

    for item in annotated:
        span = spans[item["index"]]
        try:
            existing_duration = float(span.get("duration_ms") or 0.0)
        except Exception:
            existing_duration = 0.0
        if existing_duration > 0:
            continue

        start_ms = item["start_ms"]
        if start_ms <= 0:
            continue

        inferred_ms = 0.0
        span_id = item["span_id"]
        child_starts = children_by_parent.get(span_id, []) if span_id else []
        if child_starts:
            child_end = max(child_starts)
            if child_end > start_ms:
                inferred_ms = max(inferred_ms, child_end - start_ms)

        next_start = next_start_by_index.get(item["index"], 0.0)
        if next_start > start_ms:
            inferred_ms = max(inferred_ms, next_start - start_ms)

        if inferred_ms <= 0 and child_starts and trace_end_ms > start_ms:
            inferred_ms = max(inferred_ms, trace_end_ms - start_ms)

        if inferred_ms > 0:
            span["duration_ms"] = round(inferred_ms, 3)


def _query_logs_stats_preaggregated_v2(storage_adapter: Any, time_window: str) -> Optional[Dict[str, Any]]:
    if not _has_preagg_table(storage_adapter, "obs_counts_1m"):
        return None

    total_rows = storage_adapter.execute_query(
        f"""
        SELECT sum(count) AS total
        FROM logs.obs_counts_1m
        PREWHERE signal = 'log'
             AND dim_name = 'level'
             AND ts_minute > now() - INTERVAL {time_window}
        """
    )
    by_service_rows = storage_adapter.execute_query(
        f"""
        WITH { _build_level_bucket_expr("dim_value") } AS level_bucket
        SELECT
            service_name,
            sum(count) AS total_count,
            sumIf(logs.obs_counts_1m.count, level_bucket IN ('ERROR', 'FATAL', 'CRITICAL')) AS error_count
        FROM logs.obs_counts_1m
        PREWHERE signal = 'log'
             AND dim_name = 'level'
             AND ts_minute > now() - INTERVAL {time_window}
        GROUP BY service_name
        ORDER BY total_count DESC
        LIMIT 20
        """
    )
    level_bucket_expr = _build_level_bucket_expr("dim_value")
    by_level_rows = storage_adapter.execute_query(
        f"""
        SELECT
            {level_bucket_expr} AS level_bucket,
            sum(count) AS count
        FROM logs.obs_counts_1m
        PREWHERE signal = 'log'
             AND dim_name = 'level'
             AND ts_minute > now() - INTERVAL {time_window}
        GROUP BY {level_bucket_expr}
        ORDER BY count DESC
        LIMIT {_LOG_LEVEL_BUCKET_LIMIT}
        """
    )

    total = int(total_rows[0].get("total", 0)) if total_rows else 0
    by_service = {
        str(row.get("service_name") or "unknown"): int(row.get("total_count", row.get("count", 0)))
        for row in by_service_rows
    }
    by_service_errors = {
        str(row.get("service_name") or "unknown"): int(row.get("error_count", 0))
        for row in by_service_rows
    }
    by_level = {_row_level_bucket(row): int(row.get("count", 0)) for row in by_level_rows}

    if total <= 0 and not by_service and not by_level:
        return None
    return {
        "total": total,
        "byService": by_service,
        "byServiceErrors": by_service_errors,
        "byLevel": by_level,
    }


def _query_logs_stats_preaggregated_legacy(storage_adapter: Any, time_window: str) -> Optional[Dict[str, Any]]:
    if not _has_preagg_table(storage_adapter, "logs_stats_1m"):
        return None

    total_rows = storage_adapter.execute_query(
        f"SELECT sum(count) AS total FROM logs.logs_stats_1m PREWHERE ts_minute > now() - INTERVAL {time_window}"
    )
    by_service_rows = storage_adapter.execute_query(
        f"""
        WITH { _build_level_bucket_expr("level") } AS level_bucket
        SELECT
            service_name,
            sum(count) AS total_count,
            sumIf(logs.logs_stats_1m.count, level_bucket IN ('ERROR', 'FATAL', 'CRITICAL')) AS error_count
        FROM logs.logs_stats_1m
        PREWHERE ts_minute > now() - INTERVAL {time_window}
        GROUP BY service_name
        ORDER BY total_count DESC
        LIMIT 20
        """
    )
    level_bucket_expr = _build_level_bucket_expr("level")
    by_level_rows = storage_adapter.execute_query(
        f"""
        SELECT
            {level_bucket_expr} AS level_bucket,
            sum(count) AS count
        FROM logs.logs_stats_1m
        PREWHERE ts_minute > now() - INTERVAL {time_window}
        GROUP BY {level_bucket_expr}
        ORDER BY count DESC
        LIMIT {_LOG_LEVEL_BUCKET_LIMIT}
        """
    )

    total = int(total_rows[0].get("total", 0)) if total_rows else 0
    by_service = {
        str(row.get("service_name") or "unknown"): int(row.get("total_count", row.get("count", 0)))
        for row in by_service_rows
    }
    by_service_errors = {
        str(row.get("service_name") or "unknown"): int(row.get("error_count", 0))
        for row in by_service_rows
    }
    by_level = {_row_level_bucket(row): int(row.get("count", 0)) for row in by_level_rows}

    if total <= 0 and not by_service and not by_level:
        return None
    return {
        "total": total,
        "byService": by_service,
        "byServiceErrors": by_service_errors,
        "byLevel": by_level,
    }


def _query_logs_stats_preaggregated(storage_adapter: Any, time_window: str) -> Optional[Dict[str, Any]]:
    if _should_try_v2_preagg():
        stats_v2 = _query_logs_stats_preaggregated_v2(storage_adapter, time_window=time_window)
        if stats_v2 is not None:
            return stats_v2
    if _should_try_legacy_preagg():
        return _query_logs_stats_preaggregated_legacy(storage_adapter, time_window=time_window)
    return None


def _query_metrics_stats_preaggregated_v2(storage_adapter: Any, time_window: str) -> Optional[Dict[str, Any]]:
    if not _has_preagg_table(storage_adapter, "obs_counts_1m"):
        return None

    total_rows = storage_adapter.execute_query(
        f"""
        SELECT sum(count) AS total
        FROM logs.obs_counts_1m
        PREWHERE signal = 'metric'
             AND dim_name = 'metric_name'
             AND ts_minute > now() - INTERVAL {time_window}
        """
    )
    by_service_rows = storage_adapter.execute_query(
        f"""
        SELECT
            service_name,
            sum(count) AS count
        FROM logs.obs_counts_1m
        PREWHERE signal = 'metric'
             AND dim_name = 'metric_name'
             AND ts_minute > now() - INTERVAL {time_window}
        GROUP BY service_name
        ORDER BY count DESC
        LIMIT 20
        """
    )
    by_metric_rows = storage_adapter.execute_query(
        f"""
        SELECT
            dim_value AS metric_name,
            sum(count) AS count
        FROM logs.obs_counts_1m
        PREWHERE signal = 'metric'
             AND dim_name = 'metric_name'
             AND ts_minute > now() - INTERVAL {time_window}
        GROUP BY dim_value
        ORDER BY count DESC
        LIMIT 20
        """
    )

    total = int(total_rows[0].get("total", 0)) if total_rows else 0
    by_service = {
        str(row.get("service_name") or "unknown"): int(row.get("total_count", row.get("count", 0)))
        for row in by_service_rows
    }
    by_metric = {
        str(row.get("metric_name") or "unknown"): int(row.get("count", 0))
        for row in by_metric_rows
    }

    if total <= 0 and not by_service and not by_metric:
        return None
    return {
        "total": total,
        "byService": by_service,
        "byMetricName": by_metric,
    }


def _query_metrics_stats_preaggregated_legacy(storage_adapter: Any, time_window: str) -> Optional[Dict[str, Any]]:
    if not _has_preagg_table(storage_adapter, "metrics_stats_1m"):
        return None

    total_rows = storage_adapter.execute_query(
        f"SELECT sum(count) AS total FROM logs.metrics_stats_1m PREWHERE ts_minute > now() - INTERVAL {time_window}"
    )
    by_service_rows = storage_adapter.execute_query(
        f"""
        SELECT
            service_name,
            sum(count) AS count
        FROM logs.metrics_stats_1m
        PREWHERE ts_minute > now() - INTERVAL {time_window}
        GROUP BY service_name
        ORDER BY count DESC
        LIMIT 20
        """
    )
    by_metric_rows = storage_adapter.execute_query(
        f"""
        SELECT
            metric_name,
            sum(count) AS count
        FROM logs.metrics_stats_1m
        PREWHERE ts_minute > now() - INTERVAL {time_window}
        GROUP BY metric_name
        ORDER BY count DESC
        LIMIT 20
        """
    )

    total = int(total_rows[0].get("total", 0)) if total_rows else 0
    by_service = {
        str(row.get("service_name") or "unknown"): int(row.get("total_count", row.get("count", 0)))
        for row in by_service_rows
    }
    by_metric = {
        str(row.get("metric_name") or "unknown"): int(row.get("count", 0))
        for row in by_metric_rows
    }

    if total <= 0 and not by_service and not by_metric:
        return None
    return {
        "total": total,
        "byService": by_service,
        "byMetricName": by_metric,
    }


def _query_metrics_stats_preaggregated(storage_adapter: Any, time_window: str) -> Optional[Dict[str, Any]]:
    if _should_try_v2_preagg():
        stats_v2 = _query_metrics_stats_preaggregated_v2(storage_adapter, time_window=time_window)
        if stats_v2 is not None:
            return stats_v2
    if _should_try_legacy_preagg():
        return _query_metrics_stats_preaggregated_legacy(storage_adapter, time_window=time_window)
    return None


def _query_traces_stats_preaggregated_v2(
    storage_adapter: Any,
    where_clause: str = "",
    params: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    if not _has_preagg_table(storage_adapter, "obs_traces_1m"):
        return None

    trace_rows = storage_adapter.execute_query(
        f"""
        SELECT
            sumMerge(span_count_state) AS span_count,
            sumMerge(error_span_count_state) AS error_span_count,
            uniqCombined64Merge(trace_id_state) AS trace_count,
            uniqCombined64Merge(error_trace_id_state) AS error_trace_count
        FROM logs.obs_traces_1m
        {where_clause}
        """,
        params or {},
    )
    by_service_rows = storage_adapter.execute_query(
        f"""
        SELECT
            service_name,
            uniqCombined64Merge(trace_id_state) AS count
        FROM logs.obs_traces_1m
        {where_clause}
        GROUP BY service_name
        ORDER BY count DESC
        LIMIT 20
        """,
        params or {},
    )
    by_operation_rows = storage_adapter.execute_query(
        f"""
        SELECT
            operation_name,
            sumMerge(span_count_state) AS count
        FROM logs.obs_traces_1m
        {where_clause}
        GROUP BY operation_name
        ORDER BY count DESC
        LIMIT 20
        """,
        params or {},
    )

    row = trace_rows[0] if trace_rows else {}
    span_count = int(row.get("span_count", 0) or 0)
    trace_count = int(row.get("trace_count", 0) or 0)
    error_trace_count = int(row.get("error_trace_count", 0) or 0)
    by_service = {
        str(row.get("service_name") or "unknown"): int(row.get("total_count", row.get("count", 0)))
        for row in by_service_rows
    }
    by_operation = {
        str(row.get("operation_name") or "unknown"): int(row.get("count", 0))
        for row in by_operation_rows
    }

    if span_count <= 0 and trace_count <= 0 and not by_service and not by_operation:
        return None
    return {
        "span_count": span_count,
        "trace_count": trace_count,
        "error_trace_count": error_trace_count,
        "by_service": by_service,
        "by_operation": by_operation,
    }


def _query_traces_stats_preaggregated_legacy(
    storage_adapter: Any,
    where_clause: str = "",
    params: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    if not _has_preagg_table(storage_adapter, "traces_stats_1m"):
        return None

    span_rows = storage_adapter.execute_query(
        f"SELECT sum(span_count) AS span_count FROM logs.traces_stats_1m {where_clause}",
        params or {},
    )
    by_service_rows = storage_adapter.execute_query(
        f"""
        SELECT
            service_name,
            sum(trace_count) AS count
        FROM logs.traces_stats_1m
        {where_clause}
        GROUP BY service_name
        ORDER BY count DESC
        LIMIT 20
        """,
        params or {},
    )
    by_operation_rows = storage_adapter.execute_query(
        f"""
        SELECT
            operation_name,
            sum(span_count) AS count
        FROM logs.traces_stats_1m
        {where_clause}
        GROUP BY operation_name
        ORDER BY count DESC
        LIMIT 20
        """,
        params or {},
    )

    span_count = int(span_rows[0].get("span_count", 0)) if span_rows else 0
    by_service = {
        str(row.get("service_name") or "unknown"): int(row.get("total_count", row.get("count", 0)))
        for row in by_service_rows
    }
    by_operation = {
        str(row.get("operation_name") or "unknown"): int(row.get("count", 0))
        for row in by_operation_rows
    }

    if span_count <= 0 and not by_service and not by_operation:
        return None
    return {
        "span_count": span_count,
        "by_service": by_service,
        "by_operation": by_operation,
    }


def _query_traces_stats_preaggregated(
    storage_adapter: Any,
    where_clause: str = "",
    params: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    if _should_try_v2_preagg():
        stats_v2 = _query_traces_stats_preaggregated_v2(storage_adapter, where_clause=where_clause, params=params)
        if stats_v2 is not None:
            return stats_v2
    if _should_try_legacy_preagg():
        return _query_traces_stats_preaggregated_legacy(storage_adapter, where_clause=where_clause, params=params)
    return None


def query_logs_stats(storage_adapter: Any, time_window: Optional[str] = None) -> Dict[str, Any]:
    """Get logs statistics summary."""
    safe_window = sanitize_interval(time_window or _QUERY_LOGS_STATS_DEFAULT_TIME_WINDOW, default_value="24 HOUR")
    try:
        preagg_stats = _query_logs_stats_preaggregated(storage_adapter, time_window=safe_window)
        if preagg_stats is not None:
            return preagg_stats
    except Exception:
        # 预聚合路径异常时降级回全表查询，保障兼容性。
        pass

    total_query = f"SELECT COUNT(*) as total FROM logs.logs PREWHERE timestamp > now() - INTERVAL {safe_window}"
    total_result = storage_adapter.execute_query(total_query)
    total = total_result[0]["total"] if total_result else 0

    level_bucket_expr = _build_level_bucket_expr("level")
    service_query = f"""
    SELECT
        service_name,
        COUNT(*) as count,
        countIf({level_bucket_expr} IN ('ERROR', 'FATAL', 'CRITICAL')) as error_count
    FROM logs.logs
    PREWHERE timestamp > now() - INTERVAL {safe_window}
    GROUP BY service_name
    ORDER BY count DESC
    LIMIT 20
    """
    service_results = storage_adapter.execute_query(service_query)
    by_service = {row["service_name"]: row["count"] for row in service_results}
    by_service_errors = {
        str(row.get("service_name") or "unknown"): int(row.get("error_count", 0))
        for row in service_results
    }

    level_query = f"""
    SELECT
        {level_bucket_expr} AS level_bucket,
        COUNT(*) as count
    FROM logs.logs
    PREWHERE timestamp > now() - INTERVAL {safe_window}
    GROUP BY {level_bucket_expr}
    ORDER BY count DESC
    LIMIT {_LOG_LEVEL_BUCKET_LIMIT}
    """
    level_results = storage_adapter.execute_query(level_query)
    by_level = {_row_level_bucket(row): int(row.get("count", 0)) for row in level_results}

    return {
        "total": total,
        "byService": by_service,
        "byServiceErrors": by_service_errors,
        "byLevel": by_level,
    }


def query_metrics(
    storage_adapter: Any,
    limit: int,
    service_name: Optional[str],
    metric_name: Optional[str],
    start_time: Optional[str],
    end_time: Optional[str],
    convert_timestamp_fn: Callable[[Optional[str]], Optional[str]],
) -> Dict[str, Any]:
    """Query metrics with optional filters."""
    prewhere_conditions: List[str] = []
    params: Dict[str, Any] = {"limit": int(limit)}

    if service_name:
        prewhere_conditions.append("service_name = {service_name:String}")
        params["service_name"] = service_name
    if metric_name:
        prewhere_conditions.append("metric_name = {metric_name:String}")
        params["metric_name"] = metric_name
    if start_time:
        ch_start_time = convert_timestamp_fn(start_time)
        prewhere_conditions.append("timestamp >= toDateTime64({start_time:String}, 9, 'UTC')")
        params["start_time"] = ch_start_time
    if end_time:
        ch_end_time = convert_timestamp_fn(end_time)
        prewhere_conditions.append("timestamp <= toDateTime64({end_time:String}, 9, 'UTC')")
        params["end_time"] = ch_end_time
        if not start_time:
            prewhere_conditions.append(
                f"timestamp > toDateTime64({{end_time:String}}, 9, 'UTC') - INTERVAL {_QUERY_METRICS_DEFAULT_TIME_WINDOW}"
            )
    if not start_time and not end_time:
        safe_window = sanitize_interval(_QUERY_METRICS_DEFAULT_TIME_WINDOW, default_value="24 HOUR")
        prewhere_conditions.append(f"timestamp > now() - INTERVAL {safe_window}")

    prewhere_clause = f"PREWHERE {' AND '.join(prewhere_conditions)}" if prewhere_conditions else ""

    query = f"""
    SELECT
        timestamp,
        service_name,
        metric_name,
        value_float64 as value,
        attributes_json as labels
    FROM logs.metrics
    {prewhere_clause}
    ORDER BY timestamp DESC
    LIMIT {{limit:Int32}}
    """

    results = storage_adapter.execute_query(query, params)
    return {
        "data": results,
        "count": len(results),
        "limit": limit,
    }


def query_metrics_stats(storage_adapter: Any, time_window: Optional[str] = None) -> Dict[str, Any]:
    """Get metrics statistics summary."""
    safe_window = sanitize_interval(time_window or _QUERY_METRICS_STATS_DEFAULT_TIME_WINDOW, default_value="24 HOUR")
    try:
        preagg_stats = _query_metrics_stats_preaggregated(storage_adapter, time_window=safe_window)
        if preagg_stats is not None:
            return preagg_stats
    except Exception:
        # 预聚合路径异常时降级回全表查询，保障兼容性。
        pass

    total_query = f"SELECT COUNT(*) as total FROM logs.metrics PREWHERE timestamp > now() - INTERVAL {safe_window}"
    total_result = storage_adapter.execute_query(total_query)
    total = total_result[0]["total"] if total_result else 0

    service_query = f"""
    SELECT
        service_name,
        COUNT(*) as count
    FROM logs.metrics
    PREWHERE timestamp > now() - INTERVAL {safe_window}
    GROUP BY service_name
    ORDER BY count DESC
    LIMIT 20
    """
    service_results = storage_adapter.execute_query(service_query)
    by_service = {row["service_name"]: row["count"] for row in service_results}

    metric_query = f"""
    SELECT
        metric_name,
        COUNT(*) as count
    FROM logs.metrics
    PREWHERE timestamp > now() - INTERVAL {safe_window}
    GROUP BY metric_name
    ORDER BY count DESC
    LIMIT 20
    """
    metric_results = storage_adapter.execute_query(metric_query)
    by_metric = {row["metric_name"]: row["count"] for row in metric_results}

    return {
        "total": total,
        "byService": by_service,
        "byMetricName": by_metric,
    }


def query_traces(
    storage_adapter: Any,
    limit: int,
    service_name: Optional[str],
    trace_id: Optional[str],
    start_time: Optional[str],
    end_time: Optional[str],
    resolve_trace_schema_fn: Callable[[], Dict[str, Optional[str]]],
    build_grouped_trace_duration_expr_fn: Callable[[Dict[str, Optional[str]]], str],
    normalize_trace_status_fn: Callable[[Any], str],
    convert_timestamp_fn: Callable[[Optional[str]], Optional[str]],
    time_window: Optional[str] = None,
    offset: int = 0,
) -> Dict[str, Any]:
    """Query traces grouped by trace_id."""
    schema = resolve_trace_schema_fn()
    time_col = _resolve_safe_trace_time_column(schema)
    if not time_col:
        raise ValueError("logs.traces 表缺少 timestamp/start_time 字段")
    page_limit = max(int(limit), 1)
    page_offset = max(int(offset), 0)
    page_upper_bound = page_limit + page_offset
    safe_window = sanitize_interval(time_window or _QUERY_TRACES_DEFAULT_TIME_WINDOW, default_value="24 HOUR")

    base_conditions: List[str] = []
    detail_conditions: List[str] = []
    filter_params: Dict[str, Any] = {}

    if service_name:
        base_conditions.append("service_name = {service_name:String}")
        detail_conditions.append("t.service_name = {service_name:String}")
        filter_params["service_name"] = service_name
    if trace_id:
        base_conditions.append("trace_id = {trace_id:String}")
        detail_conditions.append("t.trace_id = {trace_id:String}")
        filter_params["trace_id"] = trace_id
    if start_time:
        base_conditions.append(f"{time_col} >= toDateTime64({{start_time:String}}, 9, 'UTC')")
        detail_conditions.append(f"t.{time_col} >= toDateTime64({{start_time:String}}, 9, 'UTC')")
        filter_params["start_time"] = convert_timestamp_fn(start_time)
    if end_time:
        base_conditions.append(f"{time_col} <= toDateTime64({{end_time:String}}, 9, 'UTC')")
        detail_conditions.append(f"t.{time_col} <= toDateTime64({{end_time:String}}, 9, 'UTC')")
        filter_params["end_time"] = convert_timestamp_fn(end_time)
        if not start_time and not trace_id:
            base_conditions.append(
                f"{time_col} > toDateTime64({{end_time:String}}, 9, 'UTC') - INTERVAL {safe_window}"
            )
            detail_conditions.append(
                f"t.{time_col} > toDateTime64({{end_time:String}}, 9, 'UTC') - INTERVAL {safe_window}"
            )
    if not trace_id and not start_time and not end_time:
        base_conditions.append(f"{time_col} > now() - INTERVAL {safe_window}")
        detail_conditions.append(f"t.{time_col} > now() - INTERVAL {safe_window}")
    # 提前过滤空 trace_id，减少 recent_spans 扫描与后续 join 压力。
    base_conditions.append("notEmpty(trace_id)")
    detail_conditions.append("notEmpty(t.trace_id)")

    base_prewhere_clause = f"PREWHERE {' AND '.join(base_conditions)}" if base_conditions else ""
    detail_prewhere_clause = f"PREWHERE {' AND '.join(detail_conditions)}" if detail_conditions else ""
    duration_expr = build_grouped_trace_duration_expr_fn(schema)

    total_query = f"""
    SELECT
        toUInt64(uniqCombined64(trace_id)) AS total
    FROM logs.traces
    {base_prewhere_clause}
    """
    total_result = storage_adapter.execute_query(total_query, filter_params)
    total = int((total_result[0] or {}).get("total", 0)) if total_result else 0

    recent_span_scan_limit = _compute_recent_span_scan_limit(
        limit=page_upper_bound,
        safe_window=safe_window,
        has_trace_id=bool(trace_id),
        has_service_name=bool(service_name),
    )
    params = {
        **filter_params,
        "limit": page_limit,
        "offset": page_offset,
        "page_span_limit": page_upper_bound,
        "recent_span_scan_limit": int(recent_span_scan_limit),
    }

    query = f"""
    WITH recent_spans AS (
        SELECT
            trace_id,
            {time_col} AS trace_ts
        FROM logs.traces
        {base_prewhere_clause}
        ORDER BY trace_ts DESC
        LIMIT {{recent_span_scan_limit:Int32}}
    ),
    recent_trace_ids AS (
        SELECT
            trace_id,
            max(trace_ts) AS last_seen
        FROM recent_spans
        GROUP BY trace_id
        ORDER BY last_seen DESC
        LIMIT {{page_span_limit:Int32}}
    )
    SELECT
        t.trace_id AS trace_id,
        any(t.service_name) AS service_name,
        argMin(t.operation_name, t.{time_col}) AS operation_name,
        toString(min(t.{time_col})) AS start_time_str,
        toUInt64({duration_expr}) AS duration_ms,
        multiIf(
            countIf(toString(t.status) IN ('2', 'STATUS_CODE_ERROR', 'ERROR')) > 0, 'STATUS_CODE_ERROR',
            countIf(toString(t.status) IN ('1', 'STATUS_CODE_OK', 'OK')) > 0, 'STATUS_CODE_OK',
            'STATUS_CODE_UNSET'
        ) AS status
    FROM logs.traces AS t
    ANY INNER JOIN recent_trace_ids AS r ON t.trace_id = r.trace_id
    {detail_prewhere_clause}
    GROUP BY t.trace_id, r.last_seen
    ORDER BY r.last_seen DESC
    LIMIT {{limit:Int32}}
    OFFSET {{offset:Int32}}
    SETTINGS optimize_use_projections = 1, optimize_read_in_order = 1
    """

    results = storage_adapter.execute_query(query, params)
    for row in results:
        row["status"] = normalize_trace_status_fn(row.get("status"))

    return {
        "data": results,
        "count": len(results),
        "limit": page_limit,
        "offset": page_offset,
        "total": total,
        "has_more": bool(page_offset + len(results) < total),
        "next_offset": page_offset + len(results) if page_offset + len(results) < total else None,
    }


def query_trace_spans(
    storage_adapter: Any,
    trace_id: str,
    limit: int,
    resolve_trace_schema_fn: Callable[[], Dict[str, Optional[str]]],
    parse_json_dict_fn: Callable[[Any], Dict[str, Any]],
    extract_duration_ms_fn: Callable[[Dict[str, Any], Dict[str, Any]], float],
    normalize_trace_status_fn: Callable[[Any], str],
) -> List[Dict[str, Any]]:
    """Query spans under one trace id."""
    schema = resolve_trace_schema_fn()
    time_col = _resolve_safe_trace_time_column(schema)
    attrs_col = schema["attrs_col"]
    duration_col = schema["duration_col"]

    if not time_col:
        raise ValueError("logs.traces 表缺少 timestamp/start_time 字段")

    attrs_select = f"{attrs_col} AS attrs_payload" if attrs_col else "'' AS attrs_payload"
    if duration_col:
        duration_col_lower = str(duration_col).lower()
        if duration_col_lower.endswith("_ns"):
            duration_select = f"(toFloat64OrZero(toString({duration_col})) / 1000000.0) AS duration_ms"
        elif duration_col_lower.endswith("_us"):
            duration_select = f"(toFloat64OrZero(toString({duration_col})) / 1000.0) AS duration_ms"
        else:
            duration_select = f"toFloat64OrZero(toString({duration_col})) AS duration_ms"
    else:
        duration_select = "0.0 AS duration_ms"
    query = f"""
    SELECT
        trace_id,
        span_id,
        parent_span_id,
        service_name,
        operation_name,
        toString({time_col}) AS start_time,
        status,
        {duration_select},
        {attrs_select}
    FROM logs.traces
    PREWHERE trace_id = {{trace_id:String}}
    ORDER BY {time_col} ASC
    LIMIT {{limit:Int32}}
    """
    params = {
        "trace_id": trace_id,
        "limit": limit,
    }
    rows = storage_adapter.execute_query(query, params)

    spans: List[Dict[str, Any]] = []
    for row in rows:
        tags = parse_json_dict_fn(row.get("attrs_payload"))
        spans.append(
            {
                "trace_id": row.get("trace_id", ""),
                "span_id": row.get("span_id", ""),
                "parent_span_id": row.get("parent_span_id", ""),
                "service_name": row.get("service_name", "unknown"),
                "operation_name": row.get("operation_name", ""),
                "start_time": row.get("start_time", ""),
                "duration_ms": extract_duration_ms_fn(row, tags),
                "status": normalize_trace_status_fn(row.get("status")),
                "tags": tags,
            }
        )

    if spans:
        all_zero_duration = True
        for span in spans:
            try:
                duration_value = float(span.get("duration_ms") or 0.0)
            except Exception:
                duration_value = 0.0
            if duration_value > 0:
                all_zero_duration = False
                break
        if all_zero_duration:
            _infer_all_zero_span_durations(spans)

    return spans


def query_traces_stats(
    storage_adapter: Any,
    resolve_trace_schema_fn: Callable[[], Dict[str, Optional[str]]],
    build_grouped_trace_duration_expr_fn: Callable[[Dict[str, Optional[str]]], str],
    time_window: Optional[str] = None,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    convert_timestamp_fn: Optional[Callable[[Optional[str]], Optional[str]]] = None,
) -> Dict[str, Any]:
    """Get traces statistics summary."""
    schema = resolve_trace_schema_fn()
    time_col = _resolve_safe_trace_time_column(schema)
    duration_col = schema["duration_col"]
    safe_window = sanitize_interval(time_window or _QUERY_TRACES_STATS_DEFAULT_TIME_WINDOW, default_value="24 HOUR")

    trace_conditions: List[str] = []
    params: Dict[str, Any] = {}
    if start_time:
        trace_conditions.append(f"{time_col} >= toDateTime64({{stats_start:String}}, 9, 'UTC')")
        params["stats_start"] = (
            convert_timestamp_fn(start_time) if convert_timestamp_fn else start_time
        )
    if end_time:
        trace_conditions.append(f"{time_col} <= toDateTime64({{stats_end:String}}, 9, 'UTC')")
        params["stats_end"] = (
            convert_timestamp_fn(end_time) if convert_timestamp_fn else end_time
        )
        if not start_time:
            trace_conditions.append(
                f"{time_col} > toDateTime64({{stats_end:String}}, 9, 'UTC') - INTERVAL {safe_window}"
            )
    if not start_time and not end_time:
        trace_conditions.append(f"{time_col} > now() - INTERVAL {safe_window}")
    trace_prewhere = f"PREWHERE {' AND '.join(trace_conditions)}" if trace_conditions else ""

    preagg_conditions: List[str] = []
    if "stats_start" in params:
        preagg_conditions.append("ts_minute >= toDateTime(parseDateTimeBestEffort({stats_start:String}, 'UTC'))")
    if "stats_end" in params:
        preagg_conditions.append("ts_minute <= toDateTime(parseDateTimeBestEffort({stats_end:String}, 'UTC'))")
        if "stats_start" not in params:
            preagg_conditions.append(
                f"ts_minute > toDateTime(parseDateTimeBestEffort({{stats_end:String}}, 'UTC')) - INTERVAL {safe_window}"
            )
    if not preagg_conditions:
        preagg_conditions.append(f"ts_minute > now() - INTERVAL {safe_window}")
    preagg_where = f"PREWHERE {' AND '.join(preagg_conditions)}" if preagg_conditions else ""

    preagg_stats: Optional[Dict[str, Any]] = None
    try:
        preagg_stats = _query_traces_stats_preaggregated(storage_adapter, where_clause=preagg_where, params=params)
    except Exception:
        # 预聚合异常时保留原有全表统计逻辑。
        preagg_stats = None

    total_traces = 0
    span_count = 0
    error_traces_from_total_query: Optional[int] = None
    by_service: Dict[str, Any] = {}
    by_operation: Dict[str, Any] = {}
    if preagg_stats is not None:
        total_traces = int(preagg_stats.get("trace_count", 0) or 0)
        span_count = int(preagg_stats.get("span_count", 0) or 0)
        by_service = dict(preagg_stats.get("by_service") or {})
        by_operation = dict(preagg_stats.get("by_operation") or {})
    if not total_traces:
        total_query = f"""
        SELECT
            toUInt64(uniqCombined64(trace_id)) AS total_traces,
            count() AS span_count,
            toUInt64(uniqCombined64If(trace_id, toString(status) IN ('2', 'STATUS_CODE_ERROR', 'ERROR'))) AS error_traces
        FROM logs.traces
        {trace_prewhere}
        """
        total_result = storage_adapter.execute_query(total_query, params)
        total_traces = int((total_result[0].get("total_traces") if total_result else 0) or 0)
        if span_count <= 0:
            span_count = int((total_result[0].get("span_count") if total_result else 0) or 0)
        if total_result and isinstance(total_result[0], dict) and "error_traces" in total_result[0]:
            error_traces_from_total_query = int(total_result[0].get("error_traces") or 0)
    if not by_service or not by_operation:
        service_query = f"""
        SELECT
            service_name,
            toUInt64(uniqCombined64(trace_id)) as count
        FROM logs.traces
        {trace_prewhere}
        GROUP BY service_name
        ORDER BY count DESC
        LIMIT 20
        """
        service_results = storage_adapter.execute_query(service_query, params)
        by_service = {row["service_name"]: row["count"] for row in service_results}

        operation_query = f"""
        SELECT
            operation_name,
            COUNT(*) as count
        FROM logs.traces
        {trace_prewhere}
        GROUP BY operation_name
        ORDER BY count DESC
        LIMIT 20
        """
        operation_results = storage_adapter.execute_query(operation_query, params)
        by_operation = {row["operation_name"]: row["count"] for row in operation_results}

    duration_expr = build_grouped_trace_duration_expr_fn(schema)
    duration_params = dict(params)
    duration_params["duration_sample_limit"] = int(_TRACE_STATS_DURATION_SAMPLE_LIMIT)
    duration_scan_limit = int(_TRACE_STATS_DURATION_SAMPLE_LIMIT) * int(_TRACE_STATS_DURATION_SCAN_FACTOR)
    duration_scan_limit = max(duration_scan_limit, int(_TRACE_STATS_DURATION_SAMPLE_LIMIT))
    duration_scan_limit = min(duration_scan_limit, int(_TRACE_STATS_DURATION_SCAN_HARD_LIMIT))
    duration_params["duration_scan_limit"] = int(duration_scan_limit)
    duration_trace_conditions = list(trace_conditions)
    duration_trace_conditions.append("notEmpty(trace_id)")
    duration_trace_prewhere = (
        f"PREWHERE {' AND '.join(duration_trace_conditions)}"
        if duration_trace_conditions
        else "PREWHERE notEmpty(trace_id)"
    )
    if duration_col:
        duration_query = f"""
        SELECT
            avg(trace_duration) AS avg_duration,
            quantile(0.99)(trace_duration) AS p99_duration
        FROM (
            SELECT
                trace_id,
                argMax(trace_duration, trace_ts) AS trace_duration
            FROM (
                SELECT
                    trace_id,
                    toFloat64OrZero(toString({duration_col})) AS trace_duration,
                    {time_col} AS trace_ts
                FROM logs.traces
                {duration_trace_prewhere}
                ORDER BY trace_ts DESC
                LIMIT {{duration_scan_limit:Int32}}
            )
            GROUP BY trace_id
            LIMIT {{duration_sample_limit:Int32}}
        )
        """
    elif time_col:
        duration_query = f"""
        SELECT
            avg(trace_duration) AS avg_duration,
            quantile(0.99)(trace_duration) AS p99_duration
        FROM (
            SELECT
                trace_id,
                {duration_expr} AS trace_duration
            FROM (
                SELECT
                    trace_id,
                    {time_col}
                FROM logs.traces
                {duration_trace_prewhere}
                ORDER BY {time_col} DESC
                LIMIT {{duration_scan_limit:Int32}}
            )
            GROUP BY trace_id
            LIMIT {{duration_sample_limit:Int32}}
        )
        """
    else:
        duration_query = "SELECT 0.0 AS avg_duration, 0.0 AS p99_duration"

    duration_result = storage_adapter.execute_query(duration_query, duration_params)
    avg_duration = _sanitize_json_float(duration_result[0]["avg_duration"], 0.0) if duration_result else 0.0
    p99_duration = _sanitize_json_float(duration_result[0]["p99_duration"], 0.0) if duration_result else 0.0

    preagg_error_traces = int(preagg_stats.get("error_trace_count", 0) or 0) if preagg_stats else 0
    has_preagg_error_totals = bool(
        preagg_stats
        and ("trace_count" in preagg_stats)
        and ("error_trace_count" in preagg_stats)
    )
    if has_preagg_error_totals and total_traces > 0:
        error_traces = preagg_error_traces
        total_for_error = total_traces
    elif error_traces_from_total_query is not None:
        error_traces = int(error_traces_from_total_query)
        total_for_error = int(total_traces)
    else:
        error_rate_query = f"""
        SELECT
            toUInt64(uniqCombined64If(trace_id, toString(status) IN ('2', 'STATUS_CODE_ERROR', 'ERROR'))) AS error_traces,
            toUInt64(uniqCombined64(trace_id)) AS total_traces
        FROM logs.traces
        {trace_prewhere}
        """
        error_rate_result = storage_adapter.execute_query(error_rate_query, params)
        error_traces = int((error_rate_result[0].get("error_traces") if error_rate_result else 0) or 0)
        total_for_error = int((error_rate_result[0].get("total_traces") if error_rate_result else 0) or 0)
    error_rate = _sanitize_json_float((float(error_traces) / float(total_for_error)) if total_for_error else 0.0, 0.0)

    return {
        "total": total_traces,
        "byService": by_service,
        "byOperation": by_operation,
        "spanCount": span_count,
        "avg_duration": round(avg_duration, 2),
        "p99_duration": round(p99_duration, 2),
        "error_rate": round(error_rate, 4),
    }
