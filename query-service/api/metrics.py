"""
Semantic Engine Metrics API
处理 OpenTelemetry Metrics 数据
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


async def ingest_metrics(request: Request) -> Dict[str, Any]:
    """
    接收 OTLP Metrics 数据

    Args:
        request: FastAPI 请求对象

    Returns:
        Dict[str, Any]: 响应结果
    """
    try:
        # 获取 Content-Type
        content_type = request.headers.get("content-type", "")
        logger.info(f"接收到 Metrics 请求，Content-Type: {content_type}")

        # 解析请求体
        if "application/json" in content_type:
            body = await request.json()
            logger.info(f"收到 OTLP JSON Metrics 数据，大小: {len(str(body))} 字节")

            # 处理 metrics 数据
            metrics_data = process_otlp_metrics_json(body)

            # 保存到 ClickHouse
            if _STORAGE_ADAPTER and metrics_data:
                _STORAGE_ADAPTER.save_metrics(metrics_data)

            return {
                "status": "ok",
                "message": f"Metrics data received successfully",
                "data_points_count": len(metrics_data)
            }
        else:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported content type: {content_type}"
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"处理 Metrics 请求时出错: {type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


def process_otlp_metrics_json(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    处理 OTLP JSON 格式的 Metrics 数据

    Args:
        data: OTLP JSON 数据

    Returns:
        List[Dict[str, Any]]: 所有数据点列表
    """
    try:
        # 解析 resourceMetrics
        resource_metrics = data.get("resourceMetrics", [])
        all_data_points = []

        for rm in resource_metrics:
            resource = rm.get("resource", {})
            resource_attrs_list = resource.get("attributes", [])

            # 将 OTLP attributes 数组转换为字典
            resource_attrs = parse_otlp_attributes(resource_attrs_list)

            # 提取 service_name
            service_name = resource_attrs.get("service.name", "unknown")

            # 解析 scopeMetrics
            scope_metrics = rm.get("scopeMetrics", [])

            for sm in scope_metrics:
                scope = sm.get("scope", {})
                metrics = sm.get("metrics", [])

                # 处理每个 metric
                for metric in metrics:
                    metric_name = metric.get("name", "")

                    # 解析 metric 数据点
                    metric_data_points = parse_metric_data(metric, service_name, resource_attrs)
                    all_data_points.extend(metric_data_points)

                    logger.debug(f"Metric: {metric_name}, data points: {len(metric_data_points)}")

        logger.info(f"处理了 {len(all_data_points)} 个 metrics 数据点")
        return all_data_points

    except Exception as e:
        logger.error(f"解析 OTLP Metrics JSON 时出错: {e}")
        return []


