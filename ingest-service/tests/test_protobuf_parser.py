"""
Protobuf 解析器测试模块
包含单元测试和集成测试
"""
import pytest
import sys
import os
import json
import base64

# 添加项目根目录到 Python 路径
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.protobuf_parser import (
    ProtobufParser,
    get_protobuf_parser
)

# 导入OTLP Protobuf定义
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import (
    ExportLogsServiceRequest,
    ResourceLogs,
    ScopeLogs,
    LogRecord
)
from opentelemetry.proto.common.v1.common_pb2 import (
    KeyValue,
    AnyValue
)
from opentelemetry.proto.logs.v1.logs_pb2 import SeverityNumber


def create_test_logs_protobuf() -> bytes:
    """
    创建测试用的OTLP Protobuf日志数据

    Returns:
        bytes: 序列化的Protobuf数据
    """
    # 创建日志记录
    log_record = LogRecord(
        time_unix_nano=1708000000000000000,
        observed_time_unix_nano=1708000001000000000,
        severity_number=SeverityNumber.SEVERITY_NUMBER_INFO,
        severity_text="INFO",
        body=AnyValue(
            string_value="Test log message from Protobuf"
        )
    )

    # 创建属性
    attribute = KeyValue(
        key="service.name",
        value=AnyValue(string_value="test-service")
    )
    log_record.attributes.append(attribute)

    # 创建ScopeLogs
    scope_logs = ScopeLogs()
    scope_logs.log_records.append(log_record)

    # 创建ResourceLogs
    resource_logs = ResourceLogs()
    resource_logs.scope_logs.append(scope_logs)

    # 创建请求
    request = ExportLogsServiceRequest()
    request.resource_logs.append(resource_logs)

    # 序列化为字节
    return request.SerializeToString()


class TestProtobufParser:
    """ProtobufParser 单元测试"""

    def test_is_protobuf_content_type(self):
        """测试Content-Type检测"""
        parser = ProtobufParser()

        # 测试各种Protobuf Content-Type
        assert parser.is_protobuf_content_type("application/x-protobuf") is True
        assert parser.is_protobuf_content_type("application/protobuf") is True
        assert parser.is_protobuf_content_type("application/vnd.google.protobuf") is True
        assert parser.is_protobuf_content_type("application/octet-stream") is True

        # 测试非Protobuf Content-Type
        assert parser.is_protobuf_content_type("application/json") is False
        assert parser.is_protobuf_content_type("text/plain") is False
        assert parser.is_protobuf_content_type("") is False

    def test_validate_protobuf_schema_valid(self):
        """测试有效的Protobuf schema验证"""
        parser = ProtobufParser()
        test_data = create_test_logs_protobuf()

        assert parser.validate_protobuf_schema(test_data, data_type="logs") is True

    def test_validate_protobuf_schema_invalid(self):
        """测试无效的Protobuf schema验证"""
        parser = ProtobufParser()
        invalid_data = b"invalid protobuf data"

        with pytest.raises(Exception):
            parser.validate_protobuf_schema(invalid_data, data_type="logs")

    def test_parse_logs_protobuf(self):
        """测试解析日志Protobuf"""
        parser = ProtobufParser()
        test_data = create_test_logs_protobuf()

        result = parser.parse_logs_protobuf(test_data)

        assert result is not None
        assert "resourceLogs" in result
        assert len(result["resourceLogs"]) > 0

    def test_extract_log_records(self):
        """测试提取日志记录"""
        parser = ProtobufParser()
        test_data = create_test_logs_protobuf()
        parsed_dict = parser.parse_logs_protobuf(test_data)

        log_records = parser.extract_log_records(parsed_dict)

        assert len(log_records) == 1
        log_record = log_records[0]
        assert log_record["severityText"] == "INFO"

    def test_get_protobuf_parser_singleton(self):
        """测试单例模式"""
        parser1 = get_protobuf_parser()
        parser2 = get_protobuf_parser()

        assert parser1 is parser2


class TestProtobufParserIntegration:
    """ProtobufParser 集成测试"""

    def test_full_parse_flow(self):
        """测试完整的解析流程"""
        parser = ProtobufParser()

        # 1. 创建测试数据
        protobuf_data = create_test_logs_protobuf()

        # 2. 验证schema
        assert parser.validate_protobuf_schema(protobuf_data, data_type="logs") is True

        # 3. 解析Protobuf
        parsed_dict = parser.parse_logs_protobuf(protobuf_data)

        # 4. 提取日志记录
        log_records = parser.extract_log_records(parsed_dict)

        # 5. 验证结果
        assert len(log_records) == 1
        log_record = log_records[0]
        assert "body" in log_record
        assert "stringValue" in log_record["body"]
        assert log_record["body"]["stringValue"] == "Test log message from Protobuf"

    def test_json_conversion(self):
        """测试JSON转换"""
        parser = ProtobufParser()
        protobuf_data = create_test_logs_protobuf()
        parsed_dict = parser.parse_logs_protobuf(protobuf_data)

        # 转换为JSON
        json_str = json.dumps(parsed_dict)

        # 验证可以解析回来
        parsed_back = json.loads(json_str)
        assert "resourceLogs" in parsed_back


def test_error_handling_malformed_data():
    """测试处理格式错误的数据"""
    parser = ProtobufParser()

    with pytest.raises(Exception):
        parser.parse_logs_protobuf(b"malformed data")

    with pytest.raises(Exception):
        parser.parse_metrics_protobuf(b"malformed data")

    with pytest.raises(Exception):
        parser.parse_traces_protobuf(b"malformed data")


def test_error_handling_unknown_data_type():
    """测试处理未知的数据类型"""
    parser = ProtobufParser()
    test_data = create_test_logs_protobuf()

    with pytest.raises(ValueError):
        parser.validate_protobuf_schema(test_data, data_type="unknown_type")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
