"""
Tests for ai.skills.builtin.observability_read_path_latency.
"""
import re

import pytest

from ai.skills.base import SkillContext
from ai.skills.builtin.observability_read_path_latency import ObservabilityReadPathLatencySkill


@pytest.fixture
def skill():
    return ObservabilityReadPathLatencySkill()


def _ctx(**kwargs) -> SkillContext:
    defaults = dict(
        question="query-service logs API timeout and preview is slow",
        service_name="query-service",
        log_content="slow query timeout while reading logs aggregation preview",
        component_type="service",
        namespace="islap",
    )
    defaults.update(kwargs)
    return SkillContext(**defaults)


class TestObservabilityReadPathLatencySkill:
    def test_has_required_attributes(self, skill):
        assert skill.name
        assert skill.display_name
        assert skill.description
        assert isinstance(skill.applicable_components, list)
        assert isinstance(skill.trigger_patterns, list)

    def test_trigger_patterns_are_valid_regex(self, skill):
        for pattern in skill.trigger_patterns:
            re.compile(pattern)

    def test_trigger_on_slow_query(self, skill):
        ctx = _ctx(log_content="clickhouse slow query on query-service logs endpoint")
        assert skill.match_score(ctx) > 0.0

    def test_trigger_on_preview_timeout(self, skill):
        ctx = _ctx(log_content="preview request timeout after large read")
        assert skill.match_score(ctx) > 0.0

    def test_does_not_strongly_match_network_only_issue(self, skill):
        ctx = _ctx(
            question="service cannot connect to backend",
            log_content="connection refused ECONNREFUSED nslookup failed",
            component_type="service",
        )
        assert skill.match_score(ctx) < 0.5

    def test_does_not_match_service_component_without_read_path_signals(self, skill):
        ctx = _ctx(
            question="service health check looks unstable",
            log_content="intermittent error spikes during health checks",
            component_type="service",
        )
        assert skill.match_score(ctx) < 0.2

    def test_plan_steps_returns_expected_evidence_layers(self, skill):
        steps = skill.plan_steps(_ctx())
        titles = [step.title for step in steps]
        assert any("日志" in title for title in titles)
        assert any("query_log" in title for title in titles)
        assert any("运行查询" in title or "processes" in title for title in titles)
        assert any("关键运行指标" in title or "metrics" in title for title in titles)

    def test_plan_steps_use_only_structured_readonly_tools(self, skill):
        steps = skill.plan_steps(_ctx())
        for step in steps:
            tool = step.command_spec.get("tool")
            assert tool in {"generic_exec", "kubectl_clickhouse_query"}

    def test_plan_steps_have_query_or_command(self, skill):
        steps = skill.plan_steps(_ctx())
        for step in steps:
            args = step.command_spec.get("args", {})
            if step.command_spec.get("tool") == "kubectl_clickhouse_query":
                assert args.get("query")
            else:
                assert args.get("command") or args.get("command_argv")

    def test_plan_steps_count_within_max(self, skill):
        steps = skill.plan_steps(_ctx())
        assert len(steps) <= skill.max_steps

    def test_service_name_appears_in_log_step(self, skill):
        steps = skill.plan_steps(_ctx(service_name="query-service"))
        commands = [str(step.command_spec.get("args", {}).get("command") or "") for step in steps]
        assert any("query-service" in command for command in commands)
