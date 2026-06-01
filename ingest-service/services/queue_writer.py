"""
队列写入服务 - 解耦优化版本
负责将数据写入 Redis Stream，支持懒加载连接和内存队列降级

支持两种数据格式：
1. Fluent Bit JSON格式（直接来自Fluent Bit）
2. OpenTelemetry OTLP格式（来自OTel组件）

优化特性：
- 懒加载 Redis 连接（首次写入时才连接）
- 内存队列降级（Redis 不可用时缓存数据）
- 自动重连机制
- 连接恢复后自动刷新队列
- 日志带时间戳
"""
import json
import asyncio
import logging
import base64
import binascii
from typing import Dict, Any, Optional, Tuple
import redis.asyncio as aioredis
from datetime import datetime

from config import config

# 配置日志
logger = logging.getLogger(__name__)

# Redis 客户端
_redis_client: Optional[aioredis.Redis] = None
_redis_connected: bool = False
_redis_connecting: bool = False

# 内存队列（用于 Redis 不可用时降级）
_memory_queue: Optional[asyncio.Queue] = None
_memory_queue_max_size: int = int(config.memory_queue_max_size)
_memory_queue_drop_count: int = 0

# 重连配置
_reconnect_task: Optional[asyncio.Task] = None
_reconnect_interval: int = 5  # 秒
_max_reconnect_attempts: int = 3

# 统计信息
_stats = {
    "total_written": 0,
    "redis_written": 0,
    "memory_queued": 0,
    "dropped": 0,
    "reconnect_attempts": 0,
    "last_error": None
}


def _get_memory_queue() -> asyncio.Queue:
    """获取内存队列（懒加载）"""
    global _memory_queue
    if _memory_queue is None:
        _memory_queue = asyncio.Queue(maxsize=_memory_queue_max_size)
    return _memory_queue


async def init_queue_writer():
    """
    初始化队列写入服务（不强制连接 Redis）
    现在只是初始化内存队列，Redis 连接采用懒加载
    """
    global _memory_queue
    _memory_queue = asyncio.Queue(maxsize=_memory_queue_max_size)
    logger.info(f"Initialized with memory queue (max_size={_memory_queue_max_size})")


async def _ensure_redis_connection() -> bool:
    """
    确保 Redis 连接（懒加载 + 自动重连）

    Returns:
        bool: 是否连接成功
    """
    global _redis_client, _redis_connected, _redis_connecting

    # 如果已连接，快速检查
    if _redis_connected and _redis_client:
        try:
            await _redis_client.ping()
            return True
        except Exception:
            _redis_connected = False
            logger.warning("Redis connection lost, will retry")

    # 如果正在连接中，等待
    if _redis_connecting:
        # 等待最多 5 秒
        for _ in range(10):
            await asyncio.sleep(0.5)
            if _redis_connected:
                return True
        return False

    # 尝试连接
    _redis_connecting = True
    try:
        for attempt in range(_max_reconnect_attempts):
            try:
                logger.info(f"Connecting to Redis (attempt {attempt + 1}/{_max_reconnect_attempts})...")
                _redis_client = await aioredis.from_url(
                    f"redis://{config.redis_host}:{config.redis_port}/{config.redis_db}",
                    password=config.redis_password,
                    decode_responses=True,
                    socket_connect_timeout=5,
                    socket_keepalive=True,
                    retry_on_timeout=True
                )
                await _redis_client.ping()
                _redis_connected = True
                _stats["reconnect_attempts"] += 1
                logger.info(f"Connected to Redis at {config.redis_host}:{config.redis_port}")

                # 启动后台刷新任务
                asyncio.create_task(_flush_memory_queue())

                return True
            except Exception as e:
                logger.warning(f"Redis connection attempt {attempt + 1} failed: {e}")
                _stats["last_error"] = str(e)
                if attempt < _max_reconnect_attempts - 1:
                    await asyncio.sleep(1)

        logger.error(f"Failed to connect to Redis after {_max_reconnect_attempts} attempts")
        return False
    finally:
        _redis_connecting = False


async def _flush_memory_queue():
    """
    将内存队列中的数据刷新到 Redis
    在连接恢复后自动调用
    """
    global _memory_queue, _redis_connected

    if not _redis_connected or _memory_queue is None:
        return

    flushed = 0
    while not _memory_queue.empty():
        try:
            item = _memory_queue.get_nowait()
            await _write_to_redis(
                stream=item["stream"],
                data_type=item["data_type"],
                payload=item["payload"],
                metadata=item["metadata"]
            )
            flushed += 1
        except asyncio.QueueEmpty:
            break
        except Exception as e:
            logger.error(f"Failed to flush item from memory queue: {e}")
            break

    if flushed > 0:
        logger.info(f"Flushed {flushed} items from memory queue to Redis")


