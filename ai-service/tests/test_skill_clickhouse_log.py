"""
Tests for ai.skills.builtin.clickhouse_log — ClickHouseLogQuerySkill.
"""
import re

import pytest

from ai.skills.base import SkillContext
from ai.skills.builtin.clickhouse_log import ClickHouseLogQuerySkill


@pytest.fixture
def skill():
    return ClickHouseLogQuerySkill()


def _ctx(**kwargs) -> SkillContext:
    defaults = dict(
        question="why are there so many errors?",
        service_name="order-service",
        log_content="ERROR exception occurred timeout in query execution",
        component_type="service",
        namespace="islap",
    )
    defaults.update(kwargs)
    return SkillContext(**defaults)


class TestClickHouseLogQuerySkill:
    def test_has_required_attributes(self, skill):
        assert skill.name
        assert skill.display_name
        assert skill.description
        assert isinstance(skill.applicable_components, list)
        assert isinstance(skill.trigger_patterns, list)

    def test_trigger_patterns_are_valid_regex(self, skill):
        for pattern in skill.trigger_patterns:
            re.compile(pattern)

    def test_trigger_on_error(self, skill):
        ctx = _ctx(log_content="ERROR database connection failed")
        score = skill.match_score(ctx)
        assert score > 0.0

    def test_trigger_on_timeout(self, skill):
        ctx = _ctx(log_content="timeout exceeded for query execution")
        score = skill.match_score(ctx)
        assert score > 0.0

    def test_trigger_on_exception(self, skill):
        ctx = _ctx(log_content="java.lang.NullPointerException at ...")
        score = skill.match_score(ctx)
        assert score > 0.0

    def test_plan_steps_returns_list(self, skill):
        ctx = _ctx()
        steps = skill.plan_steps(ctx)
        assert isinstance(steps, list)
        assert len(steps) > 0

    def test_plan_steps_use_clickhouse_query_tool(self, skill):
        ctx = _ctx()
        steps = skill.plan_steps(ctx)
        for step in steps:
            tool = step.command_spec.get("tool")
            assert tool in {"kubectl_clickhouse_query", "k8s_clickhouse_query"}, (
                f"Expected clickhouse query tool, got {tool}"
            )

    def test_plan_steps_have_sql_query(self, skill):
        ctx = _ctx()
        steps = skill.plan_steps(ctx)
        for step in steps:
            args = step.command_spec.get("args", {})
            query = args.get("query") or step.command_spec.get("query")
            assert query, f"Step {step.step_id} missing SQL query"
            # Verify it looks like SELECT
            assert "SELECT" in query.upper() or "select" in query.lower()

    def test_plan_steps_have_service_name_in_query(self, skill):
        ctx = _ctx(service_name="order-service")
        steps = skill.plan_steps(ctx)
        # At least one step should reference the service name
        found = False
        for step in steps:
            args = step.command_spec.get("args", {})
            query = str(args.get("query") or step.command_spec.get("query") or "")
            if "order-service" in query:
                found = True
                break
        assert found, "Expected at least one query to reference service_name"

    def test_plan_steps_count_within_max(self, skill):
        ctx = _ctx()
        steps = skill.plan_steps(ctx)
        assert len(steps) <= skill.max_steps

    def test_to_action_dict_has_skill_name(self, skill):
        ctx = _ctx()
        steps = skill.plan_steps(ctx)
        action = steps[0].to_action_dict(skill.name)
        assert action["skill_name"] == skill.name
