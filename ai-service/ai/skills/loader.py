"""
Skill YAML loader — YAML → Claude SDK @tool / LangGraph SkillStep.

Converts the declarative YAML skill format into runtime-specific structures:

- ``langgraph`` backend → ``List[SkillStep]`` (for DeepSeek offline mode)
- ``claude_sdk`` backend → ``List[Dict]`` (@tool-compatible function definitions)
- ``mcp`` backend → ``List[Dict]`` (MCP tool definitions)

Directory resolution (three-tier):
    1. ``custom/``     — user-created skills (highest priority)
    2. ``installed/``  — GitHub-installed skills
    3. ``builtin/``    — shipped with the image (fallback)
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Tuple

from ai.skills.base import SkillContext, SkillStep
from ai.skills.builtin._helpers import _as_str


# ── Template variable helpers ──────────────────────────────────────────────

_TEMPLATE_VAR_RE = re.compile(r"\{(\w+)\}")


def _build_template_context(context: SkillContext) -> Dict[str, str]:
    """Build a flat dict of template variables from a SkillContext.

    Supports direct field access + computed derivatives like ``service_filter``.
    """
    ctx: Dict[str, str] = {}

    # Direct fields
    for key in ("question", "service_name", "log_content", "log_level",
                "component_type", "trace_id", "namespace", "request_id",
                "log_timestamp", "correlation_anchor", "correlation_anchor_value"):
        value = _as_str(getattr(context, key, ""))
        if value:
            ctx[key] = value

    # Extra fields from context.extra
    if isinstance(context.extra, dict):
        for key, value in context.extra.items():
            if isinstance(value, (str, int, float)):
                str_val = str(value).strip()
                if str_val and key not in ctx:
                    ctx[key] = str_val

    # Computed derivatives
    svc = ctx.get("service_name", "")
    ctx["service_filter"] = f" | grep -i {svc}" if svc else ""
    ctx["pod_filter"] = ctx.get("pod_name", "")

    return ctx


def _format_command(command: str, context: Dict[str, str]) -> str:
    """Substitute ``{template_vars}`` in a command string.

    Missing variables are silently replaced with empty string (unlike
    ``str.format()`` which raises KeyError).
    """
    def _replacer(match: re.Match) -> str:
        key = match.group(1)
        return context.get(key, "")

    return _TEMPLATE_VAR_RE.sub(_replacer, command)


# ── YAML loading ──────────────────────────────────────────────────────────

def _load_yaml(source: str) -> Dict[str, Any]:
    """Load a YAML skill definition from file path or raw dict."""
    if isinstance(source, dict):
        return source
    if os.path.isfile(source):
        import yaml
        with open(source, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    raise ValueError(f"source must be a file path or dict, got {type(source).__name__}")


def _normalize_skill_yaml(data: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and normalize a loaded YAML skill definition."""
    name = _as_str(data.get("name"))
    if not name:
        raise ValueError("skill YAML is missing 'name' field")

    steps_raw = data.get("steps")
    if not isinstance(steps_raw, list) or not steps_raw:
        raise ValueError(f"skill '{name}' YAML has no 'steps' list")

    normalized = dict(data)
    normalized["steps"] = []
    for i, step in enumerate(steps_raw):
        if not isinstance(step, dict):
            raise ValueError(f"skill '{name}' step {i} is not a dict")
        step_id = _as_str(step.get("id") or step.get("step_id"))
        if not step_id:
            step_id = f"step-{i}"
        normalized["steps"].append({
            "id": step_id,
            "title": _as_str(step.get("title", "")),
            "tool": _as_str(step.get("tool", "generic_exec")),
            "command": _as_str(step.get("command", "")),
            "purpose": _as_str(step.get("purpose", "")),
            "depends_on": list(step.get("depends_on") or []),
            "timeout": int(step.get("timeout") or step.get("timeout_seconds", 20)),
            "parse_hints": dict(step.get("parse_hints") or {}),
        })
    return normalized


# ── Backend-specific loaders ──────────────────────────────────────────────

