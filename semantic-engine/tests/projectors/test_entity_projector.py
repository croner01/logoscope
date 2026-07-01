import pytest
import json
from shared_src.event.envelope import EventEnvelope, serialize_envelope, deserialize_envelope
from shared_src.event.bus import InMemoryEventBus
from shared_src.event.schema_registry import SchemaRegistry
from semantic_engine.projectors.entity_projector import EntityProjector


# 模拟资源类型常量
INSTANCE = "INSTANCE"
SERVICE = "SERVICE"
HOST = "HOST"


class TestEntityProjector:
    def test_entity_projector_produces_entity_event(self):
        """EntityProjector 从 NormalizedEvent 提取 entity 并发布 platform.entity"""
        bus = InMemoryEventBus()
        schema_registry = SchemaRegistry()
        projector = EntityProjector(schema_registry, bus)

        norm_env = EventEnvelope(
            event_id="n1",
            event_type="normalized.event",
            payload=json.dumps({
                "id": "norm-001",
                "entity": {"type": "service", "name": "nova-api", "instance": "abc-123"},
                "event": {"type": "log", "level": "ERROR", "name": "Connection refused"},
                "context": {"host": "compute-01"},
            }).encode(),
        )

        projector.process(norm_env)

        # 验证 bus 收到 platform.entity 消息
        entity_events = bus._history.get("platform.entity", [])
        assert len(entity_events) >= 1
        assert entity_events[0].event_type == "entity.seen"

    def test_entity_projector_extracts_service(self):
        """从 normalized event 中提取 service entity"""
        bus = InMemoryEventBus()
        projector = EntityProjector(SchemaRegistry(), bus)

        norm_env = EventEnvelope(
            event_id="n2",
            event_type="normalized.event",
            payload=json.dumps({
                "entity": {"type": "service", "name": "neutron-server", "instance": "xyz-789"},
            }).encode(),
        )

        projector.process(norm_env)
        entity_events = bus._history.get("platform.entity", [])
        assert len(entity_events) >= 1
        payload = json.loads(entity_events[0].payload)
        assert payload["entity_type"] == SERVICE
        assert payload["entity_name"] == "neutron-server"
        assert payload["entity_instance"] == "xyz-789"

    def test_entity_projector_multiple_entities(self):
        """一个 normalized event 可以产生多个 entity"""
        bus = InMemoryEventBus()
        projector = EntityProjector(SchemaRegistry(), bus)

        norm_env = EventEnvelope(
            event_id="n3",
            event_type="normalized.event",
            payload=json.dumps({
                "entity": {"type": "service", "name": "nova-api", "instance": "abc"},
                "context": {"host": "compute-01"},
            }).encode(),
        )

        projector.process(norm_env)
        entity_events = bus._history.get("platform.entity", [])
        # 应该有 service entity + host entity
        entity_types = set()
        for ee in entity_events:
            p = json.loads(ee.payload)
            entity_types.add(p["entity_type"])
        assert SERVICE in entity_types
        assert HOST in entity_types
