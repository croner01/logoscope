"""Regression tests for shared_src.storage.adapter HTTP fallback behavior."""

import os
import sys
from typing import Any, Dict


# 添加 query-service 根目录到 Python 路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from shared_src.storage import adapter as legacy_adapter


class _DummyResponse:
    def __init__(self, status_code: int = 200, text: str = '{"data": []}'):
        self.status_code = status_code
        self.text = text


class _CaptureNativeClient:
    """Capture native ClickHouse query strings."""

    def __init__(self, rows=None):
        self.rows = rows or []
        self.queries = []

    def execute(self, query, *args, **kwargs):
        self.queries.append(str(query))
        return self.rows


def _build_http_only_adapter() -> legacy_adapter.StorageAdapter:
    adapter = legacy_adapter.StorageAdapter.__new__(legacy_adapter.StorageAdapter)
    adapter.ch_client = None
    adapter.ch_http_client = {
        "url": "http://localhost:8123",
        "database": "logs",
        "user": "default",
        "password": "",
    }
    return adapter


def test_legacy_execute_query_uses_http_fallback_and_renders_params(monkeypatch):
    """无 native client 时 execute_query 应走 HTTP，并正确渲染参数。"""
    adapter = _build_http_only_adapter()
    captured: Dict[str, Any] = {}

    def fake_get(url: str, params=None, auth=None, timeout=None):
        captured["url"] = url
        captured["params"] = params or {}
        captured["auth"] = auth
        captured["timeout"] = timeout
        return _DummyResponse(text='{"data":[{"value":7}]}')

    monkeypatch.setattr(legacy_adapter.requests, "get", fake_get)

    rows = adapter.execute_query(
        "SELECT {value:Int32} AS value",
        {"value": 7},
    )

    assert rows == [{"value": 7}]
    assert captured["params"]["query"] == "SELECT 7 AS value FORMAT JSON"


def test_legacy_execute_query_http_returns_empty_on_non_json(monkeypatch):
    """HTTP 返回非 JSON 文本时应安全降级为空列表，不抛异常。"""
    adapter = _build_http_only_adapter()

    def fake_get(url: str, params=None, auth=None, timeout=None):
        return _DummyResponse(text="not-json")

    monkeypatch.setattr(legacy_adapter.requests, "get", fake_get)

    rows = adapter.execute_query("SELECT 1")
    assert rows == []


def test_legacy_get_trace_spans_escapes_trace_id_in_query():
    """trace_id 应进行 SQL 字符串转义，避免注入拼接。"""
    adapter = legacy_adapter.StorageAdapter.__new__(legacy_adapter.StorageAdapter)
    adapter.ch_client = _CaptureNativeClient(rows=[])

    adapter.get_trace_spans("abc' OR 1=1 --")

    assert len(adapter.ch_client.queries) == 1
    query = adapter.ch_client.queries[0]
    assert "PREWHERE trace_id = 'abc'' OR 1=1 --'" in query


def test_legacy_get_log_context_escapes_inputs_and_clamps_limits():
    """pod_name/timestamp 应安全拼接，before/after limit 需至少为 1。"""
    adapter = legacy_adapter.StorageAdapter.__new__(legacy_adapter.StorageAdapter)
    adapter.ch_client = _CaptureNativeClient(rows=[])

    result = adapter.get_log_context(
        pod_name="pod-a' OR 1=1 --",
        timestamp="2026-03-01T10:11:12.123456Z",
        before_count=-3,
        after_count=0,
    )

    assert result == {"before": [], "after": [], "current": None}
    assert len(adapter.ch_client.queries) == 3
    assert all("pod_name = 'pod-a'' OR 1=1 --'" in query for query in adapter.ch_client.queries)
    assert "LIMIT 1" in adapter.ch_client.queries[0]
    assert "LIMIT 1" in adapter.ch_client.queries[1]


def test_legacy_get_metrics_escapes_service_and_metric_name():
    """get_metrics 的 service_name/metric_name 过滤条件应转义。"""
    adapter = legacy_adapter.StorageAdapter.__new__(legacy_adapter.StorageAdapter)
    adapter.ch_client = _CaptureNativeClient(rows=[])

    adapter.get_metrics(
        limit=10,
        service_name="svc' OR 1=1 --",
        metric_name="cpu' OR 'x'='x",
    )

    assert len(adapter.ch_client.queries) == 1
    query = adapter.ch_client.queries[0]
    assert "service_name = 'svc'' OR 1=1 --'" in query
    assert "metric_name = 'cpu'' OR ''x''=''x'" in query


def test_legacy_get_traces_escapes_service_and_trace_id():
    """get_traces 的 service_name/trace_id 过滤条件应转义。"""
    adapter = legacy_adapter.StorageAdapter.__new__(legacy_adapter.StorageAdapter)
    adapter.ch_client = _CaptureNativeClient(rows=[])

    adapter.get_traces(
        limit=10,
        service_name="svc' OR 1=1 --",
        trace_id="trace-' OR 1=1 --",
    )

    assert len(adapter.ch_client.queries) == 1
    query = adapter.ch_client.queries[0]
    assert "service_name = 'svc'' OR 1=1 --'" in query
    assert "trace_id = 'trace-'' OR 1=1 --'" in query
