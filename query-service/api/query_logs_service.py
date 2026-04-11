"""Logs query domain services extracted from query_routes."""

from __future__ import annotations

import json
import os
import re
import importlib.util
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from fastapi import HTTPException

from api.query_params import sanitize_interval

try:
    from utils.pattern_aggregator import PatternAggregator
except ModuleNotFoundError as exc:
    if exc.name != "utils.pattern_aggregator":
        raise
    _module_path = os.path.join(os.path.dirname(__file__), "..", "utils", "pattern_aggregator.py")
    _spec = importlib.util.spec_from_file_location("query_service_pattern_aggregator", _module_path)
    if not _spec or not _spec.loader:
        raise
    _module = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_module)
    PatternAggregator = _module.PatternAggregator

_QUERY_LOGS_DEFAULT_TIME_WINDOW_ENV = "QUERY_LOGS_DEFAULT_TIME_WINDOW"
_QUERY_LOGS_DEFAULT_TIME_WINDOW = "1 HOUR"
_QUERY_LOGS_MAX_THREADS_ENV = "QUERY_LOGS_MAX_THREADS"
_QUERY_LOGS_MAX_THREADS = 4
_QUERY_LOGS_FACETS_MAX_SERVICES_ENV = "QUERY_LOGS_FACETS_MAX_SERVICES"
_QUERY_LOGS_FACETS_MAX_SERVICES = 200
_QUERY_LOGS_FACETS_MAX_NAMESPACES_ENV = "QUERY_LOGS_FACETS_MAX_NAMESPACES"
_QUERY_LOGS_FACETS_MAX_NAMESPACES = 200
_QUERY_LOGS_FACETS_MAX_LEVELS_ENV = "QUERY_LOGS_FACETS_MAX_LEVELS"
_QUERY_LOGS_FACETS_MAX_LEVELS = 20
_QUERY_LOGS_FACETS_DEGRADE_WINDOW_MINUTES_ENV = "QUERY_LOGS_FACETS_DEGRADE_WINDOW_MINUTES"
_QUERY_LOGS_FACETS_DEGRADE_WINDOW_MINUTES = 7 * 24 * 60
_QUERY_LOGS_HARD_MAX_WINDOW_MINUTES_ENV = "QUERY_LOGS_HARD_MAX_WINDOW_MINUTES"
_QUERY_LOGS_HARD_MAX_WINDOW_MINUTES = 7 * 24 * 60
_QUERY_LOGS_CONTEXT_WINDOWS_ENV = "QUERY_LOGS_CONTEXT_WINDOWS"
_QUERY_LOGS_CONTEXT_WINDOWS_DEFAULT = ("1 HOUR", "6 HOUR", "24 HOUR")


def _resolve_query_logs_default_window() -> str:
    """Resolve bounded default time window for logs queries."""
    raw_default = os.getenv(_QUERY_LOGS_DEFAULT_TIME_WINDOW_ENV, _QUERY_LOGS_DEFAULT_TIME_WINDOW)
    return sanitize_interval(str(raw_default or ""), default_value=_QUERY_LOGS_DEFAULT_TIME_WINDOW)


def _resolve_bounded_int(raw_value: Any, default_value: int, minimum: int, maximum: int) -> int:
    """Resolve integer env/config value with bounds and safe fallback."""
    try:
        value = int(str(raw_value or "").strip())
    except (TypeError, ValueError):
        return default_value
    if value < minimum:
        return minimum
    if value > maximum:
        return maximum
    return value


def _resolve_query_logs_max_threads() -> int:
    """Resolve max_threads used by logs-related SQL."""
    raw_value = os.getenv(_QUERY_LOGS_MAX_THREADS_ENV, str(_QUERY_LOGS_MAX_THREADS))
    return _resolve_bounded_int(raw_value, default_value=_QUERY_LOGS_MAX_THREADS, minimum=1, maximum=16)


def _resolve_query_logs_facets_max_services() -> int:
    """Resolve upper bound for facets service buckets."""
    raw_value = os.getenv(_QUERY_LOGS_FACETS_MAX_SERVICES_ENV, str(_QUERY_LOGS_FACETS_MAX_SERVICES))
    return _resolve_bounded_int(raw_value, default_value=_QUERY_LOGS_FACETS_MAX_SERVICES, minimum=20, maximum=1000)


def _resolve_query_logs_facets_max_levels() -> int:
    """Resolve upper bound for facets level buckets."""
    raw_value = os.getenv(_QUERY_LOGS_FACETS_MAX_LEVELS_ENV, str(_QUERY_LOGS_FACETS_MAX_LEVELS))
    return _resolve_bounded_int(raw_value, default_value=_QUERY_LOGS_FACETS_MAX_LEVELS, minimum=5, maximum=50)


def _resolve_query_logs_facets_max_namespaces() -> int:
    """Resolve upper bound for facets namespace buckets."""
    raw_value = os.getenv(_QUERY_LOGS_FACETS_MAX_NAMESPACES_ENV, str(_QUERY_LOGS_FACETS_MAX_NAMESPACES))
    return _resolve_bounded_int(raw_value, default_value=_QUERY_LOGS_FACETS_MAX_NAMESPACES, minimum=20, maximum=1000)


def _resolve_query_logs_facets_degrade_window_minutes() -> int:
    """Resolve broad-query degrade threshold for facets time window."""
    raw_value = os.getenv(
        _QUERY_LOGS_FACETS_DEGRADE_WINDOW_MINUTES_ENV,
        str(_QUERY_LOGS_FACETS_DEGRADE_WINDOW_MINUTES),
    )
    return _resolve_bounded_int(
        raw_value,
        default_value=_QUERY_LOGS_FACETS_DEGRADE_WINDOW_MINUTES,
        minimum=60,
        maximum=7 * 24 * 60,
    )


def _interval_to_minutes(interval_text: str, default_value: str = "1 HOUR") -> int:
    """Convert sanitized interval text into minutes."""
    safe_interval = sanitize_interval(interval_text, default_value=default_value)
    amount_text, unit = safe_interval.split()
    amount = int(amount_text)
    unit_upper = unit.upper()
    if unit_upper == "MINUTE":
        return amount
    if unit_upper == "HOUR":
        return amount * 60
    if unit_upper == "WEEK":
        return amount * 7 * 24 * 60
    return amount * 24 * 60


def _minutes_to_interval(minutes: int) -> str:
    """Convert minutes into sanitized INTERVAL text."""
    safe_minutes = max(int(minutes), 1)
    if safe_minutes % (7 * 24 * 60) == 0:
        return f"{safe_minutes // (7 * 24 * 60)} WEEK"
    if safe_minutes % (24 * 60) == 0:
        return f"{safe_minutes // (24 * 60)} DAY"
    if safe_minutes % 60 == 0:
        return f"{safe_minutes // 60} HOUR"
    return f"{safe_minutes} MINUTE"


def _resolve_query_logs_hard_max_window_minutes() -> int:
    """Resolve hard cap for relative logs time window."""
    raw_value = os.getenv(
        _QUERY_LOGS_HARD_MAX_WINDOW_MINUTES_ENV,
        str(_QUERY_LOGS_HARD_MAX_WINDOW_MINUTES),
    )
    return _resolve_bounded_int(
        raw_value,
        default_value=_QUERY_LOGS_HARD_MAX_WINDOW_MINUTES,
        minimum=60,
        maximum=7 * 24 * 60,
    )


def _clamp_logs_interval(interval_text: str, default_value: str = "1 HOUR") -> str:
    """Clamp relative interval to hard max to avoid heavy scans."""
    safe_interval = sanitize_interval(interval_text, default_value=default_value)
    interval_minutes = _interval_to_minutes(safe_interval, default_value=default_value)
    max_minutes = _resolve_query_logs_hard_max_window_minutes()
    if interval_minutes <= max_minutes:
        return safe_interval
    return _minutes_to_interval(max_minutes)


def _resolve_query_logs_time_window(
    start_time: Optional[str],
    end_time: Optional[str],
    time_window: Optional[str],
) -> Optional[str]:
    """
    Add a bounded default window when request does not provide any time filter.
    This prevents unbounded scans on logs/logs facets endpoints.
    """
    has_start = bool(str(start_time or "").strip())
    has_end = bool(str(end_time or "").strip())
    has_window = bool(str(time_window or "").strip())
    if has_start or has_end or has_window:
        return time_window

    return _resolve_query_logs_default_window()


def _resolve_query_logs_context_window_minutes() -> List[int]:
    """Resolve progressive context lookup windows (minutes), small to large."""
    raw_value = os.getenv(
        _QUERY_LOGS_CONTEXT_WINDOWS_ENV,
        ",".join(_QUERY_LOGS_CONTEXT_WINDOWS_DEFAULT),
    )
    raw_candidates = [item.strip() for item in str(raw_value or "").split(",") if item.strip()]
    if not raw_candidates:
        raw_candidates = list(_QUERY_LOGS_CONTEXT_WINDOWS_DEFAULT)

    minutes: List[int] = []
    for raw_candidate in raw_candidates:
        safe_interval = _clamp_logs_interval(raw_candidate, default_value="1 HOUR")
        resolved_minutes = _interval_to_minutes(safe_interval, default_value="1 HOUR")
        if resolved_minutes > 0 and resolved_minutes not in minutes:
            minutes.append(resolved_minutes)

    if not minutes:
        return [60, 6 * 60, 24 * 60]
    return sorted(minutes)


def _decode_log_payload_fields(rows: List[Dict[str, Any]]) -> None:
    """Decode labels/attributes_json fields in-place."""
    for row in rows:
        if "labels" in row and isinstance(row["labels"], str):
            try:
                parsed_labels = json.loads(row["labels"]) if row["labels"] else {}
                row["labels"] = parsed_labels if isinstance(parsed_labels, dict) else {}
            except Exception:
                row["labels"] = {}
        elif "labels" in row and not isinstance(row.get("labels"), dict):
            row["labels"] = {}
        if "log_meta" in row and isinstance(row["log_meta"], str):
            try:
                parsed_log_meta = json.loads(row["log_meta"]) if row["log_meta"] else {}
                row["log_meta"] = parsed_log_meta if isinstance(parsed_log_meta, dict) else {}
            except Exception:
                row["log_meta"] = {}
        if "attributes_json" in row and isinstance(row["attributes_json"], str):
            try:
                parsed_attributes = json.loads(row["attributes_json"]) if row["attributes_json"] else {}
                row["attributes"] = parsed_attributes if isinstance(parsed_attributes, dict) else {}
            except Exception:
                row["attributes"] = {}
        elif "attributes_json" in row and isinstance(row["attributes_json"], dict):
            row["attributes"] = row["attributes_json"]
        elif not isinstance(row.get("attributes"), dict):
            row["attributes"] = {}
        if not row.get("log_meta") and isinstance(row.get("attributes"), dict):
            nested_log_meta = row["attributes"].get("log_meta")
            if isinstance(nested_log_meta, dict):
                row["log_meta"] = nested_log_meta


def _to_utc_datetime(value: Any) -> Optional[datetime]:
    """Best-effort parse timestamp-like value into UTC-aware datetime."""
    if value is None:
        return None

    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return None
        if not numeric:
            return None
        absolute_value = abs(numeric)
        if absolute_value >= 1e17:  # nanoseconds
            seconds = numeric / 1_000_000_000
        elif absolute_value >= 1e14:  # microseconds
            seconds = numeric / 1_000_000
        elif absolute_value >= 1e11:  # milliseconds
            seconds = numeric / 1_000
        else:  # seconds
            seconds = numeric
        try:
            dt = datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    else:
        text = str(value).strip()
        if not text:
            return None
        if text.isdigit():
            return _to_utc_datetime(int(text))

        normalized = text
        if " " in normalized and "T" not in normalized:
            normalized = normalized.replace(" ", "T")
        if normalized.endswith("Z"):
            normalized = f"{normalized[:-1]}+00:00"
        if len(normalized) >= 5 and normalized[-5] in {"+", "-"} and normalized[-3] != ":":
            normalized = f"{normalized[:-2]}:{normalized[-2:]}"

        try:
            dt = datetime.fromisoformat(normalized)
        except ValueError:
            return None

    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _format_timestamp_rfc3339_utc(value: Any) -> str:
    """Format value into RFC3339 UTC string with explicit Z suffix."""
    dt = _to_utc_datetime(value)
    if not dt:
        return str(value or "")
    timespec = "microseconds" if dt.microsecond else "seconds"
    return dt.isoformat(timespec=timespec).replace("+00:00", "Z")


