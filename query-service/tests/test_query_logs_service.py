"""Tests for extracted logs query service helpers."""

import os
import sys
from typing import Any, Dict, List

import pytest
from fastapi import HTTPException

# 添加 query-service 根目录到 Python 路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api import query_logs_service as logs_service
from api import query_routes


class FakeStorageAdapter:
    """Storage stub for logs service tests."""

    def __init__(self):
        self.calls: List[Dict[str, Any]] = []

    def execute_query(self, query: str, params: Dict[str, Any] = None) -> List[Dict[str, Any]]:
        condensed = " ".join(query.split())
        payload = {"query": condensed, "params": params or {}}
        self.calls.append(payload)

        if "ORDER BY timestamp DESC, id DESC" in condensed and "limit_plus_one" in condensed:
            return [
                {
                    "id": "l2",
                    "timestamp": "2026-03-01 10:00:02.000",
                    "_cursor_ts_ns": 1772368802000000000,
                    "service_name": "checkout",
                    "level": "ERROR",
                    "message": "timeout",
                    "pod_name": "checkout-1",
                    "namespace": "prod",
                    "node_name": "node-1",
                    "container_name": "checkout",
                    "container_id": "cid-1",
                    "container_image": "checkout:v1",
                    "pod_id": "pid-1",
                    "trace_id": "trace-1",
                    "span_id": "span-1",
                    "labels": "{\"app\":\"checkout\"}",
                    "attributes_json": "{\"peer.service\":\"payment\"}",
                    "host_ip": "10.0.0.1",
                },
                {
                    "id": "l1",
                    "timestamp": "2026-03-01 10:00:01.000",
                    "_cursor_ts_ns": 1772368801000000000,
                    "service_name": "checkout",
                    "level": "INFO",
                    "message": "request",
                    "pod_name": "checkout-1",
                    "namespace": "prod",
                    "node_name": "node-1",
                    "container_name": "checkout",
                    "container_id": "cid-1",
                    "container_image": "checkout:v1",
                    "pod_id": "pid-1",
                    "trace_id": "trace-1",
                    "span_id": "span-2",
                    "labels": "{}",
                    "attributes_json": "{}",
                    "host_ip": "10.0.0.1",
                },
            ]

        if "PREWHERE trace_id = {trace_id:String}" in condensed and "ORDER BY timestamp ASC" in condensed:
            return [
                {
                    "id": "trace-ctx-row-1",
                    "timestamp": "2026-03-01 10:00:00.000",
                    "service_name": "checkout",
                    "level": "INFO",
                    "message": "trace line",
                    "pod_name": "checkout-1",
                    "namespace": "prod",
                    "node_name": "node-1",
                    "pod_id": "pid-1",
                    "container_name": "checkout",
                    "container_id": "cid-1",
                    "container_image": "checkout:v1",
                    "trace_id": "trace-ctx-1",
                    "span_id": "span-a",
                    "labels": "{}",
                    "attributes_json": "{\"k\":\"v\"}",
                    "host_ip": "10.0.0.1",
                }
            ]

        if "PREWHERE id = {anchor_log_id:String}" in condensed:
            anchor_log_id = str((params or {}).get("anchor_log_id") or "")
            if anchor_log_id == "ctx-anchor-1":
                return [
                    {
                        "id": "ctx-anchor-1",
                        "timestamp": "2026-03-01 10:00:00+00:00",
                        "service_name": "checkout",
                        "level": "ERROR",
                        "message": "anchor",
                        "pod_name": "checkout-1",
                        "namespace": "prod",
                        "node_name": "node-1",
                        "pod_id": "pid-1",
                        "container_name": "checkout",
                        "container_id": "cid-1",
                        "container_image": "checkout:v1",
                        "trace_id": "trace-anchor-1",
                        "span_id": "span-anchor-1",
                        "labels": "{}",
                        "attributes_json": "{}",
                        "host_ip": "10.0.0.1",
                    }
                ]
            return []

        if "AND id < {anchor_id:String}" in condensed:
            return [
                {
                    "id": "ctx-before-1",
                    "timestamp": "2026-03-01 09:59:59.000",
                    "service_name": "checkout",
                    "level": "INFO",
                    "message": "before-anchor",
                    "pod_name": "checkout-1",
                    "namespace": "prod",
                    "node_name": "node-1",
                    "pod_id": "pid-1",
                    "container_name": "checkout",
                    "container_id": "cid-1",
                    "container_image": "checkout:v1",
                    "trace_id": "",
                    "span_id": "span-before-anchor",
                    "labels": "{}",
                    "attributes_json": "{}",
                    "host_ip": "10.0.0.1",
                }
            ]

        if "AND id > {anchor_id:String}" in condensed:
            return [
                {
                    "id": "ctx-after-1",
                    "timestamp": "2026-03-01 10:00:01.000",
                    "service_name": "checkout",
                    "level": "WARN",
                    "message": "after-anchor",
                    "pod_name": "checkout-1",
                    "namespace": "prod",
                    "node_name": "node-1",
                    "pod_id": "pid-1",
                    "container_name": "checkout",
                    "container_id": "cid-1",
                    "container_image": "checkout:v1",
                    "trace_id": "",
                    "span_id": "span-after-anchor",
                    "labels": "{}",
                    "attributes_json": "{}",
                    "host_ip": "10.0.0.1",
                }
            ]

        if "timestamp < toDateTime64({timestamp:String}, 9, 'UTC')" in condensed:
            return [
                {
                    "id": "ctx-before-ts-1",
                    "timestamp": "2026-03-01 09:59:59.000",
                    "service_name": "checkout",
                    "level": "INFO",
                    "message": "before",
                    "pod_name": "checkout-1",
                    "namespace": "prod",
                    "node_name": "node-1",
                    "pod_id": "pid-1",
                    "container_name": "checkout",
                    "container_id": "cid-1",
                    "container_image": "checkout:v1",
                    "trace_id": "",
                    "span_id": "span-before",
                    "labels": "{}",
                    "attributes_json": "{}",
                    "host_ip": "10.0.0.1",
                }
            ]

        if "timestamp > toDateTime64({timestamp:String}, 9, 'UTC')" in condensed:
            return [
                {
                    "id": "ctx-after-ts-1",
                    "timestamp": "2026-03-01 10:00:01.000",
                    "service_name": "checkout",
                    "level": "WARN",
                    "message": "after",
                    "pod_name": "checkout-1",
                    "namespace": "prod",
                    "node_name": "node-1",
                    "pod_id": "pid-1",
                    "container_name": "checkout",
                    "container_id": "cid-1",
                    "container_image": "checkout:v1",
                    "trace_id": "",
                    "span_id": "span-after",
                    "labels": "{}",
                    "attributes_json": "{}",
                    "host_ip": "10.0.0.1",
                }
            ]

        if "timestamp = toDateTime64({timestamp:String}, 9, 'UTC')" in condensed:
            return [
                {
                    "id": "ctx-current-ts-2",
                    "timestamp": "2026-03-01 10:00:00.000",
                    "service_name": "checkout",
                    "level": "WARN",
                    "message": "current-sibling",
                    "pod_name": "checkout-1",
                    "namespace": "prod",
                    "node_name": "node-1",
                    "pod_id": "pid-1",
                    "container_name": "checkout",
                    "container_id": "cid-1",
                    "container_image": "checkout:v1",
                    "trace_id": "",
                    "span_id": "span-current-2",
                    "labels": "{}",
                    "attributes_json": "{}",
                    "host_ip": "10.0.0.1",
                },
                {
                    "id": "ctx-current-ts-1",
                    "timestamp": "2026-03-01 10:00:00.000",
                    "service_name": "checkout",
                    "level": "ERROR",
                    "message": "current",
                    "pod_name": "checkout-1",
                    "namespace": "prod",
                    "node_name": "node-1",
                    "pod_id": "pid-1",
                    "container_name": "checkout",
                    "container_id": "cid-1",
                    "container_image": "checkout:v1",
                    "trace_id": "",
                    "span_id": "span-current",
                    "labels": "{}",
                    "attributes_json": "{}",
                    "host_ip": "10.0.0.1",
                }
            ]

        if "PREWHERE id = {log_id:String}" in condensed:
            log_id = str((params or {}).get("log_id") or "")
            if log_id == "exists":
                return [
                    {
                        "id": "exists",
                        "timestamp": "2026-03-01 10:00:00.000",
                        "service_name": "checkout",
                        "pod_name": "checkout-1",
                        "namespace": "prod",
                        "node_name": "node-1",
                        "container_name": "checkout",
                        "container_id": "cid-1",
                        "container_image": "checkout:v1",
                        "pod_id": "pid-1",
                        "level": "ERROR",
                        "message": "detail",
                        "trace_id": "trace-1",
                        "span_id": "span-1",
                        "labels": "{}",
                        "attributes_json": "{\"a\":1}",
                        "host_ip": "10.0.0.1",
                    }
                ]
            return []

        return []


