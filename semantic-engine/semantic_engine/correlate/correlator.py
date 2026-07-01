"""CorrelationEngine — 基于交互模式的相关性分析。"""
import json
from typing import List, Optional, Any
from shared_src.event.envelope import EventEnvelope
from shared_src.event.bus import EventBus
from .dynamic_rel_projection import DynamicRelProjection


class CorrelationEngine:
    """
    Correlation Engine——基于交互模式推断服务间相关性。

    - 监听 interaction.observed 事件
    - 更新 DynamicRelProjection
    - 基于交互频率产出相关性 Finding
    """

    def __init__(
        self,
        rel_projection: DynamicRelProjection,
        bus: EventBus,
        frequency_threshold: int = 5,
        time_window: str = "1 HOUR",
        min_confidence: float = 0.55,
        failure_pattern: str = "high_frequency_interaction",
    ):
        self.rel_projection = rel_projection
        self.bus = bus
        self.frequency_threshold = frequency_threshold
        self.time_window = time_window
        self.min_confidence = min_confidence
        self.failure_pattern = failure_pattern

    def process(self, envelope: EventEnvelope) -> List[dict]:
        """处理一个 Event，返回相关性 Finding 列表。"""
        if envelope.event_type != "interaction.observed":
            return []

        payload = json.loads(envelope.payload.decode("utf-8"))
        source = payload.get("source", {})
        target = payload.get("target", {})

        source_name = source.get("name", "")
        target_name = target.get("name", "")

        if not source_name or not target_name:
            return []

        # 记录交互（带 failure_pattern 标记）
        self.rel_projection.record_interaction(
            source_name, target_name, failure_pattern=self.failure_pattern
        )

        # 检查交互频率是否超过阈值（按当前 failure_pattern 过滤）
        trend = self.rel_projection.query_trend(
            source_name, target_name, [self.time_window],
            failure_pattern=self.failure_pattern,
        )
        freq = trend[0] if trend else 0

        if freq >= self.frequency_threshold:
            confidence = min(
                self.min_confidence + freq * 0.05,
                0.95,
            )
            return [
                {
                    "category": "correlation.found",
                    "failure_pattern": self.failure_pattern,
                    "hypothesis": (
                        f"{source_name} 与 {target_name} "
                        f"在 {self.time_window} 内交互 {freq} 次"
                    ),
                    "confidence": confidence,
                    "severity": "info",
                    "evidence": [
                        f"interaction_frequency={freq}",
                        f"time_window={self.time_window}",
                        f"source={source_name}",
                        f"target={target_name}",
                        f"failure_pattern={self.failure_pattern}",
                    ],
                    "affected_entities": [source_name, target_name],
                }
            ]

        return []
