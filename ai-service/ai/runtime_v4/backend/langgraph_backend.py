"""LangGraph-backed runtime v4 inner backend."""

from __future__ import annotations

from ai.runtime_v4.backend.base import RuntimeBackendRequest, RuntimeBackendResult
from ai.runtime_v4.langgraph import InnerGraphState, inner_engine_name, run_inner_graph


class LangGraphBackend:
    """Adapter around the existing LangGraph inner loop."""

    def backend_name(self) -> str:
        return inner_engine_name()

    def run(self, request: RuntimeBackendRequest) -> RuntimeBackendResult:
        state = InnerGraphState(
            run_id=request.run_id,
            question=request.question,
            max_iterations=max(1, min(10, int(request.runtime_options.get("max_iterations") or 4))),
            skill_context=dict(request.analysis_context or {}),
        )
        final_state = run_inner_graph(state)
        return RuntimeBackendResult(
            inner_engine=self.backend_name(),
            payload={
                "iteration": final_state.iteration,
                "phase": final_state.phase,
                "selected_skills": list(final_state.selected_skills),
                "actions": list(final_state.actions),
                "done": bool(final_state.done),
            },
        )
