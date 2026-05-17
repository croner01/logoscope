"""Adapters from OpenHands-style tool calls into runtime v4 command requests."""

from __future__ import annotations

from typing import Any, Dict


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def _safe_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return int(default)
        return int(str(value).strip())
    except Exception:
        return int(default)


def _clamp_timeout(value: Any, default: int = 20) -> int:
    return max(3, min(_as_int(value, default), 180))


def map_tool_call_to_runtime_command(
    *,
    run_id: str,
    tool_name: str,
    tool_args: Dict[str, Any],
) -> Dict[str, Any]:
    """Translate a tool call payload into the existing runtime command shape."""

    safe_args = tool_args if isinstance(tool_args, dict) else {}
    command = _as_str(safe_args.get("command")).strip()
    timeout_seconds = _clamp_timeout(safe_args.get("timeout_s"), default=20)
    command_spec_args = dict(safe_args)
    command_spec_args["timeout_s"] = timeout_seconds
    command_spec_args.setdefault("target_kind", _as_str(safe_args.get("target_kind") or "runtime_node"))
    command_spec_args.setdefault("target_identity", _as_str(safe_args.get("target_identity") or "runtime:local"))
    return {
        "run_id": _as_str(run_id),
        "tool_name": "command.exec",
        "command": command,
        "purpose": _as_str(safe_args.get("purpose") or "OpenHands requested command"),
        "title": _as_str(safe_args.get("title") or "OpenHands 工具调用"),
        "timeout_seconds": timeout_seconds,
        "command_spec": {
            "tool": _as_str(tool_name or "generic_exec"),
            "args": command_spec_args,
        },
        "confirmed": False,
        "elevated": False,
    }


def map_skill_step_to_runtime_command(
    *,
    run_id: str,
    skill_name: str,
    step: Dict[str, Any],
) -> Dict[str, Any]:
    """Translate a DiagnosticSkill step into the runtime command shape."""

    safe_step = _safe_dict(step)
    command_spec = _safe_dict(safe_step.get("command_spec"))
    args = _safe_dict(command_spec.get("args"))
    command = _as_str(command_spec.get("command") or args.get("command")).strip()
    timeout_seconds = _clamp_timeout(command_spec.get("timeout_s") or args.get("timeout_s"), default=20)
    return {
        "run_id": _as_str(run_id),
        "tool_name": "command.exec",
        "skill_name": _as_str(skill_name),
        "step_id": _as_str(safe_step.get("step_id")),
        "command": command,
        "purpose": _as_str(safe_step.get("purpose") or "OpenHands skill requested command"),
        "title": _as_str(safe_step.get("title") or safe_step.get("step_id") or "OpenHands 技能步骤"),
        "timeout_seconds": timeout_seconds,
        "command_spec": command_spec,
        "confirmed": False,
        "elevated": False,
    }