def _format_timestamp_for_clickhouse_utc(value: Any) -> Optional[str]:
    """Format timestamp-like value into ClickHouse DateTime64 UTC text."""
    dt = _to_utc_datetime(value)
    if not dt:
        return None
    naive_utc = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return naive_utc.strftime("%Y-%m-%d %H:%M:%S.%f")


def _append_relative_window_lower_bound(
    *,
    conditions: List[str],
    params: Dict[str, Any],
    interval_text: str,
    anchor_time: Optional[str],
    convert_timestamp_fn: Callable[[Optional[str]], Optional[str]],
) -> None:
    """Append lower-bound condition for relative window queries.

    When anchor_time is present, anchor the lower bound to that historical point instead
    of now(), so time_window + anchor_time yields a stable historical slice.
    """
    safe_interval = _clamp_logs_interval(str(interval_text or ""), default_value="1 HOUR")
    normalized_anchor_time = str(anchor_time or "").strip()
    if normalized_anchor_time:
        conditions.append(
            f"timestamp > toDateTime64({{anchor_time:String}}, 9, 'UTC') - INTERVAL {safe_interval}"
        )
        params["anchor_time"] = convert_timestamp_fn(normalized_anchor_time) or normalized_anchor_time
        return
    conditions.append(f"timestamp > now() - INTERVAL {safe_interval}")


def _normalize_log_row_timestamp(row: Dict[str, Any]) -> None:
    """Normalize row timestamp fields for API contract consistency."""
    if not isinstance(row, dict):
        return
    raw_timestamp = row.get("timestamp")
    if raw_timestamp in (None, ""):
        return
    row.setdefault("timestamp_raw", str(raw_timestamp))
    row["timestamp"] = _format_timestamp_rfc3339_utc(raw_timestamp)


def _normalize_log_rows_timestamps(rows: List[Dict[str, Any]]) -> None:
    for row in rows:
        _normalize_log_row_timestamp(row)


def _annotate_edge_candidate_rows(
    rows: List[Dict[str, Any]],
    source_service: Optional[str],
    target_service: Optional[str],
) -> None:
    """Annotate edge-only fallback rows with lightweight explainability fields."""
    source = str(source_service or "").strip().lower()
    target = str(target_service or "").strip().lower()
    if not source or not target:
        return

    for row in rows:
        service = _normalize_service_identity(row.get("service_name"), row.get("pod_name")).lower()
        message_lower = str(row.get("message") or "").lower()
        attrs_lower = str(row.get("attributes_json") or "").lower()
        mention_source = source in message_lower or source in attrs_lower
        mention_target = target in message_lower or target in attrs_lower

        if service == source:
            row["edge_side"] = "source"
        elif service == target:
            row["edge_side"] = "target"
        else:
            row["edge_side"] = "correlated"

        if service == source and mention_target:
            row["edge_match_kind"] = "source_mentions_target"
        elif service == target and mention_source:
            row["edge_match_kind"] = "target_mentions_source"
        elif mention_source and mention_target:
            row["edge_match_kind"] = "dual_text"
        elif service == source:
            row["edge_match_kind"] = "source_service"
        elif service == target:
            row["edge_match_kind"] = "target_service"
        else:
            row["edge_match_kind"] = "correlated_text"


def _append_text_search_filter(
    where_conditions: List[str],
    params: Dict[str, Any],
    search: Optional[str],
) -> None:
    """Append text search filter across message and attributes payload."""
    normalized = str(search or "").strip()
    if not normalized:
        return
    where_conditions.append(
        "("
        "message ILIKE concat('%', {search:String}, '%') "
        "OR attributes_json ILIKE concat('%', {search:String}, '%')"
        ")"
    )
    params["search"] = normalized


_REQUEST_ID_ATTRIBUTE_KEYS = (
    "request_id",
    "request.id",
    "requestId",
    "req_id",
    "x-request-id",
    "x_request_id",
    "http.request_id",
    "trace.request_id",
)


def _append_request_id_filter(
    where_conditions: List[str],
    params: Dict[str, Any],
    request_id: Optional[str],
) -> None:
    """Append exact request_id matching across structured attributes fields."""
    normalized = str(request_id or "").strip()
    if not normalized:
        return

    clauses = [
        f"JSONExtractString(attributes_json, '{key}') = {{request_id:String}}"
        for key in _REQUEST_ID_ATTRIBUTE_KEYS
    ]
    clauses.extend(
        [
            "JSONExtractString(attributes_json, 'request', 'id') = {request_id:String}",
            "JSONExtractString(attributes_json, 'http', 'request_id') = {request_id:String}",
            "JSONExtractString(attributes_json, 'trace', 'request_id') = {request_id:String}",
        ]
    )
    where_conditions.append(f"({' OR '.join(clauses)})")
    params["request_id"] = normalized


def _append_request_id_values_filter(
    where_conditions: List[str],
    params: Dict[str, Any],
    request_ids: List[str],
    param_name: str = "request_ids",
) -> None:
    """Append exact request_id matching for multiple request IDs."""
    normalized_values = [str(value or "").strip() for value in request_ids]
    normalized_values = [value for value in normalized_values if value]
    if not normalized_values:
        return

    unique_values = list(dict.fromkeys(normalized_values))
    clauses = [
        f"JSONExtractString(attributes_json, '{key}') IN {{{param_name}:Array(String)}}"
        for key in _REQUEST_ID_ATTRIBUTE_KEYS
    ]
    clauses.extend(
        [
            f"JSONExtractString(attributes_json, 'request', 'id') IN {{{param_name}:Array(String)}}",
            f"JSONExtractString(attributes_json, 'http', 'request_id') IN {{{param_name}:Array(String)}}",
            f"JSONExtractString(attributes_json, 'trace', 'request_id') IN {{{param_name}:Array(String)}}",
        ]
    )
    where_conditions.append(f"({' OR '.join(clauses)})")
    params[param_name] = unique_values


def _append_trace_id_values_filter(
    conditions: List[str],
    params: Dict[str, Any],
    trace_ids: List[str],
    param_name: str = "trace_ids",
) -> None:
    """Append exact trace_id matching for one or multiple trace IDs."""
    normalized_values = [str(value or "").strip() for value in trace_ids]
    normalized_values = [value for value in normalized_values if value]
    if not normalized_values:
        return

    unique_values = list(dict.fromkeys(normalized_values))
    if len(unique_values) == 1:
        conditions.append("trace_id = {trace_id:String}")
        params["trace_id"] = unique_values[0]
        return

    conditions.append(f"trace_id IN {{{param_name}:Array(String)}}")
    params[param_name] = unique_values


def _normalize_correlation_mode(correlation_mode: Optional[str]) -> str:
    """Normalize correlation mode for trace/request filter composition."""
    mode = str(correlation_mode or "").strip().lower()
    return "or" if mode == "or" else "and"


def _append_trace_request_correlation_filters(
    *,
    prewhere_conditions: List[str],
    where_conditions: List[str],
    params: Dict[str, Any],
    requested_trace_ids: List[str],
    requested_request_ids: List[str],
    request_id: Optional[str],
    request_ids: Optional[List[str]],
    normalize_optional_str_list_fn: Callable[[Any], List[str]],
    correlation_mode: Optional[str],
) -> str:
    """
    Append trace/request correlation filters with configurable boolean semantics.

    - Default (`and`): retain historical behavior, trace in PREWHERE and request in WHERE.
    - `or` with both trace+request present: combine as `(trace_match OR request_match)`.
    """
    normalized_mode = _normalize_correlation_mode(correlation_mode)
    has_trace_filters = len(requested_trace_ids) > 0
    has_request_filters = len(requested_request_ids) > 0
    use_or_mode = normalized_mode == "or" and has_trace_filters and has_request_filters

    if use_or_mode:
        trace_conditions: List[str] = []
        request_conditions: List[str] = []
        _append_trace_id_values_filter(trace_conditions, params, requested_trace_ids)
        _append_request_id_values_filter(request_conditions, params, requested_request_ids)
        if trace_conditions and request_conditions:
            where_conditions.append(f"({trace_conditions[0]} OR {request_conditions[0]})")
        elif trace_conditions:
            where_conditions.append(trace_conditions[0])
        elif request_conditions:
            where_conditions.append(request_conditions[0])
        return normalized_mode

    _append_trace_id_values_filter(prewhere_conditions, params, requested_trace_ids)
    if requested_request_ids:
        if len(requested_request_ids) == 1 and request_id and not normalize_optional_str_list_fn(request_ids):
            _append_request_id_filter(where_conditions, params, requested_request_ids[0])
        else:
            _append_request_id_values_filter(where_conditions, params, requested_request_ids)
    return normalized_mode


def _parse_json_dict(raw: Any) -> Dict[str, Any]:
    """Safely parse JSON dictionary payloads from attributes_json."""
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _extract_request_id_value(attributes_json: Any, message: str = "") -> str:
    """Extract request_id from structured attrs first, then fallback to message text."""
    attrs = _parse_json_dict(attributes_json)
    for key in _REQUEST_ID_ATTRIBUTE_KEYS:
        value = attrs.get(key)
        if value:
            return str(value).strip()

    request_obj = attrs.get("request")
    if isinstance(request_obj, dict) and request_obj.get("id"):
        return str(request_obj.get("id")).strip()
    http_obj = attrs.get("http")
    if isinstance(http_obj, dict) and http_obj.get("request_id"):
        return str(http_obj.get("request_id")).strip()
    trace_obj = attrs.get("trace")
    if isinstance(trace_obj, dict) and trace_obj.get("request_id"):
        return str(trace_obj.get("request_id")).strip()

    text = str(message or "")
    patterns = (
        r"(?:request[_-]?id|x-request-id)\s*[:=]\s*([a-zA-Z0-9\-_.]{6,})",
        r"\b([0-9a-fA-F]{8}-[0-9a-fA-F-]{27,})\b",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1).strip()
    return ""


_LOGS_LIGHT_FIELDS = """
        id,
        timestamp,
        toUnixTimestamp64Nano(timestamp) AS _cursor_ts_ns,
        service_name,
        level,
        message,
        pod_name,
        namespace,
        node_name,
        container_name,
        container_id,
        container_image,
        pod_id,
        trace_id,
        span_id,
        labels,
        JSONExtractRaw(attributes_json, 'log_meta') AS log_meta,
        host_ip
"""


def _normalize_service_identity(
    service_name: Any,
    pod_name: Any,
) -> str:
    """Normalize service identity using the same fallback/suffix rules as SQL filters."""
    service = str(service_name or "").strip()
    pod = str(pod_name or "").strip()
    candidate = service if service and service.lower() != "unknown" else pod
    if not candidate:
        return "unknown"

    if re.match(r"^(.+)-[a-f0-9]{8,10}-[a-z0-9]{5,10}$", candidate):
        candidate = re.sub(r"-[a-f0-9]{8,10}-[a-z0-9]{5,10}$", "", candidate)
    elif re.match(r"^(.+)-[a-f0-9]{8,10}(-[a-f0-9]{4,8})?$", candidate):
        candidate = re.sub(r"-[a-f0-9]{8,10}(-[a-f0-9]{4,8})?$", "", candidate)
    elif re.match(r"^(.+)-[a-z0-9]{5}$", candidate):
        candidate = re.sub(r"-[a-z0-9]{5}$", "", candidate)
    elif re.match(r"^(.+)-\d+$", candidate):
        candidate = re.sub(r"-\d+$", "", candidate)

    normalized = candidate.strip()
    return normalized or "unknown"


