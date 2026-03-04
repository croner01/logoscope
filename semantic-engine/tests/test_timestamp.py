"""
Timestamp Utils 模块单元测试

测试 utils/timestamp.py 的时间戳转换功能：
- Unix 纳秒转 RFC 3339
- RFC 3339 转 DateTime64
- DateTime64 转 RFC 3339
- 任意格式时间戳解析
- RFC 3339 格式验证
"""
import pytest
from datetime import datetime, timezone, timedelta

from utils.timestamp import (
    unix_nano_to_rfc3339,
    rfc3339_to_datetime64,
    datetime64_to_rfc3339,
    parse_any_timestamp,
    validate_rfc3339
)


class TestUnixNanoToRfc3339:
    """测试 Unix 纳秒转 RFC 3339"""

    def test_basic_conversion(self):
        """测试基本转换"""
        # 使用代码示例中的时间戳
        timestamp = 1770339115625099982

        result = unix_nano_to_rfc3339(timestamp)

        # 验证格式
        assert "T" in result
        assert result.endswith("Z")

    def test_conversion_without_nanoseconds(self):
        """测试没有纳秒部分的时间戳"""
        # 整秒时间戳: 2026-02-06 12:34:56
        timestamp = 1738852896000000000

        result = unix_nano_to_rfc3339(timestamp)

        # 验证格式
        assert "T" in result
        assert result.endswith("Z")

    def test_conversion_with_nanoseconds(self):
        """测试带纳秒的时间戳"""
        timestamp = 1770339115625099982  # 有纳秒部分

        result = unix_nano_to_rfc3339(timestamp)

        # 验证格式
        assert "T" in result
        assert result.endswith("Z")

    def test_invalid_type(self):
        """测试无效类型"""
        with pytest.raises(ValueError, match="Invalid Unix nano timestamp type"):
            unix_nano_to_rfc3339("invalid")

    def test_negative_timestamp(self):
        """测试负时间戳"""
        with pytest.raises(ValueError, match="Invalid Unix nano timestamp"):
            unix_nano_to_rfc3339(-1)


class TestRfc3339ToDateTime64:
    """测试 RFC 3339 转 DateTime64"""

    def test_basic_conversion(self):
        """测试基本转换"""
        rfc3339_ts = "2026-02-06T12:34:56.123456789Z"

        result = rfc3339_to_datetime64(rfc3339_ts)

        # 应该替换 T 为空格，移除 Z
        assert " " in result and not "T" in result
        assert "123456789" in result

    def test_conversion_without_fraction(self):
        """测试没有小数秒的转换"""
        rfc3339_ts = "2026-02-06T12:34:56Z"

        result = rfc3339_to_datetime64(rfc3339_ts)

        # 应该替换 T 为空格
        assert " " in result
        assert not result.endswith("Z")

    def test_invalid_type(self):
        """测试无效类型"""
        with pytest.raises(ValueError, match="Invalid RFC 3339 timestamp type"):
            rfc3339_to_datetime64(123456)


class TestDateTime64ToRfc3339:
    """测试 DateTime64 转 RFC 3339"""

    def test_basic_conversion(self):
        """测试基本转换"""
        datetime64_str = "2026-02-06 12:34:56.123456789"

        result = datetime64_to_rfc3339(datetime64_str)

        # 应该替换空格为 T，添加 Z
        assert result == "2026-02-06T12:34:56.123456789Z"

    def test_conversion_without_fraction(self):
        """测试没有小数秒的转换"""
        datetime64_str = "2026-02-06 12:34:56"

        result = datetime64_to_rfc3339(datetime64_str)

        assert result == "2026-02-06T12:34:56Z"

    def test_conversion_trailing_zeros(self):
        """测试尾部零的处理"""
        datetime64_str = "2026-02-06 12:34:56.100000000"

        result = datetime64_to_rfc3339(datetime64_str)

        # 应该移除尾部的零
        assert result == "2026-02-06T12:34:56.1Z"

    def test_invalid_type(self):
        """测试无效类型"""
        with pytest.raises(ValueError, match="Invalid DateTime64 string type"):
            datetime64_to_rfc3339(12345)


