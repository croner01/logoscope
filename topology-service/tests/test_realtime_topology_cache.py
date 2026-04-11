"""
realtime_topology 缓存策略测试
"""
import asyncio
import os
import sys
import time
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

# 添加 topology-service 根目录到 Python 路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api import realtime_topology


class _FakeBuilder:
    def __init__(self):
        self.calls = 0

    def build_topology(self, **kwargs):
        self.calls += 1
        return {
            "nodes": [{"id": "frontend"}],
            "edges": [],
            "metadata": {
                "generated_at": f"2026-03-01T00:00:0{self.calls}Z",
                "avg_confidence": 0.8,
                "source_breakdown": {},
            },
        }


def _reset_cache_state():
    realtime_topology._last_topology = None
    realtime_topology._topology_cache.clear()
    realtime_topology._topology_inflight_tasks.clear()
    realtime_topology._topology_subscribers.clear()
    realtime_topology._reset_topology_cache_metrics()
    realtime_topology.storage = object()


def test_hybrid_realtime_cache_hit(monkeypatch):
    """相同参数二次调用应命中缓存。"""
    _reset_cache_state()
    fake_builder = _FakeBuilder()
    monkeypatch.setattr(realtime_topology, "get_hybrid_topology_builder", lambda _: fake_builder)

    asyncio.run(
        realtime_topology.get_hybrid_topology(
            time_window="1 HOUR",
            namespace=None,
            confidence_threshold=0.3,
            force_refresh=False,
        )
    )
    asyncio.run(
        realtime_topology.get_hybrid_topology(
            time_window="1 HOUR",
            namespace=None,
            confidence_threshold=0.3,
            force_refresh=False,
        )
    )

    assert fake_builder.calls == 1
    metrics = realtime_topology._build_topology_cache_metrics_snapshot()
    assert metrics["requests"] >= 2
    assert metrics["hits"] >= 1
    assert metrics["misses"] >= 1


def test_hybrid_realtime_force_refresh_bypass_cache(monkeypatch):
    """force_refresh=true 应绕过缓存并重新构建。"""
    _reset_cache_state()
    fake_builder = _FakeBuilder()
    monkeypatch.setattr(realtime_topology, "get_hybrid_topology_builder", lambda _: fake_builder)

    asyncio.run(
        realtime_topology.get_hybrid_topology(
            time_window="1 HOUR",
            namespace=None,
            confidence_threshold=0.3,
            force_refresh=False,
        )
    )
    asyncio.run(
        realtime_topology.get_hybrid_topology(
            time_window="1 HOUR",
            namespace=None,
            confidence_threshold=0.3,
            force_refresh=True,
        )
    )

    assert fake_builder.calls == 2


def test_topology_cache_lru_evict_oldest():
    """超过容量后应淘汰最旧缓存项。"""
    _reset_cache_state()
    old_max_entries = realtime_topology.TOPOLOGY_CACHE_MAX_ENTRIES
    try:
        realtime_topology.TOPOLOGY_CACHE_MAX_ENTRIES = 2
        realtime_topology._set_cached_topology("k1", {"value": 1}, ttl_seconds=30)
        realtime_topology._set_cached_topology("k2", {"value": 2}, ttl_seconds=30)
        realtime_topology._set_cached_topology("k3", {"value": 3}, ttl_seconds=30)
        assert len(realtime_topology._topology_cache) == 2
        assert "k1" not in realtime_topology._topology_cache
        assert "k2" in realtime_topology._topology_cache
        assert "k3" in realtime_topology._topology_cache
        metrics = realtime_topology._build_topology_cache_metrics_snapshot()
        assert metrics["writes"] == 3
        assert metrics["evictions_capacity"] == 1
    finally:
        realtime_topology.TOPOLOGY_CACHE_MAX_ENTRIES = old_max_entries


def test_topology_cache_stats_exposes_metrics():
    """拓扑缓存状态接口应返回统一缓存指标。"""
    _reset_cache_state()
    realtime_topology._set_cached_topology("k1", {"value": 1}, ttl_seconds=30)
    assert realtime_topology._get_cached_topology("k1") == {"value": 1}
    assert realtime_topology._get_cached_topology("missing") is None

    stats = asyncio.run(realtime_topology.get_topology_cache_stats())
    metrics = stats.get("metrics", {})

    assert stats["status"] == "ok"
    assert metrics["requests"] >= 2
    assert metrics["hits"] >= 1
    assert metrics["misses"] >= 1
    assert "evictions_expired" in metrics
    assert "evictions_capacity" in metrics
    assert "manual_clears" in metrics
    assert "storage_resets" in metrics