@pytest.fixture(autouse=True)
def reset_query_routes_storage_adapter():
    query_routes.set_storage_adapter(None)
    yield
    query_routes.set_storage_adapter(None)


@pytest.fixture(autouse=True)
def inline_query_routes_run_blocking(monkeypatch: pytest.MonkeyPatch):
    """在单元测试中内联执行阻塞逻辑，避免线程池导致的测试挂起。"""

    async def _inline_run_blocking(func: Any, *args: Any, **kwargs: Any) -> Any:
        return func(*args, **kwargs)

    monkeypatch.setattr(query_routes, "_run_blocking", _inline_run_blocking)


def test_build_normalized_service_sql_includes_service_and_pod_fallback():
    expr = logs_service._build_normalized_service_sql(
        service_column="service_name",
        pod_column="pod_name",
    )

    assert "lowerUTF8(trim(service_name)) != 'unknown'" in expr
    assert "trim(pod_name)" in expr
    assert "replaceRegexpOne" in expr


def test_decode_log_payload_fields_normalizes_non_object_json_payloads():
    rows = [
        {
            "labels": "[]",
            "log_meta": "[]",
            "attributes_json": "[]",
        },
        {
            "labels": {"app": "checkout"},
            "log_meta": {},
            "attributes_json": {"request_id": "req-1"},
        },
    ]

    logs_service._decode_log_payload_fields(rows)

    assert rows[0]["labels"] == {}
    assert rows[0]["log_meta"] == {}
    assert rows[0]["attributes"] == {}
    assert rows[1]["labels"] == {"app": "checkout"}
    assert rows[1]["attributes"] == {"request_id": "req-1"}


def test_normalize_service_identity_uses_service_fallback_rules():
    assert logs_service._normalize_service_identity("unknown", "checkout-7d9c6f8b5d-abcde") == "checkout"
    assert logs_service._normalize_service_identity("payment-5f6d9c8b7c-abcde", "") == "payment"
    assert logs_service._normalize_service_identity("", "worker-2") == "worker"


def test_append_topology_edge_candidate_filter_matches_services_case_insensitively():
    prewhere_conditions: list[str] = []
    where_conditions: list[str] = []
    params: dict[str, str] = {}

    matched = logs_service._append_topology_edge_candidate_filter(
        prewhere_conditions=prewhere_conditions,
        where_conditions=where_conditions,
        params=params,
        source_service="Checkout",
        target_service="Payment",
    )

    assert matched is True
    assert "lowerUTF8" in prewhere_conditions[0]
    assert "lowerUTF8" in where_conditions[0]
    assert params["source_service"] == "Checkout"
    assert params["target_service"] == "Payment"


def test_annotate_edge_candidate_rows_uses_normalized_service_identity():
    rows = [
        {
            "service_name": "unknown",
            "pod_name": "checkout-7d9c6f8b5d-abcde",
            "message": "calling payment service",
            "attributes_json": "{}",
        }
    ]

    logs_service._annotate_edge_candidate_rows(rows, "checkout", "payment")

    assert rows[0]["edge_side"] == "source"
    assert rows[0]["edge_match_kind"] == "source_mentions_target"


def test_query_logs_builds_parameterized_filters_and_cursor():
    storage = FakeStorageAdapter()
    cursor = query_routes._encode_logs_cursor("2026-03-01 10:00:03.000", "cursor-id")

    result = logs_service.query_logs(
        storage_adapter=storage,
        limit=1,
        service_name=None,
        service_names=["checkout"],
        trace_id=None,
        pod_name=None,
        container_name="checkout",
        level="WARN",
        levels=None,
        start_time="2026-03-01T10:00:00Z",
        end_time="2026-03-01T11:00:00Z",
        exclude_health_check=False,
        search="timeout",
        source_service=None,
        target_service=None,
        time_window=None,
        cursor=cursor,
        anchor_time="2026-03-01T11:59:59Z",
        normalize_optional_str_fn=query_routes._normalize_optional_str,
        normalize_topology_context_fn=query_routes._normalize_topology_context,
        normalize_optional_str_list_fn=query_routes._normalize_optional_str_list,
        normalize_level_values_fn=query_routes._normalize_level_values,
        expand_level_match_values_fn=query_routes._expand_level_match_values,
        append_exact_match_filter_fn=query_routes._append_exact_match_filter,
        append_health_check_exclusion_fn=query_routes._append_health_check_exclusion,
        convert_timestamp_fn=query_routes._convert_timestamp,
        decode_logs_cursor_fn=query_routes._decode_logs_cursor,
        encode_logs_cursor_fn=query_routes._encode_logs_cursor,
        logger=query_routes.logger,
        namespace="prod",
        namespaces=None,
    )

    query_call = storage.calls[0]
    assert (
        "{service_name_0:String}" in query_call["query"]
        or "{service_name:String}" in query_call["query"]
    )
    assert "replaceRegexpOne" in query_call["query"]
    assert (
        "level_norm = {level_0:String}" in query_call["query"]
        or "level_norm = {level:String}" in query_call["query"]
    )
    assert "timestamp < toDateTime64({cursor_timestamp:String}, 9, 'UTC')" in query_call["query"]
    assert (query_call["params"].get("service_name_0") or query_call["params"].get("service_name")) == "checkout"
    assert query_call["params"]["namespace"] == "prod"
    assert "namespace = {namespace:String}" in query_call["query"]
    assert "container_name = {container_name:String}" in query_call["query"]
    assert query_call["params"]["container_name"] == "checkout"
    assert query_call["params"]["limit_plus_one"] == 2
    assert query_call["params"]["search"] == "timeout"
    assert "attributes_json ILIKE concat('%', {search:String}, '%')" in query_call["query"]
    assert "SETTINGS optimize_use_projections = 1, optimize_read_in_order = 1" in query_call["query"]
    assert "max_threads = {max_threads:Int32}" in query_call["query"]
    assert query_call["params"]["max_threads"] == 4

    assert result["count"] == 1
    assert result["has_more"] is True
    assert result["next_cursor"] is not None
    assert result["data"][0]["labels"]["app"] == "checkout"
    assert result["data"][0]["attributes"]["peer.service"] == "payment"
    assert "_cursor_ts_ns" not in result["data"][0]


