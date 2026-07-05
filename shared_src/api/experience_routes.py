"""Experience API routes."""
import logging
from fastapi import APIRouter, HTTPException, Query

logger = logging.getLogger(__name__)


def create_experience_router(experience_graph_ref=None) -> APIRouter:
    """
    创建 Experience API 路由。

    Args:
        experience_graph_ref: 可变引用容器（单元素列表），用于在 router 创建后
                              注入或更新 ExperienceGraph 实例。
                              传 None 时认为永无依赖，始终返回空结果。
    """
    # 兼容直接传 ExperienceGraph 实例的旧调用方式
    if experience_graph_ref is None or not isinstance(experience_graph_ref, list):
        _ref = [experience_graph_ref]  # 包装为 mutable ref
    else:
        _ref = experience_graph_ref

    router = APIRouter(tags=["experience"])

    def _safe_get_stats(pattern, capability, env):
        """安全获取 stats，experience_graph 未就绪时返回 None。"""
        graph = _ref[0] if _ref else None
        if graph is None:
            logger.warning("ExperienceGraph not initialized, returning empty stats")
            return None
        try:
            return graph.get_stats(pattern, capability, env)
        except Exception as e:
            logger.exception("Failed to get experience stats: %s", e)
            return None

    @router.get("/experience/success-rate")
    async def get_success_rate(
        pattern: str = Query(alias="pattern"),
        capability: str = Query(alias="capability"),
        env: str = Query(alias="env"),
    ):
        stats = _safe_get_stats(pattern, capability, env)
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
        stats = _safe_get_stats(pattern, capability, env)
        if not stats:
            raise HTTPException(status_code=404, detail="Stats not found or ExperienceGraph not initialized")
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
