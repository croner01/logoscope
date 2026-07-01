"""Eventized feedback — EvaluationEvent → LearningEvent。"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class EvaluationEvent:
    """
    评估事件——记录一次 Capability 执行的结果。

    发布到 platform.system topic，由 CapabilityStatsProjector 消费。
    """
    event_id: str = ""
    episode_id: str = ""
    capability_id: str = ""
    outcome: str = "unknown"  # "success", "failure", "unknown"
    duration_ms: int = 0
    error_message: str = ""
    timestamp: datetime = field(default_factory=datetime.utcnow)


@dataclass
class LearningEvent:
    """
    学习事件——用于 ExperienceGraphProjection 构建统计。

    发布到 platform.system topic。
    v15: 包含 failure_pattern 维度。
    """
    event_id: str = ""
    episode_id: str = ""
    failure_pattern: str = ""  # "RabbitMQHeartbeatLost", "NovaOOM", 等
    capability_id: str = ""
    env_fingerprint: str = ""
    outcome: str = "unknown"
    timestamp: datetime = field(default_factory=datetime.utcnow)