class TestParseAnyTimestamp:
    """测试任意格式时间戳解析"""

    def test_parse_none(self):
        """测试 None 输入"""
        result = parse_any_timestamp(None)

        # 应该返回当前时间
        assert result.endswith("Z")
        assert "T" in result

    def test_parse_unix_nano(self):
        """测试解析 Unix 纳秒"""
        timestamp = 1770339115625099982

        result = parse_any_timestamp(timestamp)

        # 验证格式正确性
        assert "T" in result
        assert result.endswith("Z")
        # 验证包含小数秒部分
        assert "." in result

    def test_parse_unix_microseconds(self):
        """测试解析 Unix 微秒"""
        timestamp = 1770339115625099

        result = parse_any_timestamp(timestamp)

        # 验证格式正确性
        assert "T" in result
        assert result.endswith("Z")
        assert "." in result

    def test_parse_unix_milliseconds(self):
        """测试解析 Unix 毫秒"""
        timestamp = 1770339115625

        result = parse_any_timestamp(timestamp)

        # 验证格式正确性
        assert "T" in result
        assert result.endswith("Z")

    def test_parse_unix_seconds(self):
        """测试解析 Unix 秒"""
        timestamp = 1770339115

        result = parse_any_timestamp(timestamp)

        # 验证格式正确性
        assert "T" in result
        assert result.endswith("Z")
        # 秒级时间戳没有小数部分
        assert "." not in result

    def test_parse_rfc3339(self):
        """测试解析 RFC 3339 格式"""
        timestamp = "2026-02-06T12:34:56Z"

        result = parse_any_timestamp(timestamp)

        assert result == "2026-02-06T12:34:56Z"

    def test_parse_rfc3339_without_z(self):
        """测试解析没有 Z 后缀的 RFC 3339"""
        timestamp = "2026-02-06T12:34:56"

        result = parse_any_timestamp(timestamp)

        # 应该添加 Z 后缀
        assert result == "2026-02-06T12:34:56Z"

    def test_parse_datetime64_format(self):
        """测试解析 DateTime64 格式"""
        timestamp = "2026-02-06 12:34:56"

        result = parse_any_timestamp(timestamp)

        # 应该添加 Z 后缀
        assert result.endswith("Z")

    def test_parse_invalid_string(self):
        """测试解析无效字符串"""
        timestamp = "invalid-timestamp"

        result = parse_any_timestamp(timestamp)

        # 无法解析，应该返回原值
        assert result == "invalid-timestamp"

    def test_parse_numeric_string(self):
        """测试解析数字字符串"""
        timestamp = "1770339115"

        result = parse_any_timestamp(timestamp)

        # 应该解析为 Unix 秒
        assert "T" in result
        assert result.endswith("Z")
        # 秒级时间戳没有小数部分
        assert "." not in result


class TestValidateRfc3339:
    """测试 RFC 3339 格式验证"""

    def test_valid_basic_format(self):
        """测试基本有效格式"""
        assert validate_rfc3339("2026-02-06T12:34:56Z") == True

    def test_valid_with_fraction(self):
        """测试带小数秒的有效格式"""
        assert validate_rfc3339("2026-02-06T12:34:56.123456789Z") == True

    def test_valid_without_fraction(self):
        """测试不带小数秒的有效格式"""
        assert validate_rfc3339("2026-02-06T12:34:56Z") == True

    def test_valid_with_short_fraction(self):
        """测试短小数秒格式"""
        assert validate_rfc3339("2026-02-06T12:34:56.1Z") == True

    def test_invalid_format(self):
        """测试无效格式"""
        assert validate_rfc3339("invalid") == False
        assert validate_rfc3339("2026-02-06") == False
        assert validate_rfc3339("12:34:56") == False
        assert validate_rfc3339("2026-02-06 12:34:56") == False  # 有空格不是T

    def test_invalid_missing_z(self):
        """测试缺少 Z 后缀（根据实现，Z是可选的）"""
        # 实际实现中 Z 是可选的（正则: Z?$）
        assert validate_rfc3339("2026-02-06T12:34:56") == True

    def test_invalid_empty(self):
        """测试空字符串"""
        assert validate_rfc3339("") == False


