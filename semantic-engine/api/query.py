"""
查询辅助 API（兼容历史测试）。
"""
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

from api.cache import clear_cache, get_cache_stats

_STORAGE_ADAPTER = None


def set_storage_adapter(storage_adapter: Any) -> None:
    """设置全局 storage adapter。"""
    global _STORAGE_ADAPTER
    _STORAGE_ADAPTER = storage_adapter


def _require_storage() -> Any:
    if _STORAGE_ADAPTER is None:
        raise HTTPException(status_code=503, detail="Storage adapter not initialized")
    return _STORAGE_ADAPTER


def _build_where(conditions: List[str]) -> str:
    if not conditions:
        return ""
    return "WHERE " + " AND ".join(conditions)


async def query_metrics(
    limit: int = 100,
    service_name: Optional[str] = None,
    metric_name: Optional[str] = None,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
) -> Dict[str, Any]:
    """查询指标数据。"""
    storage = _require_storage()
    try:
        conditions: List[str] = []
        if service_name:
            conditions.append(f"service_name = '{service_name}'")
        if metric_name:
            conditions.append(f"metric_name = '{metric_name}'")
        if start_time:
            conditions.append(f"timestamp >= '{start_time}'")
        if end_time:
            conditions.append(f"timestamp <= '{end_time}'")
        sql = (
            "SELECT timestamp, service_name, metric_name, value, labels "
            "FROM metrics "
            f"{_build_where(conditions)} "
            "ORDER BY timestamp DESC "
            f"LIMIT {max(1, int(limit))}"
        ).strip()
        rows = storage.execute_query(sql) or []
        return {
            "count": len(rows),
            "limit": max(1, int(limit)),
            "data": rows,
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


async def query_metrics_stats(
    service_name: Optional[str] = None,
    metric_name: Optional[str] = None,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
) -> Dict[str, Any]:
    """查询指标统计。"""
    storage = _require_storage()
    try:
        conditions: List[str] = []
        if service_name:
            conditions.append(f"service_name = '{service_name}'")
        if metric_name:
            conditions.append(f"metric_name = '{metric_name}'")
        if start_time:
            conditions.append(f"timestamp >= '{start_time}'")
        if end_time:
            conditions.append(f"timestamp <= '{end_time}'")
        where_sql = _build_where(conditions)

        total_rows = storage.execute_query(f"SELECT count(*) AS total FROM metrics {where_sql}") or []
        by_service_rows = storage.execute_query(
            f"SELECT service_name, count(*) AS count FROM metrics {where_sql} GROUP BY service_name"
        ) or []
        by_metric_rows = storage.execute_query(
            f"SELECT metric_name, count(*) AS count FROM metrics {where_sql} GROUP BY metric_name"
        ) or []

        total = int((total_rows[0] or {}).get("total", 0)) if total_rows else 0
        by_service = {str(item.get("service_name") or "unknown"): int(item.get("count") or 0) for item in by_service_rows}
        by_metric = {str(item.get("metric_name") or "unknown"): int(item.get("count") or 0) for item in by_metric_rows}

        return {
            "total": total,
            "byService": by_service,
            "byMetricName": by_metric,
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


async def query_traces(
    limit: int = 100,
    service_name: Optional[str] = None,
    trace_id: Optional[str] = None,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
) -> Dict[str, Any]:
    """查询链路数据。"""
    storage = _require_storage()
    try:
        conditions: List[str] = []
        if service_name:
            conditions.append(f"service_name = '{service_name}'")
        if trace_id:
            conditions.append(f"trace_id = '{trace_id}'")
        if start_time:
            conditions.append(f"start_time >= '{start_time}'")
        if end_time:
            conditions.append(f"start_time <= '{end_time}'")
        sql = (
            "SELECT trace_id, span_id, parent_span_id, service_name, operation_name, start_time, duration_ms, status "
            "FROM traces "
            f"{_build_where(conditions)} "
            "ORDER BY start_time DESC "
            f"LIMIT {max(1, int(limit))}"
        ).strip()
        rows = storage.execute_query(sql) or []
        return {
            "count": len(rows),
            "limit": max(1, int(limit)),
            "data": rows,
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


async def query_traces_stats(
    service_name: Optional[str] = None,
    trace_id: Optional[str] = None,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
) -> Dict[str, Any]:
    """查询链路统计。"""
    storage = _require_storage()
    try:
        conditions: List[str] = []
        if service_name:
            conditions.append(f"service_name = '{service_name}'")
        if trace_id:
            conditions.append(f"trace_id = '{trace_id}'")
        if start_time:
            conditions.append(f"start_time >= '{start_time}'")
        if end_time:
            conditions.append(f"start_time <= '{end_time}'")
        where_sql = _build_where(conditions)

        total_rows = storage.execute_query(f"SELECT count(*) AS total FROM traces {where_sql}") or []
        by_service_rows = storage.execute_query(
            f"SELECT service_name, count(*) AS count FROM traces {where_sql} GROUP BY service_name"
        ) or []
        by_operation_rows = storage.execute_query(
            f"SELECT operation_name, count(*) AS count FROM traces {where_sql} GROUP BY operation_name"
        ) or []
        avg_rows = storage.execute_query(f"SELECT avg(duration_ms) AS avg_duration FROM traces {where_sql}") or []

        total = int((total_rows[0] or {}).get("total", 0)) if total_rows else 0
        by_service = {str(item.get("service_name") or "unknown"): int(item.get("count") or 0) for item in by_service_rows}
        by_operation = {
            str(item.get("operation_name") or "unknown"): int(item.get("count") or 0)
            for item in by_operation_rows
        }
        avg_duration = float((avg_rows[0] or {}).get("avg_duration", 0) or 0) if avg_rows else 0.0

        return {
            "total": total,
            "byService": by_service,
            "byOperation": by_operation,
            "avgDuration": round(avg_duration, 2),
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


async def get_cache_statistics() -> Dict[str, Any]:
    """返回缓存统计。"""
    return get_cache_stats()


async def clear_api_cache(pattern: Optional[str] = None) -> Dict[str, Any]:
    """清理缓存。"""
    clear_cache(pattern)
    return {"status": "ok"}

