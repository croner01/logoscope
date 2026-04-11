"""
Command runtime service for exec-service.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from core.audit_store import append_audit
from core.dispatch import dispatch_command
from core.event_store import CommandEventStore
from core.policy import classify_command_with_auto_rewrite
from core.run_store import CommandRunStore


MAX_OUTPUT_CHARS = max(512, int(os.getenv("EXEC_COMMAND_MAX_OUTPUT_CHARS", "12000")))
TERMINAL_STATUSES = {"completed", "failed", "cancelled"}
BACKEND_UNAVAILABLE_RETRY_MAX = max(
    0,
    min(2, int(os.getenv("EXEC_BACKEND_UNAVAILABLE_MAX_RETRIES", "2"))),
)
BACKEND_UNAVAILABLE_RETRY_BACKOFF_MS = max(
    0,
    min(5000, int(os.getenv("EXEC_BACKEND_UNAVAILABLE_RETRY_BACKOFF_MS", "250"))),
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def _safe_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def build_run_id() -> str:
    return f"cmdrun-{uuid.uuid4().hex[:12]}"


def is_terminal_status(value: Any) -> bool:
    return as_str(value).strip().lower() in TERMINAL_STATUSES


def _append_output(existing_text: str, incoming_text: str) -> tuple[str, bool]:
    merged = f"{as_str(existing_text)}{as_str(incoming_text)}"
    if len(merged) <= MAX_OUTPUT_CHARS:
        return merged, False
    clipped = merged[:MAX_OUTPUT_CHARS]
    if not clipped.endswith("\n...<truncated>..."):
        clipped = f"{clipped}\n...<truncated>..."
    return clipped, True


def _contains_retryable_resolution_error(text: str) -> bool:
    lowered = as_str(text).lower()
    if not lowered:
        return False
    markers = (
        "command not found",
        "not found",
        "no such file or directory",
        "unknown command",
        "shell syntax is disabled by policy",
    )
    return any(marker in lowered for marker in markers)


def _contains_backend_unavailable_error(text: str) -> bool:
    lowered = as_str(text).lower()
    if not lowered:
        return False
    markers = (
        "curl: (7)",
        "failed to connect to",
        "could not connect to server",
        "connection refused",
        "connection timed out",
        "operation timed out",
        "no route to host",
        "temporary failure in name resolution",
        "name or service not known",
    )
    return any(marker in lowered for marker in markers)


def _looks_like_wrapped_http_error(text: str) -> bool:
    lowered = as_str(text).strip().lower()
    if not lowered:
        return False
    markers = (
        "curl: (22)",
        "requested url returned error",
        "http/1.1 500",
        "status 500",
    )
    return any(marker in lowered for marker in markers)


def _derive_failed_error_detail(stderr_text: str, stdout_text: str) -> str:
    stderr_trimmed = as_str(stderr_text).strip()
    stdout_trimmed = as_str(stdout_text).strip()

    # toolbox wrapped executions often surface gateway-level HTTP 500 in stderr,
    # while the actionable root cause (e.g. RBAC Forbidden) is returned in stdout.
    if stdout_trimmed and (not stderr_trimmed or _looks_like_wrapped_http_error(stderr_trimmed)):
        return stdout_trimmed[:500]
    if stderr_trimmed:
        return stderr_trimmed[:500]
    if stdout_trimmed:
        return stdout_trimmed[:500]
    return ""


def _is_toolbox_dispatch(dispatch: Dict[str, Any], command: str = "") -> bool:
    safe_dispatch = dispatch if isinstance(dispatch, dict) else {}
    if as_str(safe_dispatch.get("dispatch_backend")).strip().lower() != "template_executor":
        return False
    template_env = as_str(safe_dispatch.get("dispatch_template_env")).upper()
    dispatch_reason = as_str(safe_dispatch.get("dispatch_reason")).lower()
    command_text = as_str(command).lower()
    return (
        "TOOLBOX" in template_env
        or "toolbox" in dispatch_reason
        or "toolbox-gateway" in command_text
    )


def _classify_backend_unavailable_failure(
    *,
    result: Dict[str, Any],
    dispatch: Dict[str, Any],
    command: str,
    stdout_text: str,
    stderr_text: str,
) -> Dict[str, Any]:
    safe_result = result if isinstance(result, dict) else {}
    if bool(safe_result.get("timed_out")):
        return {"backend_unavailable": False}
    exit_code = int(safe_result.get("exit_code") or 0)
    if exit_code == 0:
        return {"backend_unavailable": False}
    if not _is_toolbox_dispatch(dispatch, command):
        return {"backend_unavailable": False}
    merged_text = f"{as_str(stderr_text)}\n{as_str(stdout_text)}"
    if not _contains_backend_unavailable_error(merged_text):
        return {"backend_unavailable": False}
    return {
        "backend_unavailable": True,
        "reason": "toolbox_gateway_unreachable",
        "message": "toolbox gateway unavailable: connection to execution backend failed",
    }


class ExecRuntimeService:
    """Coordinates command run lifecycle and event fan-out."""

    def __init__(self) -> None:
        self.run_store = CommandRunStore()
        self.event_store = CommandEventStore()

    def create_run(
        self,
        *,
        session_id: str,
        message_id: str,
        action_id: str,
        step_id: str = "",
        command: str,
        command_spec: Optional[Dict[str, Any]] = None,
        purpose: str,
        command_type: str,
        risk_level: str,
        command_family: str,
        approval_policy: str,
        executor_type: str,
        executor_profile: str,
        target_kind: str,
        target_identity: str,
        timeout_seconds: int,
        resolved_target_context: Optional[Dict[str, Any]] = None,
        target_metadata_contract: Optional[Dict[str, Any]] = None,
        policy_decision_id: str = "",
        client_deadline_ms: int = 0,
    ) -> Dict[str, Any]:
        now_iso = utc_now_iso()
        run_id = build_run_id()
        record = {
            "run_id": run_id,
            "command_run_id": run_id,
            "session_id": as_str(session_id),
            "message_id": as_str(message_id),
            "action_id": as_str(action_id),
            "step_id": as_str(step_id),
            "command": as_str(command),
            "command_spec": dict(_safe_dict(command_spec)),
            "purpose": as_str(purpose),
            "command_type": as_str(command_type, "unknown"),
            "risk_level": as_str(risk_level, "high"),
            "command_family": as_str(command_family, "unknown"),
            "approval_policy": as_str(approval_policy, "deny"),
            "policy_decision_id": as_str(policy_decision_id),
            "client_deadline_ms": int(client_deadline_ms or 0),
            "executor_type": as_str(executor_type, "local_process"),
            "executor_profile": as_str(executor_profile, "local-default"),
            "target_kind": as_str(target_kind, "runtime_node"),
            "target_identity": as_str(target_identity, "runtime:local"),
            "resolved_target_context": dict(_safe_dict(resolved_target_context)),
            "target_metadata_contract": dict(_safe_dict(target_metadata_contract)),
            "effective_executor_type": None,
            "effective_executor_profile": None,
            "dispatch_backend": "pending",
            "dispatch_mode": "pending",
            "dispatch_reason": "",
            "dispatch_template_env": "",
            "status": "running",
            "exit_code": 0,
            "stdout": "",
            "stderr": "",
            "output_truncated": False,
            "timed_out": False,
            "cancel_requested": False,
            "backend_unavailable": False,
            "backend_retry_count": 0,
            "backend_retry_reason": "",
            "error_code": "",
            "error_detail": "",
            "duration_ms": 0,
            "created_at": now_iso,
            "updated_at": now_iso,
            "started_at": now_iso,
            "ended_at": None,
        }
        self.run_store.save_run(record)
        task = asyncio.create_task(self._execute_run(run_id, timeout_seconds=max(3, min(180, int(timeout_seconds or 20)))))
        self.run_store.register_task(run_id, task)
        task.add_done_callback(lambda _task, rid=run_id: self.run_store.pop_task(rid))
        return dict(record)

    async def _execute_run(self, run_id: str, timeout_seconds: int) -> None:
        run = self.run_store.get_run(run_id)
        if not isinstance(run, dict):
            return
        started = time.time()

        async def _on_process_started(process: asyncio.subprocess.Process) -> None:
            self.run_store.register_process(run_id, process)

        async def _on_dispatch_resolved(dispatch: Dict[str, Any], *, emit_started: bool = True) -> None:
            now_iso = utc_now_iso()
            current_run = self.run_store.get_run(run_id) or {}
            updated = self.run_store.mutate_run(
                run_id,
                lambda current: {
                    **current,
                    "effective_executor_type": as_str(dispatch.get("effective_executor_type"), "local_process"),
                    "effective_executor_profile": as_str(dispatch.get("effective_executor_profile")),
                    "dispatch_backend": as_str(dispatch.get("dispatch_backend"), "template_unavailable"),
                    "dispatch_mode": as_str(dispatch.get("dispatch_mode"), "blocked"),
                    "dispatch_reason": as_str(dispatch.get("dispatch_reason")),
                    "dispatch_template_env": as_str(dispatch.get("dispatch_template_env")),
                    "updated_at": now_iso,
                },
            )
            payload = {
                "command_run_id": run_id,
                "command": as_str(current_run.get("command")),
                "command_spec": dict(_safe_dict(current_run.get("command_spec"))),
                "purpose": as_str(current_run.get("purpose")),
                "command_type": as_str(current_run.get("command_type"), "unknown"),
                "risk_level": as_str(current_run.get("risk_level"), "high"),
                "command_family": as_str(current_run.get("command_family"), "unknown"),
                "approval_policy": as_str(current_run.get("approval_policy"), "deny"),
                "policy_decision_id": as_str(current_run.get("policy_decision_id")),
                "step_id": as_str(current_run.get("step_id")),
                "client_deadline_ms": int(current_run.get("client_deadline_ms") or 0),
                "executor_type": as_str(current_run.get("executor_type"), "local_process"),
                "executor_profile": as_str(current_run.get("executor_profile"), "local-default"),
                    "target_kind": as_str(current_run.get("target_kind"), "runtime_node"),
                    "target_identity": as_str(current_run.get("target_identity"), "runtime:local"),
                    "resolved_target_context": dict(_safe_dict(current_run.get("resolved_target_context"))),
                    "target_metadata_contract": dict(_safe_dict(current_run.get("target_metadata_contract"))),
                    "effective_executor_type": as_str(dispatch.get("effective_executor_type"), "local_process"),
                    "effective_executor_profile": as_str(dispatch.get("effective_executor_profile")),
                    "target_cluster_id": as_str(dispatch.get("target_cluster_id")),
                    "target_namespace": as_str(dispatch.get("target_namespace")),
                    "target_node_name": as_str(dispatch.get("target_node_name")),
                    "dispatch_backend": as_str(dispatch.get("dispatch_backend"), "template_unavailable"),
                    "dispatch_mode": as_str(dispatch.get("dispatch_mode"), "blocked"),
                    "dispatch_reason": as_str(dispatch.get("dispatch_reason")),
                "dispatch_template_env": as_str(dispatch.get("dispatch_template_env")),
                "status": as_str((updated or {}).get("status"), "running"),
            }
            self.event_store.append_event(run_id, "command_dispatch_resolved", payload)
            if emit_started:
                self.event_store.append_event(run_id, "command_started", payload)

        async def _on_output(stream: str, text: str) -> None:
            def _mutate(current: Dict[str, Any]) -> Dict[str, Any]:
                merged_text, truncated = _append_output(as_str(current.get(stream)), text)
                return {
                    **current,
                    stream: merged_text,
                    "output_truncated": bool(current.get("output_truncated")) or truncated,
                    "updated_at": utc_now_iso(),
                }

            updated = self.run_store.mutate_run(
                run_id,
                _mutate,
            )
            self.event_store.append_event(
                run_id,
                "command_output_delta",
                {
                    "command_run_id": run_id,
                    "stream": as_str(stream),
                    "text": as_str(text),
                    "output_truncated": bool((updated or {}).get("output_truncated")),
                },
            )

        try:
            result = await dispatch_command(
                command=as_str(run.get("command")),
                executor_type=as_str(run.get("executor_type"), "local_process"),
                executor_profile=as_str(run.get("executor_profile"), "local-default"),
                target_kind=as_str(run.get("target_kind"), "runtime_node"),
                target_identity=as_str(run.get("target_identity"), "runtime:local"),
                resolved_target_context=_safe_dict(run.get("resolved_target_context")),
                timeout_seconds=timeout_seconds,
                on_output=_on_output,
                on_process_started=_on_process_started,
                on_dispatch_resolved=_on_dispatch_resolved,
            )
        except asyncio.CancelledError:
            self.run_store.pop_process(run_id)
            current = self.run_store.get_run(run_id) or {}
            if not is_terminal_status(current.get("status")):
                now_iso = utc_now_iso()
                updated = self.run_store.mutate_run(
                    run_id,
                    lambda current_run: {
                        **current_run,
                        "status": "cancelled",
                        "updated_at": now_iso,
                        "ended_at": now_iso,
                        "duration_ms": int((time.time() - started) * 1000),
                    },
                )
                self.event_store.append_event(
                    run_id,
                    "command_cancelled",
                    {
                        "command_run_id": run_id,
                        "status": "cancelled",
                        "run": updated or {"run_id": run_id},
                    },
                )
                if isinstance(updated, dict):
                    append_audit(updated)
            raise

        self.run_store.pop_process(run_id)
        duration_ms = int(result.get("duration_ms") or 0)
        primary_dispatch = result.get("dispatch") if isinstance(result.get("dispatch"), dict) else {}
        current_snapshot = self.run_store.get_run(run_id) or {}
        auto_retry_meta = classify_command_with_auto_rewrite(as_str(current_snapshot.get("command")))
        maybe_retry = (
            int(result.get("exit_code") or 0) != 0
            and not bool(result.get("timed_out"))
            and not bool(current_snapshot.get("cancel_requested"))
            and bool(auto_retry_meta.get("rewrite_applied"))
            and bool(auto_retry_meta.get("supported"))
            and not bool(auto_retry_meta.get("requires_write_permission"))
            and _contains_retryable_resolution_error(
                f"{as_str(current_snapshot.get('stderr'))}\n{as_str(current_snapshot.get('stdout'))}"
            )
        )
        if maybe_retry:
            rewritten_command = as_str(auto_retry_meta.get("command")).strip()
            original_command = as_str(current_snapshot.get("command")).strip()
            if rewritten_command and rewritten_command != original_command:
                await _on_output(
                    "stderr",
                    (
                        "[auto-rewrite-retry] "
                        f"command failed, retry with corrected command: {original_command} -> {rewritten_command}\n"
                    ),
                )
                now_iso = utc_now_iso()
                self.run_store.mutate_run(
                    run_id,
                    lambda current: {
                        **current,
                        "original_command": as_str(current.get("original_command") or original_command),
                        "command": rewritten_command,
                        "command_type": as_str(auto_retry_meta.get("command_type"), as_str(current.get("command_type"), "query")),
                        "risk_level": as_str(auto_retry_meta.get("risk_level"), as_str(current.get("risk_level"), "low")),
                        "command_family": as_str(auto_retry_meta.get("command_family"), as_str(current.get("command_family"), "shell")),
                        "approval_policy": as_str(auto_retry_meta.get("approval_policy"), as_str(current.get("approval_policy"), "auto_execute")),
                        "executor_type": as_str(auto_retry_meta.get("executor_type"), as_str(current.get("executor_type"), "local_process")),
                        "executor_profile": as_str(
                            auto_retry_meta.get("executor_profile"),
                            as_str(current.get("executor_profile"), "local-default"),
                        ),
                        "target_kind": as_str(auto_retry_meta.get("target_kind"), as_str(current.get("target_kind"), "runtime_node")),
                        "target_identity": as_str(
                            auto_retry_meta.get("target_identity"),
                            as_str(current.get("target_identity"), "runtime:local"),
                        ),
                        "auto_retry_count": int(current.get("auto_retry_count") or 0) + 1,
                        "auto_retry_reason": "auto_rewrite_after_failed_execution",
                        "backend_unavailable": False,
                        "backend_retry_reason": "",
                        "updated_at": now_iso,
                    },
                )
                retry_result = await dispatch_command(
                    command=rewritten_command,
                    executor_type=as_str(auto_retry_meta.get("executor_type"), as_str(current_snapshot.get("executor_type"), "local_process")),
                    executor_profile=as_str(
                        auto_retry_meta.get("executor_profile"),
                        as_str(current_snapshot.get("executor_profile"), "local-default"),
                    ),
                    target_kind=as_str(auto_retry_meta.get("target_kind"), as_str(current_snapshot.get("target_kind"), "runtime_node")),
                    target_identity=as_str(
                        auto_retry_meta.get("target_identity"),
                        as_str(current_snapshot.get("target_identity"), "runtime:local"),
                    ),
                    resolved_target_context=_safe_dict(current_snapshot.get("resolved_target_context")),
                    timeout_seconds=timeout_seconds,
                    on_output=_on_output,
                    on_process_started=_on_process_started,
                    on_dispatch_resolved=lambda dispatch: _on_dispatch_resolved(dispatch, emit_started=False),
                )
                self.run_store.pop_process(run_id)
                result = retry_result
                duration_ms += int(retry_result.get("duration_ms") or 0)
                primary_dispatch = retry_result.get("dispatch") if isinstance(retry_result.get("dispatch"), dict) else primary_dispatch

        backend_retry_count = 0
        while True:
            run_after_dispatch = self.run_store.get_run(run_id) or {}
            dispatch_candidate = (
                result.get("dispatch")
                if isinstance(result.get("dispatch"), dict)
                else primary_dispatch
            )
            backend_failure = _classify_backend_unavailable_failure(
                result=result,
                dispatch=dispatch_candidate if isinstance(dispatch_candidate, dict) else {},
                command=as_str(run_after_dispatch.get("command")),
                stdout_text=as_str(run_after_dispatch.get("stdout")),
                stderr_text=as_str(run_after_dispatch.get("stderr")),
            )
            if not bool(backend_failure.get("backend_unavailable")):
                break
            if backend_retry_count >= BACKEND_UNAVAILABLE_RETRY_MAX:
                break

            backend_retry_count += 1
            retry_notice = (
                "[backend-unavailable-retry] "
                f"attempt {backend_retry_count}/{BACKEND_UNAVAILABLE_RETRY_MAX}: "
                f"{as_str(backend_failure.get('message'), 'backend unavailable')}\n"
            )
            await _on_output("stderr", retry_notice)
            now_iso = utc_now_iso()
            self.run_store.mutate_run(
                run_id,
                lambda current: {
                    **current,
                    "backend_retry_count": backend_retry_count,
                    "backend_retry_reason": as_str(backend_failure.get("reason"), "backend_unavailable"),
                    "backend_unavailable": False,
                    "updated_at": now_iso,
                },
            )
            if BACKEND_UNAVAILABLE_RETRY_BACKOFF_MS > 0:
                await asyncio.sleep(float(BACKEND_UNAVAILABLE_RETRY_BACKOFF_MS) / 1000.0)

            retry_result = await dispatch_command(
                command=as_str(run_after_dispatch.get("command")),
                executor_type=as_str(run_after_dispatch.get("executor_type"), "local_process"),
                executor_profile=as_str(run_after_dispatch.get("executor_profile"), "local-default"),
                target_kind=as_str(run_after_dispatch.get("target_kind"), "runtime_node"),
                target_identity=as_str(run_after_dispatch.get("target_identity"), "runtime:local"),
                resolved_target_context=_safe_dict(run_after_dispatch.get("resolved_target_context")),
                timeout_seconds=timeout_seconds,
                on_output=_on_output,
                on_process_started=_on_process_started,
                on_dispatch_resolved=lambda dispatch: _on_dispatch_resolved(dispatch, emit_started=False),
            )
            self.run_store.pop_process(run_id)
            result = retry_result
            duration_ms += int(retry_result.get("duration_ms") or 0)
            primary_dispatch = (
                retry_result.get("dispatch")
                if isinstance(retry_result.get("dispatch"), dict)
                else primary_dispatch
            )

        timed_out = bool(result.get("timed_out"))
        cancel_requested = bool((self.run_store.get_run(run_id) or {}).get("cancel_requested"))
        exit_code = int(result.get("exit_code") or 0)
        dispatch = primary_dispatch
        run_before_finalize = self.run_store.get_run(run_id) or {}
        backend_failure = _classify_backend_unavailable_failure(
            result=result,
            dispatch=dispatch if isinstance(dispatch, dict) else {},
            command=as_str(run_before_finalize.get("command")),
            stdout_text=as_str(run_before_finalize.get("stdout")),
            stderr_text=as_str(run_before_finalize.get("stderr")),
        )
        backend_unavailable = bool(backend_failure.get("backend_unavailable"))
        backend_reason = as_str(backend_failure.get("reason"), "backend_unavailable")
        backend_message = as_str(
            backend_failure.get("message"),
            "toolbox gateway unavailable",
        )
        now_iso = utc_now_iso()
        final_status = "completed"
        if cancel_requested:
            final_status = "cancelled"
        elif timed_out or exit_code != 0:
            final_status = "failed"

        updated = self.run_store.mutate_run(
            run_id,
            lambda current: {
                **current,
                "status": final_status,
                "exit_code": exit_code,
                "timed_out": timed_out,
                "effective_executor_type": as_str(
                    dispatch.get("effective_executor_type"),
                    as_str(current.get("effective_executor_type"), "local_process"),
                ),
                "effective_executor_profile": as_str(
                    dispatch.get("effective_executor_profile"),
                    as_str(current.get("effective_executor_profile")),
                ),
                "dispatch_backend": as_str(
                    dispatch.get("dispatch_backend"),
                    as_str(current.get("dispatch_backend"), "template_unavailable"),
                ),
                "dispatch_mode": as_str(
                    dispatch.get("dispatch_mode"),
                    as_str(current.get("dispatch_mode"), "blocked"),
                ),
                "dispatch_reason": as_str(
                    dispatch.get("dispatch_reason"),
                    as_str(current.get("dispatch_reason")),
                ),
                "dispatch_template_env": as_str(
                    dispatch.get("dispatch_template_env"),
                    as_str(current.get("dispatch_template_env")),
                ),
                "target_cluster_id": as_str(
                    dispatch.get("target_cluster_id"),
                    as_str(current.get("target_cluster_id")),
                ),
                "target_namespace": as_str(
                    dispatch.get("target_namespace"),
                    as_str(current.get("target_namespace")),
                ),
                "target_node_name": as_str(
                    dispatch.get("target_node_name"),
                    as_str(current.get("target_node_name")),
                ),
                "backend_unavailable": backend_unavailable,
                "backend_retry_count": max(
                    int(current.get("backend_retry_count") or 0),
                    int(backend_retry_count or 0),
                ),
                "backend_retry_reason": (
                    backend_reason if backend_unavailable else as_str(current.get("backend_retry_reason"))
                ),
                "error_code": (
                    "cancelled"
                    if final_status == "cancelled"
                    else "timed_out"
                    if timed_out
                    else "backend_unavailable"
                    if backend_unavailable
                    else "command_failed"
                    if final_status == "failed"
                    else ""
                ),
                "error_detail": (
                    "cancel requested by user"
                    if final_status == "cancelled"
                    else "command execution timed out"
                    if timed_out
                    else backend_message
                    if backend_unavailable
                    else _derive_failed_error_detail(
                        as_str(current.get("stderr")),
                        as_str(current.get("stdout")),
                    )
                    if final_status == "failed"
                    else ""
                ),
                "duration_ms": int(duration_ms or int((time.time() - started) * 1000)),
                "updated_at": now_iso,
                "ended_at": now_iso,
            },
        )
        terminal_event = "command_cancelled" if final_status == "cancelled" else "command_finished"
        self.event_store.append_event(
            run_id,
            terminal_event,
            {
                "command_run_id": run_id,
                "status": final_status,
                "run": updated or {"run_id": run_id, "status": final_status},
            },
        )
        if isinstance(updated, dict):
            append_audit(updated)

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        return self.run_store.get_run(run_id)

    def list_runs(self, limit: int = 100) -> List[Dict[str, Any]]:
        return self.run_store.list_runs(limit=limit)

    def list_events(self, run_id: str, after_seq: int = 0, limit: int = 500) -> List[Dict[str, Any]]:
        return self.event_store.list_events(run_id, after_seq=after_seq, limit=limit)

    def subscribe(self, run_id: str) -> asyncio.Queue:
        return self.event_store.subscribe(run_id)

    def unsubscribe(self, run_id: str, queue: asyncio.Queue) -> None:
        self.event_store.unsubscribe(run_id, queue)

    async def cancel_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        run = self.run_store.get_run(run_id)
        if not isinstance(run, dict):
            return None
        if is_terminal_status(run.get("status")):
            return run
        now_iso = utc_now_iso()
        updated = self.run_store.mutate_run(
            run_id,
            lambda current: {
                **current,
                "cancel_requested": True,
                "status": "cancelling",
                "updated_at": now_iso,
            },
        )
        self.event_store.append_event(
            run_id,
            "command_cancel_requested",
            {
                "command_run_id": run_id,
                "status": "cancelling",
            },
        )
        process = self.run_store.get_process(run_id)
        if process is not None and process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
        else:
            task = self.run_store.get_task(run_id)
            if task is not None and not task.done():
                task.cancel()
                await asyncio.sleep(0)
                current = self.run_store.get_run(run_id) or {}
                if not is_terminal_status(current.get("status")):
                    now_iso = utc_now_iso()
                    updated = self.run_store.mutate_run(
                        run_id,
                        lambda current_run: {
                            **current_run,
                            "status": "cancelled",
                            "updated_at": now_iso,
                            "ended_at": now_iso,
                        },
                    )
                    self.event_store.append_event(
                        run_id,
                        "command_cancelled",
                        {
                            "command_run_id": run_id,
                            "status": "cancelled",
                            "run": updated or {"run_id": run_id},
                        },
                    )
                    if isinstance(updated, dict):
                        append_audit(updated)
                    return updated
        return updated

    async def wait_for_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        task = self.run_store.get_task(run_id)
        if task is not None:
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        return self.get_run(run_id)


_exec_runtime_service: Optional[ExecRuntimeService] = None


def get_exec_runtime_service() -> ExecRuntimeService:
    global _exec_runtime_service
    if _exec_runtime_service is None:
        _exec_runtime_service = ExecRuntimeService()
    return _exec_runtime_service
