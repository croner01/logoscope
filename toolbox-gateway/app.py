"""Toolbox gateway service for controlled command execution."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from dataclasses import dataclass
from typing import Any, Dict

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response


app = FastAPI(title="toolbox-gateway", version="1.0.0")
_SHELL_OPERATOR_TOKENS = {"|", "|&", "||", "&&", ";", "&", ">", ">>", "<", "<<", "<<<", "<>", "<&", ">&", "&>", ">|"}


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _load_allowed_heads() -> set[str]:
    configured = _as_str(
        os.getenv("TOOLBOX_GATEWAY_ALLOWED_HEADS", "kubectl,clickhouse-client,clickhouse"),
    )
    return {item.strip().lower() for item in configured.split(",") if item.strip()}


def _safe_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _normalize_heads(values: Any) -> set[str]:
    if isinstance(values, str):
        return {item.strip().lower() for item in values.split(",") if item.strip()}
    if isinstance(values, list):
        return {
            _as_str(item).strip().lower()
            for item in values
            if _as_str(item).strip()
        }
    return set()


def _load_allowed_heads_by_profile() -> Dict[str, set[str]]:
    raw = _as_str(os.getenv("TOOLBOX_GATEWAY_ALLOWED_HEADS_BY_PROFILE_JSON")).strip()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    normalized: Dict[str, set[str]] = {}
    for key, value in payload.items():
        profile = _as_str(key).strip().lower()
        if not profile:
            continue
        heads = _normalize_heads(value)
        if heads:
            normalized[profile] = heads
    return normalized


def _resolve_allowed_heads(executor_profile: str) -> set[str]:
    profile_map = _load_allowed_heads_by_profile()
    profile = _as_str(executor_profile).strip().lower()
    if profile and profile in profile_map:
        return profile_map[profile]
    return _load_allowed_heads()


def _extract_primary_head(command: str) -> str:
    safe_command = _as_str(command).strip()
    if not safe_command:
        return ""
    try:
        parts = shlex.split(safe_command, posix=True)
    except Exception:
        # Best effort fallback for malformed command input.
        return safe_command.split(maxsplit=1)[0].strip().lower()
    if not parts:
        return ""
    return _as_str(parts[0]).strip().lower()


def _clip_output(text: str, *, limit_bytes: int) -> str:
    safe_text = _as_str(text)
    encoded = safe_text.encode("utf-8", errors="ignore")
    if len(encoded) <= max(1, int(limit_bytes)):
        return safe_text
    clipped = encoded[: max(1, int(limit_bytes))].decode("utf-8", errors="ignore")
    return f"{clipped}\n\n[truncated by toolbox-gateway]"


def _shell_emergency_enabled() -> bool:
    raw = _as_str(os.getenv("AI_RUNTIME_SHELL_EMERGENCY_ENABLED"), "false").strip().lower()
    return raw in {"1", "true", "yes", "on"}


@dataclass
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool


def _execute_command(command: str, *, timeout_seconds: int, max_output_bytes: int) -> ExecResult:
    safe_timeout = max(1, int(timeout_seconds))
    has_shell_features = False
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars="|&;<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        for token in lexer:
            if _as_str(token).strip() in _SHELL_OPERATOR_TOKENS:
                has_shell_features = True
                break
    except Exception:
        has_shell_features = True
    if "$(" in command or "`" in command:
        has_shell_features = True
    if has_shell_features and not _shell_emergency_enabled():
        return ExecResult(
            exit_code=126,
            stdout="",
            stderr="shell syntax is disabled by policy",
            timed_out=False,
        )

    try:
        if has_shell_features and _shell_emergency_enabled():
            completed = subprocess.run(  # noqa: S602
                command,
                shell=True,
                executable="/bin/bash",
                capture_output=True,
                text=True,
                timeout=safe_timeout,
                env=os.environ.copy(),
            )
        else:
            parts = shlex.split(command, posix=True)
            completed = subprocess.run(
                parts,
                shell=False,
                capture_output=True,
                text=True,
                timeout=safe_timeout,
                env=os.environ.copy(),
            )
        return ExecResult(
            exit_code=int(completed.returncode),
            stdout=_clip_output(completed.stdout, limit_bytes=max_output_bytes),
            stderr=_clip_output(completed.stderr, limit_bytes=max_output_bytes),
            timed_out=False,
        )
    except ValueError:
        return ExecResult(
            exit_code=126,
            stdout="",
            stderr="invalid command format",
            timed_out=False,
        )
    except FileNotFoundError:
        return ExecResult(
            exit_code=127,
            stdout="",
            stderr="command not found",
            timed_out=False,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = _clip_output(_as_str(exc.stdout), limit_bytes=max_output_bytes)
        stderr = _clip_output(_as_str(exc.stderr), limit_bytes=max_output_bytes)
        return ExecResult(
            exit_code=124,
            stdout=stdout,
            stderr=stderr or f"command timed out after {safe_timeout}s",
            timed_out=True,
        )


async def _parse_request_payload(request: Request) -> Dict[str, Any]:
    content_type = _as_str(request.headers.get("content-type")).lower()
    if "application/json" in content_type:
        try:
            payload = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"invalid json payload: {exc}") from exc
        return payload if isinstance(payload, dict) else {}
    form = await request.form()
    return {key: value for key, value in form.items()}


def _normalize_resolved_target(payload: Dict[str, Any]) -> Dict[str, str]:
    safe_payload = _safe_dict(payload)
    resolved_target = _safe_dict(safe_payload.get("resolved_target"))
    execution_scope = _safe_dict(resolved_target.get("execution_scope"))
    metadata = _safe_dict(resolved_target.get("metadata"))
    target_kind = _as_str(
        resolved_target.get("target_kind"),
        _as_str(safe_payload.get("target_kind")),
    ).strip()
    target_identity = _as_str(
        resolved_target.get("target_identity"),
        _as_str(safe_payload.get("target_identity")),
    ).strip()
    node_name = _as_str(
        execution_scope.get("node_name"),
        _as_str(metadata.get("node_name"), _as_str(safe_payload.get("target_node_name"))),
    ).strip()
    return {
        "target_kind": target_kind,
        "target_identity": target_identity,
        "node_name": node_name,
    }


def _is_unknown_token(value: Any) -> bool:
    normalized = _as_str(value).strip().lower()
    if not normalized:
        return True
    if normalized in {"unknown", "n/a", "na", "none", "null", "unset"}:
        return True
    return normalized.endswith(":unknown")


@app.get("/health")
async def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.post("/exec")
async def execute_command(request: Request) -> Response:
    payload = await _parse_request_payload(request)
    command = _as_str(payload.get("command")).strip()
    if not command:
        raise HTTPException(status_code=400, detail="command is required")
    executor_profile = _as_str(payload.get("executor_profile")).strip()
    trace_payload = _safe_dict(payload.get("trace"))
    target = _normalize_resolved_target(payload)
    target_kind = _as_str(target.get("target_kind")).strip().lower()
    if target_kind == "host_node" and _is_unknown_token(target.get("node_name")):
        raise HTTPException(status_code=400, detail="host_node target requires resolved node_name")

    allowed_heads = _resolve_allowed_heads(executor_profile)
    head = _extract_primary_head(command)
    if allowed_heads and head and head not in allowed_heads:
        raise HTTPException(status_code=403, detail=f"command head not allowed: {head}")

    timeout_seconds = _as_int(
        payload.get("timeout_seconds"),
        _as_int(os.getenv("TOOLBOX_GATEWAY_DEFAULT_TIMEOUT_SECONDS"), 60),
    )
    max_output_bytes = max(2048, _as_int(os.getenv("TOOLBOX_GATEWAY_MAX_OUTPUT_BYTES"), 262144))

    result = _execute_command(
        command,
        timeout_seconds=max(1, timeout_seconds),
        max_output_bytes=max_output_bytes,
    )
    response_headers = {
        "X-Toolbox-Executor-Profile": executor_profile,
        "X-Toolbox-Target-Kind": _as_str(target.get("target_kind")),
        "X-Toolbox-Target-Identity": _as_str(target.get("target_identity")),
        "X-Toolbox-Target-Node": _as_str(target.get("node_name")),
        "X-Toolbox-Trace-Run-Id": _as_str(trace_payload.get("run_id")),
        "X-Toolbox-Trace-Action-Id": _as_str(trace_payload.get("action_id")),
    }
    response_format = _as_str(payload.get("response_format"), "text").strip().lower()

    if response_format == "json":
        body = {
            "exit_code": int(result.exit_code),
            "timed_out": bool(result.timed_out),
            "stdout": _as_str(result.stdout),
            "stderr": _as_str(result.stderr),
            "executor_profile": executor_profile,
            "target_kind": _as_str(target.get("target_kind")),
            "target_identity": _as_str(target.get("target_identity")),
            "target_node_name": _as_str(target.get("node_name")),
            "trace": {
                "run_id": _as_str(trace_payload.get("run_id")),
                "action_id": _as_str(trace_payload.get("action_id")),
                "decision_id": _as_str(trace_payload.get("decision_id")),
            },
        }
        status_code = 200
        if result.timed_out:
            status_code = 504
        elif result.exit_code != 0:
            status_code = 500
        return JSONResponse(content=body, status_code=status_code, headers=response_headers)

    if result.timed_out:
        body = result.stderr or result.stdout or "command timed out"
        return PlainTextResponse(content=body, status_code=504, headers=response_headers)
    if result.exit_code != 0:
        body = result.stderr or result.stdout or f"command failed (exit={result.exit_code})"
        return PlainTextResponse(content=body, status_code=500, headers=response_headers)
    return PlainTextResponse(content=result.stdout or "", status_code=200, headers=response_headers)
