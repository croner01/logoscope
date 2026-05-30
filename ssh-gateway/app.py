"""SSH Gateway service for controlled host-level command execution via SSH."""

from __future__ import annotations

import logging
import os
import re
import shlex
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Any, Dict, Optional

import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse

app = FastAPI(title="ssh-gateway", version="1.0.0")

# Optional: host registry management API (ClickHouse-backed)
try:
    from api.hosts import router as hosts_router

    app.include_router(hosts_router)
except Exception:
    pass

logger = logging.getLogger("ssh-gateway")

_DEFAULT_TIMEOUT = int(os.getenv("SSH_GATEWAY_DEFAULT_TIMEOUT_SECONDS", "60"))
_MAX_OUTPUT_BYTES = int(os.getenv("SSH_GATEWAY_MAX_OUTPUT_BYTES", str(256 * 1024)))
_HOSTS_CONFIG = os.getenv("SSH_GATEWAY_HOSTS_CONFIG", "/etc/ssh-hosts/config.yaml")

# Shell operator tokens that could indicate injection attempts (reused from toolbox-gateway)
_SHELL_OPERATOR_TOKENS = {
    "|", "|&", "||", "&&", ";", "&", ">", ">>", "<", "<<",
    "<<<", "<>", "<&", ">&", "&>", ">|",
}


@dataclass
class ExecResult:
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _load_hosts_config() -> Dict[str, Any]:
    """Load node connection configuration from YAML file."""
    if not os.path.exists(_HOSTS_CONFIG):
        logger.warning("Hosts config not found: %s", _HOSTS_CONFIG)
        return {}
    try:
        with open(_HOSTS_CONFIG) as f:
            hosts = yaml.safe_load(f) or {}
        return hosts
    except Exception as e:
        logger.error("Failed to load hosts config: %s", e)
        return {}


def _resolve_node_config(node_name: str) -> Dict[str, Any] | None:
    """Resolve node connection info from hosts config.

    Priority:
    1. Static YAML config file (fast, no network)
    2. ClickHouse host registry (dynamic, runtime-registerable)
    """
    # 1. Check static YAML first
    hosts = _load_hosts_config()
    cfg = hosts.get(node_name)
    if cfg is not None:
        return cfg

    # 2. Fall back to ClickHouse dynamic registry
    try:
        from core.host_registry import ensure_schema, get_host

        ensure_schema()
        ch_host = get_host(node_name)
        if ch_host is not None:
            logger.info("Resolved host '%s' from ClickHouse registry", node_name)
            return {
                "host": ch_host.get("host"),
                "user": ch_host.get("user", "root"),
                "port": int(ch_host.get("port", 22)),
                "key_file": ch_host.get("key_file", "/etc/ssh-keys/default/id_rsa"),
                "private_key_b64": ch_host.get("private_key_b64", ""),
            }
    except Exception as exc:
        logger.warning("ClickHouse host registry unavailable: %s", exc)

    return None


def _clip_output(output: str, max_bytes: int | None = None) -> str:
    """Clip output to maximum bytes."""
    if max_bytes is None:
        max_bytes = _MAX_OUTPUT_BYTES
    if len(output.encode("utf-8")) > max_bytes:
        return (
            output[:max_bytes]
            + f"\n... (truncated at {max_bytes} bytes)"
        )
    return output


def _validate_command_safety(command: str) -> str | None:
    """Validate command for shell injection attempts. Returns error message or None."""
    try:
        shlex.split(command)
    except ValueError as e:
        return f"Command parsing error: {e}"

    # Check shell operators as standalone tokens
    tokens = set(shlex.split(command))
    dangerous = tokens & _SHELL_OPERATOR_TOKENS
    if dangerous:
        return (
            f"Shell operator tokens not allowed: "
            f"{', '.join(sorted(dangerous))}"
        )

    # Check for operators embedded in words (e.g. "hostname;" where shlex
    # treats ";" as part of the word rather than a separate token).
    # Strip quoted sections first, then scan remaining operator characters.
    simplified = re.sub(r"""(['"]).*?\1""", "", command)
    for op in sorted(_SHELL_OPERATOR_TOKENS, key=len, reverse=True):
        if op in simplified:
            return f"Shell operator token '{op}' not allowed"

    # Check backtick command substitution (shlex treats backticks as regular chars)
    if "`" in command:
        return "Backtick command substitution not allowed"

    return None