def test_query_logs_trims_whitespace_pod_name():
    storage = FakeStorageAdapter()

    logs_service.query_logs(
        storage_adapter=storage,
        limit=10,
        service_name=None,
        service_names=["checkout"],
        trace_id=None,
        pod_name="  checkout-1  ",
        container_name=None,
        level=None,
        levels=None,
        start_time="2026-03-01T10:00:00Z",
        end_time="2026-03-01T11:00:00Z",
        exclude_health_check=False,
        search=None,
        source_service=None,
        target_service=None,
        time_window=None,
        cursor=None,
        anchor_time=None,
        normalize_optional_str_fn=query_routes._normalize_optional_str,
        normalize_topology_context_fn=query_routes._normalize_topology_context,
        normalize_optional_str_list_fn=query_routes._normalize_optional_str_list,
        normalize_level_values_fn=query_routes._normalize_level_values,
        expand_level_match_values_fn=query_routes._expand_level_match_values,
        append_exact_match_filter_fn=query_routes._append_exact_match_filter,
        append_health_check_exclusion_fn=query_routes._append_health_check_exclusion,
        convert_timestamp_fn=query_routes._convert_timestamp,
        decode_logs_cursor_fn=query_routes._decode_logs_cursor,
        encode_logs_cursor_fn=query_routes._encode_logs_cursor,
        logger=query_routes.logger,
    )

    query_call = storage.calls[0]
    assert query_call["params"]["pod_name"] == "checkout-1"


def test_query_logs_uses_end_time_as_default_anchor_when_anchor_not_provided():
    storage = FakeStorageAdapter()

    logs_service.query_logs(
        storage_adapter=storage,
        limit=10,
        service_name=None,
        service_names=["checkout"],
        trace_id=None,
        pod_name=None,
        container_name=None,
        level=None,
        levels=None,
        start_time="2026-03-01T10:00:00Z",
        end_time="2026-03-01T11:00:00Z",
        exclude_health_check=False,
        search=None,
        source_service=None,
        target_service=None,
        time_window=None,
        cursor=None,
        anchor_time=None,
        normalize_optional_str_fn=query_routes._normalize_optional_str,
        normalize_topology_context_fn=query_routes._normalize_topology_context,
        normalize_optional_str_list_fn=query_routes._normalize_optional_str_list,
        normalize_level_values_fn=query_routes._normalize_level_values,
        expand_level_match_values_fn=query_routes._expand_level_match_values,
        append_exact_match_filter_fn=query_routes._append_exact_match_filter,
        append_health_check_exclusion_fn=query_routes._append_health_check_exclusion,
        convert_timestamp_fn=query_routes._convert_timestamp,
        decode_logs_cursor_fn=query_routes._decode_logs_cursor,
        encode_logs_cursor_fn=query_routes._encode_logs_cursor,
        logger=query_routes.logger,
        namespace="prod",
        namespaces=None,
    )

    query_call = storage.calls[0]
    assert query_call["params"]["end_time"] in {"2026-03-01 11:00:00.000", "2026-03-01 11:00:00.000000"}
    assert query_call["params"]["anchor_time"] in {"2026-03-01 11:00:00.000", "2026-03-01 11:00:00.000000"}


def test_query_logs_applies_structured_request_id_filter():
    storage = FakeStorageAdapter()

    result = logs_service.query_logs(
        storage_adapter=storage,
        limit=10,
        service_name=None,
        service_names=None,
        trace_id=None,
        request_id="req-1",
        pod_name=None,
        container_name=None,
        level=None,
        levels=None,
        start_time="2026-03-01T10:00:00Z",
        end_time="2026-03-01T11:00:00Z",
        exclude_health_check=False,
        search=None,
        source_service=None,
        target_service=None,
        time_window=None,
        cursor=None,
        anchor_time=None,
        normalize_optional_str_fn=query_routes._normalize_optional_str,
        normalize_topology_context_fn=query_routes._normalize_topology_context,
        normalize_optional_str_list_fn=query_routes._normalize_optional_str_list,
        normalize_level_values_fn=query_routes._normalize_level_values,
        expand_level_match_values_fn=query_routes._expand_level_match_values,
        append_exact_match_filter_fn=query_routes._append_exact_match_filter,
        append_health_check_exclusion_fn=query_routes._append_health_check_exclusion,
        convert_timestamp_fn=query_routes._convert_timestamp,
        decode_logs_cursor_fn=query_routes._decode_logs_cursor,
        encode_logs_cursor_fn=query_routes._encode_logs_cursor,
        logger=query_routes.logger,
    )

    query_call = storage.calls[0]
    assert "JSONExtractString(attributes_json, 'request_id') = {request_id:String}" in query_call["query"]
    assert "JSONExtractString(attributes_json, 'request', 'id') = {request_id:String}" in query_call["query"]
    assert query_call["params"]["request_id"] == "req-1"
    assert result["context"]["effective_request_id"] == "req-1"


def test_query_logs_numeric_cursor_keeps_same_timestamp_rows_via_id_tiebreak():
    storage = FakeStorageAdapter()
    cursor = query_routes._encode_logs_cursor("1772368803000123456", "cursor-id")

    logs_service.query_logs(
        storage_adapter=storage,
        limit=1,
        service_name=None,
        service_names=["checkout"],
        trace_id=None,
        pod_name=None,
        container_name=None,
        level=None,
        levels=None,
        start_time="2026-03-01T10:00:00Z",
        end_time="2026-03-01T11:00:00Z",
        exclude_health_check=False,
        search=None,
        source_service=None,
        target_service=None,
        time_window=None,
        cursor=cursor,
        anchor_time="2026-03-01T11:59:59Z",
        normalize_optional_str_fn=query_routes._normalize_optional_str,
        normalize_topology_context_fn=query_routes._normalize_topology_context,
        normalize_optional_str_list_fn=query_routes._normalize_optional_str_list,
        normalize_level_values_fn=query_routes._normalize_level_values,
        expand_level_match_values_fn=query_routes._expand_level_match_values,
        append_exact_match_filter_fn=query_routes._append_exact_match_filter,
        append_health_check_exclusion_fn=query_routes._append_health_check_exclusion,
        convert_timestamp_fn=query_routes._convert_timestamp,
        decode_logs_cursor_fn=query_routes._decode_logs_cursor,
        encode_logs_cursor_fn=query_routes._encode_logs_cursor,
        logger=query_routes.logger,
        namespace="prod",
        namespaces=None,
    )

    query_call = storage.calls[0]
    assert "toUnixTimestamp64Nano(timestamp) = {cursor_ts_ns:Int64} AND id < {cursor_id:String}" in query_call["query"]
    assert query_call["params"]["cursor_id"] == "cursor-id"


