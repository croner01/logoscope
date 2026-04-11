"""
队列信号解析工具（metrics / traces）
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from utils.otlp import parse_otlp_attributes

logger = logging.getLogger(__name__)


def infer_data_type(subject: str, headers: Optional[Dict[str, str]]) -> str:
    """
    推断消息信号类型（logs / metrics / traces）。

    优先级：
    1. 消息头 `data_type`
    2. stream 名称
    3. 默认 logs
    """
    header_data_type = str((headers or {}).get("data_type") or "").strip().lower()
    if header_data_type:
        if header_data_type in {"logs", "metrics", "traces"}:
            return header_data_type
        return "unknown"

    stream_name = str(subject or "").strip().lower()
    if "logs" in stream_name:
        return "logs"
    if "metrics" in stream_name:
        return "metrics"
    if "traces" in stream_name:
        return "traces"
    return "logs"


def _safe_json_loads(raw: Any) -> Any:
    """安全解析 JSON；失败时返回 None。"""
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return None

    try:
        return json.loads(raw)
    except Exception:
        return None


def _unwrap_otlp_payload(message_body: Dict[str, Any], root_key: str) -> Dict[str, Any]:
    """
    兼容队列包装格式，提取 OTLP payload。

    支持：
    1. 直接 OTLP: `{root_key: [...]}`
    2. 包装 payload: `{"payload": {...}}`
    3. 包装 raw_payload: `{"raw_payload": "...json..."}`
    """
    if root_key in message_body:
        root_value = message_body.get(root_key)
        if isinstance(root_value, list):
            return message_body
        if isinstance(root_value, dict):
            return {root_key: [root_value]}

    payload = message_body.get("payload")
    parsed_payload = _safe_json_loads(payload)
    if isinstance(parsed_payload, dict) and root_key in parsed_payload:
        root_value = parsed_payload.get(root_key)
        if isinstance(root_value, list):
            return parsed_payload
        if isinstance(root_value, dict):
            return {root_key: [root_value]}

    raw_payload = message_body.get("raw_payload")
    parsed_raw_payload = _safe_json_loads(raw_payload)
    if isinstance(parsed_raw_payload, dict) and root_key in parsed_raw_payload:
        root_value = parsed_raw_payload.get(root_key)
        if isinstance(root_value, list):
            return parsed_raw_payload
        if isinstance(root_value, dict):
            return {root_key: [root_value]}

    return {}


def _to_iso8601_from_unix_nano(raw_ns: Any) -> str:
    """Unix 纳秒时间戳转 ISO8601。"""
    try:
        ns = int(raw_ns or 0)
        if ns > 0:
            dt = datetime.fromtimestamp(ns / 1_000_000_000, tz=timezone.utc)
            return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    except Exception as exc:
        logger.debug("Invalid unix nano timestamp: %r (%s)", raw_ns, exc)
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _datetime_to_unix_nano(value: datetime) -> int:
    """datetime 转 Unix 纳秒，避免浮点精度误差。"""
    dt = value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = dt.astimezone(timezone.utc) - epoch
    return max(
        delta.days * 86_400 * 1_000_000_000
        + delta.seconds * 1_000_000_000
        + delta.microseconds * 1_000,
        0,
    )


def _to_unix_nano(raw_ns: Any) -> int:
    """安全解析 Unix 纳秒时间戳，兼容秒/毫秒/微秒/纳秒及 ISO8601。"""
    if raw_ns is None:
        return 0

    if isinstance(raw_ns, datetime):
        return _datetime_to_unix_nano(raw_ns)

    text = str(raw_ns).strip()
    if not text:
        return 0

    # 纯数字：按量级自动识别单位
    numeric_text = text.lstrip("+-")
    if numeric_text.replace(".", "", 1).isdigit():
        if "." not in numeric_text:
            try:
                numeric_value = int(text)
            except Exception:
                numeric_value = 0
            if numeric_value <= 0:
                return 0

            absolute_value = abs(numeric_value)
            if absolute_value < 10**11:  # seconds
                return numeric_value * 1_000_000_000
            if absolute_value < 10**14:  # milliseconds
                return numeric_value * 1_000_000
            if absolute_value < 10**17:  # microseconds
                return numeric_value * 1_000
            return numeric_value  # nanoseconds

        try:
            numeric_decimal = Decimal(text)
        except (InvalidOperation, ValueError):
            numeric_decimal = Decimal(0)
        if numeric_decimal <= 0:
            return 0

        absolute_value = abs(numeric_decimal)
        if absolute_value < Decimal("1e11"):  # seconds
            return int(numeric_decimal * Decimal("1000000000"))
        if absolute_value < Decimal("1e14"):  # milliseconds
            return int(numeric_decimal * Decimal("1000000"))
        if absolute_value < Decimal("1e17"):  # microseconds
            return int(numeric_decimal * Decimal("1000"))
        return int(numeric_decimal)  # nanoseconds

    normalized = text
    if " " in normalized and "T" not in normalized:
        normalized = normalized.replace(" ", "T")
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"

    try:
        dt = datetime.fromisoformat(normalized)
        return _datetime_to_unix_nano(dt)
    except Exception:
        return 0


def _normalize_otel_id(raw_value: Any, expected_bytes: int) -> str:
    """Normalize OTLP id to lowercase hex; accept hex or base64 bytes."""
    if raw_value is None:
        return ""

    text = str(raw_value).strip()
    if not text:
        return ""

    if text.startswith("0x") or text.startswith("0X"):
        text = text[2:]

    compact_hex = text.replace("-", "").lower()
    if len(compact_hex) == expected_bytes * 2:
        try:
            bytes.fromhex(compact_hex)
            return compact_hex
        except ValueError:
            pass

    candidates = [text, text.replace("-", "+").replace("_", "/")]
    for candidate in candidates:
        padded = candidate + ("=" * ((4 - (len(candidate) % 4)) % 4))
        try:
            decoded = base64.b64decode(padded, validate=True)
        except (binascii.Error, ValueError):
            continue
        if len(decoded) == expected_bytes:
            return decoded.hex()

    return text


def _normalize_otel_trace_id(raw_value: Any) -> str:
    return _normalize_otel_id(raw_value, expected_bytes=16)


def _normalize_otel_span_id(raw_value: Any) -> str:
    return _normalize_otel_id(raw_value, expected_bytes=8)


def parse_metrics_points(message_body: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    从队列消息解析 metrics 数据点。

    返回结果可直接传入 `StorageAdapter.save_metrics`。
    """
    payload = _unwrap_otlp_payload(message_body, root_key="resourceMetrics")
    if not payload:
        return []

    points: List[Dict[str, Any]] = []
    for resource_metrics in payload.get("resourceMetrics", []):
        resource = resource_metrics.get("resource", {})
        resource_attrs = parse_otlp_attributes(resource.get("attributes", []) or [])
        service_name = (
            str(resource_attrs.get("service.name") or "").strip()
            or str(resource_attrs.get("service_name") or "").strip()
            or "unknown"
        )

        for scope_metrics in resource_metrics.get("scopeMetrics", []):
            for metric in scope_metrics.get("metrics", []):
                metric_name = str(metric.get("name") or "").strip()
                if not metric_name:
                    continue

                points.extend(
                    _parse_single_metric(metric, metric_name, service_name, resource_attrs)
                )
    return points


