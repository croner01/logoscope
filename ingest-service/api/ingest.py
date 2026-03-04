"""
Ingest Service 数据接收 API
处理 OpenTelemetry OTLP 协议数据（支持JSON和Protobuf格式）
"""
from fastapi import APIRouter, Request, HTTPException
from typing import Dict, Any
import json
import logging

from config import config
from services.queue_writer import write_to_queue
from services.protobuf_parser import get_protobuf_parser

router = APIRouter()
logger = logging.getLogger(__name__)

def _get_stream_name(data_type: str) -> str:
    """根据信号类型选择 stream 名称。"""
    if data_type == "logs":
        return config.redis_stream_logs
    if data_type == "metrics":
        return config.redis_stream_metrics
    if data_type == "traces":
        return config.redis_stream_traces
    return config.redis_stream


@router.post("/v1/logs")
async def ingest_logs(request: Request):
    """
    接收 OTLP 日志数据（支持JSON和Protobuf）

    Args:
        request: FastAPI Request 对象

    Returns:
        Dict[str, Any]: 处理结果
    """
    try:
        # 读取请求体（FastAPI 的 request.body() 返回 bytes）
        body_bytes = await request.body()

        if not body_bytes:
            raise HTTPException(status_code=400, detail="Empty request body")

        content_type = request.headers.get("content-type", "")
        logger.debug(
            "Received logs request, content_type=%s content_length=%s",
            content_type,
            len(body_bytes),
        )

        # 获取Protobuf解析器
        protobuf_parser = get_protobuf_parser()

        # 检查是否是Protobuf格式
        is_protobuf = protobuf_parser.is_protobuf_content_type(content_type)

        body_str = None
        parsed_dict = None

        if is_protobuf:
            logger.debug("Detected Protobuf logs payload, parsing")
            try:
                # 验证并解析Protobuf
                protobuf_parser.validate_protobuf_schema(body_bytes, data_type="logs")
                parsed_dict = protobuf_parser.parse_logs_protobuf(body_bytes)

                # 转换为JSON字符串以便存储
                body_str = json.dumps(parsed_dict)
                logger.debug("Parsed Protobuf logs payload to JSON")

            except Exception as proto_error:
                logger.warning(
                    "Protobuf logs parse failed, fallback to base64: %s",
                    proto_error,
                )
                # 如果Protobuf解析失败，使用base64编码原始数据
                import base64
                body_str = base64.b64encode(body_bytes).decode("utf-8")
        else:
            # 不是Protobuf格式，尝试解析为JSON
            try:
                body_str = body_bytes.decode("utf-8")
                # 验证是否是有效的JSON
                json.loads(body_str)
                logger.debug("Parsed logs payload as JSON")
            except Exception as json_error:
                logger.debug("Logs payload is not JSON, fallback to base64: %s", json_error)
                # 如果不是JSON，使用base64编码
                import base64
                body_str = base64.b64encode(body_bytes).decode("utf-8")

        # 写入 Redis Stream（保持现有数据处理流程不变）
        await write_to_queue(
            stream=_get_stream_name("logs"),
            data_type="logs",
            payload=body_str,
            metadata={
                "content_type": content_type,
                "content_length": len(body_bytes),
                "is_binary": not is_protobuf and (body_str is None or content_type != "application/json"),
                "is_protobuf": is_protobuf,
                "protobuf_parsed": parsed_dict is not None
            }
        )

        return {
            "status": "success",
            "message": "Logs ingested successfully",
            "service": config.app_name,
            "format": "protobuf" if is_protobuf else "json"
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to ingest logs: %s", e)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/v1/metrics")
async def ingest_metrics(request: Request):
    """
    接收 OTLP 指标数据（支持JSON和Protobuf）

    Args:
        request: FastAPI Request 对象

    Returns:
        Dict[str, Any]: 处理结果
    """
    try:
        # 读取请求体（FastAPI 的 request.body() 返回 bytes）
        body_bytes = await request.body()

        if not body_bytes:
            raise HTTPException(status_code=400, detail="Empty request body")

        content_type = request.headers.get("content-type", "")
        logger.debug(
            "Received metrics request, content_type=%s content_length=%s",
            content_type,
            len(body_bytes),
        )

        # 获取Protobuf解析器
        protobuf_parser = get_protobuf_parser()

        # 检查是否是Protobuf格式
        is_protobuf = protobuf_parser.is_protobuf_content_type(content_type)

        body_str = None
        parsed_dict = None

        if is_protobuf:
            logger.debug("Detected Protobuf metrics payload, parsing")
            try:
                # 验证并解析Protobuf
                protobuf_parser.validate_protobuf_schema(body_bytes, data_type="metrics")
                parsed_dict = protobuf_parser.parse_metrics_protobuf(body_bytes)

                # 转换为JSON字符串以便存储
                body_str = json.dumps(parsed_dict)
                logger.debug("Parsed Protobuf metrics payload to JSON")

            except Exception as proto_error:
                logger.warning(
                    "Protobuf metrics parse failed, fallback to base64: %s",
                    proto_error,
                )
                import base64
                body_str = base64.b64encode(body_bytes).decode("utf-8")
        else:
            # 不是Protobuf格式，尝试解析为JSON
            try:
                body_str = body_bytes.decode("utf-8")
                json.loads(body_str)
                logger.debug("Parsed metrics payload as JSON")
            except Exception as json_error:
                logger.debug("Metrics payload is not JSON, fallback to base64: %s", json_error)
                import base64
                body_str = base64.b64encode(body_bytes).decode("utf-8")

        # 写入 Redis Stream
        await write_to_queue(
            stream=_get_stream_name("metrics"),
            data_type="metrics",
            payload=body_str,
            metadata={
                "content_type": content_type,
                "content_length": len(body_bytes),
                "is_protobuf": is_protobuf,
                "protobuf_parsed": parsed_dict is not None
            }
        )

        return {
            "status": "success",
            "message": "Metrics ingested successfully",
            "service": config.app_name,
            "format": "protobuf" if is_protobuf else "json"
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to ingest metrics: %s", e)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/v1/traces")
async def ingest_traces(request: Request):
    """
    接收 OTLP 追踪数据（支持JSON和Protobuf）

    Args:
        request: FastAPI Request 对象

    Returns:
        Dict[str, Any]: 处理结果
    """
    try:
        # 读取请求体（FastAPI 的 request.body() 返回 bytes）
        body_bytes = await request.body()

        if not body_bytes:
            raise HTTPException(status_code=400, detail="Empty request body")

        content_type = request.headers.get("content-type", "")
        logger.debug(
            "Received traces request, content_type=%s content_length=%s",
            content_type,
            len(body_bytes),
        )

        # 获取Protobuf解析器
        protobuf_parser = get_protobuf_parser()

        # 检查是否是Protobuf格式
        is_protobuf = protobuf_parser.is_protobuf_content_type(content_type)

        body_str = None
        parsed_dict = None

        if is_protobuf:
            logger.debug("Detected Protobuf traces payload, parsing")
            try:
                # 验证并解析Protobuf
                protobuf_parser.validate_protobuf_schema(body_bytes, data_type="traces")
                parsed_dict = protobuf_parser.parse_traces_protobuf(body_bytes)

                # 转换为JSON字符串以便存储
                body_str = json.dumps(parsed_dict)
                logger.debug("Parsed Protobuf traces payload to JSON")

            except Exception as proto_error:
                logger.warning(
                    "Protobuf traces parse failed, fallback to base64: %s",
                    proto_error,
                )
                import base64
                body_str = base64.b64encode(body_bytes).decode("utf-8")
        else:
            # 不是Protobuf格式，尝试解析为JSON
            try:
                body_str = body_bytes.decode("utf-8")
                parsed_dict = json.loads(body_str)  # ⭐ 保存解析后的JSON
                logger.debug("Parsed traces payload as JSON")
            except Exception as json_error:
                logger.debug("Traces payload is not JSON, fallback to base64: %s", json_error)
                import base64
                body_str = base64.b64encode(body_bytes).decode("utf-8")

        # 写入 Redis Stream（统一单路径，避免 traces 双写导致一致性问题）
        await write_to_queue(
            stream=_get_stream_name("traces"),
            data_type="traces",
            payload=body_str,
            metadata={
                "content_type": content_type,
                "content_length": len(body_bytes),
                "is_protobuf": is_protobuf,
                "protobuf_parsed": parsed_dict is not None
            }
        )

        return {
            "status": "success",
            "message": "Traces ingested successfully",
            "service": config.app_name,
            "format": "protobuf" if is_protobuf else "json"
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to ingest traces: %s", e)
        raise HTTPException(status_code=500, detail="Internal server error")
