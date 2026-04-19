"""
ai.skills — Diagnostic skill registry, infrastructure, and workflow modules.

Importing this package auto-registers all built-in skills. External code
should use:

    # Skill registry and base classes
    from ai.skills import get_skill_registry, register_skill
    from ai.skills.matcher import match_skills_by_rules, build_skill_catalog_for_prompt
    from ai.skills.base import DiagnosticSkill, SkillContext, SkillStep

    # Diagnostic skills
    from ai.skills.diagnostics import (
        K8sDiagnosticSkill,
        OpenStackDiagnosticSkill,
        MariaDBDiagnosticSkill,
        LinuxDiagnosticSkill,
        LogAnalysisDiagnosticSkill,
    )

    # Infrastructure skills
    from ai.skills.infrastructure import (
        CommandDeduplicator,
        OutputFormatter,
        ScopeAutoDetector,
        get_deduplicator,
        get_formatter,
        get_detector,
    )

    # Workflow orchestration
    from ai.skills.workflow import (
        AnalysisWorkflowOrchestrator,
        AnalysisPhase,
        get_orchestrator,
    )

    # Knowledge management
    from ai.skills.knowledge import (
        KnowledgeBaseManager,
        AnalysisCase,
        get_knowledge_base,
        init_knowledge_base,
    )
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

# Trigger registration of diagnostic skills
import ai.skills.diagnostics  # noqa: F401

__all__ = [
    # Base classes
    "DiagnosticSkill",
    "SkillContext",
    "SkillStep",
    # Registry
    "get_skill",
    "get_skill_registry",
    "list_skills",
    "match_skills",
    "register_skill",
]
