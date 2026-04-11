"""Acting node for runtime v4 inner graph."""

from __future__ import annotations

from ai.runtime_v4.langgraph.state import InnerGraphState


def run_acting(state: InnerGraphState) -> InnerGraphState:
    state.phase = "acting"
    return state
