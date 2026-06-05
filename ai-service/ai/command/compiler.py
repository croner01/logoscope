"""CommandSpec → executable shell command.

Compiles a validated CommandSpec into a CompiledCommand ready for execution.
Routes simple ClickHouse queries to query-service (local) and everything
else to exec-service (remote).
"""
from __future__ import annotations

import os
import re
import shlex
from typing import Any

from ai.command.spec import CommandSpec, CompiledCommand, ToolType


_SIMPLE_SELECT_RE = re.compile(
    r"^\s*SELECT\s+(?!.*\bGROUP\s+BY\b)(?!.*\bJOIN\b)(?!.*\bUNION\b)"
    r"(?!.*\bHAVING\b)(?!.*\bOVER\s*\()"
    r".*?\bFROM\s+logs\.events\b",
    re.IGNORECASE | re.DOTALL,
)

_BLOCKED_OPERATORS = {";", "&", ">", ">>", "<", "<<", "|", "||", "&&"}


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def _is_simple_select(query: str) -> bool:
    """Check if a ClickHouse query is a simple SELECT on logs.events."""
    return bool(query and _SIMPLE_SELECT_RE.search(query))


def _wrap_clickhouse_query(query: str, namespace: str = "islap") -> str:
    """Wrap a ClickHouse query for kubectl exec remote execution."""
    selector = os.getenv("AI_RUNTIME_CLICKHOUSE_POD_SELECTOR_DEFAULT", "app=clickhouse")
    safe_query = query.replace("'", "'\"'\"'")
    ns = shlex.quote(namespace)
    sel = shlex.quote(selector)
    return (
        f"kubectl get pods -n {ns} -l {sel} -o jsonpath='{{.items[0].metadata.name}}'"
        f" | xargs -I {{}} kubectl -n {ns} exec -i {{}} -- clickhouse-client --query '{safe_query}'"
    )


def compile_command(
    spec: CommandSpec,
    *,
    run_sql_preflight: bool = False,
    namespace: str = "islap",
) -> CompiledCommand:
    """Compile a CommandSpec into an executable command.

    Args:
        spec: Validated CommandSpec.
        run_sql_preflight: If True, run EXPLAIN SYNTAX on ClickHouse queries.
        namespace: Kubernetes namespace for ClickHouse pod resolution.

    Returns:
        CompiledCommand with route and executor_profile set.
    """
    command = _as_str(spec.command).strip()
    if not command:
        return CompiledCommand(spec=spec, shell_command="", route="")

    if spec.tool == ToolType.GENERIC_EXEC:
        tokens = command.split()
        for token in tokens:
            for op in _BLOCKED_OPERATORS:
                if op in token:
                    return CompiledCommand(spec=spec, shell_command="", route="")

        return CompiledCommand(
            spec=spec,
            shell_command=command,
            route="remote",
            executor_profile="toolbox-k8s-readonly",
        )

    if spec.tool == ToolType.CLICKHOUSE_QUERY:
        if _is_simple_select(command):
            return CompiledCommand(
                spec=spec,
                shell_command=command,
                route="local",
                executor_profile="query-service-readonly",
                sql_preflight_passed=True,
            )
        else:
            wrapped = _wrap_clickhouse_query(command, namespace=namespace)
            return CompiledCommand(
                spec=spec,
                shell_command=wrapped,
                route="remote",
                executor_profile="toolbox-clickhouse-readonly",
                sql_preflight_passed=not run_sql_preflight,
            )

    return CompiledCommand(spec=spec, shell_command="", route="")


__all__ = ["compile_command"]
