"""
Shared helper functions for all builtin diagnostic skills.

Centralises _as_str / _generic_exec / _clickhouse_query so that every
builtin skill file can import from here instead of re-defining them.
"""

from __future__ import annotations

from typing import Any


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip() if not isinstance(value, str) else value.strip()


def _generic_exec(command: str, *, timeout_s: int = 20) -> dict:
    """
    Build a generic_exec command_spec for shell commands
    (kubectl, curl, grep, ps, df, …).
    """
    return {
        "tool": "generic_exec",
        "args": {
            "command": command,
            "target_kind": "runtime_node",
            "target_identity": "runtime:local",
            "timeout_s": timeout_s,
        },
        "command": command,
        "timeout_s": timeout_s,
    }


def _clickhouse_query(sql: str, *, database: str = "logs", timeout_s: int = 45) -> dict:
    """
    Build a kubectl_clickhouse_query command_spec for ClickHouse SQL diagnostics.
    """
    return {
        "tool": "kubectl_clickhouse_query",
        "args": {
            "target_kind": "clickhouse_cluster",
            "target_identity": f"database:{database}",
            "query": sql,
            "timeout_s": timeout_s,
        },
        "command": f"clickhouse-client --query {sql!r}",
        "timeout_s": timeout_s,
    }


def _escape_sql_string(value: str) -> str:
    """Escape a string value for safe embedding in a ClickHouse SQL literal."""
    return value.replace("\\", "\\\\").replace("'", "\\'")