def test_query_logs_prefers_nanosecond_cursor_when_payload_timestamp_is_numeric():
    storage = FakeStorageAdapter()
    cursor = query_routes._encode_logs_cursor("1772368803000123456", "cursor-id")

    logs_service.query_logs(
        storage_adapter=storage,
        limit=1,
        service_name=None,
        service_names=["checkout"],
        trace_id=None,
        pod_name=None,
        container_name=None,
        level=None,
        levels=None,
        start_time="2026-03-01T10:00:00Z",
        end_time="2026-03-01T11:00:00Z",
        exclude_health_check=False,
        search=None,
        source_service=None,
        target_service=None,
        time_window=None,
        cursor=cursor,
        anchor_time="2026-03-01T11:59:59Z",
        normalize_optional_str_fn=query_routes._normalize_optional_str,
        normalize_topology_context_fn=query_routes._normalize_topology_context,
        normalize_optional_str_list_fn=query_routes._normalize_optional_str_list,
        normalize_level_values_fn=query_routes._normalize_level_values,
        expand_level_match_values_fn=query_routes._expand_level_match_values,
        append_exact_match_filter_fn=query_routes._append_exact_match_filter,
        append_health_check_exclusion_fn=query_routes._append_health_check_exclusion,
        convert_timestamp_fn=query_routes._convert_timestamp,
        decode_logs_cursor_fn=query_routes._decode_logs_cursor,
        encode_logs_cursor_fn=query_routes._encode_logs_cursor,
        logger=query_routes.logger,
        namespace="prod",
        namespaces=None,
    )

    query_call = storage.calls[0]
    assert "toUnixTimestamp64Nano(timestamp) < {cursor_ts_ns:Int64}" in query_call["query"]
    assert query_call["params"]["cursor_ts_ns"] == 1772368803000123456
    assert "cursor_timestamp" not in query_call["params"]


def test_append_health_check_exclusion_uses_case_insensitive_search():
    conditions: List[str] = []
    params: Dict[str, Any] = {}

    query_routes._append_health_check_exclusion(conditions, params)

    assert len(conditions) == 1
    clause = conditions[0]
    assert "multiSearchAnyCaseInsensitiveUTF8(message," in clause
    assert "lowerUTF8(message)" not in clause


def test_query_logs_context_pod_timestamp_returns_same_timestamp_matches():
    storage = FakeStorageAdapter()

    result = logs_service.query_logs_context(
        storage_adapter=storage,
        log_id=None,
        trace_id=None,
        pod_name="checkout-1",
        namespace="prod",
        container_name="checkout",
        timestamp="2026-03-01T10:00:00Z",
        before_count=1,
        after_count=1,
        limit=20,
        convert_timestamp_fn=query_routes._convert_timestamp,
    )

    assert result["context_mode"] == "pod_timestamp"
    assert result["current"]["id"] == "ctx-current-ts-2"
    assert result["current_count"] == 2
    assert [item["id"] for item in result["current_matches"]] == ["ctx-current-ts-2", "ctx-current-ts-1"]
    assert result["before_count"] == 1
    assert result["after_count"] == 1


def test_query_logs_rejects_invalid_cursor():
    storage = FakeStorageAdapter()
    with pytest.raises(HTTPException) as exc_info:
        logs_service.query_logs(
            storage_adapter=storage,
            limit=10,
            service_name=None,
            service_names=None,
            trace_id=None,
            pod_name=None,
            level=None,
            levels=None,
            start_time=None,
            end_time=None,
            exclude_health_check=False,
            search=None,
            source_service=None,
            target_service=None,
            time_window=None,
            cursor="bad-cursor",
            anchor_time=None,
            normalize_optional_str_fn=query_routes._normalize_optional_str,
            normalize_topology_context_fn=query_routes._normalize_topology_context,
            normalize_optional_str_list_fn=query_routes._normalize_optional_str_list,
            normalize_level_values_fn=query_routes._normalize_level_values,
            expand_level_match_values_fn=query_routes._expand_level_match_values,
            append_exact_match_filter_fn=query_routes._append_exact_match_filter,
            append_health_check_exclusion_fn=query_routes._append_health_check_exclusion,
            convert_timestamp_fn=query_routes._convert_timestamp,
            decode_logs_cursor_fn=query_routes._decode_logs_cursor,
            encode_logs_cursor_fn=query_routes._encode_logs_cursor,
            logger=query_routes.logger,
        )

    assert exc_info.value.status_code == 400


def test_query_logs_applies_default_time_window_when_unbounded(monkeypatch: pytest.MonkeyPatch):
    storage = FakeStorageAdapter()
    monkeypatch.setenv("QUERY_LOGS_DEFAULT_TIME_WINDOW", "6 HOUR")

    result = logs_service.query_logs(
        storage_adapter=storage,
        limit=10,
        service_name=None,
        service_names=None,
        trace_id=None,
        pod_name=None,
        level=None,
        levels=None,
        start_time=None,
        end_time=None,
        exclude_health_check=False,
        search=None,
        source_service=None,
        target_service=None,
        time_window=None,
        cursor=None,
        anchor_time=None,
        normalize_optional_str_fn=query_routes._normalize_optional_str,
        normalize_topology_context_fn=query_routes._normalize_topology_context,
        normalize_optional_str_list_fn=query_routes._normalize_optional_str_list,
        normalize_level_values_fn=query_routes._normalize_level_values,
        expand_level_match_values_fn=query_routes._expand_level_match_values,
        append_exact_match_filter_fn=query_routes._append_exact_match_filter,
        append_health_check_exclusion_fn=query_routes._append_health_check_exclusion,
        convert_timestamp_fn=query_routes._convert_timestamp,
        decode_logs_cursor_fn=query_routes._decode_logs_cursor,
        encode_logs_cursor_fn=query_routes._encode_logs_cursor,
        logger=query_routes.logger,
    )

    query_call = storage.calls[0]
    assert "timestamp > now() - INTERVAL 6 HOUR" in query_call["query"]
    assert result["context"]["time_window"] == "6 HOUR"


def test_query_logs_end_time_only_backfills_lower_bound_window(monkeypatch: pytest.MonkeyPatch):
    storage = FakeStorageAdapter()
    monkeypatch.setenv("QUERY_LOGS_DEFAULT_TIME_WINDOW", "8 HOUR")

    logs_service.query_logs(
        storage_adapter=storage,
        limit=10,
        service_name=None,
        service_names=None,
        trace_id=None,
        pod_name=None,
        level=None,
        levels=None,
        start_time=None,
        end_time="2026-03-01T11:00:00Z",
        exclude_health_check=False,
        search=None,
        source_service=None,
        target_service=None,
        time_window=None,
        cursor=None,
        anchor_time=None,
        normalize_optional_str_fn=query_routes._normalize_optional_str,
        normalize_topology_context_fn=query_routes._normalize_topology_context,
        normalize_optional_str_list_fn=query_routes._normalize_optional_str_list,
        normalize_level_values_fn=query_routes._normalize_level_values,
        expand_level_match_values_fn=query_routes._expand_level_match_values,
        append_exact_match_filter_fn=query_routes._append_exact_match_filter,
        append_health_check_exclusion_fn=query_routes._append_health_check_exclusion,
        convert_timestamp_fn=query_routes._convert_timestamp,
        decode_logs_cursor_fn=query_routes._decode_logs_cursor,
        encode_logs_cursor_fn=query_routes._encode_logs_cursor,
        logger=query_routes.logger,
    )

    query_call = storage.calls[0]
    assert "timestamp <= toDateTime64({end_time:String}, 9, 'UTC')" in query_call["query"]
    assert "timestamp > toDateTime64({end_time:String}, 9, 'UTC') - INTERVAL 8 HOUR" in query_call["query"]


