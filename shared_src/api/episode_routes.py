"""Episode API routes."""
from fastapi import APIRouter, HTTPException
from shared_src.episode.store import EpisodeStore


def create_episode_router(episode_store: EpisodeStore) -> APIRouter:
    router = APIRouter(tags=["episode"])

    @router.get("/episodes/by-decision/{decision_id}")
    async def get_episode_by_decision(decision_id: str):
        episode = episode_store.get_by_decision(decision_id)
        if not episode:
            raise HTTPException(status_code=404, detail="Episode not found")
        return {
            "episode_id": episode.episode_id,
            "finding_id": episode.finding_id,
            "decision_id": episode.decision_id,
            "final_outcome": episode.final_outcome,
            "total_duration_ms": episode.total_duration_ms,
            "steps": [
                {
                    "step_type": s.step_type,
                    "data": s.data,
                    "order": s.order,
                    "timestamp": s.timestamp.isoformat(),
                }
                for s in episode.steps
            ],
            "created_at": episode.created_at.isoformat(),
        }

    @router.get("/episodes/{episode_id}")
    async def get_episode(episode_id: str):
        episode = episode_store.get(episode_id)
        if not episode:
            raise HTTPException(status_code=404, detail="Episode not found")
        return {
            "episode_id": episode.episode_id,
            "finding_id": episode.finding_id,
            "decision_id": episode.decision_id,
            "steps_count": len(episode.steps),
        }

    return router
