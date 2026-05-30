"""
测试normalize/normalizer.py模块
"""
import pytest
from normalize.normalizer import (
    normalize_log,
    extract_service_name,
    extract_k8s_context,
    extract_timestamp,
    extract_log_level,
    extract_trace_id,
    extract_trace_info,
    extract_span_id,
)


class TestExtractServiceName:
    """测试服务名称提取"""

    def test_extract_from_kubernetes_pod_name(self):
        """从kubernetes.pod_name提取"""
        log_data = {
            "kubernetes": {
                "pod_name": "test-service-abc123"
            }
        }
        result = extract_service_name(log_data)
        assert result == "test-service-abc123"

    def test_extract_from_service_dot_name(self):
        """从service.name提取"""
        log_data = {
            "service.name": "api-gateway"
        }
        result = extract_service_name(log_data)
        assert result == "api-gateway"

    def test_extract_from_resource_service_dot_name(self):
        """从resource.service.name提取"""
        log_data = {
            "resource": {
                "service.name": "frontend"
            }
        }
        result = extract_service_name(log_data)
        assert result == "frontend"

    def test_extract_fallback_to_unknown(self):
        """无法提取时返回unknown"""
        log_data = {}
        result = extract_service_name(log_data)
        assert result == "unknown"

    def test_priority_order(self):
        """测试优先级：k8s.pod_name > service.name > resource > app"""
        log_data = {
            "kubernetes": {"pod_name": "from-k8s"},
            "service.name": "from-service",
            "resource": {"service.name": "from-resource"},
            "app": "from-app"
        }
        result = extract_service_name(log_data)
        # 应该返回k8s.pod_name（最高优先级）
        assert result == "from-k8s"


class TestExtractK8sContext:
    """测试K8s上下文提取"""

    def test_extract_full_k8s_context(self):
        """提取完整的K8s上下文"""
        log_data = {
            "kubernetes": {
                "pod_name": "test-pod-123",
                "namespace_name": "production",
                "node_name": "node-5",
                "pod_id": "pod-uuid-789",
                "host_ip": "10.244.1.5",
                "labels": {"app": "test", "env": "prod"},
                "host": "node-5"
            }
        }
        result = extract_k8s_context(log_data)

        assert result["namespace"] == "production"
        assert result["pod"] == "test-pod-123"
        assert result["node"] == "node-5"
        assert result["host_ip"] == "10.244.1.5"
        assert result["labels"]["app"] == "test"
        assert result["pod_id"] == "pod-uuid-789"

    def test_extract_partial_k8s_context(self):
        """提取部分K8s上下文"""
        log_data = {
            "kubernetes": {
                "pod_name": "test-pod",
                "namespace": "default"
            }
        }
        result = extract_k8s_context(log_data)

        assert result["pod"] == "test-pod"
        assert result["namespace"] == "default"
        assert result["node"] == "unknown"  # 默认值
        assert result["host_ip"] == ""  # 默认空字符串

    def test_extract_nested_pod_format(self):
        """测试嵌套pod格式"""
        log_data = {
            "kubernetes": {
                "pod": {
                    "name": "frontend-7d8f9c5b4-k2m4n"
                }
            }
        }
        result = extract_k8s_context(log_data)
        assert result["pod"] == "frontend-7d8f9c5b4-k2m4n"

    def test_extract_empty_k8s_context(self):
        """空K8s上下文返回默认值"""
        log_data = {}
        result = extract_k8s_context(log_data)

        assert result["namespace"] == "unknown"
        assert result["pod"] == "unknown"
        assert result["node"] == "unknown"


class TestNormalizeLog:
    """测试日志标准化"""

    def test_normalize_log_structure(self, sample_log_data):
        """测试标准化后的数据结构"""
        result = normalize_log(sample_log_data)

        # 验证必需字段存在
        assert "id" in result
        assert "timestamp" in result
        assert "entity" in result
        assert "event" in result
        assert "context" in result
        assert "relations" in result

        # 验证entity结构
        assert result["entity"]["type"] == "service"
        assert result["entity"]["name"] == "test-service"

        # 验证event结构
        assert result["event"]["type"] == "log"
        assert result["event"]["raw"] == "Test log message"

        # 验证context结构
        assert "k8s" in result["context"]

    def test_normalize_generates_unique_id(self, sample_log_data):
        """测试每次调用生成唯一ID"""
        result1 = normalize_log(sample_log_data)
        result2 = normalize_log(sample_log_data)

        assert result1["id"] != result2["id"]

    def test_normalize_preserves_k8s_context(self, sample_log_data):
        """测试K8s上下文正确保留"""
        result = normalize_log(sample_log_data)

        k8s = result["context"]["k8s"]
        assert k8s["namespace"] == "default"
        assert k8s["pod"] == "test-pod-123"
        assert k8s["node"] == "node-1"
        assert k8s["host_ip"] == "10.0.0.1"

    def test_normalize_trace_context(self, sample_log_data):
        """测试trace上下文正确提取"""
        result = normalize_log(sample_log_data)

        assert result["context"]["trace_id"] == "trace-123"
        assert result["context"]["span_id"] == "span-456"

    def test_normalize_otlp_format(self, sample_otlp_log):
        """测试OTLP格式日志标准化"""
        result = normalize_log(sample_otlp_log)

        assert result["entity"]["name"] == "log-generator-568b584664-fv422"
        assert result["context"]["k8s"]["namespace"] == "islap"
        assert result["context"]["k8s"]["pod"] == "log-generator-568b584664-fv422"

    def test_normalize_empty_message(self):
        """测试空消息处理"""
        log_data = {
            "service.name": "test"
        }
        result = normalize_log(log_data)

        assert result["event"]["raw"] == ""