def _parse_single_metric(
    metric: Dict[str, Any],
    metric_name: str,
    service_name: str,
    resource_attrs: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """解析单个 metric。"""
    if "gauge" in metric:
        return _parse_number_data_points(
            metric.get("gauge", {}),
            metric_name=metric_name,
            metric_type="gauge",
            service_name=service_name,
            resource_attrs=resource_attrs,
        )

    if "sum" in metric:
        sum_data = metric.get("sum", {})
        metric_type = "counter" if bool(sum_data.get("isMonotonic")) else "updowncounter"
        return _parse_number_data_points(
            sum_data,
            metric_name=metric_name,
            metric_type=metric_type,
            service_name=service_name,
            resource_attrs=resource_attrs,
        )

    if "histogram" in metric:
        return _parse_distribution_data_points(
            metric.get("histogram", {}),
            metric_name=metric_name,
            metric_type="histogram",
            service_name=service_name,
            resource_attrs=resource_attrs,
        )

    if "summary" in metric:
        return _parse_distribution_data_points(
            metric.get("summary", {}),
            metric_name=metric_name,
            metric_type="summary",
            service_name=service_name,
            resource_attrs=resource_attrs,
        )

    return []


def _parse_number_data_points(
    metric_data: Dict[str, Any],
    metric_name: str,
    metric_type: str,
    service_name: str,
    resource_attrs: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """解析数值型数据点（gauge/sum）。"""
    points: List[Dict[str, Any]] = []
    for data_point in metric_data.get("dataPoints", []):
        value = None
        if "asDouble" in data_point:
            value = float(data_point.get("asDouble"))
        elif "asInt" in data_point:
            value = float(data_point.get("asInt"))

        if value is None:
            continue

        attrs = parse_otlp_attributes(data_point.get("attributes", []) or [])
        merged_attrs = {**resource_attrs, **attrs}
        timestamp = _to_iso8601_from_unix_nano(data_point.get("timeUnixNano"))

        points.append(
            {
                "metric_name": metric_name,
                "metric_type": metric_type,
                "timestamp": timestamp,
                "value": value,
                "attributes": merged_attrs,
                "service_name": service_name,
            }
        )
    return points


def _parse_distribution_data_points(
    metric_data: Dict[str, Any],
    metric_name: str,
    metric_type: str,
    service_name: str,
    resource_attrs: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """解析分布型数据点（histogram/summary）。"""
    points: List[Dict[str, Any]] = []
    for data_point in metric_data.get("dataPoints", []):
        attrs = parse_otlp_attributes(data_point.get("attributes", []) or [])
        merged_attrs = {**resource_attrs, **attrs}
        timestamp = _to_iso8601_from_unix_nano(data_point.get("timeUnixNano"))

        if "sum" in data_point:
            value = float(data_point.get("sum") or 0.0)
        else:
            value = float(data_point.get("count") or 0.0)

        points.append(
            {
                "metric_name": metric_name,
                "metric_type": metric_type,
                "timestamp": timestamp,
                "value": value,
                "attributes": merged_attrs,
                "service_name": service_name,
            }
        )
    return points


def parse_trace_spans(message_body: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    从队列消息解析 traces span。

    返回结果可直接传入 `StorageAdapter.save_traces`。
    """
    payload = _unwrap_otlp_payload(message_body, root_key="resourceSpans")
    if not payload:
        return []

    spans: List[Dict[str, Any]] = []
    for resource_spans in payload.get("resourceSpans", []):
        resource = resource_spans.get("resource", {})
        resource_attrs = parse_otlp_attributes(resource.get("attributes", []) or [])
        service_name = (
            str(resource_attrs.get("service.name") or "").strip()
            or str(resource_attrs.get("service_name") or "").strip()
            or "unknown"
        )

        for scope_spans in resource_spans.get("scopeSpans", []):
            for span in scope_spans.get("spans", []):
                trace_id = _normalize_otel_trace_id(span.get("traceId") or span.get("trace_id"))
                span_id = _normalize_otel_span_id(span.get("spanId") or span.get("span_id"))
                if not trace_id or not span_id:
                    continue

                span_attrs = parse_otlp_attributes(span.get("attributes", []) or [])
                merged_attrs = {**resource_attrs, **span_attrs}
                status = _normalize_span_status(span.get("status", {}))
                parent_span_id = _normalize_otel_span_id(
                    span.get("parentSpanId") or span.get("parent_span_id") or ""
                )
                start_ns = _to_unix_nano(
                    span.get("startTimeUnixNano")
                    or span.get("start_time_unix_nano")
                    or span.get("start_time")
                )
                end_ns = _to_unix_nano(
                    span.get("endTimeUnixNano")
                    or span.get("end_time_unix_nano")
                    or span.get("end_time")
                )
                duration_ns = max(end_ns - start_ns, 0) if start_ns > 0 and end_ns > 0 else 0
                duration_ms = (float(duration_ns) / 1_000_000.0) if duration_ns > 0 else 0.0
                if start_ns > 0:
                    merged_attrs.setdefault("start_time_unix_nano", start_ns)
                if end_ns > 0:
                    merged_attrs.setdefault("end_time_unix_nano", end_ns)
                if duration_ns > 0:
                    merged_attrs.setdefault("duration_ns", duration_ns)
                    merged_attrs.setdefault("duration_ms", duration_ms)

                spans.append(
                    {
                        "trace_id": trace_id,
                        "span_id": span_id,
                        "parent_span_id": parent_span_id,
                        "service_name": service_name,
                        "operation_name": str(span.get("name") or span.get("operation_name") or ""),
                        "start_time": _to_iso8601_from_unix_nano(start_ns),
                        "span_kind": str(span.get("kind") or span.get("span_kind") or ""),
                        "status_code": status,
                        "duration_ns": duration_ns,
                        "duration_ms": duration_ms,
                        "tags": merged_attrs,
                    }
                )
    return spans


def _normalize_span_status(status: Any) -> str:
    """
    统一 span 状态字段。

    OTLP `status.code` 统一映射为 `STATUS_CODE_*`：
    - 0 -> STATUS_CODE_UNSET
    - 1 -> STATUS_CODE_OK
    - 2 -> STATUS_CODE_ERROR
    """
    if isinstance(status, dict):
        code = status.get("code")
    else:
        code = status

    if isinstance(code, str) and code.strip():
        value = code.strip().upper()
        if value in {"2", "ERROR", "STATUS_CODE_ERROR"}:
            return "STATUS_CODE_ERROR"
        if value in {"1", "OK", "STATUS_CODE_OK"}:
            return "STATUS_CODE_OK"
        if value in {"0", "UNSET", "STATUS_CODE_UNSET"}:
            return "STATUS_CODE_UNSET"
        return "STATUS_CODE_UNSET"

    try:
        numeric = int(code)
    except Exception:
        numeric = 0

    if numeric == 1:
        return "STATUS_CODE_OK"
    if numeric == 2:
        return "STATUS_CODE_ERROR"
    return "STATUS_CODE_UNSET"
