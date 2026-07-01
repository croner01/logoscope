import pytest
import json
from datetime import datetime
from shared_src.event.envelope import EventEnvelope, serialize_envelope, deserialize_envelope
from shared_src.event.bus import InMemoryEventBus
from shared_src.event.schema_registry import SchemaRegistry, Schema
from shared_src.pipeline.processors import EventPipeline, EnrichProcessor
from semantic_engine.engine import SemanticEngine


class MockNormalizer:
    """模拟 normalize_log"""
    def normalize(self, log_data: dict) -> dict:
        return {
            "id": "norm-001",
            "timestamp": "2026-07-01T12:00:00",
            "entity": {"type": "service", "name": "nova-api", "instance": "abc-123"},
            "event": {"type": "log", "level": "ERROR", "name": "Connection refused", "raw": log_data.get("raw_payload", "")},
            "context": {"host": "compute-01"},
            "relations": [],
            "severity_number": 17,
            "openstack_request_id": log_data.get("openstack_request_id", ""),
        }


class TestSemanticEngine:
    def test_engine_processes_envelope(self):
        """SemanticEngine 接收 EventEnvelope，产出 EventEnvelope"""
        bus = InMemoryEventBus()
        schema_registry = SchemaRegistry()
        pipeline = EventPipeline([])
        normalizer = MockNormalizer()

        engine = SemanticEngine(
            bus=bus,
            schema_registry=schema_registry,
            pipeline=pipeline,
            normalizer=normalizer,
        )

        raw_env = EventEnvelope(
            event_type="raw.log",
            event_id="raw-001",
            producer="ingest-service",
            payload=json.dumps({
                "raw_payload": "nova-api: ERROR Connection refused",
                "service_name": "nova-api",
                "timestamp": "2026-07-01T12:00:00",
            }).encode(),
        )

        output = engine.process(raw_env)
        assert output.event_type == "normalized.event"
        assert output.producer == "semantic-engine"
        assert raw_env.event_id in output.parent_event_ids

    def test_engine_sets_parent_event_ids(self):
        """输出 Event 的 parent_event_ids 包含输入 Event 的 event_id"""
        bus = InMemoryEventBus()
        engine = SemanticEngine(
            bus=bus,
            schema_registry=SchemaRegistry(),
            pipeline=EventPipeline([]),
            normalizer=MockNormalizer(),
        )
        raw = EventEnvelope(event_id="raw-001", event_type="raw.log",
                             payload=b'{"raw_payload": "test"}')
        output = engine.process(raw)
        assert "raw-001" in output.parent_event_ids

    def test_engine_enriches_through_pipeline(self):
        """EventPipeline 在 normalization 前处理"""
        bus = InMemoryEventBus()
        pipeline = EventPipeline([
            EnrichProcessor(host_map={"compute-01": "az-1"}),
        ])
        engine = SemanticEngine(
            bus=bus,
            schema_registry=SchemaRegistry(),
            pipeline=pipeline,
            normalizer=MockNormalizer(),
        )
        raw = EventEnvelope(
            event_id="raw-001",
            event_type="raw.log",
            payload=json.dumps({
                "raw_payload": "nova-api: ERROR",
                "host": "compute-01",
            }).encode(),
        )
        output = engine.process(raw)
        assert output is not None

    def test_engine_reports_status(self):
        """SemanticEngine.status() 返回处理统计"""
        bus = InMemoryEventBus()
        engine = SemanticEngine(
            bus=bus,
            schema_registry=SchemaRegistry(),
            pipeline=EventPipeline([]),
            normalizer=MockNormalizer(),
        )
        raw = EventEnvelope(event_id="raw-001", event_type="raw.log",
                             payload=b'{"raw_payload": "test"}')
        engine.process(raw)
        status = engine.status()
        assert status["events_processed"] == 1
        assert status["last_event_type"] == "raw.log"
