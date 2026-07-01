"""Tests for CorrelationEngine and DynamicRelProjection."""
import pytest
import json
from datetime import datetime, timedelta
from unittest.mock import MagicMock
from shared_src.event.envelope import EventEnvelope
from shared_src.event.bus import InMemoryEventBus
from semantic_engine.correlate.correlator import CorrelationEngine
from semantic_engine.correlate.dynamic_rel_projection import DynamicRelProjection


class MockClickHouseClient:
    """模拟 ClickHouse 原生客户端，用于测试持久化模式。"""

    def __init__(self):
        self.executed_queries: list = []
        self.rows: dict = {}  # SQL pattern → return rows

    def execute(self, query, params=None, settings=None):
        condensed = " ".join(query.split()) if isinstance(query, str) else str(query)
        self.executed_queries.append((condensed[:120], params))

        # CREATE TABLE → 返回空
        if "CREATE TABLE" in condensed:
            return []

        # 聚合查询（按小时聚合）— 优先于 count() 检查
        if "toStartOfHour" in condensed:
            return []

        # COUNT 查询
        if "count()" in condensed and "%(source)s" in condensed:
            source = params.get("source", "") if params else ""
            target = params.get("target", "") if params else ""
            # 支持 failure_pattern 维度——key 为 source|target|failure_pattern
            if params and "failure_pattern" in params:
                fp = params["failure_pattern"]
                key = f"{source}|{target}|{fp}"
            else:
                key = f"{source}|{target}"
            if key in self.rows:
                return self.rows[key]
            return [(0,)]

        # INSERT → 记录插入数据
        if "INSERT INTO" in condensed:
            return len(params) if isinstance(params, list) else 1

        return []


