"""
Protobuf 格式数据解析服务
支持 OTLP Protobuf 格式的日志、指标、追踪数据解析
"""
from typing import Dict, Any, List, Optional
import logging

# 导入OTLP Protobuf定义
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from google.protobuf.json_format import MessageToDict

logger = logging.getLogger(__name__)


class ProtobufParser:
    """Protobuf 数据解析器"""

    @staticmethod
    def parse_logs_protobuf(data: bytes) -> Dict[str, Any]:
        """
        解析 OTLP Protobuf 格式的日志数据

        Args:
            data: Protobuf 编码的二进制数据

        Returns:
            Dict[str, Any]: 解析后的字典格式数据

        Raises:
            Exception: 解析失败时抛出异常
        """
        try:
            # 解析 Protobuf 消息
            logs_request = ExportLogsServiceRequest()
            logs_request.ParseFromString(data)

            # 转换为 JSON 格式便于处理
            logs_dict = MessageToDict(logs_request)

            logger.debug("Parsed logs protobuf, resourceLogs=%s", len(logs_dict.get("resourceLogs", [])))

            return logs_dict

        except Exception as e:
            logger.exception("Failed to parse logs protobuf: %s", e)
            raise

    @staticmethod
    def parse_metrics_protobuf(data: bytes) -> Dict[str, Any]:
        """
        解析 OTLP Protobuf 格式的指标数据

        Args:
            data: Protobuf 编码的二进制数据

        Returns:
            Dict[str, Any]: 解析后的字典格式数据

        Raises:
            Exception: 解析失败时抛出异常
        """
        try:
            # 解析 Protobuf 消息
            metrics_request = ExportMetricsServiceRequest()
            metrics_request.ParseFromString(data)

            # 转换为 JSON 格式便于处理
            metrics_dict = MessageToDict(metrics_request)

            logger.debug("Parsed metrics protobuf, resourceMetrics=%s", len(metrics_dict.get("resourceMetrics", [])))

            return metrics_dict

        except Exception as e:
            logger.exception("Failed to parse metrics protobuf: %s", e)
            raise

    @staticmethod
    def parse_traces_protobuf(data: bytes) -> Dict[str, Any]:
        """
        解析 OTLP Protobuf 格式的追踪数据

        Args:
            data: Protobuf 编码的二进制数据

        Returns:
            Dict[str, Any]: 解析后的字典格式数据

        Raises:
            Exception: 解析失败时抛出异常
        """
        try:
            # 解析 Protobuf 消息
            traces_request = ExportTraceServiceRequest()
            traces_request.ParseFromString(data)

            # 转换为 JSON 格式便于处理
            traces_dict = MessageToDict(traces_request)

            logger.debug("Parsed traces protobuf, resourceSpans=%s", len(traces_dict.get("resourceSpans", [])))

            return traces_dict

        except Exception as e:
            logger.exception("Failed to parse traces protobuf: %s", e)
            raise

    @staticmethod
    def extract_log_records(logs_dict: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        从解析后的日志数据中提取所有日志记录

        Args:
            logs_dict: 解析后的日志字典

        Returns:
            List[Dict[str, Any]]: 日志记录列表
        """
        log_records = []

        resource_logs = logs_dict.get("resourceLogs", [])
        for resource_log in resource_logs:
            scope_logs = resource_log.get("scopeLogs", [])
            for scope_log in scope_logs:
                records = scope_log.get("logRecords", [])
                for record in records:
                    log_records.append(record)

        logger.debug("Extracted log records=%s", len(log_records))
        return log_records

    @staticmethod
    def is_protobuf_content_type(content_type: str) -> bool:
        """
        检查Content-Type是否表示Protobuf格式

        Args:
            content_type: HTTP Content-Type头

        Returns:
            bool: 是否是Protobuf格式
        """
        if not content_type:
            return False

        content_type_lower = content_type.lower()
        protobuf_indicators = [
            "application/x-protobuf",
            "application/protobuf",
            "application/vnd.google.protobuf",
            "application/octet-stream",
        ]

        return any(indicator in content_type_lower for indicator in protobuf_indicators)

    @staticmethod
    def validate_protobuf_schema(data: bytes, data_type: str = "logs") -> bool:
        """
        验证Protobuf数据的schema有效性

        Args:
            data: Protobuf编码的二进制数据
            data_type: 数据类型 ("logs", "metrics", "traces")

        Returns:
            bool: schema是否有效

        Raises:
            Exception: schema验证失败时抛出异常
        """
        try:
            if data_type == "logs":
                request = ExportLogsServiceRequest()
                request.ParseFromString(data)
            elif data_type == "metrics":
                request = ExportMetricsServiceRequest()
                request.ParseFromString(data)
            elif data_type == "traces":
                request = ExportTraceServiceRequest()
                request.ParseFromString(data)
            else:
                raise ValueError(f"Unknown data type: {data_type}")

            logger.debug("Validated protobuf schema, data_type=%s", data_type)
            return True

        except Exception as e:
            logger.error("Protobuf schema validation failed: %s", e)
            raise


# 全局单例实例
_parser_instance: Optional[ProtobufParser] = None


def get_protobuf_parser() -> ProtobufParser:
    """
    获取ProtobufParser单例

    Returns:
        ProtobufParser: 解析器实例
    """
    global _parser_instance

    if _parser_instance is None:
        _parser_instance = ProtobufParser()

    return _parser_instance
