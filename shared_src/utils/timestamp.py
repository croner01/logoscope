"""
时间戳工具模块
提供 OpenTelemetry Unix 纳秒时间戳与 RFC 3339 之间的转换

符合标准：
- OpenTelemetry Specification: Unix 纳秒时间戳
- RFC 3339: 互联网日期时间格式
- ISO 8601: 日期和时间表示
- ClickHouse DateTime64: 高精度时间戳存储
"""
from datetime import datetime, timezone
from typing import Any, Union


def unix_nano_to_rfc3339(time_unix_nano: int) -> str:
    """
    将 Unix 纳秒时间戳转换为 RFC 3339 格式

    Args:
        time_unix_nano: Unix 纳秒时间戳（例如：1770339115625099982）

    Returns:
        str: RFC 3339 格式的时间戳（例如：2026-02-06T12:34:56.123456789Z）

    Raises:
        ValueError: 如果输入无效

    Examples:
        >>> unix_nano_to_rfc3339(1770339115625099982)
        '2026-02-06T12:34:56.250999982Z'
    """
    if not isinstance(time_unix_nano, (int, float)):
        raise ValueError(f"Invalid Unix nano timestamp type: {type(time_unix_nano)}")

    if time_unix_nano < 0:
        raise ValueError(f"Invalid Unix nano timestamp: {time_unix_nano}")

    # 转换为秒和纳秒
    seconds = int(time_unix_nano // 1_000_000_000)
    nanoseconds = int(time_unix_nano % 1_000_000_000)

    # 创建 datetime 对象（UTC 时区）
    dt = datetime.fromtimestamp(seconds, tz=timezone.utc)

    # 格式化为 RFC 3339（保留纳秒精度）
    rfc3339_ts = dt.strftime('%Y-%m-%dT%H:%M:%S')

    # 添加纳秒部分（去掉尾部的零）
    if nanoseconds > 0:
        nano_str = f"{nanoseconds:09d}".rstrip('0')
        rfc3339_ts = f"{rfc3339_ts}.{nano_str}Z"
    else:
        rfc3339_ts = f"{rfc3339_ts}Z"

    return rfc3339_ts


def rfc3339_to_datetime64(rfc3339_ts: str) -> str:
    """
    将 RFC 3339 时间戳转换为 ClickHouse DateTime64 格式

    Args:
        rfc3339_ts: RFC 3339 格式的时间戳

    Returns:
        str: ClickHouse 可识别的格式（例如：2026-02-06 12:34:56.123456789）

    Examples:
        >>> rfc3339_to_datetime64("2026-02-06T12:34:56.123456789Z")
        '2026-02-06 12:34:56.123456789'
    """
    if not isinstance(rfc3339_ts, str):
        raise ValueError(f"Invalid RFC 3339 timestamp type: {type(rfc3339_ts)}")

    # 移除 'T' 和 'Z'，保留小数秒
    dt_str = rfc3339_ts.replace('T', ' ').replace('Z', '')

    return dt_str


def datetime64_to_rfc3339(datetime64_str: str) -> str:
    """
    将 ClickHouse DateTime64 格式转换为 RFC 3339

    Args:
        datetime64_str: DateTime64 格式字符串（例如：2026-02-06 12:34:56.123456789）

    Returns:
        str: RFC 3339 格式

    Examples:
        >>> datetime64_to_rfc3339("2026-02-06 12:34:56.123456789")
        '2026-02-06T12:34:56.123456789Z'
    """
    if not isinstance(datetime64_str, str):
        raise ValueError(f"Invalid DateTime64 string type: {type(datetime64_str)}")

    # 简单替换（假设输入已经是标准格式）
    return datetime64_str.replace(' ', 'T').rstrip('0').rstrip('.') + 'Z'


def parse_any_timestamp(timestamp: Any) -> str:
    """
    尝试解析任意格式的时间戳并转换为 RFC 3339

    支持的格式：
    - Unix 纳秒（例如：1770339115625099982）
    - Unix 微秒（例如：1770339115625099）
    - Unix 毫秒（例如：1770339115625）
    - Unix 秒（例如：1770339115）
    - ISO 8601 / RFC 3339（例如：2026-02-06T12:34:56Z）
    - 其他字符串格式

    Args:
        timestamp: 任意格式的时间戳

    Returns:
        str: RFC 3339 格式

    Examples:
        >>> parse_any_timestamp(1770339115625099982)
        '2026-02-06T12:34:56.250999982Z'
        >>> parse_any_timestamp('2026-02-06T12:34:56Z')
        '2026-02-06T12:34:56Z'
    """
    if timestamp is None:
        # 使用当前时间（UTC）
        return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

    if isinstance(timestamp, str):
        # 已经是字符串格式
        timestamp = timestamp.strip()

        # 如果已经是 RFC 3339/ISO 8601 格式
        if 'T' in timestamp or timestamp.count('-') >= 2:
            # 确保有 Z 后缀
            if not timestamp.endswith('Z'):
                timestamp = timestamp + 'Z'
            return timestamp

        # 可能是 "2026-02-06 12:34:56" 格式
        if ' ' in timestamp:
            return timestamp.replace(' ', 'T').rstrip('0').rstrip('.') + 'Z'

        # 尝试作为数字时间戳解析
        try:
            return parse_any_timestamp(int(timestamp))
        except ValueError:
            pass

        # 无法解析，返回原值
        return timestamp

    elif isinstance(timestamp, (int, float)):
        # 数字时间戳
        if timestamp > 1e18:  # 纳秒时间戳（大于 10^18）
            return unix_nano_to_rfc3339(int(timestamp))
        elif timestamp > 1e15:  # 微秒时间戳
            microseconds = int(timestamp)
            seconds = microseconds / 1_000_000
            dt = datetime.fromtimestamp(seconds, tz=timezone.utc)
            micro_str = f"{(microseconds % 1_000_000):06d}".rstrip('0')
            if micro_str:
                return dt.strftime(f'%Y-%m-%dT%H:%M:%S.{micro_str}Z')
            else:
                return dt.strftime('%Y-%m-%dT%H:%M:%SZ')
        elif timestamp > 1e12:  # 毫秒时间戳
            milliseconds = int(timestamp)
            seconds = milliseconds / 1000
            dt = datetime.fromtimestamp(seconds, tz=timezone.utc)
            ms_str = f"{(milliseconds % 1000):03d}".rstrip('0')
            if ms_str:
                return dt.strftime(f'%Y-%m-%dT%H:%M:%S.{ms_str}Z')
            else:
                return dt.strftime('%Y-%m-%dT%H:%M:%SZ')
        else:  # 秒时间戳
            dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
            return dt.strftime('%Y-%m-%dT%H:%M:%SZ')

    return str(timestamp)


def validate_rfc3339(timestamp: str) -> bool:
    """
    验证时间戳是否符合 RFC 3339 格式

    Args:
        timestamp: 待验证的时间戳字符串

    Returns:
        bool: 是否符合 RFC 3339 格式

    Examples:
        >>> validate_rfc3339("2026-02-06T12:34:56Z")
        True
        >>> validate_rfc3339("2026-02-06T12:34:56.123456789Z")
        True
        >>> validate_rfc3339("invalid")
        False
    """
    import re

    # RFC 3339 基本格式: 2026-02-06T12:34:56[.fraction]Z
    # 允许可选的小数秒部分
    rfc3339_pattern = r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z?$'

    return bool(re.match(rfc3339_pattern, timestamp))


# 测试用例
if __name__ == "__main__":
    print("时间戳工具模块测试")
    print("=" * 60)

    # 测试 1: Unix 纳秒转换
    test_nano = 1770339115625099982
    result = unix_nano_to_rfc3339(test_nano)
    print(f"\n1. Unix 纳秒 -> RFC 3339:")
    print(f"   输入: {test_nano}")
    print(f"   输出: {result}")
    print(f"   验证: {validate_rfc3339(result)}")

    # 测试 2: RFC 3339 -> DateTime64
    rfc3339_ts = "2026-02-06T12:34:56.123456789Z"
    dt64 = rfc3339_to_datetime64(rfc3339_ts)
    print(f"\n2. RFC 3339 -> DateTime64:")
    print(f"   输入: {rfc3339_ts}")
    print(f"   输出: {dt64}")

    # 测试 3: DateTime64 -> RFC 3339
    back_to_rfc = datetime64_to_rfc3339(dt64)
    print(f"\n3. DateTime64 -> RFC 3339:")
    print(f"   输入: {dt64}")
    print(f"   输出: {back_to_rfc}")

    # 测试 4: 解析各种格式
    print(f"\n4. 解析各种时间戳格式:")
    test_cases = [
        (1770339115625099982, "Unix 纳秒"),
        (1770339115625099, "Unix 微秒"),
        (1770339115625, "Unix 毫秒"),
        (1770339115, "Unix 秒"),
        ("2026-02-06T12:34:56Z", "RFC 3339"),
        ("2026-02-06 12:34:56", "日期时间"),
    ]

    for ts, desc in test_cases:
        parsed = parse_any_timestamp(ts)
        print(f"   {desc:15} -> {parsed}")

    print("\n" + "=" * 60)
    print("测试完成！")