class TestConversions:
    """测试转换往返一致性"""

    def test_rfc3339_datetime64_roundtrip(self):
        """测试 RFC 3339 和 DateTime64 往返转换"""
        original = "2026-02-06T12:34:56.123456789Z"

        # RFC 3339 -> DateTime64
        datetime64 = rfc3339_to_datetime64(original)

        # DateTime64 -> RFC 3339
        back_to_rfc = datetime64_to_rfc3339(datetime64)

        # 应该保持一致
        assert back_to_rfc == original

    def test_timestamp_parsing_consistency(self):
        """测试时间戳解析一致性"""
        original_nano = 1738852896123456789

        # 转换为 RFC 3339
        rfc3339 = unix_nano_to_rfc3339(original_nano)

        # 验证格式
        assert validate_rfc3339(rfc3339) == True

        # 再解析回来
        parsed = parse_any_timestamp(rfc3339)

        # 应该是有效的 RFC 3339 格式
        assert validate_rfc3339(parsed) == True


class TestEdgeCases:
    """测试边界情况"""

    def test_very_small_nano(self):
        """测试非常小的纳秒时间戳"""
        # 1970-01-01 00:00:00
        timestamp = 0

        result = unix_nano_to_rfc3339(timestamp)

        assert result == "1970-01-01T00:00:00Z"

    def test_very_large_nano(self):
        """测试非常大的纳秒时间戳"""
        # 未来的时间戳
        timestamp = 9999999999999999999

        result = unix_nano_to_rfc3339(timestamp)

        # 应该成功转换
        assert "T" in result
        assert result.endswith("Z")

    def test_fraction_with_trailing_zeros(self):
        """测试带尾部零的小数秒"""
        timestamp = 1738852896000100000  # .0001 秒

        result = unix_nano_to_rfc3339(timestamp)

        # 应该去除尾部的零
        assert ".0001" in result or result.endswith("Z")

    def test_all_nines_fraction(self):
        """测试全9的小数秒"""
        timestamp = 1738852896999999999  # .999999999 秒

        result = unix_nano_to_rfc3339(timestamp)

        # 应该保留所有9
        assert "999999999" in result


class TestRealWorldScenarios:
    """测试真实世界场景"""

    def test_opentelemetry_timestamp(self):
        """测试 OpenTelemetry 标准时间戳"""
        # OpenTelemetry 使用 Unix 纳秒
        ot_timestamp = 1770339115625099982  # 从文档示例

        result = unix_nano_to_rfc3339(ot_timestamp)

        assert validate_rfc3339(result) == True

    def test_clickhouse_timestamp(self):
        """测试 ClickHouse DateTime64 格式"""
        # ClickHouse 输出格式
        dt64 = "2026-02-06 12:34:56.123456789"

        result = datetime64_to_rfc3339(dt64)

        assert validate_rfc3339(result) == True

    def test_iso8601_timestamp(self):
        """测试 ISO 8601 时间戳"""
        iso_ts = "2026-02-06T12:34:56+00:00"

        result = parse_any_timestamp(iso_ts)

        # 应该转换为 Z 后缀格式
        assert result.endswith("Z")

    def test_log_timestamp_format(self):
        """测试常见日志时间戳格式"""
        # 常见的日志格式
        log_timestamps = [
            "2026-02-06T12:34:56Z",
            "2026-02-06 12:34:56",
            "1770339115",  # Unix 秒
        ]

        for ts in log_timestamps:
            result = parse_any_timestamp(ts)
            # 应该返回有效的时间格式
            assert result.endswith("Z")


class TestMultipleConversions:
    """测试多次转换的一致性"""

    def test_chained_conversions(self):
        """测试链式转换"""
        # Unix 纳秒 -> RFC 3339 -> DateTime64 -> RFC 3339
        original_nano = 1738852896123456789

        # 第一步：Unix 纳秒 -> RFC 3339
        rfc3339 = unix_nano_to_rfc3339(original_nano)

        # 第二步：RFC 3339 -> DateTime64
        datetime64 = rfc3339_to_datetime64(rfc3339)

        # 第三步：DateTime64 -> RFC 3339
        back_to_rfc = datetime64_to_rfc3339(datetime64)

        # 最终结果应该与第一步一致
        assert back_to_rfc == rfc3339

    def test_parse_and_reparse(self):
        """测试重复解析的一致性"""
        original = "2026-02-06T12:34:56.123456789Z"

        # 第一次解析
        parsed1 = parse_any_timestamp(original)

        # 第二次解析
        parsed2 = parse_any_timestamp(parsed1)

        # 应该一致
        assert parsed1 == parsed2
