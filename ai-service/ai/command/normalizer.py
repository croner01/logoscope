"""LLM output → normalized CommandSpec.

Converts raw dict from LLM tool calling into a validated CommandSpec.
Auto-infers target_kind/target_identity from source_target metadata,
and command_type from SQL inspection.
Also detects kubectl exec clickhouse patterns and upgrades them to
the proper CLICKHOUSE_QUERY tool type.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Optional

from ai.command.spec import CommandSpec, ToolType, CommandType


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


_CLICKHOUSE_EXEC_PATTERN = re.compile(
    r"""
    kubectl\s+exec\s+
    (?:\S+\s+)?                          # optional pod/deployment name
    (?:-n\s+\S+\s+)?                     # optional -n namespace
    (?:--\s+)?                           # optional --
    (?:timeout\s+\d+\s+)?                # optional timeout prefix
    clickhouse(?:-client)?\s+
    (?:-q\s+|--query\s+)
    ["'](.+?)["']                         # extracted SQL query
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _detect_clickhouse_exec_mode(command: str) -> Optional[str]:
    """Detect if a *generic_exec* command is actually a kubectl exec to clickhouse-client.

    Matches::

        kubectl exec <pod> -n <ns> -- clickhouse-client --query "SELECT ..."
        kubectl exec deploy/clickhouse -n islap -- clickhouse -q "SHOW TABLES"

    Returns the extracted SQL query if matched, or *None*.
    """
    match = _CLICKHOUSE_EXEC_PATTERN.search(command)
    if match:
        return match.group(1).strip()
    return None


def _infer_command_type(command: str, tool: ToolType) -> CommandType:
    """Infer command type from command text."""
    text = _as_str(command).strip().upper()
    if tool == ToolType.CLICKHOUSE_QUERY:
        if text.startswith(("SELECT", "SHOW", "DESCRIBE", "EXPLAIN")):
            return CommandType.QUERY
        return CommandType.REPAIR
    # generic_exec — check for kubectl write verbs
    lower = text.lower()
    write_verbs = (
        "delete", "apply", "patch", "edit", "create", "update",
        "scale", "drain", "cordon", "uncordon",
        "set", "label", "annotate", "rollout",
    )
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

    # Cluster ID from raw dict or source_target
    target_cluster_id = _as_str(safe.get("target_cluster_id")).strip()
    if not target_cluster_id and source_target:
        target_cluster_id = _as_str(source_target.get("cluster_id")).strip()

    # Fallback: resolve cluster_id from target registry by namespace
    if not target_cluster_id and source_target:
        _ns = _as_str(source_target.get("namespace")).strip()
        if _ns:
            try:
                from ai.runtime_v4.targets.service import get_runtime_v4_target_registry
                _registry = get_runtime_v4_target_registry()
                # 前缀匹配：兼容 namespace:ns 和 namespace:ns/cluster:xxx 格式
                _t = _registry.find_target_by_identity(
                    target_kind="k8s_cluster",
                    target_identity=f"namespace:{_ns}",
                    prefix_match=True,
                )
                if _t and isinstance(_t, dict):
                    _meta = _t.get("metadata") or {}
                    if isinstance(_meta, dict):
                        target_cluster_id = _as_str(_meta.get("cluster_id")).strip()
            except Exception:
                pass

    # Backward-compat aliases
    _TOOL_ALIASES = {"kubectl_clickhouse_query": "clickhouse_query", "k8s_clickhouse_query": "clickhouse_query"}
    tool_str = _TOOL_ALIASES.get(tool_str, tool_str)

    # Auto-upgrade generic_exec → clickhouse_query when command is
    # kubectl exec pod -- clickhouse-client --query "SELECT..."
    if tool_str == "generic_exec" and command:
        clickhouse_query = _detect_clickhouse_exec_mode(command)
        if clickhouse_query:
            tool_str = "clickhouse_query"
            command = clickhouse_query  # strip the kubectl wrapper, keep just SQL
            # Also upgrade target if source_target provides k8s context
            if not target_kind and source_target:
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
        target_cluster_id=target_cluster_id,
        purpose=_as_str(safe.get("purpose")).strip(),
        command_type=cmd_type,
        timeout_seconds=int(safe.get("timeout_seconds", 20)),
    )


__all__ = ["normalize_command_spec"]
