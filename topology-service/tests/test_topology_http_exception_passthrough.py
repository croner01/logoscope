"""
Topology API HTTPException passthrough regression tests.
"""
import asyncio
import os
import sys

import pytest
from fastapi import HTTPException

# 添加 topology-service 根目录到 Python 路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api import monitor_topology, realtime_topology, topology_adjustment


@pytest.fixture(autouse=True)
def _reset_module_state():
    topology_adjustment.set_storage_adapter(None)
    monitor_topology.set_storage_adapter(None)
    realtime_topology.set_storage_adapter(None)
    yield
    topology_adjustment.set_storage_adapter(None)
    monitor_topology.set_storage_adapter(None)
    realtime_topology.set_storage_adapter(None)


def test_topology_adjustment_add_node_preserves_http_exception(monkeypatch):
    """topology_adjustment 路由不应吞掉 HTTPException 细节。"""
    topology_adjustment.set_storage_adapter(object())
    monkeypatch.setattr(topology_adjustment, "get_enhanced_topology_builder", lambda _storage: None)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(topology_adjustment.add_manual_node(topology_adjustment.NodeRequest(node_id="svc-a")))
    assert exc.value.status_code == 500
    assert exc.value.detail == "Topology builder not initialized"


def test_monitor_search_preserves_http_exception():
    """monitor search 路由应保持 storage 初始化错误语义。"""
    monitor_topology.set_storage_adapter(None)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(monitor_topology.search_topology_nodes(query="frontend"))
    assert exc.value.status_code == 500
    assert exc.value.detail == "Storage adapter not initialized"


def test_realtime_create_snapshot_preserves_http_exception(monkeypatch):
    """realtime create snapshot 路由不应将 HTTPException 改写成通用 500。"""
    realtime_topology.set_storage_adapter(object())
    monkeypatch.setattr(realtime_topology, "get_hybrid_topology_builder", lambda _storage: None)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(realtime_topology.create_topology_snapshot())
    assert exc.value.status_code == 500
    assert exc.value.detail == "Hybrid topology builder not initialized"


def test_realtime_cleanup_snapshot_preserves_http_exception(monkeypatch):
    """realtime cleanup 路由应透传底层 HTTPException。"""

    class _FailingSnapshotManager:
        def delete_old_snapshots(self, retention_days: int):
            _ = retention_days
            raise HTTPException(status_code=409, detail="cleanup blocked")

    realtime_topology.set_storage_adapter(object())
    monkeypatch.setattr(realtime_topology, "_get_snapshot_manager", lambda: _FailingSnapshotManager())

    with pytest.raises(HTTPException) as exc:
        asyncio.run(realtime_topology.cleanup_old_snapshots(retention_days=1))
    assert exc.value.status_code == 409
    assert exc.value.detail == "cleanup blocked"