class MockStorageWithCH:
    """模拟包含 ch_client 的 StorageAdapter。"""
    def __init__(self, ch_client=None):
        self.ch_client = ch_client


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

    # ── ClickHouse 持久化模式 ──

    def test_ch_record_and_query_trend(self):
        """ClickHouse 模式记录并查询交互趋势"""
        ch = MockClickHouseClient()
        ch.rows["svc-A|svc-B"] = [(5,)]
        storage = MockStorageWithCH(ch)
        rel = DynamicRelProjection(storage=storage)

        rel.record_interaction("svc-A", "svc-B")
        trend = rel.query_trend("svc-A", "svc-B", windows=["1 HOUR", "6 HOUR"])
        assert len(trend) == 2
        assert trend[0] == 5
        assert trend[1] == 5

    def test_ch_create_table_on_init(self):
        """初始化时自动创建 logs.interactions 表"""
        ch = MockClickHouseClient()
        storage = MockStorageWithCH(ch)
        rel = DynamicRelProjection(storage=storage)

        create_found = any(
            "CREATE TABLE" in q and "logs.interactions" in q
            for q, _ in ch.executed_queries
        )
        assert create_found, "DynamicRelProjection 初始化时应创建 logs.interactions 表"

    def test_ch_record_interaction_executes_insert(self):
        """record_interaction 在 ClickHouse 模式下执行 INSERT"""
        ch = MockClickHouseClient()
        storage = MockStorageWithCH(ch)
        rel = DynamicRelProjection(storage=storage)

        rel.record_interaction("svc-X", "svc-Y")

        insert_found = any(
            "INSERT INTO" in q and "logs.interactions" in q
            for q, _ in ch.executed_queries
        )
        assert insert_found

    def test_ch_trend_uses_parameterized_query(self):
        """query_trend 使用参数化查询（避免 SQL 注入）"""
        ch = MockClickHouseClient()
        ch.rows["svc-A|svc-B"] = [(3,)]
        storage = MockStorageWithCH(ch)
        rel = DynamicRelProjection(storage=storage)

        trend = rel.query_trend("svc-A", "svc-B", windows=["1 HOUR"])

        param_query_found = any(
            "count()" in q and params is not None
            for q, params in ch.executed_queries
        )
        assert param_query_found
        assert trend == [3]

    def test_ch_query_trend_multiple_windows(self):
        """多个时间窗口独立查询"""
        ch = MockClickHouseClient()
        ch.rows["A|B"] = [(10,)]
        storage = MockStorageWithCH(ch)
        rel = DynamicRelProjection(storage=storage)

        trend = rel.query_trend("A", "B",
                                 windows=["1 HOUR", "6 HOUR", "24 HOUR"])
        assert len(trend) == 3
        assert all(t == 10 for t in trend)

    def test_ch_aggregate_by_hour(self):
        """aggregate_by_hour 返回正确形状的数据"""
        ch = MockClickHouseClient()
        storage = MockStorageWithCH(ch)
        rel = DynamicRelProjection(storage=storage)

        aggregated = rel.aggregate_by_hour("A", "B", hours=24)
        assert isinstance(aggregated, list)

    def test_in_memory_fallback_no_storage(self):
        """不传 storage 时自动使用内存模式"""
        rel = DynamicRelProjection()
        assert rel._clickhouse_available is False
        assert rel._ch_client is None

    def test_in_memory_fallback_no_ch_client(self):
        """storage 没有 ch_client 时使用内存模式"""
        storage = MockStorageWithCH(ch_client=None)
        rel = DynamicRelProjection(storage=storage)
        assert rel._clickhouse_available is False

    def test_in_memory_and_ch_independent_counts(self):
        """内存模式和 ClickHouse 模式计数互相独立"""
        ch = MockClickHouseClient()
        ch.rows["svc-A|svc-B"] = [(100,)]
        storage = MockStorageWithCH(ch)
        rel_ch = DynamicRelProjection(storage=storage)
        rel_mem = DynamicRelProjection()  # 无 storage

        rel_ch.record_interaction("svc-A", "svc-B")
        rel_mem.record_interaction("svc-A", "svc-B")

        trend_ch = rel_ch.query_trend("svc-A", "svc-B", ["1 HOUR"])
        trend_mem = rel_mem.query_trend("svc-A", "svc-B", ["1 HOUR"])
        # ClickHouse 返回 mock 的 100，内存返回 1
        assert trend_ch[0] == 100
        assert trend_mem[0] == 1

    def test_ch_zero_interaction_returns_zero(self):
        """无交互时 query_trend 返回 0"""
        ch = MockClickHouseClient()
        ch.rows["A|B"] = [(0,)]
        storage = MockStorageWithCH(ch)
        rel = DynamicRelProjection(storage=storage)
        trend = rel.query_trend("A", "B", ["1 HOUR"])
        assert trend == [0]

    # ── failure_pattern 维度 ──

    def test_record_with_failure_pattern_memory(self):
        """内存模式下用 failure_pattern 记录和过滤交互"""
        rel = DynamicRelProjection()
        rel.record_interaction("A", "B", failure_pattern="RabbitMQHeartbeatLost")
        rel.record_interaction("A", "B", failure_pattern="RabbitMQHeartbeatLost")
        rel.record_interaction("A", "B", failure_pattern="NovaOOM")

        # 按指定 failure_pattern 过滤
        trend_rb = rel.query_trend("A", "B", ["1 HOUR"],
                                    failure_pattern="RabbitMQHeartbeatLost")
        assert trend_rb[0] == 2

        trend_no = rel.query_trend("A", "B", ["1 HOUR"],
                                    failure_pattern="NovaOOM")
        assert trend_no[0] == 1

        # 不传 failure_pattern（None）→ 返回全部
        trend_all = rel.query_trend("A", "B", ["1 HOUR"])
        assert trend_all[0] == 3

    def test_aggregate_by_hour_with_failure_pattern(self):
        """aggregate_by_hour 支持 failure_pattern 过滤"""
        rel = DynamicRelProjection()
        now = datetime.utcnow()
        for _ in range(5):
            rel.record_interaction("A", "B", timestamp=now,
                                    failure_pattern="pat-1")
        for _ in range(3):
            rel.record_interaction("A", "B", timestamp=now,
                                    failure_pattern="pat-2")

        agg_p1 = rel.aggregate_by_hour("A", "B", hours=24,
                                        failure_pattern="pat-1")
        assert agg_p1[0]["count"] == 5

        agg_all = rel.aggregate_by_hour("A", "B", hours=24)
        assert agg_all[0]["count"] == 8

    def test_ch_record_and_query_with_failure_pattern(self):
        """ClickHouse 模式支持 failure_pattern 维度"""
        ch = MockClickHouseClient()
        ch.rows["A|B|RabbitMQHeartbeatLost"] = [(3,)]
        storage = MockStorageWithCH(ch)
        rel = DynamicRelProjection(storage=storage)

        trend = rel.query_trend("A", "B", ["1 HOUR"],
                                 failure_pattern="RabbitMQHeartbeatLost")
        assert trend[0] == 3

        # 验证使用了带 failure_pattern 的 SQL（截断到 120 字符，检查前缀）
        fp_sql_found = any(
            "failure_patte" in q
            for q, _ in ch.executed_queries
        )
        assert fp_sql_found, "应使用包含 failure_pattern 过滤条件的 SQL"

    def test_ch_failure_pattern_backward_compat(self):
        """不传 failure_pattern 时使用旧的 SQL（不过滤 failure_pattern）"""
        ch = MockClickHouseClient()
        ch.rows["A|B"] = [(5,)]
        storage = MockStorageWithCH(ch)
        rel = DynamicRelProjection(storage=storage)

        # 不传 failure_pattern → 使用旧 SQL（不包含 failure_pattern 过滤）
        trend = rel.query_trend("A", "B", ["1 HOUR"])
        assert trend[0] == 5

        # 验证旧 SQL 被使用（不包含 failure_pattern 条件）
        no_fp_sql = any(
            "count()" in q and "failure_pattern" not in q
            for q, _ in ch.executed_queries
        )
        assert no_fp_sql


