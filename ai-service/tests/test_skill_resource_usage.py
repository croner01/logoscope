"""
Tests for ai.skills.builtin.resource_usage — ResourceUsageSkill.
"""
import re

import pytest

from ai.skills.base import SkillContext
from ai.skills.builtin.resource_usage import ResourceUsageSkill


@pytest.fixture
def skill():
    return ResourceUsageSkill()


def _ctx(**kwargs) -> SkillContext:
    defaults = dict(
        question="pod was evicted due to memory pressure",
        service_name="worker",
        log_content="OOMKilled memory limit exceeded evict",
        component_type="pod",
        namespace="islap",
    )
    defaults.update(kwargs)
    return SkillContext(**defaults)


class TestResourceUsageSkill:
    def test_has_required_attributes(self, skill):
        assert skill.name
        assert skill.display_name
        assert skill.description
        assert isinstance(skill.applicable_components, list)
        assert isinstance(skill.trigger_patterns, list)

    def test_trigger_patterns_are_valid_regex(self, skill):
        for pattern in skill.trigger_patterns:
            re.compile(pattern)

    def test_trigger_on_oomkilled(self, skill):
        ctx = _ctx(log_content="OOMKilled container exceeded memory limit")
        score = skill.match_score(ctx)
        assert score > 0.0

    def test_trigger_on_evict(self, skill):
        ctx = _ctx(log_content="pod evicted due to memory pressure")
        score = skill.match_score(ctx)
        assert score > 0.0

    def test_trigger_on_memory_limit(self, skill):
        ctx = _ctx(log_content="memory limit exceeded by container")
        score = skill.match_score(ctx)
        assert score > 0.0

    def test_no_trigger_on_network_log(self, skill):
        # Use content with no resource/memory/OOM related keywords
        # and a component type not in applicable_components.
        # Also override question to avoid having resource keywords there.
        ctx = _ctx(
            question="why can't the service connect to the database?",
            log_content="DNS lookup failed nslookup connection timeout",
            component_type="database",
        )
        score = skill.match_score(ctx)
        # None of the trigger_patterns match network/DNS content
        # and component_type "database" is not in applicable_components
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

    def test_plan_steps_include_kubectl_top(self, skill):
        ctx = _ctx()
        steps = skill.plan_steps(ctx)
        commands = [
            str(s.command_spec.get("args", {}).get("command", ""))
            for s in steps
        ]
        assert any("top" in cmd for cmd in commands)

    def test_plan_steps_count_within_max(self, skill):
        ctx = _ctx()
        steps = skill.plan_steps(ctx)
        assert len(steps) <= skill.max_steps

    def test_plan_steps_avoid_shell_chaining_and_redirection(self, skill):
        ctx = _ctx()
        steps = skill.plan_steps(ctx)
        commands = [
            str(step.command_spec.get("args", {}).get("command") or "")
            for step in steps
        ]
        assert commands
        assert all("|" not in command for command in commands)
        assert all("&&" not in command for command in commands)
        assert all(">" not in command for command in commands)

    def test_step_ids_are_unique(self, skill):
        ctx = _ctx()
        steps = skill.plan_steps(ctx)
        ids = [s.step_id for s in steps]
        assert len(ids) == len(set(ids))
