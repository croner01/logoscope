"""tests/test_skill_business_chain_analyzer.py — part 1: anchor SQL tests"""

import re

import pytest

from ai.skills.base import SkillContext
from ai.skills.builtin.business_chain_analyzer import (
    BusinessChainAnalyzerSkill,
    _build_anchor_resolve_sql,
    _build_events_anchor_sql,
    _build_discovery_sql_channel1,
    _build_discovery_sql_channel2,
    _build_discovery_sql_channel3,
    _merge_service_channels,
    _build_supplement_sql,
    _build_trace_tree_sql,
    _build_span_tree,
    DEFAULT_CHAIN_PROMPT,
    _MAX_CHAIN_SERVICES,
)


@pytest.fixture
def skill():
    return BusinessChainAnalyzerSkill()


def _ctx(**kwargs) -> SkillContext:
    defaults = dict(
        question="分析这个请求的完整业务链，req-xxx 关联到哪些服务？",
        service_name="nova-api",
        log_content="req-abcdef-12345 POST /servers",
        trace_id="trace-001",
        namespace="islap",
        extra={
            "os_request_id": "req-abcdef-12345",
            "request_id": "req-abcdef-12345",
        },
    )
    defaults.update(kwargs)
    return SkillContext(**defaults)


class TestAnchorResolveSQL:
    def test_build_anchor_resolve_sql_with_trace_id(self):
        sql = _build_anchor_resolve_sql(
            trace_id="trace-001",
            os_request_id="",
            time_window_start="2026-06-15 12:00:00",
            time_window_end="2026-06-15 12:06:00",
        )
        assert "trace_id = 'trace-001'" in sql
        assert "LIMIT 2000" in sql
        assert "logs.logs" in sql
        assert "FORMAT PrettyCompact" in sql

    def test_build_anchor_resolve_sql_with_req_xxx(self):
        sql = _build_anchor_resolve_sql(
            trace_id="",
            os_request_id="req-abcdef-12345",
            time_window_start="2026-06-15 12:00:00",
            time_window_end="2026-06-15 12:06:00",
        )
        assert "message LIKE '%req-abcdef-12345%'" in sql
        # trace_id is always in SELECT, but should NOT be in WHERE conditions
        assert "WHERE (message" in sql
        assert "trace_id = '" not in sql

    def test_build_anchor_resolve_sql_with_both(self):
        sql = _build_anchor_resolve_sql(
            trace_id="trace-001",
            os_request_id="req-abcdef-12345",
            time_window_start="2026-06-15 12:00:00",
            time_window_end="2026-06-15 12:06:00",
        )
        assert "trace_id = 'trace-001'" in sql
        assert "message LIKE '%req-abcdef-12345%'" in sql
        assert "OR" in sql

    def test_build_anchor_resolve_sql_with_no_anchor(self):
        sql = _build_anchor_resolve_sql(
            trace_id="",
            os_request_id="",
            time_window_start="2026-06-15 12:00:00",
            time_window_end="2026-06-15 12:06:00",
        )
        assert "1=1" in sql

    def test_build_events_anchor_sql(self):
        sql = _build_events_anchor_sql(
            trace_id="trace-001",
            os_request_id="req-xxx-123",
        )
        assert "logs.events" in sql
        assert "trace_id = 'trace-001'" in sql
        assert "content LIKE '%req-xxx-123%'" in sql
        assert "LIMIT 1000" in sql


