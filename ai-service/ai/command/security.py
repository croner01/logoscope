"""Unified command security — single allowlist, classification, and cost gate.

This is the ONLY place in the codebase that defines what commands
are allowed, how they are classified, and whether they need approval.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ai.command.spec import CommandSpec, CommandType, RiskLevel


# ── Single allowlist ──────────────────────────────────────────────────────

ALLOWED_HEADS: set[str] = {
    "kubectl", "curl",
    "clickhouse-client", "clickhouse",
    "grep", "rg", "cat", "tail", "head", "awk", "jq",
    "ls", "echo", "pwd", "sed", "helm",
    "systemctl", "service",
    "openstack", "psql", "postgres", "mysql", "mariadb",
    "timeout", "ps", "ss",
}

BLOCKED_OPERATORS: set[str] = {";", "&", ">", ">>", "<", "<<", "|", "||", "&&"}

_ALL_NAMESPACES_RE = re.compile(r"(?:\s|^)-(?:-all-namespaces|A)(?:\s|$)")


def _as_str(value, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def _extract_head(command: str) -> str:
    """Extract the command head (first token) from a shell command."""
    text = _as_str(command).strip()
    if not text:
        return ""
    parts = text.split()
    if not parts:
        return ""
    head = parts[0]
    if "/" in head:
        head = head.rsplit("/", 1)[-1]
    return head.lower()


# ── Data types ────────────────────────────────────────────────────────────

@dataclass
class SecurityDecision:
    allowed: bool
    reason: str = ""
    requires_approval: bool = False
    requires_elevation: bool = False
    command_type: CommandType = CommandType.QUERY
    risk_level: RiskLevel = RiskLevel.LOW


@dataclass
class SessionCostState:
    commands_executed: int = 0
    estimated_rows_scanned: int = 0
    targets_touched: set = field(default_factory=set)
    session_command_limit: int = 10


# ── Main entry point ──────────────────────────────────────────────────────

def evaluate_command(
    spec: CommandSpec,
    *,
    session_cost: SessionCostState,
    write_enabled: bool = False,
) -> SecurityDecision:
    """Evaluate a CommandSpec against all security policies.

    This is the single entry point for command security evaluation.
    Call it before compiling or executing any command.

    Checks (first failure short-circuits):
    1. Command head not in ALLOWED_HEADS → blocked
    2. Contains BLOCKED_OPERATORS → blocked
    3. Write command with write_enabled=False → blocked
    4. Write command → requires_elevation
    5. Cost threshold exceeded → requires_approval
    6. All-namespaces flag → requires_approval
    7. Default → auto
    """
    command = _as_str(spec.command).strip()
    if not command:
        return SecurityDecision(allowed=False, reason="Empty command")

    head = _extract_head(command)
    if not head or head not in ALLOWED_HEADS:
        return SecurityDecision(
            allowed=False,
            reason=f"Command head '{head or '(empty)'}' not in allowlist",
        )

    # Check blocked operators in each token
    tokens = command.split()
    for token in tokens:
        for op in BLOCKED_OPERATORS:
            if op in token and token != op:
                # Operator embedded in another token, e.g. "pods;"
                return SecurityDecision(
                    allowed=False,
                    reason=f"Blocked operator '{op}' found in token '{token}'",
                )
            if token == op:
                return SecurityDecision(
                    allowed=False,
                    reason=f"Blocked operator '{op}' in command",
                )

    # Write command handling
    if spec.command_type == CommandType.REPAIR:
        if not write_enabled:
            return SecurityDecision(
                allowed=False,
                reason="Write commands are disabled (AI_FOLLOWUP_COMMAND_WRITE_ENABLED=false)",
            )
        return SecurityDecision(
            allowed=True,
            requires_elevation=True,
            command_type=CommandType.REPAIR,
            risk_level=RiskLevel.HIGH,
        )

    # Cost preflight — session command limit
    if session_cost.commands_executed >= session_cost.session_command_limit:
        return SecurityDecision(
            allowed=True,
            requires_approval=True,
            reason=f"Session command limit reached ({session_cost.commands_executed}/{session_cost.session_command_limit})",
            command_type=CommandType.QUERY,
        )

    # Cost preflight — all-namespaces
    if _ALL_NAMESPACES_RE.search(command):
        return SecurityDecision(
            allowed=True,
            requires_approval=True,
            reason="Command uses --all-namespaces / -A, wide scope requires approval",
            command_type=CommandType.QUERY,
        )

    # Default: allowed, auto-execute
    return SecurityDecision(
        allowed=True,
        command_type=CommandType.QUERY,
        risk_level=RiskLevel.LOW,
    )


__all__ = [
    "evaluate_command",
    "SecurityDecision",
    "SessionCostState",
    "ALLOWED_HEADS",
    "BLOCKED_OPERATORS",
]
