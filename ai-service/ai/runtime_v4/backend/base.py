"""Shared runtime backend request/response models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Protocol


@dataclass
class RuntimeBackendRequest:
    """Input passed from the v4 bridge into an inner agent backend."""

    run_id: str
    question: str
    analysis_context: Dict[str, Any] = field(default_factory=dict)
    runtime_options: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RuntimeBackendResult:
    """Normalized result returned by an inner agent backend."""

    inner_engine: str
    payload: Dict[str, Any] = field(default_factory=dict)


class RuntimeBackend(Protocol):
    """Protocol implemented by runtime v4 inner agent backends."""

    def backend_name(self) -> str:
        ...

    def run(self, request: RuntimeBackendRequest) -> RuntimeBackendResult:
        ...