async def _start_reconnect_loop():
    """
    启动后台重连任务
    定期检查 Redis 连接并尝试重连
    """
    global _reconnect_task

    if _reconnect_task is not None and not _reconnect_task.done():
        return

    async def _reconnect_loop():
        while True:
            await asyncio.sleep(_reconnect_interval)
            if not _redis_connected:
                success = await _ensure_redis_connection()
                if success:
                    logger.info("Redis reconnected successfully")

    _reconnect_task = asyncio.create_task(_reconnect_loop())


async def _write_to_redis(stream: str, data_type: str, payload: str, metadata: Dict[str, Any]) -> Dict[str, Any]:
    """
    直接写入 Redis Stream（内部方法）
    """
    global _redis_client

    message_data = {
        "data": payload,
        "data_type": data_type,
        "ingest_time": datetime.utcnow().isoformat()
    }

    message_id = await _redis_client.xadd(stream, message_data)
    _stats["redis_written"] += 1
    logger.info(f"Wrote to stream {stream}, id: {message_id}, type: {data_type}")

    return {"status": "success", "stream": stream, "message_id": message_id, "mode": "redis"}


async def _write_to_memory_queue(stream: str, data_type: str, payload: str, metadata: Dict[str, Any]) -> Dict[str, Any]:
    """
    写入内存队列（降级方案）
    """
    global _memory_queue_drop_count

    queue = _get_memory_queue()

    # 如果队列已满，丢弃最旧的数据
    if queue.full():
        try:
            dropped = queue.get_nowait()
            _memory_queue_drop_count += 1
            _stats["dropped"] += 1
            if _memory_queue_drop_count % 100 == 1:  # 每100条记录一次
                logger.warning(f"Memory queue full, dropped oldest item (total dropped: {_memory_queue_drop_count})")
        except asyncio.QueueEmpty:
            pass

    await queue.put({
        "stream": stream,
        "data_type": data_type,
        "payload": payload,
        "metadata": metadata,
        "timestamp": datetime.utcnow().isoformat()
    })
    _stats["memory_queued"] += 1

    queue_size = queue.qsize()
    if queue_size % 100 == 0:  # 每100条记录一次
        logger.info(f"Queued to memory (size: {queue_size})")

    return {
        "status": "success",
        "stream": stream,
        "mode": "memory_queue",
        "queue_size": queue_size,
        "warning": "Redis unavailable, data queued in memory"
    }


def is_otlp_format(payload_dict: dict) -> bool:
    return "resourceLogs" in payload_dict or "resourceMetrics" in payload_dict or "resourceSpans" in payload_dict


def extract_value(value_dict: dict) -> Any:
    """递归提取 OTLP value 字段"""
    if "stringValue" in value_dict:
        return value_dict["stringValue"]
    elif "intValue" in value_dict:
        return value_dict["intValue"]
    elif "doubleValue" in value_dict:
        return value_dict["doubleValue"]
    elif "boolValue" in value_dict:
        return value_dict["boolValue"]
    elif "kvlistValue" in value_dict:
        kvlist = value_dict["kvlistValue"]
        if "values" in kvlist:
            result = {}
            for kv in kvlist["values"]:
                kv_key = kv.get("key", "")
                kv_value = kv.get("value", {})
                if kv_key:
                    result[kv_key] = extract_value(kv_value)
            return result
    elif "arrayValue" in value_dict:
        arr = value_dict["arrayValue"]
        if "values" in arr:
            return [extract_value(v) for v in arr["values"]]
    return None


def extract_attributes(attrs_list: list) -> Dict[str, Any]:
    """从 OTLP attributes 列表提取为字典"""
    result = {}
    for attr in attrs_list:
        key = attr.get("key", "")
        if not key:
            continue
        value = attr.get("value", {})
        extracted = extract_value(value)
        if extracted is not None:
            result[key] = extracted
    return result


def _normalize_otel_id(raw_value: Any, expected_bytes: int) -> str:
    """Normalize OTLP id to lowercase hex; accept hex or base64 bytes."""
    if raw_value is None:
        return ""

    text = str(raw_value).strip()
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


