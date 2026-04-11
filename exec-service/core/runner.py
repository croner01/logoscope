"""
Command runner for exec-service.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import shlex
import time
from typing import Any, Awaitable, Callable, Dict, Optional

CHAIN_OPERATORS = {
    "|",
    "|&",
    "||",
    "&&",
    ";",
}
BLOCKED_OPERATORS = {"&", ">", ">>", "<", "<<", "<<<", "<>", "<&", ">&", "&>", ">|"}


def as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def _shell_emergency_enabled() -> bool:
    raw = as_str(os.getenv("AI_RUNTIME_SHELL_EMERGENCY_ENABLED"), "false").strip().lower()
    return raw in {"1", "true", "yes", "on"}


async def _maybe_await(result: Any) -> None:
    if inspect.isawaitable(result):
        await result


async def _pump_output(
    reader: Optional[asyncio.StreamReader],
    stream_name: str,
    on_output: Optional[Callable[[str, str], Awaitable[None] | None]] = None,
) -> None:
    if reader is None or on_output is None:
        return
    while True:
        chunk = await reader.read(512)
        if not chunk:
            break
        text = chunk.decode("utf-8", errors="replace")
        await _maybe_await(on_output(stream_name, text))


async def stream_command(
    command: str,
    timeout_seconds: int = 20,
    on_output: Optional[Callable[[str, str], Awaitable[None] | None]] = None,
    on_process_started: Optional[Callable[[asyncio.subprocess.Process], Awaitable[None] | None]] = None,
) -> Dict[str, Any]:
    safe_timeout = max(3, min(180, int(timeout_seconds or 20)))
    started = time.time()
    try:
        use_shell = False
        has_shell_features = False
        try:
            lexer = shlex.shlex(command, posix=True, punctuation_chars="|&;<>")
            lexer.whitespace_split = True
            lexer.commenters = ""
            for token in lexer:
                normalized = as_str(token).strip()
                if normalized in CHAIN_OPERATORS or normalized in BLOCKED_OPERATORS:
                    has_shell_features = True
                    break
        except Exception:
            has_shell_features = True
        if "$(" in command or "`" in command:
            has_shell_features = True

        if has_shell_features and not _shell_emergency_enabled():
            if on_output is not None:
                await _maybe_await(on_output("stderr", "shell syntax is disabled by policy"))
            return {
                "exit_code": 126,
                "timed_out": False,
                "duration_ms": int((time.time() - started) * 1000),
            }
        use_shell = has_shell_features and _shell_emergency_enabled()

        if use_shell:
            process = await asyncio.create_subprocess_shell(
                command,
                executable="/bin/bash",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        else:
            parts = shlex.split(command)
            process = await asyncio.create_subprocess_exec(
                *parts,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        await _maybe_await(on_process_started(process) if on_process_started is not None else None)
        stdout_task = asyncio.create_task(_pump_output(process.stdout, "stdout", on_output=on_output))
        stderr_task = asyncio.create_task(_pump_output(process.stderr, "stderr", on_output=on_output))
        timed_out = False
        try:
            await asyncio.wait_for(process.wait(), timeout=safe_timeout)
        except asyncio.TimeoutError:
            timed_out = True
            process.kill()
            await process.wait()
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        return {
            "exit_code": int(process.returncode or 0),
            "timed_out": timed_out,
            "duration_ms": int((time.time() - started) * 1000),
        }
    except FileNotFoundError as exc:
        if on_output is not None:
            await _maybe_await(on_output("stderr", f"command not found: {exc}"))
        return {
            "exit_code": 127,
            "timed_out": False,
            "duration_ms": int((time.time() - started) * 1000),
        }
    except Exception as exc:
        if on_output is not None:
            await _maybe_await(on_output("stderr", as_str(exc)))
        return {
            "exit_code": 1,
            "timed_out": False,
            "duration_ms": int((time.time() - started) * 1000),
        }