def _execute_ssh(command: str, node_cfg: Dict[str, Any], timeout: int) -> ExecResult:
    """Execute a command on a remote host via SSH.

    If ``private_key_b64`` is present in node_cfg, the key is decoded
    and written to a temp file (cleaned up after execution).
    """
    private_key_b64 = node_cfg.get("private_key_b64", "")
    key_file = node_cfg.get(
        "key_file",
        f"/etc/ssh-keys/{node_cfg.get('name', 'unknown')}/id_rsa",
    )
    temp_key: tempfile.NamedTemporaryFile | None = None

    # If an inline private key is provided, write it to a temp file
    if private_key_b64:
        import base64

        try:
            key_data = base64.b64decode(private_key_b64).decode("utf-8")
            temp_key = tempfile.NamedTemporaryFile(
                mode="w", prefix="ssh-key-", suffix=".tmp", delete=False
            )
            temp_key.write(key_data)
            temp_key.write("\n")
            temp_key.close()
            os.chmod(temp_key.name, 0o600)
            key_file = temp_key.name
            logger.debug("Using inline private key via temp file for %s", node_cfg.get("host"))
        except Exception as exc:
            logger.error("Failed to decode inline private key: %s", exc)

    user = node_cfg.get("user", "root")
    host = node_cfg["host"]
    port = _as_int(node_cfg.get("port"), 22)

    ssh_cmd = [
        "ssh", "-i", key_file,
        "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=10",
        "-o", "BatchMode=yes",
        "-p", str(port),
        f"{user}@{host}",
        command,
    ]

    logger.info(
        "Executing via SSH: %s@%s (cmd len=%d)", user, host, len(command)
    )

    proc_env = os.environ.copy()
    proc_env.pop("KUBECONFIG", None)

    try:
        completed = subprocess.run(
            ssh_cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=proc_env,
        )
        return ExecResult(
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
    except subprocess.TimeoutExpired:
        logger.warning("SSH command timed out after %ds", timeout)
        return ExecResult(
            exit_code=-1,
            stderr=f"Command timed out after {timeout}s",
            timed_out=True,
        )
    finally:
        if temp_key is not None:
            try:
                os.unlink(temp_key.name)
            except Exception as exc:
                logger.warning("Failed to clean up temp SSH key: %s", exc)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/exec")
async def exec_command(request: Request):
    """Execute a command on a remote host via SSH.

    Accepts both form-encoded and JSON bodies.
    """
    content_type = request.headers.get("content-type", "")

    if "application/json" in content_type:
        body = await request.json()
        command = _as_str(body.get("command"))
        node = _as_str(body.get("node"))
        timeout_seconds = _as_int(
            body.get("timeout_seconds"), _DEFAULT_TIMEOUT
        )
    else:
        form = await request.form()
        command = _as_str(form.get("command"))
        node = _as_str(form.get("node"))
        timeout_seconds = _as_int(
            form.get("timeout_seconds"), _DEFAULT_TIMEOUT
        )

    if not command:
        raise HTTPException(
            status_code=400, detail="Missing required parameter: command"
        )
    if not node:
        raise HTTPException(
            status_code=400, detail="Missing required parameter: node"
        )

    # Clamp timeout
    timeout_seconds = max(1, min(timeout_seconds, 300))

    # Safety validation
    safety_error = _validate_command_safety(command)
    if safety_error:
        raise HTTPException(status_code=403, detail=safety_error)

    # Resolve node config
    node_cfg = _resolve_node_config(node)
    if node_cfg is None:
        available = list(_load_hosts_config().keys())
        detail = (
            f"Unknown node: '{node}'. Available nodes: {available}"
            if available
            else f"Unknown node: '{node}'. No nodes configured."
        )
        raise HTTPException(status_code=400, detail=detail)

    # Execute
    result = _execute_ssh(command, node_cfg, timeout_seconds)

    # Clip output
    stdout = _clip_output(result.stdout)
    stderr = _clip_output(result.stderr)

    if result.timed_out:
        return PlainTextResponse(
            content=stderr or "Command timed out",
            status_code=504,
        )
    if result.exit_code != 0:
        logger.warning(
            "SSH command failed (exit=%d): %s",
            result.exit_code,
            stderr[:200],
        )
        return PlainTextResponse(
            content=stderr
            or f"Command failed with exit code {result.exit_code}",
            status_code=500,
        )

    return PlainTextResponse(content=stdout)
