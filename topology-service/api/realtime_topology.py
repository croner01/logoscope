"""
实时拓扑更新 API

提供 WebSocket 和 Server-Sent Events (SSE) 支持，实时推送拓扑变化

Date: 2026-02-09
"""

from fastapi import APIRouter, Query, HTTPException, WebSocket, WebSocketDisconnect
from typing import Dict, Any, List, Optional, Set, Tuple, Callable, Awaitable
import asyncio
import logging
import os
import sys
import time
from datetime import datetime, timezone
from collections import deque, OrderedDict

from graph.hybrid_topology import get_hybrid_topology_builder
from graph.enhanced_topology import get_enhanced_topology_builder
from storage.topology_snapshots import get_topology_snapshot_manager

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_SHARED_SRC_DIR = os.path.join(_PROJECT_ROOT, "shared_src")
for _path in (_PROJECT_ROOT, _SHARED_SRC_DIR):
    if os.path.isdir(_path) and _path not in sys.path:
        sys.path.append(_path)

try:
    from shared_src.monitoring import increment as metric_increment, gauge as metric_gauge
except ImportError:
    metric_increment = None
    metric_gauge = None

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/topology", tags=["topology"])

# 全局拓扑缓存（用于比较变化）
_last_topology = None
_topology_subscribers: Set[asyncio.Queue] = set()
_topology_cache: "OrderedDict[str, Tuple[Dict[str, Any], float]]" = OrderedDict()
_topology_inflight_tasks: Dict[str, asyncio.Task] = {}

# WebSocket 连接管理
_websocket_connections: Set[WebSocket] = set()

# 拓扑更新配置
UPDATE_INTERVAL = 60  # 默认 60 秒更新一次
MIN_CHANGE_THRESHOLD = 0.1  # 最小变化阈值（10%）
def _read_int_env(name: str, default_value: int) -> int:
    """读取整数环境变量，异常时回退默认值。"""
    raw = os.getenv(name, str(default_value))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default_value
    return max(value, 1)


