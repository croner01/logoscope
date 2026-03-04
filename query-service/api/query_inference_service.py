"""Trace-lite and inference-quality domain services extracted from query_routes."""

from __future__ import annotations

import os
from typing import Any, Callable, Dict, List, Optional, Set, Tuple


def _read_positive_int_env(name: str, default_value: int, min_value: int = 1, max_value: int = 200000) -> int:
    """Read positive integer env with clamped fallback."""
    raw = os.getenv(name, str(default_value))
    try:
        parsed = int(str(raw).strip())
    except Exception:
        parsed = default_value
    return max(min_value, min(parsed, max_value))


TRACE_LITE_INFERRED_LOG_LIMIT_CAP = _read_positive_int_env("TRACE_LITE_INFERRED_LOG_LIMIT_CAP", 20000)
TRACE_LITE_PILOT_LOG_LIMIT = _read_positive_int_env("TRACE_LITE_PILOT_LOG_LIMIT", 20000)
INFERENCE_QUALITY_LOG_LIMIT = _read_positive_int_env("INFERENCE_QUALITY_LOG_LIMIT", 20000)
INFERENCE_QUALITY_MIN_FP_SAMPLE = _read_positive_int_env("INFERENCE_QUALITY_MIN_FP_SAMPLE", 5, min_value=1, max_value=5000)
INFERENCE_INCLUDE_ATTRS_SIGNAL = str(os.getenv("INFERENCE_INCLUDE_ATTRS_SIGNAL", "0")).strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
_INFERENCE_LOG_SIGNAL_PARTS: List[str] = [
    "notEmpty(trace_id)",
    "positionCaseInsensitiveUTF8(message, 'request_id') > 0",
    "positionCaseInsensitiveUTF8(message, 'x-request-id') > 0",
]
if INFERENCE_INCLUDE_ATTRS_SIGNAL:
    _INFERENCE_LOG_SIGNAL_PARTS.extend(
        [
            "positionCaseInsensitiveUTF8(attributes_json, 'request_id') > 0",
            "positionCaseInsensitiveUTF8(attributes_json, 'x-request-id') > 0",
        ]
    )
INFERENCE_LOG_SIGNAL_SQL = "(" + " OR ".join(_INFERENCE_LOG_SIGNAL_PARTS) + ")"


def _is_meaningful_service_name(value: Any) -> bool:
    """Check whether a service name is usable for pair-level quality comparison."""
    normalized = str(value or "").strip().lower()
    return bool(normalized and normalized != "unknown")


def query_trace_lite_inferred(
    *,
    storage_adapter: Any,
    time_window: str,
    source_service: Optional[str],
    target_service: Optional[str],
    namespace: Optional[str],
    limit: int,
    sanitize_interval_fn: Callable[[str, str], str],
    infer_trace_lite_fragments_fn: Callable[[List[Dict[str, Any]], float], Tuple[List[Dict[str, Any]], Dict[str, Any]]],
) -> Dict[str, Any]:
    """Query inferred trace-lite fragments from logs."""
    safe_time_window = sanitize_interval_fn(time_window, default_value="1 HOUR")
    prewhere_conditions = [f"timestamp > now() - INTERVAL {safe_time_window}"]
    if source_service:
        prewhere_conditions.append("service_name = {source_service:String}")
    if namespace:
        prewhere_conditions.append("namespace = {namespace:String}")
    prewhere_conditions.append(INFERENCE_LOG_SIGNAL_SQL)

    prewhere_clause = "PREWHERE " + " AND ".join(prewhere_conditions)
    query = f"""
    SELECT
        id,
        timestamp,
        service_name,
        namespace,
        message,
        trace_id,
        attributes_json
    FROM logs.logs
    {prewhere_clause}
    LIMIT {{query_limit:Int32}}
    """
    params: Dict[str, Any] = {
        "query_limit": min(max(limit * 50, 5000), TRACE_LITE_INFERRED_LOG_LIMIT_CAP),
    }
    if source_service:
        params["source_service"] = source_service
    if namespace:
        params["namespace"] = namespace

    rows = storage_adapter.execute_query(query, params)
    fragments, stats = infer_trace_lite_fragments_fn(rows, 2.0)

    if source_service:
        fragments = [item for item in fragments if item.get("source_service") == source_service]
    if target_service:
        fragments = [item for item in fragments if item.get("target_service") == target_service]

    fragments.sort(
        key=lambda item: (
            item.get("confidence", 0),
            item.get("sample_size", 0),
            item.get("last_seen") or "",
        ),
        reverse=True,
    )
    fragments = fragments[:limit]

    return {
        "data": fragments,
        "count": len(fragments),
        "time_window": safe_time_window,
        "stats": stats,
    }


