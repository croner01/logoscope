"""
Skill YAML exporter — DiagnosticSkill Python class → YAML dict.

Migration helper for converting existing Python-based skills to the
declarative YAML format consumed by both Claude SDK and LangGraph runtimes.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from ai.skills.base import DiagnosticSkill, SkillContext


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _escape_yaml_multiline(text: str) -> str:
    """Basic check if a command string needs YAML literal block scalar."""
    if not text:
        return text
    if "\n" in text or "|" in text or ">" in text:
        return text  # will use literal block in YAML
    return text


def _pattern_to_string(pattern: Any) -> str:
    """Convert a re.Pattern or string to its string form."""
    if isinstance(pattern, re.Pattern):
        return pattern.pattern
    return _as_str(pattern)


def export_skill_to_dict(skill: DiagnosticSkill, context: Optional[SkillContext] = None) -> Dict[str, Any]:
    """Convert a DiagnosticSkill instance to a YAML-serializable dict.

    Args:
        skill: The skill instance to export.
        context: Optional SkillContext for generating steps. If None,
                 a minimal default context is used.

    Returns:
        Dict ready for ``yaml.dump()`` or serialization.
    """
    if context is None:
        context = SkillContext(service_name="{service_name}", namespace="{namespace}")

    steps = []
    for step in skill.plan_steps(context):
        spec = step.command_spec if isinstance(step.command_spec, dict) else {}
        args = spec.get("args") if isinstance(spec.get("args"), dict) else {}

        step_entry: Dict[str, Any] = {
            "id": step.step_id,
            "title": step.title,
            "tool": _as_str(spec.get("tool", "generic_exec")),
            "command": _as_str(args.get("command") or spec.get("command") or step.title),
            "purpose": step.purpose,
            "timeout": int(args.get("timeout_s") or spec.get("timeout_seconds", 20)),
        }

        if step.depends_on:
            step_entry["depends_on"] = list(step.depends_on)
        if step.parse_hints:
            step_entry["parse_hints"] = dict(step.parse_hints)

        steps.append(step_entry)

    trigger_strings = [_pattern_to_string(p) for p in skill.trigger_patterns or []]

    result: Dict[str, Any] = {
        "name": skill.name,
        "display_name": _as_str(skill.display_name),
        "description": _as_str(skill.description),
        "applicable_components": list(skill.applicable_components or []),
        "trigger_patterns": trigger_strings,
        "risk_level": _as_str(skill.risk_level, "low"),
        "max_steps": int(skill.max_steps or 4),
        "steps": steps,
    }

    return {k: v for k, v in result.items() if v}  # strip empty fields


def export_all_skills_to_dir(output_dir: str, registry: Optional[Dict[str, DiagnosticSkill]] = None) -> List[str]:
    """Export all registered skills to YAML files in *output_dir*.

    Args:
        output_dir: Directory to write ``<skill_name>.yaml`` files.
        registry: Skill name → instance map. If None, uses the global registry.

    Returns:
        List of written file paths.
    """
    import os

    if registry is None:
        from ai.skills.registry import get_skill_registry
        registry = get_skill_registry()

    written: List[str] = []
    for name, skill in registry.items():
        data = export_skill_to_dict(skill)
        filepath = os.path.join(output_dir, f"{name}.yaml")
        _write_yaml(data, filepath)
        written.append(filepath)
    return written


def _write_yaml(data: Dict[str, Any], filepath: str) -> None:
    """Write a dict as a clean YAML file."""
    import yaml

    class _IndentSafeDumper(yaml.SafeDumper):
        pass

    def _str_representer(dumper, value):
        """Use literal block scalar for multiline strings."""
        if "\n" in value:
            return dumper.represent_scalar("tag:yaml.org,2002:str", value, style="|")
        return dumper.represent_scalar("tag:yaml.org,2002:str", value)

    _IndentSafeDumper.add_representer(str, _str_representer)

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(f"# Auto-generated from Python skill: {data.get('name', 'unknown')}\n")
        f.write("# Review and adjust template variables (e.g. {service_name}) before use.\n")
        yaml.dump(data, f, Dumper=_IndentSafeDumper, allow_unicode=True, sort_keys=False, default_flow_style=False)


__all__ = ["export_skill_to_dict", "export_all_skills_to_dir"]
