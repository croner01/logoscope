import pytest
from shared_src.episode.models import Episode
from shared_src.episode.store import EpisodeStore


class TestEpisodeStore:
    def test_save_and_get(self):
        store = EpisodeStore()
        episode = Episode(episode_id="ep-1", finding_id="f-1")
        store.save(episode)
        retrieved = store.get("ep-1")
        assert retrieved is not None
        assert retrieved.episode_id == "ep-1"

    def test_get_by_decision(self):
        store = EpisodeStore()
        episode = Episode(episode_id="ep-1", finding_id="f-1", decision_id="d-1")
        store.save(episode)
        result = store.get_by_decision("d-1")
        assert result.episode_id == "ep-1"

    def test_get_nonexistent(self):
        store = EpisodeStore()
        assert store.get("nonexistent") is None

    def test_get_by_decision_nonexistent(self):
        store = EpisodeStore()
        assert store.get_by_decision("nonexistent") is None
