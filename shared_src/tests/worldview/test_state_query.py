import pytest
from datetime import datetime, timedelta
from shared_src.worldview.state_query import StateQuery


class MockStateProjection:
    """模拟 StateProjection"""
    def __init__(self, states=None):
        self._states = states or {}

    def query(self, entity_type, entity_name):
        key = f"{entity_type}:{entity_name}"
        return self._states.get(key)

    def get_all_states(self):
        return dict(self._states)


class MockTimelineProjection:
    def __init__(self):
        self._transitions = []

    def record_transition(self, entity_id, from_state, to_state, timestamp=None, event_id=""):
        pass

    def get_timeline(self, entity_id, window="1 HOUR"):
        return [t for t in self._transitions if t.entity_id == entity_id]

    def has_state_changed(self, entity_id, window_minutes=5):
        return False


class TestStateQuery:
    def test_get_state(self):
        mock_state = MockStateProjection({"INSTANCE:vm-1": "ACTIVE"})
        sq = StateQuery(state_projection=mock_state, timeline_projection=MockTimelineProjection())
        state = sq.get_state("INSTANCE", "vm-1")
        assert state == "ACTIVE"

    def test_get_states_batch(self):
        mock_state = MockStateProjection({
            "INSTANCE:vm-1": "ACTIVE",
            "INSTANCE:vm-2": "ERROR",
        })
        sq = StateQuery(state_projection=mock_state, timeline_projection=MockTimelineProjection())
        states = sq.get_states([("INSTANCE", "vm-1"), ("INSTANCE", "vm-2")])
        assert len(states) == 2
        assert states[0] == "ACTIVE"
        assert states[1] == "ERROR"

    def test_get_state_nonexistent(self):
        mock_state = MockStateProjection({})
        sq = StateQuery(state_projection=mock_state, timeline_projection=MockTimelineProjection())
        assert sq.get_state("INSTANCE", "nonexistent") is None

    def test_resolve_field_resource_status(self):
        """resolve_field 解析 resource.status"""
        mock_state = MockStateProjection({"INSTANCE:vm-1": "ACTIVE"})
        sq = StateQuery(state_projection=mock_state, timeline_projection=MockTimelineProjection())
        value = sq.resolve_field("resource.status", "INSTANCE", "vm-1")
        assert value == "ACTIVE"

    def test_resolve_field_host_status(self):
        """resolve_field 解析 host.host_status"""
        mock_state = MockStateProjection({"HOST:vm-1": "alive"})
        sq = StateQuery(state_projection=mock_state, timeline_projection=MockTimelineProjection())
        value = sq.resolve_field("host.host_status", "INSTANCE", "vm-1")
        assert value == "alive"

    def test_get_timeline(self):
        mock_timeline = MockTimelineProjection()
        sq = StateQuery(state_projection=MockStateProjection({}), timeline_projection=mock_timeline)
        timeline = sq.get_timeline("INSTANCE:vm-1", "1 HOUR")
        assert isinstance(timeline, list)

    def test_has_state_changed(self):
        mock_timeline = MockTimelineProjection()
        sq = StateQuery(state_projection=MockStateProjection({}), timeline_projection=mock_timeline)
        assert sq.has_state_changed("INSTANCE:vm-1", window_minutes=5) == False
