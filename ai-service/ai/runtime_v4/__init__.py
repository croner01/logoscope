"""AI runtime v4 package (Temporal outer + LangGraph inner)."""

from __future__ import annotations

from typing import Optional

from ai.runtime_v4.adapter.orchestration_bridge import RuntimeV4OrchestrationBridge
from ai.runtime_v4.store import get_runtime_v4_thread_store
from ai.runtime_v4.temporal.client import get_temporal_outer_client


_runtime_v4_bridge: Optional[RuntimeV4OrchestrationBridge] = None


def get_runtime_v4_bridge() -> RuntimeV4OrchestrationBridge:
    global _runtime_v4_bridge
    if _runtime_v4_bridge is None:
        _runtime_v4_bridge = RuntimeV4OrchestrationBridge(
            temporal_client=get_temporal_outer_client(),
            thread_store=get_runtime_v4_thread_store(),
        )
    return _runtime_v4_bridge


def reset_runtime_v4_bridge() -> None:
    global _runtime_v4_bridge
    _runtime_v4_bridge = None


__all__ = ["get_runtime_v4_bridge", "reset_runtime_v4_bridge"]
