"""Unified tool execution adapter.

Routes all commands to exec-service (remote).  The local query-service
fast path has been removed — query-service has no raw-SQL endpoint.
"""
from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from typing import Any

from ai.command.compiler import CompiledCommand

import requests


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


@dataclass
class ToolResult:
    success: bool
    status: str = ""
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0
    channel: str = ""
    error: str = ""


class ToolAdapter:
    """Executes compiled commands via exec-service (remote)."""

    def __init__(
        self,
        exec_service_url: str | None = None,
    ):
        self._exec_url = (
            exec_service_url or os.getenv("EXEC_SERVICE_BASE_URL", "http://exec-service:8095")
        ).rstrip("/")

    async def execute(
        self,
        compiled: CompiledCommand,
        *,
        session_id: str = "",
        message_id: str = "",
        action_id: str = "",
    ) -> ToolResult:
        """Execute a compiled command via exec-service.

        Args:
            compiled: The CompiledCommand to execute.
            session_id: Diagnostic session ID (for audit trail correlation).
            message_id: Message ID (for audit trail correlation).
            action_id: Action ID (for audit trail correlation).

        Returns:
            ToolResult with execution outcome.
        """
        started = time.monotonic()
        try:
            # Auto-confirm read-only diagnostic commands so they bypass the
            # exec-service confirmation gate.  REPAIR/mutating commands are
            # never auto-confirmed.
            is_readonly = compiled.spec.command_type.value == "query"
            resp = await asyncio.to_thread(
                requests.post,
                f"{self._exec_url}/api/v1/exec/execute",
                json={
                    "session_id": session_id or "runtime",
                    "message_id": message_id or "runtime",
                    "action_id": action_id or "runtime",
                    "command": compiled.shell_command,
                    "purpose": compiled.spec.purpose,
                    "target_kind": compiled.spec.target_kind,
                    "target_identity": compiled.spec.target_identity,
                    "timeout_seconds": compiled.spec.timeout_seconds,
                    "confirmed": is_readonly,
                    "elevated": False,
                },
                timeout=(3, compiled.spec.timeout_seconds + 10),
            )
            duration_ms = int((time.monotonic() - started) * 1000)
            data = resp.json() if resp.ok else {}
            run_data = data.get("run", data) if isinstance(data, dict) else {}
            return ToolResult(
                success=resp.ok and run_data.get("exit_code", 1) == 0,
                status=run_data.get("status", "completed"),
                exit_code=run_data.get("exit_code", 0),
                stdout=_as_str(run_data.get("stdout", ""))[:10000],
                stderr=_as_str(run_data.get("stderr", ""))[:2000],
                duration_ms=duration_ms,
                channel="remote",
            )
        except Exception as e:
            duration_ms = int((time.monotonic() - started) * 1000)
            return ToolResult(
                success=False,
                status="failed",
                exit_code=1,
                error=_as_str(e),
                duration_ms=duration_ms,
                channel="remote",
            )


__all__ = ["ToolAdapter", "ToolResult"]
