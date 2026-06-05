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

# Commands that need to run inside the target pod → auto-wrap with kubectl exec
_POD_INTERNAL_COMMANDS = {
    "ls", "cat", "ps", "df", "ss", "curl", "head", "tail",
    "grep", "rg", "awk", "jq", "sed", "echo", "pwd", "find",
    "stat", "du", "wc", "sort", "uniq", "xargs", "env", "id",
    "whoami", "hostname", "ip", "netstat", "lsof", "top", "free",
    "uptime", "uname", "pgrep", "pidof",
}

# Commands that run on the host node → route to SSH gateway
_HOST_COMMANDS = {
    "systemctl", "service", "journalctl", "dmesg",
    "hostnamectl", "timedatectl",
}

# Commands that are already kubectl → pass through to toolbox-gateway
_K8S_COMMANDS = {"kubectl", "helm"}


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def _is_simple_select(query: str) -> bool:
    """Check if a ClickHouse query is a simple SELECT on logs.events."""
    return bool(query and _SIMPLE_SELECT_RE.search(query))


def _extract_head(command: str) -> str:
    """Extract the command head (first token)."""
    parts = command.strip().split()
    if not parts:
        return ""
    head = parts[0]
    if "/" in head:
        head = head.rsplit("/", 1)[-1]
    return head.lower()


def _parse_target_identity(target_identity: str) -> tuple[str, str]:
    """Parse 'pod:<name>/namespace:<ns>' into (pod, namespace)."""
    pod = ""
    ns = ""
    if not target_identity:
        return pod, ns
    for part in target_identity.split("/"):
        part = part.strip()
        if part.startswith("pod:"):
            pod = part[4:]
        elif part.startswith("namespace:"):
            ns = part[10:]
    return pod, ns


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
        # Blocked operator check (before wrapping)
        tokens = command.split()
        for token in tokens:
            for op in _BLOCKED_OPERATORS:
                if op in token:
                    return CompiledCommand(spec=spec, shell_command="", route="")

        head = _extract_head(command)
        pod, ns = _parse_target_identity(spec.target_identity)
        ns = ns or namespace

        # ── Host-level commands → SSH gateway ──────────────────────────
        if head in _HOST_COMMANDS:
            return CompiledCommand(
                spec=spec,
                shell_command=command,
                route="remote",
                executor_profile="host-ssh-readonly",
            )

        # ── K8s commands → toolbox-gateway ─────────────────────────────
        if head in _K8S_COMMANDS:
            return CompiledCommand(
                spec=spec,
                shell_command=command,
                route="remote",
                executor_profile="toolbox-k8s-readonly",
            )

        # ── Pod internal commands → auto-wrap with kubectl exec ────────
        if head in _POD_INTERNAL_COMMANDS:
            if pod:
                ns_flag = f"-n {ns}" if ns else ""
                wrapped = f"kubectl exec {pod} {ns_flag} -- {command}"
                return CompiledCommand(
                    spec=spec,
                    shell_command=wrapped,
                    route="remote",
                    executor_profile="toolbox-k8s-readonly",
                )
            else:
                # No target pod known — run in sandbox (limited use)
                return CompiledCommand(
                    spec=spec,
                    shell_command=command,
                    route="remote",
                    executor_profile="busybox-readonly",
                )

        # ── Unknown commands → best-effort kubectl exec if pod known ───
        if pod:
            ns_flag = f"-n {ns}" if ns else ""
            wrapped = f"kubectl exec {pod} {ns_flag} -- {command}"
            return CompiledCommand(
                spec=spec,
                shell_command=wrapped,
                route="remote",
                executor_profile="toolbox-k8s-readonly",
            )

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
