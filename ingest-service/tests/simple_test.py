#!/usr/bin/env python3
"""
简单测试脚本 - 验证Protobuf解析器功能
"""
import sys
import os
import json

# 添加项目根目录到 Python 路径
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

print("[TEST] Starting Protobuf Parser test...")

# 导入测试模块
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
    """创建测试用的OTLP Protobuf日志数据"""
    print("[TEST] Creating test Protobuf data...")

    log_record = LogRecord(
        time_unix_nano=1708000000000000000,
        observed_time_unix_nano=1708000001000000000,
        severity_number=SeverityNumber.SEVERITY_NUMBER_INFO,
        severity_text="INFO",
        body=AnyValue(
            string_value="Test log message from Protobuf"
        )
    )

    attribute = KeyValue(
        key="service.name",
        value=AnyValue(string_value="test-service")
    )
    log_record.attributes.append(attribute)

    scope_logs = ScopeLogs()
    scope_logs.log_records.append(log_record)

    resource_logs = ResourceLogs()
    resource_logs.scope_logs.append(scope_logs)

    request = ExportLogsServiceRequest()
    request.resource_logs.append(resource_logs)

    return request.SerializeToString()


def run_tests():
    """运行所有测试"""
    parser = ProtobufParser()
    test_results = []

    print("\n" + "="*60)
    print("TEST 1: Content-Type detection")
    print("="*60)
    try:
        assert parser.is_protobuf_content_type("application/x-protobuf") is True
        assert parser.is_protobuf_content_type("application/json") is False
        print("✅ PASSED: Content-Type detection works correctly")
        test_results.append(("Content-Type detection", True))
    except Exception as e:
        print(f"❌ FAILED: {e}")
        test_results.append(("Content-Type detection", False))

    print("\n" + "="*60)
    print("TEST 2: Protobuf schema validation")
    print("="*60)
    test_data = create_test_logs_protobuf()
    try:
        result = parser.validate_protobuf_schema(test_data, data_type="logs")
        assert result is True
        print("✅ PASSED: Protobuf schema validation successful")
        test_results.append(("Protobuf schema validation", True))
    except Exception as e:
        print(f"❌ FAILED: {e}")
        test_results.append(("Protobuf schema validation", False))

    print("\n" + "="*60)
    print("TEST 3: Parse logs Protobuf")
    print("="*60)
    try:
        result = parser.parse_logs_protobuf(test_data)
        assert result is not None
        assert "resourceLogs" in result
        print(f"✅ PASSED: Successfully parsed Protobuf, found {len(result.get('resourceLogs', []))} resourceLogs")
        test_results.append(("Parse logs Protobuf", True))
    except Exception as e:
        print(f"❌ FAILED: {e}")
        import traceback
        traceback.print_exc()
        test_results.append(("Parse logs Protobuf", False))

    print("\n" + "="*60)
    print("TEST 4: Extract log records")
    print("="*60)
    try:
        parsed_dict = parser.parse_logs_protobuf(test_data)
        log_records = parser.extract_log_records(parsed_dict)
        assert len(log_records) == 1
        log_record = log_records[0]
        assert log_record["severityText"] == "INFO"
        print(f"✅ PASSED: Successfully extracted {len(log_records)} log record(s)")
        test_results.append(("Extract log records", True))
    except Exception as e:
        print(f"❌ FAILED: {e}")
        import traceback
        traceback.print_exc()
        test_results.append(("Extract log records", False))

    print("\n" + "="*60)
    print("TEST 5: Singleton pattern")
    print("="*60)
    try:
        parser1 = get_protobuf_parser()
        parser2 = get_protobuf_parser()
        assert parser1 is parser2
        print("✅ PASSED: Singleton pattern works correctly")
        test_results.append(("Singleton pattern", True))
    except Exception as e:
        print(f"❌ FAILED: {e}")
        test_results.append(("Singleton pattern", False))

    print("\n" + "="*60)
    print("TEST 6: JSON conversion")
    print("="*60)
    try:
        parsed_dict = parser.parse_logs_protobuf(test_data)
        json_str = json.dumps(parsed_dict)
        parsed_back = json.loads(json_str)
        assert "resourceLogs" in parsed_back
        print("✅ PASSED: JSON conversion works correctly")
        test_results.append(("JSON conversion", True))
    except Exception as e:
        print(f"❌ FAILED: {e}")
        import traceback
        traceback.print_exc()
        test_results.append(("JSON conversion", False))

    print("\n" + "="*60)
    print("TEST SUMMARY")
    print("="*60)
    passed = sum(1 for _, result in test_results if result)
    total = len(test_results)

    for name, result in test_results:
        status = "✅ PASSED" if result else "❌ FAILED"
        print(f"{status}: {name}")

    print(f"\nTotal: {passed}/{total} tests passed")

    return passed == total


if __name__ == "__main__":
    success = run_tests()
    sys.exit(0 if success else 1)
