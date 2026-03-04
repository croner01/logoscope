"""
数据质量监控 API

提供数据质量指标、unknown 数据统计和告警阈值检查
"""

import asyncio
from fastapi import APIRouter, Query
from typing import Dict, Any, List
import logging
import re
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/quality", tags=["data-quality"])

# 数据质量阈值
QUALITY_THRESHOLDS = {
    "unknown_max_percent": 5.0,      # unknown 数据最大占比 (%)
    "null_max_percent": 0.1,         # null 数据最大占比 (%)
    "empty_max_percent": 0.1,        # 空字符串最大占比 (%)
    "min_data_freshness_seconds": 3600  # 数据最大延迟（秒）
}

# 全局 storage adapter
storage = None


def set_storage_adapter(storage_adapter):
    """设置 storage adapter"""
    global storage
    storage = storage_adapter


def _row_value(row: Any, key: str, index: int, default: Any = None) -> Any:
    """兼容 dict/tuple 两种查询返回结构。"""
    if isinstance(row, dict):
        return row.get(key, default)
    if isinstance(row, (list, tuple)):
        if 0 <= index < len(row):
            return row[index]
    return default


def _sanitize_interval(time_window: str, default_value: str = "24 HOUR") -> str:
    """规范化 INTERVAL 参数，避免 SQL 注入。"""
    pattern = re.compile(r"^\s*(\d+)\s+([A-Za-z]+)\s*$")
    match = pattern.match(str(time_window or ""))
    if not match:
        return default_value

    amount = int(match.group(1))
    unit_raw = match.group(2).upper()
    valid_units = {
        "MINUTE": "MINUTE",
        "MINUTES": "MINUTE",
        "HOUR": "HOUR",
        "HOURS": "HOUR",
        "DAY": "DAY",
        "DAYS": "DAY",
        "WEEK": "WEEK",
        "WEEKS": "WEEK",
    }
    if amount <= 0 or unit_raw not in valid_units:
        return default_value
    return f"{amount} {valid_units[unit_raw]}"


def _sanitize_limit(value: int, default_value: int, max_value: int) -> int:
    """限制 LIMIT 范围，避免异常值扩大查询压力。"""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default_value
    if parsed < 1:
        return 1
    return min(parsed, max_value)


async def _run_blocking(func, *args, **kwargs):
    """在线程池执行阻塞 IO，避免阻塞事件循环。"""
    return await asyncio.to_thread(func, *args, **kwargs)


