"""Knowledge Object Model — 类型化知识对象。"""
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class KnowledgeDocument:
    """知识对象基类。"""
    document_id: str
    title: str
    document_type: str = "knowledge"
    origin: str = ""
    version: str = "v1"
    trust_level: int = 1
    content: str = ""
    tags: List[str] = field(default_factory=list)


@dataclass
class SOP(KnowledgeDocument):
    """标准操作流程。"""
    document_type: str = "sop"
    steps: List[str] = field(default_factory=list)
    prerequisites: List[str] = field(default_factory=list)
    estimated_duration_minutes: int = 0


@dataclass
class Runbook(KnowledgeDocument):
    """运维手册。"""
    document_type: str = "runbook"
    category: str = ""
    severity: str = "P3"
    services_affected: List[str] = field(default_factory=list)


@dataclass
class FailurePattern(KnowledgeDocument):
    """故障模式。"""
    document_type: str = "failure_pattern"
    symptoms: List[str] = field(default_factory=list)
    root_cause: str = ""
    related_patterns: List[str] = field(default_factory=list)
    recommended_actions: List[str] = field(default_factory=list)


@dataclass
class Incident(KnowledgeDocument):
    """事件记录。"""
    document_type: str = "incident"
    severity: str = "P3"
    duration_minutes: int = 0
    services_affected: List[str] = field(default_factory=list)
    resolution: str = ""


@dataclass
class RCA(KnowledgeDocument):
    """根因分析。"""
    document_type: str = "rca"
    finding: str = ""
    recommendation: str = ""
    related_incidents: List[str] = field(default_factory=list)
