"""
LangGraph inner-loop runner.

Uses real LangGraph execution when available and requested; otherwise falls back
to deterministic local pipeline.
"""

from __future__ import annotations

import os
from typing import Any, Dict

from ai.runtime_v4.langgraph.checkpoint import get_graph_checkpoint_store
from ai.runtime_v4.langgraph.nodes import run_acting, run_observing, run_planning, run_replan
from ai.runtime_v4.langgraph.state import InnerGraphState


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def _inner_engine_mode() -> str:
    raw = _as_str(os.getenv("AI_RUNTIME_V4_INNER_ENGINE"), "langgraph_local").strip().lower()
    if raw in {"langgraph_required", "langgraph-strict", "langgraph_strict"}:
        return "langgraph_required"
    if raw in {"langgraph", "langgraph_v1"}:
        return "langgraph"
    return "langgraph_local"


def inner_engine_name() -> str:
    requested = _inner_engine_mode()
    if requested in {"langgraph", "langgraph_required"} and _langgraph_available():
        return "langgraph-v1"
    if requested in {"langgraph", "langgraph_required"} and not _langgraph_available():
        return "langgraph-local-v1"
    return "langgraph-local-v1"


def _langgraph_available() -> bool:
    try:
        import langgraph  # noqa: F401

        return True
    except Exception:
        return False


def validate_inner_engine_readiness() -> None:
    if _inner_engine_mode() != "langgraph_required":
        return
    if not _langgraph_available():
        raise RuntimeError("langgraph_required but langgraph package is unavailable")


def _state_to_payload(state: InnerGraphState) -> Dict[str, Any]:
    return {
        "run_id": _as_str(state.run_id),
        "question": _as_str(state.question),
        "iteration": int(state.iteration),
        "max_iterations": int(state.max_iterations),
        "phase": _as_str(state.phase, "planning"),
        "actions": list(state.actions),
        "observations": list(state.observations),
        "reflection": dict(state.reflection),
        "done": bool(state.done),
    }


def _payload_to_state(payload: Dict[str, Any]) -> InnerGraphState:
    safe_payload = payload if isinstance(payload, dict) else {}
    return InnerGraphState(
        run_id=_as_str(safe_payload.get("run_id")),
        question=_as_str(safe_payload.get("question")),
        iteration=max(0, int(safe_payload.get("iteration") or 0)),
        max_iterations=max(1, int(safe_payload.get("max_iterations") or 4)),
        phase=_as_str(safe_payload.get("phase"), "planning") or "planning",
        actions=safe_payload.get("actions") if isinstance(safe_payload.get("actions"), list) else [],
        observations=safe_payload.get("observations") if isinstance(safe_payload.get("observations"), list) else [],
        reflection=safe_payload.get("reflection") if isinstance(safe_payload.get("reflection"), dict) else {},
        done=bool(safe_payload.get("done")),
    )


def _run_local_pipeline(state: InnerGraphState) -> InnerGraphState:
    state = run_planning(state)
    if state.done:
        return state
    state = run_acting(state)
    state = run_observing(state)
    state = run_replan(state)
    return state


def _run_real_langgraph(state: InnerGraphState) -> InnerGraphState:
    from langgraph.graph import END, StateGraph

    def _planning_node(payload: Dict[str, Any]) -> Dict[str, Any]:
        next_state = run_planning(_payload_to_state(payload))
        return _state_to_payload(next_state)

    def _acting_node(payload: Dict[str, Any]) -> Dict[str, Any]:
        next_state = run_acting(_payload_to_state(payload))
        return _state_to_payload(next_state)

    def _observing_node(payload: Dict[str, Any]) -> Dict[str, Any]:
        next_state = run_observing(_payload_to_state(payload))
        return _state_to_payload(next_state)

    def _replan_node(payload: Dict[str, Any]) -> Dict[str, Any]:
        next_state = run_replan(_payload_to_state(payload))
        return _state_to_payload(next_state)

    def _route_after_planning(payload: Dict[str, Any]) -> str:
        if bool((payload if isinstance(payload, dict) else {}).get("done")):
            return "done"
        return "acting"

    builder = StateGraph(dict)
    builder.add_node("planning", _planning_node)
    builder.add_node("acting", _acting_node)
    builder.add_node("observing", _observing_node)
    builder.add_node("replan", _replan_node)
    builder.set_entry_point("planning")
    builder.add_conditional_edges("planning", _route_after_planning, {"done": END, "acting": "acting"})
    builder.add_edge("acting", "observing")
    builder.add_edge("observing", "replan")
    builder.add_edge("replan", END)
    graph = builder.compile()

    result = graph.invoke(_state_to_payload(state))
    return _payload_to_state(result if isinstance(result, dict) else {})


def _should_restore_from_checkpoint(state: InnerGraphState) -> bool:
    if int(state.iteration) > 0:
        return False
    if bool(state.actions) or bool(state.observations) or bool(state.reflection):
        return False
    return True


def run_inner_graph(state: InnerGraphState) -> InnerGraphState:
    """Run a minimal planning->acting->observing->replan cycle."""
    requested = _inner_engine_mode()
    if requested == "langgraph_required" and not _langgraph_available():
        raise RuntimeError("langgraph_required but langgraph package is unavailable")

    checkpoint_store = get_graph_checkpoint_store()
    if _should_restore_from_checkpoint(state):
        restored = checkpoint_store.load(state.run_id)
        if restored is not None:
            state = restored

    checkpoint_store.save(state)

    if requested in {"langgraph", "langgraph_required"} and _langgraph_available():
        next_state = _run_real_langgraph(state)
    else:
        next_state = _run_local_pipeline(state)
    checkpoint_store.save(next_state)
    return next_state