@router.get("/overview")
async def get_data_quality_overview(
    time_window: str = Query("24 HOUR", description="时间窗口（如 24 HOUR, 7 DAY）")
) -> Dict[str, Any]:
    """
    获取数据质量概览

    返回各项数据质量指标的概览，包括：
    - 总记录数
    - unknown 数据占比
    - 空值数据占比
    - 数据新鲜度
    - 质量评分

    Returns:
        {
            "total_records": 1650000,
            "unknown_records": 87993,
            "unknown_percent": 5.3,
            "quality_score": 94.7,
            "status": "warning",
            "metrics": {...}
        }
    """
    try:
        safe_time_window = _sanitize_interval(time_window, default_value="24 HOUR")

        if not storage or not storage.ch_client:
            return {
                "status": "error",
                "error": "Storage not available"
            }

        # 查询总记录数和 unknown 记录数
        query = f"""
            SELECT
                COUNT(*) as total,
                countIf(service_name = 'unknown') as unknown_count,
                countIf(service_name = '') as empty_count,
                countIf(service_name IS NULL) as null_count,
                countIf(pod_name = '') as empty_pod_count,
                countIf(pod_name IS NULL) as null_pod_count,
                max(timestamp) as latest_timestamp,
                min(timestamp) as earliest_timestamp
            FROM logs.logs
            PREWHERE timestamp > now() - INTERVAL {safe_time_window}
        """

        result = await _run_blocking(storage.execute_query, query)

        if not result or len(result) == 0:
            return {
                "status": "error",
                "error": "No data returned"
            }

        row = result[0]
        total = _row_value(row, "total", 0, 1) or 1  # 避免除以 0
        unknown_count = _row_value(row, "unknown_count", 1, 0) or 0
        empty_count = _row_value(row, "empty_count", 2, 0) or 0
        null_count = _row_value(row, "null_count", 3, 0) or 0
        empty_pod_count = _row_value(row, "empty_pod_count", 4, 0) or 0
        null_pod_count = _row_value(row, "null_pod_count", 5, 0) or 0
        latest_timestamp = _row_value(row, "latest_timestamp", 6)
        earliest_timestamp = _row_value(row, "earliest_timestamp", 7)

        # 计算百分比
        unknown_percent = (unknown_count / total) * 100
        empty_percent = (empty_count / total) * 100
        null_percent = (null_count / total) * 100
        empty_pod_percent = (empty_pod_count / total) * 100
        null_pod_percent = (null_pod_count / total) * 100

        # 计算数据新鲜度（秒）
        data_freshness_seconds = 0
        if latest_timestamp:
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc)
            ts = latest_timestamp
            if hasattr(ts, 'tzinfo') and ts.tzinfo is not None and now.tzinfo is not None:
                pass
            elif hasattr(ts, 'tzinfo') and ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            delta = now - ts
            data_freshness_seconds = int(delta.total_seconds())

        # 计算质量评分（0-100）
        quality_score = 100.0
        quality_score -= unknown_percent * 2  # unknown 扣分
        quality_score -= null_percent * 10  # null 扣分
        quality_score -= empty_percent * 5  # empty 扣分

        # 判断状态
        if unknown_percent > QUALITY_THRESHOLDS["unknown_max_percent"]:
            status = "error"
        elif unknown_percent > QUALITY_THRESHOLDS["unknown_max_percent"] * 0.8:
            status = "warning"
        else:
            status = "healthy"

        return {
            "status": status,
            "quality_score": round(max(0, quality_score), 2),
            "total_records": total,
            "metrics": {
                "unknown": {
                    "count": unknown_count,
                    "percent": round(unknown_percent, 2),
                    "threshold": QUALITY_THRESHOLDS["unknown_max_percent"],
                    "status": "error" if unknown_percent > QUALITY_THRESHOLDS["unknown_max_percent"] else "ok"
                },
                "service_name": {
                    "empty_count": empty_count,
                    "empty_percent": round(empty_percent, 2),
                    "null_count": null_count,
                    "null_percent": round(null_percent, 2)
                },
                "pod_name": {
                    "empty_count": empty_pod_count,
                    "empty_percent": round(empty_pod_percent, 2),
                    "null_count": null_pod_count,
                    "null_percent": round(null_pod_percent, 2)
                },
                "freshness": {
                    "latest_timestamp": latest_timestamp.isoformat() if latest_timestamp else None,
                    "earliest_timestamp": earliest_timestamp.isoformat() if earliest_timestamp else None,
                    "lag_seconds": data_freshness_seconds,
                    "status": "ok" if data_freshness_seconds < QUALITY_THRESHOLDS["min_data_freshness_seconds"] else "warning"
                }
            },
            "generated_at": datetime.now(timezone.utc).isoformat()
        }

    except Exception as e:
        logger.error(f"Error getting data quality overview: {e}")
        return {
            "status": "error",
            "error": str(e)
        }


@router.get("/unknown/analysis")
async def get_unknown_analysis(
    limit: int = Query(100, ge=1, le=1000, description="返回数量限制"),
    time_window: str = Query("24 HOUR", description="时间窗口（如 24 HOUR, 7 DAY）"),
) -> Dict[str, Any]:
    """
    分析 unknown 数据的详细情况

    返回 unknown 数据的示例、时间分布、可能原因

    Args:
        limit: 返回数量限制

    Returns:
        {
            "total_unknown": 87993,
            "samples": [...],
            "time_distribution": {...},
            "possible_causes": [...]
        }
    """
    try:
        safe_limit = _sanitize_limit(limit, default_value=100, max_value=1000)
        safe_time_window = _sanitize_interval(time_window, default_value="24 HOUR")

        if not storage or not storage.ch_client:
            return {
                "status": "error",
                "error": "Storage not available"
            }

        # 查询 unknown 数据的示例
        query = f"""
            SELECT
                substring(message, 1, 150) as message,
                timestamp,
                pod_name,
                namespace
            FROM logs.logs
            PREWHERE service_name = 'unknown'
                 AND timestamp > now() - INTERVAL {safe_time_window}
            ORDER BY timestamp DESC
            LIMIT {{limit:Int32}}
        """

        result = await _run_blocking(storage.execute_query, query, {"limit": safe_limit})

        samples = []
        for row in result:
            samples.append({
                "message": _row_value(row, "message", 0),
                "timestamp": (
                    _row_value(row, "timestamp", 1).isoformat()
                    if _row_value(row, "timestamp", 1)
                    else None
                ),
                "pod_name": _row_value(row, "pod_name", 2),
                "namespace": _row_value(row, "namespace", 3),
            })

        # 查询时间分布
        time_query = f"""
            SELECT
                toStartOfHour(timestamp) as time_bucket,
                COUNT(*) as count
            FROM logs.logs
            PREWHERE service_name = 'unknown'
                 AND timestamp > now() - INTERVAL {safe_time_window}
            GROUP BY time_bucket
            ORDER BY time_bucket DESC
        """

        time_result = await _run_blocking(storage.execute_query, time_query)

        time_distribution = []
        for row in time_result:
            time_distribution.append({
                "time_bucket": (
                    _row_value(row, "time_bucket", 0).isoformat()
                    if _row_value(row, "time_bucket", 0)
                    else None
                ),
                "count": _row_value(row, "count", 1, 0),
            })

        return {
            "status": "ok",
            "samples": samples,
            "time_distribution": time_distribution,
            "possible_causes": [
                "K8s metadata not attached by OTel Collector",
                "Fluent Bit parsing failure",
                "Original container log format without service labels"
            ],
            "generated_at": datetime.now(timezone.utc).isoformat()
        }

    except Exception as e:
        logger.error(f"Error analyzing unknown data: {e}")
        return {
            "status": "error",
            "error": str(e)
        }


