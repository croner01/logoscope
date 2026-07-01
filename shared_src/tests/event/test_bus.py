import pytest
from shared_src.event.envelope import EventEnvelope
from shared_src.event.bus import InMemoryEventBus


class TestEventBus:
    def test_publish_and_subscribe(self):
        bus = InMemoryEventBus()
        received = []

        def callback(env):
            received.append(env)

        bus.subscribe("platform.raw", "test-group", callback)
        env = EventEnvelope(event_id="e1", event_type="raw.log")
        bus.publish("platform.raw", env)
        assert len(received) == 1
        assert received[0].event_id == "e1"

    def test_topic_isolation(self):
        """不同 topic 不互相干扰"""
        bus = InMemoryEventBus()
        received = []
        bus.subscribe("platform.raw", "g1", lambda e: received.append(e))
        bus.publish("platform.normalized", EventEnvelope(event_id="e1"))
        assert len(received) == 0

    def test_multiple_subscribers(self):
        bus = InMemoryEventBus()
        r1, r2 = [], []
        bus.subscribe("platform.raw", "g1", lambda e: r1.append(e))
        bus.subscribe("platform.raw", "g2", lambda e: r2.append(e))
        bus.publish("platform.raw", EventEnvelope(event_id="e1"))
        assert len(r1) == 1
        assert len(r2) == 1