def transform_single_otlp_log(resource_dict: dict, log_record: dict, metadata: Dict[str, Any]) -> Dict[str, Any]:
    """转换单条 OTLP Log Record 为 Worker 期望的格式"""
    log_attrs = log_record.get("attributes", [])
    log_attrs_dict = extract_attributes(log_attrs)

    log_body = log_record.get("body", {})
    log_content = log_body.get("stringValue", "") if isinstance(log_body, dict) else str(log_body)

    # 初始化变量
    pod_name = None
    namespace_name = None
    node_name = None
    pod_id = None
    container_name = None
    container_id = None
    container_image = None
    k8s_labels = {}

    # 1. 从 log_attrs_dict 提取 kubernetes 信息
    k8s_info = log_attrs_dict.get("kubernetes", {})
    if isinstance(k8s_info, dict):
        pod_name = k8s_info.get("pod_name")
        namespace_name = k8s_info.get("namespace_name")
        node_name = k8s_info.get("host") or k8s_info.get("node_name")
        pod_id = k8s_info.get("pod_id")
        container_name = k8s_info.get("container_name")
        container_id = k8s_info.get("docker_id") or k8s_info.get("container_id")
        container_image = k8s_info.get("container_image")
        if "labels" in k8s_info and isinstance(k8s_info["labels"], dict):
            k8s_labels.update(k8s_info["labels"])

    # 2. 从 log_attrs_dict 提取 k8s_ 前缀字段
    for key, value in log_attrs_dict.items():
        if key.startswith("k8s_"):
            clean_key = key[4:]
            if clean_key == "pod_name" and not pod_name:
                pod_name = value
            elif clean_key == "namespace_name" and not namespace_name:
                namespace_name = value
            elif clean_key == "host" and not node_name:
                node_name = value
            elif clean_key == "pod_id" and not pod_id:
                pod_id = value
            elif clean_key == "container_name" and not container_name:
                container_name = value
            elif clean_key == "docker_id" and not container_id:
                container_id = value
            elif clean_key == "container_image" and not container_image:
                container_image = value
            elif clean_key == "labels" and isinstance(value, dict):
                k8s_labels.update(value)

    # 3. 从 resource_dict 提取
    for key, value in resource_dict.items():
        if key == "kubernetes" and isinstance(value, dict):
            if not pod_name and "pod_name" in value:
                pod_name = value["pod_name"]
            if not namespace_name and "namespace_name" in value:
                namespace_name = value["namespace_name"]
            if not node_name and "host" in value:
                node_name = value["host"]
            if not pod_id and "pod_id" in value:
                pod_id = value["pod_id"]
            if not container_name and "container_name" in value:
                container_name = value["container_name"]
            if not container_id and "docker_id" in value:
                container_id = value["docker_id"]
            if not container_image and "container_image" in value:
                container_image = value["container_image"]
            if "labels" in value and isinstance(value["labels"], dict):
                k8s_labels.update(value["labels"])
        elif key.startswith("k8s_"):
            clean_key = key[4:]
            if clean_key == "pod_name" and not pod_name:
                pod_name = value
            elif clean_key == "namespace_name" and not namespace_name:
                namespace_name = value
            elif clean_key == "host" and not node_name:
                node_name = value
            elif clean_key == "labels" and isinstance(value, dict):
                k8s_labels.update(value)

    # 从 pod_name 推断 service_name
    service_name = ""
    if pod_name:
        import re
        match = re.match(r'^(.+?)-[a-f0-9]{8,10}(-[a-f0-9]{4,8})?$', pod_name)
        service_name = match.group(1) if match else pod_name

    logger.debug(f"Service: {service_name}, Pod: {pod_name}, Node: {node_name}")

    trace_id = _normalize_otel_trace_id(
        log_record.get("traceId")
        or log_record.get("trace_id")
        or log_attrs_dict.get("trace_id")
        or log_attrs_dict.get("traceId")
        or log_attrs_dict.get("trace.id")
        or log_attrs_dict.get("otel.trace_id")
    )
    span_id = _normalize_otel_span_id(
        log_record.get("spanId")
        or log_record.get("span_id")
        or log_attrs_dict.get("span_id")
        or log_attrs_dict.get("spanId")
        or log_attrs_dict.get("span.id")
        or log_attrs_dict.get("otel.span_id")
    )
    flags = log_record.get("flags", log_attrs_dict.get("flags", 0))
    try:
        normalized_flags = int(flags)
    except (TypeError, ValueError):
        normalized_flags = 0

    trace_id_source = "otlp" if trace_id else "missing"
    log_attrs_dict.setdefault("trace_id_source", trace_id_source)
    if trace_id:
        log_attrs_dict.setdefault("trace_id", trace_id)
    if span_id:
        log_attrs_dict.setdefault("span_id", span_id)
    if normalized_flags:
        log_attrs_dict.setdefault("flags", normalized_flags)

    return {
        "log": log_content,
        "timestamp": log_record.get("timeUnixNano", ""),
        "severity": log_record.get("severityText", ""),
        "service.name": service_name,
        "trace_id": trace_id,
        "span_id": span_id,
        "flags": normalized_flags,
        "trace_id_source": trace_id_source,
        "attributes": log_attrs_dict,
        "resource": resource_dict,
        "kubernetes": {
            "pod": pod_name,
            "pod_name": pod_name,
            "namespace": namespace_name,
            "namespace_name": namespace_name,
            "node": node_name,
            "node_name": node_name,
            "pod_id": pod_id,
            "container_name": container_name,
            "container_id": container_id,
            "container_image": container_image,
            "labels": k8s_labels
        }
    }