class TestExtractTimestamp:
    """测试时间戳提取"""

    def test_extract_rfc3339_timestamp(self):
        """提取RFC3339格式时间戳"""
        log_data = {
            "timestamp": "2026-02-07T12:34:56.789Z"
        }
        result = extract_timestamp(log_data)
        assert "2026-02-07" in result

    def test_extract_unix_nano_timestamp(self):
        """提取Unix纳秒时间戳"""
        log_data = {
            "timestamp_unix_nano": 1738892346814567000
        }
        result = extract_timestamp(log_data)
        assert result is not None

    def test_extract_missing_timestamp(self):
        """缺失时间戳时使用当前时间"""
        log_data = {}
        result = extract_timestamp(log_data)
        assert result is not None


class TestExtractLogLevel:
    """测试日志级别提取"""

    def test_extract_standard_levels(self):
        """测试标准日志级别"""
        levels = ["debug", "info", "warn", "warning", "error", "fatal"]
        for level in levels:
            log_data = {"level": level, "severity": level}
            result = extract_log_level(log_data)
            assert result in ["debug", "info", "warn", "warning", "error", "fatal"]

    def test_extract_severity_number(self):
        """从severity_number提取级别"""
        log_data = {
            "severity_number": 9  # Error level
        }
        result = extract_log_level(log_data)
        assert result == "error"

    def test_extract_default_level(self):
        """默认级别为info"""
        log_data = {}
        result = extract_log_level(log_data)
        assert result == "info"

    def test_not_infer_error_from_log_body_keywords(self):
        """日志正文包含 ERROR/WARN 关键词时不应误判级别。"""
        log_data = {
            "message": "validation fields: ERROR=WARN threshold=3",
            "log": "payload contains ERROR WARN tokens",
        }
        result = extract_log_level(log_data)
        assert result == "info"

    def test_extract_level_from_structured_assignment(self):
        """兼容 level=ERROR 这类结构化字符串。"""
        log_data = {
            "severity_text": "level=ERROR",
        }
        result = extract_log_level(log_data)
        assert result == "error"

    def test_extract_level_from_timestamp_prefix_message(self):
        """兼容 2026-... WARNING ... 这类文本前缀级别。"""
        log_data = {
            "message": (
                "2026-03-03 09:35:08.583 WARNING [query-service] "
                "[CH_QUERY_SLOW] Slow query detected | sql=SELECT ... IN ('ERROR','WARN','INFO')"
            ),
        }
        result = extract_log_level(log_data)
        assert result == "warn"

    def test_extract_level_from_timestamp_pid_prefix_message(self):
        """兼容 2026-... <pid> WARNING ... 这类 OpenStack 前缀级别。"""
        log_data = {
            "message": (
                "2026-03-07 14:31:47.944 1711 WARNING "
                "os_brick.initiator.connectors.iscsi [req-xxx] "
                "Could not find the iSCSI Initiator File"
            ),
        }
        result = extract_log_level(log_data)
        assert result == "warn"

    def test_extract_level_from_bracket_prefix_message(self):
        """兼容 [WARNING] ... 这类行首级别。"""
        log_data = {
            "message": "[WARNING] No files matching import glob pattern: /etc/coredns/custom/*.server",
        }
        result = extract_log_level(log_data)
        assert result == "warn"

    def test_message_prefix_overrides_default_info_level(self):
        """当 level=info 为默认值时，允许前缀 WARNING 覆盖。"""
        log_data = {
            "level": "info",
            "message": "[WARNING] query timeout detected",
        }
        result = extract_log_level(log_data)
        assert result == "warn"

    def test_extract_level_from_key_value_fragment_message(self):
        """兼容 `time=... level=error msg=...` 结构化片段。"""
        log_data = {
            "level": "info",
            "message": (
                'time="2026-03-05T02:59:03.598707283Z" level=error '
                'msg="response completed with error"'
            ),
        }
        result = extract_log_level(log_data)
        assert result == "error"

    def test_extract_level_from_nested_json_log_field(self):
        """兼容包裹 JSON 中 log 字段的级别前缀。"""
        log_data = {
            "level": "info",
            "message": (
                '{"log":"2026-03-05 02:21:46.577 WARNING [query-service] '
                '[CH_QUERY_SLOW] Slow query detected"}'
            ),
        }
        result = extract_log_level(log_data)
        assert result == "warn"


