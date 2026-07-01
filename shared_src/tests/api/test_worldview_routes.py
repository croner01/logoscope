import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from shared_src.api.worldview_routes import create_worldview_router


class MockTopology:
    def get_dependents(self, t, n):
        return [f"SERVICE:{n}_dep"]
    def get_dependencies(self, t, n):
        return [f"SERVICE:rabbitmq"]
    def get_impact_set(self, t, n, depth=3):
        return [[f"INSTANCE:{n}_dep1"]]
    def query_path(self, ft, fn, tt, tn):
        return [f"{ft}:{fn}", f"{tt}:{tn}"]
    def estimate_vm_count(self, t, n, depth=3):
        return 5


class MockState:
    def get_state(self, t, n):
        return "ACTIVE"
    def get_states(self, entities):
        return ["ACTIVE"] * len(entities)
    def get_timeline(self, eid, window="1 HOUR"):
        return []
    def has_state_changed(self, eid, window_minutes=5):
        return False
    def resolve_field(self, fp, t, n):
        return "ACTIVE"


class MockHistory:
    def get_recent_events(self, count=50):
        return []
    def get_alarms(self):
        return []
    def get_events_by_type(self, t):
        return []


@pytest.fixture
def client():
    app = FastAPI()
    router = create_worldview_router(
        topology=MockTopology(),
        state=MockState(),
        history=MockHistory(),
    )
    app.include_router(router, prefix="/api/v1")
    return TestClient(app)


class TestWorldViewEndpoints:
    def test_topology_dependents(self, client):
        resp = client.get("/api/v1/worldview/topology/dependents?type=SERVICE&id=rabbitmq")
        assert resp.status_code == 200
        data = resp.json()
        assert "dependents" in data

    def test_state_current(self, client):
        resp = client.get("/api/v1/worldview/state/current?type=INSTANCE&id=vm-1")
        assert resp.status_code == 200
        data = resp.json()
        assert "state" in data

    def test_history_events(self, client):
        resp = client.get("/api/v1/worldview/history/events?type=SERVICE&id=nova-api&count=50")
        assert resp.status_code == 200
        data = resp.json()
        assert "events" in data

    def test_expressions_evaluate(self, client):
        """Expression 求值端点"""
        resp = client.post("/api/v1/expressions/evaluate", json={
            "field": "resource.status",
            "operator": "==",
            "value": "ACTIVE",
            "target": {"type": "INSTANCE", "id": "vm-1"},
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "result" in data

    def test_health(self, client):
        resp = client.get("/api/v1/worldview/health")
        assert resp.status_code == 200
