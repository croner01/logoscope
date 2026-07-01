"""Workflow 数据模型。"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any
from datetime import datetime


@dataclass
class WorkflowStep:
    """工作流步骤。"""
    capability: str = ""
    params: Dict[str, Any] = field(default_factory=dict)
    timeout_seconds: int = 30


@dataclass
class Workflow:
    """工作流定义。"""
    workflow_id: str = ""
    name: str = ""
    steps: List[WorkflowStep] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class WorkflowCommand:
    """工作流命令——发送到 platform.workflow.command。"""
    command_id: str = ""
    workflow_id: str = ""
    action: str = "execute"
    params: Dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.utcnow)


@dataclass
class WorkflowEvent:
    """工作流事件——发送到 platform.workflow.event。"""
    event_id: str = ""
    workflow_id: str = ""
    command_id: str = ""
    outcome: str = "pending"  # "success", "failure", "running"
    result: Dict[str, Any] = field(default_factory=dict)
    error_message: str = ""
    timestamp: datetime = field(default_factory=datetime.utcnow)


@dataclass
class WorkflowContext:
    """工作流执行上下文。"""
    trigger: str = ""
    environment: str = "production"
    user: str = "system"
    metadata: Dict[str, Any] = field(default_factory=dict)
