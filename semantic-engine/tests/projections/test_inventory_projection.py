import pytest
from shared_src.event.envelope import EventEnvelope
from semantic_engine.projections.inventory_projection import InventoryProjection

SERVICE = "SERVICE"
INSTANCE = "INSTANCE"
HOST = "HOST"


class TestInventoryProjection:
    def test_inventory_rebuild(self):
        """InventoryProjection 可以从事件流重建"""
        projection = InventoryProjection(epoch="20260701")
        events = [
            EventEnvelope(
                event_id="e1",
                event_type="entity.seen",
                payload=b'{"entity_type": "INSTANCE", "entity_name": "abc-123", "entity_instance": "nova-api"}',
            ),
            EventEnvelope(
                event_id="e2",
                event_type="entity.seen",
                payload=b'{"entity_type": "INSTANCE", "entity_name": "abc-456", "entity_instance": "neutron-server"}',
            ),
        ]
        for e in events:
            projection.apply(e)
        assert projection.query("INSTANCE", "abc-123") is not None
        assert projection.query("INSTANCE", "abc-456") is not None

    def test_query_nonexistent(self):
        projection = InventoryProjection(epoch="20260701")
        assert projection.query("INSTANCE", "nonexistent") is None

    def test_inventory_update_existing(self):
        """相同 entity_id 的多次出现只更新不重复"""
        projection = InventoryProjection(epoch="20260701")
        projection.apply(EventEnvelope(
            event_id="e1",
            event_type="entity.seen",
            payload=b'{"entity_type": "SERVICE", "entity_name": "nova-api", "entity_instance": "v1"}',
        ))
        projection.apply(EventEnvelope(
            event_id="e2",
            event_type="entity.seen",
            payload=b'{"entity_type": "SERVICE", "entity_name": "nova-api", "entity_instance": "v2"}',
        ))
        record = projection.query("SERVICE", "nova-api")
        assert record is not None
        assert record["entity_instance"] == "v2"  # 更新为最新

    def test_rebuild_clears_and_reloads(self):
        """rebuild 清除旧数据后重新加载"""
        projection = InventoryProjection(epoch="20260701")
        projection.apply(EventEnvelope(
            event_id="e1",
            event_type="entity.seen",
            payload=b'{"entity_type": "SERVICE", "entity_name": "old-svc", "entity_instance": "v1"}',
        ))
        assert projection.query("SERVICE", "old-svc") is not None
        # rebuild
        new_events = [
            EventEnvelope(
                event_id="e2",
                event_type="entity.seen",
                payload=b'{"entity_type": "SERVICE", "entity_name": "new-svc", "entity_instance": "v2"}',
            ),
        ]
        projection.rebuild(new_events)
        assert projection.query("SERVICE", "old-svc") is None  # 清除
        assert projection.query("SERVICE", "new-svc") is not None  # 重新加载

    def test_inventory_total_count(self):
        projection = InventoryProjection(epoch="20260701")
        projection.apply(EventEnvelope(
            event_id="e1", event_type="entity.seen",
            payload=b'{"entity_type": "SERVICE", "entity_name": "svc1", "entity_instance": "i1"}',
        ))
        projection.apply(EventEnvelope(
            event_id="e2", event_type="entity.seen",
            payload=b'{"entity_type": "SERVICE", "entity_name": "svc2", "entity_instance": "i2"}',
        ))
        assert projection.total_count() == 2