def test_realtime_stats_concurrent_requests_coalesced(monkeypatch):
    """并发请求 stats/realtime 缓存未命中时应合并构建。"""
    _reset_cache_state()
    fake_builder = _FakeBuilder()
    monkeypatch.setattr(realtime_topology, "get_hybrid_topology_builder", lambda _: fake_builder)

    async def _run():
        return await asyncio.gather(
            realtime_topology.get_topology_stats(time_window="1 HOUR", force_refresh=False),
            realtime_topology.get_topology_stats(time_window="1 HOUR", force_refresh=False),
        )

    results = asyncio.run(_run())
    assert fake_builder.calls == 1
    assert results[0]["total_nodes"] == 1
    assert results[1]["total_nodes"] == 1


def test_hybrid_invalidation_keeps_realtime_stats_cache(monkeypatch):
    """hybrid 拓扑变化失效不应清空 realtime_stats 缓存。"""
    _reset_cache_state()
    fake_builder = _FakeBuilder()
    monkeypatch.setattr(realtime_topology, "get_hybrid_topology_builder", lambda _: fake_builder)

    stats_cache_key = f"{realtime_topology.TOPOLOGY_CACHE_PREFIX_REALTIME_STATS}1 HOUR"
    hybrid_cache_key = f"{realtime_topology.TOPOLOGY_CACHE_PREFIX_HYBRID}1 HOUR:None:0.3"
    realtime_topology._set_cached_topology(stats_cache_key, {"total_nodes": 9}, ttl_seconds=30)
    realtime_topology._set_cached_topology(hybrid_cache_key, {"nodes": [], "edges": [], "metadata": {}}, ttl_seconds=30)

    asyncio.run(
        realtime_topology.get_hybrid_topology(
            time_window="1 HOUR",
            namespace=None,
            confidence_threshold=0.3,
            force_refresh=True,
        )
    )

    assert realtime_topology._get_cached_topology(stats_cache_key) == {"total_nodes": 9}
    assert realtime_topology._get_cached_topology(hybrid_cache_key) is not None


def test_get_topology_changes_to_timestamp_is_valid_iso8601(monkeypatch):
    """changes 接口返回的 to 字段应是可解析的 ISO8601 UTC 时间。"""
    _reset_cache_state()
    fake_builder = _FakeBuilder()
    monkeypatch.setattr(realtime_topology, "get_hybrid_topology_builder", lambda _: fake_builder)

    result = asyncio.run(
        realtime_topology.get_topology_changes(since="2026-03-01T00:00:00Z")
    )

    to_value = result.get("to")
    assert isinstance(to_value, str)
    assert not to_value.endswith("+00:00Z")
    parsed = datetime.fromisoformat(to_value.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None


def test_get_topology_changes_invalid_since_returns_400():
    """changes 接口对非法 since 参数应返回 400。"""
    _reset_cache_state()
    with pytest.raises(HTTPException) as exc:
        asyncio.run(realtime_topology.get_topology_changes(since="not-a-time"))
    assert exc.value.status_code == 400
    assert "invalid 'since' timestamp" in str(exc.value.detail)


def test_broadcast_topology_update_tolerates_connection_set_mutation():
    """广播过程中连接集合变化不应触发 RuntimeError。"""
    _reset_cache_state()

    class _MutatingWebSocket:
        def __init__(self, mutate_once: bool = False):
            self.messages = []
            self._mutate_once = mutate_once

        async def send_json(self, payload):
            self.messages.append(payload)
            if self._mutate_once:
                self._mutate_once = False
                realtime_topology._websocket_connections.add(_MutatingWebSocket())

    ws = _MutatingWebSocket(mutate_once=True)
    realtime_topology._websocket_connections.add(ws)

    asyncio.run(realtime_topology.broadcast_topology_update({"nodes": [], "edges": []}))

    assert len(ws.messages) == 1
    sent = ws.messages[0]
    assert sent.get("type") == "topology_update"
    assert sent.get("data") == {"nodes": [], "edges": []}


def test_set_storage_adapter_resets_snapshot_manager_and_cache():
    """切换 storage 时应重置快照管理器并清空缓存。"""
    _reset_cache_state()
    realtime_topology._snapshot_manager = object()
    realtime_topology._topology_cache["k1"] = ({"v": 1}, time.time() + 60)
    realtime_topology._topology_inflight_tasks["k1"] = object()

    realtime_topology.set_storage_adapter(object())

    assert realtime_topology._snapshot_manager is None
    assert len(realtime_topology._topology_cache) == 0
    assert len(realtime_topology._topology_inflight_tasks) == 0


def test_list_topology_snapshots_invalid_time_returns_400(monkeypatch):
    """快照列表接口对非法时间参数应返回 400。"""
    _reset_cache_state()

    class _FakeSnapshotManager:
        def list_snapshots(self, **kwargs):
            _ = kwargs
            return []

    monkeypatch.setattr(realtime_topology, "_get_snapshot_manager", lambda: _FakeSnapshotManager())

    with pytest.raises(HTTPException) as exc:
        asyncio.run(realtime_topology.list_topology_snapshots(from_time="invalid-time"))
    assert exc.value.status_code == 400
