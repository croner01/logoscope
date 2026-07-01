import pytest
from datetime import datetime, timedelta
from shared_src.event.envelope import EventEnvelope
from shared_src.event.raw_event_store import RawEventStore
from shared_src.worldview.history_query import HistoryQuery


class TestHistoryQuery:
    def test_get_recent_events(self):
        store = RawEventStore()
        for i in range(10):
            env = EventEnvelope(
                event_id=f"e{i:03d}",
                event_type="normalized.event" if i > 0 else "raw.log",
                payload=f"event-{i}".encode(),
                metadata={"resource": "SERVICE:rabbitmq" if i % 2 == 0 else "SERVICE:nova-api"},
            )
            store.append(env)

        hq = HistoryQuery(store)
        events = hq.get_recent_events(count=5)
        assert len(events) == 5
        assert events[0].event_id == "e005"  # 最近的第 1 条

    def test_get_alarms(self):
        store = RawEventStore()
        for i in range(3):
            env = EventEnvelope(
                event_id=f"alarm-{i}",
                event_type="alert.triggered",
                payload=f"Alarm {i}".encode(),
            )
            store.append(env)
        # 非 alarm 事件
        store.append(EventEnvelope(event_id="norm-1", event_type="normalized.event"))

        hq = HistoryQuery(store)
        alarms = hq.get_alarms()
        assert len(alarms) == 3

    def test_get_events_by_type(self):
        store = RawEventStore()
        store.append(EventEnvelope(event_id="r1", event_type="raw.log"))
        store.append(EventEnvelope(event_id="n1", event_type="normalized.event"))
        store.append(EventEnvelope(event_id="n2", event_type="normalized.event"))
        store.append(EventEnvelope(event_id="r2", event_type="raw.log"))

        hq = HistoryQuery(store)
        normalized = hq.get_events_by_type("normalized.event")
        assert len(normalized) == 2

    def test_empty_store(self):
        store = RawEventStore()
        hq = HistoryQuery(store)
        assert hq.get_recent_events(count=10) == []
        assert hq.get_alarms() == []
        assert hq.get_events_by_type("any") == []
