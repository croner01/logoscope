"""
Semantic Engine Normalize 模块
负责日志数据的语义标准化
"""

import base64
import binascii
import hashlib
import os
import re
import uuid
from datetime import datetime
from typing import Any, Dict

from .service_name_enhanced import extract_service_name_enhanced


def _normalize_level_value(value: Any) -> str:
    """
    规范化日志级别取值，仅接受明确级别值或 level=xxx 结构。
    避免将日志正文中的 ERROR/WARN 关键字误识别为真实级别。
    """
    if value is None:
        return ""

    text = str(value).strip()
    if not text:
        return ""

    lower = text.lower()
    aliases = {
        "warning": "warn",
        "critical": "fatal",
    }
    if lower in {"trace", "debug", "info", "warn", "warning", "error", "fatal", "critical"}:
        return aliases.get(lower, lower)

    # 仅匹配结构化 level 表达式，避免扫描整句日志正文。
    patterns = (
        r'^\[?(trace|debug|info|warn(?:ing)?|error|fatal|critical)\]?$',
        r'^(?:level|log_level|severity|severity_text)\s*[:=]\s*"?'
        r'(trace|debug|info|warn(?:ing)?|error|fatal|critical)"?$',
        r'^"?(?:level|log_level|severity|severity_text)"?\s*[:=]\s*"?'
        r'(trace|debug|info|warn(?:ing)?|error|fatal|critical)"?$',
    )
    for pattern in patterns:
        match = re.match(pattern, lower)
        if not match:
            continue
        token = (match.group(1) or "").lower()
        return aliases.get(token, token)

    return ""


def _map_severity_number_to_level(value: Any) -> str:
    """兼容不同严重度数字映射，返回标准 level。"""
    try:
        num = int(value)
    except (TypeError, ValueError):
        return ""

    # 兼容历史映射：9 往上按 error 处理；更高等级归并为 fatal。
    if num >= 17:
        return "fatal"
    if num >= 9:
        return "error"
    if num >= 5:
        return "warn"
    if num > 0:
        return "info"
    return ""


def _extract_level_from_message_prefix(value: Any) -> str:
    """
    仅从日志正文前缀识别级别，避免扫描整句导致误判。

    典型场景：
    - 2026-03-03 09:35:08.583 WARNING [query-service] ...
    - [ERROR] failed to connect db
    """
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""

    aliases = {
        "warning": "warn",
        "critical": "fatal",
    }
    patterns = (
        # WARNING ...
        r'^\[?(trace|debug|info|warn(?:ing)?|error|fatal|critical)\]?(?:\s+|:|-)',
        # 2026-03-03 09:35:08.583 WARNING ...
        r'^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\s+'
        r'(trace|debug|info|warn(?:ing)?|error|fatal|critical)\b',
    )
    lower_text = text.lower()
    for pattern in patterns:
        match = re.match(pattern, lower_text)
        if not match:
            continue
        token = (match.group(1) or "").lower()
        return aliases.get(token, token)
    return ""


