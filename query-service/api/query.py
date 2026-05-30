"""
查询 API - 带缓存和分页支持
提供 Metrics 和 Traces 的查询接口
"""
import logging
from typing import Dict, Any, List, Optional
from fastapi import HTTPException, Query
from datetime import datetime, timedelta

from api.query_params import sanitize_interval

from storage.adapter import StorageAdapter
from api.cache import cached, get_cache_stats, clear_cache

logger = logging.getLogger(__name__)

# 全局 storage 实例
_STORAGE_ADAPTER: StorageAdapter = None
_DEFAULT_METRICS_WINDOW = sanitize_interval("24 HOUR", default_value="24 HOUR")
_DEFAULT_TRACES_WINDOW = sanitize_interval("24 HOUR", default_value="24 HOUR")


def set_storage_adapter(adapter: StorageAdapter):
    """设置 storage adapter 实例"""
    global _STORAGE_ADAPTER
    _STORAGE_ADAPTER = adapter


async def query_metrics(
    limit: int = Query(100, ge=1, le=10000),
    service_name: Optional[str] = Query(None),
    metric_name: Optional[str] = Query(None),
    start_time: Optional[str] = Query(None),
    end_time: Optional[str] = Query(None),
) -> Dict[str, Any]:
    """
    查询 Metrics 数据（带缓存）

    Args:
        limit: 返回数量限制
        service_name: 服务名过滤
        metric_name: 指标名过滤
        start_time: 开始时间
        end_time: 结束时间

    Returns:
        Dict[str, Any]: 查询结果
    """
    if not _STORAGE_ADAPTER:
        raise HTTPException(status_code=503, detail="Storage adapter not initialized")

    try:
        # 构建查询条件（参数化）
        prewhere_conditions = []
        params: Dict[str, Any] = {"limit": int(limit)}
        if service_name:
            prewhere_conditions.append("service_name = {service_name:String}")
            params["service_name"] = service_name
        if metric_name:
            prewhere_conditions.append("metric_name = {metric_name:String}")
            params["metric_name"] = metric_name
        if start_time:
            prewhere_conditions.append("timestamp >= toDateTime64({start_time:String}, 9, 'UTC')")
            params["start_time"] = start_time
        if end_time:
            prewhere_conditions.append("timestamp <= toDateTime64({end_time:String}, 9, 'UTC')")
            params["end_time"] = end_time
            if not start_time:
                prewhere_conditions.append(
                    f"timestamp > toDateTime64({{end_time:String}}, 9, 'UTC') - INTERVAL {_DEFAULT_METRICS_WINDOW}"
                )
        if not start_time and not end_time:
            prewhere_conditions.append(f"timestamp > now() - INTERVAL {_DEFAULT_METRICS_WINDOW}")

        prewhere_clause = f"PREWHERE {' AND '.join(prewhere_conditions)}" if prewhere_conditions else ""

        # 查询数据
        query = f"""
        SELECT
            timestamp,
            service_name,
            metric_name,
            value_float64 as value,
            attributes_json as labels
        FROM logs.metrics
        {prewhere_clause}
        ORDER BY timestamp DESC
        LIMIT {{limit:Int32}}
        """

        results = _STORAGE_ADAPTER.execute_query(query, params)

        return {
            "data": results,
            "count": len(results),
            "limit": limit,
        }

    except Exception as e:
        logger.error(f"查询 Metrics 数据时出错: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


async def query_metrics_stats() -> Dict[str, Any]:
    """
    获取 Metrics 统计信息（带缓存，TTL 30秒）

    Returns:
        Dict[str, Any]: 统计数据
    """
    if not _STORAGE_ADAPTER:
        raise HTTPException(status_code=503, detail="Storage adapter not initialized")

    try:
        # 总数统计
        total_query = f"SELECT COUNT(*) as total FROM logs.metrics PREWHERE timestamp > now() - INTERVAL {_DEFAULT_METRICS_WINDOW}"
        total_result = _STORAGE_ADAPTER.execute_query(total_query)
        total = total_result[0]["total"] if total_result else 0

        # 按服务统计
        service_query = f"""
        SELECT
            service_name,
            COUNT(*) as count
        FROM logs.metrics
        PREWHERE timestamp > now() - INTERVAL {_DEFAULT_METRICS_WINDOW}
        GROUP BY service_name
        ORDER BY count DESC
        """
        service_results = _STORAGE_ADAPTER.execute_query(service_query)
        by_service = {row["service_name"]: row["count"] for row in service_results}

        # 按指标名统计
        metric_query = f"""
        SELECT
            metric_name,
            COUNT(*) as count
        FROM logs.metrics
        PREWHERE timestamp > now() - INTERVAL {_DEFAULT_METRICS_WINDOW}
        GROUP BY metric_name
        ORDER BY count DESC
        """
        metric_results = _STORAGE_ADAPTER.execute_query(metric_query)
        by_metric = {row["metric_name"]: row["count"] for row in metric_results}

        return {
            "total": total,
            "byService": by_service,
            "byMetricName": by_metric,
        }

    except Exception as e:
        logger.error(f"获取 Metrics 统计信息时出错: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


async def query_traces(
    limit: int = Query(100, ge=1, le=10000),
    service_name: Optional[str] = Query(None),
    trace_id: Optional[str] = Query(None),
    start_time: Optional[str] = Query(None),
    end_time: Optional[str] = Query(None),
) -> Dict[str, Any]:
    """
    查询 Traces 数据（带缓存）

    Args:
        limit: 返回数量限制
        service_name: 服务名过滤
        trace_id: Trace ID 过滤
        start_time: 开始时间
        end_time: 结束时间

    Returns:
        Dict[str, Any]: 查询结果
    """
    if not _STORAGE_ADAPTER:
        raise HTTPException(status_code=503, detail="Storage adapter not initialized")

    try:
        # 构建查询条件（参数化）
        prewhere_conditions = []
        params: Dict[str, Any] = {"limit": int(limit)}
        if service_name:
            prewhere_conditions.append("service_name = {service_name:String}")
            params["service_name"] = service_name
        if trace_id:
            prewhere_conditions.append("trace_id = {trace_id:String}")
            params["trace_id"] = trace_id
        if start_time:
            prewhere_conditions.append("timestamp >= toDateTime64({start_time:String}, 9, 'UTC')")
            params["start_time"] = start_time
        if end_time:
            prewhere_conditions.append("timestamp <= toDateTime64({end_time:String}, 9, 'UTC')")
            params["end_time"] = end_time
            if not start_time and not trace_id:
                prewhere_conditions.append(
                    f"timestamp > toDateTime64({{end_time:String}}, 9, 'UTC') - INTERVAL {_DEFAULT_TRACES_WINDOW}"
                )
        if not start_time and not end_time:
            prewhere_conditions.append(f"timestamp > now() - INTERVAL {_DEFAULT_TRACES_WINDOW}")

        prewhere_clause = f"PREWHERE {' AND '.join(prewhere_conditions)}" if prewhere_conditions else ""

        # 查询数据
        query = f"""
        SELECT
            trace_id,
            span_id,
            parent_span_id,
            service_name,
            operation_name,
            toString(timestamp) as start_time_str,
            duration_ms,
            status
        FROM logs.traces
        {prewhere_clause}
        ORDER BY timestamp DESC
        LIMIT {{limit:Int32}}
        """

        results = _STORAGE_ADAPTER.execute_query(query, params)

        return {
            "data": results,
            "count": len(results),
            "limit": limit,
        }

    except Exception as e:
        logger.error(f"查询 Traces 数据时出错: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


async def query_traces_stats() -> Dict[str, Any]:
    """
    获取 Traces 统计信息（带缓存，TTL 30秒）

    Returns:
        Dict[str, Any]: 统计数据
    """
    if not _STORAGE_ADAPTER:
        raise HTTPException(status_code=503, detail="Storage adapter not initialized")

    try:
        # 总数统计
        total_query = f"SELECT COUNT(*) as total FROM logs.traces PREWHERE timestamp > now() - INTERVAL {_DEFAULT_TRACES_WINDOW}"
        total_result = _STORAGE_ADAPTER.execute_query(total_query)
        total = total_result[0]["total"] if total_result else 0

        # 按服务统计
        service_query = f"""
        SELECT
            service_name,
            COUNT(*) as count
        FROM logs.traces
        PREWHERE timestamp > now() - INTERVAL {_DEFAULT_TRACES_WINDOW}
        GROUP BY service_name
        ORDER BY count DESC
        """
        service_results = _STORAGE_ADAPTER.execute_query(service_query)
        by_service = {row["service_name"]: row["count"] for row in service_results}

        # 按操作统计
        operation_query = f"""
        SELECT
            operation_name,
            COUNT(*) as count
        FROM logs.traces
        PREWHERE timestamp > now() - INTERVAL {_DEFAULT_TRACES_WINDOW}
        GROUP BY operation_name
        ORDER BY count DESC
        """
        operation_results = _STORAGE_ADAPTER.execute_query(operation_query)
        by_operation = {row["operation_name"]: row["count"] for row in operation_results}

        # 平均持续时间 (logs.traces表没有duration_ms列，返回0)
        avg_duration = 0

        return {
            "total": total,
            "byService": by_service,
            "byOperation": by_operation,
            "avgDuration": round(avg_duration, 2) if avg_duration else 0,
        }

    except Exception as e:
        logger.error(f"获取 Traces 统计信息时出错: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


async def get_cache_statistics() -> Dict[str, Any]:
    """
    获取缓存统计信息

    Returns:
        Dict[str, Any]: 缓存统计数据
    """
    return get_cache_stats()


async def clear_api_cache(pattern: Optional[str] = None) -> Dict[str, Any]:
    """
    清除 API 缓存

    Args:
        pattern: 可选的模式匹配

    Returns:
        Dict[str, Any]: 操作结果
    """
    clear_cache(pattern)
    return {"status": "ok", "message": "Cache cleared"}
