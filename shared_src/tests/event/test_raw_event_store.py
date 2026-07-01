import pytest
from datetime import datetime
from shared_src.event.envelope import EventEnvelope
from shared_src.event.raw_event_store import RawEventStore


class TestRawEventStore:
    def test_append_and_read(self):
        store = RawEventStore()
        env = EventEnvelope(
            event_id="e1",
            event_type="raw.log",
            payload=b"test log line",
        )
        store.append(env)
        retrieved = store.read("e1")
        assert retrieved is not None
        assert retrieved.event_id == "e1"
        assert retrieved.payload == b"test log line"

    def test_read_nonexistent(self):
        store = RawEventStore()
        assert store.read("nonexistent") is None

    def test_immutable_append_only(self):
        """写入后的 Event 不能修改"""
        store = RawEventStore()
        env = EventEnvelope(event_id="e1", payload=b"original")
        store.append(env)
        env.payload = b"modified"
        retrieved = store.read("e1")
        assert retrieved.payload == b"original"

    def test_replay_all(self):
        store = RawEventStore()
        for i in range(5):
            store.append(EventEnvelope(event_id=f"e{i}", payload=str(i).encode()))
        events = list(store.replay())
        assert len(events) == 5
        assert events[0].event_id == "e0"
