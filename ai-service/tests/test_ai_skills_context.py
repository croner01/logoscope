"""Tests for build_skills_context — AI skill matching and injection."""
from __future__ import annotations

from unittest.mock import patch, MagicMock

from api.ai import build_skills_context, _match_skill_to_query


def _mock_skill(name, desc, skill_type="diagnostic",
                trigger_patterns=None, applicable_components=None):
    s = MagicMock()
    s.name = name
    s.display_name = name.replace("_", " ").title()
    s.description = desc
    s.skill_type = skill_type
    s.trigger_patterns = trigger_patterns or []
    s.applicable_components = applicable_components or []
    s.step_count = 3
    s.body = f"# {name}\nMethodology content."
    s.auxiliary_files = {"guide.md": "# Guide"}
    return s


class TestMatchSkillToQuery:
    def test_trigger_pattern_match(self):
        skill = _mock_skill("debug", "", trigger_patterns=["crash", "bug"])
        assert _match_skill_to_query(skill, "Pod CrashLoopBackOff")

    def test_component_match(self):
        skill = _mock_skill("k8s", "", applicable_components=["kubernetes"])
        assert _match_skill_to_query(skill, "kubernetes pod failed")

    def test_description_keyword_match(self):
        skill = _mock_skill("sysdebug", "Systematic debugging for any bug")
        assert _match_skill_to_query(skill, "debugging this bug")

    def test_no_match(self):
        skill = _mock_skill("db", "Database diagnostics")
        assert not _match_skill_to_query(skill, "networking issue")


class TestBuildSkillsContext:
    def test_includes_diagnostic_tools(self):
        skills = [_mock_skill("k8s_diag", "K8s diagnostics",
                              applicable_components=["kubernetes"])]
        with patch("ai.skills.manager.SkillManager") as MockMgr:
            MockMgr.return_value.list_all.return_value = skills
            result = build_skills_context("kubernetes issue")
            assert "k8s_diag" in result["diagnostic_tools"]

    def test_includes_reference_methods(self):
        skill = _mock_skill("sysdebug", "Systematic debugging")
        skill.skill_type = "reference"
        with patch("ai.skills.manager.SkillManager") as MockMgr:
            MockMgr.return_value.list_all.return_value = [skill]
            result = build_skills_context("debugging issue")
            assert "sysdebug" in result["reference_methods"]
            assert "Methodology content" in result["reference_methods"]
