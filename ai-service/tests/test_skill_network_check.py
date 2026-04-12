"""
Tests for ai.skills.builtin.network_check — NetworkConnectivitySkill.
"""
import re

import pytest

from ai.skills.base import SkillContext
from ai.skills.builtin.network_check import NetworkConnectivitySkill


@pytest.fixture
def skill():
    return NetworkConnectivitySkill()


def _ctx(**kwargs) -> SkillContext:
    defaults = dict(
        question="why can't service reach the backend?",
        service_name="frontend",
        log_content="connection refused ECONNREFUSED 127.0.0.1:8080",
        component_type="service",
        namespace="islap",
    )
    defaults.update(kwargs)
    return SkillContext(**defaults)


class TestNetworkConnectivitySkill:
    def test_has_required_attributes(self, skill):
        assert skill.name
        assert skill.display_name
        assert skill.description
        assert isinstance(skill.applicable_components, list)
        assert isinstance(skill.trigger_patterns, list)

    def test_trigger_patterns_are_valid_regex(self, skill):
        for pattern in skill.trigger_patterns:
            re.compile(pattern)

    def test_trigger_on_connection_refused(self, skill):
        ctx = _ctx(log_content="connection refused to backend:8080")
        score = skill.match_score(ctx)
        assert score > 0.0

    def test_trigger_on_econnrefused(self, skill):
        ctx = _ctx(log_content="ECONNREFUSED 10.0.0.1:9000")
        score = skill.match_score(ctx)
        assert score > 0.0

    def test_trigger_on_dns_failure(self, skill):
        ctx = _ctx(log_content="DNS lookup failed for svc.islap.svc.cluster.local")
        score = skill.match_score(ctx)
        assert score > 0.0

    def test_no_trigger_on_unrelated_log(self, skill):
        ctx = _ctx(log_content="CPU usage normal", component_type="database")
        score = skill.match_score(ctx)
        assert score == 0.0

    def test_plan_steps_returns_list(self, skill):
        ctx = _ctx()
        steps = skill.plan_steps(ctx)
        assert isinstance(steps, list)
        assert len(steps) > 0

    def test_plan_steps_use_generic_exec_tool(self, skill):
        ctx = _ctx()
        steps = skill.plan_steps(ctx)
        for step in steps:
            tool = step.command_spec.get("tool")
            assert tool == "generic_exec", f"Expected generic_exec, got {tool}"

    def test_plan_steps_have_commands(self, skill):
        ctx = _ctx()
        steps = skill.plan_steps(ctx)
        for step in steps:
            args = step.command_spec.get("args", {})
            cmd = args.get("command") or args.get("command_argv")
            assert cmd, f"Step {step.step_id} has no command"

    def test_plan_steps_count_within_max(self, skill):
        ctx = _ctx()
        steps = skill.plan_steps(ctx)
        assert len(steps) <= skill.max_steps

    def test_step_ids_are_unique(self, skill):
        ctx = _ctx()
        steps = skill.plan_steps(ctx)
        ids = [s.step_id for s in steps]
        assert len(ids) == len(set(ids))
