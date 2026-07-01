"""Workflow — 可执行工作流模型。"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any


@dataclass
class WorkflowStep:
    """工作流步骤——一个 Capability 调用。"""
    capability: str = ""
    params: Dict[str, Any] = field(default_factory=dict)
    timeout_ms: int = 30000


@dataclass
class Workflow:
    """可执行工作流——由多个有序步骤组成。"""
    name: str = ""
    steps: List[WorkflowStep] = field(default_factory=list)
    max_retries: int = 0
    timeout_ms: int = 60000
