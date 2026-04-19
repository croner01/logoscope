"""
Request flow analysis agent with predefined tools.

This module implements a lightweight "agent + tools" pipeline:
1. Extract request/trace identifiers and anchor timestamp.
2. Query related logs in a bounded time window.
3. Query trace spans (when trace_id is available).
4. Optionally query external search endpoint (disabled by default).
5. Build a deterministic request-flow context for downstream LLM/rule analysis.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None


logger = logging.getLogger(__name__)

_REQUEST_ID_KEYS: Tuple[str, ...] = (
    "request_id",
    "request.id",
    "requestId",
    "req_id",
    "x-request-id",
    "x_request_id",
    "http.request_id",
    "trace.request_id",
)
_TRACE_ID_KEYS: Tuple[str, ...] = (
    "trace_id",
    "trace.id",
    "traceId",
    "trace-id",
    "otel.trace_id",
)
_ERROR_LEVELS = {"ERROR", "FATAL", "CRITICAL"}
_WARNING_LEVELS = {"WARN", "WARNING"}

_TRACE_ID_REGEX = re.compile(r"\b(?:trace[_-]?id|trace\.id)\s*[:=]\s*([a-zA-Z0-9_-]{8,})\b", re.IGNORECASE)
_REQUEST_ID_REGEX = re.compile(
    r"\b(?:request[_-]?id|req[_-]?id|x-request-id)\s*[:=]\s*([a-zA-Z0-9._:-]{6,})\b",
    re.IGNORECASE,
)
_REQ_PREFIX_REGEX = re.compile(r"\b(req-[a-zA-Z0-9._:-]{3,})\b", re.IGNORECASE)
_TIMESTAMP_REGEX = re.compile(
    r"\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"
)
_TRACEBACK_HINT_REGEX = re.compile(
    r"(traceback|exception|caused by:|stack trace|^\s*at\s+\S+\()", re.IGNORECASE | re.MULTILINE
)


def _as_str(value: Any, default: str = "") -> str:
    text = str(value or "").strip()
    return text if text else default


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if not text:
        return default
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _as_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _resolve_timezone(value: Any, fallback: tzinfo = timezone.utc) -> tzinfo:
    text = _as_str(value)
    if not text:
        return fallback
    if text.upper() == "UTC":
        return timezone.utc
    offset_match = re.fullmatch(r"([+-])(\d{2}):?(\d{2})", text)
    if offset_match:
        sign, hh, mm = offset_match.groups()
        minutes = int(hh) * 60 + int(mm)
        if sign == "-":
            minutes = -minutes
        return timezone(timedelta(minutes=minutes))
    if ZoneInfo is not None:
        try:
            return ZoneInfo(text)
        except Exception:
            return fallback
    return fallback


_DEFAULT_INPUT_TZ = _resolve_timezone(os.getenv("AI_AGENT_INPUT_DEFAULT_TZ", "UTC"), timezone.utc)


def _resolve_context_timezone(context: Optional[Dict[str, Any]]) -> tzinfo:
    safe_context = context or {}
    for key in (
        "input_timezone",
        "timezone",
        "source_timezone",
        "client_timezone",
    ):
        resolved = _resolve_timezone(safe_context.get(key), fallback=None)  # type: ignore[arg-type]
        if resolved is not None:
            return resolved

    for key in (
        "input_timezone_offset_minutes",
        "timezone_offset_minutes",
        "client_timezone_offset_minutes",
        "tz_offset_minutes",
    ):
        raw = safe_context.get(key)
        if raw is None:
            continue
        try:
            minutes = int(str(raw).strip())
        except (TypeError, ValueError):
            continue
        return timezone(timedelta(minutes=minutes))

    return _DEFAULT_INPUT_TZ


def _parse_timestamp(value: Any, default_tz: tzinfo = timezone.utc) -> Optional[datetime]:
    raw = _as_str(value)
    if not raw:
        return None
    normalized = raw.replace(" ", "T")
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    if len(normalized) >= 5 and normalized[-5] in {"+", "-"} and normalized[-3] != ":":
        normalized = normalized[:-2] + ":" + normalized[-2:]
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=default_tz)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.astimezone(timezone.utc)


def _to_clickhouse_utc_text(value: datetime) -> str:
    naive_utc = value.astimezone(timezone.utc).replace(tzinfo=None)
    return naive_utc.strftime("%Y-%m-%d %H:%M:%S.%f")


def _is_timestamp_type_mismatch_error(exc: Exception) -> bool:
    text = _as_str(exc).lower()
    return (
        "between string and datetime64" in text
        or "between string and datetime" in text
    )


def _safe_json_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _extract_dot_path(data: Dict[str, Any], path: str) -> str:
    current: Any = data
    for segment in path.split("."):
        if not isinstance(current, dict):
            return ""
        current = current.get(segment)
    return _as_str(current)


def _extract_request_id_from_attrs(attributes_json: Any) -> str:
    attrs = _safe_json_dict(attributes_json)
    if not attrs:
        return ""
    for key in _REQUEST_ID_KEYS:
        direct = _as_str(attrs.get(key))
        if direct:
            return direct
        if "." in key:
            nested = _extract_dot_path(attrs, key)
            if nested:
                return nested
    nested_request_id = _as_str(_extract_dot_path(attrs, "request.id"))
    if nested_request_id:
        return nested_request_id
    return _as_str(_extract_dot_path(attrs, "http.request_id"))


def _normalize_level(value: Any) -> str:
    return _as_str(value, "INFO").upper()


def _short_message(text: Any, max_length: int = 220) -> str:
    normalized = " ".join(_as_str(text).split())
    if len(normalized) <= max_length:
        return normalized
    return normalized[: max_length - 3] + "..."


def _compact_traceback_message(
    text: Any,
    *,
    line_limit_head: int = 16,
    line_limit_tail: int = 28,
    max_length: int = 1800,
) -> str:
    raw_text = str(text or "")
    if not raw_text.strip():
        return ""
    normalized = raw_text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not _TRACEBACK_HINT_REGEX.search(normalized):
        return _short_message(normalized, max_length=max_length)

    lines = [line.rstrip() for line in normalized.split("\n")]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return ""

    clipped_lines = lines
    if len(lines) > (line_limit_head + line_limit_tail):
        clipped_lines = lines[:line_limit_head] + ["...<truncated traceback>..."] + lines[-line_limit_tail:]

    clipped_text = "\n".join(clipped_lines).strip()
    if len(clipped_text) <= max_length:
        return clipped_text

    if max_length <= 64:
        return clipped_text[:max_length]
    return clipped_text[: max_length - 24].rstrip() + "\n...<truncated>..."


def _build_request_id_clause(param_name: str = "request_id") -> str:
    clauses = [
        f"JSONExtractString(attributes_json, '{key}') = {{{param_name}:String}}"
        for key in _REQUEST_ID_KEYS
    ]
    clauses.extend(
        [
            f"JSONExtractString(attributes_json, 'request', 'id') = {{{param_name}:String}}",
            f"JSONExtractString(attributes_json, 'http', 'request_id') = {{{param_name}:String}}",
            f"JSONExtractString(attributes_json, 'trace', 'request_id') = {{{param_name}:String}}",
            f"message ILIKE concat('%', {{{param_name}:String}}, '%')",
        ]
    )
    return "(" + " OR ".join(clauses) + ")"


def _prioritize_logs_for_injection(related_logs: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    normalized_limit = max(1, int(limit))
    traceback_errors: List[Dict[str, Any]] = []
    other_errors: List[Dict[str, Any]] = []
    other_logs: List[Dict[str, Any]] = []
    for row in related_logs:
        level = _normalize_level(row.get("level"))
        message = _as_str(row.get("message"))
        if level in _ERROR_LEVELS and _TRACEBACK_HINT_REGEX.search(message):
            traceback_errors.append(row)
        elif level in _ERROR_LEVELS or level in _WARNING_LEVELS:
            other_errors.append(row)
        else:
            other_logs.append(row)

    selected = (traceback_errors + other_errors + other_logs)[:normalized_limit]
    return sorted(selected, key=lambda item: _as_str(item.get("timestamp")))


@dataclass
class ToolExecution:
    """Execution metadata for one tool invocation."""

    name: str
    success: bool
    duration_ms: int
    detail: Dict[str, Any] = field(default_factory=dict)


class LogsQueryTool:
    """Tool: query related logs in bounded time window."""

    name = "logs_query"
    _TIMESTAMP_CAST_EXPR = "parseDateTime64BestEffortOrNull(toString(timestamp), 9, 'UTC')"

    def __init__(self, storage_adapter: Any):
        self.storage_adapter = storage_adapter
        self.query_api_base = _as_str(os.getenv("AI_AGENT_QUERY_API_BASE")).rstrip("/")
        self.query_api_timeout_seconds = max(1, _as_int(os.getenv("AI_AGENT_QUERY_API_TIMEOUT_SECONDS"), 6))
        self._storage_timestamp_expr = "timestamp"
        self._timestamp_expr_switched = False

    def _run_via_query_api(
        self,
        *,
        start_time: datetime,
        end_time: datetime,
        request_id: str,
        trace_id: str,
        service_name: str,
        source_service: str,
        target_service: str,
        limit: int,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        if not self.query_api_base:
            raise RuntimeError("query_api_base_not_configured")

        params: Dict[str, Any] = {
            "start_time": start_time.isoformat().replace("+00:00", "Z"),
            "end_time": end_time.isoformat().replace("+00:00", "Z"),
            "exclude_health_check": "true",
            "limit": max(20, min(_as_int(limit, 240), 800)),
        }
        if trace_id:
            params["trace_id"] = trace_id
        if request_id:
            params["request_id"] = request_id
        if service_name and not (trace_id or request_id):
            params["service_name"] = service_name
        if source_service and not (trace_id or request_id or service_name):
            params["source_service"] = source_service
        if target_service and not (trace_id or request_id or service_name):
            params["target_service"] = target_service

        encoded_query = urlencode(params)
        url = f"{self.query_api_base}/api/v1/logs?{encoded_query}"
        req = Request(url, method="GET")
        req.add_header("Accept", "application/json")
        with urlopen(req, timeout=self.query_api_timeout_seconds) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="ignore"))
        if not isinstance(payload, dict):
            raise RuntimeError("query_api_payload_invalid")
        rows = payload.get("data")
        if not isinstance(rows, list):
            raise RuntimeError("query_api_rows_invalid")

        normalized_rows: List[Dict[str, Any]] = []
        seen_keys: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            log_id = _as_str(row.get("id"))
            timestamp = _as_str(row.get("timestamp"))
            service = _as_str(row.get("service_name"), "unknown")
            level = _normalize_level(row.get("level"))
            message = _as_str(row.get("message"))
            trace_val = _as_str(row.get("trace_id"))
            attrs = row.get("attributes")
            request_val = _extract_request_id_from_attrs(attrs)
            dedupe_key = log_id or f"{timestamp}|{service}|{level}|{message}"
            if dedupe_key in seen_keys:
                continue
            seen_keys.add(dedupe_key)
            normalized_rows.append(
                {
                    "id": log_id,
                    "timestamp": timestamp,
                    "service_name": service,
                    "pod_name": _as_str(row.get("pod_name")),
                    "namespace": _as_str(row.get("namespace")),
                    "level": level,
                    "message": message,
                    "trace_id": trace_val,
                    "span_id": _as_str(row.get("span_id")),
                    "request_id": request_val,
                }
            )

        normalized_rows.sort(key=lambda item: _as_str(item.get("timestamp")))
        return normalized_rows, {"source": "query_api", "rows": len(normalized_rows)}

    def run(
        self,
        *,
        start_time: datetime,
        end_time: datetime,
        request_id: str,
        trace_id: str,
        service_name: str,
        source_service: str = "",
        target_service: str = "",
        limit: int,
    ) -> Tuple[List[Dict[str, Any]], ToolExecution]:
        start_at = time.perf_counter()
        if self.storage_adapter is None:
            if not self.query_api_base:
                return [], ToolExecution(
                    name=self.name,
                    success=False,
                    duration_ms=0,
                    detail={"reason": "storage_not_initialized"},
                )

        if self.query_api_base:
            try:
                api_rows, api_detail = self._run_via_query_api(
                    start_time=start_time,
                    end_time=end_time,
                    request_id=request_id,
                    trace_id=trace_id,
                    service_name=service_name,
                    source_service=source_service,
                    target_service=target_service,
                    limit=limit,
                )
                duration_ms = int((time.perf_counter() - start_at) * 1000)
                detail = {
                    **api_detail,
                    "request_id": bool(request_id),
                    "trace_id": bool(trace_id),
                    "service_name": service_name or "",
                    "source_service": source_service or "",
                    "target_service": target_service or "",
                }
                return api_rows, ToolExecution(name=self.name, success=True, duration_ms=duration_ms, detail=detail)
            except Exception as exc:
                logger.warning("logs_query query_api failed, fallback to storage: %s", exc)

        filters: List[str] = []
        params: Dict[str, Any] = {
            "start_time": _to_clickhouse_utc_text(start_time),
            "end_time": _to_clickhouse_utc_text(end_time),
            "limit": max(20, min(_as_int(limit, 240), 800)),
        }
        if trace_id:
            filters.append("trace_id = {trace_id:String}")
            params["trace_id"] = trace_id
        if request_id:
            filters.append(_build_request_id_clause("request_id"))
            params["request_id"] = request_id
        if service_name and not filters:
            filters.append("service_name = {service_name:String}")
            params["service_name"] = service_name
        if not filters:
            fallback_services = []
            if source_service:
                fallback_services.append(source_service)
            if target_service and target_service not in fallback_services:
                fallback_services.append(target_service)
            if len(fallback_services) == 1:
                filters.append("service_name = {source_service:String}")
                params["source_service"] = fallback_services[0]
            elif len(fallback_services) >= 2:
                filters.append("(service_name = {source_service:String} OR service_name = {target_service:String})")
                params["source_service"] = fallback_services[0]
                params["target_service"] = fallback_services[1]
        if not filters:
            filters.append("1 = 1")

        where_clause = " OR ".join(filters)

        def _build_storage_query(timestamp_expr: str) -> str:
            return f"""
            SELECT
                id,
                toString(timestamp) AS timestamp,
                service_name,
                pod_name,
                namespace,
                level,
                message,
                trace_id,
                span_id,
                attributes_json
            FROM logs.logs
            PREWHERE
                {timestamp_expr} >= toDateTime64({{start_time:String}}, 9, 'UTC')
                AND {timestamp_expr} <= toDateTime64({{end_time:String}}, 9, 'UTC')
            WHERE ({where_clause})
            ORDER BY timestamp ASC
            LIMIT {{limit:Int32}}
            """

        query = _build_storage_query(self._storage_timestamp_expr)

        try:
            rows = self.storage_adapter.execute_query(query, params) or []
        except Exception as exc:
            if not _is_timestamp_type_mismatch_error(exc):
                duration_ms = int((time.perf_counter() - start_at) * 1000)
                logger.warning("logs_query tool failed: %s", exc)
                return [], ToolExecution(
                    name=self.name,
                    success=False,
                    duration_ms=duration_ms,
                    detail={"error": str(exc)},
                )

            if self._storage_timestamp_expr != "timestamp":
                duration_ms = int((time.perf_counter() - start_at) * 1000)
                logger.warning("logs_query tool failed after timestamp expression switched: %s", _short_message(str(exc)))
                return [], ToolExecution(
                    name=self.name,
                    success=False,
                    duration_ms=duration_ms,
                    detail={"error": str(exc), "timestamp_expr": self._storage_timestamp_expr},
                )

            retry_query = _build_storage_query(self._TIMESTAMP_CAST_EXPR)
            try:
                rows = self.storage_adapter.execute_query(retry_query, params) or []
                self._storage_timestamp_expr = self._TIMESTAMP_CAST_EXPR
                if not self._timestamp_expr_switched:
                    logger.warning(
                        "logs_query timestamp type mismatch detected, switch timestamp expression to cast mode: %s",
                        _short_message(str(exc)),
                    )
                    self._timestamp_expr_switched = True
            except Exception as retry_exc:
                duration_ms = int((time.perf_counter() - start_at) * 1000)
                logger.warning("logs_query tool failed after timestamp-cast retry: %s", retry_exc)
                return [], ToolExecution(
                    name=self.name,
                    success=False,
                    duration_ms=duration_ms,
                    detail={"error": str(retry_exc), "retry": "timestamp_cast"},
                )

        normalized_rows: List[Dict[str, Any]] = []
        seen_keys: set[str] = set()
        for row in rows:
            log_id = _as_str(row.get("id"))
            timestamp = _as_str(row.get("timestamp"))
            service = _as_str(row.get("service_name"), "unknown")
            level = _normalize_level(row.get("level"))
            message = _as_str(row.get("message"))
            trace_val = _as_str(row.get("trace_id"))
            request_val = _extract_request_id_from_attrs(row.get("attributes_json"))
            dedupe_key = log_id or f"{timestamp}|{service}|{level}|{message}"
            if dedupe_key in seen_keys:
                continue
            seen_keys.add(dedupe_key)
            normalized_rows.append(
                {
                    "id": log_id,
                    "timestamp": timestamp,
                    "service_name": service,
                    "pod_name": _as_str(row.get("pod_name")),
                    "namespace": _as_str(row.get("namespace")),
                    "level": level,
                    "message": message,
                    "trace_id": trace_val,
                    "span_id": _as_str(row.get("span_id")),
                    "request_id": request_val,
                }
            )

        duration_ms = int((time.perf_counter() - start_at) * 1000)
        detail = {
            "rows": len(normalized_rows),
            "source": "storage",
            "request_id": bool(request_id),
            "trace_id": bool(trace_id),
            "service_name": service_name or "",
            "source_service": source_service or "",
            "target_service": target_service or "",
        }
        return normalized_rows, ToolExecution(name=self.name, success=True, duration_ms=duration_ms, detail=detail)


class TraceQueryTool:
    """Tool: query trace spans when trace_id exists."""

    name = "trace_query"

    def __init__(self, storage_adapter: Any):
        self.storage_adapter = storage_adapter
        self.query_api_base = _as_str(os.getenv("AI_AGENT_QUERY_API_BASE")).rstrip("/")
        self.query_api_timeout_seconds = max(1, _as_int(os.getenv("AI_AGENT_QUERY_API_TIMEOUT_SECONDS"), 6))

    def _run_via_query_api(
        self,
        *,
        trace_id: str,
        start_time: datetime,
        end_time: datetime,
        limit: int,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        if not self.query_api_base:
            raise RuntimeError("query_api_base_not_configured")
        if not trace_id:
            return [], {"source": "query_api", "reason": "trace_id_missing"}

        safe_trace = quote(trace_id, safe="")
        params = urlencode({"limit": max(20, min(_as_int(limit, 300), 1200))})
        url = f"{self.query_api_base}/api/v1/traces/{safe_trace}/spans?{params}"
        req = Request(url, method="GET")
        req.add_header("Accept", "application/json")
        with urlopen(req, timeout=self.query_api_timeout_seconds) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="ignore"))
        rows = payload if isinstance(payload, list) else []

        normalized_rows: List[Dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            timestamp_text = _as_str(row.get("timestamp"))
            parsed_ts = _parse_timestamp(timestamp_text)
            if parsed_ts is not None and (parsed_ts < start_time or parsed_ts > end_time):
                continue
            normalized_rows.append(
                {
                    "timestamp": timestamp_text,
                    "trace_id": _as_str(row.get("trace_id")),
                    "span_id": _as_str(row.get("span_id")),
                    "parent_span_id": _as_str(row.get("parent_span_id")),
                    "service_name": _as_str(row.get("service_name"), "unknown"),
                    "operation_name": _as_str(row.get("operation_name")),
                    "status": _as_str(row.get("status"), "unknown"),
                    "duration_ms": _as_float(row.get("duration_ms"), 0.0),
                }
            )
        return normalized_rows, {"source": "query_api", "rows": len(normalized_rows)}

    def run(
        self,
        *,
        trace_id: str,
        start_time: datetime,
        end_time: datetime,
        limit: int,
    ) -> Tuple[List[Dict[str, Any]], ToolExecution]:
        start_at = time.perf_counter()
        if self.storage_adapter is None:
            if not self.query_api_base:
                return [], ToolExecution(
                    name=self.name,
                    success=False,
                    duration_ms=0,
                    detail={"reason": "storage_not_initialized"},
                )
        if not trace_id:
            return [], ToolExecution(
                name=self.name,
                success=True,
                duration_ms=0,
                detail={"reason": "trace_id_missing"},
            )

        if self.query_api_base:
            try:
                api_rows, api_detail = self._run_via_query_api(
                    trace_id=trace_id,
                    start_time=start_time,
                    end_time=end_time,
                    limit=limit,
                )
                duration_ms = int((time.perf_counter() - start_at) * 1000)
                return api_rows, ToolExecution(
                    name=self.name,
                    success=True,
                    duration_ms=duration_ms,
                    detail={**api_detail, "trace_id": trace_id},
                )
            except Exception as exc:
                logger.warning("trace_query query_api failed, fallback to storage: %s", exc)

        query = """
        SELECT
            toString(timestamp) AS timestamp,
            trace_id,
            span_id,
            parent_span_id,
            service_name,
            operation_name,
            status,
            duration_ms
        FROM logs.traces
        PREWHERE
            trace_id = {trace_id:String}
            AND timestamp >= toDateTime64({start_time:String}, 9, 'UTC')
            AND timestamp <= toDateTime64({end_time:String}, 9, 'UTC')
        ORDER BY timestamp ASC
        LIMIT {limit:Int32}
        """
        params = {
            "trace_id": trace_id,
            "start_time": _to_clickhouse_utc_text(start_time),
            "end_time": _to_clickhouse_utc_text(end_time),
            "limit": max(20, min(_as_int(limit, 300), 1200)),
        }

        try:
            rows = self.storage_adapter.execute_query(query, params) or []
        except Exception as exc:
            duration_ms = int((time.perf_counter() - start_at) * 1000)
            logger.warning("trace_query tool failed: %s", exc)
            return [], ToolExecution(
                name=self.name,
                success=False,
                duration_ms=duration_ms,
                detail={"error": str(exc)},
            )

        normalized_rows = [
            {
                "timestamp": _as_str(row.get("timestamp")),
                "trace_id": _as_str(row.get("trace_id")),
                "span_id": _as_str(row.get("span_id")),
                "parent_span_id": _as_str(row.get("parent_span_id")),
                "service_name": _as_str(row.get("service_name"), "unknown"),
                "operation_name": _as_str(row.get("operation_name")),
                "status": _as_str(row.get("status"), "unknown"),
                "duration_ms": _as_float(row.get("duration_ms"), 0.0),
            }
            for row in rows
        ]
        duration_ms = int((time.perf_counter() - start_at) * 1000)
        return normalized_rows, ToolExecution(
            name=self.name,
            success=True,
            duration_ms=duration_ms,
            detail={"rows": len(normalized_rows), "trace_id": trace_id, "source": "storage"},
        )


class WebSearchTool:
    """Tool: optional internet search (disabled unless endpoint/env enabled)."""

    name = "web_search"

    def __init__(self):
        self.enabled = str(os.getenv("AI_AGENT_WEB_SEARCH_ENABLED", "false")).lower() == "true"
        self.endpoint = _as_str(os.getenv("AI_AGENT_WEB_SEARCH_ENDPOINT"))
        self.timeout_seconds = max(1, _as_int(os.getenv("AI_AGENT_WEB_SEARCH_TIMEOUT_SECONDS"), 6))

    def run(self, *, query: str, limit: int = 3) -> Tuple[List[Dict[str, str]], ToolExecution]:
        start_at = time.perf_counter()
        if not self.enabled or not self.endpoint:
            return [], ToolExecution(
                name=self.name,
                success=True,
                duration_ms=0,
                detail={"enabled": False},
            )

        encoded_query = urlencode({"q": query, "limit": max(1, min(limit, 10))})
        url = f"{self.endpoint}?{encoded_query}"
        req = Request(url, method="GET")
        req.add_header("Accept", "application/json")

        try:
            with urlopen(req, timeout=self.timeout_seconds) as resp:
                payload = json.loads(resp.read().decode("utf-8", errors="ignore"))
        except Exception as exc:
            duration_ms = int((time.perf_counter() - start_at) * 1000)
            logger.warning("web_search tool failed: %s", exc)
            return [], ToolExecution(
                name=self.name,
                success=False,
                duration_ms=duration_ms,
                detail={"error": str(exc)},
            )

        items = payload.get("items") if isinstance(payload, dict) else []
        normalized_items: List[Dict[str, str]] = []
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            title = _as_str(item.get("title"))
            url_text = _as_str(item.get("url"))
            snippet = _as_str(item.get("snippet"))
            if not (title or url_text or snippet):
                continue
            normalized_items.append(
                {"title": title, "url": url_text, "snippet": _short_message(snippet, max_length=300)}
            )

        duration_ms = int((time.perf_counter() - start_at) * 1000)
        return normalized_items, ToolExecution(
            name=self.name,
            success=True,
            duration_ms=duration_ms,
            detail={"rows": len(normalized_items)},
        )


@dataclass
class AgentPreparation:
    """Prepared analysis payload generated by request-flow agent."""

    log_content: str
    context: Dict[str, Any]
    request_flow: Dict[str, Any]
    tool_runs: List[ToolExecution]
    related_logs: List[Dict[str, Any]]
    web_findings: List[Dict[str, str]]
    notice: str = ""


class RequestFlowAgent:
    """Agent orchestration for request/response flow reconstruction."""

    def __init__(self, storage_adapter: Any):
        self.storage_adapter = storage_adapter
        self.logs_tool = LogsQueryTool(storage_adapter)
        self.trace_tool = TraceQueryTool(storage_adapter)
        self.web_tool = WebSearchTool()
        self.window_minutes = max(1, _as_int(os.getenv("AI_AGENT_TIME_WINDOW_MINUTES"), 5))
        self.log_fetch_limit = max(40, _as_int(os.getenv("AI_AGENT_LOG_FETCH_LIMIT"), 240))
        self.log_inject_limit = max(10, _as_int(os.getenv("AI_AGENT_LOG_INJECT_LIMIT"), 80))
        self.log_raw_limit = max(6, _as_int(os.getenv("AI_AGENT_LOG_RAW_LIMIT"), 30))
        self.trace_fetch_limit = max(40, _as_int(os.getenv("AI_AGENT_TRACE_FETCH_LIMIT"), 500))

    def prepare_analysis_input(
        self,
        *,
        log_content: str,
        service_name: str = "",
        context: Optional[Dict[str, Any]] = None,
    ) -> AgentPreparation:
        safe_context = dict(context or {})
        input_tz = _resolve_context_timezone(safe_context)
        anchor_dt = self._resolve_anchor_timestamp(
            log_content=log_content,
            context=safe_context,
            input_tz=input_tz,
        )
        start_time = anchor_dt - timedelta(minutes=self.window_minutes)
        end_time = anchor_dt + timedelta(minutes=self.window_minutes)

        trace_id = self._extract_trace_id(log_content=log_content, context=safe_context)
        request_id = self._extract_request_id(log_content=log_content, context=safe_context)
        normalized_service = _as_str(service_name or safe_context.get("service_name"))
        source_service = _as_str(
            safe_context.get("source_service")
            or safe_context.get("source_service_name")
            or safe_context.get("sourceService")
        )
        target_service = _as_str(
            safe_context.get("target_service")
            or safe_context.get("target_service_name")
            or safe_context.get("targetService")
        )

        related_logs, logs_run = self.logs_tool.run(
            start_time=start_time,
            end_time=end_time,
            request_id=request_id,
            trace_id=trace_id,
            service_name=normalized_service,
            source_service=source_service,
            target_service=target_service,
            limit=self.log_fetch_limit,
        )
        pull_mode = _as_str(safe_context.get("pull_mode"), "auto_correlation").strip().lower()
        manual_before = max(0, _as_int(safe_context.get("manual_before"), 10))
        manual_after = max(0, _as_int(safe_context.get("manual_after"), 10))
        if pull_mode == "manual_context":
            related_logs = self._apply_manual_context_window(
                related_logs=related_logs,
                anchor_dt=anchor_dt,
                before_count=manual_before,
                after_count=manual_after,
            )

        if not trace_id:
            trace_id = self._pick_trace_id_from_logs(related_logs)

        trace_spans, trace_run = self.trace_tool.run(
            trace_id=trace_id,
            start_time=start_time,
            end_time=end_time,
            limit=self.trace_fetch_limit,
        )

        request_flow = self._build_request_flow(
            related_logs=related_logs,
            trace_spans=trace_spans,
            service_name=normalized_service,
            request_id=request_id,
            trace_id=trace_id,
            anchor_dt=anchor_dt,
        )

        enable_web_search = _as_bool(safe_context.get("enable_web_search"), default=False)
        web_findings: List[Dict[str, str]] = []
        web_run = ToolExecution(name=self.web_tool.name, success=True, duration_ms=0, detail={"enabled": False})
        if enable_web_search:
            web_query = self._build_web_query(request_flow=request_flow, service_name=normalized_service)
            web_findings, web_run = self.web_tool.run(query=web_query, limit=3)

        selected_logs = _prioritize_logs_for_injection(related_logs, self.log_inject_limit)
        agent_context = dict(safe_context)
        agent_context.update(
            {
                "trace_id": trace_id or _as_str(safe_context.get("trace_id")),
                "request_id": request_id or _as_str(safe_context.get("request_id")),
                "service_name": normalized_service or _as_str(safe_context.get("service_name")),
                "agent_mode": "request_flow",
                "request_flow": request_flow,
                "agent_related_logs": [
                    {
                        "id": row.get("id"),
                        "timestamp": row.get("timestamp"),
                        "level": row.get("level"),
                        "service_name": row.get("service_name"),
                        "message": _compact_traceback_message(
                            row.get("message"),
                            max_length=900,
                            line_limit_head=12,
                            line_limit_tail=20,
                        ),
                        "trace_id": row.get("trace_id"),
                        "request_id": row.get("request_id"),
                    }
                    for row in selected_logs[: self.log_raw_limit]
                ],
                "agent_tool_runs": [
                    {"name": run.name, "success": run.success, "duration_ms": run.duration_ms, "detail": run.detail}
                    for run in (logs_run, trace_run, web_run)
                ],
                "request_flow_window_minutes": self.window_minutes,
                "request_flow_window_start": start_time.isoformat().replace("+00:00", "Z"),
                "request_flow_window_end": end_time.isoformat().replace("+00:00", "Z"),
                "request_flow_input_timezone": str(input_tz),
            }
        )
        if web_findings:
            agent_context["agent_web_findings"] = web_findings
        integrity = self._build_log_integrity(
            related_logs=selected_logs,
            context=safe_context,
        )
        agent_context["log_integrity"] = integrity

        enhanced_input = self._build_augmented_input(
            base_log_content=log_content,
            request_flow=request_flow,
            related_logs=selected_logs,
        )

        notice = (
            f"后端Agent已在 ±{self.window_minutes} 分钟窗口内补充 {len(selected_logs)} 条关联日志"
            f"{'（含 trace 路径）' if trace_spans else ''}"
        )
        if bool(integrity.get("partial")):
            missing = ", ".join([_as_str(item) for item in _as_list(integrity.get("missing_components")) if _as_str(item)])
            notice = f"{notice}；检测到部分关联日志缺失（{missing or 'unknown'}）"
        return AgentPreparation(
            log_content=enhanced_input,
            context=agent_context,
            request_flow=request_flow,
            tool_runs=[logs_run, trace_run, web_run],
            related_logs=selected_logs,
            web_findings=web_findings,
            notice=notice,
        )

    def augment_result(self, raw_result: Dict[str, Any], preparation: AgentPreparation) -> Dict[str, Any]:
        """Inject deterministic agent evidence into model/rule result."""
        result = dict(raw_result or {})
        if result.get("data_flow") is None and result.get("dataFlow") is None:
            result["data_flow"] = {
                "summary": _as_str(preparation.request_flow.get("summary")),
                "path": preparation.request_flow.get("path") or [],
                "evidence": preparation.request_flow.get("evidence") or [],
                "confidence": _as_float(preparation.request_flow.get("confidence"), 0.0),
            }

        if result.get("root_causes") is None and result.get("rootCauses") is None:
            root_hints = preparation.request_flow.get("root_cause_hints") or []
            if root_hints:
                result["root_causes"] = root_hints

        if preparation.web_findings:
            similar_cases = result.get("similar_cases")
            if not isinstance(similar_cases, list):
                similar_cases = []
            for item in preparation.web_findings:
                title = _as_str(item.get("title"))
                snippet = _as_str(item.get("snippet"))
                url_text = _as_str(item.get("url"))
                if not (title or snippet):
                    continue
                description = snippet
                if url_text:
                    description = f"{snippet} ({url_text})".strip()
                similar_cases.append({"title": title or "external-reference", "description": description})
            result["similar_cases"] = similar_cases[:8]

        result["agent"] = {
            "mode": "request_flow",
            "notice": preparation.notice,
            "tool_runs": [
                {"name": run.name, "success": run.success, "duration_ms": run.duration_ms, "detail": run.detail}
                for run in preparation.tool_runs
            ],
            "related_log_count": len(preparation.related_logs),
            "log_integrity": preparation.context.get("log_integrity", {}),
        }
        return result

    def _apply_manual_context_window(
        self,
        *,
        related_logs: List[Dict[str, Any]],
        anchor_dt: datetime,
        before_count: int,
        after_count: int,
    ) -> List[Dict[str, Any]]:
        if not related_logs:
            return []
        ordered: List[tuple[datetime, Dict[str, Any]]] = []
        for row in related_logs:
            parsed = _parse_timestamp(row.get("timestamp"), default_tz=timezone.utc)
            if parsed is None:
                continue
            ordered.append((parsed, row))
        if not ordered:
            return related_logs[: max(1, before_count + after_count + 1)]
        ordered.sort(key=lambda item: item[0])
        nearest_index = min(
            range(len(ordered)),
            key=lambda idx: abs((ordered[idx][0] - anchor_dt).total_seconds()),
        )
        start = max(0, nearest_index - before_count)
        end = min(len(ordered), nearest_index + after_count + 1)
        return [ordered[idx][1] for idx in range(start, end)]

    def _build_log_integrity(
        self,
        *,
        related_logs: List[Dict[str, Any]],
        context: Dict[str, Any],
    ) -> Dict[str, Any]:
        expected_components = []
        for item in _as_list(context.get("expected_components")):
            text = _as_str(item).strip()
            if text and text not in expected_components:
                expected_components.append(text)
        if not expected_components:
            return {
                "partial": False,
                "missing_components": [],
                "next_action": "continue",
                "message": "integrity_check_skipped",
            }
        seen_services = {
            _as_str(row.get("service_name")).strip().lower()
            for row in related_logs
            if _as_str(row.get("service_name")).strip()
        }
        missing = [
            item for item in expected_components
            if item.lower() not in seen_services
        ]
        allow_partial = _as_bool(context.get("allow_partial"), default=False)
        partial = bool(missing)
        next_action = "continue" if (allow_partial or not partial) else "repull_required"
        message = "完整性校验通过"
        if partial:
            message = "部分关联日志未获取，是否继续分析？"
        return {
            "partial": partial,
            "missing_components": missing,
            "next_action": next_action,
            "message": message,
        }

    def _resolve_anchor_timestamp(
        self,
        *,
        log_content: str,
        context: Dict[str, Any],
        input_tz: Optional[tzinfo] = None,
    ) -> datetime:
        effective_tz = input_tz or _resolve_context_timezone(context)
        candidates = [
            context.get("source_log_timestamp"),
            context.get("timestamp"),
            context.get("related_log_anchor_timestamp"),
        ]
        for candidate in candidates:
            parsed = _parse_timestamp(candidate, default_tz=effective_tz)
            if parsed:
                return parsed
        text_match = _TIMESTAMP_REGEX.search(_as_str(log_content))
        if text_match:
            parsed = _parse_timestamp(text_match.group(0), default_tz=effective_tz)
            if parsed:
                return parsed
        return datetime.now(timezone.utc)

    def _extract_trace_id(self, *, log_content: str, context: Dict[str, Any]) -> str:
        for key in _TRACE_ID_KEYS:
            direct = _as_str(context.get(key))
            if direct:
                return direct
            if "." in key:
                nested = _extract_dot_path(context, key)
                if nested:
                    return nested
        match = _TRACE_ID_REGEX.search(_as_str(log_content))
        if match:
            return _as_str(match.group(1))
        return ""

    def _extract_request_id(self, *, log_content: str, context: Dict[str, Any]) -> str:
        for key in _REQUEST_ID_KEYS:
            direct = _as_str(context.get(key))
            if direct:
                return direct
            if "." in key:
                nested = _extract_dot_path(context, key)
                if nested:
                    return nested
        match = _REQUEST_ID_REGEX.search(_as_str(log_content))
        if match:
            return _as_str(match.group(1))
        req_match = _REQ_PREFIX_REGEX.search(_as_str(log_content))
        if req_match:
            return _as_str(req_match.group(1))
        return ""

    def _pick_trace_id_from_logs(self, related_logs: List[Dict[str, Any]]) -> str:
        trace_counter: Dict[str, int] = {}
        for row in related_logs:
            trace_id = _as_str(row.get("trace_id"))
            if not trace_id:
                continue
            trace_counter[trace_id] = trace_counter.get(trace_id, 0) + 1
        if not trace_counter:
            return ""
        return sorted(trace_counter.items(), key=lambda item: item[1], reverse=True)[0][0]

    def _build_request_flow(
        self,
        *,
        related_logs: List[Dict[str, Any]],
        trace_spans: List[Dict[str, Any]],
        service_name: str,
        request_id: str,
        trace_id: str,
        anchor_dt: datetime,
    ) -> Dict[str, Any]:
        path: List[Dict[str, Any]] = []
        evidence: List[str] = []
        root_cause_hints: List[str] = []

        if trace_spans:
            for index, span in enumerate(trace_spans[:12], start=1):
                status_raw = _as_str(span.get("status"), "unknown").lower()
                status = "error" if status_raw in {"error", "failed", "2"} else "ok"
                duration_ms = round(_as_float(span.get("duration_ms"), 0.0), 2)
                operation = _as_str(span.get("operation_name"), "span")
                service = _as_str(span.get("service_name"), "unknown")
                entry = {
                    "step": index,
                    "component": service,
                    "operation": operation,
                    "status": status,
                    "evidence": f"trace span={_as_str(span.get('span_id'))}",
                }
                if duration_ms > 0:
                    entry["latency_ms"] = duration_ms
                path.append(entry)
                if status == "error":
                    evidence.append(f"{service}/{operation} span status={_as_str(span.get('status'))}")
        else:
            ordered_logs = sorted(related_logs, key=lambda item: _as_str(item.get("timestamp")))
            last_service = ""
            for row in ordered_logs:
                service = _as_str(row.get("service_name"), "unknown")
                if service == last_service and path:
                    continue
                level = _normalize_level(row.get("level"))
                status = "error" if level in _ERROR_LEVELS else "ok"
                path.append(
                    {
                        "step": len(path) + 1,
                        "component": service,
                        "operation": _short_message(row.get("message"), max_length=96),
                        "status": status,
                        "evidence": f"log id={_as_str(row.get('id'))}",
                    }
                )
                last_service = service
                if len(path) >= 12:
                    break

        error_rows = [
            row for row in related_logs if _normalize_level(row.get("level")) in _ERROR_LEVELS
        ]
        for item in error_rows[:3]:
            message = _short_message(item.get("message"), max_length=180)
            service = _as_str(item.get("service_name"), "unknown")
            evidence.append(f"{service}: {message}")
            root_cause_hints.append(message)

        unique_services = []
        seen_services = set()
        for row in related_logs:
            service = _as_str(row.get("service_name"), "unknown")
            if service in seen_services:
                continue
            seen_services.add(service)
            unique_services.append(service)

        summary_parts = []
        if request_id:
            summary_parts.append(f"request_id={request_id}")
        if trace_id:
            summary_parts.append(f"trace_id={trace_id}")
        if unique_services:
            summary_parts.append(f"service_path={' -> '.join(unique_services[:8])}")
        if not summary_parts and service_name:
            summary_parts.append(f"service={service_name}")
        summary_text = " | ".join(summary_parts) if summary_parts else "未提取到明确调用路径，已基于时间窗口进行关联"

        error_node: Dict[str, Any] = {}
        if error_rows:
            first_error = error_rows[0]
            error_node = {
                "service_name": _as_str(first_error.get("service_name"), "unknown"),
                "timestamp": _as_str(first_error.get("timestamp")),
                "message": _short_message(first_error.get("message"), max_length=260),
                "level": _normalize_level(first_error.get("level")),
            }

        return {
            "summary": summary_text,
            "path": path,
            "evidence": evidence[:8],
            "confidence": 0.9 if trace_spans else (0.76 if related_logs else 0.42),
            "window_minutes": self.window_minutes,
            "anchor_time": anchor_dt.isoformat().replace("+00:00", "Z"),
            "service_count": len(unique_services),
            "log_count": len(related_logs),
            "trace_span_count": len(trace_spans),
            "error_node": error_node,
            "root_cause_hints": root_cause_hints[:4],
        }

    def _build_augmented_input(
        self,
        *,
        base_log_content: str,
        request_flow: Dict[str, Any],
        related_logs: List[Dict[str, Any]],
    ) -> str:
        base = _as_str(base_log_content)
        summary = _as_str(request_flow.get("summary"))
        window_minutes = _as_int(request_flow.get("window_minutes"), self.window_minutes)
        raw_lines = []
        for row in related_logs[: self.log_raw_limit]:
            timestamp = _as_str(row.get("timestamp"))
            level = _normalize_level(row.get("level"))
            service = _as_str(row.get("service_name"), "unknown")
            message = _compact_traceback_message(
                row.get("message"),
                max_length=1200,
                line_limit_head=14,
                line_limit_tail=24,
            )
            raw_lines.append(f"[{timestamp}] [{level}] [{service}] {message}")

        sections = [
            base,
            "",
            "[agent-request-flow-summary]",
            f"window=±{window_minutes}m",
            f"summary={summary}",
            f"log_count={_as_int(request_flow.get('log_count'), 0)}",
            f"trace_span_count={_as_int(request_flow.get('trace_span_count'), 0)}",
            "",
            "[agent-related-logs]",
            *raw_lines,
        ]
        return "\n".join([line for line in sections if line is not None]).strip()

    def _build_web_query(self, *, request_flow: Dict[str, Any], service_name: str) -> str:
        error_node = request_flow.get("error_node") if isinstance(request_flow.get("error_node"), dict) else {}
        message = _as_str((error_node or {}).get("message"))
        problem_terms = []
        if service_name:
            problem_terms.append(service_name)
        if message:
            problem_terms.append(message)
        if not problem_terms:
            problem_terms.append(_as_str(request_flow.get("summary"), "distributed tracing request flow issue"))
        return " ".join(problem_terms)[:300]


_request_flow_agent: Optional[RequestFlowAgent] = None
_request_flow_agent_storage: Any = None


def get_request_flow_agent(storage_adapter: Any) -> RequestFlowAgent:
    """Get or rebuild singleton request-flow agent."""
    global _request_flow_agent, _request_flow_agent_storage
    if _request_flow_agent is None or storage_adapter is not _request_flow_agent_storage:
        _request_flow_agent = RequestFlowAgent(storage_adapter)
        _request_flow_agent_storage = storage_adapter
    return _request_flow_agent