def parse_metric_data(metric: Dict[str, Any], service_name: str, resource_attrs: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    解析单个 metric 的数据点

    Args:
        metric: Metric 数据
        service_name: 服务名称
        resource_attrs: Resource attributes

    Returns:
        List[Dict[str, Any]]: 数据点列表
    """
    data_points = []

    # OpenTelemetry Metrics 类型
    # Gauge: 任意值（如内存使用）
    # Sum: 单调递增或递减的计数器（Counter 是单调递增的 Sum）
    # Histogram: 统计分布
    # ExponentialHistogram: 指数直方图
    # Summary: 客户端计算的统计摘要

    metric_name = metric.get("name", "")

    # 处理 Gauge
    if "gauge" in metric:
        gauge_data = metric["gauge"]
        data_points.extend(parse_number_data_points(gauge_data, metric_name, "gauge", service_name, resource_attrs))

    # 处理 Sum (Counter/UpDownCounter)
    elif "sum" in metric:
        sum_data = metric["sum"]
        is_monotonic = sum_data.get("isMonotonic", False)
        metric_type = "counter" if is_monotonic else "updowncounter"
        data_points.extend(parse_number_data_points(sum_data, metric_name, metric_type, service_name, resource_attrs))

    # 处理 Histogram
    elif "histogram" in metric:
        histogram_data = metric["histogram"]
        data_points.extend(parse_histogram_data_points(histogram_data, metric_name, service_name, resource_attrs))

    # 处理 ExponentialHistogram
    elif "exponentialHistogram" in metric:
        exp_hist_data = metric["exponentialHistogram"]
        data_points.extend(parse_exponential_histogram_data_points(exp_hist_data, metric_name, service_name, resource_attrs))

    # 处理 Summary
    elif "summary" in metric:
        summary_data = metric["summary"]
        data_points.extend(parse_summary_data_points(summary_data, metric_name, service_name, resource_attrs))

    return data_points


def parse_number_data_points(
    data_points: Dict[str, Any],
    metric_name: str,
    metric_type: str,
    service_name: str,
    resource_attrs: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """
    解析数值数据点（Gauge/Sum）
    """
    points = []

    # 数据点数组
    dp_array = data_points.get("dataPoints", [])

    for dp in dp_array:
        # 时间戳（Unix 纳秒）
        time_unix_nano = dp.get("timeUnixNano", 0)
        timestamp = datetime.fromtimestamp(time_unix_nano / 1e9).isoformat()

        # 数值
        value = None
        if "asInt" in dp:
            value = float(dp["asInt"])
        elif "asDouble" in dp:
            value = dp["asDouble"]

        # Attributes（OTLP 数组格式）
        attributes_list = dp.get("attributes", [])
        attributes = parse_otlp_attributes(attributes_list)

        # 合并 resource attributes
        all_attributes = {**resource_attrs, **attributes}

        if value is not None:
            points.append({
                "metric_name": metric_name,
                "metric_type": metric_type,
                "timestamp": timestamp,
                "value": value,
                "attributes": all_attributes,
                "service_name": service_name
            })

    return points


def parse_histogram_data_points(
    histogram_data: Dict[str, Any],
    metric_name: str,
    service_name: str,
    resource_attrs: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """
    解析直方图数据点
    """
    points = []

    dp_array = histogram_data.get("dataPoints", [])

    for dp in dp_array:
        time_unix_nano = dp.get("timeUnixNano", 0)
        timestamp = datetime.fromtimestamp(time_unix_nano / 1e9).isoformat()

        # 统计数据
        count = dp.get("count", 0)
        sum_value = dp.get("sum", 0.0)

        # 桶数据
        bucket_counts = dp.get("bucketCounts", [])
        explicit_bounds = dp.get("explicitBounds", [])

        # Attributes（OTLP 数组格式）
        attributes_list = dp.get("attributes", [])
        attributes = parse_otlp_attributes(attributes_list)
        all_attributes = {**resource_attrs, **attributes}

        # 转换为 JSON 字符串存储
        histogram_json = json.dumps({
            "count": count,
            "sum": sum_value,
            "bucket_counts": bucket_counts,
            "explicit_bounds": explicit_bounds
        })

        points.append({
            "metric_name": metric_name,
            "metric_type": "histogram",
            "timestamp": timestamp,
            "value": float(sum_value),
            "attributes": all_attributes,
            "service_name": service_name,
            "histogram_data": histogram_json
        })

    return points


def parse_exponential_histogram_data_points(
    exp_hist_data: Dict[str, Any],
    metric_name: str,
    service_name: str,
    resource_attrs: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """
    解析指数直方图数据点
    """
    # 指数直方图处理较复杂，暂不实现
    logger.debug(f"Exponential histogram 暂不支持: {metric_name}")
    return []


def parse_summary_data_points(
    summary_data: Dict[str, Any],
    metric_name: str,
    service_name: str,
    resource_attrs: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """
    解析摘要数据点
    """
    points = []

    dp_array = summary_data.get("dataPoints", [])

    for dp in dp_array:
        time_unix_nano = dp.get("timeUnixNano", 0)
        timestamp = datetime.fromtimestamp(time_unix_nano / 1e9).isoformat()

        # 统计数据
        count = dp.get("count", 0)
        sum_value = dp.get("sum", 0.0)

        # 分位数
        quantile_values = dp.get("quantileValues", [])

        # Attributes（OTLP 数组格式）
        attributes_list = dp.get("attributes", [])
        attributes = parse_otlp_attributes(attributes_list)
        all_attributes = {**resource_attrs, **attributes}

        # 转换为 JSON 字符串存储
        summary_json = json.dumps({
            "count": count,
            "sum": sum_value,
            "quantile_values": quantile_values
        })

        points.append({
            "metric_name": metric_name,
            "metric_type": "summary",
            "timestamp": timestamp,
            "value": float(sum_value),
            "attributes": all_attributes,
            "service_name": service_name,
            "summary_data": summary_json
        })

    return points
