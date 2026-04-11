"""
OTLP Metrics 解析辅助函数（兼容历史测试）。
"""
import json
from datetime import datetime, timezone
from typing import Any, Dict, List


def _extract_any_value(value: Any) -> Any:
    """解析 OTLP AnyValue JSON 结构。"""
    if not isinstance(value, dict):
        return value
    if "stringValue" in value:
        return value.get("stringValue")
    if "intValue" in value:
        return value.get("intValue")
    if "doubleValue" in value:
        return value.get("doubleValue")
    if "boolValue" in value:
        return value.get("boolValue")
    if "kvlistValue" in value:
        values = value.get("kvlistValue", {}).get("values", [])
        return parse_otlp_attributes(values)
    if "arrayValue" in value:
        return [_extract_any_value(item) for item in value.get("arrayValue", {}).get("values", [])]
    return value


def parse_otlp_attributes(attributes_list: List[Dict[str, Any]]) -> Dict[str, Any]:
    """把 OTLP attributes 数组转为 dict。"""
    parsed: Dict[str, Any] = {}
    for item in attributes_list or []:
        key = str(item.get("key") or "").strip()
        if not key:
            continue
        parsed[key] = _extract_any_value(item.get("value"))
    return parsed


def _nanos_to_iso(time_unix_nano: Any) -> str:
    """纳秒时间戳转 ISO8601。"""
    try:
        nanos = int(time_unix_nano or 0)
    except (TypeError, ValueError):
        nanos = 0
    seconds = nanos / 1_000_000_000
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def parse_number_data_points(
    data: Dict[str, Any],
    metric_name: str,
    metric_type: str,
    service_name: str,
    resource_attrs: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """解析 Gauge/Sum 数据点。"""
    rows: List[Dict[str, Any]] = []
    for point in (data or {}).get("dataPoints", []):
        raw_value = None
        if "asDouble" in point:
            raw_value = point.get("asDouble")
        elif "asInt" in point:
            raw_value = point.get("asInt")
        if raw_value is None:
            continue
        attrs = dict(resource_attrs or {})
        attrs.update(parse_otlp_attributes(point.get("attributes", [])))
        rows.append(
            {
                "timestamp": _nanos_to_iso(point.get("timeUnixNano")),
                "metric_name": metric_name,
                "metric_type": metric_type,
                "service_name": service_name or str((resource_attrs or {}).get("service.name") or "unknown"),
                "value": float(raw_value),
                "attributes": attrs,
            }
        )
    return rows


def parse_histogram_data_points(
    data: Dict[str, Any],
    metric_name: str,
    service_name: str,
    resource_attrs: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """解析 Histogram 数据点。"""
    rows: List[Dict[str, Any]] = []
    for point in (data or {}).get("dataPoints", []):
        attrs = dict(resource_attrs or {})
        attrs.update(parse_otlp_attributes(point.get("attributes", [])))
        histogram_payload = {
            "count": int(point.get("count") or 0),
            "sum": float(point.get("sum") or 0.0),
            "bucket_counts": list(point.get("bucketCounts") or []),
            "explicit_bounds": list(point.get("explicitBounds") or []),
        }
        rows.append(
            {
                "timestamp": _nanos_to_iso(point.get("timeUnixNano")),
                "metric_name": metric_name,
                "metric_type": "histogram",
                "service_name": service_name or str((resource_attrs or {}).get("service.name") or "unknown"),
                "value": float(point.get("sum") or 0.0),
                "attributes": attrs,
                "histogram_data": json.dumps(histogram_payload, ensure_ascii=False),
            }
        )
    return rows


def parse_summary_data_points(
    data: Dict[str, Any],
    metric_name: str,
    service_name: str,
    resource_attrs: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """解析 Summary 数据点。"""
    rows: List[Dict[str, Any]] = []
    for point in (data or {}).get("dataPoints", []):
        attrs = dict(resource_attrs or {})
        attrs.update(parse_otlp_attributes(point.get("attributes", [])))
        summary_payload = {
            "count": int(point.get("count") or 0),
            "sum": float(point.get("sum") or 0.0),
            "quantile_values": list(point.get("quantileValues") or []),
        }
        rows.append(
            {
                "timestamp": _nanos_to_iso(point.get("timeUnixNano")),
                "metric_name": metric_name,
                "metric_type": "summary",
                "service_name": service_name or str((resource_attrs or {}).get("service.name") or "unknown"),
                "value": float(point.get("sum") or 0.0),
                "attributes": attrs,
                "summary_data": json.dumps(summary_payload, ensure_ascii=False),
            }
        )
    return rows


def parse_metric_data(metric: Dict[str, Any], scope_name: str, resource_attrs: Dict[str, Any]) -> List[Dict[str, Any]]:
    """按指标类型分派解析。"""
    metric_name = str(metric.get("name") or "")
    service_name = str((resource_attrs or {}).get("service.name") or "unknown")
    if "gauge" in metric:
        return parse_number_data_points(metric.get("gauge", {}), metric_name, "gauge", service_name, resource_attrs)
    if "sum" in metric:
        metric_type = "counter" if bool(metric.get("sum", {}).get("isMonotonic")) else "updowncounter"
        return parse_number_data_points(metric.get("sum", {}), metric_name, metric_type, service_name, resource_attrs)
    if "histogram" in metric:
        return parse_histogram_data_points(metric.get("histogram", {}), metric_name, service_name, resource_attrs)
    if "summary" in metric:
        return parse_summary_data_points(metric.get("summary", {}), metric_name, service_name, resource_attrs)
    return []


def process_otlp_metrics_json(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """处理 OTLP Metrics JSON，输出扁平化数据点列表。"""
    records: List[Dict[str, Any]] = []
    for resource_metric in (payload or {}).get("resourceMetrics", []):
        resource = resource_metric.get("resource", {})
        resource_attrs = parse_otlp_attributes(resource.get("attributes", []))
        for scope_metric in resource_metric.get("scopeMetrics", []):
            scope_name = str((scope_metric.get("scope") or {}).get("name") or "")
            for metric in scope_metric.get("metrics", []):
                records.extend(parse_metric_data(metric, scope_name, resource_attrs))
    return records

