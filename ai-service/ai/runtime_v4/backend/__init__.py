"""Runtime v4 backend factory and helpers."""

from __future__ import annotations

import os
from typing import Dict

from ai.runtime_v4.backend.base import RuntimeBackend
from ai.runtime_v4.backend.langgraph_backend import LangGraphBackend
from ai.runtime_v4.backend.openhands_backend import OpenHandsBackend
from ai.runtime_v4.backend.openhands_provider import (
    reset_openhands_provider,
    validate_openhands_provider_readiness,
)


_runtime_backends: Dict[str, RuntimeBackend] = {}


def _normalize_backend_mode(value: str) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"openhands", "openhands_v1", "openhands-v1"}:
        return "openhands"
    return "langgraph"


def _backend_mode(requested_backend: str = "") -> str:
    if str(requested_backend or "").strip():
        return _normalize_backend_mode(requested_backend)
    return _normalize_backend_mode(os.getenv("AI_RUNTIME_V4_AGENT_BACKEND") or "langgraph")


def validate_runtime_backend_readiness(requested_backend: str = "") -> None:
    """Fail closed when an experimental backend is configured but not enabled."""

    mode = _backend_mode(requested_backend)
    if mode != "openhands":
        return
    enabled = str(os.getenv("AI_RUNTIME_V4_OPENHANDS_ENABLED") or "false").strip().lower()
    if enabled not in {"1", "true", "yes", "on"}:
        raise RuntimeError(
            "OpenHands backend is disabled; set AI_RUNTIME_V4_OPENHANDS_ENABLED=true to enable it"
        )
    validate_openhands_provider_readiness()


def get_runtime_backend(*, requested_backend: str = "") -> RuntimeBackend:
    mode = _backend_mode(requested_backend)
    validate_runtime_backend_readiness(mode)
    backend = _runtime_backends.get(mode)
    if backend is None:
        if mode == "openhands":
            backend = OpenHandsBackend()
        else:
            backend = LangGraphBackend()
        _runtime_backends[mode] = backend
    return backend


def reset_runtime_backend() -> None:
    _runtime_backends.clear()
    reset_openhands_provider()


__all__ = [
    "RuntimeBackend",
    "get_runtime_backend",
    "reset_runtime_backend",
    "validate_runtime_backend_readiness",
]
