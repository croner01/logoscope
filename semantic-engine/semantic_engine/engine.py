"""SemanticEngine — 核心推理引擎。

消费 EventEnvelope（来自 platform.raw）：
1. 通过 EventPipeline 处理
2. 调用 normalizer.normalize() 标准化
3. 产出 EventEnvelope 到 platform.normalized
"""
import json
import uuid
from datetime import datetime
from typing import Any, Dict, Optional

from shared_src.event.envelope import EventEnvelope, serialize_envelope
from shared_src.event.bus import EventBus
from shared_src.event.schema_registry import SchemaRegistry
from shared_src.pipeline.processors import EventPipeline


class SemanticEngine:
    """Semantic Engine——消费 EventEnvelope，产出 Normalized Event。"""

    def __init__(
        self,
        bus: EventBus,
        schema_registry: SchemaRegistry,
        pipeline: Optional[EventPipeline] = None,
        normalizer: Any = None,
        producer_name: str = "semantic-engine",
    ):
        self.bus = bus
        self.schema_registry = schema_registry
        self.pipeline = pipeline or EventPipeline([])
        self.normalizer = normalizer
        self.producer_name = producer_name
        self._stats = {"events_processed": 0, "last_event_type": ""}

    def process(self, envelope: EventEnvelope) -> EventEnvelope:
        """处理一个 Raw EventEnvelope，返回 Normalized EventEnvelope。"""
        # 1. 解析 payload
        payload_data = json.loads(envelope.payload.decode("utf-8"))

        # 2. 通过 Pipeline 预处理
        pipeline_results = self.pipeline.execute(payload_data)
        if not pipeline_results:
            # 被 pipeline 缓冲/过滤掉了
            normalized_data = payload_data
        else:
            normalized_data = pipeline_results[0]

        # 3. 标准化
        if self.normalizer and hasattr(self.normalizer, "normalize"):
            normalized = self.normalizer.normalize(normalized_data)
        else:
            normalized = normalized_data

        # 4. 创建输出 EventEnvelope
        output_env = EventEnvelope(
            schema_version=1,
            event_type="normalized.event",
            producer=self.producer_name,
            event_id=uuid.uuid4().hex,
            parent_event_ids=[envelope.event_id] + envelope.parent_event_ids,
            timestamp=datetime.utcnow(),
            payload=json.dumps(normalized, default=str).encode("utf-8"),
            metadata={"source_event_id": envelope.event_id},
        )

        # 5. 发布到 platform.normalized
        self.bus.publish("platform.normalized", output_env)

        # 6. 更新统计
        self._stats["events_processed"] += 1
        self._stats["last_event_type"] = envelope.event_type

        return output_env

    def status(self) -> Dict[str, Any]:
        """返回处理统计。"""
        return dict(self._stats)
