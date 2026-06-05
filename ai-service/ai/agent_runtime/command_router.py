"""
Dual-channel command router.

Routes command_specs to the appropriate execution channel:
- local: query-service API for simple log queries
- remote: exec-service for kubectl, pod exec, complex SQL, host commands
"""
from __future__ import annotations

import re
from typing import Any, Dict, Tuple


_ROUTE_LOCAL = "local"
_ROUTE_REMOTE = "remote"

_CLICKHOUSE_TOOLS = {"kubectl_clickhouse_query", "clickhouse_query", "k8s_clickhouse_query"}

_SIMPLE_SELECT_RE = re.compile(
    r"^\s*SELECT\s+(?!.*\bGROUP\s+BY\b)(?!.*\bJOIN\b)(?!.*\bUNION\b)"
    r"(?!.*\bHAVING\b)(?!.*\bOVER\s*\()"
    r".*?\bFROM\s+logs\.events\b",
    re.IGNORECASE | re.DOTALL,
)

_K8S_TOOLS = {"generic_exec"}


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


class CommandRouter:
    """Routes command_spec to the appropriate execution channel."""

    ROUTE_LOCAL = _ROUTE_LOCAL
    ROUTE_REMOTE = _ROUTE_REMOTE

    def route(self, command_spec: Dict[str, Any]) -> Tuple[str, str]:
        """Return (channel, reason) for the given command_spec.

        ``channel`` is ``"local"`` (query-service) or ``"remote"`` (exec-service).
        ``reason`` is a human-readable explanation of the routing decision.
        """
        if not isinstance(command_spec, dict) or not command_spec:
            return (_ROUTE_REMOTE, "empty or invalid command_spec, defaulting to remote")

        tool = _as_str(command_spec.get("tool")).strip().lower()
        if not tool:
            return (_ROUTE_REMOTE, "no tool specified, defaulting to remote")

        args = command_spec.get("args") if isinstance(command_spec.get("args"), dict) else {}
        query = _as_str(args.get("query") or command_spec.get("query"))

        # ClickHouse tool → check SQL complexity
        if tool in _CLICKHOUSE_TOOLS:
            if query and _SIMPLE_SELECT_RE.search(query):
                return (_ROUTE_LOCAL, "simple ClickHouse log query → query-service")
            return (_ROUTE_REMOTE, "complex ClickHouse SQL → exec-service kubectl exec")

        # All other tools go remote
        if tool in _K8S_TOOLS:
            command = _as_str(args.get("command") or command_spec.get("command")).strip().lower()
            target_kind = _as_str(args.get("target_kind") or command_spec.get("target_kind")).strip().lower()
            if target_kind in {"host_node"} or command.startswith("systemctl") or command.startswith("service "):
                return (_ROUTE_REMOTE, "host-level command → exec-service ssh-gateway")
            return (_ROUTE_REMOTE, "shell/k8s command → exec-service toolbox-gateway")

        return (_ROUTE_REMOTE, f"unknown tool '{tool}', defaulting to remote")


__all__ = ["CommandRouter"]
