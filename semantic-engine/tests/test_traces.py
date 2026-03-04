"""
Traces API 单元测试
测试 OpenTelemetry Traces 数据的解析和处理
"""
import pytest
from api.traces import (
    parse_otlp_attributes,
    process_otlp_traces_json,
    parse_span_data
)
from datetime import datetime


class TestParseSpanData:
    """测试 Span 数据解析"""

    def test_parse_basic_span(self):
        """测试基本 Span 解析"""
        span = {
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
                {"key": "http.url", "value": {"stringValue": "/api/users"}}
            ],
            "events": [],
            "links": []
        }

        result = parse_span_data(
            span,
            service_name="api-server",
            pod_name="api-pod-123",
            namespace="default",
            resource_attrs={}
        )

        assert result["trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"
        assert result["span_id"] == "00f067aa0ba902b7"
        assert result["parent_span_id"] == ""
        assert result["operation_name"] == "GET /api/users"
        assert result["duration_ms"] == 100  # 10ms

    def test_parse_span_with_parent(self):
        """测试带父 Span 的 Span"""
        span = {
            "traceId": "trace123",
            "spanId": "span456",
            "parentSpanId": "parent789",
            "name": "database.query",
            "kind": "CLIENT",
            "startTimeUnixNano": 1738892346000000000,
            "endTimeUnixNano": 1738892346050000000,
            "status": {"code": "OK"},
            "attributes": [],
            "events": [],
            "links": []
        }

        result = parse_span_data(
            span,
            service_name="backend",
            pod_name="backend-pod",
            namespace="default",
            resource_attrs={}
        )

        assert result["parent_span_id"] == "parent789"
        assert result["duration_ms"] == 50  # 50ms

    def test_parse_span_with_events(self):
        """测试带事件的 Span"""
        span = {
            "traceId": "trace123",
            "spanId": "span456",
            "parentSpanId": "",
            "name": "operation",
            "kind": "INTERNAL",
            "startTimeUnixNano": 1738892346000000000,
            "endTimeUnixNano": 1738892346100000000,
            "status": {"code": "OK"},
            "attributes": [],
            "events": [
                {
                    "timeUnixNano": 1738892346020000000,
                    "name": "cache.miss",
                    "attributes": [
                        {"key": "cache.key", "value": {"stringValue": "user:123"}}
                    ]
                }
            ],
            "links": []
        }

        result = parse_span_data(
            span,
            service_name="cache",
            pod_name="cache-pod",
            namespace="default",
            resource_attrs={}
        )

        # events 应该被序列化为 JSON
        import json
        events = json.loads(result["events"])
        assert len(events) == 1
        assert events[0]["name"] == "cache.miss"

    def test_parse_span_with_links(self):
        """测试带链接的 Span"""
        span = {
            "traceId": "trace123",
            "spanId": "span456",
            "parentSpanId": "",
            "name": "async.operation",
            "kind": "PRODUCER",
            "startTimeUnixNano": 1738892346000000000,
            "endTimeUnixNano": 1738892346100000000,
            "status": {"code": "OK"},
            "attributes": [],
            "events": [],
            "links": [
                {
                    "traceId": "related-trace",
                    "spanId": "related-span"
                }
            ]
        }

        result = parse_span_data(
            span,
            service_name="producer",
            pod_name="producer-pod",
            namespace="default",
            resource_attrs={}
        )

        import json
        links = json.loads(result["links"])
        assert len(links) == 1
        assert links[0]["traceId"] == "related-trace"

    def test_parse_span_error_status(self):
        """测试带错误状态的 Span"""
        span = {
            "traceId": "trace123",
            "spanId": "span456",
            "parentSpanId": "",
            "name": "failing.operation",
            "kind": "INTERNAL",
            "startTimeUnixNano": 1738892346000000000,
            "endTimeUnixNano": 1738892346150000000,
            "status": {
                "code": "ERROR",
                "message": "Database connection failed"
            },
            "attributes": [],
            "events": [],
            "links": []
        }

        result = parse_span_data(
            span,
            service_name="service",
            pod_name="pod",
            namespace="default",
            resource_attrs={}
        )

        assert result["status_code"] == "ERROR"

    def test_calculate_duration(self):
        """测试持续时间计算"""
        span = {
            "traceId": "trace",
            "spanId": "span",
            "parentSpanId": "",
            "name": "test",
            "kind": "INTERNAL",
            "startTimeUnixNano": 1000000000,  # 1秒
            "endTimeUnixNano": 1500000000,    # 1.5秒
            "status": {"code": "OK"},
            "attributes": [],
            "events": [],
            "links": []
        }

        result = parse_span_data(
            span,
            service_name="test",
            pod_name="test",
            namespace="default",
            resource_attrs={}
        )

        # 持续时间 = (1.5 - 1) 秒 = 500ms
        assert result["duration_ms"] == 500

    def test_merge_attributes(self):
        """测试合并 resource attributes 和 span attributes"""
        span = {
            "traceId": "trace",
            "spanId": "span",
            "parentSpanId": "",
            "name": "test",
            "kind": "INTERNAL",
            "startTimeUnixNano": 1738892346000000000,
            "endTimeUnixNano": 1738892346100000000,
            "status": {"code": "OK"},
            "attributes": [
                {"key": "custom.attr", "value": {"stringValue": "custom"}}
            ],
            "events": [],
            "links": []
        }

        resource_attrs = {"service.name": "my-service", "env": "prod"}

        result = parse_span_data(
            span,
            service_name="my-service",
            pod_name="pod",
            namespace="default",
            resource_attrs=resource_attrs
        )

        import json
        tags = json.loads(result["tags"])
        assert tags["service.name"] == "my-service"
        assert tags["env"] == "prod"
        assert tags["custom.attr"] == "custom"


class TestProcessOtlpTracesJson:
    """测试 OTLP Traces JSON 处理"""

    def test_process_single_span(self):
        """测试处理单个 Span"""
        otlp_data = {
            "resourceSpans": [{
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": "test-service"}}
                    ]
                },
                "scopeSpans": [{
                    "scope": {"name": "test"},
                    "spans": [{
                        "traceId": "trace123",
                        "spanId": "span456",
                        "parentSpanId": "",
                        "name": "operation",
                        "kind": "SERVER",
                        "startTimeUnixNano": 1738892346000000000,
                        "endTimeUnixNano": 1738892346100000000,
                        "status": {"code": "OK"},
                        "attributes": [],
                        "events": [],
                        "links": []
                    }]
                }]
            }]
        }

        result = process_otlp_traces_json(otlp_data)
        assert len(result) == 1
        assert result[0]["trace_id"] == "trace123"
        assert result[0]["operation_name"] == "operation"

    def test_process_multiple_spans(self):
        """测试处理多个 Spans"""
        otlp_data = {
            "resourceSpans": [{
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": "multi-span-service"}}
                    ]
                },
                "scopeSpans": [{
                    "scope": {"name": "test"},
                    "spans": [
                        {
                            "traceId": "trace123",
                            "spanId": "span1",
                            "parentSpanId": "",
                            "name": "parent",
                            "kind": "SERVER",
                            "startTimeUnixNano": 1738892346000000000,
                            "endTimeUnixNano": 1738892346200000000,
                            "status": {"code": "OK"},
                            "attributes": [],
                            "events": [],
                            "links": []
                        },
                        {
                            "traceId": "trace123",
                            "spanId": "span2",
                            "parentSpanId": "span1",
                            "name": "child",
                            "kind": "CLIENT",
                            "startTimeUnixNano": 1738892346050000000,
                            "endTimeUnixNano": 1738892346150000000,
                            "status": {"code": "OK"},
                            "attributes": [],
                            "events": [],
                            "links": []
                        }
                    ]
                }]
            }]
        }

        result = process_otlp_traces_json(otlp_data)
        assert len(result) == 2
        # 验证父子关系
        assert result[0]["span_id"] == "span1"
        assert result[1]["parent_span_id"] == "span1"

    def test_process_empty_traces(self):
        """测试处理空的 Traces"""
        otlp_data = {"resourceSpans": []}
        result = process_otlp_traces_json(otlp_data)
        assert len(result) == 0

    def test_extract_service_name(self):
        """测试提取 service name"""
        otlp_data = {
            "resourceSpans": [{
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": "my-service"}}
                    ]
                },
                "scopeSpans": [{
                    "scope": {"name": "test"},
                    "spans": [{
                        "traceId": "trace",
                        "spanId": "span",
                        "parentSpanId": "",
                        "name": "op",
                        "kind": "INTERNAL",
                        "startTimeUnixNano": 1738892346000000000,
                        "endTimeUnixNano": 1738892346100000000,
                        "status": {"code": "OK"},
                        "attributes": [],
                        "events": [],
                        "links": []
                    }]
                }]
            }]
        }

        result = process_otlp_traces_json(otlp_data)
        assert result[0]["service_name"] == "my-service"

    def test_extract_k8s_metadata(self):
        """测试提取 Kubernetes metadata"""
        otlp_data = {
            "resourceSpans": [{
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": "app"}},
                        {
                            "key": "kubernetes",
                            "value": {
                                "kvlistValue": {
                                    "values": [
                                        {"key": "pod_name", "value": {"stringValue": "app-pod-123"}},
                                        {"key": "namespace_name", "value": {"stringValue": "production"}}
                                    ]
                                }
                            }
                        }
                    ]
                },
                "scopeSpans": [{
                    "scope": {"name": "test"},
                    "spans": [{
                        "traceId": "trace",
                        "spanId": "span",
                        "parentSpanId": "",
                        "name": "op",
                        "kind": "INTERNAL",
                        "startTimeUnixNano": 1738892346000000000,
                        "endTimeUnixNano": 1738892346100000000,
                        "status": {"code": "OK"},
                        "attributes": [],
                        "events": [],
                        "links": []
                    }]
                }]
            }]
        }

        result = process_otlp_traces_json(otlp_data)
        # K8s metadata 应该在 tags 中
        import json
        tags = json.loads(result[0]["tags"])
        assert "kubernetes" in tags
        assert tags["kubernetes"]["pod_name"] == "app-pod-123"


