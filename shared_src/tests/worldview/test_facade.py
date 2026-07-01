import pytest
from shared_src.worldview.facade import WorldView


class MockTopologyQuery:
    def get_dependents(self, type_, name):
        return [f"HOST:{name}_host"]
    def get_dependencies(self, type_, name):
        return []
    def get_impact_set(self, type_, name, depth=3):
        return [[f"INSTANCE:{name}_dep"]]
    def query_path(self, ft, fn, tt, tn):
        return []
    def estimate_vm_count(self, type_, name, depth=3):
        return 0


class MockStateQuery:
    def get_state(self, type_, name):
        return "ACTIVE"
    def get_states(self, entities):
        return ["ACTIVE"] * len(entities)
    def get_timeline(self, entity_id, window="1 HOUR"):
        return []
    def has_state_changed(self, entity_id, window_minutes=5):
        return False
    def resolve_field(self, field_path, entity_type, entity_name):
        return None


class MockHistoryQuery:
    def get_recent_events(self, count=50):
        return []
    def get_alarms(self):
        return []
    def get_events_by_type(self, event_type):
        return []


class TestWorldViewFacade:
    def test_worldview_is_pure_facade(self):
        """WorldView 是纯 Facade，不含查询实现"""
        wv = WorldView(
            topology=MockTopologyQuery(),
            state=MockStateQuery(),
            history=MockHistoryQuery(),
        )
        assert hasattr(wv, "topology")
        assert hasattr(wv, "state")
        assert hasattr(wv, "history")
        # Facade 自身没有方法
        facade_methods = [m for m in dir(wv) if not m.startswith("_")]
        assert "topology" in facade_methods
        assert "state" in facade_methods

    def test_worldview_delegates_topology(self):
        """Facade 委派拓扑查询"""
        wv = WorldView(
            topology=MockTopologyQuery(),
            state=MockStateQuery(),
            history=MockHistoryQuery(),
        )
        deps = wv.topology.get_dependents("SERVICE", "rabbitmq")
        assert len(deps) >= 1

    def test_worldview_delegates_state(self):
        """Facade 委派状态查询"""
        wv = WorldView(
            topology=MockTopologyQuery(),
            state=MockStateQuery(),
            history=MockHistoryQuery(),
        )
        state = wv.state.get_state("INSTANCE", "vm-1")
        assert state == "ACTIVE"

    def test_worldview_delegates_history(self):
        """Facade 委派历史查询"""
        wv = WorldView(
            topology=MockTopologyQuery(),
            state=MockStateQuery(),
            history=MockHistoryQuery(),
        )
        events = wv.history.get_recent_events(count=10)
        assert isinstance(events, list)

    def test_worldview_not_god_object(self):
        """WorldView 没有自己的查询方法"""
        wv = WorldView(
            topology=MockTopologyQuery(),
            state=MockStateQuery(),
            history=MockHistoryQuery(),
        )
        # 这些方法应该不在 WorldView 上
        assert not hasattr(wv, "get_dependents")
        assert not hasattr(wv, "get_state")
        assert not hasattr(wv, "get_recent_events")
        assert not hasattr(wv, "get_impact_set")
        assert not hasattr(wv, "get_timeline")
