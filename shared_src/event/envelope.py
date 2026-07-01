from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Any
import json


@dataclass
class EventEnvelope:
    """所有 Event 的统一容器。parent_event_ids 构成血缘链。"""
    envelope_version: str = "v1"
    schema_version: int = 1
    event_type: str = ""
    producer: str = ""
    event_id: str = ""
    parent_event_ids: List[str] = field(default_factory=list)
    timestamp: datetime = field(default_factory=datetime.utcnow)
    payload: bytes = b""
    metadata: Dict[str, str] = field(default_factory=dict)


def serialize_envelope(env: EventEnvelope) -> bytes:
    """序列化 EventEnvelope 为 JSON bytes。"""
    return json.dumps({
        "envelope_version": env.envelope_version,
        "schema_version": env.schema_version,
        "event_type": env.event_type,
        "producer": env.producer,
        "event_id": env.event_id,
        "parent_event_ids": env.parent_event_ids,
        "timestamp": env.timestamp.isoformat(),
        "payload": env.payload.decode("utf-8", errors="replace"),
        "metadata": env.metadata,
    }).encode("utf-8")


def deserialize_envelope(data: bytes) -> EventEnvelope:
    """从 JSON bytes 反序列化为 EventEnvelope。"""
    obj = json.loads(data.decode("utf-8"))
    payload = obj.get("payload", "")
    return EventEnvelope(
        envelope_version=obj.get("envelope_version", "v1"),
        schema_version=obj.get("schema_version", 1),
        event_type=obj.get("event_type", ""),
        producer=obj.get("producer", ""),
        event_id=obj.get("event_id", ""),
        parent_event_ids=obj.get("parent_event_ids", []),
        timestamp=datetime.fromisoformat(obj.get("timestamp", datetime.utcnow().isoformat())),
        payload=payload.encode("utf-8") if isinstance(payload, str) else payload,
        metadata=obj.get("metadata", {}),
    )
