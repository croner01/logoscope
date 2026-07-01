"""Blast Radius 数据模型。"""
from dataclasses import dataclass, field
from typing import List


@dataclass
class BlastRadiusReport:
    """
    影响范围报告。

    - primary_target: 主要目标
    - directly_affected: 直接受影响的服务
    - indirectly_affected: 间接受影响的服务
    - risk_level: low / medium / high / critical
    - reasoning: 分析推理过程
    """
    primary_target_type: str = ""
    primary_target_name: str = ""
    directly_affected: List[str] = field(default_factory=list)
    indirectly_affected: List[str] = field(default_factory=list)
    estimated_vm_count: int = 0
    estimated_service_count: int = 0
    risk_level: str = "low"
    reasoning: str = ""