def trace_lite_pilot_readiness(
    *,
    storage_adapter: Any,
    time_window: str,
    min_services: int,
    sanitize_interval_fn: Callable[[str, str], str],
    infer_trace_lite_fragments_fn: Callable[[List[Dict[str, Any]], float], Tuple[List[Dict[str, Any]], Dict[str, Any]]],
) -> Dict[str, Any]:
    """Evaluate trace-lite pilot readiness."""
    safe_time_window = sanitize_interval_fn(time_window, default_value="24 HOUR")
    query = f"""
    SELECT
        id,
        timestamp,
        service_name,
        namespace,
        message,
        trace_id,
        attributes_json
    FROM logs.logs
    PREWHERE timestamp > now() - INTERVAL {safe_time_window}
         AND {INFERENCE_LOG_SIGNAL_SQL}
    LIMIT {{query_limit:Int32}}
    """
    rows = storage_adapter.execute_query(query, {"query_limit": int(TRACE_LITE_PILOT_LOG_LIMIT)})
    fragments, stats = infer_trace_lite_fragments_fn(rows, 2.0)

    inferred_services: Set[str] = set()
    for fragment in fragments:
        inferred_services.add(fragment.get("source_service"))
        inferred_services.add(fragment.get("target_service"))

    service_pairs = [
        f"{fragment.get('source_service')}->{fragment.get('target_service')}"
        for fragment in fragments[:20]
    ]
    inferred_services_list = sorted(service for service in inferred_services if service and service != "unknown")
    return {
        "ready": len(inferred_services_list) >= min_services,
        "min_services": min_services,
        "inferred_service_count": len(inferred_services_list),
        "inferred_services": inferred_services_list[:50],
        "sample_pairs": service_pairs,
        "stats": stats,
    }


