"""
Metrics API 单元测试
测试 OpenTelemetry Metrics 数据的解析和处理
"""
import pytest
from api.metrics import (
    parse_otlp_attributes,
    process_otlp_metrics_json,
    parse_number_data_points,
    parse_histogram_data_points,
    parse_summary_data_points,
    parse_metric_data
)
from datetime import datetime


class TestParseOtlpAttributes:
    """测试 OTLP attributes 数组到字典的转换"""

    def test_parse_string_attributes(self):
        """测试字符串属性"""
        attributes_list = [
            {"key": "service.name", "value": {"stringValue": "test-service"}},
            {"key": "env", "value": {"stringValue": "production"}}
        ]
        result = parse_otlp_attributes(attributes_list)
        assert result == {"service.name": "test-service", "env": "production"}

    def test_parse_int_attributes(self):
        """测试整数属性"""
        attributes_list = [
            {"key": "port", "value": {"intValue": "8080"}},
            {"key": "count", "value": {"intValue": "42"}}
        ]
        result = parse_otlp_attributes(attributes_list)
        assert result == {"port": "8080", "count": "42"}

    def test_parse_bool_attributes(self):
        """测试布尔属性"""
        attributes_list = [
            {"key": "enabled", "value": {"boolValue": True}},
            {"key": "disabled", "value": {"boolValue": False}}
        ]
        result = parse_otlp_attributes(attributes_list)
        assert result == {"enabled": True, "disabled": False}

    def test_parse_double_attributes(self):
        """测试浮点数属性"""
        attributes_list = [
            {"key": "ratio", "value": {"doubleValue": 0.95}}
        ]
        result = parse_otlp_attributes(attributes_list)
        assert result == {"ratio": 0.95}

    def test_parse_kvlist_attributes(self):
        """测试 kvlist 属性（如 kubernetes metadata）"""
        attributes_list = [
            {
                "key": "kubernetes",
                "value": {
                    "kvlistValue": {
                        "values": [
                            {"key": "pod_name", "value": {"stringValue": "test-pod"}},
                            {"key": "namespace", "value": {"stringValue": "default"}}
                        ]
                    }
                }
            }
        ]
        result = parse_otlp_attributes(attributes_list)
        assert result == {"kubernetes": {"pod_name": "test-pod", "namespace": "default"}}

    def test_parse_empty_attributes(self):
        """测试空属性列表"""
        result = parse_otlp_attributes([])
        assert result == {}


class TestParseNumberDataPoints:
    """测试数值数据点解析（Gauge/Sum）"""

    def test_parse_gauge_double_value(self):
        """测试 Gauge 类型（浮点数值）"""
        data_points = {
            "dataPoints": [
                {
                    "timeUnixNano": 1738892346000000000,
                    "asDouble": 123.45,
                    "attributes": [
                        {"key": "unit", "value": {"stringValue": "bytes"}}
                    ]
                }
            ]
        }
        resource_attrs = {"service.name": "test-service"}
        result = parse_number_data_points(
            data_points, "memory.usage", "gauge", "test-service", resource_attrs
        )

        assert len(result) == 1
        assert result[0]["metric_name"] == "memory.usage"
        assert result[0]["metric_type"] == "gauge"
        assert result[0]["value"] == 123.45
        assert result[0]["attributes"]["unit"] == "bytes"
        assert result[0]["service_name"] == "test-service"

    def test_parse_counter_int_value(self):
        """测试 Counter 类型（整数值）"""
        data_points = {
            "dataPoints": [
                {
                    "timeUnixNano": 1738892346000000000,
                    "asInt": 1000,
                    "attributes": [
                        {"key": "method", "value": {"stringValue": "GET"}}
                    ]
                }
            ]
        }
        resource_attrs = {"service.name": "api-server"}
        result = parse_number_data_points(
            data_points, "http.requests", "counter", "api-server", resource_attrs
        )

        assert len(result) == 1
        assert result[0]["value"] == 1000.0
        assert result[0]["attributes"]["method"] == "GET"

    def test_parse_multiple_data_points(self):
        """测试多个数据点"""
        data_points = {
            "dataPoints": [
                {
                    "timeUnixNano": 1738892346000000000,
                    "asDouble": 100.0,
                    "attributes": [{"key": "label", "value": {"stringValue": "a"}}]
                },
                {
                    "timeUnixNano": 1738892356000000000,
                    "asDouble": 200.0,
                    "attributes": [{"key": "label", "value": {"stringValue": "b"}}]
                }
            ]
        }
        resource_attrs = {}
        result = parse_number_data_points(
            data_points, "test.metric", "gauge", "test-service", resource_attrs
        )

        assert len(result) == 2
        assert result[0]["value"] == 100.0
        assert result[1]["value"] == 200.0

    def test_merge_resource_attributes(self):
        """测试合并 resource attributes"""
        data_points = {
            "dataPoints": [
                {
                    "timeUnixNano": 1738892346000000000,
                    "asDouble": 50.0,
                    "attributes": [
                        {"key": "custom", "value": {"stringValue": "value"}}
                    ]
                }
            ]
        }
        resource_attrs = {"service.name": "my-service", "env": "prod"}
        result = parse_number_data_points(
            data_points, "test.metric", "gauge", "my-service", resource_attrs
        )

        assert result[0]["attributes"]["service.name"] == "my-service"
        assert result[0]["attributes"]["env"] == "prod"
        assert result[0]["attributes"]["custom"] == "value"


