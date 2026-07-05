"""InteractionProjector — 从 NormalizedEvent 提取交互关系并补全上下文。"""
import json
import uuid
from datetime import datetime
from typing import Dict, List, Optional, Any

from shared_src.event.envelope import EventEnvelope
from shared_src.event.bus import EventBus
from shared_src.event.schema_registry import SchemaRegistry


INSTANCE = "INSTANCE"
HOST = "HOST"
SERVICE = "SERVICE"


class InteractionProjector:
    """
    Interaction Projector——提取服务间的交互关系。

    从 NormalizedEvent 解析：
    - 主实体（service/instance）
    - host 上下文（作为交互目标）
    - 事件名称中的交互目标
    - 上下文字段：request_id, host, namespace, pod_name, instance 等

    产出 interaction.observed 事件，携带完整上下文供 CorrelationEngine 使用。
    """

    def __init__(self, schema_registry: SchemaRegistry, bus: EventBus):
        self.schema_registry = schema_registry
        self.bus = bus

    def process(self, envelope: EventEnvelope) -> None:
        payload = json.loads(envelope.payload.decode("utf-8"))
        entity = payload.get("entity", {})
        context = payload.get("context", {})
        event_data = payload.get("event", {})

        entity_type = (entity.get("type", "") or "").upper()
        entity_name = entity.get("name", "") or ""
        entity_instance = entity.get("instance", "") or ""
        host = context.get("host", "") or ""
        event_name = event_data.get("name", "") or ""
        event_raw = event_data.get("raw", "") or ""

        if not entity_type and not entity_name:
            return

        # Source: 主实体
        source = {
            "type": entity_type.upper() if entity_type else SERVICE,
            "name": entity_name,
            "instance": entity_instance,
        }

        # Target: host 或 event 中提到的服务
        target = None
        if host:
            target = {"type": HOST, "name": host, "instance": ""}

        # 如果 event.raw 包含 "to <service>" 模式，提取目标
        if not target and event_raw:
            for svc in ["neutron", "cinder", "glance", "keystone", "nova"]:
                if svc in event_raw.lower():
                    target = {"type": SERVICE, "name": svc, "instance": ""}
                    break

        if not target:
            # 默认：service <-> host 交互
            if host:
                target = {"type": HOST, "name": host, "instance": ""}
            else:
                return  # 无法推断交互

        # 从 normalized event 中提取 K8s 上下文（安全取值）
        k8s_ctx = context.get("k8s", {}) or {}

        # 构建完整交互事件，携带丰富的上下文
        interaction_payload = {
            "source": source,
            "target": target,
            "event_name": event_name,
            "timestamp": envelope.timestamp.isoformat() if hasattr(envelope.timestamp, "isoformat") else str(envelope.timestamp),
            "source_event_id": envelope.event_id,

            # 从 normalized event 提取的上下文（参考值：request_id, global_request_id, host, instance, namespace, pod_name）
            "request_id": payload.get("openstack_request_id", ""),
            "global_request_id": payload.get("openstack_global_request_id", ""),
            "host": host,
            "instance": entity_instance,
            "namespace": k8s_ctx.get("namespace", ""),
            "pod_name": k8s_ctx.get("pod", ""),
        }

        interaction_env = EventEnvelope(
            schema_version=1,
            event_type="interaction.observed",
            producer="semantic-engine",
            event_id=uuid.uuid4().hex,
            parent_event_ids=[envelope.event_id],
            timestamp=datetime.utcnow(),
            payload=json.dumps(interaction_payload).encode("utf-8"),
            metadata={"source_type": "normalized.event"},
        )

        self.bus.publish("platform.interaction", interaction_env)
