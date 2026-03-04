"""
Ingest Service Queue Writer 单元测试
测试数据转换和验证逻辑
"""
import json
import sys
import os

# 添加项目根目录到 Python 路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.queue_writer import (
    validate_otlp_payload,
    extract_attributes,
    transform_otlp_logs,
    transform_single_otlp_log,
    _build_log_queue_messages,
    _build_non_log_queue_message,
)


def test_validate_otlp_logs_valid():
    """测试有效的 OTLP Logs 数据验证"""
    payload = {
        "resourceLogs": [{
            "resource": {
                "attributes": [
                    {"key": "service.name", "value": {"stringValue": "test-service"}}
                ]
            },
            "scopeLogs": [{
                "logRecords": [{
                    "body": {"stringValue": "Test log"}
                }]
            }]
        }]
    }

    is_valid, error_msg = validate_otlp_payload(payload, "logs")
    assert is_valid is True
    assert error_msg == "Valid"


def test_validate_otlp_logs_missing_resource_logs():
    """测试缺少 resourceLogs 字段"""
    payload = {"some_field": "value"}

    is_valid, error_msg = validate_otlp_payload(payload, "logs")
    assert is_valid is False
    assert "Missing required field" in error_msg


def test_validate_otlp_logs_empty_resource_logs():
    """测试 resourceLogs 为空数组"""
    payload = {"resourceLogs": []}

    is_valid, error_msg = validate_otlp_payload(payload, "logs")
    assert is_valid is False
    assert "non-empty list" in error_msg


def test_extract_attributes_string_value():
    """测试提取 stringValue 类型的 attributes"""
    attrs_list = [
        {"key": "service.name", "value": {"stringValue": "test-service"}},
        {"key": "service.namespace", "value": {"stringValue": "islap"}}
    ]

    result = extract_attributes(attrs_list)
    assert result["service.name"] == "test-service"
    assert result["service.namespace"] == "islap"


def test_extract_attributes_int_value():
    """测试提取 intValue 类型的 attributes"""
    attrs_list = [
        {"key": "http.status_code", "value": {"intValue": 200}},
        {"key": "count", "value": {"intValue": 42}}
    ]

    result = extract_attributes(attrs_list)
    assert result["http.status_code"] == 200
    assert result["count"] == 42


def test_extract_attributes_double_value():
    """测试提取 doubleValue 类型的 attributes"""
    attrs_list = [
        {"key": "latency", "value": {"doubleValue": 0.123}},
        {"key": "score", "value": {"doubleValue": 99.5}}
    ]

    result = extract_attributes(attrs_list)
    assert result["latency"] == 0.123
    assert result["score"] == 99.5


def test_extract_attributes_bool_value():
    """测试提取 boolValue 类型的 attributes"""
    attrs_list = [
        {"key": "success", "value": {"boolValue": True}},
        {"key": "enabled", "value": {"boolValue": False}}
    ]

    result = extract_attributes(attrs_list)
    assert result["success"] is True
    assert result["enabled"] is False


def test_transform_otlp_logs_basic():
    """测试基本的 OTLP Logs 转换"""
    payload_dict = {
        "resourceLogs": [{
            "resource": {
                "attributes": [
                    {"key": "service.name", "value": {"stringValue": "test-service"}},
                    {"key": "host.name", "value": {"stringValue": "test-node"}}
                ]
            },
            "scopeLogs": [{
                "logRecords": [{
                    "timeUnixNano": "1771138633871997440",
                    "severityText": "ERROR",
                    "body": {"stringValue": "Test log message"}
                }]
            }]
        }]
    }

    metadata = {}
    result = transform_otlp_logs(payload_dict, metadata)

    assert result["log"] == "Test log message"
    assert result["timestamp"] == "1771138633871997440"
    assert result["severity"] == "ERROR"
    assert result["service.name"] == "test-service"


def test_transform_otlp_logs_with_attributes():
    """测试包含 log record attributes 的转换"""
    payload_dict = {
        "resourceLogs": [{
            "resource": {
                "attributes": [
                    {"key": "service.name", "value": {"stringValue": "test-service"}}
                ]
            },
            "scopeLogs": [{
                "logRecords": [{
                    "body": {"stringValue": "Test log"},
                    "attributes": [
                        {"key": "http.method", "value": {"stringValue": "GET"}},
                        {"key": "http.status_code", "value": {"intValue": 200}}
                    ]
                }]
            }]
        }]
    }

    metadata = {}
    result = transform_otlp_logs(payload_dict, metadata)

    assert result["attributes"]["http.method"] == "GET"
    assert result["attributes"]["http.status_code"] == 200


