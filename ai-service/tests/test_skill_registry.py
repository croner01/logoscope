"""
Tests for ai.skills.registry — registration, lookup, dedup, and match_skills().
"""
import pytest

from ai.skills.base import DiagnosticSkill, SkillContext, SkillStep
from ai.skills.registry import (
    _REGISTRY,
    get_skill_registry,
    match_skills,
    register_skill,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_context(**kwargs) -> SkillContext:
    defaults = dict(
        question="test",
        service_name="svc",
        log_content="",
        log_level="ERROR",
        component_type="pod",
        namespace="islap",
        previous_observations=[],
    )
    defaults.update(kwargs)
    return SkillContext(**defaults)


# ---------------------------------------------------------------------------
# Fixture: isolated registry for each test
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clean_registry():
    """Snapshot and restore the registry around each test."""
    snapshot = dict(_REGISTRY)
    yield
    _REGISTRY.clear()
    _REGISTRY.update(snapshot)


# ---------------------------------------------------------------------------
# Dummy skill for testing
# ---------------------------------------------------------------------------

class _DummySkill(DiagnosticSkill):
    name = "dummy_skill"
    display_name = "Dummy Skill"
    description = "A skill used only in tests"
    applicable_components = ["pod"]
    trigger_patterns = []
    risk_level = "low"

    def plan_steps(self, context: SkillContext):
        return [
            SkillStep(
                step_id="dummy-step-1",
                title="Dummy step",
                command_spec={"tool": "generic_exec", "args": {"command": "echo hello", "target_kind": "k8s_node", "target_identity": "node:local", "timeout_s": 5}},
                purpose="Test purpose",
            )
        ]


class _HighScoreSkill(DiagnosticSkill):
    name = "high_score_skill"
    display_name = "High Score"
    description = "Triggers on 'critical_error'"
    applicable_components = ["pod", "deployment"]
    trigger_patterns = [r"critical_error"]
    risk_level = "medium"

    def plan_steps(self, context: SkillContext):
        return []


# ---------------------------------------------------------------------------
# Tests: register_skill decorator
# ---------------------------------------------------------------------------

class TestRegisterSkill:
    def test_register_adds_to_registry(self):
        register_skill(_DummySkill)
        assert "dummy_skill" in _REGISTRY

    def test_registered_instance_is_correct_type(self):
        register_skill(_DummySkill)
        assert isinstance(_REGISTRY["dummy_skill"], _DummySkill)

    def test_register_duplicate_raises(self):
        # Two different classes with the same `name` attribute should raise
        class _Dup1(DiagnosticSkill):
            name = "dup_skill"
            display_name = "Dup 1"
            description = "first"
            applicable_components = ["pod"]
            trigger_patterns = []
            risk_level = "low"

            def plan_steps(self, context: SkillContext):
                return []

        class _Dup2(DiagnosticSkill):
            name = "dup_skill"  # same name, different class
            display_name = "Dup 2"
            description = "second"
            applicable_components = ["pod"]
            trigger_patterns = []
            risk_level = "low"

            def plan_steps(self, context: SkillContext):
                return []

        register_skill(_Dup1)
        with pytest.raises(RuntimeError, match="already registered"):
            register_skill(_Dup2)

    def test_register_returns_class_unchanged(self):
        result = register_skill(_DummySkill)
        assert result is _DummySkill


# ---------------------------------------------------------------------------
# Tests: get_skill_registry
# ---------------------------------------------------------------------------

class TestGetSkillRegistry:
    def test_returns_copy(self):
        register_skill(_DummySkill)
        registry = get_skill_registry()
        registry.pop("dummy_skill", None)
        # Original should be untouched
        assert "dummy_skill" in _REGISTRY

    def test_contains_registered_skills(self):
        register_skill(_DummySkill)
        assert "dummy_skill" in get_skill_registry()


# ---------------------------------------------------------------------------
# Tests: match_skills
# ---------------------------------------------------------------------------

class TestMatchSkills:
    def test_returns_empty_when_no_skills(self):
        _REGISTRY.clear()
        ctx = _make_context()
        results = match_skills(ctx)
        assert results == []

    def test_skill_with_matching_component_returns_result(self):
        register_skill(_DummySkill)
        ctx = _make_context(component_type="pod")
        results = match_skills(ctx, min_score=0.0)
        names = [s.name for s, _ in results]
        assert "dummy_skill" in names

    def test_skill_with_non_matching_component_filtered(self):
        register_skill(_DummySkill)
        ctx = _make_context(component_type="database")
        results = match_skills(ctx, min_score=0.0)
        names = [s.name for s, _ in results]
        assert "dummy_skill" not in names

    def test_min_score_filters_low_scoring_skills(self):
        register_skill(_DummySkill)
        ctx = _make_context(component_type="pod")
        results_strict = match_skills(ctx, min_score=0.9)
        names = [s.name for s, _ in results_strict]
        assert "dummy_skill" not in names

    def test_trigger_pattern_boosts_score(self):
        register_skill(_HighScoreSkill)
        ctx = _make_context(
            component_type="pod",
            log_content="Something critical_error happened",
        )
        results = match_skills(ctx, min_score=0.0)
        scores = {s.name: score for s, score in results}
        assert "high_score_skill" in scores
        assert scores["high_score_skill"] > 0.5

    def test_max_skills_limits_results(self):
        register_skill(_DummySkill)
        register_skill(_HighScoreSkill)
        ctx = _make_context(component_type="pod")
        results = match_skills(ctx, min_score=0.0, max_skills=1)
        assert len(results) <= 1

    def test_results_sorted_by_score_descending(self):
        register_skill(_DummySkill)
        register_skill(_HighScoreSkill)
        ctx = _make_context(
            component_type="pod",
            log_content="critical_error in container",
        )
        results = match_skills(ctx, min_score=0.0)
        scores = [score for _, score in results]
        assert scores == sorted(scores, reverse=True)

    def test_runtime_diagnosis_orchestrator_matches_clickhouse_gaps(self):
        # Use the built-in registry snapshot (includes builtin skills).
        ctx = _make_context(
            component_type="clickhouse",
            service_name="query-service",
            log_content="ClickHouse slow query detected; 证据不足，需要继续排查",
            question="继续排查 clickhouse 慢查询，当前证据不足，阻断在重规划",
        )
        results = match_skills(ctx, min_score=0.1, max_skills=6)
        names = [s.name for s, _ in results]
        assert "runtime_diagnosis_orchestrator" in names