@router.get("/service/distribution")
async def get_service_distribution(
    limit: int = Query(20, ge=1, le=200, description="返回数量限制"),
    time_window: str = Query("24 HOUR", description="时间窗口（如 24 HOUR, 7 DAY）"),
) -> Dict[str, Any]:
    """
    获取服务分布统计

    返回各服务的日志数量、占比、趋势

    Args:
        limit: 返回数量限制

    Returns:
        {
            "services": [
                {
                    "service_name": "semantic-engine-worker",
                    "count": 1140162,
                    "percent": 69.1
                },
                ...
            ],
            "total_services": 15
        }
    """
    try:
        safe_limit = _sanitize_limit(limit, default_value=20, max_value=200)
        safe_time_window = _sanitize_interval(time_window, default_value="24 HOUR")

        if not storage or not storage.ch_client:
            return {
                "status": "error",
                "error": "Storage not available"
            }

        # 查询服务分布（单查询返回 total，避免额外一次全表扫描）
        query = f"""
            SELECT
                service_name,
                count() AS count,
                sum(count()) OVER () AS total
            FROM logs.logs
            PREWHERE timestamp > now() - INTERVAL {safe_time_window}
            GROUP BY service_name
            ORDER BY count DESC
            LIMIT {{limit:Int32}}
        """

        result = await _run_blocking(storage.execute_query, query, {"limit": safe_limit})

        services = []
        total = 0
        for row in result:
            service_name = _row_value(row, "service_name", 0)
            count = int(_row_value(row, "count", 1, 0) or 0)
            total = max(total, int(_row_value(row, "total", 2, 0) or 0))
            services.append({
                "service_name": service_name,
                "count": count,
                "percent": round((count / total) * 100, 2) if total > 0 else 0.0
            })

        return {
            "status": "ok",
            "services": services,
            "total_services": len(services),
            "total_records": total,
            "generated_at": datetime.now(timezone.utc).isoformat()
        }

    except Exception as e:
        logger.error(f"Error getting service distribution: {e}")
        return {
            "status": "error",
            "error": str(e)
        }


@router.post("/unknown/reprocess")
async def reprocess_unknown_data(
    time_range: str = Query("24 HOUR", description="时间范围"),
    dry_run: bool = Query(True, description="试运行模式")
) -> Dict[str, Any]:
    """
    重处理 unknown 数据

    尝试对 unknown 数据重新提取服务名

    Args:
        time_range: 时间范围（如 "24 HOUR", "7 DAY"）
        dry_run: 是否为试运行（不实际更新）

    Returns:
        {
            "status": "ok",
            "scanned": 87993,
            "would_fix": 5000,
            "fixed": 0
        }
    """
    try:
        if not storage or not storage.ch_client:
            return {
                "status": "error",
                "error": "Storage not available"
            }

        safe_time_range = _sanitize_interval(time_range, default_value="24 HOUR")

        # 查询需要重处理的 unknown 数据
        query = f"""
            SELECT
                id,
                substring(message, 1, 200) as message,
                pod_name,
                namespace,
                attributes_json
            FROM logs.logs
            PREWHERE service_name = 'unknown'
                 AND timestamp > now() - INTERVAL {safe_time_range}
            LIMIT 1000
        """

        result = await _run_blocking(storage.execute_query, query)

        scanned = len(result) if result else 0
        would_fix = 0

        # 在实际实现中，这里会：
        # 1. 对每条记录调用增强的服务名提取
        # 2. 如果提取到有效服务名，更新记录
        # 3. 统计修复数量

        return {
            "status": "ok",
            "dry_run": dry_run,
            "scanned": scanned,
            "would_fix": would_fix,
            "fixed": 0 if dry_run else would_fix,
            "message": "Dry run completed. Set dry_run=false to actually update records." if dry_run else f"Fixed {would_fix} records.",
            "note": "This is a simplified implementation. Full implementation requires service name re-extraction logic."
        }

    except Exception as e:
        logger.error(f"Error reprocessing unknown data: {e}")
        return {
            "status": "error",
            "error": str(e)
        }