def normalize_log(log_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    标准化日志数据为统一事件模型

    Args:
        log_data: 原始日志数据，包含各种格式的日志信息

    Returns:
        Dict[str, Any]: 标准化后的事件模型，包含以下字段：
            - id: 事件唯一标识符
            - timestamp: 事件时间戳
            - entity: 实体信息（服务名、实例等）
            - event: 事件详情（类型、级别、内容等）
            - context: 上下文信息（trace_id、span_id、主机等）
            - relations: 关系列表（初始为空）
    """
    # ⭐ P0/P1优化：保留OTLP标准字段和原始attributes
    severity_number = log_data.get("severity_number", 0)
    flags = log_data.get("flags", 0)
    trace_info = extract_trace_info(log_data)
    raw_attributes = log_data.get("_raw_attributes")
    if not isinstance(raw_attributes, dict):
        raw_attributes = log_data.get("attributes", {})
    if not isinstance(raw_attributes, dict):
        raw_attributes = {}
    raw_attributes = dict(raw_attributes)
    existing_source = _candidate_text(raw_attributes.get("trace_id_source")).lower()
    resolved_source = _candidate_text(trace_info.get("source", "missing")).lower() or "missing"
    if resolved_source == "otlp":
        raw_attributes["trace_id_source"] = "otlp"
    elif not existing_source:
        raw_attributes["trace_id_source"] = resolved_source

    # 构建统一事件模型
    normalized = {
        # 生成唯一事件ID
        "id": str(uuid.uuid4()),
        # 获取或生成时间戳
        "timestamp": extract_timestamp(log_data),
        # 提取实体信息
        "entity": {
            "type": "service",
            "name": extract_service_name_enhanced(log_data),
            "instance": extract_instance_id(log_data)
        },
        # 提取事件详情
        "event": {
            "type": "log",
            "level": extract_log_level(log_data),
            "name": extract_event_name(log_data),
            "raw": log_data.get("message", "") or log_data.get("log", "") or ""
        },
        # 提取上下文信息
        "context": {
            "trace_id": trace_info.get("trace_id", ""),
            "trace_id_source": trace_info.get("source", "missing"),
            "span_id": extract_span_id(log_data),
            "host": extract_host(log_data),
            "k8s": extract_k8s_context(log_data)
        },
        # ⭐ P0/P1优化：传递OTLP字段和原始attributes
        "severity_number": severity_number,
        "flags": flags,
        "_raw_attributes": raw_attributes
    }

    return normalized


def _candidate_text(value: Any) -> str:
    if value is None:
        return ""
    candidate = str(value).strip()
    if not candidate:
        return ""
    return candidate


def _normalize_otel_id(raw_value: Any, expected_bytes: int) -> str:
    """Normalize OTLP id to lowercase hex; accept hex or base64 bytes."""
    text = _candidate_text(raw_value)
    if not text:
        return ""

    if text.startswith("0x") or text.startswith("0X"):
        text = text[2:]

    compact_hex = text.replace("-", "").lower()
    if len(compact_hex) == expected_bytes * 2:
        try:
            bytes.fromhex(compact_hex)
            return compact_hex
        except ValueError:
            pass

    candidates = [text, text.replace("-", "+").replace("_", "/")]
    for candidate in candidates:
        padded = candidate + ("=" * ((4 - (len(candidate) % 4)) % 4))
        try:
            decoded = base64.b64decode(padded, validate=True)
        except (binascii.Error, ValueError):
            continue
        if len(decoded) == expected_bytes:
            return decoded.hex()

    return text


def _normalize_otel_trace_id(raw_value: Any) -> str:
    return _normalize_otel_id(raw_value, expected_bytes=16)


def _normalize_otel_span_id(raw_value: Any) -> str:
    return _normalize_otel_id(raw_value, expected_bytes=8)


_TRACE_ID_TEXT_PATTERNS = (
    re.compile(r"\btrace(?:_id)?=([0-9a-fA-F]{32})\b"),
    re.compile(r'"trace(?:_id)?"\s*[:=]\s*"([0-9a-fA-F]{32})"'),
)
_SPAN_ID_TEXT_PATTERNS = (
    re.compile(r"\bspan(?:_id)?=([0-9a-fA-F]{16})\b"),
    re.compile(r'"span(?:_id)?"\s*[:=]\s*"([0-9a-fA-F]{16})"'),
)


def _extract_trace_span_from_log_text(log_data: Dict[str, Any]) -> Dict[str, str]:
    """Fallback parser: extract trace/span ids from raw log message text."""
    context = log_data.get("context", {})
    candidates = [
        _candidate_text(log_data.get("message")),
        _candidate_text(log_data.get("log")),
    ]
    if isinstance(context, dict):
        candidates.extend(
            [
                _candidate_text(context.get("message")),
                _candidate_text(context.get("log")),
            ]
        )

    for text in candidates:
        if not text:
            continue
        trace_id = ""
        span_id = ""

        for pattern in _TRACE_ID_TEXT_PATTERNS:
            match = pattern.search(text)
            if match:
                trace_id = _normalize_otel_trace_id(match.group(1))
                if trace_id:
                    break
        for pattern in _SPAN_ID_TEXT_PATTERNS:
            match = pattern.search(text)
            if match:
                span_id = _normalize_otel_span_id(match.group(1))
                if span_id:
                    break

        if trace_id or span_id:
            return {"trace_id": trace_id, "span_id": span_id}
    return {}


def _is_pseudo_trace_fallback_enabled() -> bool:
    raw = str(os.getenv("ENABLE_PSEUDO_TRACE_ID_FALLBACK", "true") or "true").strip().lower()
    return raw not in {"0", "false", "off", "no"}


def extract_trace_info(log_data: Dict[str, Any]) -> Dict[str, str]:
    """Extract trace id and source marker from log payload."""
    attributes = log_data.get("attributes", {})
    resource = log_data.get("resource", {})
    context = log_data.get("context", {})
    raw_attributes = log_data.get("_raw_attributes", {})

    trace_id = _normalize_otel_trace_id(
        log_data.get("trace_id")
        or log_data.get("traceId")
        or log_data.get("trace.id")
        or (context.get("trace_id") if isinstance(context, dict) else "")
        or (context.get("traceId") if isinstance(context, dict) else "")
        or (attributes.get("trace_id") if isinstance(attributes, dict) else "")
        or (attributes.get("traceId") if isinstance(attributes, dict) else "")
        or (attributes.get("trace.id") if isinstance(attributes, dict) else "")
        or (attributes.get("otel.trace_id") if isinstance(attributes, dict) else "")
        or (resource.get("trace_id") if isinstance(resource, dict) else "")
        or (resource.get("trace.id") if isinstance(resource, dict) else "")
        or (raw_attributes.get("trace_id") if isinstance(raw_attributes, dict) else "")
        or (raw_attributes.get("trace.id") if isinstance(raw_attributes, dict) else "")
    )
    if trace_id:
        return {"trace_id": trace_id, "source": "otlp"}

    text_context = _extract_trace_span_from_log_text(log_data)
    text_trace_id = _normalize_otel_trace_id(text_context.get("trace_id", ""))
    if text_trace_id:
        return {"trace_id": text_trace_id, "source": "otlp"}

    if not _is_pseudo_trace_fallback_enabled():
        return {"trace_id": "", "source": "missing"}

    # 默认兼容：缺失时生成稳定的 pseudo trace id。
    service_name = (
        _candidate_text(log_data.get("service_name"))
        or _candidate_text(log_data.get("service.name"))
        or _candidate_text(resource.get("service.name") if isinstance(resource, dict) else "")
        or "unknown"
    )
    timestamp = (
        _candidate_text(log_data.get("timestamp"))
        or _candidate_text(log_data.get("time"))
        or datetime.now().isoformat()
    )
    message = _candidate_text(log_data.get("message")) or _candidate_text(log_data.get("log"))
    host = _candidate_text(log_data.get("host")) or _candidate_text(
        context.get("host") if isinstance(context, dict) else ""
    )

    seed = f"{service_name}|{timestamp[:16]}|{host}|{message[:120]}"
    return {"trace_id": hashlib.md5(seed.encode("utf-8")).hexdigest(), "source": "synthetic"}


def extract_service_name(log_data: Dict[str, Any]) -> str:
    """
    从日志数据中提取服务名称

    Args:
        log_data: 原始日志数据

    Returns:
        str: 服务名称，如果无法提取则返回 "unknown"
    """
    # 尝试从不同字段提取服务名（按优先级排序）
    service_name = (
        log_data.get("service.name") or  # OTel 标准字段
        log_data.get("resource", {}).get("service.name") or  # Resource 属性
        log_data.get("kubernetes", {}).get("labels", {}).get("app") or  # K8s 标签
        log_data.get("app") or  # 通用字段
        "unknown"
    )

    # 调试：打印提取的信息
    if service_name == "unknown":
        # 只在第一次遇到时打印
        import sys
        if not hasattr(extract_service_name, 'warned'):
            extract_service_name.warned = True
            print(f"[DEBUG] Service name not found. Available keys: {list(log_data.keys())}")
            if 'resource' in log_data:
                print(f"[DEBUG] Resource keys: {list(log_data['resource'].keys())}")

    return service_name


def extract_instance_id(log_data: Dict[str, Any]) -> str:
    """
    从日志数据中提取实例ID
    
    Args:
        log_data: 原始日志数据
        
    Returns:
        str: 实例ID，如果无法提取则返回 "unknown"
    """
    # 尝试从不同字段提取实例ID
    k8s = log_data.get("kubernetes", {})
    pod_field = k8s.get("pod")
    instance_id = (
        log_data.get("service.instance.id") or 
        (pod_field if isinstance(pod_field, str) else (pod_field.get("name") if isinstance(pod_field, dict) else None)) or 
        k8s.get("pod_name") or
        log_data.get("instance") or 
        "unknown"
    )
    return instance_id


def extract_log_level(log_data: Dict[str, Any]) -> str:
    """
    从日志数据中提取日志级别

    Args:
        log_data: 原始日志数据

    Returns:
        str: 日志级别（小写），如果无法提取则返回 "info"
    """
    # 优先读取结构化字段，避免从 message/log 正文中误判。
    candidates = [
        log_data.get("level"),
        log_data.get("log_level"),
        log_data.get("severity"),
        log_data.get("severity_text"),
        log_data.get("event", {}).get("level"),
    ]
    tentative_info = ""
    for candidate in candidates:
        normalized = _normalize_level_value(candidate)
        if not normalized:
            continue
        # INFO 常被上游默认填充，优先让更强证据（severity_number / 前缀级别）覆盖。
        if normalized == "info":
            tentative_info = "info"
            continue
        if normalized:
            return normalized

    severity_number_level = _map_severity_number_to_level(log_data.get("severity_number"))
    if severity_number_level:
        return severity_number_level

    # 兼容纯文本日志前缀：仅在“时间戳/行首级别”模式命中时才提取。
    message_prefix_level = _extract_level_from_message_prefix(log_data.get("message"))
    if message_prefix_level:
        return message_prefix_level
    log_prefix_level = _extract_level_from_message_prefix(log_data.get("log"))
    if log_prefix_level:
        return log_prefix_level

    # 兼容部分旧格式：仅当 log 字段本身就是 level 表达时才接受。
    legacy_log_level = _normalize_level_value(log_data.get("log"))
    if legacy_log_level:
        if legacy_log_level == "info":
            return tentative_info or "info"
        return legacy_log_level

    return tentative_info or "info"


def extract_event_name(log_data: Dict[str, Any]) -> str:
    """
    从日志数据中提取事件名称
    
    Args:
        log_data: 原始日志数据
        
    Returns:
        str: 事件名称，基于日志内容推断
    """
    # 获取日志消息内容
    message = log_data.get("message", "") or log_data.get("log", "")
    
    # 基于关键词推断事件类型
    if "error" in message.lower():
        return "error"
    elif "warn" in message.lower():
        return "warning"
    elif "info" in message.lower():
        return "info"
    return "unknown"


def extract_trace_id(log_data: Dict[str, Any]) -> str:
    """
    从日志数据中提取 trace_id

    ⭐ P0优化：如果无法提取，自动生成伪trace_id

    Args:
        log_data: 原始日志数据

    Returns:
        str: trace_id，如果无法提取则生成一个基于服务名和时间戳的伪trace_id
    """
    return extract_trace_info(log_data).get("trace_id", "")


def extract_span_id(log_data: Dict[str, Any]) -> str:
    """
    从日志数据中提取 span_id
    
    Args:
        log_data: 原始日志数据
        
    Returns:
        str: span_id，如果无法提取则返回空字符串
    """
    attributes = log_data.get("attributes", {})
    resource = log_data.get("resource", {})
    context = log_data.get("context", {})
    raw_attributes = log_data.get("_raw_attributes", {})

    span_id = _normalize_otel_span_id(
        log_data.get("span_id")
        or log_data.get("spanId")
        or (context.get("span_id") if isinstance(context, dict) else "")
        or (context.get("spanId") if isinstance(context, dict) else "")
        or (attributes.get("span_id") if isinstance(attributes, dict) else "")
        or (attributes.get("spanId") if isinstance(attributes, dict) else "")
        or (attributes.get("span.id") if isinstance(attributes, dict) else "")
        or (attributes.get("otel.span_id") if isinstance(attributes, dict) else "")
        or (resource.get("span_id") if isinstance(resource, dict) else "")
        or (resource.get("span.id") if isinstance(resource, dict) else "")
        or (raw_attributes.get("span_id") if isinstance(raw_attributes, dict) else "")
        or (raw_attributes.get("span.id") if isinstance(raw_attributes, dict) else "")
    )
    if span_id:
        return span_id

    text_context = _extract_trace_span_from_log_text(log_data)
    return _normalize_otel_span_id(text_context.get("span_id", ""))


def extract_host(log_data: Dict[str, Any]) -> str:
    """
    从日志数据中提取主机信息
    
    Args:
        log_data: 原始日志数据
        
    Returns:
        str: 主机名，如果无法提取则返回 "unknown"
    """
    k8s = log_data.get("kubernetes", {})
    node_field = k8s.get("node")
    # 尝试从不同字段提取主机名
    host = (
        log_data.get("host.name") or 
        log_data.get("host") or 
        (node_field if isinstance(node_field, str) else (node_field.get("name") if isinstance(node_field, dict) else None)) or
        k8s.get("node_name") or 
        "unknown"
    )
    return host


def extract_k8s_context(log_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    从日志数据中提取 Kubernetes 上下文信息

    Args:
        log_data: 原始日志数据

    Returns:
        Dict[str, Any]: Kubernetes 上下文信息，包含：
            - namespace: 命名空间
            - pod: Pod 名称
            - node: 节点名称
            - host: 主机名
            - host_ip: 主机IP
            - labels: 标签信息
            - pod_id: Pod ID
    """
    # ⭐ P0修复：获取原始attributes（包含完整K8s信息）
    raw_attributes = log_data.get("_raw_attributes", {})

    # 获取 Kubernetes 相关信息（支持多种来源）
    k8s = log_data.get("kubernetes", {})

    # ⭐ P0修复：如果kubernetes字段为空，尝试从raw_attributes提取
    if not k8s and "kubernetes" in raw_attributes:
        k8s = raw_attributes["kubernetes"]

    # 提取 pod 名称（支持多种格式）
    pod_field = k8s.get("pod")
    pod_name = (
        k8s.get("pod_name") or 
        (pod_field if isinstance(pod_field, str) else (pod_field.get("name") if isinstance(pod_field, dict) else None)) or
        "unknown"
    )

    # 提取节点名称（支持多种格式）
    node_field = k8s.get("node")
    node_name = (
        k8s.get("node_name") or 
        (node_field if isinstance(node_field, str) else (node_field.get("name") if isinstance(node_field, dict) else None)) or
        k8s.get("host") or 
        "unknown"
    )

    # 提取 host IP（如果有的话）
    node_obj = k8s.get("node")
    host_ip = (
        k8s.get("host_ip") or 
        (node_obj.get("ip") if isinstance(node_obj, dict) else "") or 
        ""
    )

    # ⭐ P1优化：提取完整的kubernetes metadata
    pod_id = k8s.get("pod_id", "")
    container_name = k8s.get("container_name", "")
    container_id = k8s.get("container_id", "") or k8s.get("docker_id", "")  # ⭐ 支持docker_id
    container_image = k8s.get("container_image", "")

    # ⭐ P0修复：优先从fluent-bit添加的labels字段提取，否则从kubernetes对象中提取
    labels = k8s.get("labels", {})
    if not labels and isinstance(k8s, dict):
        # 尝试从kubernetes字典的子对象中提取
        if "labels" in k8s:
            labels = k8s["labels"]
        else:
            pod_obj = k8s.get("pod")
            if isinstance(pod_obj, dict) and "labels" in pod_obj:
                labels = pod_obj["labels"]

    # ⭐ P1优化：提取 annotations 中的资源指标
    annotations = k8s.get("annotations", {})
    if not annotations and isinstance(k8s, dict):
        if "annotations" in k8s:
            annotations = k8s["annotations"]
        else:
            pod_obj = k8s.get("pod")
            if isinstance(pod_obj, dict) and "annotations" in pod_obj:
                annotations = pod_obj["annotations"]

    # 提取资源限制和请求（从 annotations）
    resources = {
        "cpu_limit": annotations.get("cpu_limit", ""),
        "cpu_request": annotations.get("cpu_request", ""),
        "memory_limit": annotations.get("memory_limit", ""),
        "memory_request": annotations.get("memory_request", "")
    }

    # 返回结构化的 K8s 上下文
    return {
        "namespace": k8s.get("namespace_name") or k8s.get("namespace") or "unknown",
        "pod": pod_name,
        "node": node_name,
        "host": k8s.get("host") or node_name,
        "host_ip": host_ip,
        "labels": labels if labels else {},  # ⭐ P0修复：确保labels不为None
        "pod_id": pod_id,  # ⭐ 保留pod_id
        "container_name": container_name,  # ⭐ 保留container_name
        "container_id": container_id,  # ⭐ 保留container_id
        "container_image": container_image,  # ⭐ 保留container_image
        "resources": resources  # ⭐ P1优化：添加资源指标
    }


def extract_timestamp(log_data: Dict[str, Any]) -> str:
    """
    从日志数据中提取时间戳

    Args:
        log_data: 原始日志数据

    Returns:
        str: ISO 格式的时间戳字符串
    """
    # 尝试从不同字段提取时间戳
    timestamp = log_data.get("timestamp") or \
               log_data.get("time") or \
               log_data.get("@timestamp") or \
               log_data.get("time_unix_nano") or \
               log_data.get("timeUnixNano")

    # 如果没有找到时间戳，使用当前时间
    if not timestamp:
        return datetime.now().isoformat()

    # 如果是数字时间戳，转换为 ISO 格式
    if isinstance(timestamp, (int, float)):
        # 判断时间戳单位：
        # - 纳秒: > 1e15 (例如: 1739316000000000000)
        # - 微秒: > 1e12 且 < 1e15
        # - 毫秒: > 1e9 且 < 1e12
        # - 秒: < 1e10 (例如: 1739316000)
        if timestamp > 1e15:  # 纳秒级时间戳
            return datetime.fromtimestamp(timestamp / 1_000_000_000).isoformat()
        elif timestamp > 1e12:  # 微秒级时间戳
            return datetime.fromtimestamp(timestamp / 1_000_000).isoformat()
        elif timestamp > 1e9:  # 毫秒级时间戳
            return datetime.fromtimestamp(timestamp / 1_000).isoformat()
        else:  # 秒级时间戳
            return datetime.fromtimestamp(timestamp).isoformat()

    return str(timestamp)