def inference_quality_metrics(
    *,
    storage_adapter: Any,
    time_window: str,
    sanitize_interval_fn: Callable[[str, str], str],
    infer_trace_lite_fragments_fn: Callable[[List[Dict[str, Any]], float], Tuple[List[Dict[str, Any]], Dict[str, Any]]],
) -> Dict[str, Any]:
    """Compute inference-quality metrics."""
    safe_time_window = sanitize_interval_fn(time_window, default_value="1 HOUR")
    logs_query = f"""
    SELECT
        id,
        timestamp,
        service_name,
        namespace,
        message,
        trace_id,
        attributes_json
    FROM logs.logs
    PREWHERE timestamp > now() - INTERVAL {safe_time_window}
         AND {INFERENCE_LOG_SIGNAL_SQL}
    LIMIT {{query_limit:Int32}}
    """
    log_rows = storage_adapter.execute_query(logs_query, {"query_limit": int(INFERENCE_QUALITY_LOG_LIMIT)})
    fragments, stats = infer_trace_lite_fragments_fn(log_rows, 2.0)

    inferred_pairs = {
        (item.get("source_service"), item.get("target_service"))
        for item in fragments
        if _is_meaningful_service_name(item.get("source_service"))
        and _is_meaningful_service_name(item.get("target_service"))
    }

    observed_query = f"""
    SELECT
        parent.service_name AS source_service,
        child.service_name AS target_service
    FROM (
        SELECT trace_id, parent_span_id, service_name
        FROM logs.traces
        PREWHERE timestamp > now() - INTERVAL {safe_time_window}
             AND notEmpty(trace_id)
             AND notEmpty(parent_span_id)
    ) AS child
    INNER JOIN (
        SELECT trace_id, span_id, service_name
        FROM logs.traces
        PREWHERE timestamp > now() - INTERVAL {safe_time_window}
             AND notEmpty(trace_id)
             AND notEmpty(span_id)
    ) AS parent
        ON child.trace_id = parent.trace_id
       AND child.parent_span_id = parent.span_id
    GROUP BY source_service, target_service
    """
    observed_rows = storage_adapter.execute_query(observed_query)
    observed_pairs = {
        (row.get("source_service"), row.get("target_service"))
        for row in observed_rows
        if _is_meaningful_service_name(row.get("source_service"))
        and _is_meaningful_service_name(row.get("target_service"))
    }

    has_observed_baseline = bool(observed_pairs)
    false_positive_count = 0
    direction_mismatch_count = 0
    if has_observed_baseline:
        observed_undirected_pairs = {tuple(sorted(pair)) for pair in observed_pairs}
        for pair in inferred_pairs:
            if pair in observed_pairs:
                continue
            if tuple(sorted(pair)) in observed_undirected_pairs:
                direction_mismatch_count += 1
                continue
            false_positive_count += 1

    service_count = len({row.get("service_name") for row in log_rows if row.get("service_name")})
    coverage = (len({service for pair in inferred_pairs for service in pair}) / service_count) if service_count else 0.0
    inferred_ratio = (
        len(inferred_pairs) / (len(observed_pairs) + len(inferred_pairs))
        if (observed_pairs or inferred_pairs)
        else 0.0
    )
    false_positive_rate_state = "ok"
    false_positive_rate_reason = "comparable"
    if inferred_pairs and not has_observed_baseline:
        false_positive_rate = 0.0
        false_positive_rate_state = "unknown"
        false_positive_rate_reason = "no_observed_baseline"
    elif inferred_pairs and len(inferred_pairs) < int(INFERENCE_QUALITY_MIN_FP_SAMPLE):
        false_positive_rate = 0.0
        false_positive_rate_state = "unknown"
        false_positive_rate_reason = "insufficient_inferred_sample"
    else:
        false_positive_rate = (false_positive_count / len(inferred_pairs)) if inferred_pairs else 0.0

    metrics = {
        "coverage": round(coverage, 3),
        "inferred_ratio": round(inferred_ratio, 3),
        "false_positive_rate": round(false_positive_rate, 3),
        "false_positive_rate_state": false_positive_rate_state,
        "false_positive_rate_reason": false_positive_rate_reason,
        "false_positive_rate_min_sample": int(INFERENCE_QUALITY_MIN_FP_SAMPLE),
        "has_observed_baseline": has_observed_baseline,
        "inferred_pairs": len(inferred_pairs),
        "observed_pairs": len(observed_pairs),
        "false_positive_count": false_positive_count,
        "direction_mismatch_count": direction_mismatch_count,
        "strategy": stats.get("strategy"),
    }
    return {
        "status": "ok",
        "time_window": time_window,
        "metrics": metrics,
    }


def inference_quality_alerts(
    *,
    metrics: Dict[str, Any],
    time_window: str,
    min_coverage: float,
    max_inferred_ratio: float,
    max_false_positive_rate: float,
    suppressed_metrics: Set[str],
) -> Dict[str, Any]:
    """Evaluate inference-quality alerts with suppression flags."""
    false_positive_state = str(metrics.get("false_positive_rate_state", "ok")).strip().lower()
    false_positive_comparable = false_positive_state != "unknown"

    rules = [
        ("coverage", metrics.get("coverage", 0.0) < min_coverage, f"coverage<{min_coverage:.2f}"),
        (
            "inferred_ratio",
            metrics.get("inferred_ratio", 0.0) > max_inferred_ratio,
            f"inferred_ratio>{max_inferred_ratio:.2f}",
        ),
        (
            "false_positive_rate",
            false_positive_comparable and metrics.get("false_positive_rate", 0.0) > max_false_positive_rate,
            f"false_positive_rate>{max_false_positive_rate:.2f}",
        ),
    ]

    alerts: List[Dict[str, Any]] = []
    for metric_name, triggered, expression in rules:
        if not triggered:
            continue
        alerts.append(
            {
                "metric": metric_name,
                "expression": expression,
                "value": metrics.get(metric_name),
                "triggered": True,
                "suppressed": metric_name in suppressed_metrics,
            }
        )

    return {
        "status": "ok",
        "time_window": time_window,
        "alerts": alerts,
        "active_alerts": sum(1 for item in alerts if not item.get("suppressed")),
        "suppressed_metrics": sorted(suppressed_metrics),
    }
