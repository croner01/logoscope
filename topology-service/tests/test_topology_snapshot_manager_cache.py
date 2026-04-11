"""
Tests for topology snapshot manager cache behavior.
"""
import os
import sys

# 添加 topology-service 根目录到 Python 路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from storage import topology_snapshots


def test_snapshot_manager_recreated_when_storage_changes(monkeypatch):
    created_with = []

    class _FakeSnapshotManager:
        def __init__(self, storage_adapter):
            created_with.append(storage_adapter)
            self.storage_adapter = storage_adapter

    monkeypatch.setattr(topology_snapshots, "TopologySnapshotManager", _FakeSnapshotManager)
    topology_snapshots._snapshot_manager = None
    topology_snapshots._snapshot_manager_storage = None

    storage_a = object()
    storage_b = object()

    manager_a = topology_snapshots.get_topology_snapshot_manager(storage_a)
    manager_a_repeat = topology_snapshots.get_topology_snapshot_manager(storage_a)
    manager_b = topology_snapshots.get_topology_snapshot_manager(storage_b)

    assert manager_a is manager_a_repeat
    assert manager_b is not manager_a
    assert created_with == [storage_a, storage_b]
