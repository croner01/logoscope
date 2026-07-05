"""WorldView + Expression API routes."""
import logging
from typing import Optional
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from shared_src.expression.models import Expression
from shared_src.goal.models import Goal, GoalNode

logger = logging.getLogger(__name__)


class ExpressionRequest(BaseModel):
    field: str
    operator: str
    value: object = None
    target: dict = {}


class ExpressionResponse(BaseModel):
    result: bool
    value: object = None


def create_worldview_router(topology=None, state=None, history=None) -> APIRouter:
    """创建 WorldView 路由。"""
    router = APIRouter(tags=["worldview"])

    @router.get("/worldview/health")
    async def health():
        return {"status": "ok"}

    @router.get("/worldview/topology/dependents")
    async def get_dependents(type: str = Query(alias="type"), id: str = Query(alias="id")):
        if topology:
            deps = topology.get_dependents(type, id)
            return {"entity_type": type, "entity_id": id, "dependents": deps}
        return {"entity_type": type, "entity_id": id, "dependents": []}

    @router.get("/worldview/state/current")
    async def get_current_state(type: str = Query(alias="type"), id: str = Query(alias="id")):
        if state:
            current = state.get_state(type, id)
            return {"entity_type": type, "entity_id": id, "state": current}
        return {"entity_type": type, "entity_id": id, "state": None}

    @router.get("/worldview/history/events")
    async def get_history_events(type: str = Query(alias="type"), id: str = Query(alias="id"), count: int = 50):
        if history:
            events = history.get_recent_events(count=count)
            return {"entity_type": type, "entity_id": id, "events": [str(e) for e in events]}
        return {"entity_type": type, "entity_id": id, "events": []}

    @router.post("/expressions/evaluate")
    async def evaluate_expression(req: ExpressionRequest):
        """Expression 求值——使用 Expression.evaluate() 基于 WorldView 状态执行。"""
        try:
            expr = Expression(field=req.field, operator=req.operator, value=req.value)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        target = req.target or {}
        entity_type = target.get("type", "SERVICE")
        entity_name = target.get("name", "")
        if not entity_name:
            return {
                "result": False,
                "value": None,
                "expression": {
                    "field": req.field,
                    "operator": req.operator,
                    "value": str(req.value) if req.value else None,
                },
                "error": "target.name is required",
            }

        # 使用 WorldView state resolver 进行求值
        # 如果没有 WorldView 实例，回退到假值模式
        if not state:
            logger.warning("WorldView state not available, returning placeholder result")
            return {
                "result": True,
                "value": None,
                "expression": {
                    "field": req.field,
                    "operator": req.operator,
                    "value": str(req.value) if req.value else None,
                },
            }

        try:
            result = expr.evaluate(state, entity_type, entity_name)
            return {
                "result": result,
                "value": result,
                "expression": {
                    "field": expr.field,
                    "operator": expr.operator,
                    "value": str(expr.value) if expr.value else None,
                },
            }
        except Exception as e:
            logger.exception("Expression evaluation failed: %s", str(e))
            raise HTTPException(status_code=500, detail=f"Expression evaluation error: {str(e)}")

    @router.get("/goals/infer")
    async def infer_goal(type: str = Query(alias="type"), id: str = Query(alias="id")):
        """基于实体类型推断恢复目标。"""
        # 推断 primary goal
        goal_map = {
            "SERVICE": "restore_service",
            "HOST": "restore_host",
            "INSTANCE": "restore_instance",
            "CONTAINER": "restore_container",
            "NAMESPACE": "restore_namespace",
        }
        primary = goal_map.get(type.upper(), "restore_service")

        # 构建 Goal 对象
        goal = Goal(
            primary=primary,
            tree=GoalNode(
                goal_id="root",
                desired_state=f"{type}.healthy" if type else "Service.healthy",
                children=[],
            ),
        )
        return {
            "primary": goal.primary,
            "tree": {
                "goal_id": goal.tree.goal_id,
                "desired_state": goal.tree.desired_state,
                "target": {"type": type, "id": id},
            },
        }

    return router
