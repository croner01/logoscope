"""Unified tool execution adapter.

Routes commands to query-service (local) or exec-service (remote).
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
    """Executes compiled commands via local or remote channels."""

    def __init__(
        self,
        query_service_url: str | None = None,
        exec_service_url: str | None = None,
    ):
        self._query_url = (query_service_url or os.getenv("QUERY_SERVICE_BASE_URL", "http://query-service:8092")).rstrip("/")
        self._exec_url = (exec_service_url or os.getenv("EXEC_SERVICE_BASE_URL", "http://exec-service:8095")).rstrip("/")

    async def execute(self, compiled: CompiledCommand) -> ToolResult:
        if compiled.route == "local":
            return await self._execute_local(compiled)
        else:
            return await self._execute_remote(compiled)

    async def _execute_local(self, compiled: CompiledCommand) -> ToolResult:
        """Execute via query-service /api/v1/logs."""
        started = time.monotonic()
        try:
            resp = await asyncio.to_thread(
                requests.get,
                f"{self._query_url}/api/v1/logs",
                params={"search": compiled.shell_command[:200], "limit": 200},
                timeout=(3, 30),
            )
            duration_ms = int((time.monotonic() - started) * 1000)
            data = resp.json() if resp.ok else {}
            events = data.get("events", []) if isinstance(data, dict) else []
            return ToolResult(
                success=resp.ok,
                status="completed" if resp.ok else "failed",
                exit_code=0 if resp.ok else 1,
                stdout=str(events)[:5000],
                duration_ms=duration_ms,
                channel="local",
            )
        except Exception as e:
            duration_ms = int((time.monotonic() - started) * 1000)
            return ToolResult(
                success=False,
                status="failed",
                exit_code=1,
                error=_as_str(e),
                duration_ms=duration_ms,
                channel="local",
            )

    async def _execute_remote(self, compiled: CompiledCommand) -> ToolResult:
        """Execute via exec-service."""
        started = time.monotonic()
        try:
            resp = await asyncio.to_thread(
                requests.post,
                f"{self._exec_url}/api/v1/exec/execute",
                json={
                    "session_id": "runtime",
                    "message_id": "runtime",
                    "action_id": "runtime",
                    "command": compiled.shell_command,
                    "purpose": compiled.spec.purpose,
                    "target_kind": compiled.spec.target_kind,
                    "target_identity": compiled.spec.target_identity,
                    "timeout_seconds": compiled.spec.timeout_seconds,
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