def test_query_logs_facets_end_time_only_backfills_lower_bound_window(monkeypatch: pytest.MonkeyPatch):
    storage = FakeStorageAdapter()
    monkeypatch.setenv("QUERY_LOGS_DEFAULT_TIME_WINDOW", "4 HOUR")

    logs_service.query_logs_facets(
        storage_adapter=storage,
        service_name=None,
        service_names=None,
        trace_id=None,
        pod_name=None,
        level=None,
        levels=None,
        start_time=None,
        end_time="2026-03-01T11:00:00Z",
        exclude_health_check=False,
        search=None,
        source_service=None,
        target_service=None,
        time_window=None,
        limit_services=10,
        limit_levels=10,
        normalize_topology_context_fn=query_routes._normalize_topology_context,
        normalize_optional_str_list_fn=query_routes._normalize_optional_str_list,
        normalize_level_values_fn=query_routes._normalize_level_values,
        expand_level_match_values_fn=query_routes._expand_level_match_values,
        append_exact_match_filter_fn=query_routes._append_exact_match_filter,
        append_health_check_exclusion_fn=query_routes._append_health_check_exclusion,
        convert_timestamp_fn=query_routes._convert_timestamp,
    )

    assert len(storage.calls) >= 2
    assert "timestamp > toDateTime64({end_time:String}, 9, 'UTC') - INTERVAL 4 HOUR" in storage.calls[0]["query"]
    assert "timestamp > toDateTime64({end_time:String}, 9, 'UTC') - INTERVAL 4 HOUR" in storage.calls[1]["query"]
    assert "timestamp > toDateTime64({end_time:String}, 9, 'UTC') - INTERVAL 4 HOUR" in storage.calls[2]["query"]



def test_query_logs_uses_anchor_relative_window_for_historical_topology_context():
    storage = FakeStorageAdapter()

    logs_service.query_logs(
        storage_adapter=storage,
        limit=10,
        service_name=None,
        service_names=None,
        trace_id=None,
        pod_name=None,
        level=None,
        levels=None,
        start_time=None,
        end_time=None,
        exclude_health_check=False,
        search=None,
        source_service="checkout",
        target_service="payment",
        time_window="15 MINUTE",
        cursor=None,
        anchor_time="2026-03-01T11:00:00Z",
        normalize_optional_str_fn=query_routes._normalize_optional_str,
        normalize_topology_context_fn=query_routes._normalize_topology_context,
        normalize_optional_str_list_fn=query_routes._normalize_optional_str_list,
        normalize_level_values_fn=query_routes._normalize_level_values,
        expand_level_match_values_fn=query_routes._expand_level_match_values,
        append_exact_match_filter_fn=query_routes._append_exact_match_filter,
        append_health_check_exclusion_fn=query_routes._append_health_check_exclusion,
        convert_timestamp_fn=query_routes._convert_timestamp,
        decode_logs_cursor_fn=query_routes._decode_logs_cursor,
        encode_logs_cursor_fn=query_routes._encode_logs_cursor,
        logger=query_routes.logger,
    )

    query_call = storage.calls[0]
    assert "timestamp > toDateTime64({anchor_time:String}, 9, 'UTC') - INTERVAL 15 MINUTE" in query_call["query"]
    assert "timestamp > now() - INTERVAL 15 MINUTE" not in query_call["query"]
    assert "replaceRegexpOne" in query_call["query"]
    assert "message ILIKE concat('%', {target_service:String}, '%')" in query_call["query"]
    assert "message ILIKE concat('%', {source_service:String}, '%')" in query_call["query"]
    assert query_call["params"]["anchor_time"] in {"2026-03-01 11:00:00.000", "2026-03-01 11:00:00.000000"}
    assert query_call["params"]["source_service"] == "checkout"
    assert query_call["params"]["target_service"] == "payment"
    assert "service_name" not in query_call["params"]
    assert "search" not in query_call["params"]


def test_query_logs_facets_supports_anchor_time_and_container_name():
    storage = FakeStorageAdapter()

    result = logs_service.query_logs_facets(
        storage_adapter=storage,
        service_name=None,
        service_names=None,
        trace_id=None,
        request_id=None,
        pod_name=None,
        container_name="checkout",
        level=None,
        levels=None,
        start_time=None,
        end_time=None,
        exclude_health_check=False,
        search=None,
        source_service=None,
        target_service=None,
        time_window="1 HOUR",
        anchor_time="2026-03-01T11:59:59Z",
        limit_services=10,
        limit_levels=10,
        limit_namespaces=10,
        normalize_topology_context_fn=query_routes._normalize_topology_context,
        normalize_optional_str_list_fn=query_routes._normalize_optional_str_list,
        normalize_level_values_fn=query_routes._normalize_level_values,
        expand_level_match_values_fn=query_routes._expand_level_match_values,
        append_exact_match_filter_fn=query_routes._append_exact_match_filter,
        append_health_check_exclusion_fn=query_routes._append_health_check_exclusion,
        convert_timestamp_fn=query_routes._convert_timestamp,
    )

    assert len(storage.calls) == 3
    for call in storage.calls:
        assert "container_name = {container_name:String}" in call["query"]
        assert "timestamp <= toDateTime64({anchor_time:String}, 9, 'UTC')" in call["query"]
        assert call["params"]["container_name"] == "checkout"
        assert call["params"]["anchor_time"] in {"2026-03-01 11:59:59.000", "2026-03-01 11:59:59.000000"}

    assert result["context"]["effective_container_name"] == "checkout"
    assert result["context"]["anchor_time"] == "2026-03-01T11:59:59Z"



def test_query_logs_facets_trims_whitespace_pod_name():
    storage = FakeStorageAdapter()

    logs_service.query_logs_facets(
        storage_adapter=storage,
        service_name=None,
        service_names=None,
        trace_id=None,
        request_id=None,
        pod_name="  checkout-1  ",
        container_name=None,
        level=None,
        levels=None,
        start_time=None,
        end_time=None,
        exclude_health_check=False,
        search=None,
        source_service=None,
        target_service=None,
        time_window="1 HOUR",
        anchor_time=None,
        limit_services=10,
        limit_levels=10,
        limit_namespaces=10,
        normalize_topology_context_fn=query_routes._normalize_topology_context,
        normalize_optional_str_list_fn=query_routes._normalize_optional_str_list,
        normalize_level_values_fn=query_routes._normalize_level_values,
        expand_level_match_values_fn=query_routes._expand_level_match_values,
        append_exact_match_filter_fn=query_routes._append_exact_match_filter,
        append_health_check_exclusion_fn=query_routes._append_health_check_exclusion,
        convert_timestamp_fn=query_routes._convert_timestamp,
    )

    assert len(storage.calls) == 3
    for call in storage.calls:
        assert call["params"]["pod_name"] == "checkout-1"


