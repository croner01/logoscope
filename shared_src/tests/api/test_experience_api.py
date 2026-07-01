import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from shared_src.api.experience_routes import create_experience_router
from semantic_engine.projections.experience_graph import ExperienceGraphProjection


@pytest.fixture
def client():
    app = FastAPI()
    exp = ExperienceGraphProjection(epoch="20260701")

    exp.record_learning("RabbitMQHeartbeatLost", "ssh.restart_service", "prod", "success")
    exp.record_learning("RabbitMQHeartbeatLost", "ssh.restart_service", "prod", "success")
    exp.record_learning("NovaOOM", "ssh.restart_service", "prod", "failure")

    router = create_experience_router(exp)
    app.include_router(router, prefix="/api/v1")
    return TestClient(app)


class TestExperienceAPI:
    def test_success_rate(self, client):
        resp = client.get(
            "/api/v1/experience/success-rate"
            "?pattern=RabbitMQHeartbeatLost"
            "&capability=ssh.restart_service&env=prod"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "success_rate" in data
        assert data["success_rate"] == 1.0  # 2/2

    def test_stats_by_pattern(self, client):
        resp = client.get(
            "/api/v1/experience/stats"
            "?pattern=RabbitMQHeartbeatLost"
            "&capability=ssh.restart_service&env=prod"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_executions"] == 2