class TestParseHistogramDataPoints:
    """测试直方图数据点解析"""

    def test_parse_histogram_basic(self):
        """测试基本直方图"""
        histogram_data = {
            "dataPoints": [
                {
                    "timeUnixNano": 1738892346000000000,
                    "count": 100,
                    "sum": 5000.0,
                    "bucketCounts": [10, 30, 40, 20],
                    "explicitBounds": [10, 50, 100],
                    "attributes": [{"key": "endpoint", "value": {"stringValue": "/api"}}]
                }
            ]
        }
        resource_attrs = {}
        result = parse_histogram_data_points(
            histogram_data, "http.duration", "test-service", resource_attrs
        )

        assert len(result) == 1
        assert result[0]["metric_name"] == "http.duration"
        assert result[0]["metric_type"] == "histogram"
        assert result[0]["value"] == 5000.0
        assert result[0]["attributes"]["endpoint"] == "/api"

    def test_parse_histogram_with_data(self):
        """验证直方图数据正确序列化为 JSON"""
        import json
        histogram_data = {
            "dataPoints": [
                {
                    "timeUnixNano": 1738892346000000000,
                    "count": 100,
                    "sum": 5000.0,
                    "bucketCounts": [10, 30, 40, 20],
                    "explicitBounds": [10, 50, 100],
                    "attributes": []
                }
            ]
        }
        resource_attrs = {}
        result = parse_histogram_data_points(
            histogram_data, "test.metric", "test", resource_attrs
        )

        assert "histogram_data" in result[0]
        hist_data = json.loads(result[0]["histogram_data"])
        assert hist_data["count"] == 100
        assert hist_data["sum"] == 5000.0
        assert hist_data["bucket_counts"] == [10, 30, 40, 20]
        assert hist_data["explicit_bounds"] == [10, 50, 100]


class TestParseSummaryDataPoints:
    """测试摘要数据点解析"""

    def test_parse_summary_basic(self):
        """测试基本 Summary"""
        summary_data = {
            "dataPoints": [
                {
                    "timeUnixNano": 1738892346000000000,
                    "count": 1000,
                    "sum": 50000.0,
                    "quantileValues": [
                        {"quantile": 0.5, "value": 45.0},
                        {"quantile": 0.9, "value": 80.0},
                        {"quantile": 0.99, "value": 95.0}
                    ],
                    "attributes": []
                }
            ]
        }
        resource_attrs = {}
        result = parse_summary_data_points(
            summary_data, "response.time", "test-service", resource_attrs
        )

        assert len(result) == 1
        assert result[0]["metric_name"] == "response.time"
        assert result[0]["metric_type"] == "summary"
        assert result[0]["value"] == 50000.0

    def test_parse_summary_with_data(self):
        """验证 Summary 数据正确序列化为 JSON"""
        import json
        summary_data = {
            "dataPoints": [
                {
                    "timeUnixNano": 1738892346000000000,
                    "count": 1000,
                    "sum": 50000.0,
                    "quantileValues": [
                        {"quantile": 0.95, "value": 90.0}
                    ],
                    "attributes": []
                }
            ]
        }
        resource_attrs = {}
        result = parse_summary_data_points(
            summary_data, "test.metric", "test", resource_attrs
        )

        assert "summary_data" in result[0]
        summary_data_json = json.loads(result[0]["summary_data"])
        assert summary_data_json["count"] == 1000
        assert summary_data_json["sum"] == 50000.0
        assert len(summary_data_json["quantile_values"]) == 1