def test_query_logs_facets_anchor_relative_window_uses_anchor_time():
    storage = FakeStorageAdapter()

    logs_service.query_logs_facets(
        storage_adapter=storage,
        service_name=None,
        service_names=None,
        trace_id=None,
        request_id=None,
        pod_name=None,
        container_name=None,
        level=None,
        levels=None,
        start_time=None,
        end_time=None,
        exclude_health_check=False,
        search=None,
        source_service="checkout",
        target_service="payment",
        time_window="15 MINUTE",
        anchor_time="2026-03-01T11:00:00Z",
        limit_services=10,
        limit_levels=10,
        limit_namespaces=10,
        normalize_topology_context_fn=query_routes._normalize_topology_context,
        normalize_optional_str_list_fn=query_routes._normalize_optional_str_list,
        normalize_level_values_fn=query_routes._normalize_level_values,
        expand_level_match_values_fn=query_routes._expand_level_match_values,
        append_exact_match_filter_fn=query_routes._append_exact_match_filter,
        append_health_check_exclusion_fn=query_routes._append_health_check_exclusion,
        convert_timestamp_fn=query_routes._convert_timestamp,
    )

    assert len(storage.calls) == 3
    for call in storage.calls:
        assert "timestamp > toDateTime64({anchor_time:String}, 9, 'UTC') - INTERVAL 15 MINUTE" in call["query"]
        assert "timestamp > now() - INTERVAL 15 MINUTE" not in call["query"]
        assert "replaceRegexpOne" in call["query"]
        assert call["params"]["anchor_time"] in {"2026-03-01 11:00:00.000", "2026-03-01 11:00:00.000000"}
        assert call["params"]["source_service"] == "checkout"
        assert call["params"]["target_service"] == "payment"


def test_query_logs_facets_same_source_target_degrades_to_single_service_filter():
    storage = FakeStorageAdapter()

    result = logs_service.query_logs_facets(
        storage_adapter=storage,
        service_name=None,
        service_names=None,
        trace_id=None,
        request_id=None,
        pod_name=None,
        container_name=None,
        level=None,
        levels=None,
        start_time=None,
        end_time=None,
        exclude_health_check=False,
        search=None,
        source_service="checkout",
        target_service="checkout",
        time_window="15 MINUTE",
        anchor_time="2026-03-01T11:00:00Z",
        limit_services=10,
        limit_levels=10,
        limit_namespaces=10,
        normalize_topology_context_fn=query_routes._normalize_topology_context,
        normalize_optional_str_list_fn=query_routes._normalize_optional_str_list,
        normalize_level_values_fn=query_routes._normalize_level_values,
        expand_level_match_values_fn=query_routes._expand_level_match_values,
        append_exact_match_filter_fn=query_routes._append_exact_match_filter,
        append_health_check_exclusion_fn=query_routes._append_health_check_exclusion,
        convert_timestamp_fn=query_routes._convert_timestamp,
    )

    assert len(storage.calls) == 3
    for call in storage.calls:
        assert "target_service:String" not in call["query"]
        assert "source_service" not in call["params"]
        assert "target_service" not in call["params"]
    assert result["context"]["effective_service_name"] == "checkout"
    assert result["context"]["effective_search"] is None


def test_query_logs_facets_degrades_for_broad_large_window(monkeypatch: pytest.MonkeyPatch):
    storage = FakeStorageAdapter()
    monkeypatch.setenv("QUERY_LOGS_FACETS_DEGRADE_WINDOW_MINUTES", "60")

    result = logs_service.query_logs_facets(
        storage_adapter=storage,
        service_name=None,
        service_names=None,
        trace_id=None,
        pod_name=None,
        level=None,
        levels=None,
        start_time=None,
        end_time=None,
        exclude_health_check=False,
        search=None,
        source_service=None,
        target_service=None,
        time_window="2 HOUR",
        limit_services=100,
        limit_levels=20,
        normalize_topology_context_fn=query_routes._normalize_topology_context,
        normalize_optional_str_list_fn=query_routes._normalize_optional_str_list,
        normalize_level_values_fn=query_routes._normalize_level_values,
        expand_level_match_values_fn=query_routes._expand_level_match_values,
        append_exact_match_filter_fn=query_routes._append_exact_match_filter,
        append_health_check_exclusion_fn=query_routes._append_health_check_exclusion,
        convert_timestamp_fn=query_routes._convert_timestamp,
    )

    assert storage.calls == []
    assert result["services"] == []
    assert result["namespaces"] == []
    assert result["levels"] == []
    assert result["context"]["facets_degraded"] is True
    assert "broad_window_exceeded" in result["context"]["facets_degrade_reasons"]
    assert result["context"]["effective_window_minutes"] == 120
    assert result["context"]["degrade_window_minutes"] == 60


def test_query_logs_facets_clamps_limits_and_marks_degraded(monkeypatch: pytest.MonkeyPatch):
    storage = FakeStorageAdapter()
    monkeypatch.setenv("QUERY_LOGS_FACETS_MAX_SERVICES", "50")
    monkeypatch.setenv("QUERY_LOGS_FACETS_MAX_NAMESPACES", "8")
    monkeypatch.setenv("QUERY_LOGS_FACETS_MAX_LEVELS", "10")
    monkeypatch.setenv("QUERY_LOGS_MAX_THREADS", "3")

    result = logs_service.query_logs_facets(
        storage_adapter=storage,
        service_name="checkout",
        service_names=None,
        trace_id=None,
        pod_name=None,
        level=None,
        levels=None,
        start_time=None,
        end_time=None,
        exclude_health_check=False,
        search=None,
        source_service=None,
        target_service=None,
        time_window="1 HOUR",
        limit_services=200,
        limit_namespaces=30,
        limit_levels=30,
        normalize_topology_context_fn=query_routes._normalize_topology_context,
        normalize_optional_str_list_fn=query_routes._normalize_optional_str_list,
        normalize_level_values_fn=query_routes._normalize_level_values,
        expand_level_match_values_fn=query_routes._expand_level_match_values,
        append_exact_match_filter_fn=query_routes._append_exact_match_filter,
        append_health_check_exclusion_fn=query_routes._append_health_check_exclusion,
        convert_timestamp_fn=query_routes._convert_timestamp,
    )

    assert len(storage.calls) == 3
    assert storage.calls[0]["params"]["limit_services"] == 50
    assert storage.calls[1]["params"]["limit_levels"] == 10
    assert storage.calls[2]["params"]["limit_namespaces"] == 20
    assert storage.calls[0]["params"]["max_threads"] == 3
    assert storage.calls[1]["params"]["max_threads"] == 3
    assert storage.calls[2]["params"]["max_threads"] == 3
    assert result["context"]["facets_degraded"] is True
    assert "services_limit_clamped" in result["context"]["facets_degrade_reasons"]
    assert "namespaces_limit_clamped" in result["context"]["facets_degrade_reasons"]
    assert "levels_limit_clamped" in result["context"]["facets_degrade_reasons"]
    assert result["context"]["facet_limit_applied"] == {"services": 50, "namespaces": 20, "levels": 10}
    assert result["context"]["facet_limit_requested"] == {"services": 200, "namespaces": 30, "levels": 30}



