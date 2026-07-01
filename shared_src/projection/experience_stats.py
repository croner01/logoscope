"""ExperienceStats — 按 (failure_pattern, capability_id, env_fingerprint) 索引的统计。"""
from dataclasses import dataclass


@dataclass
class ExperienceStats:
    """
    统计经验——按 (failure_pattern, capability_id, env_fingerprint) 索引。

    v15: 增加 failure_pattern 维度。
    不同故障场景的相同操作有不同成功率。
    "RabbitMQHeartbeatLost 时 restart 成功率" vs "NovaOOM 时 restart 成功率"
    """
    failure_pattern: str = ""
    capability_id: str = ""
    env_fingerprint: str = ""
    total_executions: int = 0
    success_count: int = 0
    failure_count: int = 0
    avg_duration_ms: float = 0.0

    @property
    def key(self) -> str:
        return f"{self.failure_pattern}|{self.capability_id}|{self.env_fingerprint}"

    @property
    def success_rate(self) -> float:
        return self.success_count / self.total_executions if self.total_executions else 0.0