class TestServiceDiscovery:
    def test_channel1_sql(self):
        sql = _build_discovery_sql_channel1("trace-001", "T1", "T2")
        assert "trace_id = 'trace-001'" in sql
        assert "DISTINCT service_name" in sql
        assert "ORDER BY service_name" in sql

    def test_channel2_sql(self):
        sql = _build_discovery_sql_channel2("req-abc", "T1", "T2")
        assert "message LIKE '%req-abc%'" in sql
        assert "DISTINCT service_name" in sql

    def test_channel3_sql(self):
        sql = _build_discovery_sql_channel3("T1", "T2")
        assert "DISTINCT service_name" in sql
        assert "trace_id" not in sql
        assert "message" not in sql

    def test_merge_channels_all_exclusive(self):
        result = _merge_service_channels(
            channel1=["nova-api", "nova-compute"],
            channel2=["keystone", "cinder-api"],
            channel3=["rabbitmq", "mysql"],
        )
        assert result["nova-api"] == "trace_id"
        assert result["cinder-api"] == "req_xxx"
        assert result["rabbitmq"] == "time_window"

    def test_merge_channels_overlap(self):
        result = _merge_service_channels(
            channel1=["nova-api"],
            channel2=["nova-api", "cinder-api"],
            channel3=["nova-api", "cinder-api", "mysql"],
        )
        assert result["nova-api"] == "trace_id"
        assert result["cinder-api"] == "req_xxx"
        assert result["mysql"] == "time_window"

    def test_merge_channels_truncate(self):
        services_ch1 = [f"svc-{i}" for i in range(10)]
        services_ch2 = [f"svc-{i}" for i in range(10, 15)]
        services_ch3 = [f"svc-{i}" for i in range(15, 30)]
        result = _merge_service_channels(
            services_ch1, services_ch2, services_ch3,
            max_services=15,
        )
        assert len(result) == 15
        for svc in services_ch1:
            assert svc in result

    def test_merge_empty_channels(self):
        result = _merge_service_channels([], [], [])
        assert result == {}

    def test_supplement_sql_trace_id(self):
        sql = _build_supplement_sql(
            service_name="nova-compute",
            anchor_type="trace_id",
            trace_id="trace-001",
            os_request_id="",
            start="2026-06-15 12:00:00",
            end="2026-06-15 12:06:00",
        )
        assert "service_name = 'nova-compute'" in sql
        assert "trace_id = 'trace-001'" in sql
        assert "LIMIT 300" in sql

    def test_supplement_sql_req_xxx(self):
        sql = _build_supplement_sql(
            service_name="cinder-api",
            anchor_type="req_xxx",
            trace_id="",
            os_request_id="req-abcdef-12345",
            start="T1", end="T2",
        )
        assert "message LIKE '%req-abcdef-12345%'" in sql
        assert "service_name = 'cinder-api'" in sql

    def test_supplement_sql_time_window(self):
        sql = _build_supplement_sql(
            service_name="keystone",
            anchor_type="time_window",
            trace_id="",
            os_request_id="",
            start="T1", end="T2",
        )
        assert "1=1" in sql
        assert "service_name = 'keystone'" in sql


class TestTraceTreeRebuild:
    def test_build_trace_tree_sql(self):
        sql = _build_trace_tree_sql("trace-001")
        assert "logs.traces" in sql
        assert "PREWHERE trace_id = 'trace-001'" in sql
        assert "parent_span_id" in sql

    def test_build_span_tree_single_root(self):
        spans = [
            {"span_id": "a", "parent_span_id": "", "service_name": "nova-api",
             "span_kind": "SERVER", "operation_name": "POST /servers",
             "timestamp": "12:00:01", "duration_ms": "452", "status": "OK"},
        ]
        lines = _build_span_tree(spans)
        assert len(lines) == 1
        assert "nova-api" in lines[0]
        assert "POST /servers" in lines[0]

    def test_build_span_tree_parent_child(self):
        spans = [
            {"span_id": "a", "parent_span_id": "", "service_name": "nova-api",
             "span_kind": "SERVER", "operation_name": "POST /servers",
             "timestamp": "12:00:01", "duration_ms": "452", "status": "OK"},
            {"span_id": "b", "parent_span_id": "a", "service_name": "nova-compute",
             "span_kind": "CLIENT", "operation_name": "spawn",
             "timestamp": "12:00:05", "duration_ms": "5000", "status": "ERROR"},
        ]
        lines = _build_span_tree(spans)
        assert len(lines) == 2
        assert lines[1].startswith("  ")
        assert "nova-compute" in lines[1]
        assert "[ERROR]" in lines[1]

    def test_build_span_tree_grandchild(self):
        spans = [
            {"span_id": "a", "parent_span_id": "", "service_name": "nova-api",
             "span_kind": "SERVER", "operation_name": "POST",
             "timestamp": "12:00:01", "duration_ms": "452", "status": "OK"},
            {"span_id": "b", "parent_span_id": "a", "service_name": "nova-compute",
             "span_kind": "CLIENT", "operation_name": "spawn",
             "timestamp": "12:00:05", "duration_ms": "8000", "status": "OK"},
            {"span_id": "c", "parent_span_id": "b", "service_name": "proton",
             "span_kind": "CLIENT", "operation_name": "setup_network",
             "timestamp": "12:00:10", "duration_ms": "1200", "status": "OK"},
        ]
        lines = _build_span_tree(spans)
        assert len(lines) == 3
        # Verify indentation: root no indent, child 2, grandchild 4
        assert not lines[0].startswith(" ")
        assert lines[1].startswith("  ")
        assert lines[2].startswith("    ")

    def test_build_span_tree_empty(self):
        lines = _build_span_tree([])
        assert lines == []

    def test_build_span_tree_no_root(self):
        spans = [
            {"span_id": "b", "parent_span_id": "orphan", "service_name": "lost",
             "span_kind": "INTERNAL", "operation_name": "orphan",
             "timestamp": "12:00:01", "duration_ms": "100", "status": "OK"},
        ]
        lines = _build_span_tree(spans)
        assert lines == []
