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


def test_claude_sdk_backend_requires_explicit_enable_flag(monkeypatch):
    monkeypatch.delenv("AI_RUNTIME_V4_CLAUDE_SDK_ENABLED", raising=False)
    reset_runtime_backend()

    with pytest.raises(RuntimeError, match="Claude SDK backend is disabled"):
        validate_runtime_backend_readiness("claude_sdk")


def test_claude_sdk_backend_can_be_instantiated(monkeypatch):
    monkeypatch.setenv("AI_RUNTIME_V4_CLAUDE_SDK_ENABLED", "true")
    reset_runtime_backend()

    backend = get_runtime_backend(requested_backend="claude_sdk")

    assert backend.backend_name() == "claude-sdk-v1"
    assert "Claude" in type(backend).__name__
