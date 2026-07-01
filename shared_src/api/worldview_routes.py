"""WorldView + Expression API routes."""
from typing import Optional
from fastapi import APIRouter, Query
from pydantic import BaseModel


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
        """Expression 求值（简化版——实际求值由 Expression.evaluate() 完成）。"""
        return {
            "result": True,
            "value": None,
            "expression": {
                "field": req.field,
                "operator": req.operator,
                "value": str(req.value) if req.value else None,
            },
        }

    @router.get("/goals/infer")
    async def infer_goal(type: str = Query(alias="type"), id: str = Query(alias="id")):
        return {
            "primary": "restore_service",
            "tree": {
                "desired_state": "Service.healthy",
                "target": {"type": type, "id": id},
            },
        }

    return router
