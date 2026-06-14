"""Tests for runtime v4 backend factory selection."""

import pytest

from ai.runtime_v4.backend import (
    get_runtime_backend,
    reset_runtime_backend,
    validate_runtime_backend_readiness,
)


def test_default_runtime_backend_is_langgraph(monkeypatch):
    monkeypatch.delenv("AI_RUNTIME_V4_AGENT_BACKEND", raising=False)
    reset_runtime_backend()

    backend = get_runtime_backend()

    assert backend.backend_name() == "langgraph-local-v1"
