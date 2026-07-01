"""InventoryProjection — 实体清单投影。"""
import json
from typing import Dict, List, Optional, Any
from shared_src.event.envelope import EventEnvelope


class InventoryProjection:
    """
    实体清单投影——维护所有已知实体及其最新状态。

    - query(entity_type, entity_name) -> dict | None
    - rebuild(events) — 清除后重新加载
    - total_count() — 实体总数
    """

    def __init__(self, epoch: str = ""):
        self.epoch = epoch
        self._inventory: Dict[str, dict] = {}
        self._applied_count = 0

    def apply(self, envelope: EventEnvelope) -> None:
        """应用一个 entity.seen 事件。"""
        if envelope.event_type != "entity.seen":
            return

        payload = json.loads(envelope.payload.decode("utf-8"))
        entity_type = payload.get("entity_type", "")
        entity_name = payload.get("entity_name", "")
        entity_instance = payload.get("entity_instance", "")

        key = f"{entity_type}:{entity_name}"
        self._inventory[key] = {
            "entity_type": entity_type,
            "entity_name": entity_name,
            "entity_instance": entity_instance,
            "last_seen": envelope.timestamp.isoformat() if hasattr(envelope.timestamp, "isoformat") else str(envelope.timestamp),
            "source_event_id": payload.get("source_event_id", envelope.event_id),
        }
        self._applied_count += 1

    def query(self, entity_type: str, entity_name: str) -> Optional[dict]:
        """查询实体信息。"""
        key = f"{entity_type}:{entity_name}"
        return self._inventory.get(key)

    def rebuild(self, events: List[EventEnvelope]) -> None:
        """清除后重新加载事件流。"""
        self._inventory.clear()
        self._applied_count = 0
        for event in events:
            self.apply(event)

    def total_count(self) -> int:
        return len(self._inventory)

    def status(self) -> dict:
        return {
            "entity_count": self.total_count(),
            "epoch": self.epoch,
            "events_applied": self._applied_count,
        }
