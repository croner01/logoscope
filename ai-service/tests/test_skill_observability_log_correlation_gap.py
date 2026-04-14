"""
Tests for ai.skills.builtin.observability_log_correlation_gap.
"""
import re

import pytest

from ai.skills.base import SkillContext
from ai.skills.builtin.observability_log_correlation_gap import ObservabilityLogCorrelationGapSkill


@pytest.fixture
def skill():
    return ObservabilityLogCorrelationGapSkill()


def _ctx(**kwargs) -> SkillContext:
    defaults = dict(
        question="missing trace_id but request_id exists, how should we continue diagnosis?",
        service_name="query-service",
        log_content="request_id=req-001 no trace_id found around 2026-04-14T10:30:00Z",
        component_type="service",
        namespace="islap",
        extra={
            "request_id": "req-001",
            "request_flow_window_start": "2026-04-14T10:25:00Z",
            "request_flow_window_end": "2026-04-14T10:35:00Z",
        },
    )
    defaults.update(kwargs)
    return SkillContext(**defaults)


class TestObservabilityLogCorrelationGapSkill:
    def test_has_required_attributes(self, skill):
        assert skill.name
        assert skill.display_name
        assert skill.description
        assert isinstance(skill.applicable_components, list)
        assert isinstance(skill.trigger_patterns, list)

    def test_trigger_patterns_are_valid_regex(self, skill):
        for pattern in skill.trigger_patterns:
            re.compile(pattern)

    def test_trigger_on_missing_trace_with_request_id(self, skill):
        ctx = _ctx(log_content="missing trace_id but request_id=req-001 is present")
        assert skill.match_score(ctx) > 0.0

    def test_trigger_on_time_window_gap(self, skill):
        ctx = _ctx(question="time window is too broad and anchors are incomplete")
        assert skill.match_score(ctx) > 0.0

    def test_does_not_strongly_match_generic_time_window_without_anchor_gap(self, skill):
        ctx = _ctx(
            question="分析最近 time window 内 query-service 错误率抖动",
            log_content="观察 10:00-10:05 time window 错误率变化",
            extra={},
        )
        assert skill.match_score(ctx) < 0.2

    def test_does_not_strongly_match_clear_slow_query_incident(self, skill):
        ctx = _ctx(
            question="query-service slow query timeout",
            log_content="ClickHouse slow query read_rows huge query_duration_ms=9000",
            extra={},
        )
        assert skill.match_score(ctx) < 0.5

    def test_plan_steps_return_anchor_focused_steps(self, skill):
        steps = skill.plan_steps(_ctx())
        titles = [step.title for step in steps]
        assert any("时间窗" in title or "窗口" in title for title in titles)
        assert any("request_id" in title or "trace_id" in title for title in titles)
        assert any("query-service" in title or "读路径" in title for title in titles)

    def test_plan_steps_use_only_structured_readonly_tools(self, skill):
        steps = skill.plan_steps(_ctx())
        for step in steps:
            tool = step.command_spec.get("tool")
            assert tool in {"generic_exec", "kubectl_clickhouse_query"}

    def test_request_id_is_preferred_when_present(self, skill):
        steps = skill.plan_steps(_ctx())
        queries = [
            str(step.command_spec.get("args", {}).get("query") or "")
            for step in steps
            if step.command_spec.get("tool") == "kubectl_clickhouse_query"
        ]
        assert any("req-001" in query for query in queries)

    def test_explicit_window_is_used_when_present(self, skill):
        steps = skill.plan_steps(_ctx())
        commands = [str(step.command_spec.get("args", {}).get("command") or "") for step in steps]
        assert any("2026-04-14T10:25:00Z" in command for command in commands)

    def test_plan_steps_count_within_max(self, skill):
        steps = skill.plan_steps(_ctx())
        assert len(steps) <= skill.max_steps
