"""Experience API routes."""
from fastapi import APIRouter, HTTPException, Query


def create_experience_router(experience_graph) -> APIRouter:
    router = APIRouter(tags=["experience"])

    @router.get("/experience/success-rate")
    async def get_success_rate(
        pattern: str = Query(alias="pattern"),
        capability: str = Query(alias="capability"),
        env: str = Query(alias="env"),
    ):
        stats = experience_graph.get_stats(pattern, capability, env)
        if not stats:
            return {"pattern": pattern, "capability": capability,
                    "env": env, "success_rate": 0.0, "total_executions": 0}
        return {
            "pattern": pattern,
            "capability": capability,
            "env": env,
            "success_rate": stats.success_rate,
            "total_executions": stats.total_executions,
        }

    @router.get("/experience/stats")
    async def get_stats(
        pattern: str = Query(alias="pattern"),
        capability: str = Query(alias="capability"),
        env: str = Query(alias="env"),
    ):
        stats = experience_graph.get_stats(pattern, capability, env)
        if not stats:
            raise HTTPException(status_code=404, detail="Stats not found")
        return {
            "failure_pattern": stats.failure_pattern,
            "capability_id": stats.capability_id,
            "env_fingerprint": stats.env_fingerprint,
            "total_executions": stats.total_executions,
            "success_count": stats.success_count,
            "failure_count": stats.failure_count,
            "success_rate": stats.success_rate,
        }

    return router