def load_skill_steps(source: Any, context: Optional[SkillContext] = None) -> List[SkillStep]:
    """Load YAML skill → ``List[SkillStep]`` (LangGraph/offline mode).

    Args:
        source: YAML file path, or already-loaded dict.
        context: Optional SkillContext for template substitution.
                 If None, template variables remain as-is.

    Returns:
        List of ``SkillStep`` instances consumable by the LangGraph inner loop.
    """
    data = _normalize_skill_yaml(_load_yaml(source))
    ctx_dict = _build_template_context(context) if context else {}

    steps: List[SkillStep] = []
    for step_data in data["steps"]:
        raw_cmd = step_data["command"]
        command = _format_command(raw_cmd, ctx_dict) if ctx_dict else raw_cmd

        command_spec: Dict[str, Any] = {
            "tool": step_data["tool"],
            "command": command,
            "target_kind": "k8s_cluster",
            "target_identity": "",
            "timeout_seconds": step_data["timeout"],
        }

        steps.append(SkillStep(
            step_id=step_data["id"],
            title=step_data["title"],
            command_spec=command_spec,
            purpose=step_data["purpose"],
            depends_on=list(step_data["depends_on"]),
            parse_hints=dict(step_data["parse_hints"]),
        ))

    return steps


def load_tool_definitions(source: Any) -> List[Dict[str, Any]]:
    """Load YAML skill → ``List[Dict]`` (Claude SDK @tool definitions).

    Returns a list of tool definition dicts that can be converted to
    ``@tool`` decorated functions or injected into the Claude SDK tool list::

        tools = load_tool_definitions("skills/builtin/k8s_pod.yaml")
        for t in tools:
            # t["name"], t["description"], t["input_schema"]
            # t["steps"] — ordered tool calls for this skill
    """
    data = _normalize_skill_yaml(_load_yaml(source))
    name = data["name"]

    # Build the steps summary for the tool description
    step_titles = [s["title"] for s in data["steps"]]
    tool_desc = data.get("description", "")
    tool_desc += f"\\n\\n执行步骤：\\n" + "\\n".join(f"  {i+1}. {t}" for i, t in enumerate(step_titles))

    return [
        {
            "name": name,
            "description": tool_desc[:1024],
            "input_schema": _build_input_schema(data["steps"]),
            "steps": data["steps"],  # preserved for the executor
            "risk_level": _as_str(data.get("risk_level"), "low"),
            "max_iterations": int(data.get("max_steps", 4)),
        }
    ]


