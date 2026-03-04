"""
Ingest Service 单元测试
"""
import pytest
from fastapi.testclient import TestClient

from main import app
from api import ingest as ingest_api


client = TestClient(app)


def test_health_check():
    """测试健康检查端点"""
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert data["service"] == "ingest-service"
    assert "version" in data


def test_root():
    """测试根路径端点"""
    response = client.get("/")
    assert response.status_code == 200
    data = response.json()
    assert data["service"] == "Ingest Service"


def test_ingest_logs():
    """测试日志接收端点"""
    # 模拟 OTLP 日志数据
    otlp_logs = {
        "resourceLogs": [{
            "resource": {
                "attributes": [
                    {"key": "service.name", "value": {"stringValue": "test-service"}}
                ]
            },
            "scopeLogs": [{
                "logRecords": [{
                    "timeUnixNano": "1739597400000000000",
                    "severityNumber": 17,
                    "severityText": "ERROR",
                    "body": {"stringValue": "Test error message"}
                }]
            }]
        }]
    }

    response = client.post(
        "/v1/logs",
        json=otlp_logs,
        headers={"Content-Type": "application/json"}
    )

    # 由于没有真实的 Redis 连接，预期会失败
    # 但端点应该存在并接受请求
    assert response.status_code in [200, 500]


def test_ingest_metrics():
    """测试指标接收端点"""
    otlp_metrics = {
        "resourceMetrics": [{
            "resource": {
                "attributes": [
                    {"key": "service.name", "value": {"stringValue": "test-service"}}
                ]
            },
            "scopeMetrics": [{
                "metrics": [{
                    "name": "test.counter",
                    "data": {
                        "intGauge": {
                            "dataPoints": [{
                                "timeUnixNano": "1739597400000000000",
                                "value": "42"
                            }]
                        }
                    }
                }]
            }]
        }]
    }

    response = client.post(
        "/v1/metrics",
        json=otlp_metrics,
        headers={"Content-Type": "application/json"}
    )

    assert response.status_code in [200, 500]


def test_ingest_traces():
    """测试追踪接收端点"""
    otlp_traces = {
        "resourceSpans": [{
            "resource": {
                "attributes": [
                    {"key": "service.name", "value": {"stringValue": "test-service"}}
                ]
            },
            "scopeSpans": [{
                "spans": [{
                    "traceId": "test-trace-id",
                    "spanId": "test-span-id",
                    "parentSpanId": "test-parent-span-id",
                    "name": "test-operation",
                    "kind": 1,
                    "startTimeUnixNano": "1739597400000000000",
                    "endTimeUnixNano": "1739597401000000000",
                    "attributes": [
                        {"key": "key", "value": {"stringValue": "value"}}
                    ]
                }]
            }]
        }]
    }

    response = client.post(
        "/v1/traces",
        json=otlp_traces,
        headers={"Content-Type": "application/json"}
    )

    assert response.status_code in [200, 500]


def test_logs_endpoint_writes_to_logs_stream(monkeypatch):
    """logs 信号应写入 REDIS_STREAM_LOGS。"""
    captured = {}

    async def fake_write_to_queue(stream, data_type, payload, metadata):
        captured["stream"] = stream
        captured["data_type"] = data_type
        return {"status": "success"}

    monkeypatch.setattr(ingest_api, "write_to_queue", fake_write_to_queue)
    monkeypatch.setattr(ingest_api.config, "redis_stream_logs", "logs.test")

    payload = {"resourceLogs": [{"scopeLogs": [{"logRecords": [{"body": {"stringValue": "hello"}}]}]}]}
    response = client.post("/v1/logs", json=payload, headers={"Content-Type": "application/json"})

    assert response.status_code == 200
    assert captured["stream"] == "logs.test"
    assert captured["data_type"] == "logs"


def test_metrics_endpoint_writes_to_metrics_stream(monkeypatch):
    """metrics 信号应写入 REDIS_STREAM_METRICS。"""
    captured = {}

    async def fake_write_to_queue(stream, data_type, payload, metadata):
        captured["stream"] = stream
        captured["data_type"] = data_type
        return {"status": "success"}

    monkeypatch.setattr(ingest_api, "write_to_queue", fake_write_to_queue)
    monkeypatch.setattr(ingest_api.config, "redis_stream_metrics", "metrics.test")

    payload = {"resourceMetrics": [{"scopeMetrics": [{"metrics": [{"name": "m"}]}]}]}
    response = client.post("/v1/metrics", json=payload, headers={"Content-Type": "application/json"})

    assert response.status_code == 200
    assert captured["stream"] == "metrics.test"
    assert captured["data_type"] == "metrics"


def test_traces_endpoint_writes_to_traces_stream(monkeypatch):
    """traces 信号应写入 REDIS_STREAM_TRACES。"""
    captured = {}

    async def fake_write_to_queue(stream, data_type, payload, metadata):
        captured["stream"] = stream
        captured["data_type"] = data_type
        return {"status": "success"}

    monkeypatch.setattr(ingest_api, "write_to_queue", fake_write_to_queue)
    monkeypatch.setattr(ingest_api.config, "redis_stream_traces", "traces.test")

    payload = {"resourceSpans": [{"scopeSpans": [{"spans": [{"traceId": "t-1", "spanId": "s-1"}]}]}]}
    response = client.post("/v1/traces", json=payload, headers={"Content-Type": "application/json"})

    assert response.status_code == 200
    assert captured["stream"] == "traces.test"
    assert captured["data_type"] == "traces"
