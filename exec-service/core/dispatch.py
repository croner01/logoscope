"""
Command dispatch helpers for exec-service.
"""

from __future__ import annotations

import inspect
from typing import Any, Awaitable, Callable, Dict, Optional

from core.executor_registry import resolve_executor
from core.runner import stream_command


async def _maybe_await(result: Any) -> None:
    if inspect.isawaitable(result):
        await result


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def _is_dispatch_ready(dispatch: Dict[str, Any]) -> bool:
    safe = dispatch if isinstance(dispatch, dict) else {}
    return (
        _as_str(safe.get("dispatch_backend")).strip().lower() == "template_executor"
        and bool(safe.get("dispatch_ready"))
        and not bool(safe.get("dispatch_degraded"))
        and bool(_as_str(safe.get("resolved_command")).strip())
    )


async def dispatch_command(
    *,
    command: str,
    executor_type: str,
    executor_profile: str,
    target_kind: str,
    target_identity: str,
    resolved_target_context: Optional[Dict[str, Any]] = None,
    timeout_seconds: int = 20,
    on_output: Optional[Callable[[str, str], Awaitable[None] | None]] = None,
    on_process_started: Optional[Callable[[Any], Awaitable[None] | None]] = None,
    on_dispatch_resolved: Optional[Callable[[Dict[str, Any]], Awaitable[None] | None]] = None,
) -> Dict[str, Any]:
    dispatch = resolve_executor(
        command=command,
        executor_type=executor_type,
        executor_profile=executor_profile,
        target_kind=target_kind,
        target_identity=target_identity,
        resolved_target_context=resolved_target_context,
    )
    if on_dispatch_resolved is not None:
        await _maybe_await(on_dispatch_resolved(dispatch))
    if not _is_dispatch_ready(dispatch):
        reason = _as_str(dispatch.get("dispatch_reason"), "controlled executor unavailable")
        if on_output is not None:
            await _maybe_await(on_output("stderr", f"[dispatch-blocked] {reason}\n"))
        return {
            "exit_code": 126,
            "timed_out": False,
            "duration_ms": 0,
            "dispatch": dispatch,
        }
    result = await stream_command(
        command=dispatch.get("resolved_command", command),
        timeout_seconds=timeout_seconds,
        on_output=on_output,
        on_process_started=on_process_started,
    )
    return {
        **result,
        "dispatch": dispatch,
    }


__all__ = ["dispatch_command"]
