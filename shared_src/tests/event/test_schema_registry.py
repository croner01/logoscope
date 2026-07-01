import pytest
import json
from shared_src.event.schema_registry import SchemaRegistry, Schema, SchemaMigrationError
from shared_src.event.envelope import EventEnvelope


class TestSchemaRegistry:
    def test_register_and_latest_version(self):
        registry = SchemaRegistry()
        registry.register("normalized.event", 1, Schema(
            event_type="normalized.event", version=1,
            fields={"event_id": str, "message": str},
        ))
        registry.register("normalized.event", 2, Schema(
            event_type="normalized.event", version=2,
            fields={"event_id": str, "message": str, "tenant_id": str},
        ))
        assert registry.latest_version("normalized.event") == 2

    def test_migration_v1_to_v2(self):
        registry = SchemaRegistry()
        registry.register("normalized.event", 1, Schema(
            event_type="normalized.event", version=1,
            fields={"event_id": str, "message": str},
        ))
        registry.register("normalized.event", 2, Schema(
            event_type="normalized.event", version=2,
            fields={"event_id": str, "message": str, "tenant_id": str},
        ))
        registry.register_migration(
            "normalized.event", 1, 2,
            migrate_fn=lambda p: {**p, "tenant_id": ""},
        )
        env = EventEnvelope(
            schema_version=1,
            event_type="normalized.event",
            payload=json.dumps({"event_id": "e1", "message": "test"}).encode(),
        )
        payload = registry.deserialize(env)
        assert "tenant_id" in payload
        assert payload["tenant_id"] == ""

    def test_deserialize_latest_version_no_migration(self):
        """最新版本不需要迁移"""
        registry = SchemaRegistry()
        registry.register("raw.log", 1, Schema(
            event_type="raw.log", version=1,
            fields={"raw_id": str, "raw_payload": str},
        ))
        env = EventEnvelope(
            schema_version=1,
            event_type="raw.log",
            payload=json.dumps({"raw_id": "r1", "raw_payload": "test"}).encode(),
        )
        payload = registry.deserialize(env)
        assert payload["raw_id"] == "r1"

    def test_missing_migration_raises_error(self):
        registry = SchemaRegistry()
        registry.register("test.event", 1, Schema(
            event_type="test.event", version=1, fields={"key": str},
        ))
        registry.register("test.event", 3, Schema(
            event_type="test.event", version=3, fields={"key": str, "extra": str},
        ))
        env = EventEnvelope(
            schema_version=1,
            event_type="test.event",
            payload=json.dumps({"key": "val"}).encode(),
        )
        with pytest.raises(SchemaMigrationError):
            registry.deserialize(env)

    def test_validate_payload(self):
        registry = SchemaRegistry()
        registry.register("test.event", 1, Schema(
            event_type="test.event", version=1,
            fields={"id": str, "count": int},
        ))
        assert registry.validate("test.event", 1, {"id": "x", "count": 1})
        assert not registry.validate("test.event", 1, {"id": "x"})  # 缺少 count
        assert not registry.validate("test.event", 1, {"id": 123, "count": 1})  # id 类型错误
