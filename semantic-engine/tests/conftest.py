"""
Pytest配置和共享fixture
"""
import sys
import os
import pytest

# 添加项目根目录到Python路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from normalize.normalizer import normalize_log, extract_service_name, extract_k8s_context
from storage.adapter import StorageAdapter


@pytest.fixture
def clear_stats():
    """重置缓存统计，供缓存测试复用。"""
    from api.cache import _cache_store, reset_cache_stats

    _cache_store.clear()
    reset_cache_stats()
    yield
    _cache_store.clear()
    reset_cache_stats()


@pytest.fixture
def sample_metrics_data():
    """示例 Metrics 数据"""
    return {
        "resourceMetrics": [{
            "resource": {
                "attributes": [
                    {"key": "service.name", "value": {"stringValue": "test-service"}},
                    {"key": "deployment.environment", "value": {"stringValue": "production"}}
                ]
            },
            "scopeMetrics": [{
                "scope": {
                    "name": "test-metrics-scope"
                },
                "metrics": [
                    {
                        "name": "process.memory.usage",
                        "description": "Process memory usage in bytes",
                        "unit": "By",
                        "gauge": {
                            "dataPoints": [{
                                "timeUnixNano": 1738892346000000000,
                                "asDouble": 125829120,
                                "attributes": [
                                    {"key": "type", "value": {"stringValue": "heap"}}
                                ]
                            }]
                        }
                    },
                    {
                        "name": "http.requests.total",
                        "description": "Total HTTP requests",
                        "unit": "1",
                        "sum": {
                            "isMonotonic": True,
                            "dataPoints": [{
                                "timeUnixNano": 1738892346000000000,
                                "asInt": 1000,
                                "attributes": [
                                    {"key": "method", "value": {"stringValue": "GET"}},
                                    {"key": "status", "value": {"stringValue": "200"}}
                                ]
                            }]
                        }
                    }
                ]
            }]
        }]
    }


@pytest.fixture
def sample_traces_data():
    """示例 Traces 数据"""
    return {
        "resourceSpans": [{
            "resource": {
                "attributes": [
                    {"key": "service.name", "value": {"stringValue": "test-service"}}
                ]
            },
            "scopeSpans": [{
                "scope": {
                    "name": "test-traces-scope"
                },
                "spans": [
                    {
                        "traceId": "4bf92f3577b34da6a3ce929d0e0e4736",
                        "spanId": "00f067aa0ba902b7",
                        "parentSpanId": "",
                        "name": "GET /api/users",
                        "kind": "SERVER",
                        "startTimeUnixNano": 1738892346000000000,
                        "endTimeUnixNano": 1738892346100000000,
                        "status": {
                            "code": "OK"
                        },
                        "attributes": [
                            {"key": "http.method", "value": {"stringValue": "GET"}},
                            {"key": "http.url", "value": {"stringValue": "/api/users"}},
                            {"key": "http.status_code", "value": {"intValue": "200"}}
                        ],
                        "events": [],
                        "links": []
                    },
                    {
                        "traceId": "4bf92f3577b34da6a3ce929d0e0e4736",
                        "spanId": "child123",
                        "parentSpanId": "00f067aa0ba902b7",
                        "name": "database.query",
                        "kind": "CLIENT",
                        "startTimeUnixNano": 1738892346020000000,
                        "endTimeUnixNano": 1738892346080000000,
                        "status": {
                            "code": "OK"
                        },
                        "attributes": [
                            {"key": "db.system", "value": {"stringValue": "postgresql"}},
                            {"key": "db.name", "value": {"stringValue": "users"}}
                        ],
                        "events": [],
                        "links": []
                    }
                ]
            }]
        }]
    }


@pytest.fixture
def sample_log_data():
    """示例日志数据"""
    return {
        "service.name": "test-service",
        "kubernetes": {
            "pod_name": "test-pod-123",
            "namespace_name": "default",
            "node_name": "node-1",
            "pod_id": "pod-uuid-123",
            "host_ip": "10.0.0.1",
            "labels": {
                "app": "test",
                "version": "v1.0"
            }
        },
        "message": "Test log message",
        "timestamp": "2026-02-07T00:00:00Z",
        "level": "info",
        "trace_id": "trace-123",
        "span_id": "span-456"
    }


@pytest.fixture
def sample_otlp_log():
    """示例OTLP格式日志数据"""
    return {
        "service.name": "log-generator-568b584664-fv422",
        "kubernetes": {
            "pod_name": "log-generator-568b584664-fv422",
            "namespace_name": "islap",
            "container_name": "log-generator",
            "labels": {
                "app": "log-generator"
            }
        },
        "timestamp_unix_nano": 1738892346814567000,
        "severity": "info",
        "message": "2026-02-07T00:45:46 UTC 2026 - INFO - Test log message"
    }


@pytest.fixture
def sample_event():
    """标准化后的事件数据"""
    return {
        "id": "test-event-id",
        "timestamp": "2026-02-07T00:00:00Z",
        "entity": {
            "type": "service",
            "name": "test-service",
            "instance": "test-pod-123"
        },
        "event": {
            "type": "log",
            "level": "info",
            "name": "test-log",
            "raw": "Test log message"
        },
        "context": {
            "trace_id": "trace-123",
            "span_id": "span-456",
            "host": "node-1",
            "k8s": {
                "namespace": "default",
                "pod": "test-pod-123",
                "node": "node-1",
                "host": "node-1",
                "host_ip": "10.0.0.1",
                "labels": {"app": "test"},
                "pod_id": "pod-uuid-123"
            }
        },
        "relations": []
    }
