import pytest
import json
from shared_src.event.envelope import EventEnvelope
from shared_src.event.bus import InMemoryEventBus
from shared_src.event.schema_registry import SchemaRegistry
from semantic_engine.projectors.state_projector import StateProjector


class TestStateProjector:
    def test_state_projector_produces_state_event(self):
        """StateProjector 从 NormalizedEvent 提取状态并发布"""
        bus = InMemoryEventBus()
        schema_registry = SchemaRegistry()
        projector = StateProjector(schema_registry, bus)

        norm_env = EventEnvelope(
            event_id="n1",
            event_type="normalized.event",
            payload=json.dumps({
                "entity": {"type": "service", "name": "nova-api", "instance": "abc-123"},
                "severity_number": 17,  # ERROR
            }).encode(),
        )

        projector.process(norm_env)
        state_events = bus._history.get("platform.state", [])
        assert len(state_events) >= 1
        payload = json.loads(state_events[0].payload)
        assert "state" in payload

    def test_state_derived_from_severity(self):
        """从 severity_number 推断状态"""
        bus = InMemoryEventBus()
        projector = StateProjector(SchemaRegistry(), bus)

        for severity, expected in [(17, "ERROR"), (9, "WARN"), (5, "INFO")]:
            env = EventEnvelope(
                event_id=f"n-{severity}",
                event_type="normalized.event",
                payload=json.dumps({
                    "entity": {"type": "service", "name": "test-svc"},
                    "severity_number": severity,
                }).encode(),
            )
            projector.process(env)
            state_events = bus._history.get("platform.state", [])
            payload = json.loads(state_events[-1].payload)
            assert payload["state"] == expected, f"severity {severity} -> {expected}"

    def test_state_event_has_parent_event_id(self):
        """state event 追踪血缘"""
        bus = InMemoryEventBus()
        projector = StateProjector(SchemaRegistry(), bus)
        norm_env = EventEnvelope(
            event_id="n-001",
            event_type="normalized.event",
            payload=json.dumps({
                "entity": {"type": "service", "name": "svc"},
                "severity_number": 17,
            }).encode(),
        )
        projector.process(norm_env)
        state_events = bus._history.get("platform.state", [])
        assert "n-001" in state_events[0].parent_event_ids
