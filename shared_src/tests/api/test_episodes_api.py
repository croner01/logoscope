import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from shared_src.api.episode_routes import create_episode_router
from shared_src.episode.models import Episode, EpisodeStep
from shared_src.episode.store import EpisodeStore


@pytest.fixture
def client():
    app = FastAPI()
    store = EpisodeStore()

    # 填充测试数据
    episode = Episode(episode_id="ep-001", finding_id="f-001", decision_id="d-001")
    episode.add_step("observation", {"event": "heartbeat lost"})
    episode.add_step("decision", {
        "candidates_scores": {"restart": 85.0},
        "selected_candidate_id": "restart",
        "reject_reasons": ["diagnose: lower utility"],
    })
    store.save(episode)

    router = create_episode_router(store)
    app.include_router(router, prefix="/api/v1")
    return TestClient(app)


class TestEpisodeAPI:
    def test_get_by_decision(self, client):
        resp = client.get("/api/v1/episodes/by-decision/d-001")
        assert resp.status_code == 200
        data = resp.json()
        assert "episode_id" in data
        assert any(s["step_type"] == "decision" for s in data["steps"])
