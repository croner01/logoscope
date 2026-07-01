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

    def __init__(self, rel_projection: DynamicRelProjection, bus: EventBus):
        self.rel_projection = rel_projection
        self.bus = bus

    def process(self, envelope: EventEnvelope) -> List[dict]:
        """处理一个 Event，返回相关性 Finding 列表。"""
        if envelope.event_type != "interaction.observed":
            return []

        payload = json.loads(envelope.payload.decode("utf-8"))
        source = payload.get("source", {})
        target = payload.get("target", {})

        source_name = source.get("name", "")
        target_name = target.get("name", "")

        if source_name and target_name:
            self.rel_projection.record_interaction(
                source_name,
                target_name,
            )

        return []
