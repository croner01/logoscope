"""Replan node for runtime v4 inner graph."""

from __future__ import annotations

from ai.runtime_v4.langgraph.state import InnerGraphState


def run_replan(state: InnerGraphState) -> InnerGraphState:
    state.phase = "replan"
    if state.iteration >= state.max_iterations:
        state.done = True
    return state
