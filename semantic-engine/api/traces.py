"""
OTLP Traces 解析辅助函数（兼容历史测试）。
"""
import json
from datetime import datetime, timezone
from typing import Any, Dict, List


def _extract_any_value(value: Any) -> Any:
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
        return parse_otlp_attributes(value.get("kvlistValue", {}).get("values", []))
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


def _nanos_to_iso(value: Any) -> str:
    try:
        nanos = int(value or 0)
    except (TypeError, ValueError):
        nanos = 0
    seconds = nanos / 1_000_000_000
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_span_events(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    parsed_events: List[Dict[str, Any]] = []
    for event in events or []:
        parsed_events.append(
            {
                "timeUnixNano": event.get("timeUnixNano"),
                "name": event.get("name"),
                "attributes": parse_otlp_attributes(event.get("attributes", [])),
            }
        )
    return parsed_events


def parse_span_data(
    span: Dict[str, Any],
    service_name: str,
    pod_name: str,
    namespace: str,
    resource_attrs: Dict[str, Any],
) -> Dict[str, Any]:
    """解析单个 OTLP Span。"""
    start_ns = int(span.get("startTimeUnixNano") or 0)
    end_ns = int(span.get("endTimeUnixNano") or 0)
    duration_ms = max(0, int(round((end_ns - start_ns) / 1_000_000)))
    attrs = dict(resource_attrs or {})
    attrs.update(parse_otlp_attributes(span.get("attributes", [])))
    return {
        "trace_id": str(span.get("traceId") or ""),
        "span_id": str(span.get("spanId") or ""),
        "parent_span_id": str(span.get("parentSpanId") or ""),
        "operation_name": str(span.get("name") or ""),
        "span_kind": str(span.get("kind") or "INTERNAL"),
        "status_code": str((span.get("status") or {}).get("code") or "UNSET"),
        "status_message": str((span.get("status") or {}).get("message") or ""),
        "service_name": service_name or "unknown",
        "pod_name": pod_name or "unknown",
        "namespace": namespace or "default",
        "start_time": _nanos_to_iso(start_ns),
        "start_time_str": _nanos_to_iso(start_ns),
        "end_time": _nanos_to_iso(end_ns),
        "duration_ms": duration_ms,
        "tags": json.dumps(attrs, ensure_ascii=False),
        "events": json.dumps(_parse_span_events(span.get("events", [])), ensure_ascii=False),
        "links": json.dumps(list(span.get("links") or []), ensure_ascii=False),
    }


def _extract_k8s_context(resource_attrs: Dict[str, Any]) -> Dict[str, str]:
    kubernetes = resource_attrs.get("kubernetes")
    if isinstance(kubernetes, dict):
        return {
            "pod_name": str(kubernetes.get("pod_name") or kubernetes.get("pod") or "unknown"),
            "namespace": str(kubernetes.get("namespace_name") or kubernetes.get("namespace") or "default"),
        }
    return {
        "pod_name": str(resource_attrs.get("kubernetes.pod.name") or "unknown"),
        "namespace": str(resource_attrs.get("kubernetes.namespace.name") or "default"),
    }


def process_otlp_traces_json(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """处理 OTLP Traces JSON，输出扁平化 Span 列表。"""
    spans: List[Dict[str, Any]] = []
    for resource_span in (payload or {}).get("resourceSpans", []):
        resource = resource_span.get("resource", {})
        resource_attrs = parse_otlp_attributes(resource.get("attributes", []))
        service_name = str(resource_attrs.get("service.name") or "unknown")
        k8s = _extract_k8s_context(resource_attrs)
        for scope_span in resource_span.get("scopeSpans", []):
            for span in scope_span.get("spans", []):
                spans.append(
                    parse_span_data(
                        span=span,
                        service_name=service_name,
                        pod_name=k8s["pod_name"],
                        namespace=k8s["namespace"],
                        resource_attrs=resource_attrs,
                    )
                )
    return spans