class TestProcessOtlpMetricsJson:
    """测试 OTLP Metrics JSON 处理"""

    def test_process_gauge_metrics(self):
        """测试处理 Gauge 类型 Metrics"""
        otlp_data = {
            "resourceMetrics": [{
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": "test-service"}}
                    ]
                },
                "scopeMetrics": [{
                    "scope": {
                        "name": "test-scope"
                    },
                    "metrics": [{
                        "name": "cpu.usage",
                        "description": "CPU usage percentage",
                        "unit": "%",
                        "gauge": {
                            "dataPoints": [{
                                "timeUnixNano": 1738892346000000000,
                                "asDouble": 75.5,
                                "attributes": []
                            }]
                        }
                    }]
                }]
            }]
        }

        result = process_otlp_metrics_json(otlp_data)
        assert len(result) == 1
        assert result[0]["metric_name"] == "cpu.usage"
        assert result[0]["value"] == 75.5

    def test_process_sum_metrics(self):
        """测试处理 Sum 类型 Metrics（Counter/UpDownCounter）"""
        otlp_data = {
            "resourceMetrics": [{
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": "api-server"}}
                    ]
                },
                "scopeMetrics": [{
                    "scope": {"name": "http"},
                    "metrics": [{
                        "name": "http.requests",
                        "sum": {
                            "isMonotonic": True,
                            "dataPoints": [{
                                "timeUnixNano": 1738892346000000000,
                                "asInt": 5000,
                                "attributes": [
                                    {"key": "method", "value": {"stringValue": "GET"}}
                                ]
                            }]
                        }
                    }]
                }]
            }]
        }

        result = process_otlp_metrics_json(otlp_data)
        assert len(result) == 1
        assert result[0]["metric_type"] == "counter"  # isMonotonic=True
        assert result[0]["value"] == 5000.0

    def test_process_multiple_metrics(self):
        """测试处理多个 Metrics"""
        otlp_data = {
            "resourceMetrics": [{
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": "test"}}
                    ]
                },
                "scopeMetrics": [{
                    "scope": {"name": "test"},
                    "metrics": [
                        {
                            "name": "metric1",
                            "gauge": {
                                "dataPoints": [{
                                    "timeUnixNano": 1738892346000000000,
                                    "asDouble": 100.0,
                                    "attributes": []
                                }]
                            }
                        },
                        {
                            "name": "metric2",
                            "gauge": {
                                "dataPoints": [{
                                    "timeUnixNano": 1738892346000000000,
                                    "asDouble": 200.0,
                                    "attributes": []
                                }]
                            }
                        }
                    ]
                }]
            }]
        }

        result = process_otlp_metrics_json(otlp_data)
        assert len(result) == 2

    def test_process_empty_metrics(self):
        """测试处理空的 Metrics"""
        otlp_data = {"resourceMetrics": []}
        result = process_otlp_metrics_json(otlp_data)
        assert len(result) == 0


class TestEdgeCases:
    """测试边界情况和错误处理"""

    def test_missing_time_unix_nano(self):
        """测试缺少时间戳"""
        data_points = {
            "dataPoints": [
                {
                    "asDouble": 100.0,
                    "attributes": []
                }
            ]
        }
        resource_attrs = {}
        result = parse_number_data_points(
            data_points, "test.metric", "gauge", "test", resource_attrs
        )

        # 应该使用 epoch 时间（0）
        assert len(result) == 1

    def test_missing_value(self):
        """测试缺少值字段"""
        data_points = {
            "dataPoints": [
                {
                    "timeUnixNano": 1738892346000000000,
                    "attributes": []
                }
            ]
        }
        resource_attrs = {}
        result = parse_number_data_points(
            data_points, "test.metric", "gauge", "test", resource_attrs
        )

        # 没有 asInt 或 asDouble，不应添加数据点
        assert len(result) == 0

    def test_empty_data_points_array(self):
        """测试空数据点数组"""
        data_points = {"dataPoints": []}
        resource_attrs = {}
        result = parse_number_data_points(
            data_points, "test.metric", "gauge", "test", resource_attrs
        )

        assert len(result) == 0

    def test_invalid_metric_type(self):
        """测试未知的 metric 类型"""
        metric = {
            "name": "unknown.metric",
            "unknownType": {
                "dataPoints": []
            }
        }
        resource_attrs = {}
        result = parse_metric_data(metric, "test", resource_attrs)

        # 未知类型应返回空列表
        assert len(result) == 0
