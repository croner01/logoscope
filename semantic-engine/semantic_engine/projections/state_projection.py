"""StateProjection — 当前状态投影。"""
import json
from typing import Dict, List, Optional, Any
from datetime import datetime, timedelta
from shared_src.event.envelope import EventEnvelope


class StateProjection:
    """
    当前状态投影——维护实体的最新状态。

    - query(entity_type, entity_name) -> str | None（当前状态）
    - rebuild(events) — 清除后重新加载
    """

    def __init__(self):
        self._states: Dict[str, str] = {}
        self._timestamps: Dict[str, datetime] = {}

    def apply(self, envelope: EventEnvelope) -> None:
        if envelope.event_type != "state.changed":
            return
        payload = json.loads(envelope.payload.decode("utf-8"))
        entity_type = payload.get("entity_type", "")
        entity_name = payload.get("entity_name", "")
        state = payload.get("state", "UNKNOWN")

        key = f"{entity_type}:{entity_name}"
        self._states[key] = state
        self._timestamps[key] = datetime.utcnow()

    def query(self, entity_type: str, entity_name: str) -> Optional[str]:
        """查询实体当前状态。"""
        key = f"{entity_type}:{entity_name}"
        return self._states.get(key)

    def rebuild(self, events: List[EventEnvelope]) -> None:
        """清除后重新加载事件流。"""
        self._states.clear()
        self._timestamps.clear()
        for event in events:
            self.apply(event)

    def status(self) -> dict:
        return {
            "entity_count": len(self._states),
            "keys": list(self._states.keys()),
        }

    def get_all_states(self) -> Dict[str, str]:
        """返回所有实体的状态快照。"""
        return dict(self._states)
