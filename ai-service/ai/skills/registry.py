"""
Skill registry: registration decorator and global skill store.

Usage:
    from ai.skills.registry import register_skill, get_skill_registry

    @register_skill
    class MySkill(DiagnosticSkill):
        name = "my_skill"
        ...
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple, Type

from ai.skills.base import DiagnosticSkill, SkillContext

logger = logging.getLogger(__name__)

# Module-level registry: skill name -> instance
_REGISTRY: Dict[str, DiagnosticSkill] = {}


def register_skill(cls: Type[DiagnosticSkill]) -> Type[DiagnosticSkill]:
    """
    Class decorator that registers a DiagnosticSkill in the global registry.

    The class is instantiated once (singleton) and stored by skill name.
    Duplicate names raise RuntimeError to catch configuration mistakes.

    Example::

        @register_skill
        class K8sPodDiagnosticsSkill(DiagnosticSkill):
            name = "k8s_pod_diagnostics"
            ...
    """
    if not issubclass(cls, DiagnosticSkill):
        raise TypeError(f"@register_skill requires a DiagnosticSkill subclass, got {cls!r}")

    instance = cls()
    skill_name = instance.name

    if not skill_name:
        raise ValueError(f"{cls.__name__} must set a non-empty `name` attribute")

    if skill_name in _REGISTRY:
        existing = _REGISTRY[skill_name]
        if type(existing) is not cls:
            raise RuntimeError(
                f"Skill name conflict: '{skill_name}' is already registered by "
                f"{type(existing).__name__!r}, cannot re-register as {cls.__name__!r}"
            )
        # Same class re-imported (e.g. hot-reload); silently keep existing
        return cls

    _REGISTRY[skill_name] = instance
    logger.debug("Registered skill: %s (%s)", skill_name, cls.__name__)
    return cls


def get_skill_registry() -> Dict[str, DiagnosticSkill]:
    """Return a shallow copy of the current registry (name -> instance)."""
    return dict(_REGISTRY)


def get_skill(name: str) -> Optional[DiagnosticSkill]:
    """Retrieve a single skill by name, or None if not registered."""
    return _REGISTRY.get(_as_str(name))


def list_skills() -> List[DiagnosticSkill]:
    """Return all registered skills as a list."""
    return list(_REGISTRY.values())


def match_skills(
    context: SkillContext,
    *,
    min_score: float = 0.1,
    max_skills: int = 4,
) -> List[Tuple[DiagnosticSkill, float]]:
    """
    Score all registered skills against *context* and return those that
    pass *min_score*, sorted descending by score, capped at *max_skills*.

    Returns list of (skill, score) tuples.
    """
    scored: List[Tuple[DiagnosticSkill, float]] = []
    for skill in _REGISTRY.values():
        try:
            score = skill.match_score(context)
        except Exception:
            logger.exception("match_score failed for skill %r", skill.name)
            score = 0.0
        if score >= min_score:
            scored.append((skill, score))

    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored[:max_skills]


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()