def transform_otlp_logs(payload_dict: dict, metadata: Dict[str, Any]) -> Dict[str, Any]:
    """转换 OTLP Logs 数据为 Worker 期望的格式（保留向后兼容）"""
    resource_logs = payload_dict.get("resourceLogs", [{}])
    resource = resource_logs[0].get("resource", {})
    resource_attrs = resource.get("attributes", [])
    resource_dict = extract_attributes(resource_attrs)

    scope_logs = resource_logs[0].get("scopeLogs", [{}])
    log_records = scope_logs[0].get("logRecords", [{}]) if isinstance(scope_logs[0], dict) else []
    first_log_record = log_records[0] if log_records else {}

    return transform_single_otlp_log(resource_dict, first_log_record, metadata)


def transform_fluent_bit_json(payload_dict: dict, metadata: Dict[str, Any]) -> Dict[str, Any]:
    """转换 Fluent Bit JSON 格式"""
    log_content = payload_dict.get("log", "") or payload_dict.get("message", "")
    timestamp = payload_dict.get("timestamp", "")
    severity = payload_dict.get("severity", "") or payload_dict.get("level", "")

    pod_name = None
    namespace_name = None
    node_name = None
    pod_id = None
    container_name = None
    container_id = None
    container_image = None
    k8s_labels = {}

    # 从 attributes 提取
    attrs = payload_dict.get("attributes", {})
    for key, value in attrs.items():
        if key.startswith("k8s_"):
            clean_key = key[4:]
            if clean_key == "pod_name":
                pod_name = value
            elif clean_key == "namespace_name":
                namespace_name = value
            elif clean_key == "host":
                node_name = value
            elif clean_key == "pod_id":
                pod_id = value
            elif clean_key == "container_name":
                container_name = value
            elif clean_key == "docker_id":
                container_id = value
            elif clean_key == "container_image":
                container_image = value
            elif clean_key == "labels" and isinstance(value, dict):
                k8s_labels.update(value)

    service_name = pod_name or ""

    return {
        "log": log_content,
        "timestamp": timestamp,
        "severity": severity,
        "service.name": service_name,
        "attributes": payload_dict,
        "resource": {},
        "kubernetes": {
            "pod_name": pod_name,
            "namespace_name": namespace_name,
            "node_name": node_name,
            "pod_id": pod_id,
            "container_name": container_name,
            "container_id": container_id,
            "container_image": container_image,
            "labels": k8s_labels
        }
    }


