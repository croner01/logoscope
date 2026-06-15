"""tests/test_skill_business_chain_analyzer.py — part 1: anchor SQL tests"""

import re

import pytest

from ai.skills.base import SkillContext
from ai.skills.builtin.business_chain_analyzer import (
    BusinessChainAnalyzerSkill,
    _build_anchor_resolve_sql,
    _build_events_anchor_sql,
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
