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


def test_runtime_backend_uses_openhands_flag(monkeypatch):
    monkeypatch.setenv("AI_RUNTIME_V4_AGENT_BACKEND", "openhands")
    monkeypatch.setenv("AI_RUNTIME_V4_OPENHANDS_ENABLED", "true")
    reset_runtime_backend()

    backend = get_runtime_backend()

    assert backend.backend_name() == "openhands-v1"


def test_runtime_backend_uses_explicit_requested_backend(monkeypatch):
    monkeypatch.delenv("AI_RUNTIME_V4_AGENT_BACKEND", raising=False)
    monkeypatch.setenv("AI_RUNTIME_V4_OPENHANDS_ENABLED", "true")
    reset_runtime_backend()

    backend = get_runtime_backend(requested_backend="openhands")

    assert backend.backend_name() == "openhands-v1"


def test_openhands_backend_requires_explicit_enable_flag(monkeypatch):
    monkeypatch.setenv("AI_RUNTIME_V4_AGENT_BACKEND", "openhands")
    monkeypatch.delenv("AI_RUNTIME_V4_OPENHANDS_ENABLED", raising=False)
    reset_runtime_backend()

    with pytest.raises(RuntimeError, match="OpenHands backend is disabled"):
        validate_runtime_backend_readiness()


def test_openhands_backend_requires_valid_provider_factory_when_configured(monkeypatch):
    monkeypatch.setenv("AI_RUNTIME_V4_AGENT_BACKEND", "openhands")
    monkeypatch.setenv("AI_RUNTIME_V4_OPENHANDS_ENABLED", "true")
    monkeypatch.setenv("AI_RUNTIME_V4_OPENHANDS_PROVIDER_FACTORY", "missing.module:create_provider")
    reset_runtime_backend()

    with pytest.raises(RuntimeError, match="OpenHands provider factory import failed"):
        validate_runtime_backend_readiness()


def test_openhands_backend_requires_valid_helper_paths_when_helper_enabled(monkeypatch, tmp_path):
    helper_script = tmp_path / "openhands_helper.py"
    helper_script.write_text("# helper placeholder\n", encoding="utf-8")

    monkeypatch.setenv("AI_RUNTIME_V4_AGENT_BACKEND", "openhands")
    monkeypatch.setenv("AI_RUNTIME_V4_OPENHANDS_ENABLED", "true")
    monkeypatch.setenv("AI_RUNTIME_V4_OPENHANDS_HELPER_ENABLED", "true")
    monkeypatch.setenv("AI_RUNTIME_V4_OPENHANDS_HELPER_PYTHON", str(tmp_path / "missing-python"))
    monkeypatch.setenv("AI_RUNTIME_V4_OPENHANDS_HELPER_SCRIPT", str(helper_script))
    reset_runtime_backend()

    with pytest.raises(RuntimeError, match="OpenHands helper python not found"):
        validate_runtime_backend_readiness()
