"""
Realtime topology compatibility API for semantic-engine tests.
"""
import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Set, Tuple
import time

from fastapi import HTTPException

from graph.hybrid_topology import get_hybrid_topology_builder
from storage.topology_snapshots import TopologySnapshotManager

storage = None
_snapshot_manager: Optional[TopologySnapshotManager] = None
_topology_cache: Dict[Tuple[str, str, float], Tuple[Dict[str, Any], float]] = {}
_topology_subscribers: Set[Any] = set()
_CACHE_TTL_SECONDS = 30.0


def set_storage_adapter(storage_adapter: Any) -> None:
    """Set storage adapter and reset runtime caches."""
    global storage, _snapshot_manager
    storage = storage_adapter
    _snapshot_manager = None
    _topology_cache.clear()


def _get_snapshot_manager() -> TopologySnapshotManager:
    global _snapshot_manager
    if _snapshot_manager is None:
        if storage is None:
            raise HTTPException(status_code=503, detail="Storage adapter not initialized")
        _snapshot_manager = TopologySnapshotManager(storage)
    return _snapshot_manager


def _cache_key(time_window: str, namespace: Optional[str], confidence_threshold: float) -> Tuple[str, str, float]:
    return (str(time_window or "1 HOUR"), str(namespace or ""), float(confidence_threshold))


def _extract_node_ids(topology: Dict[str, Any]) -> set[str]:
    return {str((item or {}).get("id") or "") for item in (topology or {}).get("nodes", [])}


def _extract_edge_ids(topology: Dict[str, Any]) -> set[str]:
    edge_ids = set()
    for edge in (topology or {}).get("edges", []):
        source = str((edge or {}).get("source") or "")
        target = str((edge or {}).get("target") or "")
        edge_type = str((edge or {}).get("type") or "")
        edge_ids.add(f"{source}->{target}:{edge_type}")
    return edge_ids


def _has_significant_changes(old_topology: Optional[Dict[str, Any]], new_topology: Optional[Dict[str, Any]]) -> bool:
    """Detect significant topology changes based on node/edge identity sets."""
    if old_topology is None:
        return True
    if new_topology is None:
        return False
    return _extract_node_ids(old_topology) != _extract_node_ids(new_topology) or _extract_edge_ids(old_topology) != _extract_edge_ids(new_topology)


async def broadcast_topology_update(topology: Dict[str, Any]) -> None:
    """Broadcast hook for websocket integration (compatibility placeholder)."""
    return None


async def _notify_subscribers(topology: Dict[str, Any]) -> None:
    """Push topology updates to in-memory subscribers and websocket hook."""
    await broadcast_topology_update(topology)
    payload = {"type": "topology_update", "data": topology}
    stale = []
    for subscriber in list(_topology_subscribers):
        try:
            await subscriber.put(payload)
        except Exception:
            stale.append(subscriber)
    for subscriber in stale:
        _topology_subscribers.discard(subscriber)


async def get_hybrid_topology(
    time_window: str = "1 HOUR",
    namespace: Optional[str] = None,
    confidence_threshold: float = 0.3,
    force_refresh: bool = False,
) -> Dict[str, Any]:
    """Build and optionally cache topology."""
    builder = get_hybrid_topology_builder(storage)
    if builder is None:
        raise HTTPException(status_code=500, detail="Hybrid topology builder unavailable")

    key = _cache_key(time_window, namespace, confidence_threshold)
    cached_entry = _topology_cache.get(key)
    now = time.time()
    if not force_refresh and cached_entry and cached_entry[1] > now:
        return cached_entry[0]

    previous = cached_entry[0] if cached_entry else None
    topology = builder.build_topology(
        time_window=time_window,
        namespace=namespace,
        confidence_threshold=confidence_threshold,
    )
    if not isinstance(topology, dict):
        topology = {"nodes": [], "edges": [], "metadata": {}}

    _topology_cache[key] = (topology, now + _CACHE_TTL_SECONDS)
    if _has_significant_changes(previous, topology):
        await _notify_subscribers(topology)
    return topology


async def get_topology_changes(since: str) -> Dict[str, Any]:
    """Return coarse-grained topology changes since timestamp."""
    builder = get_hybrid_topology_builder(storage)
    if builder is None:
        raise HTTPException(status_code=500, detail="Hybrid topology builder unavailable")
    # Let invalid values raise ValueError for test expectations.
    datetime.fromisoformat(str(since).replace("Z", "+00:00"))
    current = builder.build_topology(time_window="1 HOUR", namespace=None, confidence_threshold=0.3)
    return {
        "from": since,
        "to": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "current_topology": current,
    }


