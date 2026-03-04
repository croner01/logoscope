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

        if "timestamp < toDateTime64({timestamp:String}, 9)" in condensed:
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

        if "timestamp > toDateTime64({timestamp:String}, 9)" in condensed:
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

        if "timestamp = toDateTime64({timestamp:String}, 9)" in condensed:
            return [
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
    )

    query_call = storage.calls[0]
    assert (
        "service_name = {service_name_0:String}" in query_call["query"]
        or "service_name = {service_name:String}" in query_call["query"]
    )
    assert "level_norm = {level_0:String}" in query_call["query"]
    assert "timestamp < toDateTime64({cursor_timestamp:String}, 9)" in query_call["query"]
    assert (query_call["params"].get("service_name_0") or query_call["params"].get("service_name")) == "checkout"
    assert query_call["params"]["limit_plus_one"] == 2
    assert query_call["params"]["search"] == "timeout"

    assert result["count"] == 1
    assert result["has_more"] is True
    assert result["next_cursor"] is not None
    assert result["data"][0]["labels"]["app"] == "checkout"
    assert result["data"][0]["attributes"]["peer.service"] == "payment"


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
    assert "timestamp <= toDateTime64({end_time:String}, 9)" in query_call["query"]
    assert "timestamp > toDateTime64({end_time:String}, 9) - INTERVAL 8 HOUR" in query_call["query"]


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
    assert "timestamp > toDateTime64({end_time:String}, 9) - INTERVAL 4 HOUR" in storage.calls[0]["query"]
    assert "timestamp > toDateTime64({end_time:String}, 9) - INTERVAL 4 HOUR" in storage.calls[1]["query"]


def test_query_logs_context_mode_and_detail():
    storage = FakeStorageAdapter()

    trace_result = logs_service.query_logs_context(
        storage_adapter=storage,
        log_id=None,
        trace_id="trace-ctx-1",
        pod_name=None,
        namespace=None,
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
        timestamp="2026-03-01T10:00:00Z",
        before_count=1,
        after_count=1,
        limit=100,
        convert_timestamp_fn=query_routes._convert_timestamp,
    )
    assert pod_result["before_count"] == 1
    assert pod_result["after_count"] == 1
    assert pod_result["current"]["message"] == "current"

    before_call = next(call for call in storage.calls if "before_count" in call["params"])
    assert before_call["params"]["timestamp"] == "2026-03-01 10:00:00.000"

    by_id_result = logs_service.query_logs_context(
        storage_adapter=storage,
        log_id="ctx-anchor-1",
        trace_id=None,
        pod_name="ignored-pod",
        namespace="ignored-ns",
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

    by_id_before_call = next(
        call
        for call in storage.calls
        if call["params"].get("anchor_id") == "ctx-anchor-1" and "before_count" in call["params"]
    )
    assert by_id_before_call["params"]["pod_name"] == "checkout-1"
    assert by_id_before_call["params"]["namespace"] == "prod"
    assert by_id_before_call["params"]["anchor_timestamp"] == "2026-03-01 10:00:00.000"

    detail = logs_service.query_log_detail(storage_adapter=storage, log_id="exists")
    assert detail["data"]["id"] == "exists"
    assert detail["data"]["attributes"]["a"] == 1

    with pytest.raises(HTTPException) as exc_info:
        logs_service.query_log_detail(storage_adapter=storage, log_id="missing")
    assert exc_info.value.status_code == 404