def _build_log_queue_messages(
    payload_dict: Optional[Any],
    payload: str,
    metadata: Dict[str, Any]
) -> list[Dict[str, Any]]:
    """
    构建日志消息列表（logs 信号）

    对 OTLP logs 尽量拆分为单条记录；对 Fluent Bit JSON 直接转换。
    """
    messages: list[Dict[str, Any]] = []

    if payload_dict is None:
        return [{
            "log": payload,
            "timestamp": "",
            "severity": "",
            "service.name": "",
            "attributes": {"raw_payload": payload},
            "resource": {},
            "kubernetes": {
                "pod_name": None,
                "namespace_name": None,
                "node_name": None,
                "pod_id": None,
                "labels": {}
            }
        }]

    items = payload_dict if isinstance(payload_dict, list) else [payload_dict]
    for item in items:
        if not isinstance(item, dict):
            continue

        if is_otlp_format(item):
            resource_logs = item.get("resourceLogs", [])
            for resource_log in resource_logs:
                resource = resource_log.get("resource", {})
                resource_attrs = resource.get("attributes", [])
                resource_dict = extract_attributes(resource_attrs)

                scope_logs = resource_log.get("scopeLogs", [])
                for scope_log in scope_logs:
                    log_records = scope_log.get("logRecords", [])
                    for log_record in log_records:
                        messages.append(
                            transform_single_otlp_log(resource_dict, log_record, metadata)
                        )
        else:
            messages.append(transform_fluent_bit_json(item, metadata))

    if not messages:
        messages.append({
            "log": payload,
            "timestamp": "",
            "severity": "",
            "service.name": "",
            "attributes": {"raw_payload": payload},
            "resource": {},
            "kubernetes": {
                "pod_name": None,
                "namespace_name": None,
                "node_name": None,
                "pod_id": None,
                "labels": {}
            }
        })

    return messages


def _build_non_log_queue_message(
    data_type: str,
    payload_dict: Optional[Any],
    payload: str
) -> Dict[str, Any]:
    """
    构建非日志消息（metrics/traces）

    保留原始信号语义，避免伪装成日志格式导致下游误处理。
    """
    if payload_dict is None:
        return {
            "signal_type": data_type,
            "raw_payload": payload,
        }

    return {
        "signal_type": data_type,
        "payload": payload_dict,
    }


async def write_to_queue(stream: str, data_type: str, payload: str, metadata: Dict[str, Any]) -> Dict[str, Any]:
    """
    写入数据到队列（支持 Redis 和内存队列降级）

    Args:
        stream: Stream 名称
        data_type: 数据类型 (logs/metrics/traces)
        payload: 数据内容
        metadata: 元数据

    Returns:
        Dict[str, Any]: 写入结果
    """
    global _stats, _redis_connected

    _stats["total_written"] += 1

    try:
        # 解析 payload（优先 JSON）
        payload_dict: Optional[Any] = None
        try:
            payload_dict = json.loads(payload)
            logger.debug("Successfully parsed payload as JSON")
        except Exception as e:
            logger.debug(f"Payload is not valid JSON: {e}")

        # 按信号类型构建消息，避免 metrics/traces 被错误转换成日志
        if data_type == "logs":
            queue_messages = _build_log_queue_messages(payload_dict, payload, metadata)
        else:
            queue_messages = [_build_non_log_queue_message(data_type, payload_dict, payload)]

        # 尝试连接 Redis
        redis_available = await _ensure_redis_connection()

        results = []
        for message_payload in queue_messages:
            json_payload = json.dumps(message_payload, ensure_ascii=False)

            if redis_available:
                # Redis 可用，直接写入
                try:
                    result = await _write_to_redis(stream, data_type, json_payload, metadata)
                    results.append(result)
                except Exception as e:
                    logger.warning(f"Redis write failed, falling back to memory queue: {e}")
                    result = await _write_to_memory_queue(stream, data_type, json_payload, metadata)
                    results.append(result)
                    # 标记连接断开，启动重连
                    _redis_connected = False
                    await _start_reconnect_loop()
            else:
                # Redis 不可用，写入内存队列
                result = await _write_to_memory_queue(stream, data_type, json_payload, metadata)
                results.append(result)
                # 启动后台重连
                await _start_reconnect_loop()

        # 返回汇总结果
        if len(results) == 1:
            return results[0]
        else:
            return {
                "status": "success",
                "stream": stream,
                "message_count": len(results),  # logs 可拆多条，metrics/traces 通常为 1
                "results": results,
                "mode": "batch"
            }

    except Exception as e:
        logger.error(f"Failed to write to queue: {e}")
        import traceback
        traceback.print_exc()
        raise


def get_stats() -> Dict[str, Any]:
    """获取队列写入统计信息"""
    return {
        **_stats,
        "redis_connected": _redis_connected,
        "memory_queue_size": _memory_queue.qsize() if _memory_queue else 0,
        "memory_queue_max_size": _memory_queue_max_size,
        "mode": "normal" if _redis_connected else "degraded"
    }


def is_redis_connected() -> bool:
    """检查 Redis 连接状态"""
    return _redis_connected