def test_query_logs_aggregated_anchor_relative_window_uses_anchor_time():
    storage = FakeStorageAdapter()

    logs_service.query_logs_aggregated(
        storage_adapter=storage,
        limit=20,
        min_pattern_count=1,
        max_patterns=10,
        max_samples=2,
        service_name=None,
        service_names=None,
        trace_id=None,
        request_id=None,
        pod_name=None,
        level=None,
        levels=None,
        namespace=None,
        namespaces=None,
        container_name=None,
        start_time=None,
        end_time=None,
        exclude_health_check=False,
        search=None,
        source_service="checkout",
        target_service="payment",
        time_window="15 MINUTE",
        normalize_topology_context_fn=query_routes._normalize_topology_context,
        normalize_optional_str_list_fn=query_routes._normalize_optional_str_list,
        normalize_level_values_fn=query_routes._normalize_level_values,
        expand_level_match_values_fn=query_routes._expand_level_match_values,
        append_exact_match_filter_fn=query_routes._append_exact_match_filter,
        append_health_check_exclusion_fn=query_routes._append_health_check_exclusion,
        convert_timestamp_fn=query_routes._convert_timestamp,
        logger=query_routes.logger,
        anchor_time="2026-03-01T11:00:00Z",
    )

    query_call = storage.calls[0]
    assert "timestamp > toDateTime64({anchor_time:String}, 9, 'UTC') - INTERVAL 15 MINUTE" in query_call["query"]
    assert "timestamp > now() - INTERVAL 15 MINUTE" not in query_call["query"]
    assert "replaceRegexpOne" in query_call["query"]
    assert query_call["params"]["anchor_time"] in {"2026-03-01 11:00:00.000", "2026-03-01 11:00:00.000000"}
    assert query_call["params"]["source_service"] == "checkout"
    assert query_call["params"]["target_service"] == "payment"
    assert "service_name" not in query_call["params"]
    assert "search" not in query_call["params"]


def test_query_logs_aggregated_trims_whitespace_pod_name():
    storage = FakeStorageAdapter()

    logs_service.query_logs_aggregated(
        storage_adapter=storage,
        limit=20,
        min_pattern_count=1,
        max_patterns=10,
        max_samples=2,
        service_name=None,
        service_names=None,
        trace_id=None,
        request_id=None,
        pod_name="  checkout-1  ",
        level=None,
        levels=None,
        namespace=None,
        namespaces=None,
        container_name=None,
        start_time=None,
        end_time=None,
        exclude_health_check=False,
        search=None,
        source_service=None,
        target_service=None,
        time_window="1 HOUR",
        normalize_topology_context_fn=query_routes._normalize_topology_context,
        normalize_optional_str_list_fn=query_routes._normalize_optional_str_list,
        normalize_level_values_fn=query_routes._normalize_level_values,
        expand_level_match_values_fn=query_routes._expand_level_match_values,
        append_exact_match_filter_fn=query_routes._append_exact_match_filter,
        append_health_check_exclusion_fn=query_routes._append_health_check_exclusion,
        convert_timestamp_fn=query_routes._convert_timestamp,
        logger=query_routes.logger,
        anchor_time=None,
    )

    query_call = storage.calls[0]
    assert query_call["params"]["pod_name"] == "checkout-1"


def test_query_logs_aggregated_same_source_target_degrades_to_single_service_filter():
    storage = FakeStorageAdapter()

    logs_service.query_logs_aggregated(
        storage_adapter=storage,
        limit=20,
        min_pattern_count=1,
        max_patterns=10,
        max_samples=2,
        service_name=None,
        service_names=None,
        trace_id=None,
        request_id=None,
        pod_name=None,
        level=None,
        levels=None,
        namespace=None,
        namespaces=None,
        container_name=None,
        start_time=None,
        end_time=None,
        exclude_health_check=False,
        search=None,
        source_service="checkout",
        target_service="checkout",
        time_window="15 MINUTE",
        normalize_topology_context_fn=query_routes._normalize_topology_context,
        normalize_optional_str_list_fn=query_routes._normalize_optional_str_list,
        normalize_level_values_fn=query_routes._normalize_level_values,
        expand_level_match_values_fn=query_routes._expand_level_match_values,
        append_exact_match_filter_fn=query_routes._append_exact_match_filter,
        append_health_check_exclusion_fn=query_routes._append_health_check_exclusion,
        convert_timestamp_fn=query_routes._convert_timestamp,
        logger=query_routes.logger,
        anchor_time="2026-03-01T11:00:00Z",
    )

    query_call = storage.calls[0]
    assert "target_service:String" not in query_call["query"]
    assert "source_service" not in query_call["params"]
    assert "target_service" not in query_call["params"]
    assert query_call["params"]["service_name"] == "checkout"
    assert "search" not in query_call["params"]


def test_query_logs_context_mode_and_detail():
    storage = FakeStorageAdapter()

    trace_result = logs_service.query_logs_context(
        storage_adapter=storage,
        log_id=None,
        trace_id="trace-ctx-1",
        pod_name=None,
        namespace=None,
        container_name=None,
        timestamp=None,
        before_count=5,
        after_count=5,
        limit=20,
        convert_timestamp_fn=query_routes._convert_timestamp,
    )
    assert trace_result["trace_id"] == "trace-ctx-1"
    assert trace_result["count"] == 1
    assert trace_result["data"][0]["attributes"]["k"] == "v"

    pod_result = logs_service.query_logs_context(
        storage_adapter=storage,
        log_id=None,
        trace_id=None,
        pod_name="checkout-1",
        namespace=None,
        container_name="checkout",
        timestamp="2026-03-01T10:00:00Z",
        before_count=1,
        after_count=1,
        limit=100,
        convert_timestamp_fn=query_routes._convert_timestamp,
    )
    assert pod_result["before_count"] == 1
    assert pod_result["after_count"] == 1
    assert pod_result["container_name"] == "checkout"
    assert pod_result["current"]["message"] in {"current", "current-sibling"}
    assert pod_result["current_count"] == 2
    assert {item["message"] for item in pod_result["current_matches"]} == {"current", "current-sibling"}

    before_call = next(call for call in storage.calls if "before_count" in call["params"])
    assert before_call["params"]["timestamp"] == "2026-03-01 10:00:00.000000"
    assert before_call["params"]["container_name"] == "checkout"

    by_id_result = logs_service.query_logs_context(
        storage_adapter=storage,
        log_id="ctx-anchor-1",
        trace_id=None,
        pod_name="ignored-pod",
        namespace="ignored-ns",
        container_name="ignored-container",
        timestamp="2026-03-01T10:00:00Z",
        before_count=1,
        after_count=1,
        limit=100,
        convert_timestamp_fn=query_routes._convert_timestamp,
    )
    assert by_id_result["context_mode"] == "log_id"
    assert by_id_result["current"]["id"] == "ctx-anchor-1"
    assert by_id_result["before"][0]["id"] == "ctx-before-1"
    assert by_id_result["after"][0]["id"] == "ctx-after-1"
    assert by_id_result["container_name"] == "checkout"

    by_id_before_call = next(
        call
        for call in storage.calls
        if call["params"].get("anchor_id") == "ctx-anchor-1" and "before_count" in call["params"]
    )
    assert by_id_before_call["params"]["pod_name"] == "checkout-1"
    assert by_id_before_call["params"]["namespace"] == "prod"
    assert by_id_before_call["params"]["container_name"] == "checkout"
    assert by_id_before_call["params"]["anchor_timestamp"] == "2026-03-01 10:00:00.000000"

    detail = logs_service.query_log_detail(storage_adapter=storage, log_id="exists")
    assert detail["data"]["id"] == "exists"
    assert detail["data"]["attributes"]["a"] == 1

    with pytest.raises(HTTPException) as exc_info:
        logs_service.query_log_detail(storage_adapter=storage, log_id="missing")
    assert exc_info.value.status_code == 404


