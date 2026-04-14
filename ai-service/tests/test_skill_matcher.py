"""
Tests for ai.skills.matcher — rule-based skill matching and catalog generation.
"""
import pytest

from ai.skills.base import DiagnosticSkill, SkillContext, SkillStep
from ai.skills.matcher import (
    build_skill_catalog_for_prompt,
    extract_auto_selected_skills,
    extract_high_confidence_skills,
    get_skill_selection_summary,
    match_skills_by_rules,
)
from ai.skills.registry import _REGISTRY, register_skill


# ---------------------------------------------------------------------------
# Fixture: isolated registry
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clean_registry():
    snapshot = dict(_REGISTRY)
    yield
    _REGISTRY.clear()
    _REGISTRY.update(snapshot)


# ---------------------------------------------------------------------------
# Dummy skills
# ---------------------------------------------------------------------------

class _CrashSkill(DiagnosticSkill):
    name = "crash_skill"
    display_name = "Crash Skill"
    description = "Detects pod crashes"
    applicable_components = ["pod"]
    trigger_patterns = [r"CrashLoopBackOff", r"OOMKilled"]
    risk_level = "low"

    def plan_steps(self, context: SkillContext):
        return [
            SkillStep(
                step_id="crash-check",
                title="Check crash",
                command_spec={"tool": "generic_exec", "args": {"command": "kubectl describe pod test", "target_kind": "k8s_node", "target_identity": "node:local", "timeout_s": 10}},
                purpose="Understand crash reason",
            )
        ]


class _NetworkSkill(DiagnosticSkill):
    name = "network_skill"
    display_name = "Network Skill"
    description = "Detects network issues"
    applicable_components = ["pod", "service"]
    trigger_patterns = [r"connection refused", r"ECONNREFUSED"]
    risk_level = "low"

    def plan_steps(self, context: SkillContext):
        return []


# ---------------------------------------------------------------------------
# Tests: match_skills_by_rules
# ---------------------------------------------------------------------------

class TestMatchSkillsByRules:
    def test_empty_registry_returns_empty(self):
        _REGISTRY.clear()
        ctx = SkillContext(
            question="pod failing",
            service_name="svc",
            log_content="CrashLoopBackOff",
            component_type="pod",
        )
        assert match_skills_by_rules(ctx) == []

    def test_matching_trigger_pattern_returns_skill(self):
        register_skill(_CrashSkill)
        ctx = SkillContext(
            question="pod failing",
            service_name="svc",
            log_content="CrashLoopBackOff detected",
            component_type="pod",
        )
        results = match_skills_by_rules(ctx)
        names = [s.name for s, _ in results]
        assert "crash_skill" in names

    def test_non_matching_component_excluded(self):
        # log_content has no crash keywords, so pattern_score = 0.
        # component_type="database" is not in applicable_components=["pod"],
        # so component_bonus = 0. Total score = 0 → filtered out.
        register_skill(_CrashSkill)
        ctx = SkillContext(
            question="database query running slowly",
            service_name="svc",
            log_content="slow query execution time exceeded threshold",
            component_type="database",
        )
        results = match_skills_by_rules(ctx)
        names = [s.name for s, _ in results]
        assert "crash_skill" not in names

    def test_multiple_pattern_hits_raise_score(self):
        register_skill(_CrashSkill)
        ctx_one = SkillContext(
            question="pod failing",
            service_name="svc",
            log_content="CrashLoopBackOff",
            component_type="pod",
        )
        ctx_both = SkillContext(
            question="pod failing",
            service_name="svc",
            log_content="CrashLoopBackOff OOMKilled",
            component_type="pod",
        )
        results_one = match_skills_by_rules(ctx_one)
        results_both = match_skills_by_rules(ctx_both)
        score_one = dict(results_one).get(_REGISTRY.get("crash_skill"))
        score_both = dict(results_both).get(_REGISTRY.get("crash_skill"))
        # Both should be present; with more hits score should be >= one-hit score
        assert score_one is not None
        assert score_both is not None
        assert score_both >= score_one

    def test_results_sorted_descending(self):
        register_skill(_CrashSkill)
        register_skill(_NetworkSkill)
        ctx = SkillContext(
            question="pod failing with network error",
            service_name="svc",
            log_content="CrashLoopBackOff connection refused",
            component_type="pod",
        )
        results = match_skills_by_rules(ctx)
        scores = [score for _, score in results]
        assert scores == sorted(scores, reverse=True)


