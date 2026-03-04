"""
Semantic Engine Traces API
处理 OpenTelemetry Traces 数据

支持两种格式：
1. application/json - OTLP JSON 格式
2. application/x-protobuf - OTLP Protobuf 二进制格式（推荐）
"""
import logging
import sys
import os
from typing import Dict, Any, List
from datetime import datetime

import json
from fastapi import Request, HTTPException

# 添加项目根目录到 Python 路径
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.otlp import parse_otlp_attributes
from storage.adapter import StorageAdapter

logger = logging.getLogger(__name__)

# 全局 storage 实例（在 main.py 中设置）
_STORAGE_ADAPTER: StorageAdapter = None


def set_storage_adapter(adapter: StorageAdapter):
    """设置 storage adapter 实例"""
    global _STORAGE_ADAPTER
    _STORAGE_ADAPTER = adapter


async def ingest_traces(request: Request) -> Dict[str, Any]:
    """
    接收 OTLP Traces 数据

    支持两种格式：
    1. application/json - OTLP JSON 格式
    2. application/x-protobuf - OTLP Protobuf 二进制格式（推荐）

    Args:
        request: FastAPI 请求对象

    Returns:
        Dict[str, Any]: 响应结果
    """
    try:
        # 获取 Content-Type
        content_type = request.headers.get("content-type", "")
        logger.info(f"接收到 Traces 请求，Content-Type: {content_type}")

        traces_data = []

        # 根据 Content-Type 选择解析方式
        if "application/x-protobuf" in content_type:
            # ⭐ OTLP Protobuf 二进制格式
            body = await request.body()
            logger.info(f"收到 OTLP Protobuf Traces 数据，大小: {len(body)} 字节")

            # 解析 protobuf 格式
            traces_data = process_otlp_traces_protobuf(body)

        elif "application/json" in content_type:
            # OTLP JSON 格式
            body = await request.json()
            logger.info(f"收到 OTLP JSON Traces 数据，大小: {len(str(body))} 字节")

            # 处理 traces 数据
            traces_data = process_otlp_traces_json(body)
        else:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported content type: {content_type}. Supported: application/x-protobuf, application/json"
            )

        # 保存到 ClickHouse
        if _STORAGE_ADAPTER and traces_data:
            _STORAGE_ADAPTER.save_traces(traces_data)

        return {
            "status": "ok",
            "message": f"Traces data received successfully",
            "spans_count": len(traces_data)
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"处理 Traces 请求时出错: {type(e).__name__}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


def process_otlp_traces_json(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    处理 OTLP JSON 格式的 Traces 数据

    Args:
        data: OTLP JSON 数据

    Returns:
        List[Dict[str, Any]]: Spans 数据列表
    """
    try:
        # 解析 resourceSpans
        resource_spans = data.get("resourceSpans", [])
        all_spans = []

        for rs in resource_spans:
            resource = rs.get("resource", {})
            resource_attrs_list = resource.get("attributes", [])

            # 将 OTLP attributes 数组转换为字典
            resource_attrs = parse_otlp_attributes(resource_attrs_list)

            # 提取 service_name
            service_name = resource_attrs.get("service.name", "unknown")

            # 提取 k8s 信息
            k8s_attrs = resource_attrs.get("kubernetes", {})
            pod_name = k8s_attrs.get("pod_name", "unknown")
            namespace = k8s_attrs.get("namespace_name", "unknown")

            # 解析 scopeSpans
            scope_spans = rs.get("scopeSpans", [])

            for ss in scope_spans:
                spans = ss.get("spans", [])

                # 处理每个 span
                for span in spans:
                    span_data = parse_span_data(span, service_name, pod_name, namespace, resource_attrs)
                    if span_data:
                        all_spans.append(span_data)

        logger.info(f"处理了 {len(all_spans)} 个 spans")
        return all_spans

    except Exception as e:
        logger.error(f"解析 OTLP Traces JSON 时出错: {e}")
        return []


def parse_span_data(
    span: Dict[str, Any],
    service_name: str,
    pod_name: str,
    namespace: str,
    resource_attrs: Dict[str, Any]
) -> Dict[str, Any]:
    """
    解析单个 Span 的数据

    Args:
        span: Span 数据
        service_name: 服务名称
        pod_name: Pod 名称
        namespace: 命名空间
        resource_attrs: Resource attributes

    Returns:
        Dict[str, Any]: Span 数据字典
    """
    try:
        # 基本信息
        trace_id = span.get("traceId", "")
        span_id = span.get("spanId", "")
        parent_span_id = span.get("parentSpanId", "")
        trace_state = span.get("traceState", "")

        # 名称和类型
        name = span.get("name", "")
        kind = span.get("kind", "INTERNAL")  # INTERNAL, SERVER, CLIENT, PRODUCER, CONSUMER

        # 时间信息
        start_time_unix_nano = span.get("startTimeUnixNano", 0)
        end_time_unix_nano = span.get("endTimeUnixNano", 0)

        # 转换时间戳
        start_time = datetime.fromtimestamp(start_time_unix_nano / 1e9).isoformat()
        end_time = datetime.fromtimestamp(end_time_unix_nano / 1e9).isoformat()

        # 计算持续时间（纳秒）
        duration_ns = end_time_unix_nano - start_time_unix_nano
        duration_ms = int(duration_ns / 1_000_000)  # 转换为毫秒

        # Status
        status = span.get("status", {})
        status_code = status.get("code", "UNSET")  # UNSET, OK, ERROR
        status_message = status.get("message", "")

        # Attributes（OTLP 数组格式）
        attributes_list = span.get("attributes", [])
        attributes = parse_otlp_attributes(attributes_list)

        # Events
        events = span.get("events", [])
        events_json = json.dumps(events, ensure_ascii=False)

        # Links
        links = span.get("links", [])
        links_json = json.dumps(links, ensure_ascii=False)

        # 合并所有 attributes 用于 tags
        all_attributes = {**resource_attrs, **attributes}
        tags_json = json.dumps(all_attributes, ensure_ascii=False)

        return {
            "trace_id": trace_id,
            "span_id": span_id,
            "parent_span_id": parent_span_id,
            "trace_state": trace_state,
            "service_name": service_name,
            "pod_name": pod_name,
            "namespace": namespace,
            "operation_name": name,
            "span_kind": kind,
            "start_time": start_time,
            "end_time": end_time,
            "duration_ns": duration_ns,
            "duration_ms": duration_ms,
            "status_code": status_code,
            "status_message": status_message,
            "attributes": json.dumps(attributes, ensure_ascii=False),
            "events": events_json,
            "links": links_json,
            "tags": tags_json
        }

    except Exception as e:
        logger.error(f"解析 Span 数据时出错: {e}")
        return {}


def process_otlp_traces_protobuf(body: bytes) -> List[Dict[str, Any]]:
    """
    处理 OTLP Protobuf 二进制格式的 Traces 数据

    Args:
        body: Protobuf 二进制数据

    Returns:
        List[Dict[str, Any]]: Spans 数据列表
    """
    try:
        # 动态导入 protobuf 模块
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

        # 解析 protobuf 消息
        request = ExportTraceServiceRequest()
        request.ParseFromString(body)

        logger.info(f"成功解析 OTLP Protobuf 请求，包含 {len(request.resource_spans)} 个 resource spans")

        all_spans = []

        # 遍历 resource_spans
        for resource_span in request.resource_spans:
            resource = resource_span.resource

            # 解析 resource attributes
            resource_attrs = {}
            for kv in resource.attributes:
                key = kv.key
                value = parse_protobuf_value(kv.value)
                resource_attrs[key] = value

            # 提取 service_name
            service_name = resource_attrs.get("service.name", "unknown")

            # 提取 k8s 信息
            k8s_pod_name = resource_attrs.get("k8s.pod.name", "unknown")
            k8s_namespace = resource_attrs.get("k8s.namespace.name", "unknown")

            # 遍历 scope_spans
            for scope_span in resource_span.scope_spans:
                # 遍历 spans
                for span in scope_span.spans:
                    span_data = parse_span_data_protobuf(
                        span, service_name, k8s_pod_name, k8s_namespace, resource_attrs
                    )
                    if span_data:
                        all_spans.append(span_data)

        logger.info(f"处理了 {len(all_spans)} 个 spans (protobuf)")
        return all_spans

    except Exception as e:
        logger.error(f"解析 OTLP Traces Protobuf 时出错: {e}", exc_info=True)
        return []


def parse_protobuf_value(any_value) -> Any:
    """
    解析 OTLP Protobuf AnyValue 类型

    Args:
        any_value: opentelemetry.proto.common.v1.common_pb2.AnyValue

    Returns:
        Any: 解析后的值
    """
    if any_value.HasField('string_value'):
        return any_value.string_value
    elif any_value.HasField('bool_value'):
        return any_value.bool_value
    elif any_value.HasField('int_value'):
        return any_value.int_value
    elif any_value.HasField('double_value'):
        return any_value.double_value
    elif any_value.HasField('array_value'):
        return [parse_protobuf_value(v) for v in any_value.array_value.values]
    elif any_value.HasField('kvlist_value'):
        return {kv.key: parse_protobuf_value(kv.value) for kv in any_value.kvlist_value.values}
    elif any_value.HasField('bytes_value'):
        return any_value.bytes_value.hex()
    else:
        return None


def parse_span_data_protobuf(
    span,
    service_name: str,
    pod_name: str,
    namespace: str,
    resource_attrs: Dict[str, Any]
) -> Dict[str, Any]:
    """
    解析单个 Protobuf Span 的数据

    Args:
        span: opentelemetry.proto.trace.v1.trace_pb2.Span
        service_name: 服务名称
        pod_name: Pod 名称
        namespace: 命名空间
        resource_attrs: Resource attributes

    Returns:
        Dict[str, Any]: Span 数据字典
    """
    try:
        # 基本信息
        trace_id = span.trace_id.hex()
        span_id = span.span_id.hex()
        parent_span_id = span.parent_span_id.hex() if span.parent_span_id else ""
        trace_state = span.trace_state

        # 名称和类型
        name = span.name
        kind = span.SpanKind.Name(span.kind)  # INTERNAL, SERVER, CLIENT, PRODUCER, CONSUMER

        # 时间信息
        start_time_unix_nano = span.start_time_unix_nano
        end_time_unix_nano = span.end_time_unix_nano

        # 转换时间戳
        start_time = datetime.fromtimestamp(start_time_unix_nano / 1e9).isoformat()
        end_time = datetime.fromtimestamp(end_time_unix_nano / 1e9).isoformat()

        # 计算持续时间
        duration_ns = end_time_unix_nano - start_time_unix_nano
        duration_ms = int(duration_ns / 1_000_000)

        # Status
        # Status.Code 枚举: 0=UNSET, 1=OK, 2=ERROR
        status_code_map = {0: "UNSET", 1: "OK", 2: "ERROR"}
        status_code = status_code_map.get(span.status.code, "UNSET")
        status_message = span.status.message if hasattr(span.status, 'message') else ""

        # Attributes
        attributes = {}
        for kv in span.attributes:
            key = kv.key
            value = parse_protobuf_value(kv.value)
            attributes[key] = value

        # Events
        events_list = []
        for event in span.events:
            event_dict = {
                "time_unix_nano": event.time_unix_nano,
                "name": event.name,
                "attributes": {kv.key: parse_protobuf_value(kv.value) for kv in event.attributes}
            }
            events_list.append(event_dict)
        events_json = json.dumps(events_list, ensure_ascii=False)

        # Links
        links_list = []
        for link in span.links:
            link_dict = {
                "trace_id": link.trace_id.hex(),
                "span_id": link.span_id.hex(),
                "trace_state": link.trace_state,
                "attributes": {kv.key: parse_protobuf_value(kv.value) for kv in link.attributes}
            }
            links_list.append(link_dict)
        links_json = json.dumps(links_list, ensure_ascii=False)

        # 合并所有 attributes 用于 tags
        all_attributes = {**resource_attrs, **attributes}
        tags_json = json.dumps(all_attributes, ensure_ascii=False)

        return {
            "trace_id": trace_id,
            "span_id": span_id,
            "parent_span_id": parent_span_id,
            "trace_state": trace_state,
            "service_name": service_name,
            "pod_name": pod_name,
            "namespace": namespace,
            "operation_name": name,
            "span_kind": kind,
            "start_time": start_time,
            "end_time": end_time,
            "duration_ns": duration_ns,
            "duration_ms": duration_ms,
            "status_code": status_code,
            "status_message": status_message,
            "attributes": json.dumps(attributes, ensure_ascii=False),
            "events": events_json,
            "links": links_json,
            "tags": tags_json
        }

    except Exception as e:
        logger.error(f"解析 Protobuf Span 数据时出错: {e}", exc_info=True)
        return {}
