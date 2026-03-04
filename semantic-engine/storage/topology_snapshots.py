"""Topology snapshot adapter (shared implementation)."""

import os
import sys
from typing import Optional

_SHARED_SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "shared_src"))
if os.path.isdir(_SHARED_SRC_DIR) and _SHARED_SRC_DIR not in sys.path:
    sys.path.append(_SHARED_SRC_DIR)

from shared_src.storage import topology_snapshots as _shared_snapshots

TopologySnapshotManager = _shared_snapshots.TopologySnapshotManager

_snapshot_manager: Optional[TopologySnapshotManager] = None


def get_topology_snapshot_manager(storage_adapter) -> TopologySnapshotManager:
    """Return cached snapshot manager instance for backward compatibility."""
    global _snapshot_manager
    if _snapshot_manager is None:
        _snapshot_manager = TopologySnapshotManager(storage_adapter)
    return _snapshot_manager


__all__ = [
    "TopologySnapshotManager",
    "_snapshot_manager",
    "get_topology_snapshot_manager",
]