class TestExtractTraceId:
    """测试Trace ID提取"""

    def test_extract_from_trace_id_field(self):
        """从trace_id字段提取"""
        log_data = {
            "trace_id": "trace-abc-123"
        }
        result = extract_trace_id(log_data)
        assert result == "trace-abc-123"

    def test_extract_from_attributes(self):
        """从attributes中提取"""
        log_data = {
            "attributes": {
                "trace_id": "trace-xyz-789"
            }
        }
        result = extract_trace_id(log_data)
        assert result == "trace-xyz-789"

    def test_extract_missing_trace_id(self):
        """缺失trace_id时应回退到稳定伪trace_id"""
        log_data = {}
        result = extract_trace_id(log_data)
        assert isinstance(result, str)
        assert len(result) == 32

    def test_extract_missing_trace_id_is_stable_for_same_seed(self):
        """同一日志上下文应生成稳定伪trace_id。"""
        log_data = {
            "service_name": "billing-service",
            "timestamp": "2026-03-01T10:20:30Z",
            "message": "payment timeout for order=1234",
            "host": "node-a-1",
        }
        result1 = extract_trace_id(log_data)
        result2 = extract_trace_id(log_data)
        assert result1 == result2

    def test_extract_trace_info_marks_otlp_source(self):
        """能提取到 trace_id 时标记为 otlp。"""
        log_data = {
            "traceId": "S/kvNXezTaajzpKdDg5HNg==",
        }
        result = extract_trace_info(log_data)
        assert result["trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"
        assert result["source"] == "otlp"

    def test_extract_trace_info_from_text_message(self):
        """从文本日志 trace=... 提取 trace_id。"""
        log_data = {
            "message": (
                "2026-03-03 13:26:43 INFO [query-service] "
                "[req-x trace=72db3dc831bb091f74119722761aa17e span=8682103c981c1c9f] request done"
            ),
        }
        result = extract_trace_info(log_data)
        assert result["trace_id"] == "72db3dc831bb091f74119722761aa17e"
        assert result["source"] == "otlp"

    def test_extract_span_id_from_text_message(self):
        """从文本日志 span=... 提取 span_id。"""
        log_data = {
            "message": (
                "2026-03-03 13:26:43 INFO [query-service] "
                "[req-x trace=72db3dc831bb091f74119722761aa17e span=8682103c981c1c9f] request done"
            ),
        }
        span_id = extract_span_id(log_data)
        assert span_id == "8682103c981c1c9f"

    def test_extract_trace_info_respects_fallback_switch(self, monkeypatch):
        """关闭 fallback 后，缺失 trace_id 应保持 missing。"""
        monkeypatch.setenv("ENABLE_PSEUDO_TRACE_ID_FALLBACK", "false")
        result = extract_trace_info({})
        assert result["trace_id"] == ""
        assert result["source"] == "missing"

    def test_normalize_log_writes_trace_source_into_context_and_raw_attributes(self):
        """normalize 后应保留 trace 来源标记。"""
        log_data = {
            "message": "plain log without trace context",
            "attributes": {"http.method": "GET"},
        }
        result = normalize_log(log_data)
        assert result["context"]["trace_id_source"] in {"synthetic", "missing"}
        assert result["_raw_attributes"]["trace_id_source"] == result["context"]["trace_id_source"]

    def test_normalize_log_extracts_trace_span_from_text_message(self):
        """normalize 能从文本日志补齐 trace/span 上下文。"""
        log_data = {
            "message": (
                "2026-03-03 13:26:43.969 INFO [query-service] "
                "[req-a trace=72db3dc831bb091f74119722761aa17e span=8682103c981c1c9f] "
                "[http.request] main: HTTP request completed"
            ),
        }
        result = normalize_log(log_data)
        assert result["context"]["trace_id"] == "72db3dc831bb091f74119722761aa17e"
        assert result["context"]["span_id"] == "8682103c981c1c9f"
        assert result["context"]["trace_id_source"] == "otlp"
        assert result["_raw_attributes"]["trace_id_source"] == "otlp"