def _build_input_schema(steps: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Build JSON Schema for a skill tool, using the first step's params as hint."""
    return {
        "type": "object",
        "properties": {
            "context": {
                "type": "object",
                "description": "Diagnostic context (service_name, namespace, etc.)",
                "properties": {
                    "service_name": {"type": "string", "description": "Target service name"},
                    "namespace": {"type": "string", "description": "Kubernetes namespace"},
                },
            }
        },
        "required": [],
    }


# ── Skill metadata helpers ────────────────────────────────────────────────

def get_skill_metadata(source: Any) -> Dict[str, Any]:
    """Extract just the metadata from a YAML skill (no steps)."""
    data = _normalize_skill_yaml(_load_yaml(source))
    return {
        "name": data.get("name"),
        "display_name": data.get("display_name", data.get("name")),
        "description": _as_str(data.get("description")),
        "applicable_components": list(data.get("applicable_components") or []),
        "trigger_patterns": list(data.get("trigger_patterns") or []),
        "risk_level": _as_str(data.get("risk_level"), "low"),
        "step_count": len(data.get("steps", [])),
    }


def match_skill_yaml(source: Any, context: SkillContext, min_score: float = 0.1) -> float:
    """Score a YAML skill against a SkillContext, same algorithm as ``DiagnosticSkill.match_score()``."""
    meta = get_skill_metadata(source)
    text = context.combined_text().lower()
    if not text:
        return 0.0

    patterns = meta.get("trigger_patterns", [])
    hits = sum(1 for p in patterns if re.search(p, text, re.IGNORECASE))
    pattern_score = min(1.0, hits / max(len(patterns), 1))

    component_bonus = 0.0
    ct = _as_str(context.component_type).lower()
    if ct and meta.get("applicable_components"):
        for comp in meta["applicable_components"]:
            if comp.lower() in ct or ct in comp.lower():
                component_bonus = 0.2
                break

    total = min(1.0, pattern_score + component_bonus)
    return total if total >= min_score else 0.0


__all__ = [
    "load_skill_steps",
    "load_tool_definitions",
    "get_skill_metadata",
    "match_skill_yaml",
    "resolve_skill_path",
    "list_skill_names",
    "scan_skill_directories",
    "_build_template_context",
    "_format_command",
]


# ═════════════════════════════════════════════════════════════════════════════
# Three-directory resolution
# ═════════════════════════════════════════════════════════════════════════════

SKILL_DIRECTORIES = {
    "custom": os.path.join(os.path.dirname(__file__), "custom"),
    "installed": os.path.join(os.path.dirname(__file__), "installed"),
    "builtin": os.path.join(os.path.dirname(__file__), "builtin"),
}


def resolve_skill_path(name: str, *, source: Optional[str] = None) -> Optional[str]:
    """Resolve a skill name to its YAML file path across the three directories.

    Priority: custom > installed > builtin.
    When *source* is set (e.g. ``"custom"``), only that directory is searched.

    Returns:
        Absolute file path, or None if not found.
    """
    dirs = []
    if source:
        path = SKILL_DIRECTORIES.get(source)
        if path:
            dirs.append((source, path))
    else:
        # Highest priority first
        for label in ("custom", "installed", "builtin"):
            path = SKILL_DIRECTORIES.get(label)
            if path:
                dirs.append((label, path))

    for label, dir_path in dirs:
        if not os.path.isdir(dir_path):
            continue
        candidate = os.path.join(dir_path, f"{name}.yaml")
        if os.path.isfile(candidate):
            return candidate

    # Also search for any .yaml file whose internal name matches
    if not source:
        for label, dir_path in reversed(dirs):
            if not os.path.isdir(dir_path):
                continue
            for fname in os.listdir(dir_path):
                if not fname.endswith(".yaml"):
                    continue
                filepath = os.path.join(dir_path, fname)
                meta = get_skill_metadata(filepath)
                if meta.get("name") == name:
                    return filepath

    return None


def list_skill_names() -> Dict[str, str]:
    """Return a mapping ``{skill_name: source_label}`` for all visible skills.

    On name collision, custom > installed > builtin (same priority rule).
    """
    import yaml
    seen: Dict[str, str] = {}
    for label, dir_path in reversed(list(SKILL_DIRECTORIES.items())):
        if not os.path.isdir(dir_path):
            continue
        for fname in sorted(os.listdir(dir_path)):
            if not fname.endswith(".yaml"):
                continue
            filepath = os.path.join(dir_path, fname)
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f)
                if isinstance(data, dict) and data.get("name"):
                    seen[str(data["name"])] = label
            except Exception:
                continue
    return seen


def scan_skill_directories() -> Dict[str, List[Dict[str, Any]]]:
    """Scan all three directories and return grouped metadata.

    Returns:
        ``{"builtin": [...], "installed": [...], "custom": [...]}``
    """
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for label, dir_path in SKILL_DIRECTORIES.items():
        if not os.path.isdir(dir_path):
            continue
        entries: List[Dict[str, Any]] = []
        for fname in sorted(os.listdir(dir_path)):
            if not fname.endswith(".yaml"):
                continue
            filepath = os.path.join(dir_path, fname)
            meta = get_skill_metadata(filepath)
            if meta.get("name"):
                meta["_file"] = filepath
                meta["_source"] = label
                entries.append(meta)
        groups[label] = entries
    return groups


def load_skill_by_name(
    name: str,
    context: Optional[SkillContext] = None,
    backend: str = "langgraph",
) -> Any:
    """Load a skill by name (resolved across three directories).

    Args:
        name: Skill name.
        context: Optional SkillContext for template substitution.
        backend: ``"langgraph"`` (returns ``List[SkillStep]``) or
                 ``"claude_sdk"`` (returns ``List[Dict]`` tool definitions).

    Returns:
        The backend-specific skill representation, or None if not found.
    """
    path = resolve_skill_path(name)
    if path is None:
        return None

    if backend == "langgraph":
        return load_skill_steps(path, context)
    elif backend == "claude_sdk":
        return load_tool_definitions(path)
    else:
        raise ValueError(f"Unknown backend: {backend!r}")
