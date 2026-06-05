"""Unified command specification data model.

CommandSpec is the single data contract for the entire command pipeline:
LLM output → normalizer → security → compiler → execution.
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class ToolType(str, Enum):
    GENERIC_EXEC = "generic_exec"
    CLICKHOUSE_QUERY = "clickhouse_query"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class CommandType(str, Enum):
    QUERY = "query"
    REPAIR = "repair"


class CommandSpec(BaseModel):
    """Unified command specification — the single data contract.

    LLM tool call output, frontend display, security policy,
    and execution engine all consume this model.
    """
    tool: ToolType
    command: str = ""
    target_kind: str = ""
    target_identity: str = ""
    purpose: str = ""
    risk_level: RiskLevel = RiskLevel.LOW
    command_type: CommandType = CommandType.QUERY
    timeout_seconds: int = Field(default=20, ge=1, le=120)


class CompiledCommand(BaseModel):
    """Compiled command ready for execution.

    Produced by the compiler after security validation passes.
    """
    spec: CommandSpec
    shell_command: str
    route: str = "remote"
    executor_profile: str = ""
    sql_preflight_passed: bool = False

    model_config = {"arbitrary_types_allowed": True}


__all__ = [
    "CommandSpec",
    "CompiledCommand",
    "ToolType",
    "RiskLevel",
    "CommandType",
]
