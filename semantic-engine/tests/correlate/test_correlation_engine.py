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
        """CorrelationEngine 在高频交互时生成相关性 Finding"""
        bus = InMemoryEventBus()
        engine = CorrelationEngine(rel_projection=DynamicRelProjection(), bus=bus,
                                    frequency_threshold=3)

        # 发送 3 次交互，超过阈值
        for i in range(3):
            env = EventEnvelope(
                event_id=f"e{i}",
                event_type="interaction.observed",
                payload=json.dumps({
                    "source": {"type": "SERVICE", "name": "nova-api"},
                    "target": {"type": "SERVICE", "name": "neutron-server"},
                }).encode(),
            )
            findings = engine.process(env)

        # 第 3 次应该触发 Finding
        assert len(findings) == 1
        f = findings[0]
        assert f["category"] == "correlation.found"
        assert "nova-api" in f["hypothesis"]
        assert "neutron-server" in f["hypothesis"]
        assert f["severity"] == "info"
        assert f["confidence"] >= 0.6
        assert "nova-api" in f["affected_entities"]
        assert "neutron-server" in f["affected_entities"]

    def test_low_frequency_no_finding(self):
        """低频交互不应生成 Finding"""
        bus = InMemoryEventBus()
        engine = CorrelationEngine(rel_projection=DynamicRelProjection(), bus=bus,
                                    frequency_threshold=5)

        # 只发送 2 次交互，低于阈值
        for i in range(2):
            env = EventEnvelope(
                event_id=f"e{i}",
                event_type="interaction.observed",
                payload=json.dumps({
                    "source": {"type": "SERVICE", "name": "svc-A"},
                    "target": {"type": "SERVICE", "name": "svc-B"},
                }).encode(),
            )
            findings = engine.process(env)
            # 前 4 次都不应触发
            assert len(findings) == 0

    def test_frequent_interaction_correlation(self):
        """高频交互触发相关性分析"""
        bus = InMemoryEventBus()
        engine = CorrelationEngine(rel_projection=DynamicRelProjection(), bus=bus,
                                    frequency_threshold=5)

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

        # 验证趋势
        rel = engine.rel_projection
        trend = rel.query_trend("svc-A", "svc-B", windows=["1 HOUR"])
        assert trend[0] >= 5

        # 验证第 5 次生成了 Finding
        env = EventEnvelope(
            event_id="e5",
            event_type="interaction.observed",
            payload=json.dumps({
                "source": {"type": "SERVICE", "name": "svc-A"},
                "target": {"type": "SERVICE", "name": "svc-B"},
            }).encode(),
        )
        findings = engine.process(env)
        assert len(findings) == 1
        assert findings[0]["category"] == "correlation.found"

    def test_zero_interaction(self):
        """无关事件类型不产生任何 Finding"""
        bus = InMemoryEventBus()
        engine = CorrelationEngine(rel_projection=DynamicRelProjection(), bus=bus)
        env = EventEnvelope(event_id="e1", event_type="normalized.event",
                             payload=json.dumps({"event": {"type": "log"}}).encode())
        findings = engine.process(env)
        assert len(findings) == 0

    def test_empty_source_target_returns_no_finding(self):
        """缺少 source/target 的交互不生成 Finding"""
        bus = InMemoryEventBus()
        engine = CorrelationEngine(rel_projection=DynamicRelProjection(), bus=bus)

        env = EventEnvelope(
            event_id="e1",
            event_type="interaction.observed",
            payload=json.dumps({
                "source": {"type": "SERVICE", "name": ""},
                "target": {"type": "SERVICE", "name": "svc-B"},
            }).encode(),
        )
        findings = engine.process(env)
        assert len(findings) == 0

    def test_confidence_scales_with_frequency(self):
        """交互频率越高，confidence 越高"""
        bus = InMemoryEventBus()
        engine = CorrelationEngine(rel_projection=DynamicRelProjection(), bus=bus,
                                    frequency_threshold=3)

        # 3 次交互 -> threshold
        for i in range(3):
            env = EventEnvelope(
                event_id=f"e{i}",
                event_type="interaction.observed",
                payload=json.dumps({
                    "source": {"type": "SERVICE", "name": "svc-A"},
                    "target": {"type": "SERVICE", "name": "svc-B"},
                }).encode(),
            )
            findings = engine.process(env)

        confidence_3 = findings[0]["confidence"]

        # 再发 5 次 -> confidence 应更高
        for i in range(3, 8):
            env = EventEnvelope(
                event_id=f"e{i}",
                event_type="interaction.observed",
                payload=json.dumps({
                    "source": {"type": "SERVICE", "name": "svc-A"},
                    "target": {"type": "SERVICE", "name": "svc-B"},
                }).encode(),
            )
            findings = engine.process(env)

        assert findings[0]["confidence"] > confidence_3
