"""
ai.skills — Diagnostic skill registry and built-in skills.

Importing this package auto-registers all built-in skills. External code
should use:

    from ai.skills import get_skill_registry, register_skill
    from ai.skills.matcher import match_skills_by_rules, build_skill_catalog_for_prompt
    from ai.skills.base import DiagnosticSkill, SkillContext, SkillStep
"""

from ai.skills.base import DiagnosticSkill, SkillContext, SkillStep  # noqa: F401
from ai.skills.registry import (  # noqa: F401
    get_skill,
    get_skill_registry,
    list_skills,
    match_skills,
    register_skill,
)

# Trigger registration of all built-in skills
import ai.skills.builtin  # noqa: F401

__all__ = [
    "DiagnosticSkill",
    "SkillContext",
    "SkillStep",
    "get_skill",
    "get_skill_registry",
    "list_skills",
    "match_skills",
    "register_skill",
]
