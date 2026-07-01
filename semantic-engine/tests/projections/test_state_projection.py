import pytest
import json
from datetime import datetime, timedelta
from shared_src.event.envelope import EventEnvelope
from semantic_engine.projections.state_projection import StateProjection


class TestStateProjection:
    def test_state_write_and_query(self):
        projection = StateProjection()
        env = EventEnvelope(
            event_id="s1",
            event_type="state.changed",
            payload=json.dumps({
                "entity_type": "SERVICE",
                "entity_name": "nova-api",
                "state": "ERROR",
            }).encode(),
        )
        projection.apply(env)
        assert projection.query("SERVICE", "nova-api") == "ERROR"

    def test_state_update(self):
        projection = StateProjection()
        env1 = EventEnvelope(event_id="s1", event_type="state.changed",
            payload=json.dumps({"entity_type": "SERVICE", "entity_name": "svc", "state": "ACTIVE"}).encode())
        env2 = EventEnvelope(event_id="s2", event_type="state.changed",
            payload=json.dumps({"entity_type": "SERVICE", "entity_name": "svc", "state": "ERROR"}).encode())
        projection.apply(env1)
        projection.apply(env2)
        assert projection.query("SERVICE", "svc") == "ERROR"  # 最新状态

    def test_query_nonexistent(self):
        projection = StateProjection()
        assert projection.query("SERVICE", "nonexistent") is None

    def test_rebuild(self):
        projection = StateProjection()
        env = EventEnvelope(event_id="s1", event_type="state.changed",
            payload=json.dumps({"entity_type": "SERVICE", "entity_name": "svc", "state": "ACTIVE"}).encode())
        projection.apply(env)
        projection.rebuild([])
        assert projection.query("SERVICE", "svc") is None
