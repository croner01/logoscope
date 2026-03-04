"""Logs query domain services extracted from query_routes."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from fastapi import HTTPException

from api.query_params import sanitize_interval
from utils.pattern_aggregator import PatternAggregator

_QUERY_LOGS_DEFAULT_TIME_WINDOW_ENV = "QUERY_LOGS_DEFAULT_TIME_WINDOW"
_QUERY_LOGS_DEFAULT_TIME_WINDOW = "24 HOUR"


def _resolve_query_logs_default_window() -> str:
    """Resolve bounded default time window for logs queries."""
    raw_default = os.getenv(_QUERY_LOGS_DEFAULT_TIME_WINDOW_ENV, _QUERY_LOGS_DEFAULT_TIME_WINDOW)
    return sanitize_interval(str(raw_default or ""), default_value=_QUERY_LOGS_DEFAULT_TIME_WINDOW)


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


def _decode_log_payload_fields(rows: List[Dict[str, Any]]) -> None:
    """Decode labels/attributes_json fields in-place."""
    for row in rows:
        if "labels" in row and isinstance(row["labels"], str):
            try:
                row["labels"] = json.loads(row["labels"]) if row["labels"] else {}
            except Exception:
                row["labels"] = {}
        if "attributes_json" in row and isinstance(row["attributes_json"], str):
            try:
                row["attributes"] = json.loads(row["attributes_json"]) if row["attributes_json"] else {}
            except Exception:
                row["attributes"] = {}


def query_logs(
    *,
    storage_adapter: Any,
    limit: int,
    service_name: Optional[str],
    service_names: Optional[List[str]],
    trace_id: Optional[str],
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

    effective_service_name = context.get("service_name")
    requested_service_names = normalize_optional_str_list_fn(service_names)
    if effective_service_name:
        requested_service_names = normalize_optional_str_list_fn([effective_service_name, *requested_service_names])
    effective_search = context.get("search")
    effective_start_time = context.get("start_time")
    effective_end_time = context.get("end_time")
    fallback_time_window = context.get("time_window") or _resolve_query_logs_default_window()
    requested_levels = normalize_level_values_fn([level or "", *(normalize_optional_str_list_fn(levels))])
    requested_level_match_values = expand_level_match_values_fn(requested_levels)

    append_exact_match_filter_fn(
        conditions=prewhere_conditions,
        params=params,
        column_name="service_name",
        param_prefix="service_name",
        values=requested_service_names,
    )
    if trace_id:
        prewhere_conditions.append("trace_id = {trace_id:String}")
        params["trace_id"] = trace_id
    if pod_name:
        prewhere_conditions.append("pod_name = {pod_name:String}")
        params["pod_name"] = pod_name
    append_exact_match_filter_fn(
        conditions=prewhere_conditions,
        params=params,
        column_name="level_norm",
        param_prefix="level",
        values=requested_levels,
    )

    if effective_start_time and str(effective_start_time).startswith("__RELATIVE_INTERVAL__::"):
        relative_interval = str(effective_start_time).split("::", 1)[1]
        prewhere_conditions.append(f"timestamp > now() - INTERVAL {relative_interval}")
    elif effective_start_time:
        prewhere_conditions.append("timestamp >= toDateTime64({start_time:String}, 9)")
        params["start_time"] = convert_timestamp_fn(effective_start_time)
    if effective_end_time:
        prewhere_conditions.append("timestamp <= toDateTime64({end_time:String}, 9)")
        params["end_time"] = convert_timestamp_fn(effective_end_time)
        if not effective_start_time:
            prewhere_conditions.append(
                f"timestamp > toDateTime64({{end_time:String}}, 9) - INTERVAL {fallback_time_window}"
            )

    effective_anchor_time = normalized_anchor_time or datetime.now(timezone.utc).isoformat()
    prewhere_conditions.append("timestamp <= toDateTime64({anchor_time:String}, 9)")
    params["anchor_time"] = convert_timestamp_fn(effective_anchor_time)

    if normalized_cursor:
        try:
            cursor_timestamp, cursor_id = decode_logs_cursor_fn(normalized_cursor)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"Invalid cursor: {exc}") from exc
        prewhere_conditions.append(
            "("
            "timestamp < toDateTime64({cursor_timestamp:String}, 9) "
            "OR (timestamp = toDateTime64({cursor_timestamp:String}, 9) AND id < {cursor_id:String})"
            ")"
        )
        params["cursor_timestamp"] = convert_timestamp_fn(cursor_timestamp)
        params["cursor_id"] = cursor_id

    if exclude_health_check:
        append_health_check_exclusion_fn(where_conditions, params)

    if effective_search:
        where_conditions.append("message LIKE concat('%', {search:String}, '%')")
        params["search"] = effective_search

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
        pod_id,
        trace_id,
        span_id,
        labels,
        attributes_json,
        host_ip
    FROM logs.logs
    {prewhere_clause}
    {where_clause}
    ORDER BY timestamp DESC, id DESC
    LIMIT {{limit_plus_one:Int32}}
    """
    params["limit_plus_one"] = limit + 1

    results = storage_adapter.execute_query(query, params)
    has_more = len(results) > limit
    page_results = results[:limit]
    _decode_log_payload_fields(page_results)

    next_cursor = None
    if has_more and page_results:
        last_row = page_results[-1]
        next_cursor = encode_logs_cursor_fn(last_row.get("timestamp"), last_row.get("id"))

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
                "trace_id": trace_id,
                "pod_name": pod_name,
                "level": level,
                "levels": requested_levels,
                "level_match_values": requested_level_match_values,
                "exclude_health_check": exclude_health_check,
                "search": effective_search,
                "source_service": context.get("source_service"),
                "target_service": context.get("target_service"),
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
        "anchor_time": effective_anchor_time,
        "context": {
            "source_service": context.get("source_service"),
            "target_service": context.get("target_service"),
            "time_window": context.get("time_window"),
            "effective_service_name": effective_service_name,
            "effective_service_names": requested_service_names,
            "effective_search": effective_search,
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

    effective_service_name = context.get("service_name")
    requested_service_names = normalize_optional_str_list_fn(service_names)
    if effective_service_name:
        requested_service_names = normalize_optional_str_list_fn([effective_service_name, *requested_service_names])
    requested_levels = normalize_level_values_fn([level or "", *(normalize_optional_str_list_fn(levels))])
    requested_level_match_values = expand_level_match_values_fn(requested_levels)
    effective_search = context.get("search")
    effective_start_time = context.get("start_time")
    effective_end_time = context.get("end_time")
    fallback_time_window = context.get("time_window") or _resolve_query_logs_default_window()

    base_prewhere_conditions: List[str] = []
    base_where_conditions: List[str] = []
    base_params: Dict[str, Any] = {}
    if trace_id:
        base_prewhere_conditions.append("trace_id = {trace_id:String}")
        base_params["trace_id"] = trace_id
    if pod_name:
        base_prewhere_conditions.append("pod_name = {pod_name:String}")
        base_params["pod_name"] = pod_name
    if effective_start_time and str(effective_start_time).startswith("__RELATIVE_INTERVAL__::"):
        relative_interval = str(effective_start_time).split("::", 1)[1]
        base_prewhere_conditions.append(f"timestamp > now() - INTERVAL {relative_interval}")
    elif effective_start_time:
        base_prewhere_conditions.append("timestamp >= toDateTime64({start_time:String}, 9)")
        base_params["start_time"] = convert_timestamp_fn(effective_start_time)
    if effective_end_time:
        base_prewhere_conditions.append("timestamp <= toDateTime64({end_time:String}, 9)")
        base_params["end_time"] = convert_timestamp_fn(effective_end_time)
        if not effective_start_time:
            base_prewhere_conditions.append(
                f"timestamp > toDateTime64({{end_time:String}}, 9) - INTERVAL {fallback_time_window}"
            )
    if exclude_health_check:
        append_health_check_exclusion_fn(base_where_conditions, base_params)
    if effective_search:
        base_where_conditions.append("message LIKE concat('%', {search:String}, '%')")
        base_params["search"] = effective_search

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
    service_prewhere = (
        f"PREWHERE {' AND '.join(service_prewhere_conditions)}"
        if service_prewhere_conditions
        else ""
    )
    service_where = f"WHERE {' AND '.join(service_where_conditions)}" if service_where_conditions else ""
    service_query = f"""
    SELECT
        service_name AS value,
        count() AS count
    FROM logs.logs
    {service_prewhere}
    {service_where}
    GROUP BY service_name
    ORDER BY count DESC, value ASC
    LIMIT {{limit_services:Int32}}
    """
    service_params["limit_services"] = limit_services
    service_rows = storage_adapter.execute_query(service_query, service_params)

    level_prewhere_conditions = list(base_prewhere_conditions)
    level_where_conditions = list(base_where_conditions)
    level_params = dict(base_params)
    append_exact_match_filter_fn(
        conditions=level_prewhere_conditions,
        params=level_params,
        column_name="service_name",
        param_prefix="facet_service",
        values=requested_service_names,
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
    """
    level_params["limit_levels"] = limit_levels
    level_rows = storage_adapter.execute_query(level_query, level_params)

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
    ][:limit_levels]

    service_buckets = [
        {
            "value": str(row.get("value") or ""),
            "count": int(row.get("count") or 0),
        }
        for row in service_rows
        if str(row.get("value") or "").strip()
    ]

    return {
        "services": service_buckets,
        "levels": level_buckets,
        "context": {
            "source_service": context.get("source_service"),
            "target_service": context.get("target_service"),
            "time_window": context.get("time_window"),
            "effective_service_name": effective_service_name,
            "effective_service_names": requested_service_names,
            "effective_search": effective_search,
            "effective_levels": requested_levels,
            "effective_level_match_values": requested_level_match_values,
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
    sanitize_interval_fn: Callable[[str, str], str],
    append_health_check_exclusion_fn: Callable[[List[str], Dict[str, Any]], None],
    to_datetime_fn: Callable[[Any], datetime],
) -> Dict[str, Any]:
    """Query logs preview for a topology edge."""
    source = str(source_service or "").strip()
    target = str(target_service or "").strip()
    if not source or not target:
        raise HTTPException(status_code=400, detail="source_service and target_service are required")

    safe_window = sanitize_interval_fn(time_window, default_value="1 HOUR")
    prewhere_conditions = [
        f"timestamp > now() - INTERVAL {safe_window}",
        "(service_name = {source_service:String} OR service_name = {target_service:String})",
    ]
    where_conditions: List[str] = []
    params: Dict[str, Any] = {
        "source_service": source,
        "target_service": target,
        "query_limit": max(limit * 8, 200),
    }

    if exclude_health_check:
        append_health_check_exclusion_fn(where_conditions, params)

    where_conditions.append(
        "("
        "positionCaseInsensitiveUTF8(message, {source_service:String}) > 0 "
        "OR positionCaseInsensitiveUTF8(message, {target_service:String}) > 0 "
        "OR positionCaseInsensitiveUTF8(attributes_json, {source_service:String}) > 0 "
        "OR positionCaseInsensitiveUTF8(attributes_json, {target_service:String}) > 0 "
        "OR length(trim(trace_id)) > 0"
        ")"
    )

    prewhere_clause = "PREWHERE " + " AND ".join(prewhere_conditions)
    where_clause = "WHERE " + " AND ".join(where_conditions)
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
    """

    rows = storage_adapter.execute_query(query, params)
    source_lower = source.lower()
    target_lower = target.lower()
    ranked_rows: List[Dict[str, Any]] = []

    for row in rows:
        message = str(row.get("message") or "")
        attrs_text = str(row.get("attributes_json") or "")
        service = str(row.get("service_name") or "")
        level = str(row.get("level") or "").upper()

        message_lower = message.lower()
        attrs_lower = attrs_text.lower()
        mention_source = source_lower in message_lower or source_lower in attrs_lower
        mention_target = target_lower in message_lower or target_lower in attrs_lower
        has_trace_id = bool(str(row.get("trace_id") or "").strip())

        score = 0
        if service == source and mention_target:
            score += 5
        if service == target and mention_source:
            score += 5
        if mention_source and mention_target:
            score += 3
        if has_trace_id:
            score += 2
        if level in {"ERROR", "FATAL"}:
            score += 2
        elif level in {"WARN", "WARNING"}:
            score += 1
        if any(token in message_lower for token in ("timeout", "exception", "failed", "error", "超时", "失败")):
            score += 2

        if score <= 0:
            continue

        row["edge_match_score"] = score
        row["edge_side"] = "source" if service == source else ("target" if service == target else "unknown")
        ranked_rows.append(row)

    ranked_rows.sort(
        key=lambda item: (
            int(item.get("edge_match_score", 0)),
            to_datetime_fn(item.get("timestamp")).isoformat(),
        ),
        reverse=True,
    )
    ranked_rows = ranked_rows[:limit]
    _decode_log_payload_fields(ranked_rows)

    return {
        "data": ranked_rows,
        "count": len(ranked_rows),
        "limit": limit,
        "context": {
            "source_service": source,
            "target_service": target,
            "time_window": safe_window,
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
    trace_id: Optional[str],
    pod_name: Optional[str],
    level: Optional[str],
    start_time: Optional[str],
    end_time: Optional[str],
    exclude_health_check: bool,
    search: Optional[str],
    source_service: Optional[str],
    target_service: Optional[str],
    time_window: Optional[str],
    normalize_topology_context_fn: Callable[..., Dict[str, Any]],
    expand_level_match_values_fn: Callable[[List[str]], List[str]],
    append_exact_match_filter_fn: Callable[..., None],
    append_health_check_exclusion_fn: Callable[[List[str], Dict[str, Any]], None],
    convert_timestamp_fn: Callable[[Optional[str]], Optional[str]],
    logger: Any,
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

    effective_service_name = context.get("service_name")
    effective_search = context.get("search")
    effective_start_time = context.get("start_time")
    effective_end_time = context.get("end_time")
    fallback_time_window = context.get("time_window") or _resolve_query_logs_default_window()

    if effective_service_name:
        prewhere_conditions.append("service_name = {service_name:String}")
        params["service_name"] = effective_service_name
    if trace_id:
        prewhere_conditions.append("trace_id = {trace_id:String}")
        params["trace_id"] = trace_id
    if pod_name:
        prewhere_conditions.append("pod_name = {pod_name:String}")
        params["pod_name"] = pod_name
    if level:
        requested_level_values = expand_level_match_values_fn([level])
        append_exact_match_filter_fn(
            conditions=prewhere_conditions,
            params=params,
            column_name="level_norm",
            param_prefix="level",
            values=[item for item in requested_level_values if item != "WARNING"],
        )
    if effective_start_time and str(effective_start_time).startswith("__RELATIVE_INTERVAL__::"):
        relative_interval = str(effective_start_time).split("::", 1)[1]
        prewhere_conditions.append(f"timestamp > now() - INTERVAL {relative_interval}")
    elif effective_start_time:
        prewhere_conditions.append("timestamp >= toDateTime64({start_time:String}, 9)")
        params["start_time"] = convert_timestamp_fn(effective_start_time)
    if effective_end_time:
        prewhere_conditions.append("timestamp <= toDateTime64({end_time:String}, 9)")
        params["end_time"] = convert_timestamp_fn(effective_end_time)
        if not effective_start_time:
            prewhere_conditions.append(
                f"timestamp > toDateTime64({{end_time:String}}, 9) - INTERVAL {fallback_time_window}"
            )

    if exclude_health_check:
        append_health_check_exclusion_fn(where_conditions, params)

    if effective_search:
        where_conditions.append("message LIKE concat('%', {search:String}, '%')")
        params["search"] = effective_search

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
    """
    params["limit"] = limit

    rows = storage_adapter.execute_query(query, params)
    _decode_log_payload_fields(rows)

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
                "trace_id": trace_id,
                "pod_name": pod_name,
                "level": level,
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
    timestamp: Optional[str],
    before_count: int,
    after_count: int,
    limit: int,
    convert_timestamp_fn: Callable[[Optional[str]], Optional[str]],
) -> Dict[str, Any]:
    """Query logs context by log_id/trace_id/pod+timestamp."""
    context_fields = """
        id,
        timestamp,
        service_name,
        level,
        message,
        pod_name,
        namespace,
        node_name,
        pod_id,
        container_name,
        container_id,
        container_image,
        trace_id,
        span_id,
        labels,
        attributes_json,
        host_ip
    """

    normalized_log_id = str(log_id or "").strip()
    normalized_namespace = str(namespace or "").strip()

    if trace_id and not normalized_log_id:
        query = f"""
        SELECT
            {context_fields}
        FROM logs.logs
        PREWHERE trace_id = {{trace_id:String}}
        ORDER BY timestamp ASC, id ASC
        LIMIT {{limit:Int32}}
        """
        params = {
            "trace_id": trace_id,
            "limit": limit,
        }
        results = storage_adapter.execute_query(query, params)
        _decode_log_payload_fields(results)
        return {
            "trace_id": trace_id,
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
            anchor_timestamp = str(anchor_row.get("timestamp") or "").strip()
            anchor_timestamp_for_query = convert_timestamp_fn(anchor_timestamp) or anchor_timestamp
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
                where_prefix = " AND ".join(where_parts)

                before_query = f"""
                SELECT {context_fields}
                FROM logs.logs
                PREWHERE {where_prefix}
                    AND timestamp <= toDateTime64({{anchor_timestamp:String}}, 9)
                WHERE (
                    timestamp < toDateTime64({{anchor_timestamp:String}}, 9)
                    OR (
                      timestamp = toDateTime64({{anchor_timestamp:String}}, 9)
                      AND id < {{anchor_id:String}}
                    )
                  )
                ORDER BY timestamp DESC, id DESC
                LIMIT {{before_count:Int32}}
                """
                before_results = storage_adapter.execute_query(
                    before_query,
                    {
                        **shared_params,
                        "before_count": before_count,
                    },
                )
                before_results = list(reversed(before_results))

                after_query = f"""
                SELECT {context_fields}
                FROM logs.logs
                PREWHERE {where_prefix}
                    AND timestamp >= toDateTime64({{anchor_timestamp:String}}, 9)
                WHERE (
                    timestamp > toDateTime64({{anchor_timestamp:String}}, 9)
                    OR (
                      timestamp = toDateTime64({{anchor_timestamp:String}}, 9)
                      AND id > {{anchor_id:String}}
                    )
                  )
                ORDER BY timestamp ASC, id ASC
                LIMIT {{after_count:Int32}}
                """
                after_results = storage_adapter.execute_query(
                    after_query,
                    {
                        **shared_params,
                        "after_count": after_count,
                    },
                )

                for rows in (before_results, after_results):
                    _decode_log_payload_fields(rows)
                _decode_log_payload_fields(anchor_rows)

                return {
                    "log_id": normalized_log_id,
                    "pod_name": anchor_pod_name,
                    "namespace": anchor_namespace or None,
                    "timestamp": anchor_timestamp,
                    "before": before_results,
                    "after": after_results,
                    "current": anchor_rows[0],
                    "before_count": len(before_results),
                    "after_count": len(after_results),
                    "context_mode": "log_id",
                }

            _decode_log_payload_fields(anchor_rows)
            return {
                "log_id": normalized_log_id,
                "before": [],
                "after": [],
                "current": anchor_rows[0],
                "before_count": 0,
                "after_count": 0,
                "context_mode": "log_id",
            }

    if pod_name and timestamp:
        ch_timestamp = convert_timestamp_fn(timestamp) or timestamp
        where_parts = ["pod_name = {pod_name:String}"]
        if normalized_namespace:
            where_parts.append("namespace = {namespace:String}")
        where_prefix = " AND ".join(where_parts)
        context_params: Dict[str, Any] = {
            "pod_name": pod_name,
            "timestamp": ch_timestamp,
        }
        if normalized_namespace:
            context_params["namespace"] = normalized_namespace

        before_anchor_clause = "timestamp < toDateTime64({timestamp:String}, 9)"
        after_anchor_clause = "timestamp > toDateTime64({timestamp:String}, 9)"

        before_query = f"""
        SELECT {context_fields}
        FROM logs.logs
        PREWHERE {where_prefix}
            AND {before_anchor_clause}
        ORDER BY timestamp DESC, id DESC
        LIMIT {{before_count:Int32}}
        """
        before_results = storage_adapter.execute_query(
            before_query,
            {
                **context_params,
                "before_count": before_count,
            },
        )
        before_results = list(reversed(before_results))

        after_query = f"""
        SELECT {context_fields}
        FROM logs.logs
        PREWHERE {where_prefix}
            AND {after_anchor_clause}
        ORDER BY timestamp ASC, id ASC
        LIMIT {{after_count:Int32}}
        """
        after_results = storage_adapter.execute_query(
            after_query,
            {
                **context_params,
                "after_count": after_count,
            },
        )

        current_query = f"""
        SELECT {context_fields}
        FROM logs.logs
        PREWHERE {where_prefix}
            AND timestamp = toDateTime64({{timestamp:String}}, 9)
        ORDER BY timestamp DESC, id DESC
        LIMIT 1
        """
        current_results = storage_adapter.execute_query(
            current_query,
            context_params,
        )

        for rows in (before_results, after_results, current_results):
            _decode_log_payload_fields(rows)

        return {
            "pod_name": pod_name,
            "namespace": normalized_namespace or None,
            "timestamp": timestamp,
            "before": before_results,
            "after": after_results,
            "current": current_results[0] if current_results else None,
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
    results = storage_adapter.execute_query(query, {"log_id": log_id})
    if not results:
        raise HTTPException(status_code=404, detail="Log not found")

    row = results[0]
    _decode_log_payload_fields([row])
    return {"data": row}