# ---------------------------------------------------------------------------
# Tests: extract_high_confidence_skills
# ---------------------------------------------------------------------------

class TestExtractHighConfidenceSkills:
    def test_returns_skills_above_threshold(self):
        register_skill(_CrashSkill)
        ctx = SkillContext(
            question="pod failing",
            service_name="svc",
            log_content="CrashLoopBackOff OOMKilled",
            component_type="pod",
        )
        skills = extract_high_confidence_skills(ctx, threshold=0.1)
        assert any(s.name == "crash_skill" for s in skills)

    def test_max_skills_respected(self):
        register_skill(_CrashSkill)
        register_skill(_NetworkSkill)
        ctx = SkillContext(
            question="many errors",
            service_name="svc",
            log_content="CrashLoopBackOff connection refused",
            component_type="pod",
        )
        skills = extract_high_confidence_skills(ctx, threshold=0.0, max_skills=1)
        assert len(skills) <= 1

    def test_low_confidence_excluded(self):
        register_skill(_CrashSkill)
        ctx = SkillContext(
            question="generic question",
            service_name="svc",
            log_content="",
            component_type="database",
        )
        skills = extract_high_confidence_skills(ctx, threshold=0.9)
        assert len(skills) == 0


class TestExtractAutoSelectedSkills:
    def test_rejects_component_only_match(self):
        register_skill(_NetworkSkill)
        ctx = SkillContext(
            question="service health check looks unstable",
            service_name="query-service",
            log_content="health endpoint flaps intermittently",
            component_type="service",
        )
        skills = extract_auto_selected_skills(ctx, threshold=0.35)
        assert skills == []

    def test_catalog_can_include_low_score_skill_without_auto_selecting_it(self):
        register_skill(_NetworkSkill)
        ctx = SkillContext(
            question="service health check looks unstable",
            service_name="query-service",
            log_content="health endpoint flaps intermittently",
            component_type="service",
        )
        catalog = build_skill_catalog_for_prompt(ctx)
        skills = extract_auto_selected_skills(ctx, threshold=0.35)
        assert "network_skill" in catalog
        assert skills == []


# ---------------------------------------------------------------------------
# Tests: build_skill_catalog_for_prompt
# ---------------------------------------------------------------------------

class TestBuildSkillCatalogForPrompt:
    def test_empty_registry_returns_empty_string(self):
        _REGISTRY.clear()
        ctx = SkillContext(question="test")
        catalog = build_skill_catalog_for_prompt(ctx)
        assert catalog == ""

    def test_catalog_contains_skill_name(self):
        register_skill(_CrashSkill)
        ctx = SkillContext(
            question="pod crash",
            service_name="svc",
            log_content="CrashLoopBackOff",
            component_type="pod",
        )
        catalog = build_skill_catalog_for_prompt(ctx)
        assert "crash_skill" in catalog or "Crash Skill" in catalog

    def test_catalog_is_string(self):
        register_skill(_CrashSkill)
        ctx = SkillContext(question="test", component_type="pod")
        result = build_skill_catalog_for_prompt(ctx)
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# Tests: get_skill_selection_summary
# ---------------------------------------------------------------------------

class TestGetSkillSelectionSummary:
    def test_returns_non_empty_string(self):
        register_skill(_CrashSkill)
        ctx = SkillContext(question="pod issue", component_type="pod")
        summary = get_skill_selection_summary(ctx, ["crash_skill"])
        assert isinstance(summary, str)
        assert len(summary) > 0

    def test_includes_count_or_name(self):
        register_skill(_CrashSkill)
        ctx = SkillContext(question="pod issue", component_type="pod")
        summary = get_skill_selection_summary(ctx, ["crash_skill"])
        # Should mention the skill or count
        assert "crash_skill" in summary or "1" in summary

    def test_empty_skill_list(self):
        ctx = SkillContext(question="generic", component_type="pod")
        summary = get_skill_selection_summary(ctx, [])
        assert isinstance(summary, str)
