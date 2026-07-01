import pytest
import json
from datetime import datetime
from shared_src.event.envelope import EventEnvelope, serialize_envelope, deserialize_envelope

class TestEventEnvelope:
    def test_create_envelope(self):
        """EventEnvelope 创建并携带所有字段"""
        env = EventEnvelope(
            envelope_version="v1",
            schema_version=1,
            event_type="raw.log",
            producer="ingest-service",
            event_id="test-001",
            parent_event_ids=[],
            timestamp=datetime(2026, 7, 1, 12, 0, 0),
            payload=b'{"key": "value"}',
            metadata={"cluster": "prod"},
        )
        assert env.envelope_version == "v1"
        assert env.schema_version == 1
        assert env.event_type == "raw.log"
        assert env.producer == "ingest-service"
        assert env.event_id == "test-001"
        assert env.parent_event_ids == []
        assert env.metadata["cluster"] == "prod"

    def test_serialize_deserialize_roundtrip(self):
        """序列化后再反序列化，字段不变"""
        env = EventEnvelope(
            schema_version=1,
            event_type="raw.log",
            producer="test",
            event_id="test-001",
            payload=json.dumps({"key": "value"}).encode(),
        )
        data = serialize_envelope(env)
        restored = deserialize_envelope(data)
        assert restored.schema_version == env.schema_version
        assert restored.event_type == env.event_type
        assert restored.event_id == env.event_id
        assert json.loads(restored.payload) == {"key": "value"}

    def test_parent_event_ids_lineage(self):
        """parent_event_ids 构建血缘链"""
        raw = EventEnvelope(event_id="raw-001", parent_event_ids=[])
        norm = EventEnvelope(
            event_id="norm-001",
            parent_event_ids=[raw.event_id],
        )
        finding = EventEnvelope(
            event_id="finding-001",
            parent_event_ids=[norm.event_id] + norm.parent_event_ids,
        )
        assert "raw-001" in finding.parent_event_ids
        assert "norm-001" in finding.parent_event_ids
        assert len(finding.parent_event_ids) == 2

    def test_producer_tracking(self):
        """producer 字段追踪谁产生了这个 Event"""
        env = EventEnvelope(
            event_id="e1",
            event_type="normalized.event",
            producer="semantic-engine",
        )
        assert env.producer == "semantic-engine"
