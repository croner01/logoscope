"""统一 DiagnosisBackend ABC — 诊断执行后端接口。

两个运行时共享此接口:
- ClaudeSdkBackend: Messages API + YAML skills → ToolAdapter
- LangGraphBackend:  PromptBuilder + LLMService → ToolAdapter
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Type

from ai.diagnosis.context import DiagnosisContext
from ai.runtime.events import EventEmitter
from ai.runtime.memory import SessionMemory
from ai.runtime.tools import ToolAdapter


@dataclass
class BackendRequest:
    """后端执行请求。"""
    context: DiagnosisContext
    event_emitter: EventEmitter
    tools: ToolAdapter
    memory: SessionMemory


@dataclass
class BackendResult:
    """后端执行结果。"""
    actions: List[Dict[str, Any]] = field(default_factory=list)
    action_observations: List[Dict[str, Any]] = field(default_factory=list)
    iterations: List[Dict[str, Any]] = field(default_factory=list)
    summary: str = ""
    replan_needed: bool = False


class DiagnosisBackend(ABC):
    """诊断执行后端的抽象基类。"""

    @property
    @abstractmethod
    def name(self) -> str:
        ...

    @abstractmethod
    async def run(self, request: BackendRequest) -> BackendResult:
        ...


# ── 注册表 ────────────────────────────────────────────────────────────────

_registry: Dict[str, Type[DiagnosisBackend]] = {}


def register_backend(name: str, cls: Type[DiagnosisBackend]) -> None:
    """注册后端类到全局注册表。"""
    _registry[name] = cls


def get_backend(name: Optional[str] = None) -> DiagnosisBackend:
    """获取后端实例。

    如果未指定 name，按以下优先级:
    1. 旧变量 AI_RUNTIME_UNIFIED_ENGINE_ENABLED=true → claude-sdk
    2. AI_RUNTIME_BACKEND 环境变量 → 指定值
    3. 默认 → claude-sdk
    """
    if name is None:
        if _is_legacy_unified_engine_enabled():
            name = "claude-sdk"
        else:
            name = os.getenv("AI_RUNTIME_BACKEND", "claude-sdk")
    cls = _registry.get(name)
    if cls is None:
        raise KeyError(
            f"Unknown backend: {name}, available: {list(_registry.keys())}"
        )
    return cls()


def _is_legacy_unified_engine_enabled() -> bool:
    """检查旧版 AI_RUNTIME_UNIFIED_ENGINE_ENABLED 环境变量。"""
    val = os.getenv("AI_RUNTIME_UNIFIED_ENGINE_ENABLED", "").strip().lower()
    return val in ("1", "true", "yes", "on")
