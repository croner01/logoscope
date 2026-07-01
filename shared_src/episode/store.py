"""EpisodeStore — Episode 存储。"""
from typing import Dict, Optional
from .models import Episode


class EpisodeStore:
    """Episode 存储——按 event_id 和 decision_id 索引。"""

    def __init__(self):
        self._episodes: Dict[str, Episode] = {}
        self._by_decision: Dict[str, str] = {}

    def save(self, episode: Episode):
        self._episodes[episode.episode_id] = episode
        if episode.decision_id:
            self._by_decision[episode.decision_id] = episode.episode_id

    def get(self, episode_id: str) -> Optional[Episode]:
        return self._episodes.get(episode_id)

    def get_by_decision(self, decision_id: str) -> Optional[Episode]:
        episode_id = self._by_decision.get(decision_id)
        return self._episodes.get(episode_id) if episode_id else None
