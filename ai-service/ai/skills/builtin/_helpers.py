"""
Shared helper functions for all builtin diagnostic skills.

Centralises _as_str / _generic_exec / _clickhouse_query so that every
builtin skill file can import from here instead of re-defining them.

All returned dicts are compatible with ai.command.spec.CommandSpec.
Use build_command_spec() to get a validated CommandSpec object.
See ai/command/spec.py for the canonical data model.
"""
from __future__ import annotations

from typing import Any

from ai.command.spec import CommandSpec, ToolType


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip() if not isinstance(value, str) else value.strip()


def _generic_exec(command: str, *, timeout_s: int = 20) -> dict:
    """Build a generic_exec command_spec dict (compatible with CommandSpec)."""
    return {
        "tool": "generic_exec",
        "args": {
            "command": command,
            "target_kind": "k8s_cluster",
            "target_identity": "",
            "timeout_s": timeout_s,
        },
        "command": command,
        "target_kind": "k8s_cluster",
        "target_identity": "",
        "timeout_seconds": timeout_s,
    }


def _clickhouse_query(sql: str, *, database: str = "logs", timeout_s: int = 45) -> dict:
    """Build a kubectl_clickhouse_query command_spec dict (compatible with CommandSpec).

    Uses 'kubectl_clickhouse_query' tool name for backward compatibility.
    The normalizer maps it to ToolType.CLICKHOUSE_QUERY.
    """
    return {
        "tool": "kubectl_clickhouse_query",
        "args": {
            "target_kind": "clickhouse_cluster",
            "target_identity": f"database:{database}",
            "query": sql,
            "timeout_s": timeout_s,
        },
        "command": sql,
        "target_kind": "clickhouse_cluster",
        "target_identity": f"database:{database}",
        "timeout_seconds": timeout_s,
    }


def build_command_spec(skill_dict: dict, *, purpose: str = "", source_target: dict | None = None) -> CommandSpec:
    """Convert a skill-generated dict to a validated CommandSpec.

    Args:
        skill_dict: Dict from _generic_exec() or _clickhouse_query().
        purpose: Human-readable purpose of the command.
        source_target: Optional metadata for target inference.

    Returns:
        Validated CommandSpec.
    """
    from ai.command.normalizer import normalize_command_spec

    merged = dict(skill_dict)
    if purpose:
        merged["purpose"] = purpose

    return normalize_command_spec(merged, source_target=source_target)


def _escape_sql_string(value: str) -> str:
    """Escape a string value for safe embedding in a ClickHouse SQL literal."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


__all__ = [
    "_as_str",
    "_generic_exec",
    "_clickhouse_query",
    "build_command_spec",
    "_escape_sql_string",
]