def test_query_logs_context_trims_whitespace_inputs():
    storage = FakeStorageAdapter()

    trace_result = logs_service.query_logs_context(
        storage_adapter=storage,
        log_id=None,
        trace_id="  trace-ctx-1  ",
        pod_name=None,
        namespace=None,
        container_name=None,
        timestamp=None,
        before_count=5,
        after_count=5,
        limit=20,
        convert_timestamp_fn=query_routes._convert_timestamp,
    )
    assert trace_result["trace_id"] == "trace-ctx-1"
    trace_call = next(call for call in storage.calls if "trace_id" in call["params"])
    assert trace_call["params"]["trace_id"] == "trace-ctx-1"

    pod_result = logs_service.query_logs_context(
        storage_adapter=storage,
        log_id=None,
        trace_id=None,
        pod_name="  checkout-1  ",
        namespace="  prod  ",
        container_name="  checkout  ",
        timestamp=" 2026-03-01T10:00:00Z  ",
        before_count=1,
        after_count=1,
        limit=100,
        convert_timestamp_fn=query_routes._convert_timestamp,
    )
    assert pod_result["pod_name"] == "checkout-1"
    assert pod_result["namespace"] == "prod"
    assert pod_result["container_name"] == "checkout"
    pod_before_call = next(
        call
        for call in storage.calls
        if "before_count" in call["params"] and "timestamp" in call["params"] and "pod_name" in call["params"]
    )
    assert pod_before_call["params"]["pod_name"] == "checkout-1"
    assert pod_before_call["params"]["namespace"] == "prod"
    assert pod_before_call["params"]["container_name"] == "checkout"
    assert pod_before_call["params"]["timestamp"] == "2026-03-01 10:00:00.000000"


def test_query_log_detail_rejects_empty_id():
    storage = FakeStorageAdapter()
    with pytest.raises(HTTPException) as exc_info:
        logs_service.query_log_detail(storage_adapter=storage, log_id="   ")
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_query_logs_route_preserves_http_exception(monkeypatch: pytest.MonkeyPatch):
    query_routes.set_storage_adapter(FakeStorageAdapter())

    def _raise_invalid_cursor(**_: Any):
        raise HTTPException(status_code=400, detail="Invalid cursor")

    monkeypatch.setattr(query_routes.logs_query_utils, "query_logs", _raise_invalid_cursor)

    with pytest.raises(HTTPException) as exc_info:
        await query_routes.query_logs(cursor="bad-cursor")
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Invalid cursor"


@pytest.mark.asyncio
async def test_query_logs_facets_route_preserves_http_exception(monkeypatch: pytest.MonkeyPatch):
    query_routes.set_storage_adapter(FakeStorageAdapter())

    def _raise_bad_request(**_: Any):
        raise HTTPException(status_code=422, detail="bad facets params")

    monkeypatch.setattr(query_routes.logs_query_utils, "query_logs_facets", _raise_bad_request)

    with pytest.raises(HTTPException) as exc_info:
        await query_routes.query_logs_facets()
    assert exc_info.value.status_code == 422
    assert exc_info.value.detail == "bad facets params"


@pytest.mark.asyncio
async def test_query_logs_aggregated_route_preserves_http_exception(monkeypatch: pytest.MonkeyPatch):
    query_routes.set_storage_adapter(FakeStorageAdapter())

    def _raise_bad_request(**_: Any):
        raise HTTPException(status_code=409, detail="aggregation conflict")

    monkeypatch.setattr(query_routes.logs_query_utils, "query_logs_aggregated", _raise_bad_request)

    with pytest.raises(HTTPException) as exc_info:
        await query_routes.query_logs_aggregated()
    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "aggregation conflict"


@pytest.mark.asyncio
async def test_query_logs_context_route_normalizes_whitespace_params(monkeypatch: pytest.MonkeyPatch):
    query_routes.set_storage_adapter(FakeStorageAdapter())
    captured: Dict[str, Any] = {}

    def _capture_params(**kwargs: Any):
        captured.update(kwargs)
        return {"count": 0, "data": []}

    monkeypatch.setattr(query_routes.logs_query_utils, "query_logs_context", _capture_params)

    result = await query_routes.query_logs_context(
        log_id="  log-1  ",
        trace_id="  trace-1  ",
        pod_name="  checkout-1  ",
        namespace="  prod  ",
        container_name="  checkout  ",
        timestamp=" 2026-03-01T10:00:00Z  ",
        before_count=1,
        after_count=1,
        limit=10,
    )

    assert result["count"] == 0
    assert captured["log_id"] == "log-1"
    assert captured["trace_id"] == "trace-1"
    assert captured["pod_name"] == "checkout-1"
    assert captured["namespace"] == "prod"
    assert captured["container_name"] == "checkout"
    assert captured["timestamp"] == "2026-03-01T10:00:00Z"


@pytest.mark.asyncio
async def test_query_logs_stats_route_normalizes_time_window(monkeypatch: pytest.MonkeyPatch):
    query_routes.set_storage_adapter(FakeStorageAdapter())
    captured: Dict[str, Any] = {}

    def _capture_stats(_storage: Any, *, time_window: str):
        captured["time_window"] = time_window
        return {"count": 1}

    monkeypatch.setattr(query_routes.obs_query_utils, "query_logs_stats", _capture_stats)

    result = await query_routes.query_logs_stats(time_window=" 24 HOUR ")
    assert result["count"] == 1
    assert captured["time_window"] == "24 HOUR"


@pytest.mark.asyncio
async def test_query_logs_stats_route_preserves_http_exception(monkeypatch: pytest.MonkeyPatch):
    query_routes.set_storage_adapter(FakeStorageAdapter())

    def _raise_bad_request(_storage: Any, *, time_window: str):
        raise HTTPException(status_code=422, detail=f"invalid window: {time_window}")

    monkeypatch.setattr(query_routes.obs_query_utils, "query_logs_stats", _raise_bad_request)

    with pytest.raises(HTTPException) as exc_info:
        await query_routes.query_logs_stats(time_window="bad-window")
    assert exc_info.value.status_code == 422
    assert str(exc_info.value.detail).startswith("invalid window:")


def test_convert_timestamp_preserves_microseconds_for_exact_queries():
    assert query_routes._convert_timestamp("2026-03-01T10:00:00.123456Z") == "2026-03-01 10:00:00.123456"


def test_format_timestamp_for_clickhouse_utc_preserves_microseconds():
    assert logs_service._format_timestamp_for_clickhouse_utc("2026-03-01T10:00:00.123456Z") == "2026-03-01 10:00:00.123456"