def _build_normalized_service_sql(
    *,
    service_column: str = "service_name",
    pod_column: str = "pod_name",
) -> str:
    """
    Build SQL expression for canonical service name.

    优先使用 service_name；当其缺失/unknown 时回退 pod_name。
    并统一清理常见 Kubernetes pod 后缀，避免 facets 出现 pod 名洪泛。
    """
    candidate = (
        f"if(length(trim({service_column})) > 0 AND lowerUTF8(trim({service_column})) != 'unknown', "
        f"trim({service_column}), trim({pod_column}))"
    )
    return (
        "multiIf("
        f"length({candidate}) = 0, 'unknown', "
        f"match({candidate}, '^(.+)-[a-f0-9]{{8,10}}-[a-z0-9]{{5,10}}$'), "
        f"replaceRegexpOne({candidate}, '-[a-f0-9]{{8,10}}-[a-z0-9]{{5,10}}$', ''), "
        f"match({candidate}, '^(.+)-[a-f0-9]{{8,10}}(-[a-f0-9]{{4,8}})?$'), "
        f"replaceRegexpOne({candidate}, '-[a-f0-9]{{8,10}}(-[a-f0-9]{{4,8}})?$', ''), "
        f"match({candidate}, '^(.+)-[a-z0-9]{{5}}$'), "
        f"replaceRegexpOne({candidate}, '-[a-z0-9]{{5}}$', ''), "
        f"match({candidate}, '^(.+)-\\\\d+$'), "
        f"replaceRegexpOne({candidate}, '-\\\\d+$', ''), "
        f"{candidate}"
        ")"
    )


def _append_normalized_service_filter(
    *,
    conditions: List[str],
    params: Dict[str, Any],
    values: List[str],
    param_prefix: str,
    service_column: str = "service_name",
    pod_column: str = "pod_name",
) -> List[str]:
    """Append exact-match filters based on normalized service expression."""
    normalized_values = [str(item).strip() for item in values if str(item).strip()]
    if not normalized_values:
        return []

    service_expr = _build_normalized_service_sql(
        service_column=service_column,
        pod_column=pod_column,
    )
    if len(normalized_values) == 1:
        param_name = param_prefix
        conditions.append(f"{service_expr} = {{{param_name}:String}}")
        params[param_name] = normalized_values[0]
        return normalized_values

    sub_conditions: List[str] = []
    for idx, value in enumerate(normalized_values):
        param_name = f"{param_prefix}_{idx}"
        sub_conditions.append(f"{service_expr} = {{{param_name}:String}}")
        params[param_name] = value
    conditions.append(f"({' OR '.join(sub_conditions)})")
    return normalized_values




def _append_topology_edge_candidate_filter(
    *,
    prewhere_conditions: List[str],
    where_conditions: List[str],
    params: Dict[str, Any],
    source_service: Optional[str],
    target_service: Optional[str],
    source_namespace: Optional[str] = None,
    target_namespace: Optional[str] = None,
) -> bool:
    """Append edge-scoped candidate matching for topology source/target context."""
    source = str(source_service or "").strip()
    target = str(target_service or "").strip()
    if not source or not target or source.lower() == target.lower():
        return False

    params["source_service"] = source
    params["target_service"] = target
    edge_namespaces = sorted({
        str(item or "").strip()
        for item in [source_namespace, target_namespace]
        if str(item or "").strip()
    })
    if edge_namespaces:
        params["edge_namespaces"] = edge_namespaces
        prewhere_conditions.append("namespace IN {edge_namespaces:Array(String)}")
    service_expr = _build_normalized_service_sql(service_column="service_name", pod_column="pod_name")
    normalized_service_expr = f"lowerUTF8({service_expr})"
    source_match = f"{normalized_service_expr} = lowerUTF8({{source_service:String}})"
    target_match = f"{normalized_service_expr} = lowerUTF8({{target_service:String}})"
    prewhere_conditions.append(f"({source_match} OR {target_match})")
    where_conditions.append(
        "("
        f"({source_match} AND (message ILIKE concat('%', {{target_service:String}}, '%') OR attributes_json ILIKE concat('%', {{target_service:String}}, '%'))) "
        "OR "
        f"({target_match} AND (message ILIKE concat('%', {{source_service:String}}, '%') OR attributes_json ILIKE concat('%', {{source_service:String}}, '%'))) "
        "OR "
        "((message ILIKE concat('%', {source_service:String}, '%') OR attributes_json ILIKE concat('%', {source_service:String}, '%')) "
        "AND (message ILIKE concat('%', {target_service:String}, '%') OR attributes_json ILIKE concat('%', {target_service:String}, '%')))"
        ")"
    )
    return True

def query_logs(
    *,
    storage_adapter: Any,
    limit: int,
    service_name: Optional[str],
    service_names: Optional[List[str]],
    trace_id: Optional[str],
    request_id: Optional[str] = None,
    pod_name: Optional[str],
    level: Optional[str],
    levels: Optional[List[str]],
    start_time: Optional[str],
    end_time: Optional[str],
    exclude_health_check: bool,
    search: Optional[str],
    source_service: Optional[str],
    target_service: Optional[str],
    source_namespace: Optional[str] = None,
    target_namespace: Optional[str] = None,
    time_window: Optional[str],
    cursor: Optional[str],
    anchor_time: Optional[str],
    normalize_optional_str_fn: Callable[[Any], Optional[str]],
    normalize_topology_context_fn: Callable[..., Dict[str, Any]],
    normalize_optional_str_list_fn: Callable[[Any], List[str]],
    normalize_level_values_fn: Callable[[Any], List[str]],
    expand_level_match_values_fn: Callable[[List[str]], List[str]],
    append_exact_match_filter_fn: Callable[..., None],
    append_health_check_exclusion_fn: Callable[[List[str], Dict[str, Any]], None],
    convert_timestamp_fn: Callable[[Optional[str]], Optional[str]],
    decode_logs_cursor_fn: Callable[[str], Any],
    encode_logs_cursor_fn: Callable[[Any, Any], str],
    logger: Any,
    namespace: Optional[str] = None,
    namespaces: Optional[List[str]] = None,
    container_name: Optional[str] = None,
    trace_ids: Optional[List[str]] = None,
    request_ids: Optional[List[str]] = None,
    correlation_mode: Optional[str] = None,
) -> Dict[str, Any]:
    """Query logs with filters and keyset pagination."""
    prewhere_conditions: List[str] = []
    where_conditions: List[str] = []
    params: Dict[str, Any] = {}
    normalized_cursor = normalize_optional_str_fn(cursor)
    normalized_anchor_time = normalize_optional_str_fn(anchor_time)
    effective_time_window = _resolve_query_logs_time_window(start_time, end_time, time_window)
    context = normalize_topology_context_fn(
        service_name=service_name,
        search=search,
        start_time=start_time,
        end_time=end_time,
        source_service=source_service,
        target_service=target_service,
        time_window=effective_time_window,
    )

    explicit_service_name = normalize_optional_str_fn(service_name)
    explicit_service_names = normalize_optional_str_list_fn(service_names)
    explicit_search = normalize_optional_str_fn(search)
    normalized_source_service = normalize_optional_str_fn(source_service)
    normalized_target_service = normalize_optional_str_fn(target_service)
    normalized_source_namespace = normalize_optional_str_fn(source_namespace)
    normalized_target_namespace = normalize_optional_str_fn(target_namespace)
    normalized_pod_name = normalize_optional_str_fn(pod_name)
    same_edge_endpoints = bool(
        normalized_source_service
        and normalized_target_service
        and normalized_source_service.lower() == normalized_target_service.lower()
    )
    requested_trace_ids = normalize_optional_str_list_fn([trace_id or "", *(normalize_optional_str_list_fn(trace_ids))])
    requested_request_ids = normalize_optional_str_list_fn([request_id or "", *(normalize_optional_str_list_fn(request_ids))])
    edge_context_active = bool(
        normalized_source_service
        and normalized_target_service
        and not same_edge_endpoints
        and not explicit_service_name
        and not explicit_service_names
        and not explicit_search
        and not requested_trace_ids
        and not requested_request_ids
    )

    effective_service_name = None if edge_context_active else context.get("service_name")
    if same_edge_endpoints and not explicit_service_name and not explicit_service_names:
        # source/target 相同场景降级为单服务过滤，避免 edge 上下文路径失效后扩大扫描范围。
        effective_service_name = normalized_source_service
    requested_service_names = list(explicit_service_names)
    if effective_service_name:
        requested_service_names = normalize_optional_str_list_fn([effective_service_name, *requested_service_names])
    requested_namespaces = normalize_optional_str_list_fn(namespaces)
    if namespace:
        requested_namespaces = normalize_optional_str_list_fn([namespace, *requested_namespaces])
    requested_container_name = normalize_optional_str_fn(container_name)
    effective_search = None if edge_context_active else context.get("search")
    if same_edge_endpoints and not explicit_search:
        effective_search = None
    effective_start_time = context.get("start_time")
    effective_end_time = context.get("end_time")
    fallback_time_window = context.get("time_window") or _resolve_query_logs_default_window()
    requested_levels = normalize_level_values_fn([level or "", *(normalize_optional_str_list_fn(levels))])
    requested_level_match_values = expand_level_match_values_fn(requested_levels)
    effective_correlation_mode = _normalize_correlation_mode(correlation_mode)
    max_threads = _resolve_query_logs_max_threads()

    if edge_context_active:
        _append_topology_edge_candidate_filter(
            prewhere_conditions=prewhere_conditions,
            where_conditions=where_conditions,
            params=params,
            source_service=normalized_source_service,
            target_service=normalized_target_service,
            source_namespace=normalized_source_namespace,
            target_namespace=normalized_target_namespace,
        )
    else:
        _append_normalized_service_filter(
            conditions=prewhere_conditions,
            params=params,
            values=requested_service_names,
            param_prefix="service_name",
        )
    append_exact_match_filter_fn(
        conditions=prewhere_conditions,
        params=params,
        column_name="namespace",
        param_prefix="namespace",
        values=requested_namespaces,
    )
    if normalized_pod_name:
        prewhere_conditions.append("pod_name = {pod_name:String}")
        params["pod_name"] = normalized_pod_name
    if requested_container_name:
        prewhere_conditions.append("container_name = {container_name:String}")
        params["container_name"] = requested_container_name
    append_exact_match_filter_fn(
        conditions=prewhere_conditions,
        params=params,
        column_name="level_norm",
        param_prefix="level",
        values=requested_levels,
    )

    if effective_start_time and str(effective_start_time).startswith("__RELATIVE_INTERVAL__::"):
        relative_interval = str(effective_start_time).split("::", 1)[1]
        _append_relative_window_lower_bound(
            conditions=prewhere_conditions,
            params=params,
            interval_text=relative_interval,
            anchor_time=normalized_anchor_time,
            convert_timestamp_fn=convert_timestamp_fn,
        )
    elif effective_start_time:
        prewhere_conditions.append("timestamp >= toDateTime64({start_time:String}, 9, 'UTC')")
        params["start_time"] = convert_timestamp_fn(effective_start_time)
    if effective_end_time:
        prewhere_conditions.append("timestamp <= toDateTime64({end_time:String}, 9, 'UTC')")
        params["end_time"] = convert_timestamp_fn(effective_end_time)
        if not effective_start_time:
            fallback_time_window = _clamp_logs_interval(str(fallback_time_window), default_value="1 HOUR")
            prewhere_conditions.append(
                f"timestamp > toDateTime64({{end_time:String}}, 9, 'UTC') - INTERVAL {fallback_time_window}"
            )

    if normalized_anchor_time:
        effective_anchor_time = normalized_anchor_time
    elif effective_end_time:
        # 当调用方提供 end_time 时，默认使用 end_time 作为分页锚点，
        # 避免未来时间日志被 now() 锚点误过滤。
        effective_anchor_time = str(effective_end_time)
    else:
        effective_anchor_time = datetime.now(timezone.utc).isoformat()
    prewhere_conditions.append("timestamp <= toDateTime64({anchor_time:String}, 9, 'UTC')")
    params["anchor_time"] = convert_timestamp_fn(effective_anchor_time)

    if normalized_cursor:
        try:
            cursor_timestamp, cursor_id = decode_logs_cursor_fn(normalized_cursor)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"Invalid cursor: {exc}") from exc
        cursor_timestamp_text = str(cursor_timestamp or "").strip()
        # 游标兼容两种格式：
        # 1) 旧格式: 时间字符串（微秒级）
        # 2) 新格式: 纳秒 epoch 整数（避免 DateTime64(9) 边界漏数）
        if cursor_timestamp_text.isdigit():
            prewhere_conditions.append(
                "("
                "toUnixTimestamp64Nano(timestamp) < {cursor_ts_ns:Int64} "
                "OR (toUnixTimestamp64Nano(timestamp) = {cursor_ts_ns:Int64} AND id < {cursor_id:String})"
                ")"
            )
            params["cursor_ts_ns"] = int(cursor_timestamp_text)
        else:
            prewhere_conditions.append(
                "("
                "timestamp < toDateTime64({cursor_timestamp:String}, 9, 'UTC') "
                "OR (timestamp = toDateTime64({cursor_timestamp:String}, 9, 'UTC') AND id < {cursor_id:String})"
                ")"
            )
            params["cursor_timestamp"] = convert_timestamp_fn(cursor_timestamp_text)
        params["cursor_id"] = cursor_id

    if exclude_health_check:
        append_health_check_exclusion_fn(where_conditions, params)

    effective_correlation_mode = _append_trace_request_correlation_filters(
        prewhere_conditions=prewhere_conditions,
        where_conditions=where_conditions,
        params=params,
        requested_trace_ids=requested_trace_ids,
        requested_request_ids=requested_request_ids,
        request_id=request_id,
        request_ids=request_ids,
        normalize_optional_str_list_fn=normalize_optional_str_list_fn,
        correlation_mode=effective_correlation_mode,
    )
    _append_text_search_filter(where_conditions, params, effective_search)

    prewhere_clause = f"PREWHERE {' AND '.join(prewhere_conditions)}" if prewhere_conditions else ""
    where_clause = f"WHERE {' AND '.join(where_conditions)}" if where_conditions else ""
    query = f"""
    SELECT
{_LOGS_LIGHT_FIELDS}
    FROM logs.logs
    {prewhere_clause}
    {where_clause}
    ORDER BY timestamp DESC, id DESC
    LIMIT {{limit_plus_one:Int32}}
    SETTINGS optimize_use_projections = 1, optimize_read_in_order = 1, max_threads = {{max_threads:Int32}}
    """
    params["limit_plus_one"] = limit + 1
    params["max_threads"] = max_threads

    results = storage_adapter.execute_query(query, params)
    has_more = len(results) > limit
    page_results = results[:limit]
    if edge_context_active:
        _annotate_edge_candidate_rows(page_results, normalized_source_service, normalized_target_service)
    _decode_log_payload_fields(page_results)

    next_cursor = None
    if has_more and page_results:
        last_row = page_results[-1]
        next_cursor = encode_logs_cursor_fn(last_row.get("_cursor_ts_ns"), last_row.get("id"))
    for row in page_results:
        row.pop("_cursor_ts_ns", None)
    _normalize_log_rows_timestamps(page_results)

    logger.info(
        "Query executed",
        extra={
            "query_type": "logs",
            "limit": limit,
            "cursor": bool(normalized_cursor),
            "has_more": has_more,
            "filters": {
                "service_name": effective_service_name,
                "service_names": requested_service_names,
                "namespace": namespace,
                "namespaces": requested_namespaces,
                "trace_id": trace_id,
                "trace_ids": requested_trace_ids,
                "request_id": request_id,
                "request_ids": requested_request_ids,
                "correlation_mode": effective_correlation_mode,
                "pod_name": normalized_pod_name,
                "container_name": requested_container_name,
                "level": level,
                "levels": requested_levels,
                "level_match_values": requested_level_match_values,
                "exclude_health_check": exclude_health_check,
                "search": effective_search,
                "source_service": context.get("source_service"),
                "target_service": context.get("target_service"),
            "source_namespace": normalized_source_namespace,
            "target_namespace": normalized_target_namespace,
                "time_window": context.get("time_window"),
                "anchor_time": effective_anchor_time,
            },
            "result_count": len(page_results),
        },
    )

    return {
        "data": page_results,
        "count": len(page_results),
        "limit": limit,
        "has_more": has_more,
        "next_cursor": next_cursor,
        "anchor_time": _format_timestamp_rfc3339_utc(effective_anchor_time),
        "context": {
            "source_service": context.get("source_service"),
            "target_service": context.get("target_service"),
            "time_window": context.get("time_window"),
            "effective_service_name": effective_service_name,
            "effective_service_names": requested_service_names,
            "edge_context_active": edge_context_active,
            "effective_namespaces": requested_namespaces,
            "effective_container_name": requested_container_name,
            "effective_search": effective_search,
            "effective_trace_id": str(trace_id or "").strip() or None,
            "effective_trace_ids": requested_trace_ids,
            "effective_request_id": str(request_id or "").strip() or None,
            "effective_request_ids": requested_request_ids,
            "effective_correlation_mode": effective_correlation_mode,
            "effective_levels": requested_levels,
            "effective_level_match_values": requested_level_match_values,
            "cursor": normalized_cursor,
        },
    }


