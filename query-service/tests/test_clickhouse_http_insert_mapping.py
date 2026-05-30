"""Regression tests for ClickHouse HTTP INSERT column mapping."""

import json
import os
import sys
from datetime import datetime
from typing import Any, Dict, List


# 添加 query-service 根目录到 Python 路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from storage.adapter import StorageAdapter
from logoscope_storage import adapter as shared_adapter


class _DummyResponse:
    def __init__(self, status_code: int = 200, text: str = '{"data": []}'):
        self.status_code = status_code
        self.text = text


class _CaptureHttpSession:
    """Capture outgoing HTTP calls without real network I/O."""

    def __init__(self, response_text: str = '{"data": []}', status_code: int = 200):
        self.calls: List[Dict[str, Any]] = []
        self.response_text = response_text
        self.status_code = status_code

    def post(self, url: str, **kwargs: Any):
        self.calls.append({"method": "post", "url": url, **kwargs})
        return _DummyResponse(status_code=self.status_code, text=self.response_text)

    def get(self, url: str, **kwargs: Any):
        self.calls.append({"method": "get", "url": url, **kwargs})
        return _DummyResponse(status_code=self.status_code, text=self.response_text)


def _build_adapter() -> StorageAdapter:
    adapter = StorageAdapter.__new__(StorageAdapter)
    adapter.ch_http_client = {
        "url": "http://localhost:8123",
        "database": "logs",
        "user": "default",
        "password": "",
    }
    adapter.http_session = _CaptureHttpSession()
    return adapter


def test_execute_clickhouse_http_maps_insert_columns_case_insensitive():
    """INSERT 列映射应大小写无关，支持 metrics/traces 等非 logs 列结构。"""
    adapter = _build_adapter()
    row = [
        "latency_p95",
        datetime(2026, 3, 1, 10, 11, 12, 345678),
        128.5,
        '{"env":"prod"}',
        "checkout",
    ]
    query = (
        "insert into logs.metrics "
        "(metric_name, timestamp, value_float64, attributes_json, service_name) values"
    )

    adapter._execute_clickhouse_http(query, [row])

    assert len(adapter.http_session.calls) == 1
    call = adapter.http_session.calls[0]
    assert call["method"] == "post"
    assert "FORMAT JSONEachRow" in call["params"]["query"]

    body_lines = str(call["data"]).splitlines()
    assert len(body_lines) == 1
    payload = json.loads(body_lines[0])
    assert payload["metric_name"] == "latency_p95"
    assert payload["timestamp"] == "2026-03-01 10:11:12.345678"
    assert payload["value_float64"] == 128.5
    assert payload["attributes_json"] == '{"env":"prod"}'
    assert payload["service_name"] == "checkout"


def test_execute_clickhouse_http_logs_warning_for_non_json_response(caplog):
    """SELECT 响应非 JSON 时应记录告警，避免静默吞异常。"""
    adapter = _build_adapter()
    adapter.http_session = _CaptureHttpSession(response_text="OK", status_code=200)
    caplog.set_level("WARNING")

    result = adapter._execute_clickhouse_http("SELECT 1")

    assert result == "OK"
    assert any(
        "ClickHouse HTTP response is not valid JSON" in record.message
        for record in caplog.records
    )


def test_parse_json_object_payload_warns_on_invalid_json(caplog):
    """JSON 解析失败时应返回空字典并输出告警。"""
    caplog.set_level("WARNING")

    result = shared_adapter._parse_json_object_payload(
        "{invalid-json",
        field_name="labels",
        event_id="TEST_JSON_PARSE_FAILED",
        source="unit-test",
    )

    assert result == {}
    assert any("Failed to parse JSON object payload" in record.message for record in caplog.records)


def test_parse_json_object_payload_warns_on_non_object_json(caplog):
    """JSON 解析成功但不是对象时也要可观测。"""
    caplog.set_level("WARNING")

    result = shared_adapter._parse_json_object_payload(
        "[1,2,3]",
        field_name="labels",
        event_id="TEST_JSON_NOT_OBJECT",
        source="unit-test",
    )

    assert result == {}
    assert any("JSON payload is not an object" in record.message for record in caplog.records)
