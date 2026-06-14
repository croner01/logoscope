"""Skill format adapters — pluggable format handlers for SkillManager."""
from ai.skills.adapters.base import SkillAdapter, SkillSource
from ai.skills.adapters.yaml_adapter import YamlAdapter
from ai.skills.adapters.markdown_adapter import MarkdownAdapter

__all__ = [
    "SkillAdapter",
    "SkillSource",
    "YamlAdapter",
    "MarkdownAdapter",
]
