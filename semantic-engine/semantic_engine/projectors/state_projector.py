"""StateProjector — 从 NormalizedEvent 提取状态并发布到 platform.state。"""
import json
import uuid
from datetime import datetime
from typing import Dict, Optional, Any

from shared_src.event.envelope import EventEnvelope
from shared_src.event.bus import EventBus
from shared_src.event.schema_registry import SchemaRegistry


SEVERITY_MAP = {
    1: "DEBUG", 2: "DEBUG", 3: "DEBUG", 4: "DEBUG", 5: "INFO",
    6: "INFO", 7: "INFO", 8: "INFO", 9: "WARN", 10: "WARN",
    11: "WARN", 12: "WARN", 13: "WARN", 14: "WARN", 15: "WARN",
    16: "ERROR", 17: "ERROR", 18: "ERROR", 19: "ERROR", 20: "ERROR",
    21: "FATAL", 22: "FATAL", 23: "FATAL", 24: "FATAL",
}


class StateProjector:
    """
    State Projector——从 NormalizedEvent 提取状态并发布。

    - 从 severity_number 推断 ACTIVE/ERROR/WARN 状态
    - 发布 state.changed 事件到 platform.state
    """

    def __init__(self, schema_registry: SchemaRegistry, bus: EventBus):
        self.schema_registry = schema_registry
        self.bus = bus

    def process(self, envelope: EventEnvelope) -> None:
        payload = json.loads(envelope.payload.decode("utf-8"))
        entity = payload.get("entity", {})
        severity = payload.get("severity_number", 0)

        entity_type = (entity.get("type", "") or "").upper()
        entity_name = entity.get("name", "") or ""

        if not entity_type or not entity_name:
            return

        state = SEVERITY_MAP.get(severity, "UNKNOWN")

        state_payload = {
            "entity_type": entity_type,
            "entity_name": entity_name,
            "state": state,
            "severity_number": severity,
            "source_event_id": envelope.event_id,
        }

        state_env = EventEnvelope(
            schema_version=1,
            event_type="state.changed",
            producer="semantic-engine",
            event_id=uuid.uuid4().hex,
            parent_event_ids=[envelope.event_id],
            timestamp=datetime.utcnow(),
            payload=json.dumps(state_payload).encode("utf-8"),
            metadata={"source_type": "normalized.event"},
        )

        self.bus.publish("platform.state", state_env)
