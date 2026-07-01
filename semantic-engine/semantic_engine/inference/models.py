"""Inference 数据模型。"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class InferenceInput:
    """推理输入。"""
    context: Dict[str, Any] = field(default_factory=dict)
    knowledge: List[Any] = field(default_factory=list)
    event_type: str = ""


@dataclass
class InferenceOutput:
    """推理输出。"""
    findings: List[Any] = field(default_factory=list)
    processing_time_ms: float = 0.0
    stages_completed: List[str] = field(default_factory=list)
