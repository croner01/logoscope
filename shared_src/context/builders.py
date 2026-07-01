"""Context builders — 各种上下文类型的构建器。"""
from dataclasses import dataclass, field
from typing import List, Optional, Any


@dataclass
class IncidentContext:
    """事件上下文——相关的 Finding、告警、时间线。"""
    findings: List[dict] = field(default_factory=list)
    alarms: List[dict] = field(default_factory=list)
    timeline: List[dict] = field(default_factory=list)
    summary: str = ""


@dataclass
class TopologyContext:
    """拓扑上下文——依赖关系、影响范围。"""
    dependents: List[str] = field(default_factory=list)
    dependencies: List[str] = field(default_factory=list)
    impact_set: List[List[str]] = field(default_factory=list)
    estimated_vm_count: int = 0


@dataclass
class WorkflowContext:
    """工作流上下文——执行历史、允许的操作。"""
    recent_actions: List[str] = field(default_factory=list)
    allowed_capabilities: List[str] = field(default_factory=list)
    running_workflows: int = 0


@dataclass
class RuleContext:
    """规则上下文——适用的约束和策略。"""
    constraints: List[dict] = field(default_factory=list)
    active_policies: List[str] = field(default_factory=list)
