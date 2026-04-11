"""Planning node for runtime v4 inner graph."""

from __future__ import annotations

from ai.runtime_v4.langgraph.state import InnerGraphState


def run_planning(state: InnerGraphState) -> InnerGraphState:
    state.phase = "planning"
    state.iteration += 1
    if state.iteration > state.max_iterations:
        state.done = True
    return state