TOPOLOGY_CACHE_TTL_SECONDS = _read_int_env("TOPOLOGY_CACHE_TTL_SECONDS", max(UPDATE_INTERVAL // 2, 5))
TOPOLOGY_STATS_CACHE_TTL_SECONDS = _read_int_env(
    "TOPOLOGY_STATS_CACHE_TTL_SECONDS",
    max(TOPOLOGY_CACHE_TTL_SECONDS, 60),
)
TOPOLOGY_CACHE_MAX_ENTRIES = _read_int_env("TOPOLOGY_CACHE_MAX_ENTRIES", 128)
TOPOLOGY_CACHE_METRICS_LOG_EVERY = _read_int_env("TOPOLOGY_CACHE_METRICS_LOG_EVERY", 200)
TOPOLOGY_SUBSCRIBER_QUEUE_MAXSIZE = _read_int_env("TOPOLOGY_SUBSCRIBER_QUEUE_MAXSIZE", 200)
TOPOLOGY_CACHE_PREFIX_HYBRID = "hybrid_topology:"
TOPOLOGY_CACHE_PREFIX_REALTIME_STATS = "realtime_stats:"


async def _run_blocking(func: Any, *args: Any, **kwargs: Any) -> Any:
    """Execute blocking work inline to avoid executor shutdown deadlocks."""
    return func(*args, **kwargs)


async def _build_topology_async(builder: Any, **kwargs) -> Dict[str, Any]:
    """Run blocking topology build inline."""
    return await _run_blocking(builder.build_topology, **kwargs)

_TOPOLOGY_CACHE_METRICS: Dict[str, int] = {
    "requests": 0,
    "hits": 0,
    "misses": 0,
    "writes": 0,
    "evictions_expired": 0,
    "evictions_capacity": 0,
    "manual_clears": 0,
    "storage_resets": 0,
    "topology_change_invalidations": 0,
    "last_log_request_count": 0,
}


def _emit_cache_counter(metric_name: str, value: int, tags: Dict[str, str]) -> None:
    """向统一监控模块上报计数器（可选能力）。"""
    if not metric_increment:
        return
    try:
        metric_increment(metric_name, value=value, tags=tags)
    except Exception as exc:
        logger.debug("cache metric increment failed: %s", exc)


def _emit_cache_gauge(metric_name: str, value: float, tags: Dict[str, str]) -> None:
    """向统一监控模块上报 gauge（可选能力）。"""
    if not metric_gauge:
        return
    try:
        metric_gauge(metric_name, value=value, tags=tags)
    except Exception as exc:
        logger.debug("cache metric gauge failed: %s", exc)


def _build_topology_cache_metrics_snapshot() -> Dict[str, Any]:
    """生成拓扑缓存指标快照。"""
    requests = int(_TOPOLOGY_CACHE_METRICS.get("requests", 0))
    hits = int(_TOPOLOGY_CACHE_METRICS.get("hits", 0))
    misses = int(_TOPOLOGY_CACHE_METRICS.get("misses", 0))
    hit_rate = round((hits / requests), 4) if requests > 0 else 0.0
    return {
        "requests": requests,
        "hits": hits,
        "misses": misses,
        "hit_rate": hit_rate,
        "writes": int(_TOPOLOGY_CACHE_METRICS.get("writes", 0)),
        "evictions_expired": int(_TOPOLOGY_CACHE_METRICS.get("evictions_expired", 0)),
        "evictions_capacity": int(_TOPOLOGY_CACHE_METRICS.get("evictions_capacity", 0)),
        "manual_clears": int(_TOPOLOGY_CACHE_METRICS.get("manual_clears", 0)),
        "storage_resets": int(_TOPOLOGY_CACHE_METRICS.get("storage_resets", 0)),
        "topology_change_invalidations": int(_TOPOLOGY_CACHE_METRICS.get("topology_change_invalidations", 0)),
    }


def _update_topology_cache_gauges() -> None:
    """更新拓扑缓存大小与命中率 gauge。"""
    snapshot = _build_topology_cache_metrics_snapshot()
    tags = {"service": "topology-service", "cache": "realtime_topology"}
    _emit_cache_gauge("cache.size", float(len(_topology_cache)), tags=tags)
    _emit_cache_gauge("cache.hit_rate", float(snapshot["hit_rate"]), tags=tags)


def _maybe_log_topology_cache_metrics() -> None:
    """按请求数节流打印缓存摘要，便于日志基线观测。"""
    requests = int(_TOPOLOGY_CACHE_METRICS.get("requests", 0))
    if requests <= 0:
        return
    if requests % TOPOLOGY_CACHE_METRICS_LOG_EVERY != 0:
        return
    if _TOPOLOGY_CACHE_METRICS.get("last_log_request_count") == requests:
        return
    _TOPOLOGY_CACHE_METRICS["last_log_request_count"] = requests
    summary = _build_topology_cache_metrics_snapshot()
    logger.info(
        "topology_cache metrics: requests=%s hits=%s misses=%s hit_rate=%.4f writes=%s evictions_expired=%s evictions_capacity=%s size=%s",
        summary["requests"],
        summary["hits"],
        summary["misses"],
        summary["hit_rate"],
        summary["writes"],
        summary["evictions_expired"],
        summary["evictions_capacity"],
        len(_topology_cache),
    )


def _record_topology_cache_request(hit: bool) -> None:
    """记录缓存请求命中/未命中。"""
    _TOPOLOGY_CACHE_METRICS["requests"] += 1
    result = "hit" if hit else "miss"
    if hit:
        _TOPOLOGY_CACHE_METRICS["hits"] += 1
    else:
        _TOPOLOGY_CACHE_METRICS["misses"] += 1
    _emit_cache_counter(
        "cache.requests_total",
        value=1,
        tags={"service": "topology-service", "cache": "realtime_topology", "result": result},
    )
    _update_topology_cache_gauges()
    _maybe_log_topology_cache_metrics()


def _record_topology_cache_write() -> None:
    """记录缓存写入。"""
    _TOPOLOGY_CACHE_METRICS["writes"] += 1
    _emit_cache_counter(
        "cache.writes_total",
        value=1,
        tags={"service": "topology-service", "cache": "realtime_topology"},
    )


def _record_topology_cache_eviction(reason: str, count: int) -> None:
    """记录缓存淘汰事件。"""
    if count <= 0:
        return
    metric_key = "evictions_expired" if reason == "expired" else "evictions_capacity"
    _TOPOLOGY_CACHE_METRICS[metric_key] += int(count)
    _emit_cache_counter(
        "cache.evictions_total",
        value=int(count),
        tags={"service": "topology-service", "cache": "realtime_topology", "reason": reason},
    )
    _update_topology_cache_gauges()


def _record_topology_cache_clear(reason: str, cleared: int) -> None:
    """记录缓存清理事件。"""
    if reason == "manual":
        _TOPOLOGY_CACHE_METRICS["manual_clears"] += 1
    elif reason == "storage_reset":
        _TOPOLOGY_CACHE_METRICS["storage_resets"] += 1
    elif reason == "topology_changed":
        _TOPOLOGY_CACHE_METRICS["topology_change_invalidations"] += 1
    _emit_cache_counter(
        "cache.clears_total",
        value=1,
        tags={"service": "topology-service", "cache": "realtime_topology", "reason": reason},
    )
    logger.info("topology_cache cleared: reason=%s cleared=%s", reason, cleared)
    _update_topology_cache_gauges()


def _reset_topology_cache_metrics() -> None:
    """重置缓存指标（仅用于测试隔离）。"""
    for key in _TOPOLOGY_CACHE_METRICS.keys():
        _TOPOLOGY_CACHE_METRICS[key] = 0


def _evict_expired_topology_cache(now_ts: Optional[float] = None) -> None:
    """清理过期拓扑缓存。"""
    current_ts = now_ts if now_ts is not None else time.time()
    expired_keys = [key for key, (_, expiry) in _topology_cache.items() if expiry <= current_ts]
    for key in expired_keys:
        _topology_cache.pop(key, None)
    _record_topology_cache_eviction("expired", len(expired_keys))


def _get_cached_topology(cache_key: str) -> Optional[Dict[str, Any]]:
    """读取拓扑缓存并刷新 LRU 顺序。"""
    current_ts = time.time()
    _evict_expired_topology_cache(current_ts)
    entry = _topology_cache.get(cache_key)
    if not entry:
        _record_topology_cache_request(hit=False)
        return None
    value, expiry_ts = entry
    if expiry_ts <= current_ts:
        _topology_cache.pop(cache_key, None)
        _record_topology_cache_eviction("expired", 1)
        _record_topology_cache_request(hit=False)
        return None
    _topology_cache.move_to_end(cache_key)
    _record_topology_cache_request(hit=True)
    return value


def _set_cached_topology(cache_key: str, topology: Dict[str, Any], ttl_seconds: int = TOPOLOGY_CACHE_TTL_SECONDS) -> None:
    """写入拓扑缓存并执行容量淘汰。"""
    expiry_ts = time.time() + max(int(ttl_seconds), 1)
    _topology_cache[cache_key] = (topology, expiry_ts)
    _topology_cache.move_to_end(cache_key)
    _record_topology_cache_write()
    _evict_expired_topology_cache()
    capacity_evicted = 0
    while len(_topology_cache) > TOPOLOGY_CACHE_MAX_ENTRIES:
        _topology_cache.popitem(last=False)
        capacity_evicted += 1
    _record_topology_cache_eviction("capacity", capacity_evicted)
    _update_topology_cache_gauges()


async def _coalesce_topology_build(
    cache_key: str,
    build_fn: Callable[[], Awaitable[Dict[str, Any]]],
) -> Dict[str, Any]:
    """
    合并同一 cache_key 的并发构建，避免缓存击穿导致重复重建拓扑。
    """
    existing_task = _topology_inflight_tasks.get(cache_key)
    if existing_task is not None:
        return await existing_task

    task = asyncio.create_task(build_fn())
    _topology_inflight_tasks[cache_key] = task
    try:
        return await task
    finally:
        current_task = _topology_inflight_tasks.get(cache_key)
        if current_task is task:
            _topology_inflight_tasks.pop(cache_key, None)


def _clear_topology_cache(prefix: Optional[str] = None, reason: str = "manual") -> int:
    """清理拓扑缓存，可按前缀精确失效。"""
    if not prefix:
        cleared = len(_topology_cache)
        _topology_cache.clear()
        _record_topology_cache_clear(reason=reason, cleared=cleared)
        return cleared
    keys_to_delete = [key for key in _topology_cache.keys() if key.startswith(prefix)]
    for key in keys_to_delete:
        _topology_cache.pop(key, None)
    cleared = len(keys_to_delete)
    _record_topology_cache_clear(reason=reason, cleared=cleared)
    return cleared


@router.get("/hybrid/realtime")
async def get_hybrid_topology(
    time_window: str = Query("1 HOUR", description="时间窗口（如 '1 HOUR', '15 MINUTE'）"),
    namespace: str = Query(None, description="命名空间过滤"),
    confidence_threshold: float = Query(0.3, description="置信度阈值（0.0-1.0）"),
    force_refresh: bool = Query(False, description="强制刷新缓存")
) -> Dict[str, Any]:
    """
    获取混合数据源的服务拓扑图

    结合 traces、logs、metrics 三个数据源生成更准确的拓扑

    参数:
        - time_window: 时间窗口，默认 "1 HOUR"
        - namespace: 命名空间过滤
        - confidence_threshold: 置信度阈值，低于此值的边将被过滤
        - force_refresh: 是否强制刷新缓存

    返回:
        {
            "nodes": [...],
            "edges": [...],
            "metadata": {
                "data_sources": ["traces", "logs", "metrics"],
                "time_window": "1 HOUR",
                "node_count": 10,
                "edge_count": 15,
                "avg_confidence": 0.75
            }
        }

    示例:
        GET /api/v1/topology/hybrid?time_window=15%20MINUTE&confidence_threshold=0.5
    """
    global _last_topology, _topology_cache

    try:
        # 如果不强制刷新且有缓存，返回缓存
        cache_key = f"{TOPOLOGY_CACHE_PREFIX_HYBRID}{time_window}:{namespace}:{confidence_threshold}"

        if not force_refresh:
            cached_topology = _get_cached_topology(cache_key)
            if cached_topology is not None:
                logger.debug("Returning cached topology: %s", cache_key)
                return cached_topology

        # 构建混合拓扑
        builder = get_hybrid_topology_builder(storage)

        if not builder:
            raise HTTPException(status_code=500, detail="Hybrid topology builder not initialized")

        topology = await _build_topology_async(
            builder,
            time_window=time_window,
            namespace=namespace,
            confidence_threshold=confidence_threshold,
        )

        # 检查是否有显著变化
        has_significant_change = _has_significant_changes(_last_topology, topology)

        if has_significant_change or force_refresh:
            _last_topology = topology
            # 仅失效 hybrid 拓扑缓存，避免无关接口（如 stats/realtime）被高频抖动误伤。
            _clear_topology_cache(prefix=TOPOLOGY_CACHE_PREFIX_HYBRID, reason="topology_changed")
            # 通知所有订阅者
            await _notify_subscribers(topology)

        # 缓存结果（TTL + 容量上限）
        _set_cached_topology(cache_key, topology)

        return topology

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting hybrid topology: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/changes")
async def get_topology_changes(
    since: str = Query(..., description="起始时间（ISO 8601格式）")
) -> Dict[str, Any]:
    """
    获取拓扑变化历史

    参数:
        - since: 起始时间，如 "2026-02-09T00:00:00Z"

    返回:
        {
            "from": "2026-02-09T00:00:00Z",
            "to": "2026-02-09T01:00:00Z",
            "changes": [
                {
                    "type": "node_added",
                    "timestamp": "2026-02-09T00:30:00Z",
                    "data": {"id": "new-service", "label": "New Service"}
                },
                {
                    "type": "edge_added",
                    "timestamp": "2026-02-09T00:30:00Z",
                    "data": {"source": "service-a", "target": "service-b"}
                }
            ]
        }
    """
    try:
        # 解析时间
        _ = datetime.fromisoformat(since.replace('Z', '+00:00'))
        to_iso = datetime.now(timezone.utc)

        # 查询变化（简化版本，实际应该持久化历史）
        # 这里返回当前拓扑作为"最新变化"
        builder = get_hybrid_topology_builder(storage)

        if not builder:
            raise HTTPException(status_code=500, detail="Hybrid topology builder not initialized")

        current_topology = await _build_topology_async(builder, time_window="1 HOUR")

        return {
            "from": since,
            "to": to_iso.isoformat(),
            "current_topology": current_topology,
            "note": "完整历史变化记录功能待实现"
        }

    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"invalid 'since' timestamp: {since}") from exc
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting topology changes: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/stats/realtime")
async def get_topology_stats(
    time_window: str = Query("1 HOUR", description="时间窗口"),
    force_refresh: bool = Query(False, description="强制刷新缓存"),
) -> Dict[str, Any]:
    """
    获取拓扑统计信息

    参数:
        - time_window: 时间窗口

    返回:
        {
            "total_nodes": 10,
            "total_edges": 15,
            "avg_confidence": 0.75,
            "data_sources": {
                "traces": {"nodes": 8, "edges": 12},
                "logs": {"nodes": 10, "edges": 5},
                "metrics": {"nodes": 10, "edges": 0}
            },
            "top_services": [
                {"service": "frontend", "log_count": 1000, "trace_count": 50}
            ]
        }
    """
    try:
        cache_key = f"{TOPOLOGY_CACHE_PREFIX_REALTIME_STATS}{time_window}"
        if not force_refresh:
            cached_stats = _get_cached_topology(cache_key)
            if cached_stats is not None:
                return cached_stats

        async def _build_stats_payload() -> Dict[str, Any]:
            builder = get_hybrid_topology_builder(storage)
            if not builder:
                raise HTTPException(status_code=500, detail="Hybrid topology builder not initialized")
            topology = await _build_topology_async(builder, time_window=time_window)

            nodes = topology.get("nodes", [])
            edges = topology.get("edges", [])
            metadata = topology.get("metadata", {})

            # 计算统计信息
            total_nodes = len(nodes)
            total_edges = len(edges)
            avg_confidence = metadata.get("avg_confidence", 0)

            # 提取源数据统计
            source_breakdown = metadata.get("source_breakdown", {})

            # 找出最活跃的服务（基于 log_count 或 trace_count）
            top_services = sorted(
                nodes,
                key=lambda n: (
                    n.get("metrics", {}).get("log_count", 0) +
                    n.get("metrics", {}).get("trace_count", 0) * 10  # traces 权重更高
                ),
                reverse=True
            )[:5]

            return {
                "total_nodes": total_nodes,
                "total_edges": total_edges,
                "avg_confidence": avg_confidence,
                "data_sources": source_breakdown,
                "top_services": [
                    {
                        "service": n["id"],
                        "log_count": n.get("metrics", {}).get("log_count", 0),
                        "trace_count": n.get("metrics", {}).get("trace_count", 0),
                        "error_count": n.get("metrics", {}).get("error_count", 0)
                    }
                    for n in top_services
                ],
                "generated_at": metadata.get("generated_at")
            }

        if force_refresh:
            result = await _build_stats_payload()
        else:
            result = await _coalesce_topology_build(cache_key, _build_stats_payload)

        _set_cached_topology(cache_key, result, ttl_seconds=TOPOLOGY_STATS_CACHE_TTL_SECONDS)
        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting topology stats: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/cache/stats")
async def get_topology_cache_stats() -> Dict[str, Any]:
    """查询拓扑接口缓存状态。"""
    now_ts = time.time()
    _evict_expired_topology_cache(now_ts)
    entries = [
        {
            "key": key,
            "expires_in_seconds": max(0, int(expiry - now_ts)),
        }
        for key, (_, expiry) in _topology_cache.items()
    ]
    return {
        "status": "ok",
        "size": len(_topology_cache),
        "ttl_seconds": TOPOLOGY_CACHE_TTL_SECONDS,
        "stats_ttl_seconds": TOPOLOGY_STATS_CACHE_TTL_SECONDS,
        "max_entries": TOPOLOGY_CACHE_MAX_ENTRIES,
        "metrics": _build_topology_cache_metrics_snapshot(),
        "entries": entries,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


@router.delete("/cache")
async def clear_topology_cache() -> Dict[str, Any]:
    """手动清空拓扑接口缓存。"""
    cleared = _clear_topology_cache(reason="manual")
    return {
        "status": "ok",
        "cleared": cleared,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def _has_significant_changes(
    old_topology: Optional[Dict[str, Any]],
    new_topology: Dict[str, Any]
) -> bool:
    """
    检查拓扑是否有显著变化

    变化判定标准:
    1. 节点数量变化超过 10%
    2. 边数量变化超过 10%
    3. 有新节点或新边出现
    4. 有节点或边消失

    Args:
        old_topology: 旧的拓扑数据
        new_topology: 新的拓扑数据

    Returns:
        bool: 是否有显著变化
    """
    if not old_topology:
        return True

    old_nodes = set(n["id"] for n in old_topology.get("nodes", []))
    new_nodes = set(n["id"] for n in new_topology.get("nodes", []))

    old_edges = set(
        (e["source"], e["target"])
        for e in old_topology.get("edges", [])
    )
    new_edges = set(
        (e["source"], e["target"])
        for e in new_topology.get("edges", [])
    )

    # 检查节点变化
    node_count_change = abs(len(new_nodes) - len(old_nodes))
    node_count_ratio = node_count_change / len(old_nodes) if old_nodes else 0

    # 检查边变化
    edge_count_change = abs(len(new_edges) - len(old_edges))
    edge_count_ratio = edge_count_change / len(old_edges) if old_edges else 0

    # 检查新增或删除
    added_nodes = new_nodes - old_nodes
    removed_nodes = old_nodes - new_nodes
    added_edges = new_edges - old_edges
    removed_edges = old_edges - new_edges

    has_changes = (
        node_count_ratio > MIN_CHANGE_THRESHOLD or
        edge_count_ratio > MIN_CHANGE_THRESHOLD or
        len(added_nodes) > 0 or
        len(removed_nodes) > 0 or
        len(added_edges) > 0 or
        len(removed_edges) > 0
    )

    if has_changes:
        logger.info(
            f"Topology changed: "
            f"nodes {len(old_nodes)} -> {len(new_nodes)}, "
            f"edges {len(old_edges)} -> {len(new_edges)}, "
            f"added_nodes={len(added_nodes)}, removed_nodes={len(removed_nodes)}, "
            f"added_edges={len(added_edges)}, removed_edges={len(removed_edges)}"
        )

    return has_changes


async def _notify_subscribers(topology: Dict[str, Any]):
    """
    通知所有订阅者拓扑已更新

    用于实时推送（WebSocket/SSE 实现）

    Args:
        topology: 更新后的拓扑数据
    """
    global _topology_subscribers

    if not _topology_subscribers:
        return

    payload = {
        "type": "topology_update",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "data": topology,
    }
    # 队列上限保护：优先丢弃最旧事件，避免慢消费者撑爆内存。
    for queue in list(_topology_subscribers):
        try:
            queue.put_nowait(payload)
        except asyncio.QueueFull:
            try:
                queue.get_nowait()
                queue.put_nowait(payload)
            except Exception:
                logger.warning("topology subscriber queue full, dropping update")
        except Exception as e:
            logger.error(f"Error notifying subscriber: {e}")


async def start_topology_update_task(
    interval_seconds: int = UPDATE_INTERVAL,
    enable_auto_snapshot: bool = True,
    snapshot_interval_hours: int = 1
):
    """
    启动后台拓扑更新任务

    定期检查拓扑变化并通知订阅者

    Args:
        interval_seconds: 更新间隔（秒）
        enable_auto_snapshot: 是否启用自动快照保存
        snapshot_interval_hours: 自动快照保存间隔（小时）
    """
    global _last_topology

    logger.info(f"Starting topology update task (interval: {interval_seconds}s)")
    if enable_auto_snapshot:
        logger.info(f"Auto-snapshot enabled (every {snapshot_interval_hours} hour(s))")

    last_snapshot_time = time.time()

    while True:
        try:
            await asyncio.sleep(interval_seconds)

            # 构建新拓扑
            builder = get_hybrid_topology_builder(storage)
            if not builder:
                logger.warning("Hybrid topology builder not initialized")
                continue

            try:
                new_topology = await _build_topology_async(builder, time_window="1 HOUR")
            except Exception as e:
                logger.exception(f"Error building topology: {e}")
                continue

            # 检查是否有变化
            if _has_significant_changes(_last_topology, new_topology):
                _last_topology = new_topology
                await _notify_subscribers(new_topology)
                logger.info("Topology updated and subscribers notified")

            # 自动保存快照（定期）
            if enable_auto_snapshot:
                current_time = time.time()
                snapshot_interval_seconds = snapshot_interval_hours * 3600

                if current_time - last_snapshot_time >= snapshot_interval_seconds:
                    try:
                        snapshot_mgr = _get_snapshot_manager()
                        snapshot_id = snapshot_mgr.save_snapshot(
                            topology=new_topology,
                            time_window="1 HOUR",
                            confidence_threshold=0.3
                        )
                        logger.info(f"Auto-saved topology snapshot: {snapshot_id}")
                        last_snapshot_time = current_time
                    except Exception as e:
                        logger.error(f"Failed to auto-save snapshot: {e}")

        except Exception as e:
            logger.error(f"Error in topology update task: {e}")
            # 继续运行，不中断任务


# 导入 storage（需要在模块加载时注入）
storage = None


def set_storage_adapter(storage_adapter):
    """设置 storage adapter"""
    global storage, _snapshot_manager
    storage = storage_adapter
    _snapshot_manager = None
    _topology_inflight_tasks.clear()
    _clear_topology_cache(reason="storage_reset")


# ==================== WebSocket 实时推送 ====================

@router.websocket("/subscribe")
async def websocket_subscribe_topology(websocket: WebSocket):
    """
    WebSocket 订阅拓扑实时更新

    客户端连接后，当拓扑发生显著变化时会自动收到推送

    消息格式:
        {
            "type": "topology_update",
            "timestamp": "2026-02-09T12:00:00Z",
            "data": {
                "nodes": [...],
                "edges": [...],
                "metadata": {...}
            }
        }

    客户端示例:
        ```javascript
        const ws = new WebSocket('ws://localhost:8080/api/v1/topology/subscribe');

        ws.onmessage = (event) => {
            const message = JSON.parse(event.data);
            if (message.type === 'topology_update') {
                console.log('Topology updated:', message.data);
                // 更新前端可视化
            }
        };

        ws.onerror = (error) => {
            console.error('WebSocket error:', error);
        };

        ws.onclose = () => {
            console.log('WebSocket connection closed');
        };
        ```

    Python 客户端示例:
        ```python
        import asyncio
        import websockets
        import json

        async def subscribe_topology():
            uri = "ws://localhost:8080/api/v1/topology/subscribe"
            async with websockets.connect(uri) as websocket:
                while True:
                    message = await websocket.recv()
                    data = json.loads(message)
                    if data['type'] == 'topology_update':
                        print(f"Topology updated: {data['timestamp']}")
        ```
    """
    await websocket.accept()
    logger.info("New WebSocket client connected")

    # 创建消息队列
    queue: asyncio.Queue = asyncio.Queue(maxsize=TOPOLOGY_SUBSCRIBER_QUEUE_MAXSIZE)
    _topology_subscribers.add(queue)
    _websocket_connections.add(websocket)

    try:
        # 发送初始拓扑数据
        try:
            builder = get_hybrid_topology_builder(storage)
            if builder:
                initial_topology = await _build_topology_async(builder, time_window="1 HOUR")
                await websocket.send_json({
                    "type": "topology_update",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "data": initial_topology,
                    "message": "Initial topology"
                })
                logger.info("Sent initial topology to new subscriber")
        except Exception as e:
            logger.error(f"Error sending initial topology: {e}")

        # 持续监听并发送更新
        while True:
            try:
                # 等待队列中的更新消息（带超时，用于心跳检测）
                message = await asyncio.wait_for(queue.get(), timeout=30.0)

                # 发送更新给客户端
                await websocket.send_json(message)
                logger.debug(f"Sent topology update to WebSocket client")

            except asyncio.TimeoutError:
                # 发送心跳保持连接
                await websocket.send_json({
                    "type": "heartbeat",
                    "timestamp": datetime.now(timezone.utc).isoformat()
                })

    except WebSocketDisconnect:
        logger.info("WebSocket client disconnected")
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
    finally:
        # 清理连接
        _topology_subscribers.discard(queue)
        _websocket_connections.discard(websocket)
        logger.info("WebSocket connection cleaned up")


async def broadcast_topology_update(topology: Dict[str, Any]):
    """
    广播拓扑更新到所有 WebSocket 连接

    Args:
        topology: 更新后的拓扑数据
    """
    global _websocket_connections
    if not _websocket_connections:
        return

    message = {
        "type": "topology_update",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "data": topology
    }

    # 记录需要移除的断开连接
    dead_connections = set()

    for websocket in list(_websocket_connections):
        try:
            await websocket.send_json(message)
        except Exception as e:
            logger.warning(f"Failed to send to WebSocket client: {e}")
            dead_connections.add(websocket)

    # 移除断开的连接
    _websocket_connections -= dead_connections

    if dead_connections:
        logger.info(f"Removed {len(dead_connections)} dead WebSocket connections")


# 覆盖原有的 _notify_subscribers 函数，添加 WebSocket 广播
async def _notify_subscribers(topology: Dict[str, Any]):
    """
    通知所有订阅者拓扑已更新

    支持两种订阅方式:
    1. Queue 订阅者（用于内部组件）
    2. WebSocket 连接（用于前端实时推送）

    Args:
        topology: 更新后的拓扑数据
    """
    global _topology_subscribers

    # 1. 通知 Queue 订阅者
    if _topology_subscribers:
        payload = {
            "type": "topology_update",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "data": topology,
        }
        for queue in list(_topology_subscribers):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                    queue.put_nowait(payload)
                except Exception:
                    logger.warning("topology subscriber queue full, dropping update")
            except Exception as e:
                logger.error(f"Error notifying queue subscriber: {e}")

    # 2. 广播到 WebSocket 连接
    await broadcast_topology_update(topology)


# ==================== 拓扑历史快照 API ====================

_snapshot_manager = None


def _get_snapshot_manager():
    """获取快照管理器实例"""
    global _snapshot_manager
    if _snapshot_manager is None:
        _snapshot_manager = get_topology_snapshot_manager(storage)
    return _snapshot_manager


@router.post("/snapshots")
async def create_topology_snapshot(
    time_window: str = Query("1 HOUR", description="时间窗口"),
    namespace: str = Query(None, description="命名空间"),
    confidence_threshold: float = Query(0.3, description="置信度阈值")
) -> Dict[str, Any]:
    """
    创建拓扑快照

    保存当前拓扑状态到数据库，用于历史记录和对比

    参数:
        - time_window: 时间窗口
        - namespace: 命名空间
        - confidence_threshold: 置信度阈值

    返回:
        {
            "snapshot_id": "snap_1234567890_abc12345",
            "timestamp": "2026-02-09T12:00:00Z",
            "node_count": 10,
            "edge_count": 15
        }
    """
    try:
        # 构建拓扑
        builder = get_hybrid_topology_builder(storage)
        if not builder:
            raise HTTPException(status_code=500, detail="Hybrid topology builder not initialized")

        topology = await _build_topology_async(
            builder,
            time_window=time_window,
            namespace=namespace,
            confidence_threshold=confidence_threshold,
        )

        # 保存快照
        snapshot_mgr = _get_snapshot_manager()
        snapshot_id = await _run_blocking(
            snapshot_mgr.save_snapshot,
            topology=topology,
            time_window=time_window,
            namespace=namespace,
            confidence_threshold=confidence_threshold,
        )

        return {
            "status": "success",
            "snapshot_id": snapshot_id,
            "timestamp": topology.get("metadata", {}).get("generated_at"),
            "node_count": len(topology.get("nodes", [])),
            "edge_count": len(topology.get("edges", []))
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating topology snapshot: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/snapshots")
async def list_topology_snapshots(
    from_time: str = Query(None, description="起始时间 (ISO 8601)"),
    to_time: str = Query(None, description="结束时间 (ISO 8601)"),
    namespace: str = Query(None, description="命名空间过滤"),
    limit: int = Query(100, description="返回数量限制")
) -> Dict[str, Any]:
    """
    列出拓扑快照

    参数:
        - from_time: 起始时间 (如 "2026-02-09T00:00:00Z")
        - to_time: 结束时间
        - namespace: 命名空间过滤
        - limit: 返回数量限制 (默认 100)

    返回:
        {
            "snapshots": [
                {
                    "snapshot_id": "snap_1234567890_abc12345",
                    "timestamp": "2026-02-09T12:00:00Z",
                    "node_count": 10,
                    "edge_count": 15
                }
            ],
            "count": 10
        }
    """
    try:
        snapshot_mgr = _get_snapshot_manager()

        # 解析时间参数
        from_dt = None
        to_dt = None

        if from_time:
            from_dt = datetime.fromisoformat(from_time.replace('Z', '+00:00'))

        if to_time:
            to_dt = datetime.fromisoformat(to_time.replace('Z', '+00:00'))

        # 查询快照列表
        snapshots = await _run_blocking(
            snapshot_mgr.list_snapshots,
            from_time=from_dt,
            to_time=to_dt,
            namespace=namespace,
            limit=limit,
        )

        return {
            "snapshots": snapshots,
            "count": len(snapshots)
        }

    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid from_time/to_time format, expected ISO 8601") from exc
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing topology snapshots: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/snapshots/{snapshot_id}")
async def get_topology_snapshot(snapshot_id: str) -> Dict[str, Any]:
    """
    获取单个拓扑快照详情

    参数:
        - snapshot_id: 快照 ID

    返回:
        {
            "snapshot_id": "snap_1234567890_abc12345",
            "timestamp": "2026-02-09T12:00:00Z",
            "topology": {
                "nodes": [...],
                "edges": [...],
                "metadata": {...}
            },
            "statistics": {...}
        }
    """
    try:
        snapshot_mgr = _get_snapshot_manager()
        snapshot = await _run_blocking(snapshot_mgr.get_snapshot, snapshot_id)

        if not snapshot:
            raise HTTPException(status_code=404, detail=f"Snapshot {snapshot_id} not found")

        return snapshot

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting topology snapshot {snapshot_id}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/snapshots/compare")
async def compare_topology_snapshots(
    snapshot_id_1: str = Query(..., description="快照 1 ID"),
    snapshot_id_2: str = Query(..., description="快照 2 ID")
) -> Dict[str, Any]:
    """
    对比两个拓扑快照

    参数:
        - snapshot_id_1: 快照 1 ID
        - snapshot_id_2: 快照 2 ID

    返回:
        {
            "snapshot_1": {...},
            "snapshot_2": {...},
            "changes": {
                "nodes": {
                    "added": ["service-c"],
                    "removed": ["service-old"],
                    "count": 2
                },
                "edges": {
                    "added": [["service-a", "service-c"]],
                    "removed": [["service-a", "service-old"]],
                    "count": 2
                },
                "statistics": {
                    "node_count_diff": 1,
                    "edge_count_diff": 0,
                    "confidence_diff": 0.05
                }
            }
        }
    """
    try:
        snapshot_mgr = _get_snapshot_manager()
        comparison = await _run_blocking(snapshot_mgr.compare_snapshots, snapshot_id_1, snapshot_id_2)

        if 'error' in comparison:
            raise HTTPException(status_code=404, detail=comparison['error'])

        return comparison

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error comparing topology snapshots: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.delete("/snapshots/cleanup")
async def cleanup_old_snapshots(
    retention_days: int = Query(30, description="保留天数")
) -> Dict[str, Any]:
    """
    清理旧快照

    删除指定天数之前的所有快照

    参数:
        - retention_days: 保留天数（默认 30 天）

    返回:
        {
            "status": "success",
            "deleted_count": 5,
            "retention_days": 30
        }
    """
    try:
        snapshot_mgr = _get_snapshot_manager()
        deleted_count = await _run_blocking(
            snapshot_mgr.delete_old_snapshots,
            retention_days=retention_days,
        )

        return {
            "status": "success",
            "deleted_count": deleted_count,
            "retention_days": retention_days
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error cleaning up old snapshots: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