async def get_topology_stats(
    time_window: str = "1 HOUR",
    namespace: Optional[str] = None,
    confidence_threshold: float = 0.3,
) -> Dict[str, Any]:
    """Return topology summary statistics."""
    topology = await get_hybrid_topology(
        time_window=time_window,
        namespace=namespace,
        confidence_threshold=confidence_threshold,
        force_refresh=True,
    )
    nodes = topology.get("nodes", [])
    edges = topology.get("edges", [])
    metadata = topology.get("metadata", {})

    ranked = []
    for node in nodes:
        metrics = (node or {}).get("metrics", {})
        score = float(metrics.get("log_count", 0) or 0) + float(metrics.get("trace_count", 0) or 0)
        ranked.append({"service": (node or {}).get("id"), "score": score})
    ranked.sort(key=lambda item: item["score"], reverse=True)

    return {
        "total_nodes": len(nodes),
        "total_edges": len(edges),
        "avg_confidence": float(metadata.get("avg_confidence", 0.0) or 0.0),
        "data_sources": list(metadata.get("data_sources", [])),
        "top_services": ranked[:10],
    }


async def create_topology_snapshot(
    time_window: str = "1 HOUR",
    namespace: Optional[str] = None,
    confidence_threshold: float = 0.3,
) -> Dict[str, Any]:
    """Create and persist a topology snapshot."""
    builder = get_hybrid_topology_builder(storage)
    if builder is None:
        raise HTTPException(status_code=500, detail="Hybrid topology builder unavailable")
    topology = builder.build_topology(
        time_window=time_window,
        namespace=namespace,
        confidence_threshold=confidence_threshold,
    )
    manager = _get_snapshot_manager()
    snapshot_id = manager.save_snapshot(
        topology=topology,
        time_window=time_window,
        namespace=namespace,
        confidence_threshold=confidence_threshold,
    )
    return {
        "status": "success",
        "snapshot_id": snapshot_id,
        "node_count": len((topology or {}).get("nodes", [])),
        "edge_count": len((topology or {}).get("edges", [])),
    }


async def list_topology_snapshots(
    from_time: Optional[str] = None,
    to_time: Optional[str] = None,
    namespace: Optional[str] = None,
    limit: int = 100,
) -> Dict[str, Any]:
    """List saved topology snapshots."""
    manager = _get_snapshot_manager()
    parsed_from = datetime.fromisoformat(str(from_time).replace("Z", "+00:00")) if from_time else None
    parsed_to = datetime.fromisoformat(str(to_time).replace("Z", "+00:00")) if to_time else None
    snapshots = manager.list_snapshots(
        from_time=parsed_from,
        to_time=parsed_to,
        namespace=namespace,
        limit=max(1, int(limit)),
    )
    return {
        "count": len(snapshots),
        "snapshots": snapshots,
    }


async def get_topology_snapshot(snapshot_id: str) -> Dict[str, Any]:
    """Load one snapshot by id."""
    manager = _get_snapshot_manager()
    snapshot = manager.get_snapshot(snapshot_id)
    if not snapshot:
        raise HTTPException(status_code=404, detail="Snapshot not found")
    return snapshot


async def compare_topology_snapshots(snapshot_id_1: str, snapshot_id_2: str) -> Dict[str, Any]:
    """Compare two snapshots."""
    manager = _get_snapshot_manager()
    result = manager.compare_snapshots(snapshot_id_1, snapshot_id_2)
    if isinstance(result, dict) and result.get("error"):
        raise HTTPException(status_code=404, detail=str(result.get("error")))
    return result


async def cleanup_old_snapshots(retention_days: int = 30) -> Dict[str, Any]:
    """Delete old snapshots by retention window."""
    manager = _get_snapshot_manager()
    deleted = manager.delete_old_snapshots(retention_days=max(1, int(retention_days)))
    return {
        "status": "success",
        "deleted_count": int(deleted or 0),
        "retention_days": max(1, int(retention_days)),
    }


async def start_topology_update_task(
    interval_seconds: int = 60,
    enable_auto_snapshot: bool = False,
    snapshot_interval_hours: int = 24,
) -> None:
    """Background task that refreshes topology and optionally persists snapshots."""
    last_snapshot_ts = 0.0
    while True:
        try:
            topology = await get_hybrid_topology(force_refresh=True)
            if enable_auto_snapshot:
                now = time.time()
                interval = max(0, int(snapshot_interval_hours)) * 3600
                if interval == 0 or (now - last_snapshot_ts) >= interval:
                    manager = _get_snapshot_manager()
                    manager.save_snapshot(topology=topology)
                    last_snapshot_ts = now
            await asyncio.sleep(max(0, interval_seconds))
        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.sleep(max(0.05, interval_seconds if interval_seconds > 0 else 0.05))
