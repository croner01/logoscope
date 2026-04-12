"""
Tests for ai.skills.builtin.k8s_pod — K8sPodDiagnosticsSkill.
"""
import re

import pytest

from ai.skills.base import SkillContext
from ai.skills.builtin.k8s_pod import K8sPodDiagnosticsSkill


@pytest.fixture
def skill():
    return K8sPodDiagnosticsSkill()


def _ctx(**kwargs) -> SkillContext:
    defaults = dict(
        question="pod is crashing",
        service_name="my-service",
        log_content="CrashLoopBackOff back-off 1m30s restarting failed container",
        component_type="pod",
        namespace="islap",
    )
    defaults.update(kwargs)
    return SkillContext(**defaults)


class TestK8sPodDiagnosticsSkill:
    def test_has_required_attributes(self, skill):
        assert skill.name
        assert skill.display_name
        assert skill.description
        assert isinstance(skill.applicable_components, list)
        assert isinstance(skill.trigger_patterns, list)

    def test_applicable_components_include_pod(self, skill):
        assert "pod" in skill.applicable_components

    def test_trigger_patterns_are_valid_regex(self, skill):
        for pattern in skill.trigger_patterns:
            re.compile(pattern)  # should not raise

    def test_trigger_on_crash_loop(self, skill):
        ctx = _ctx(log_content="CrashLoopBackOff restarting container")
        score = skill.match_score(ctx)
        assert score > 0.0

    def test_trigger_on_oomkilled(self, skill):
        ctx = _ctx(log_content="OOMKilled container exceeded memory limit")
        score = skill.match_score(ctx)
        assert score > 0.0

    def test_trigger_on_imagepullbackoff(self, skill):
        ctx = _ctx(log_content="ImagePullBackOff cannot pull image")
        score = skill.match_score(ctx)
        assert score > 0.0

    def test_no_trigger_on_empty_log(self, skill):
        ctx = _ctx(log_content="", component_type="database")
        score = skill.match_score(ctx)
        assert score == 0.0

    def test_plan_steps_returns_list(self, skill):
        ctx = _ctx()
        steps = skill.plan_steps(ctx)
        assert isinstance(steps, list)
        assert len(steps) > 0

    def test_plan_steps_have_required_fields(self, skill):
        ctx = _ctx()
        steps = skill.plan_steps(ctx)
        for step in steps:
            assert step.step_id
            assert step.title
            assert isinstance(step.command_spec, dict)
            assert step.purpose

    def test_plan_steps_use_generic_exec_tool(self, skill):
        ctx = _ctx()
        steps = skill.plan_steps(ctx)
        for step in steps:
            tool = step.command_spec.get("tool") or step.command_spec.get("args", {}).get("tool")
            assert tool == "generic_exec", f"Expected generic_exec, got {tool} in step {step.step_id}"

    def test_plan_steps_include_kubectl_describe(self, skill):
        ctx = _ctx()
        steps = skill.plan_steps(ctx)
        commands = [
            str(s.command_spec.get("args", {}).get("command", ""))
            for s in steps
        ]
        assert any("describe" in cmd for cmd in commands)

    def test_plan_steps_count_within_max(self, skill):
        ctx = _ctx()
        steps = skill.plan_steps(ctx)
        assert len(steps) <= skill.max_steps

    def test_to_action_dict(self, skill):
        ctx = _ctx()
        steps = skill.plan_steps(ctx)
        action = steps[0].to_action_dict(skill.name)
        assert action["skill_name"] == skill.name
        assert action["step_id"] == steps[0].step_id
        assert "command_spec" in action
