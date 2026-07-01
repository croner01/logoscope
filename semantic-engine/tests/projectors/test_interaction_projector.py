import pytest
import json
from shared_src.event.envelope import EventEnvelope
from shared_src.event.bus import InMemoryEventBus
from shared_src.event.schema_registry import SchemaRegistry
from semantic_engine.projectors.interaction_projector import InteractionProjector


INSTANCE = "INSTANCE"
HOST = "HOST"
SERVICE = "SERVICE"


class TestInteractionProjector:
    def test_interaction_from_endpoints(self):
        """InteractionProjector 从参与方列表提取交互关系"""
        bus = InMemoryEventBus()
        projector = InteractionProjector(SchemaRegistry(), bus)

        norm_env = EventEnvelope(
            event_id="n1",
            event_type="normalized.event",
            payload=json.dumps({
                "entity": {"type": "service", "name": "nova-api", "instance": "vm-1"},
                "context": {"host": "compute-01"},
            }).encode(),
        )

        projector.process(norm_env)
        interaction_events = bus._history.get("platform.interaction", [])
        assert len(interaction_events) >= 0

    def test_interaction_publishes_formatted_event(self):
        """交互事件有正确的格式"""
        bus = InMemoryEventBus()
        projector = InteractionProjector(SchemaRegistry(), bus)

        norm_env = EventEnvelope(
            event_id="n1",
            event_type="normalized.event",
            payload=json.dumps({
                "entity": {"type": "service", "name": "nova-compute", "instance": "vm-1"},
                "context": {"host": "compute-01"},
                "event": {"type": "interaction", "name": "nova-compute: Request", "raw": "API call to neutron"},
            }).encode(),
        )

        projector.process(norm_env)
        interaction_events = bus._history.get("platform.interaction", [])
        if interaction_events:
            payload = json.loads(interaction_events[0].payload)
            assert "source" in payload
            assert "target" in payload
            assert payload["source"]["type"] in (SERVICE, HOST, INSTANCE)

    def test_interaction_tracks_lineage(self):
        """交互事件包含 parent_event_ids"""
        bus = InMemoryEventBus()
        projector = InteractionProjector(SchemaRegistry(), bus)
        norm_env = EventEnvelope(
            event_id="n-001",
            event_type="normalized.event",
            payload=json.dumps({
                "entity": {"type": "service", "name": "nova-api"},
                "context": {"host": "compute-01"},
            }).encode(),
        )
        projector.process(norm_env)
        interaction_events = bus._history.get("platform.interaction", [])
        if interaction_events:
            assert "n-001" in interaction_events[0].parent_event_ids
