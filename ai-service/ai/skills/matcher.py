"""
Skill context matcher and LLM prompt catalog builder.

Two public functions:
- ``match_skills_by_rules(context)``  -- rule-based ranking (fast, no LLM)
- ``build_skill_catalog_for_prompt(context)``  -- text block for LLM injection
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Tuple

from ai.skills.base import DiagnosticSkill, SkillContext, SkillMatchDetails
from ai.skills.registry import get_skill_registry

logger = logging.getLogger(__name__)

# Threshold above which a skill is considered "high-confidence" for auto-selection
_HIGH_CONFIDENCE_THRESHOLD = 0.7
_AUTO_SELECT_MIN_SCORE = 0.35


def match_skills_by_rules(
    context: SkillContext,
    *,
    min_score: float = 0.1,
    max_skills: int = 4,
) -> List[Tuple[DiagnosticSkill, float]]:
    """
    Score all registered skills against *context* and return candidates
    sorted by score (descending), filtered by *min_score*, capped at *max_skills*.

    This is the rule-based pass — pure regex and component-type matching,
    no LLM call required.
    """
    registry = get_skill_registry()
    scored: List[Tuple[DiagnosticSkill, float]] = []

    for skill in registry.values():
        try:
            score = skill.match_details(context).total_score
        except Exception:
            logger.exception("match_score raised for skill %r", skill.name)
            score = 0.0
        if score >= min_score:
            scored.append((skill, score))

    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored[:max_skills]


def build_skill_catalog_for_prompt(
    context: SkillContext,
    *,
    max_skills: int = 6,
    include_all: bool = False,
) -> str:
    """
    Build a human-readable (and LLM-parseable) skill catalog for prompt injection.

    The catalog lists available skills with their names, descriptions, and
    applicable components. The LLM can reference skill names in its structured
    output to select which ones to invoke.

    When *include_all* is True every registered skill is listed regardless of
    score; otherwise only those with score >= 0.1 are included (up to
    *max_skills*).

    Returns an empty string if no skills are registered.
    """
    registry = get_skill_registry()
    if not registry:
        return ""

    if include_all:
        candidates = [(skill, skill.match_score(context)) for skill in registry.values()]
        candidates.sort(key=lambda pair: pair[1], reverse=True)
    else:
        candidates = match_skills_by_rules(context, min_score=0.1, max_skills=max_skills)

    if not candidates:
        return ""

    lines: List[str] = [
        "## 可用诊断技能（Diagnostic Skills）",
        "以下技能已注册，可在 actions 中通过 skill_name 字段引用：",
        "",
    ]

    for skill, score in candidates:
        entry = skill.to_catalog_entry()
        confidence = "高" if score >= _HIGH_CONFIDENCE_THRESHOLD else ("中" if score >= 0.4 else "低")
        lines.append(f"### {entry['display_name']} (`{entry['name']}`)")
        lines.append(f"- 描述: {entry['description']}")
        lines.append(f"- 适用组件: {', '.join(entry['applicable_components']) or '通用'}")
        lines.append(f"- 风险等级: {entry['risk_level']}")
        lines.append(f"- 最大步骤数: {entry['max_steps']}")
        lines.append(f"- 上下文匹配置信度: {confidence} ({score:.2f})")
        lines.append("")

    lines.append(
        "若需执行某技能的诊断步骤，在 actions 中添加 `skill_name` 字段指定技能名称，"
        "系统将自动展开为结构化命令。"
    )

    return "\n".join(lines)


def extract_high_confidence_skills(
    context: SkillContext,
    *,
    threshold: float = _HIGH_CONFIDENCE_THRESHOLD,
    max_skills: int = 3,
) -> List[DiagnosticSkill]:
    """
    Return skills whose rule-based score exceeds *threshold*.

    Used by the planning node to auto-inject steps without waiting for LLM
    decision (fast path for obvious scenarios like CrashLoopBackOff).
    """
    candidates = match_skills_by_rules(context, min_score=threshold, max_skills=max_skills)
    return [skill for skill, _score in candidates]


def extract_auto_selected_skills(
    context: SkillContext,
    *,
    threshold: float = _AUTO_SELECT_MIN_SCORE,
    max_skills: int = 3,
) -> List[DiagnosticSkill]:
    """
    Return skills eligible for automatic step injection.

    Unlike prompt/catalog discovery, auto-selection requires direct pattern
    evidence and a stricter threshold so generic component matches do not
    automatically expand into command chains.
    """
    registry = get_skill_registry()
    candidates: List[Tuple[DiagnosticSkill, SkillMatchDetails]] = []

    for skill in registry.values():
        try:
            details = skill.match_details(context)
        except Exception:
            logger.exception("match_details raised for skill %r", skill.name)
            continue
        if details.total_score < threshold:
            continue
        if details.pattern_hits <= 0:
            continue
        candidates.append((skill, details))

    candidates.sort(key=lambda pair: pair[1].total_score, reverse=True)
    return [skill for skill, _details in candidates[:max_skills]]


def get_skill_selection_summary(
    context: SkillContext,
    selected_skill_names: List[str],
) -> str:
    """
    Build a short human-readable summary of the skill selection decision.
    Used for reasoning_step events.
    """
    registry = get_skill_registry()
    selected = [registry[name] for name in selected_skill_names if name in registry]
    if not selected:
        return "未匹配到适用的诊断技能，将使用通用分析流程。"

    parts = [f"「{s.display_name}」" for s in selected]
    return f"已选择 {len(selected)} 个诊断技能：{' + '.join(parts)}"


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()
