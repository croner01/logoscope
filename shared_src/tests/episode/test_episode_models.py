import pytest
from datetime import datetime
from shared_src.episode.models import Episode, EpisodeStep, DecisionStep


class TestEpisodeModels:
    def test_episode_creation(self):
        episode = Episode(episode_id="ep-001", finding_id="f-001",
                           decision_id="d-001", context_hash="ctx_abc123")
        assert episode.finding_id == "f-001"
        assert episode.decision_id == "d-001"
        assert episode.context_hash == "ctx_abc123"

    def test_episode_add_step(self):
        episode = Episode(episode_id="ep-1", finding_id="f-1")
        episode.add_step("observation", {"event": "rabbitmq heartbeat lost"})
        assert len(episode.steps) == 1
        assert episode.steps[0].step_type == "observation"

    def test_episode_append_only(self):
        """Episode 是 append-only"""
        episode = Episode(episode_id="ep-1", finding_id="f-1")
        episode.add_step("observation", {"data": "test"})
        with pytest.raises(Exception):
            episode.steps[0] = EpisodeStep(order=0, step_type="observation", data={})

    def test_decision_step_records_reason(self):
        """DecisionStep 记录候选方案评分和拒绝理由（v15 新增）"""
        step = DecisionStep(
            candidates_scores={"restart": 85.0, "diagnose": 72.3},
            selected_candidate_id="restart",
            reject_reasons=["diagnose: Lower utility"],
            selected_reason="Highest utility: 85.0 (success=0.95, risk=30)",
        )
        assert step.candidates_scores["restart"] == 85.0
        assert len(step.reject_reasons) == 1
        assert step.selected_candidate_id == "restart"

    def test_episode_contains_decision_step(self):
        """Episode 包含 DecisionStep"""
        episode = Episode(episode_id="ep-1", finding_id="f-1")
        episode.add_step("decision", {
            "candidates_scores": {"restart": 85.0},
            "selected_candidate_id": "restart",
            "reject_reasons": [],
        })
        assert episode.steps[-1].step_type == "decision"
        assert "candidates_scores" in episode.steps[-1].data

    def test_episode_multiple_steps(self):
        episode = Episode(episode_id="ep-1", finding_id="f-1")
        episode.add_step("observation", {"msg": "observed"})
        episode.add_step("hypothesis", {"msg": "hypothesized"})
        episode.add_step("decision", {"choice": "restart"})
        episode.add_step("execution", {"outcome": "success"})
        assert len(episode.steps) == 4

    def test_episode_final_outcome(self):
        episode = Episode(episode_id="ep-1", finding_id="f-1")
        episode.final_outcome = "success"
        episode.total_duration_ms = 12000
        assert episode.final_outcome == "success"
        assert episode.total_duration_ms == 12000
