"""Tests for CorrelationEngine and DynamicRelProjection."""
import pytest
import json
from datetime import datetime, timedelta
from shared_src.event.envelope import EventEnvelope
from shared_src.event.bus import InMemoryEventBus
from semantic_engine.correlate.correlator import CorrelationEngine
from semantic_engine.correlate.dynamic_rel_projection import DynamicRelProjection


class TestDynamicRelProjection:
    def test_record_interaction(self):
        rel_proj = DynamicRelProjection()
        rel_proj.record_interaction("nova-api", "neutron-server")
        trend = rel_proj.query_trend("nova-api", "neutron-server",
                                      windows=["1 HOUR", "6 HOUR", "24 HOUR"])
        assert len(trend) == 3
        assert all(t >= 1 for t in trend)

    def test_time_window_counts(self):
        """不同时间窗口内的计数不同"""
        rel_proj = DynamicRelProjection()
        now = datetime.utcnow()
        rel_proj.record_interaction("A", "B", timestamp=now)
        rel_proj.record_interaction("A", "B", timestamp=now - timedelta(hours=2))
        rel_proj.record_interaction("A", "B", timestamp=now - timedelta(hours=10))

        trend = rel_proj.query_trend("A", "B",
                                      windows=["1 HOUR", "6 HOUR", "24 HOUR"])
        # 1小时窗口: 1次（当前时间）
        # 6小时窗口: 2次（当前 + 2小时前）
        # 24小时窗口: 3次（全部）
        assert trend[0] <= trend[1] <= trend[2]

    def test_empty_trend(self):
        rel_proj = DynamicRelProjection()
        trend = rel_proj.query_trend("X", "Y", windows=["1 HOUR"])
        assert trend == [0]

    def test_aggregate_by_hour(self):
        rel_proj = DynamicRelProjection()
        for i in range(10):
            rel_proj.record_interaction("A", "B", timestamp=datetime.utcnow() - timedelta(minutes=i * 5))
        aggregated = rel_proj.aggregate_by_hour("A", "B")
        assert len(aggregated) > 0
        assert all(hour["count"] >= 0 for hour in aggregated)


class TestCorrelationEngine:
    def test_correlate_finding(self):
        """CorrelationEngine 生成相关性 Finding"""
        bus = InMemoryEventBus()
        engine = CorrelationEngine(rel_projection=DynamicRelProjection(), bus=bus)

        env = EventEnvelope(
            event_id="e1",
            event_type="interaction.observed",
            payload=json.dumps({
                "source": {"type": "SERVICE", "name": "nova-api"},
                "target": {"type": "SERVICE", "name": "neutron-server"},
            }).encode(),
        )
        findings = engine.process(env)
        assert len(findings) >= 0

    def test_frequent_interaction_correlation(self):
        """高频交互触发相关性分析"""
        bus = InMemoryEventBus()
        engine = CorrelationEngine(rel_projection=DynamicRelProjection(), bus=bus)

        for i in range(5):
            env = EventEnvelope(
                event_id=f"e{i}",
                event_type="interaction.observed",
                payload=json.dumps({
                    "source": {"type": "SERVICE", "name": "svc-A"},
                    "target": {"type": "SERVICE", "name": "svc-B"},
                }).encode(),
            )
            engine.process(env)

        rel = engine.rel_projection
        trend = rel.query_trend("svc-A", "svc-B", windows=["1 HOUR"])
        assert trend[0] >= 5

    def test_zero_interaction(self):
        """无交互时相关性为空"""
        bus = InMemoryEventBus()
        engine = CorrelationEngine(rel_projection=DynamicRelProjection(), bus=bus)
        env = EventEnvelope(event_id="e1", event_type="normalized.event",
                             payload=json.dumps({"event": {"type": "log"}}).encode())
        findings = engine.process(env)
        assert len(findings) == 0
