"""
OpenTelemetry 协议工具函数
"""
from typing import Dict, Any, List


def parse_otlp_attributes(attributes_list: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    将 OTLP attributes 数组转换为字典

    Args:
        attributes_list: OTLP attributes 数组格式

    Returns:
        Dict[str, Any]: 转换后的字典

    Examples:
        >>> attrs = [
        ...     {"key": "service.name", "value": {"stringValue": "my-service"}},
        ...     {"key": "port", "value": {"intValue": "8080"}}
        ... ]
        >>> parse_otlp_attributes(attrs)
        {'service.name': 'my-service', 'port': '8080'}
    """
    attrs = {}
    for attr in attributes_list:
        key = attr.get("key", "")
        value = attr.get("value", {})

        if "stringValue" in value:
            attrs[key] = value["stringValue"]
        elif "intValue" in value:
            attrs[key] = str(value["intValue"])
        elif "boolValue" in value:
            attrs[key] = value["boolValue"]
        elif "doubleValue" in value:
            attrs[key] = value["doubleValue"]
        elif "arrayValue" in value:
            attrs[key] = value["arrayValue"]
        elif "kvlistValue" in value:
            # 处理 Map/kvlist 类型
            kv_list = {}
            for kv in value["kvlistValue"].get("values", []):
                if "key" in kv and "value" in kv:
                    v = kv["value"]
                    if "stringValue" in v:
                        kv_list[kv["key"]] = v["stringValue"]
                    elif "intValue" in v:
                        kv_list[kv["key"]] = str(v["intValue"])
                    elif "boolValue" in v:
                        kv_list[kv["key"]] = str(v["boolValue"])
            attrs[key] = kv_list

    return attrs