class TestSpanKinds:
    """测试不同的 Span Kind"""

    @pytest.mark.parametrize("span_kind,expected", [
        ("INTERNAL", "INTERNAL"),
        ("SERVER", "SERVER"),
        ("CLIENT", "CLIENT"),
        ("PRODUCER", "PRODUCER"),
        ("CONSUMER", "CONSUMER"),
    ])
    def test_span_kind(self, span_kind, expected):
        """测试各种 Span Kind"""
        span = {
            "traceId": "trace",
            "spanId": "span",
            "parentSpanId": "",
            "name": "operation",
            "kind": span_kind,
            "startTimeUnixNano": 1738892346000000000,
            "endTimeUnixNano": 1738892346100000000,
            "status": {"code": "OK"},
            "attributes": [],
            "events": [],
            "links": []
        }

        result = parse_span_data(
            span,
            service_name="test",
            pod_name="test",
            namespace="default",
            resource_attrs={}
        )

        assert result["span_kind"] == expected


class TestEdgeCases:
    """测试边界情况和错误处理"""

    def test_missing_optional_fields(self):
        """测试缺少可选字段"""
        span = {
            "traceId": "trace",
            "spanId": "span",
            # parentSpanId 缺失
            "name": "operation",
            # kind 缺失（应使用默认值）
            "startTimeUnixNano": 1738892346000000000,
            "endTimeUnixNano": 1738892346100000000,
            # status 缺失
            "attributes": [],
            "events": [],
            "links": []
        }

        result = parse_span_data(
            span,
            service_name="test",
            pod_name="test",
            namespace="default",
            resource_attrs={}
        )

        assert result["parent_span_id"] == ""  # 默认值
        assert result["span_kind"] == "INTERNAL"  # 默认值

    def test_empty_events_and_links(self):
        """测试空 events 和 links"""
        span = {
            "traceId": "trace",
            "spanId": "span",
            "parentSpanId": "",
            "name": "operation",
            "kind": "INTERNAL",
            "startTimeUnixNano": 1738892346000000000,
            "endTimeUnixNano": 1738892346100000000,
            "status": {"code": "OK"},
            "attributes": [],
            "events": [],
            "links": []
        }

        result = parse_span_data(
            span,
            service_name="test",
            pod_name="test",
            namespace="default",
            resource_attrs={}
        )

        import json
        events = json.loads(result["events"])
        links = json.loads(result["links"])
        assert events == []
        assert links == []
