"""Observing node for runtime v4 inner graph."""

from __future__ import annotations

from ai.runtime_v4.langgraph.state import InnerGraphState


def run_observing(state: InnerGraphState) -> InnerGraphState:
    state.phase = "observing"
    return state
