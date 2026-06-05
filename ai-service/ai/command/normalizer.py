"""LLM output → normalized CommandSpec.

Converts raw dict from LLM tool calling into a validated CommandSpec.
Auto-infers target_kind/target_identity from source_target metadata,
and command_type from SQL inspection.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from ai.command.spec import CommandSpec, ToolType, CommandType


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def _infer_command_type(command: str, tool: ToolType) -> CommandType:
    """Infer command type from command text."""
    text = _as_str(command).strip().upper()
    if tool == ToolType.CLICKHOUSE_QUERY:
        if text.startswith(("SELECT", "SHOW", "DESCRIBE", "EXPLAIN")):
            return CommandType.QUERY
        return CommandType.REPAIR
    # generic_exec — check for kubectl write verbs
    lower = text.lower()
    write_verbs = ("delete", "apply", "patch", "edit", "create", "update", "scale", "drain", "cordon", "uncordon")
    for verb in write_verbs:
        if lower.startswith(f"kubectl {verb}"):
            return CommandType.REPAIR
    return CommandType.QUERY


def _build_target_identity(source_target: Optional[dict]) -> str:
    """Build target_identity from source_target metadata."""
    if not isinstance(source_target, dict):
        return ""
    pod = _as_str(source_target.get("pod_name")).strip()
    ns = _as_str(source_target.get("namespace")).strip()
    if pod and ns:
        return f"pod:{pod}/namespace:{ns}"
    if pod:
        return f"pod:{pod}"
    if ns:
        return f"namespace:{ns}"
    return ""


def normalize_command_spec(
    raw: Dict[str, Any],
    *,
    source_target: Optional[Dict[str, Any]] = None,
) -> CommandSpec:
    """Normalize a raw LLM tool call dict into a validated CommandSpec.

    Args:
        raw: Raw dict from LLM tool call.
        source_target: Optional metadata from log entry (pod/namespace/node/labels).

    Returns:
        Validated CommandSpec.

    Raises:
        pydantic.ValidationError: If the raw dict fails validation.
    """
    safe = raw if isinstance(raw, dict) else {}

    tool_str = _as_str(safe.get("tool")).strip().lower()
    command = _as_str(safe.get("command") or safe.get("query")).strip()

    if not command:
        from pydantic import ValidationError
        raise ValidationError.from_exception_data(
            "CommandSpec", [{"type": "missing", "loc": ("command",), "msg": "command is required"}]
        )

    # Infer target if missing
    target_kind = _as_str(safe.get("target_kind")).strip()
    target_identity = _as_str(safe.get("target_identity")).strip()
    if not target_kind and source_target:
        if isinstance(source_target, dict) and source_target.get("pod_name"):
            target_kind = "k8s_cluster"
    if not target_identity and source_target:
        target_identity = _build_target_identity(source_target)

    try:
        tool = ToolType(tool_str) if tool_str else ToolType.GENERIC_EXEC
    except ValueError:
        from pydantic import ValidationError
        raise ValidationError.from_exception_data(
            "CommandSpec",
            [{"type": "enum", "loc": ("tool",), "msg": f"'{tool_str}' is not a valid ToolType", "ctx": {"expected": ", ".join(t.value for t in ToolType)}}],
        )
    cmd_type = _infer_command_type(command, tool)

    return CommandSpec(
        tool=tool,
        command=command,
        target_kind=target_kind,
        target_identity=target_identity,
        purpose=_as_str(safe.get("purpose")).strip(),
        command_type=cmd_type,
        timeout_seconds=int(safe.get("timeout_seconds", 20)),
    )


__all__ = ["normalize_command_spec"]