def query_logs_facets(
    *,
    storage_adapter: Any,
    service_name: Optional[str],
    service_names: Optional[List[str]],
    trace_id: Optional[str],
    request_id: Optional[str] = None,
    pod_name: Optional[str],
    level: Optional[str],
    levels: Optional[List[str]],
    start_time: Optional[str],
    end_time: Optional[str],
    exclude_health_check: bool,
    search: Optional[str],
    source_service: Optional[str],
    target_service: Optional[str],
    time_window: Optional[str],
    limit_services: int,
    limit_levels: int,
    normalize_topology_context_fn: Callable[..., Dict[str, Any]],
    normalize_optional_str_list_fn: Callable[[Any], List[str]],
    normalize_level_values_fn: Callable[[Any], List[str]],
    expand_level_match_values_fn: Callable[[List[str]], List[str]],
    append_exact_match_filter_fn: Callable[..., None],
    append_health_check_exclusion_fn: Callable[[List[str], Dict[str, Any]], None],
    convert_timestamp_fn: Callable[[Optional[str]], Optional[str]],
    namespace: Optional[str] = None,
    namespaces: Optional[List[str]] = None,
    container_name: Optional[str] = None,
    anchor_time: Optional[str] = None,
    limit_namespaces: int = 200,
    trace_ids: Optional[List[str]] = None,
    request_ids: Optional[List[str]] = None,
    correlation_mode: Optional[str] = None,
) -> Dict[str, Any]:
    """Return facets for logs filters."""
    effective_time_window = _resolve_query_logs_time_window(start_time, end_time, time_window)
    context = normalize_topology_context_fn(
        service_name=service_name,
        search=search,
        start_time=start_time,
        end_time=end_time,
        source_service=source_service,
        target_service=target_service,
        time_window=effective_time_window,
    )

    explicit_service_name = str(service_name or "").strip() or None
    explicit_service_names = normalize_optional_str_list_fn(service_names)
    explicit_search = str(search or "").strip() or None
    normalized_source_service = str(source_service or "").strip() or None
    normalized_target_service = str(target_service or "").strip() or None
    same_edge_endpoints = bool(
        normalized_source_service
        and normalized_target_service
        and normalized_source_service.lower() == normalized_target_service.lower()
    )
    requested_trace_ids = normalize_optional_str_list_fn([trace_id or "", *(normalize_optional_str_list_fn(trace_ids))])
    requested_request_ids = normalize_optional_str_list_fn([request_id or "", *(normalize_optional_str_list_fn(request_ids))])
    edge_context_active = bool(
        normalized_source_service
        and normalized_target_service
        and not same_edge_endpoints
        and not explicit_service_name
        and not explicit_service_names
        and not explicit_search
        and not requested_trace_ids
        and not requested_request_ids
    )

    effective_service_name = None if edge_context_active else context.get("service_name")
    if same_edge_endpoints and not explicit_service_name and not explicit_service_names:
        effective_service_name = normalized_source_service
    requested_service_names = list(explicit_service_names)
    if effective_service_name:
        requested_service_names = normalize_optional_str_list_fn([effective_service_name, *requested_service_names])
    requested_namespaces = normalize_optional_str_list_fn(namespaces)
    if namespace:
        requested_namespaces = normalize_optional_str_list_fn([namespace, *requested_namespaces])
    requested_levels = normalize_level_values_fn([level or "", *(normalize_optional_str_list_fn(levels))])
    normalized_pod_name = str(pod_name or "").strip() or None
    requested_container_name = str(container_name or "").strip() or None
    normalized_anchor_time = str(anchor_time or "").strip() or None
    requested_level_match_values = expand_level_match_values_fn(requested_levels)
    effective_correlation_mode = _normalize_correlation_mode(correlation_mode)
    effective_search = None if edge_context_active else context.get("search")
    if same_edge_endpoints and not explicit_search:
        effective_search = None
    effective_start_time = context.get("start_time")
    effective_end_time = context.get("end_time")
    fallback_time_window = context.get("time_window") or _resolve_query_logs_default_window()
    max_threads = _resolve_query_logs_max_threads()

    requested_limit_services = _resolve_bounded_int(
        limit_services,
        default_value=_QUERY_LOGS_FACETS_MAX_SERVICES,
        minimum=1,
        maximum=5000,
    )
    requested_limit_namespaces = _resolve_bounded_int(
        limit_namespaces,
        default_value=_QUERY_LOGS_FACETS_MAX_NAMESPACES,
        minimum=1,
        maximum=5000,
    )
    requested_limit_levels = _resolve_bounded_int(
        limit_levels,
        default_value=_QUERY_LOGS_FACETS_MAX_LEVELS,
        minimum=1,
        maximum=200,
    )
    applied_limit_services = min(requested_limit_services, _resolve_query_logs_facets_max_services())
    applied_limit_namespaces = min(requested_limit_namespaces, _resolve_query_logs_facets_max_namespaces())
    applied_limit_levels = min(requested_limit_levels, _resolve_query_logs_facets_max_levels())

    degrade_reasons: List[str] = []
    if applied_limit_services < requested_limit_services:
        degrade_reasons.append("services_limit_clamped")
    if applied_limit_namespaces < requested_limit_namespaces:
        degrade_reasons.append("namespaces_limit_clamped")
    if applied_limit_levels < requested_limit_levels:
        degrade_reasons.append("levels_limit_clamped")

    broad_query = not any(
        [
            requested_trace_ids,
            requested_request_ids,
            normalized_pod_name,
            requested_container_name,
            effective_search,
            requested_service_names,
            requested_namespaces,
            requested_levels,
            edge_context_active,
        ]
    )
    context_window = context.get("time_window")
    if broad_query and context_window:
        clamped_context_window = _clamp_logs_interval(str(context_window), default_value="1 HOUR")
        window_minutes = _interval_to_minutes(clamped_context_window, default_value="1 HOUR")
        degrade_window_minutes = _resolve_query_logs_facets_degrade_window_minutes()
        if window_minutes > degrade_window_minutes:
            degrade_reasons.append("broad_window_exceeded")
            return {
                "services": [],
                "namespaces": [],
                "levels": [],
                "context": {
                    "source_service": context.get("source_service"),
                    "target_service": context.get("target_service"),
                    "time_window": clamped_context_window,
                    "effective_service_name": effective_service_name,
                    "effective_service_names": requested_service_names,
                    "effective_namespaces": requested_namespaces,
                    "effective_container_name": requested_container_name,
                    "effective_search": effective_search,
                    "anchor_time": _format_timestamp_rfc3339_utc(normalized_anchor_time),
                    "effective_trace_id": str(trace_id or "").strip() or None,
                    "effective_trace_ids": requested_trace_ids,
                    "effective_request_id": str(request_id or "").strip() or None,
                    "effective_request_ids": requested_request_ids,
                    "effective_correlation_mode": effective_correlation_mode,
                    "effective_levels": requested_levels,
                    "effective_level_match_values": requested_level_match_values,
                    "facets_degraded": True,
                    "facets_degrade_reasons": degrade_reasons,
                    "facet_limit_requested": {
                        "services": requested_limit_services,
                        "namespaces": requested_limit_namespaces,
                        "levels": requested_limit_levels,
                    },
                    "facet_limit_applied": {
                        "services": applied_limit_services,
                        "namespaces": applied_limit_namespaces,
                        "levels": applied_limit_levels,
                    },
                    "degrade_window_minutes": degrade_window_minutes,
                    "effective_window_minutes": window_minutes,
                },
                "generated_at": datetime.now(timezone.utc).isoformat(),
            }

    base_prewhere_conditions: List[str] = []
    base_where_conditions: List[str] = []
    base_params: Dict[str, Any] = {}
    if normalized_pod_name:
        base_prewhere_conditions.append("pod_name = {pod_name:String}")
        base_params["pod_name"] = normalized_pod_name
    if requested_container_name:
        base_prewhere_conditions.append("container_name = {container_name:String}")
        base_params["container_name"] = requested_container_name
    if effective_start_time and str(effective_start_time).startswith("__RELATIVE_INTERVAL__::"):
        relative_interval = str(effective_start_time).split("::", 1)[1]
        _append_relative_window_lower_bound(
            conditions=base_prewhere_conditions,
            params=base_params,
            interval_text=relative_interval,
            anchor_time=normalized_anchor_time,
            convert_timestamp_fn=convert_timestamp_fn,
        )
    elif effective_start_time:
        base_prewhere_conditions.append("timestamp >= toDateTime64({start_time:String}, 9, 'UTC')")
        base_params["start_time"] = convert_timestamp_fn(effective_start_time)
    if effective_end_time:
        base_prewhere_conditions.append("timestamp <= toDateTime64({end_time:String}, 9, 'UTC')")
        base_params["end_time"] = convert_timestamp_fn(effective_end_time)
        if not effective_start_time:
            fallback_time_window = _clamp_logs_interval(str(fallback_time_window), default_value="1 HOUR")
            base_prewhere_conditions.append(
                f"timestamp > toDateTime64({{end_time:String}}, 9, 'UTC') - INTERVAL {fallback_time_window}"
            )
    if normalized_anchor_time:
        base_prewhere_conditions.append("timestamp <= toDateTime64({anchor_time:String}, 9, 'UTC')")
        base_params["anchor_time"] = convert_timestamp_fn(normalized_anchor_time)
    if exclude_health_check:
        append_health_check_exclusion_fn(base_where_conditions, base_params)
    effective_correlation_mode = _append_trace_request_correlation_filters(
        prewhere_conditions=base_prewhere_conditions,
        where_conditions=base_where_conditions,
        params=base_params,
        requested_trace_ids=requested_trace_ids,
        requested_request_ids=requested_request_ids,
        request_id=request_id,
        request_ids=request_ids,
        normalize_optional_str_list_fn=normalize_optional_str_list_fn,
        correlation_mode=effective_correlation_mode,
    )
    if edge_context_active:
        _append_topology_edge_candidate_filter(
            prewhere_conditions=base_prewhere_conditions,
            where_conditions=base_where_conditions,
            params=base_params,
            source_service=normalized_source_service,
            target_service=normalized_target_service,
        )
    _append_text_search_filter(base_where_conditions, base_params, effective_search)

    service_prewhere_conditions = list(base_prewhere_conditions)
    service_where_conditions = list(base_where_conditions)
    service_params = dict(base_params)
    append_exact_match_filter_fn(
        conditions=service_prewhere_conditions,
        params=service_params,
        column_name="level_norm",
        param_prefix="facet_level",
        values=requested_levels,
    )
    append_exact_match_filter_fn(
        conditions=service_prewhere_conditions,
        params=service_params,
        column_name="namespace",
        param_prefix="facet_namespace",
        values=requested_namespaces,
    )
    service_prewhere = (
        f"PREWHERE {' AND '.join(service_prewhere_conditions)}"
        if service_prewhere_conditions
        else ""
    )
    service_where = f"WHERE {' AND '.join(service_where_conditions)}" if service_where_conditions else ""
    service_value_expr = _build_normalized_service_sql(service_column="service_name", pod_column="pod_name")
    service_query = f"""
    SELECT
        {service_value_expr} AS value,
        count() AS count
    FROM logs.logs
    {service_prewhere}
    {service_where}
    GROUP BY value
    ORDER BY count DESC, value ASC
    LIMIT {{limit_services:Int32}}
    SETTINGS optimize_use_projections = 1, max_threads = {{max_threads:Int32}}
    """
    service_params["limit_services"] = applied_limit_services
    service_params["max_threads"] = max_threads
    service_rows = storage_adapter.execute_query(service_query, service_params)

    level_prewhere_conditions = list(base_prewhere_conditions)
    level_where_conditions = list(base_where_conditions)
    level_params = dict(base_params)
    if not edge_context_active:
        _append_normalized_service_filter(
            conditions=level_prewhere_conditions,
            params=level_params,
            values=requested_service_names,
            param_prefix="facet_service",
            service_column="service_name",
            pod_column="pod_name",
        )
    append_exact_match_filter_fn(
        conditions=level_prewhere_conditions,
        params=level_params,
        column_name="namespace",
        param_prefix="facet_namespace_for_level",
        values=requested_namespaces,
    )
    level_prewhere = (
        f"PREWHERE {' AND '.join(level_prewhere_conditions)}"
        if level_prewhere_conditions
        else ""
    )
    level_where = f"WHERE {' AND '.join(level_where_conditions)}" if level_where_conditions else ""
    level_query = f"""
    SELECT
        level_norm AS value,
        count() AS count
    FROM logs.logs
    {level_prewhere}
    {level_where}
    GROUP BY value
    ORDER BY count DESC, value ASC
    LIMIT {{limit_levels:Int32}}
    SETTINGS optimize_use_projections = 1, max_threads = {{max_threads:Int32}}
    """
    level_params["limit_levels"] = applied_limit_levels
    level_params["max_threads"] = max_threads
    level_rows = storage_adapter.execute_query(level_query, level_params)

    namespace_prewhere_conditions = list(base_prewhere_conditions)
    namespace_where_conditions = list(base_where_conditions)
    namespace_params = dict(base_params)
    if not edge_context_active:
        _append_normalized_service_filter(
            conditions=namespace_prewhere_conditions,
            params=namespace_params,
            values=requested_service_names,
            param_prefix="facet_service_for_namespace",
            service_column="service_name",
            pod_column="pod_name",
        )
    append_exact_match_filter_fn(
        conditions=namespace_prewhere_conditions,
        params=namespace_params,
        column_name="level_norm",
        param_prefix="facet_level_for_namespace",
        values=requested_levels,
    )
    namespace_prewhere = (
        f"PREWHERE {' AND '.join(namespace_prewhere_conditions)}"
        if namespace_prewhere_conditions
        else ""
    )
    namespace_where = (
        f"WHERE {' AND '.join(namespace_where_conditions)}"
        if namespace_where_conditions
        else ""
    )
    namespace_value_expr = "if(length(trim(namespace)) > 0, trim(namespace), 'unknown')"
    namespace_query = f"""
    SELECT
        {namespace_value_expr} AS value,
        count() AS count
    FROM logs.logs
    {namespace_prewhere}
    {namespace_where}
    GROUP BY value
    ORDER BY count DESC, value ASC
    LIMIT {{limit_namespaces:Int32}}
    SETTINGS optimize_use_projections = 1, max_threads = {{max_threads:Int32}}
    """
    namespace_params["limit_namespaces"] = applied_limit_namespaces
    namespace_params["max_threads"] = max_threads
    namespace_rows = storage_adapter.execute_query(namespace_query, namespace_params)

    merged_level_counts: Dict[str, int] = {}
    for row in level_rows:
        value = str(row.get("value") or "").upper()
        if value == "WARNING":
            value = "WARN"
        if not value:
            continue
        merged_level_counts[value] = merged_level_counts.get(value, 0) + int(row.get("count") or 0)

    level_buckets = [
        {"value": value, "count": count}
        for value, count in sorted(
            merged_level_counts.items(),
            key=lambda item: (-item[1], item[0]),
        )
    ][:applied_limit_levels]

    service_buckets = [
        {
            "value": str(row.get("value") or ""),
            "count": int(row.get("count") or 0),
        }
        for row in service_rows
        if str(row.get("value") or "").strip()
    ]
    namespace_buckets = [
        {
            "value": str(row.get("value") or "").strip(),
            "count": int(row.get("count") or 0),
        }
        for row in namespace_rows
        if str(row.get("value") or "").strip()
    ]

    return {
        "services": service_buckets,
        "namespaces": namespace_buckets,
        "levels": level_buckets,
        "context": {
            "source_service": context.get("source_service"),
            "target_service": context.get("target_service"),
            "time_window": context.get("time_window"),
            "effective_service_name": effective_service_name,
            "effective_service_names": requested_service_names,
            "effective_namespaces": requested_namespaces,
            "effective_container_name": requested_container_name,
            "effective_search": effective_search,
            "anchor_time": _format_timestamp_rfc3339_utc(normalized_anchor_time),
            "effective_trace_id": str(trace_id or "").strip() or None,
            "effective_trace_ids": requested_trace_ids,
            "effective_request_id": str(request_id or "").strip() or None,
            "effective_request_ids": requested_request_ids,
            "effective_correlation_mode": effective_correlation_mode,
            "effective_levels": requested_levels,
            "effective_level_match_values": requested_level_match_values,
            "facets_degraded": bool(degrade_reasons),
            "facets_degrade_reasons": degrade_reasons,
            "facet_limit_requested": {
                "services": requested_limit_services,
                "namespaces": requested_limit_namespaces,
                "levels": requested_limit_levels,
            },
            "facet_limit_applied": {
                "services": applied_limit_services,
                "namespaces": applied_limit_namespaces,
                "levels": applied_limit_levels,
            },
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def query_topology_edge_logs_preview(
    *,
    storage_adapter: Any,
    source_service: str,
    target_service: str,
    time_window: str,
    limit: int,
    exclude_health_check: bool,
    namespace: Optional[str] = None,
    source_namespace: Optional[str] = None,
    target_namespace: Optional[str] = None,
    anchor_time: Optional[str] = None,
    sanitize_interval_fn: Callable[[str, str], str],
    append_health_check_exclusion_fn: Callable[[List[str], Dict[str, Any]], None],
    convert_timestamp_fn: Callable[[Optional[str]], Optional[str]],
    to_datetime_fn: Callable[[Any], datetime],
) -> Dict[str, Any]:
    """Query logs preview for a topology edge."""
    source = str(source_service or "").strip()
    target = str(target_service or "").strip()
    if not source or not target:
        raise HTTPException(status_code=400, detail="source_service and target_service are required")

    safe_window = sanitize_interval_fn(time_window, default_value="1 HOUR")
    normalized_namespace = str(namespace or "").strip() or None
    normalized_source_namespace = str(source_namespace or "").strip() or None
    normalized_target_namespace = str(target_namespace or "").strip() or None
    normalized_anchor_time = str(anchor_time or "").strip() or None
    source_lower = source.lower()
    target_lower = target.lower()
    edge_namespaces = sorted({
        item for item in [normalized_source_namespace, normalized_target_namespace]
        if item
    })
    base_params: Dict[str, Any] = {
        "source_service": source,
        "target_service": target,
        "max_threads": _resolve_query_logs_max_threads(),
    }

    requested_namespace_scoped = bool(normalized_namespace or edge_namespaces)
    degrade_reasons: List[str] = []
    effective_exclude_health_check = exclude_health_check
    namespace_scope_relaxed = False

    def _build_base_time_filters(
        prewhere_conditions: List[str],
        query_params: Dict[str, Any],
        *,
        apply_namespace_scope: bool = True,
    ) -> None:
        if normalized_anchor_time:
            prewhere_conditions.append("timestamp <= toDateTime64({anchor_time:String}, 9, 'UTC')")
            query_params["anchor_time"] = convert_timestamp_fn(normalized_anchor_time) or normalized_anchor_time
            prewhere_conditions.append(
                f"timestamp > toDateTime64({{anchor_time:String}}, 9, 'UTC') - INTERVAL {safe_window}"
            )
        else:
            prewhere_conditions.append(f"timestamp > now() - INTERVAL {safe_window}")

        if apply_namespace_scope and normalized_namespace:
            prewhere_conditions.append("namespace = {namespace:String}")
            query_params["namespace"] = normalized_namespace
        elif apply_namespace_scope and edge_namespaces:
            prewhere_conditions.append("namespace IN {edge_namespaces:Array(String)}")
            query_params["edge_namespaces"] = edge_namespaces
        else:
            service_expr = _build_normalized_service_sql(service_column="service_name", pod_column="pod_name")
            normalized_service_expr = f"lowerUTF8({service_expr})"
            prewhere_conditions.append(
                f"({normalized_service_expr} = lowerUTF8({{source_service:String}}) OR {normalized_service_expr} = lowerUTF8({{target_service:String}}))"
            )

    def _run_preview_query(
        *,
        prewhere_conditions: List[str],
        where_conditions: List[str],
        query_params: Dict[str, Any],
        query_limit: int,
    ) -> List[Dict[str, Any]]:
        final_params = {**base_params, **query_params}
        final_params["query_limit"] = query_limit
        prewhere_clause = "PREWHERE " + " AND ".join(prewhere_conditions)
        where_clause = f"WHERE {' AND '.join(where_conditions)}" if where_conditions else ""
        query = f"""
        SELECT
            id,
            timestamp,
            service_name,
            level,
            message,
            pod_name,
            namespace,
            node_name,
            container_name,
            container_id,
            container_image,
            pod_id,
            trace_id,
            span_id,
            labels,
            attributes_json,
            host_ip
        FROM logs.logs
        {prewhere_clause}
        {where_clause}
        ORDER BY timestamp DESC
        LIMIT {{query_limit:Int32}}
        SETTINGS optimize_use_projections = 1, optimize_read_in_order = 1, max_threads = {{max_threads:Int32}}
        """
        return storage_adapter.execute_query(query, final_params)

    def _score_row(
        row: Dict[str, Any],
        *,
        seed_trace_ids: Optional[set[str]] = None,
        seed_request_ids: Optional[set[str]] = None,
        seed_ids: Optional[set[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        message = str(row.get("message") or "")
        attrs_text = str(row.get("attributes_json") or "")
        service = _normalize_service_identity(row.get("service_name"), row.get("pod_name")).lower()
        level = str(row.get("level") or "").upper()
        trace_id = str(row.get("trace_id") or "").strip()
        request_id = _extract_request_id_value(row.get("attributes_json"), message)
        message_lower = message.lower()
        attrs_lower = attrs_text.lower()
        mention_source = source_lower in message_lower or source_lower in attrs_lower
        mention_target = target_lower in message_lower or target_lower in attrs_lower

        correlated_by_trace = bool(seed_trace_ids and trace_id and trace_id in seed_trace_ids)
        correlated_by_request = bool(seed_request_ids and request_id and request_id in seed_request_ids)
        is_seed = bool(seed_ids and str(row.get("id") or "") in seed_ids)

        score = 0
        if service == source_lower:
            score += 1
        if service == target_lower:
            score += 1
        if mention_source:
            score += 1
        if mention_target:
            score += 1
        if service == source_lower and mention_target:
            score += 5
        if service == target_lower and mention_source:
            score += 5
        if mention_source and mention_target:
            score += 3
        if trace_id:
            score += 2
        if request_id:
            score += 2
        if correlated_by_trace:
            score += 4
        if correlated_by_request:
            score += 4
        if is_seed:
            score += 3
        if level in {"ERROR", "FATAL"}:
            score += 2
        elif level in {"WARN", "WARNING"}:
            score += 1
        if any(token in message_lower for token in ("timeout", "exception", "failed", "error", "超时", "失败")):
            score += 2

        if score <= 0:
            return None

        scored = dict(row)
        edge_side = "source" if service == source_lower else ("target" if service == target_lower else "correlated")
        if service == source_lower and mention_target:
            edge_match_kind = "source_mentions_target"
        elif service == target_lower and mention_source:
            edge_match_kind = "target_mentions_source"
        elif mention_source and mention_target:
            edge_match_kind = "dual_text"
        elif service == source_lower:
            edge_match_kind = "source_service"
        elif service == target_lower:
            edge_match_kind = "target_service"
        else:
            edge_match_kind = "correlated_text"
        scored["edge_match_score"] = score
        scored["edge_side"] = edge_side
        scored["edge_match_kind"] = edge_match_kind
        scored["edge_side_priority"] = 2 if edge_side == "source" else (1 if edge_side == "target" else 0)
        if request_id:
            scored["correlation_request_id"] = request_id
        if correlated_by_trace:
            scored["correlation_trace_id"] = trace_id
        scored["correlation_kind"] = "seed" if is_seed else ("expanded" if (correlated_by_trace or correlated_by_request) else "candidate")
        return scored

    seed_service_expr = _build_normalized_service_sql(service_column="service_name", pod_column="pod_name")
    normalized_seed_service_expr = f"lowerUTF8({seed_service_expr})"
    seed_match_clause = (
        "("
        f"{normalized_seed_service_expr} = lowerUTF8({{source_service:String}}) "
        "OR "
        f"{normalized_seed_service_expr} = lowerUTF8({{target_service:String}}) "
        "OR message ILIKE concat('%', {source_service:String}, '%') "
        "OR message ILIKE concat('%', {target_service:String}, '%') "
        "OR attributes_json ILIKE concat('%', {source_service:String}, '%') "
        "OR attributes_json ILIKE concat('%', {target_service:String}, '%')"
        ")"
    )

    def _query_seed_rows(*, apply_namespace_scope: bool, apply_health_check_exclusion: bool) -> List[Dict[str, Any]]:
        seed_prewhere: List[str] = []
        seed_where: List[str] = []
        seed_params: Dict[str, Any] = {}
        _build_base_time_filters(
            seed_prewhere,
            seed_params,
            apply_namespace_scope=apply_namespace_scope,
        )
        if apply_health_check_exclusion:
            append_health_check_exclusion_fn(seed_where, seed_params)
        seed_where.append(seed_match_clause)
        return _run_preview_query(
            prewhere_conditions=seed_prewhere,
            where_conditions=seed_where,
            query_params=seed_params,
            query_limit=max(limit * 10, 240),
        )

    seed_rows = _query_seed_rows(
        apply_namespace_scope=True,
        apply_health_check_exclusion=effective_exclude_health_check,
    )
    if not seed_rows and effective_exclude_health_check:
        effective_exclude_health_check = False
        degrade_reasons.append("health_check_filter_relaxed")
        seed_rows = _query_seed_rows(
            apply_namespace_scope=True,
            apply_health_check_exclusion=False,
        )

    if not seed_rows and requested_namespace_scoped:
        namespace_scope_relaxed = True
        degrade_reasons.append("namespace_scope_relaxed")
        seed_rows = _query_seed_rows(
            apply_namespace_scope=False,
            apply_health_check_exclusion=effective_exclude_health_check,
        )

    ranked_by_id: Dict[str, Dict[str, Any]] = {}
    seed_ids: set[str] = set()
    seed_trace_ids: set[str] = set()
    seed_request_ids: set[str] = set()
    seed_timestamps: List[datetime] = []

    seed_row_namespaces: set[str] = set()
    for row in seed_rows:
        row_id = str(row.get("id") or "").strip()
        if not row_id:
            continue
        row_namespace = str(row.get("namespace") or "").strip()
        if row_namespace:
            seed_row_namespaces.add(row_namespace)
        seed_ids.add(row_id)
        trace_id = str(row.get("trace_id") or "").strip()
        if trace_id:
            seed_trace_ids.add(trace_id)
        request_id = _extract_request_id_value(row.get("attributes_json"), row.get("message") or "")
        if request_id:
            seed_request_ids.add(request_id)
        row_ts = _to_utc_datetime(row.get("timestamp"))
        if row_ts:
            seed_timestamps.append(row_ts)
        scored = _score_row(row, seed_ids=seed_ids)
        if scored:
            ranked_by_id[row_id] = scored

    if namespace_scope_relaxed:
        correlation_namespaces = sorted(seed_row_namespaces)
    else:
        correlation_namespaces = [normalized_namespace] if normalized_namespace else edge_namespaces
    expansion_enabled = bool(correlation_namespaces and seed_timestamps and (seed_trace_ids or seed_request_ids))
    expanded_rows = 0
    if expansion_enabled:
        min_seed_ts = min(seed_timestamps)
        max_seed_ts = max(seed_timestamps)
        correlation_start = min_seed_ts.timestamp() - 5 * 60
        correlation_end = max_seed_ts.timestamp() + 5 * 60
        correlation_params: Dict[str, Any] = {
            "edge_namespaces": correlation_namespaces,
            "correlation_start": convert_timestamp_fn(_format_timestamp_rfc3339_utc(correlation_start)) or _format_timestamp_rfc3339_utc(correlation_start),
            "correlation_end": convert_timestamp_fn(_format_timestamp_rfc3339_utc(correlation_end)) or _format_timestamp_rfc3339_utc(correlation_end),
            "seed_ids": list(seed_ids),
        }
        correlation_prewhere = [
            "namespace IN {edge_namespaces:Array(String)}",
            "timestamp >= toDateTime64({correlation_start:String}, 9, 'UTC')",
            "timestamp <= toDateTime64({correlation_end:String}, 9, 'UTC')",
        ]
        correlation_where: List[str] = []
        if effective_exclude_health_check:
            append_health_check_exclusion_fn(correlation_where, correlation_params)
        if seed_ids:
            correlation_where.append("id NOT IN {seed_ids:Array(String)}")

        correlation_match_clauses: List[str] = []
        if seed_trace_ids:
            correlation_match_clauses.append("trace_id IN {trace_ids:Array(String)}")
            correlation_params["trace_ids"] = sorted(seed_trace_ids)
        if seed_request_ids:
            request_id_conditions: List[str] = []
            _append_request_id_values_filter(request_id_conditions, correlation_params, sorted(seed_request_ids), param_name="request_ids")
            if request_id_conditions:
                correlation_match_clauses.extend(request_id_conditions)
        if correlation_match_clauses:
            correlation_where.append(f"({' OR '.join(correlation_match_clauses)})")
            correlation_rows = _run_preview_query(
                prewhere_conditions=correlation_prewhere,
                where_conditions=correlation_where,
                query_params=correlation_params,
                query_limit=max(limit * 20, 320),
            )
            for row in correlation_rows:
                row_id = str(row.get("id") or "").strip()
                if not row_id:
                    continue
                scored = _score_row(
                    row,
                    seed_trace_ids=seed_trace_ids,
                    seed_request_ids=seed_request_ids,
                    seed_ids=seed_ids,
                )
                if not scored:
                    continue
                expanded_rows += 1
                existing = ranked_by_id.get(row_id)
                if not existing or int(scored.get("edge_match_score", 0)) > int(existing.get("edge_match_score", 0)):
                    ranked_by_id[row_id] = scored

    ranked_rows = sorted(
        ranked_by_id.values(),
        key=lambda item: (
            int(item.get("edge_side_priority", 0)),
            int(item.get("edge_match_score", 0)),
            to_datetime_fn(item.get("timestamp")).isoformat(),
        ),
        reverse=True,
    )[:limit]
    _decode_log_payload_fields(ranked_rows)
    _normalize_log_rows_timestamps(ranked_rows)

    return {
        "data": ranked_rows,
        "count": len(ranked_rows),
        "limit": limit,
        "context": {
            "source_service": source,
            "target_service": target,
            "namespace": normalized_namespace,
            "source_namespace": normalized_source_namespace,
            "target_namespace": normalized_target_namespace,
            "time_window": safe_window,
            "anchor_time": _format_timestamp_rfc3339_utc(normalized_anchor_time) if normalized_anchor_time else None,
            "seed_count": len(seed_ids),
            "expanded_count": expanded_rows,
            "expansion_enabled": expansion_enabled,
            "trace_id_count": len(seed_trace_ids),
            "request_id_count": len(seed_request_ids),
            "effective_exclude_health_check": effective_exclude_health_check,
            "namespace_scope_relaxed": namespace_scope_relaxed,
            "degrade_reasons": degrade_reasons,
            "seed_namespaces": sorted(seed_row_namespaces)[:20],
            "trace_ids": sorted(seed_trace_ids)[:50],
            "request_ids": sorted(seed_request_ids)[:50],
        },
    }


def query_logs_aggregated(
    *,
    storage_adapter: Any,
    limit: int,
    min_pattern_count: int,
    max_patterns: int,
    max_samples: int,
    service_name: Optional[str],
    service_names: Optional[List[str]],
    trace_id: Optional[str],
    request_id: Optional[str] = None,
    pod_name: Optional[str],
    level: Optional[str],
    levels: Optional[List[str]],
    namespace: Optional[str],
    namespaces: Optional[List[str]],
    container_name: Optional[str],
    start_time: Optional[str],
    end_time: Optional[str],
    exclude_health_check: bool,
    search: Optional[str],
    source_service: Optional[str],
    target_service: Optional[str],
    time_window: Optional[str],
    normalize_topology_context_fn: Callable[..., Dict[str, Any]],
    normalize_optional_str_list_fn: Callable[[Any], List[str]],
    normalize_level_values_fn: Callable[[Any], List[str]],
    expand_level_match_values_fn: Callable[[List[str]], List[str]],
    append_exact_match_filter_fn: Callable[..., None],
    append_health_check_exclusion_fn: Callable[[List[str], Dict[str, Any]], None],
    convert_timestamp_fn: Callable[[Optional[str]], Optional[str]],
    logger: Any,
    trace_ids: Optional[List[str]] = None,
    request_ids: Optional[List[str]] = None,
    anchor_time: Optional[str] = None,
    correlation_mode: Optional[str] = None,
) -> Dict[str, Any]:
    """Aggregate logs into patterns."""
    prewhere_conditions: List[str] = []
    where_conditions: List[str] = []
    params: Dict[str, Any] = {}
    context = normalize_topology_context_fn(
        service_name=service_name,
        search=search,
        start_time=start_time,
        end_time=end_time,
        source_service=source_service,
        target_service=target_service,
        time_window=_resolve_query_logs_time_window(start_time, end_time, time_window),
    )

    explicit_service_name = str(service_name or "").strip() or None
    explicit_service_names = normalize_optional_str_list_fn(service_names)
    explicit_search = str(search or "").strip() or None
    normalized_source_service = str(source_service or "").strip() or None
    normalized_target_service = str(target_service or "").strip() or None
    normalized_source_namespace = None
    normalized_target_namespace = None
    same_edge_endpoints = bool(
        normalized_source_service
        and normalized_target_service
        and normalized_source_service.lower() == normalized_target_service.lower()
    )
    requested_trace_ids = normalize_optional_str_list_fn([trace_id or "", *(normalize_optional_str_list_fn(trace_ids))])
    requested_request_ids = normalize_optional_str_list_fn([request_id or "", *(normalize_optional_str_list_fn(request_ids))])
    edge_context_active = bool(
        normalized_source_service
        and normalized_target_service
        and not same_edge_endpoints
        and not explicit_service_name
        and not explicit_service_names
        and not explicit_search
        and not requested_trace_ids
        and not requested_request_ids
    )

    effective_service_name = None if edge_context_active else context.get("service_name")
    if same_edge_endpoints and not explicit_service_name and not explicit_service_names:
        effective_service_name = normalized_source_service
    requested_service_names = list(explicit_service_names)
    if effective_service_name:
        requested_service_names = normalize_optional_str_list_fn([effective_service_name, *requested_service_names])
    requested_namespaces = normalize_optional_str_list_fn(namespaces)
    if namespace:
        requested_namespaces = normalize_optional_str_list_fn([namespace, *requested_namespaces])
    requested_levels = normalize_level_values_fn([level or "", *(normalize_optional_str_list_fn(levels))])
    normalized_pod_name = str(pod_name or "").strip() or None
    requested_level_match_values = expand_level_match_values_fn(requested_levels)
    effective_correlation_mode = _normalize_correlation_mode(correlation_mode)
    requested_container_name = str(container_name or "").strip() or None
    normalized_anchor_time = str(anchor_time or "").strip() or None
    effective_search = None if edge_context_active else context.get("search")
    if same_edge_endpoints and not explicit_search:
        effective_search = None
    effective_start_time = context.get("start_time")
    effective_end_time = context.get("end_time")
    fallback_time_window = context.get("time_window") or _resolve_query_logs_default_window()
    max_threads = _resolve_query_logs_max_threads()

    if edge_context_active:
        _append_topology_edge_candidate_filter(
            prewhere_conditions=prewhere_conditions,
            where_conditions=where_conditions,
            params=params,
            source_service=normalized_source_service,
            target_service=normalized_target_service,
            source_namespace=normalized_source_namespace,
            target_namespace=normalized_target_namespace,
        )
    else:
        _append_normalized_service_filter(
            conditions=prewhere_conditions,
            params=params,
            values=requested_service_names,
            param_prefix="service_name",
        )
    append_exact_match_filter_fn(
        conditions=prewhere_conditions,
        params=params,
        column_name="namespace",
        param_prefix="namespace",
        values=requested_namespaces,
    )
    if normalized_pod_name:
        prewhere_conditions.append("pod_name = {pod_name:String}")
        params["pod_name"] = normalized_pod_name
    if requested_container_name:
        prewhere_conditions.append("container_name = {container_name:String}")
        params["container_name"] = requested_container_name
    append_exact_match_filter_fn(
        conditions=prewhere_conditions,
        params=params,
        column_name="level_norm",
        param_prefix="level",
        values=requested_levels,
    )
    if effective_start_time and str(effective_start_time).startswith("__RELATIVE_INTERVAL__::"):
        relative_interval = str(effective_start_time).split("::", 1)[1]
        _append_relative_window_lower_bound(
            conditions=prewhere_conditions,
            params=params,
            interval_text=relative_interval,
            anchor_time=normalized_anchor_time,
            convert_timestamp_fn=convert_timestamp_fn,
        )
    elif effective_start_time:
        prewhere_conditions.append("timestamp >= toDateTime64({start_time:String}, 9, 'UTC')")
        params["start_time"] = convert_timestamp_fn(effective_start_time)
    if effective_end_time:
        prewhere_conditions.append("timestamp <= toDateTime64({end_time:String}, 9, 'UTC')")
        params["end_time"] = convert_timestamp_fn(effective_end_time)
        if not effective_start_time:
            fallback_time_window = _clamp_logs_interval(str(fallback_time_window), default_value="1 HOUR")
            prewhere_conditions.append(
                f"timestamp > toDateTime64({{end_time:String}}, 9, 'UTC') - INTERVAL {fallback_time_window}"
            )
    if normalized_anchor_time:
        prewhere_conditions.append("timestamp <= toDateTime64({anchor_time:String}, 9, 'UTC')")
        params["anchor_time"] = convert_timestamp_fn(normalized_anchor_time)

    if exclude_health_check:
        append_health_check_exclusion_fn(where_conditions, params)

    effective_correlation_mode = _append_trace_request_correlation_filters(
        prewhere_conditions=prewhere_conditions,
        where_conditions=where_conditions,
        params=params,
        requested_trace_ids=requested_trace_ids,
        requested_request_ids=requested_request_ids,
        request_id=request_id,
        request_ids=request_ids,
        normalize_optional_str_list_fn=normalize_optional_str_list_fn,
        correlation_mode=effective_correlation_mode,
    )
    _append_text_search_filter(where_conditions, params, effective_search)

    prewhere_clause = f"PREWHERE {' AND '.join(prewhere_conditions)}" if prewhere_conditions else ""
    where_clause = f"WHERE {' AND '.join(where_conditions)}" if where_conditions else ""
    query = f"""
    SELECT
        id,
        timestamp,
        service_name,
        level,
        message,
        pod_name,
        namespace,
        node_name,
        container_name,
        container_id,
        container_image,
        trace_id,
        span_id,
        labels,
        attributes_json,
        host_ip
    FROM logs.logs
    {prewhere_clause}
    {where_clause}
    ORDER BY timestamp DESC
    LIMIT {{limit:Int32}}
    SETTINGS optimize_use_projections = 1, optimize_read_in_order = 1, max_threads = {{max_threads:Int32}}
    """
    params["limit"] = limit
    params["max_threads"] = max_threads

    rows = storage_adapter.execute_query(query, params)
    _decode_log_payload_fields(rows)
    _normalize_log_rows_timestamps(rows)

    aggregator = PatternAggregator(
        min_samples=min_pattern_count,
        max_samples=max_samples,
        max_patterns=max_patterns,
    )
    aggregation_result = aggregator.aggregate(rows)

    logger.info(
        "Aggregation query executed",
        extra={
            "query_type": "logs_aggregated",
            "input_logs": len(rows),
            "patterns_found": aggregation_result["total_patterns"],
            "aggregation_ratio": aggregation_result["aggregation_ratio"],
            "filters": {
                "service_name": effective_service_name,
                "service_names": requested_service_names,
                "namespaces": requested_namespaces,
                "trace_id": trace_id,
                "trace_ids": requested_trace_ids,
                "request_id": request_id,
                "request_ids": requested_request_ids,
                "correlation_mode": effective_correlation_mode,
                "pod_name": normalized_pod_name,
                "container_name": requested_container_name,
                "level": level,
                "levels": requested_levels,
                "level_match_values": requested_level_match_values,
                "exclude_health_check": exclude_health_check,
                "search": effective_search,
                "source_service": context.get("source_service"),
                "target_service": context.get("target_service"),
                "time_window": context.get("time_window"),
            },
        },
    )

    return aggregation_result


def query_logs_context(
    *,
    storage_adapter: Any,
    log_id: Optional[str],
    trace_id: Optional[str],
    pod_name: Optional[str],
    namespace: Optional[str],
    container_name: Optional[str],
    timestamp: Optional[str],
    before_count: int,
    after_count: int,
    limit: int,
    convert_timestamp_fn: Callable[[Optional[str]], Optional[str]],
) -> Dict[str, Any]:
    """Query logs context by log_id/trace_id/pod+timestamp."""
    context_fields = _LOGS_LIGHT_FIELDS
    context_max_threads = _resolve_query_logs_max_threads()
    context_window_minutes_candidates = _resolve_query_logs_context_window_minutes()

    normalized_log_id = str(log_id or "").strip()
    normalized_trace_id = str(trace_id or "").strip()
    normalized_pod_name = str(pod_name or "").strip()
    normalized_timestamp = str(timestamp or "").strip()
    normalized_namespace = str(namespace or "").strip()
    normalized_container_name = str(container_name or "").strip()

    if normalized_trace_id and not normalized_log_id:
        query = f"""
        SELECT
            {context_fields}
        FROM logs.logs
        PREWHERE trace_id = {{trace_id:String}}
        ORDER BY timestamp ASC, id ASC
        LIMIT {{limit:Int32}}
        SETTINGS optimize_use_projections = 1, optimize_read_in_order = 1, max_threads = {{max_threads:Int32}}
        """
        params = {
            "trace_id": normalized_trace_id,
            "limit": limit,
            "max_threads": context_max_threads,
        }
        results = storage_adapter.execute_query(query, params)
        _decode_log_payload_fields(results)
        _normalize_log_rows_timestamps(results)
        return {
            "trace_id": normalized_trace_id,
            "data": results,
            "count": len(results),
            "limit": limit,
        }

    if normalized_log_id:
        anchor_query = f"""
        SELECT {context_fields}
        FROM logs.logs
        PREWHERE id = {{anchor_log_id:String}}
        LIMIT 1
        """
        anchor_rows = storage_adapter.execute_query(
            anchor_query,
            {"anchor_log_id": normalized_log_id},
        )
        if anchor_rows:
            anchor_row = anchor_rows[0]
            anchor_pod_name = str(anchor_row.get("pod_name") or "").strip()
            anchor_namespace = str(anchor_row.get("namespace") or "").strip()
            anchor_container_name = str(anchor_row.get("container_name") or "").strip()
            anchor_timestamp = _format_timestamp_rfc3339_utc(anchor_row.get("timestamp"))
            anchor_timestamp_for_query = (
                _format_timestamp_for_clickhouse_utc(anchor_row.get("timestamp"))
                or convert_timestamp_fn(anchor_timestamp)
                or anchor_timestamp
            )
            anchor_id = str(anchor_row.get("id") or "").strip()

            if anchor_pod_name and anchor_timestamp and anchor_id:
                where_parts = ["pod_name = {pod_name:String}"]
                shared_params: Dict[str, Any] = {
                    "pod_name": anchor_pod_name,
                    "anchor_timestamp": anchor_timestamp_for_query,
                    "anchor_id": anchor_id,
                }
                if anchor_namespace:
                    where_parts.append("namespace = {namespace:String}")
                    shared_params["namespace"] = anchor_namespace
                if anchor_container_name:
                    where_parts.append("container_name = {container_name:String}")
                    shared_params["container_name"] = anchor_container_name
                where_prefix = " AND ".join(where_parts)

                bounded_before_query = f"""
                SELECT {context_fields}
                FROM logs.logs
                PREWHERE {where_prefix}
                    AND timestamp <= toDateTime64({{anchor_timestamp:String}}, 9, 'UTC')
                    AND timestamp >= toDateTime64({{anchor_timestamp:String}}, 9, 'UTC') - toIntervalMinute({{context_window_minutes:Int32}})
                WHERE (
                    timestamp < toDateTime64({{anchor_timestamp:String}}, 9, 'UTC')
                    OR (
                      timestamp = toDateTime64({{anchor_timestamp:String}}, 9, 'UTC')
                      AND id < {{anchor_id:String}}
                    )
                  )
                ORDER BY timestamp DESC, id DESC
                LIMIT {{before_count:Int32}}
                SETTINGS optimize_use_projections = 1, optimize_read_in_order = 1, max_threads = {{max_threads:Int32}}
                """
                fallback_before_query = f"""
                SELECT {context_fields}
                FROM logs.logs
                PREWHERE {where_prefix}
                    AND timestamp <= toDateTime64({{anchor_timestamp:String}}, 9, 'UTC')
                WHERE (
                    timestamp < toDateTime64({{anchor_timestamp:String}}, 9, 'UTC')
                    OR (
                      timestamp = toDateTime64({{anchor_timestamp:String}}, 9, 'UTC')
                      AND id < {{anchor_id:String}}
                    )
                  )
                ORDER BY timestamp DESC, id DESC
                LIMIT {{before_count:Int32}}
                SETTINGS optimize_use_projections = 1, optimize_read_in_order = 1, max_threads = {{max_threads:Int32}}
                """

                before_results: List[Dict[str, Any]] = []
                for context_window_minutes in context_window_minutes_candidates:
                    before_rows = storage_adapter.execute_query(
                        bounded_before_query,
                        {
                            **shared_params,
                            "before_count": before_count,
                            "context_window_minutes": context_window_minutes,
                            "max_threads": context_max_threads,
                        },
                    )
                    before_results = list(reversed(before_rows))
                    if len(before_results) >= before_count:
                        break
                if len(before_results) < before_count:
                    before_results = list(
                        reversed(
                            storage_adapter.execute_query(
                                fallback_before_query,
                                {
                                    **shared_params,
                                    "before_count": before_count,
                                    "max_threads": context_max_threads,
                                },
                            )
                        )
                    )

                bounded_after_query = f"""
                SELECT {context_fields}
                FROM logs.logs
                PREWHERE {where_prefix}
                    AND timestamp >= toDateTime64({{anchor_timestamp:String}}, 9, 'UTC')
                    AND timestamp <= toDateTime64({{anchor_timestamp:String}}, 9, 'UTC') + toIntervalMinute({{context_window_minutes:Int32}})
                WHERE (
                    timestamp > toDateTime64({{anchor_timestamp:String}}, 9, 'UTC')
                    OR (
                      timestamp = toDateTime64({{anchor_timestamp:String}}, 9, 'UTC')
                      AND id > {{anchor_id:String}}
                    )
                  )
                ORDER BY timestamp ASC, id ASC
                LIMIT {{after_count:Int32}}
                SETTINGS optimize_use_projections = 1, optimize_read_in_order = 1, max_threads = {{max_threads:Int32}}
                """
                fallback_after_query = f"""
                SELECT {context_fields}
                FROM logs.logs
                PREWHERE {where_prefix}
                    AND timestamp >= toDateTime64({{anchor_timestamp:String}}, 9, 'UTC')
                WHERE (
                    timestamp > toDateTime64({{anchor_timestamp:String}}, 9, 'UTC')
                    OR (
                      timestamp = toDateTime64({{anchor_timestamp:String}}, 9, 'UTC')
                      AND id > {{anchor_id:String}}
                    )
                  )
                ORDER BY timestamp ASC, id ASC
                LIMIT {{after_count:Int32}}
                SETTINGS optimize_use_projections = 1, optimize_read_in_order = 1, max_threads = {{max_threads:Int32}}
                """

                after_results: List[Dict[str, Any]] = []
                for context_window_minutes in context_window_minutes_candidates:
                    after_results = storage_adapter.execute_query(
                        bounded_after_query,
                        {
                            **shared_params,
                            "after_count": after_count,
                            "context_window_minutes": context_window_minutes,
                            "max_threads": context_max_threads,
                        },
                    )
                    if len(after_results) >= after_count:
                        break
                if len(after_results) < after_count:
                    after_results = storage_adapter.execute_query(
                        fallback_after_query,
                        {
                            **shared_params,
                            "after_count": after_count,
                            "max_threads": context_max_threads,
                        },
                    )

                for rows in (before_results, after_results):
                    _decode_log_payload_fields(rows)
                _decode_log_payload_fields(anchor_rows)
                _normalize_log_rows_timestamps(before_results)
                _normalize_log_rows_timestamps(after_results)
                _normalize_log_rows_timestamps(anchor_rows)

                return {
                    "log_id": normalized_log_id,
                    "pod_name": anchor_pod_name,
                    "namespace": anchor_namespace or None,
                    "container_name": anchor_container_name or None,
                    "timestamp": anchor_timestamp,
                    "before": before_results,
                    "after": after_results,
                    "current": anchor_rows[0],
                    "before_count": len(before_results),
                    "after_count": len(after_results),
                    "context_mode": "log_id",
                }

            _decode_log_payload_fields(anchor_rows)
            _normalize_log_rows_timestamps(anchor_rows)
            return {
                "log_id": normalized_log_id,
                "container_name": anchor_container_name or None,
                "before": [],
                "after": [],
                "current": anchor_rows[0],
                "before_count": 0,
                "after_count": 0,
                "context_mode": "log_id",
            }

    if normalized_pod_name and normalized_timestamp:
        ch_timestamp = convert_timestamp_fn(normalized_timestamp) or normalized_timestamp
        where_parts = ["pod_name = {pod_name:String}"]
        if normalized_namespace:
            where_parts.append("namespace = {namespace:String}")
        if normalized_container_name:
            where_parts.append("container_name = {container_name:String}")
        where_prefix = " AND ".join(where_parts)
        context_params: Dict[str, Any] = {
            "pod_name": normalized_pod_name,
            "timestamp": ch_timestamp,
        }
        if normalized_namespace:
            context_params["namespace"] = normalized_namespace
        if normalized_container_name:
            context_params["container_name"] = normalized_container_name

        before_anchor_clause = "timestamp < toDateTime64({timestamp:String}, 9, 'UTC')"
        after_anchor_clause = "timestamp > toDateTime64({timestamp:String}, 9, 'UTC')"

        bounded_before_query = f"""
        SELECT {context_fields}
        FROM logs.logs
        PREWHERE {where_prefix}
            AND timestamp >= toDateTime64({{timestamp:String}}, 9, 'UTC') - toIntervalMinute({{context_window_minutes:Int32}})
            AND {before_anchor_clause}
        ORDER BY timestamp DESC, id DESC
        LIMIT {{before_count:Int32}}
        SETTINGS optimize_use_projections = 1, optimize_read_in_order = 1, max_threads = {{max_threads:Int32}}
        """
        fallback_before_query = f"""
        SELECT {context_fields}
        FROM logs.logs
        PREWHERE {where_prefix}
            AND {before_anchor_clause}
        ORDER BY timestamp DESC, id DESC
        LIMIT {{before_count:Int32}}
        SETTINGS optimize_use_projections = 1, optimize_read_in_order = 1, max_threads = {{max_threads:Int32}}
        """
        before_results: List[Dict[str, Any]] = []
        for context_window_minutes in context_window_minutes_candidates:
            before_rows = storage_adapter.execute_query(
                bounded_before_query,
                {
                    **context_params,
                    "before_count": before_count,
                    "context_window_minutes": context_window_minutes,
                    "max_threads": context_max_threads,
                },
            )
            before_results = list(reversed(before_rows))
            if len(before_results) >= before_count:
                break
        if len(before_results) < before_count:
            before_results = list(
                reversed(
                    storage_adapter.execute_query(
                        fallback_before_query,
                        {
                            **context_params,
                            "before_count": before_count,
                            "max_threads": context_max_threads,
                        },
                    )
                )
            )

        bounded_after_query = f"""
        SELECT {context_fields}
        FROM logs.logs
        PREWHERE {where_prefix}
            AND timestamp <= toDateTime64({{timestamp:String}}, 9, 'UTC') + toIntervalMinute({{context_window_minutes:Int32}})
            AND {after_anchor_clause}
        ORDER BY timestamp ASC, id ASC
        LIMIT {{after_count:Int32}}
        SETTINGS optimize_use_projections = 1, optimize_read_in_order = 1, max_threads = {{max_threads:Int32}}
        """
        fallback_after_query = f"""
        SELECT {context_fields}
        FROM logs.logs
        PREWHERE {where_prefix}
            AND {after_anchor_clause}
        ORDER BY timestamp ASC, id ASC
        LIMIT {{after_count:Int32}}
        SETTINGS optimize_use_projections = 1, optimize_read_in_order = 1, max_threads = {{max_threads:Int32}}
        """
        after_results: List[Dict[str, Any]] = []
        for context_window_minutes in context_window_minutes_candidates:
            after_results = storage_adapter.execute_query(
                bounded_after_query,
                {
                    **context_params,
                    "after_count": after_count,
                    "context_window_minutes": context_window_minutes,
                    "max_threads": context_max_threads,
                },
            )
            if len(after_results) >= after_count:
                break
        if len(after_results) < after_count:
            after_results = storage_adapter.execute_query(
                fallback_after_query,
                {
                    **context_params,
                    "after_count": after_count,
                    "max_threads": context_max_threads,
                },
            )

        current_query = f"""
        SELECT {context_fields}
        FROM logs.logs
        PREWHERE {where_prefix}
            AND timestamp = toDateTime64({{timestamp:String}}, 9, 'UTC')
        ORDER BY timestamp DESC, id DESC
        LIMIT {{current_limit:Int32}}
        SETTINGS optimize_use_projections = 1, optimize_read_in_order = 1, max_threads = {{max_threads:Int32}}
        """
        current_limit = max(int(limit or 0), before_count + after_count + 1, 20)
        current_results = storage_adapter.execute_query(
            current_query,
            {
                **context_params,
                "current_limit": current_limit,
                "max_threads": context_max_threads,
            },
        )

        for rows in (before_results, after_results, current_results):
            _decode_log_payload_fields(rows)
            _normalize_log_rows_timestamps(rows)

        return {
            "pod_name": normalized_pod_name,
            "namespace": normalized_namespace or None,
            "container_name": normalized_container_name or None,
            "timestamp": _format_timestamp_rfc3339_utc(normalized_timestamp),
            "before": before_results,
            "after": after_results,
            "current": current_results[0] if current_results else None,
            "current_matches": current_results,
            "current_count": len(current_results),
            "before_count": len(before_results),
            "after_count": len(after_results),
            "context_mode": "pod_timestamp",
        }

    if normalized_log_id:
        raise HTTPException(status_code=404, detail="指定 log_id 不存在")

    raise HTTPException(status_code=400, detail="必须提供 trace_id、log_id 或 (pod_name 和 timestamp)")


def query_log_detail(
    *,
    storage_adapter: Any,
    log_id: str,
) -> Dict[str, Any]:
    """Query one log detail by id."""
    normalized_log_id = str(log_id or "").strip()
    if not normalized_log_id:
        raise HTTPException(status_code=400, detail="log_id is required")
    query = """
    SELECT
        id,
        timestamp,
        service_name,
        pod_name,
        namespace,
        node_name,
        container_name,
        container_id,
        container_image,
        pod_id,
        level,
        message,
        trace_id,
        span_id,
        labels,
        attributes_json,
        host_ip
    FROM logs.logs
    PREWHERE id = {log_id:String}
    LIMIT 1
    """
    results = storage_adapter.execute_query(query, {"log_id": normalized_log_id})
    if not results:
        raise HTTPException(status_code=404, detail="Log not found")

    row = results[0]
    _decode_log_payload_fields([row])
    _normalize_log_row_timestamp(row)
    return {"data": row}
