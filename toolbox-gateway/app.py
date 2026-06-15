"""Toolbox gateway service for controlled command execution."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import subprocess
from dataclasses import dataclass
from typing import Any, Dict

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response


app = FastAPI(title="toolbox-gateway", version="1.0.0")
_logger = logging.getLogger(__name__)
_SHELL_OPERATOR_TOKENS = {"|", "|&", "||", "&&", ";", "&", ">", ">>", "<", "<<", "<<<", "<>", "<&", ">&", "&>", ">|"}
# Pipe-only operators — safe to handle programmatically without a shell
_PIPE_OPERATORS = {"|"}
_PIPE_ENABLED_DEFAULT = True  # pipe chain execution enabled by default


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


# ── virsh subcommand validation ──────────────────────────────────────────────
# Allow only read-only virsh subcommands. All write/destructive operations
# are explicitly blocked at the application level (beyond the head allowlist).
#
# Policy is loaded from a JSON file (mounted from ConfigMap toolbox-gateway-config).
# Falls back to built-in defaults if the file is not present.

_VIRSH_POLICY_PATH = os.getenv(
    "TOOLBOX_GATEWAY_VIRSH_POLICY_PATH",
    "/etc/toolbox-gateway/virsh-policy.json",
)

# ── built-in defaults (used when ConfigMap is not mounted) ─────────────────

_VIRSH_READONLY_DEFAULT = frozenset({
    "list", "dominfo", "domstate", "domstats", "domuuid", "domid", "domname",
    "domxml", "domxml-from-native", "domxml-to-native",
    "domblklist", "domblkinfo", "domblkstat", "dominblkerrors", "domblkerror",
    "domiflist", "domifstat", "domifaddr",
    "dommemstat", "vcpucount", "freecell", "freepages", "maxvcpus",
    "dommemstats", "domfsinfo", "domhostname",
    "nodeinfo", "nodesysinfo", "capabilities", "domcapabilities", "sysinfo",
    "version", "hostname", "uri", "connect", "help",
    "net-list", "net-info", "net-name", "net-dumpxml",
    "pool-list", "pool-info", "pool-dumpxml",
    "vol-list", "vol-info", "vol-dumpxml",
    "nodedev-list", "nodedev-info", "nodedev-dumpxml",
    "secret-list", "secret-info", "secret-dumpxml",
    "iface-list", "iface-info", "iface-dumpxml",
    "snapshot-list", "snapshot-info", "snapshot-dumpxml",
    "backup-dumpxml",
    "nwfilter-list", "nwfilter-info", "nwfilter-dumpxml",
    "echo",
})

_VIRSH_BLOCKED_DEFAULT = frozenset({
    "undefine", "destroy", "define", "create", "start", "shutdown",
    "reboot", "reset", "suspend", "resume", "save", "restore", "managedsave",
    "managedsave-remove", "domjobabort",
    "migrate", "migrate-setmaxdowntime", "migrate-compcache",
    "migrate-setspeed", "migrate-getmaxdowntime",
    "attach-device", "detach-device", "attach-disk", "detach-disk",
    "attach-interface", "detach-interface", "update-device", "change-media",
    "setmaxmem", "setmem", "setvcpus", "set-user-password",
    "vcpupin", "emulatorpin", "iothreadpin", "add-iothread", "del-iothread",
    "blkdeviotune", "blkiotune", "memtune", "schedinfo",
    "blockjob", "blockcommit", "blockcopy", "blockpull", "blockresize",
    "snapshot-create", "snapshot-create-as", "snapshot-delete",
    "snapshot-revert", "snapshot-edit",
    "backup-begin", "backup-end",
    "nodedev-create", "nodedev-destroy",
    "net-create", "net-destroy", "net-define", "net-undefine",
    "net-update", "net-edit", "net-autostart",
    "iface-create", "iface-destroy", "iface-define", "iface-undefine",
    "iface-edit", "iface-start", "iface-bridge", "iface-unbridge",
    "pool-create", "pool-destroy", "pool-define", "pool-undefine",
    "pool-start", "pool-stop", "pool-delete", "pool-edit", "pool-build",
    "pool-refresh", "pool-autostart",
    "vol-create", "vol-create-from", "vol-delete", "vol-upload",
    "vol-download", "vol-resize", "vol-wipe", "vol-clone",
    "secret-define", "secret-undefine", "secret-set-value",
    "domrename", "inject-nmi", "send-key", "send-process-signal",
    "qemu-agent-command", "guest-agent-timeout",
    "domtime", "set-time", "dompmsuspend", "dompmwakeup",
    "set-lifecycle-action", "set-domain-state", "domfstrim",
    "qemu-monitor-command", "qemu-monitor-event",
    "nwfilter-define", "nwfilter-undefine", "nwfilter-edit",
    "event", "allocpages",
    "iothreadinfo",
})


def _load_virsh_policy() -> tuple[frozenset[str], frozenset[str]]:
    """Load virsh subcommand policy from JSON file, falling back to built-in defaults.

    The JSON file must contain a dict with optional ``readonly`` and ``blocked``
    keys, each an array of subcommand strings.  Missing keys fall back to
    the built-in defaults so partial overrides work.

    Returns:
        (readonly_set, blocked_set) — each a frozenset of subcommand names.
    """
    readonly = set(_VIRSH_READONLY_DEFAULT)
    blocked = set(_VIRSH_BLOCKED_DEFAULT)
    path = _as_str(_VIRSH_POLICY_PATH).strip()
    if not path:
        return frozenset(readonly), frozenset(blocked)

    try:
        with open(path, "r") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            _logger.warning(
                "virsh policy file %s: root value is not a dict, using defaults",
                path,
            )
            return frozenset(readonly), frozenset(blocked)

        raw_readonly = data.get("readonly")
        if isinstance(raw_readonly, list) and raw_readonly:
            readonly = {_as_str(item).strip().lower() for item in raw_readonly if _as_str(item).strip()}

        raw_blocked = data.get("blocked")
        if isinstance(raw_blocked, list) and raw_blocked:
            blocked = {_as_str(item).strip().lower() for item in raw_blocked if _as_str(item).strip()}

        _logger.info(
            "loaded virsh policy from %s: %d readonly, %d blocked",
            path,
            len(readonly),
            len(blocked),
        )
    except FileNotFoundError:
        _logger.info(
            "virsh policy file not found at %s, using built-in defaults",
            path,
        )
    except Exception as exc:
        _logger.warning(
            "failed to load virsh policy from %s: %s, using built-in defaults",
            path,
            exc,
        )

    return frozenset(readonly), frozenset(blocked)


# Module-level singleton — loaded once at import time.
# Restart the process (or call _load_virsh_policy() again) to pick up
# ConfigMap changes; there is no hot-reload.
_VIRSH_READONLY_SUBCOMMANDS, _VIRSH_BLOCKED_SUBCOMMANDS = _load_virsh_policy()


def _validate_virsh_command(command: str) -> str | None:
    """Validate a virsh command. Returns None if allowed, error string if blocked.

    Only read-only virsh subcommands are permitted. The function parses
    the command line, skipping leading options (e.g. ``virsh -c qemu:///system list``),
    and checks the first non-option argument (the subcommand).
    """
    try:
        parts = shlex.split(_as_str(command), posix=True)
    except Exception:
        return "virsh: unable to parse command"

    if len(parts) < 2:
        return "virsh requires a subcommand (e.g., list, dominfo)"

    # Skip leading options (anything starting with -)
    subcmd: str | None = None
    for idx, part in enumerate(parts[1:], start=1):
        stripped = part.strip()
        if stripped.startswith("-"):
            continue
        subcmd = stripped.lower()
        break

    if subcmd is None:
        return "virsh requires a subcommand (e.g., list, dominfo)"

    if subcmd in _VIRSH_READONLY_SUBCOMMANDS:
        return None  # allowed

    if subcmd in _VIRSH_BLOCKED_SUBCOMMANDS:
        return (
            f"virsh subcommand '{subcmd}' is blocked: "
            f"write/destructive operations are not permitted"
        )

    # Unknown subcommand — safe default is to block
    return (
        f"virsh subcommand '{subcmd}' is not recognized as a read-only "
        f"operation and has been blocked"
    )


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


def _pipe_enabled() -> bool:
    raw = _as_str(os.getenv("TOOLBOX_GATEWAY_PIPE_ENABLED"), str(_PIPE_ENABLED_DEFAULT)).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _detect_shell_operators(command: str) -> tuple[bool, bool]:
    """Scan command for shell operator tokens.

    Returns (has_any_operator, has_only_pipe).
    ``has_only_pipe`` is True when the command contains ONLY ``|`` operators
    (no ``;``, ``&&``, ``>``, etc.).
    """
    has_any = False
    found_non_pipe = False
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars="|&;<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        for token in lexer:
            token_str = _as_str(token).strip()
            if token_str in _SHELL_OPERATOR_TOKENS:
                has_any = True
                if token_str not in _PIPE_OPERATORS:
                    found_non_pipe = True
                    break
    except Exception:
        has_any = True
        found_non_pipe = True

    return has_any, has_any and not found_non_pipe


def _split_pipe_segments(command: str) -> list[str] | None:
    """Split a command string on ``|`` operators.

    Returns a list of command segments (at least 2) when the command
    contains pipe operators, or ``None`` if parsing fails.
    """
    try:
        lexer_with_pipe = shlex.shlex(command, posix=True, punctuation_chars="|")
        lexer_with_pipe.whitespace_split = True
        lexer_with_pipe.commenters = ""
        segments: list[list[str]] = [[]]
        for token in lexer_with_pipe:
            token_str = _as_str(token).strip()
            if token_str == "|":
                segments.append([])
            elif token_str:
                segments[-1].append(token_str)
        result = [" ".join(seg) for seg in segments if seg]
        return result if len(result) >= 2 else None
    except Exception:
        return None


KUBECONFIG_BASE_DIR = "/etc/kubeconfigs"


def _resolve_kubeconfig_path(kubeconfig_name: str) -> str | None:
    """Resolve a kubeconfig name to an absolute file path.

    Returns None when the name is empty or "default" (meaning use the
    pod's own ServiceAccount), or when the named config does not exist.
    """
    safe_name = _as_str(kubeconfig_name).strip()
    if not safe_name or safe_name.lower() == "default":
        return None
    candidate = os.path.join(KUBECONFIG_BASE_DIR, safe_name)
    if os.path.isfile(candidate):
        return candidate
    _toolbox_logger().warning("kubeconfig not found: %s (using default)", candidate)
    return None


def _toolbox_logger() -> logging.Logger:
    return logging.getLogger(__name__)


@dataclass
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool


async def _execute_pipe_chain(
    segments: list[str],
    *,
    timeout_seconds: int,
    max_output_bytes: int,
    kubeconfig_path: str | None = None,
) -> ExecResult:
    """Execute a pipe chain (``cmd1 | cmd2 | ...``) safely without a shell.

    Each segment is spawned via ``create_subprocess_exec`` (no shell).
    ``os.pipe()`` + ``pass_fds`` connects each segment's stdout to the
    next segment's stdin.  Only ``|`` is supported — other shell operators
    (``;``, ``&&``, ``>``, etc.) are rejected earlier in the calling code.

    Returns the last segment's stdout and a composite stderr + exit code.
    """
    proc_env = os.environ.copy()
    if kubeconfig_path:
        proc_env["KUBECONFIG"] = kubeconfig_path

    processes: list[asyncio.subprocess.Process] = []
    prev_read_fd: int | None = None
    pipe_write_fds: list[int] = []

    try:
        for i, segment in enumerate(segments):
            parts = shlex.split(segment, posix=True)
            if not parts:
                continue

            is_last = i == len(segments) - 1

            if is_last:
                # Last segment: capture stdout + stderr
                process = await asyncio.create_subprocess_exec(
                    *parts,
                    stdin=prev_read_fd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=proc_env,
                    pass_fds=(prev_read_fd,) if prev_read_fd is not None else (),
                )
                processes.append(process)
            else:
                # Intermediate segment: create pipe for stdout
                pipe_r, pipe_w = os.pipe()
                pipe_write_fds.append(pipe_w)
                process = await asyncio.create_subprocess_exec(
                    *parts,
                    stdin=prev_read_fd,
                    stdout=pipe_w,
                    stderr=asyncio.subprocess.PIPE,
                    env=proc_env,
                    pass_fds=(prev_read_fd,) if prev_read_fd is not None else (),
                )
                os.close(pipe_w)  # write end owned by child now
                if prev_read_fd is not None:
                    os.close(prev_read_fd)
                prev_read_fd = pipe_r
                processes.append(process)

        if not processes:
            return ExecResult(exit_code=0, stdout="", stderr="", timed_out=False)

        # Wait for the last process (with timeout)
        try:
            last_stdout_bytes, last_stderr_bytes = await asyncio.wait_for(
                processes[-1].communicate(),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            for p in processes:
                try:
                    p.kill()
                except ProcessLookupError:
                    pass
            for p in processes:
                try:
                    await p.wait()
                except ProcessLookupError:
                    pass
            return ExecResult(
                exit_code=124,
                stdout="",
                stderr=f"piped command timed out after {timeout_seconds}s",
                timed_out=True,
            )

        # Collect stderr and exit code from all intermediate processes
        all_stderr_parts: list[str] = []
        overall_exit = 0
        for idx, p in enumerate(processes):
            if p.returncode is None:
                try:
                    await p.wait()
                except ProcessLookupError:
                    pass
            rc = int(p.returncode or 0)
            if rc != 0 and overall_exit == 0:
                overall_exit = rc
            # Reap stderr from intermediate processes
            if idx < len(processes) - 1:
                try:
                    _se = p.stderr
                    if _se:
                        se_bytes = await _se.read()
                        if se_bytes:
                            all_stderr_parts.append(se_bytes.decode("utf-8", errors="ignore"))
                except Exception:
                    pass

        if last_stderr_bytes:
            all_stderr_parts.append(last_stderr_bytes.decode("utf-8", errors="ignore"))

        return ExecResult(
            exit_code=overall_exit,
            stdout=_clip_output(
                last_stdout_bytes.decode("utf-8", errors="ignore") if last_stdout_bytes else "",
                limit_bytes=max_output_bytes,
            ),
            stderr=_clip_output(
                "\n".join(all_stderr_parts) if all_stderr_parts else "",
                limit_bytes=max_output_bytes,
            ),
            timed_out=False,
        )
    except FileNotFoundError:
        return ExecResult(
            exit_code=127,
            stdout="",
            stderr="command not found in pipe chain",
            timed_out=False,
        )
    except Exception:
        return ExecResult(
            exit_code=126,
            stdout="",
            stderr="failed to execute piped command",
            timed_out=False,
        )
    finally:
        # Safety: close any leftover pipe write fds
        for fd in pipe_write_fds:
            try:
                os.close(fd)
            except OSError:
                pass
        if prev_read_fd is not None:
            try:
                os.close(prev_read_fd)
            except OSError:
                pass


async def _execute_command(
    command: str,
    *,
    timeout_seconds: int,
    max_output_bytes: int,
    kubeconfig_path: str | None = None,
) -> ExecResult:
    safe_timeout = max(1, int(timeout_seconds))

    # Detect shell operators and classify
    has_shell_features, has_only_pipe = _detect_shell_operators(command)
    if "$(" in command or "`" in command:
        has_shell_features = True
        has_only_pipe = False

    # ── Pipe-only mode: execute via safe process chain ──────────────
    if has_only_pipe and _pipe_enabled():
        segments = _split_pipe_segments(command)
        if segments and len(segments) >= 2:
            return await _execute_pipe_chain(
                segments,
                timeout_seconds=safe_timeout,
                max_output_bytes=max_output_bytes,
                kubeconfig_path=kubeconfig_path,
            )

    # ── Other shell operators or pipe disabled → block unless emergency ──
    if has_shell_features and not _shell_emergency_enabled():
        return ExecResult(
            exit_code=126,
            stdout="",
            stderr="shell syntax is disabled by policy",
            timed_out=False,
        )

    proc_env = os.environ.copy()
    if kubeconfig_path:
        proc_env["KUBECONFIG"] = kubeconfig_path

    try:
        if has_shell_features and _shell_emergency_enabled():
            process = await asyncio.create_subprocess_shell(
                command,
                executable="/bin/bash",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=proc_env,
            )
        else:
            parts = shlex.split(command, posix=True)
            process = await asyncio.create_subprocess_exec(
                *parts,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=proc_env,
            )
        try:
            _stdout_bytes, _stderr_bytes = await asyncio.wait_for(
                process.communicate(), timeout=safe_timeout
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            return ExecResult(
                exit_code=124,
                stdout="",
                stderr=f"command timed out after {safe_timeout}s",
                timed_out=True,
            )
        return ExecResult(
            exit_code=int(process.returncode or 0),
            stdout=_clip_output(
                _stdout_bytes.decode("utf-8", errors="ignore") if _stdout_bytes else "",
                limit_bytes=max_output_bytes,
            ),
            stderr=_clip_output(
                _stderr_bytes.decode("utf-8", errors="ignore") if _stderr_bytes else "",
                limit_bytes=max_output_bytes,
            ),
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

    kubeconfig_name = _as_str(payload.get("kubeconfig")).strip()
    kubeconfig_path = _resolve_kubeconfig_path(kubeconfig_name)

    allowed_heads = _resolve_allowed_heads(executor_profile)
    head = _extract_primary_head(command)
    if allowed_heads and head and head not in allowed_heads:
        raise HTTPException(status_code=403, detail=f"command head not allowed: {head}")

    # virsh-specific: allow only read-only subcommands
    if head == "virsh":
        virsh_error = _validate_virsh_command(command)
        if virsh_error:
            raise HTTPException(status_code=403, detail=virsh_error)

    timeout_seconds = _as_int(
        payload.get("timeout_seconds"),
        _as_int(os.getenv("TOOLBOX_GATEWAY_DEFAULT_TIMEOUT_SECONDS"), 60),
    )
    max_output_bytes = max(2048, _as_int(os.getenv("TOOLBOX_GATEWAY_MAX_OUTPUT_BYTES"), 262144))

    result = await _execute_command(
        command,
        timeout_seconds=max(1, timeout_seconds),
        max_output_bytes=max_output_bytes,
        kubeconfig_path=kubeconfig_path,
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