def test_transform_single_otlp_log_normalizes_trace_and_span_ids():
    """OTLP logRecord 中 base64 trace/span id 需归一化为十六进制。"""
    resource_dict = {"service.name": "checkout-service"}
    log_record = {
        "timeUnixNano": "1771138633871997440",
        "severityText": "INFO",
        "traceId": "S/kvNXezTaajzpKdDg5HNg==",
        "spanId": "APBnqgupArc=",
        "flags": "1",
        "body": {"stringValue": "checkout ok"},
        "attributes": [],
    }

    result = transform_single_otlp_log(resource_dict, log_record, metadata={})

    assert result["trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert result["span_id"] == "00f067aa0ba902b7"
    assert result["flags"] == 1
    assert result["trace_id_source"] == "otlp"
    assert result["attributes"]["trace_id_source"] == "otlp"


def test_transform_otlp_logs_with_kubernetes_info():
    """测试包含 Kubernetes 信息的转换"""
    payload_dict = {
        "resourceLogs": [{
            "resource": {
                "attributes": [
                    {"key": "service.name", "value": {"stringValue": "test-service"}},
                    {"key": "k8s.pod.name", "value": {"stringValue": "test-pod-123"}},
                    {"key": "k8s.namespace.name", "value": {"stringValue": "islap"}},
                    {"key": "k8s.node.name", "value": {"stringValue": "node-01"}},
                    {"key": "k8s.pod.labels.app", "value": {"stringValue": "test-app"}},
                    {"key": "k8s.pod.labels.version", "value": {"stringValue": "1.0.0"}}
                ]
            },
            "scopeLogs": [{
                "logRecords": [{
                    "body": {"stringValue": "Test log"}
                }]
            }]
        }]
    }

    metadata = {}
    result = transform_otlp_logs(payload_dict, metadata)

    k8s = result["kubernetes"]
    assert k8s["pod_name"] == "test-pod-123"
    assert k8s["namespace_name"] == "islap"
    assert k8s["node_name"] == "node-01"
    assert k8s["labels"]["app"] == "test-app"
    assert k8s["labels"]["version"] == "1.0.0"
    assert k8s["labels"]["service_name"] == "test-service"


def test_transform_otlp_logs_metadata_override():
    """测试 metadata 覆盖 kubernetes 信息"""
    payload_dict = {
        "resourceLogs": [{
            "resource": {
                "attributes": [
                    {"key": "k8s.pod.name", "value": {"stringValue": "from-attr"}},
                    {"key": "k8s.namespace.name", "value": {"stringValue": "from-attr-ns"}}
                ]
            },
            "scopeLogs": [{
                "logRecords": [{
                    "body": {"stringValue": "Test log"}
                }]
            }]
        }]
    }

    metadata = {
        "pod_name": "from-metadata",
        "namespace_name": "from-metadata-ns"
    }

    result = transform_otlp_logs(payload_dict, metadata)

    k8s = result["kubernetes"]
    assert k8s["pod_name"] == "from-metadata"  # metadata 优先级更高
    assert k8s["namespace_name"] == "from-metadata-ns"


def test_validate_otlp_metrics_valid():
    """测试有效的 OTLP Metrics 数据验证"""
    payload = {"resourceMetrics": []}
    is_valid, error_msg = validate_otlp_payload(payload, "metrics")
    assert is_valid is True


def test_validate_otlp_traces_valid():
    """测试有效的 OTLP Traces 数据验证"""
    payload = {"resourceSpans": []}
    is_valid, error_msg = validate_otlp_payload(payload, "traces")
    assert is_valid is True


def test_transform_otlp_logs_complete_resource():
    """测试完整的 resource 字段"""
    payload_dict = {
        "resourceLogs": [{
            "resource": {
                "attributes": [
                    {"key": "service.name", "value": {"stringValue": "my-service"}},
                    {"key": "service.namespace", "value": {"stringValue": "production"}},
                    {"key": "service.version", "value": {"stringValue": "2.3.4"}},
                    {"key": "host.name", "value": {"stringValue": "prod-node-01"}},
                    {"key": "host.arch", "value": {"stringValue": "x86_64"}},
                    {"key": "os.type", "value": {"stringValue": "linux"}}
                ]
            },
            "scopeLogs": [{
                "logRecords": [{
                    "body": {"stringValue": "Application started"}
                }]
            }]
        }]
    }

    metadata = {}
    result = transform_otlp_logs(payload_dict, metadata)

    resource = result["resource"]
    assert resource["service.name"] == "my-service"
    assert resource["service.namespace"] == "production"
    assert resource["service.version"] == "2.3.4"
    assert resource["host.name"] == "prod-node-01"
    assert resource["host.arch"] == "x86_64"
    assert resource["os.type"] == "linux"


def test_build_non_log_queue_message_with_parsed_payload():
    """非日志信号保留原始结构（JSON payload）"""
    payload_dict = {"resourceMetrics": [{"resource": {"attributes": []}}]}
    message = _build_non_log_queue_message("metrics", payload_dict, "{}")

    assert message["signal_type"] == "metrics"
    assert "payload" in message
    assert message["payload"] == payload_dict
    assert "raw_payload" not in message


def test_build_non_log_queue_message_with_raw_payload():
    """非日志信号在无法解析 JSON 时保留 raw_payload"""
    raw_payload = "not-json-content"
    message = _build_non_log_queue_message("traces", None, raw_payload)

    assert message["signal_type"] == "traces"
    assert message["raw_payload"] == raw_payload


def test_build_log_queue_messages_otlp_split_records():
    """OTLP logs 会按 logRecords 拆分为多条消息"""
    payload_dict = {
        "resourceLogs": [{
            "resource": {
                "attributes": [
                    {"key": "service.name", "value": {"stringValue": "split-test"}}
                ]
            },
            "scopeLogs": [{
                "logRecords": [
                    {"body": {"stringValue": "log-1"}, "severityText": "INFO"},
                    {"body": {"stringValue": "log-2"}, "severityText": "ERROR"}
                ]
            }]
        }]
    }

    messages = _build_log_queue_messages(payload_dict, json.dumps(payload_dict), {})

    assert len(messages) == 2
    assert messages[0]["log"] == "log-1"
    assert messages[1]["log"] == "log-2"
    assert messages[0]["service.name"] == "split-test"


def test_build_log_queue_messages_fallback_raw_payload():
    """logs 无法解析时回退到 raw_payload"""
    raw_payload = "raw-log-text"
    messages = _build_log_queue_messages(None, raw_payload, {})

    assert len(messages) == 1
    assert messages[0]["attributes"]["raw_payload"] == raw_payload


if __name__ == "__main__":
    # 运行测试
    import pytest
    pytest.main([__file__, "-v"])
