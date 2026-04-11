"""
Exec-service command run client for agent runtime.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Dict, Generator, Iterable, Optional

import requests


class ExecServiceClientError(RuntimeError):
    """Raised when exec-service returns an invalid or failed response."""


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _build_base_url() -> str:
    raw = _as_str(os.getenv("EXEC_SERVICE_BASE_URL"), "http://exec-service:8095")
    return raw.rstrip("/")


def _json_response_or_empty(response: requests.Response) -> Dict[str, Any]:
    content_type = _as_str(response.headers.get("content-type")).lower()
    if "application/json" not in content_type:
        return {}
    try:
        payload = response.json()
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _request_json(
    *,
    method: str,
    path: str,
    payload: Optional[Dict[str, Any]] = None,
    timeout_seconds: int = 10,
) -> Dict[str, Any]:
    base_url = _build_base_url()
    endpoint = f"{base_url}{path}"

    def _do_request() -> Dict[str, Any]:
        try:
            response = requests.request(
                method=method.upper(),
                url=endpoint,
                json=payload if isinstance(payload, dict) else None,
                timeout=(3, max(3, int(timeout_seconds))),
            )
        except Exception as exc:
            raise ExecServiceClientError(f"exec-service unavailable: {exc}") from exc

        body = _json_response_or_empty(response)
        if int(response.status_code) >= 400:
            detail = _as_str(body.get("detail")) or _as_str(body.get("message")) or response.text.strip()
            raise ExecServiceClientError(
                f"exec-service request failed status={response.status_code} path={path}: {detail or 'unknown error'}"
            )
        return body

    return _do_request()


async def _run_blocking(func, *args, **kwargs):
    if os.environ.get("PYTEST_CURRENT_TEST") is not None:
        return func(*args, **kwargs)
    return await asyncio.to_thread(func, *args, **kwargs)


async def create_command_run(
    *,
    session_id: str,
    message_id: str,
    action_id: str,
    command: str,
    command_spec: Optional[Dict[str, Any]] = None,
    purpose: str,
    step_id: str = "",
    confirmed: bool = False,
    elevated: bool = False,
    confirmation_ticket: str = "",
    approval_token: str = "",
    client_deadline_ms: int = 0,
    timeout_seconds: int = 20,
    target_kind: str = "",
    target_identity: str = "",
) -> Dict[str, Any]:
    safe_ticket = _as_str(approval_token).strip() or _as_str(confirmation_ticket)
    payload = {
        "session_id": _as_str(session_id),
        "message_id": _as_str(message_id),
        "action_id": _as_str(action_id),
        "step_id": _as_str(step_id),
        "command": _as_str(command),
        "command_spec": command_spec if isinstance(command_spec, dict) else {},
        "purpose": _as_str(purpose),
        "confirmed": bool(confirmed),
        "elevated": bool(elevated),
        "confirmation_ticket": safe_ticket,
        "approval_token": safe_ticket,
        "client_deadline_ms": _as_int(client_deadline_ms, 0),
        "timeout_seconds": _as_int(timeout_seconds, 20),
        "target_kind": _as_str(target_kind),
        "target_identity": _as_str(target_identity),
    }
    return await _run_blocking(
        _request_json,
        method="POST",
        path="/api/v1/exec/runs",
        payload=payload,
        timeout_seconds=timeout_seconds,
    )


async def execute_command(
    *,
    session_id: str,
    message_id: str,
    action_id: str,
    command: str,
    command_spec: Optional[Dict[str, Any]] = None,
    purpose: str,
    step_id: str = "",
    confirmed: bool = False,
    elevated: bool = False,
    confirmation_ticket: str = "",
    approval_token: str = "",
    client_deadline_ms: int = 0,
    timeout_seconds: int = 20,
    target_kind: str = "",
    target_identity: str = "",
) -> Dict[str, Any]:
    safe_ticket = _as_str(approval_token).strip() or _as_str(confirmation_ticket)
    payload = {
        "session_id": _as_str(session_id),
        "message_id": _as_str(message_id),
        "action_id": _as_str(action_id),
        "step_id": _as_str(step_id),
        "command": _as_str(command),
        "command_spec": command_spec if isinstance(command_spec, dict) else {},
        "purpose": _as_str(purpose),
        "confirmed": bool(confirmed),
        "elevated": bool(elevated),
        "confirmation_ticket": safe_ticket,
        "approval_token": safe_ticket,
        "client_deadline_ms": _as_int(client_deadline_ms, 0),
        "timeout_seconds": _as_int(timeout_seconds, 20),
        "target_kind": _as_str(target_kind),
        "target_identity": _as_str(target_identity),
    }
    return await _run_blocking(
        _request_json,
        method="POST",
        path="/api/v1/exec/execute",
        payload=payload,
        timeout_seconds=timeout_seconds,
    )


async def precheck_command(
    *,
    session_id: str,
    message_id: str,
    action_id: str,
    command: str,
    purpose: str = "",
    timeout_seconds: int = 10,
    target_kind: str = "",
    target_identity: str = "",
) -> Dict[str, Any]:
    payload = {
        "session_id": _as_str(session_id),
        "message_id": _as_str(message_id),
        "action_id": _as_str(action_id),
        "command": _as_str(command),
        "purpose": _as_str(purpose),
        "target_kind": _as_str(target_kind),
        "target_identity": _as_str(target_identity),
    }
    return await _run_blocking(
        _request_json,
        method="POST",
        path="/api/v1/exec/precheck",
        payload=payload,
        timeout_seconds=timeout_seconds,
    )


async def get_command_run(run_id: str, timeout_seconds: int = 10) -> Dict[str, Any]:
    return await _run_blocking(
        get_command_run_sync,
        run_id,
        timeout_seconds=timeout_seconds,
    )


def get_command_run_sync(run_id: str, timeout_seconds: int = 10) -> Dict[str, Any]:
    return _request_json(
        method="GET",
        path=f"/api/v1/exec/runs/{_as_str(run_id)}",
        timeout_seconds=timeout_seconds,
    )


async def list_command_run_events(
    run_id: str,
    *,
    after_seq: int = 0,
    limit: int = 200,
    timeout_seconds: int = 10,
) -> Dict[str, Any]:
    return await _run_blocking(
        list_command_run_events_sync,
        run_id,
        after_seq=after_seq,
        limit=limit,
        timeout_seconds=timeout_seconds,
    )


def list_command_run_events_sync(
    run_id: str,
    *,
    after_seq: int = 0,
    limit: int = 200,
    timeout_seconds: int = 10,
) -> Dict[str, Any]:
    path = f"/api/v1/exec/runs/{_as_str(run_id)}/events?after_seq={max(0, int(after_seq or 0))}&limit={max(1, min(int(limit or 200), 5000))}"
    return _request_json(
        method="GET",
        path=path,
        timeout_seconds=timeout_seconds,
    )


async def cancel_command_run(run_id: str, timeout_seconds: int = 10) -> Dict[str, Any]:
    return await _run_blocking(
        _request_json,
        method="POST",
        path=f"/api/v1/exec/runs/{_as_str(run_id)}/cancel",
        payload={},
        timeout_seconds=timeout_seconds,
    )


def _parse_sse_event_block(raw_block: str) -> Optional[Dict[str, Any]]:
    block = _as_str(raw_block).strip()
    if not block:
        return None
    event_name = "message"
    data_lines = []
    for line in block.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("event:"):
            event_name = _as_str(stripped.split(":", 1)[1]).strip() or "message"
            continue
        if stripped.startswith("data:"):
            data_lines.append(stripped.split(":", 1)[1].lstrip())
    data_text = "\n".join(data_lines)
    try:
        payload = json.loads(data_text) if data_text else {}
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return {
        "event": event_name,
        "data": payload,
    }


def _iter_sse_blocks(chunks: Iterable[bytes | str]) -> Generator[str, None, None]:
    buffer = ""
    for chunk in chunks:
        text = chunk.decode("utf-8", errors="replace") if isinstance(chunk, bytes) else _as_str(chunk)
        if not text:
            continue
        buffer += text
        while True:
            separator = None
            for marker in ("\r\n\r\n", "\n\n"):
                index = buffer.find(marker)
                if index >= 0:
                    separator = (index, len(marker))
                    break
            if separator is None:
                break
            index, marker_len = separator
            block = buffer[:index]
            buffer = buffer[index + marker_len :]
            if block.strip():
                yield block
    if buffer.strip():
        yield buffer


def iter_command_run_stream(
    run_id: str,
    *,
    after_seq: int = 0,
    timeout_seconds: int = 90,
) -> Generator[Dict[str, Any], None, None]:
    base_url = _build_base_url()
    endpoint = f"{base_url}/api/v1/exec/runs/{_as_str(run_id)}/stream?after_seq={max(0, int(after_seq or 0))}"
    try:
        response = requests.get(
            endpoint,
            timeout=(3, max(10, int(timeout_seconds))),
            stream=True,
            headers={"Accept": "text/event-stream"},
        )
    except Exception as exc:
        raise ExecServiceClientError(f"exec-service stream unavailable: {exc}") from exc

    if int(response.status_code) >= 400:
        body = _json_response_or_empty(response)
        detail = _as_str(body.get("detail")) or _as_str(body.get("message")) or response.text.strip()
        response.close()
        raise ExecServiceClientError(
            f"exec-service stream failed status={response.status_code} run_id={run_id}: {detail or 'unknown error'}"
        )

    try:
        for block in _iter_sse_blocks(response.iter_content(chunk_size=1024)):
            parsed = _parse_sse_event_block(block)
            if parsed is not None:
                yield parsed
    finally:
        response.close()


__all__ = [
    "ExecServiceClientError",
    "cancel_command_run",
    "create_command_run",
    "execute_command",
    "precheck_command",
    "get_command_run",
    "get_command_run_sync",
    "iter_command_run_stream",
    "list_command_run_events",
    "list_command_run_events_sync",
]
