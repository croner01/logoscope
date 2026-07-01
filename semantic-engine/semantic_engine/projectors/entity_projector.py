"""EntityProjector — 从 NormalizedEvent 提取实体并发布到 platform.entity。"""
import json
import uuid
from datetime import datetime
from typing import Optional, Dict, Any

from shared_src.event.envelope import EventEnvelope
from shared_src.event.bus import EventBus
from shared_src.event.schema_registry import SchemaRegistry


INSTANCE = "INSTANCE"
SERVICE = "SERVICE"
HOST = "HOST"
CLUSTER = "CLUSTER"


class EntityProjector:
    """
    Entity Projector——从 NormalizedEvent 提取实体信息。

    - 从 event.entity 提取 service/instance 实体
    - 从 event.context.host 提取 host 实体
    - 发布 entity.seen 事件到 platform.entity topic
    """

    def __init__(self, schema_registry: SchemaRegistry, bus: EventBus):
        self.schema_registry = schema_registry
        self.bus = bus

    def process(self, envelope: EventEnvelope) -> None:
        """处理一个 NormalizedEvent Envelope，提取实体并发布。"""
        payload = json.loads(envelope.payload.decode("utf-8"))
        entity = payload.get("entity", {})
        context = payload.get("context", {})

        # 1. 提取 service/instance 实体
        entity_type = entity.get("type", "").upper() if entity.get("type") else ""
        if entity_type == SERVICE:
            self._emit_entity(SERVICE, entity.get("name", ""), entity.get("instance", ""), envelope)
        elif entity_type == INSTANCE:
            self._emit_entity(INSTANCE, entity.get("name", ""), entity.get("instance", ""), envelope)

        # 如果 entity.type 为空但从 event 字段可以推断，也发射
        event_data = payload.get("event", {})
        service_name = event_data.get("name", "").split(":")[0] if event_data.get("name") else ""
        if service_name and not entity_type:
            self._emit_entity(SERVICE, service_name, "", envelope)

        # 2. 提取 host 实体
        host = context.get("host", "")
        if host:
            self._emit_entity(HOST, host, "", envelope)

    def _emit_entity(self, entity_type: str, entity_name: str,
                     entity_instance: str, source: EventEnvelope) -> None:
        """构造并发布 entity.seen 事件。"""
        entity_payload = {
            "entity_type": entity_type,
            "entity_name": entity_name,
            "entity_instance": entity_instance,
            "source_event_id": source.event_id,
        }

        entity_env = EventEnvelope(
            schema_version=1,
            event_type="entity.seen",
            producer="semantic-engine",
            event_id=uuid.uuid4().hex,
            parent_event_ids=[source.event_id],
            timestamp=datetime.utcnow(),
            payload=json.dumps(entity_payload).encode("utf-8"),
            metadata={"source_type": "normalized.event"},
        )

        self.bus.publish("platform.entity", entity_env)