def _make_interaction(source_name: str, target_name: str) -> EventEnvelope:
    """创建 interaction.observed 事件的快捷方法。"""
    return EventEnvelope(
        event_id="e-" + source_name,
        event_type="interaction.observed",
        payload=json.dumps({
            "source": {"type": "SERVICE", "name": source_name},
            "target": {"type": "SERVICE", "name": target_name},
        }).encode(),
    )


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

    # ── failure_pattern 维度 ──

    def test_finding_includes_failure_pattern(self):
        """correlation.found finding 包含 failure_pattern 字段"""
        bus = InMemoryEventBus()
        engine = CorrelationEngine(rel_projection=DynamicRelProjection(), bus=bus,
                                    frequency_threshold=2)

        for _ in range(2):
            engine.process(_make_interaction("svc-A", "svc-B"))

        findings = engine.process(_make_interaction("svc-A", "svc-B"))
        assert len(findings) == 1
        f = findings[0]
        assert "failure_pattern" in f
        assert f["failure_pattern"] == "high_frequency_interaction"

    def test_finding_evidence_contains_failure_pattern(self):
        """evidence 列表中包含 failure_pattern"""
        bus = InMemoryEventBus()
        engine = CorrelationEngine(rel_projection=DynamicRelProjection(), bus=bus,
                                    frequency_threshold=2)

        for _ in range(3):
            engine.process(_make_interaction("X", "Y"))

        findings = engine.process(_make_interaction("X", "Y"))
        assert len(findings) == 1
        evidence = findings[0]["evidence"]
        assert any("failure_pattern=" in e for e in evidence)

    def test_different_failure_pattern_separate_counts(self):
        """不同 failure_pattern 的交互计数互不干扰"""
        bus = InMemoryEventBus()
        engine_rabbit = CorrelationEngine(
            rel_projection=DynamicRelProjection(), bus=bus,
            frequency_threshold=3,
            failure_pattern="RabbitMQHeartbeatLost",
        )
        engine_nova = CorrelationEngine(
            rel_projection=DynamicRelProjection(), bus=bus,
            frequency_threshold=3,
            failure_pattern="NovaOOM",
        )

        # RabbitMQ 场景 3 次交互 → 达到阈值 → 第 3 次触发 Finding
        last_r = None
        for i in range(3):
            last_r = engine_rabbit.process(_make_interaction("svc-A", "svc-B"))
        # 第 3 次应触发
        assert len(last_r) == 1
        assert last_r[0]["failure_pattern"] == "RabbitMQHeartbeatLost"

        # Nova 场景只 2 次交互（低于阈值 3）→ 不应触发
        last_n = None
        for i in range(2):
            last_n = engine_nova.process(_make_interaction("svc-A", "svc-B"))
        # 第 2 次仍不应触发
        assert len(last_n) == 0

    def test_custom_failure_pattern_init(self):
        """可自定义 failure_pattern 参数"""
        bus = InMemoryEventBus()
        engine = CorrelationEngine(
            rel_projection=DynamicRelProjection(), bus=bus,
            frequency_threshold=2,
            failure_pattern="custom.pattern.v1",
        )

        for _ in range(3):
            engine.process(_make_interaction("svc-A", "svc-B"))

        findings = engine.process(_make_interaction("svc-A", "svc-B"))
        assert findings[0]["failure_pattern"] == "custom.pattern.v1"
