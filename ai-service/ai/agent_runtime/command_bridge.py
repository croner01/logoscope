"""
Bridge exec-service command events into agent runtime events.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from ai.agent_runtime import event_protocol
from ai.agent_runtime.exec_client import (
    ExecServiceClientError,
    get_command_run_sync,
    iter_command_run_stream,
    list_command_run_events_sync,
)


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


def _event_payload(data: Dict[str, Any]) -> Dict[str, Any]:
    """Accept both raw payloads and exec-service event envelopes."""
    if not isinstance(data, dict):
        return {}
    payload = data.get("payload")
    if isinstance(payload, dict):
        return payload
    return data


def _normalize_terminal_status(status: str, *, timed_out: bool, exit_code: int) -> str:
    safe_status = _as_str(status, "completed").strip().lower()
    if timed_out or exit_code in {-9, -15}:
        return "timed_out"
    return safe_status or "completed"


def _is_terminal_exec_status(status: str) -> bool:
    return _as_str(status).strip().lower() in {"completed", "failed", "cancelled", "timed_out"}


def _build_terminal_payload(
    *,
    tool_call_id: str,
    tool_name: str,
    title: str,
    exec_run_id: str,
    data_payload: Dict[str, Any],
    run_payload: Dict[str, Any],
) -> Dict[str, Any]:
    exit_code = _as_int(run_payload.get("exit_code"), 0)
    timed_out = bool(run_payload.get("timed_out")) or exit_code in {-9, -15}
    return {
        "tool_call_id": _as_str(tool_call_id),
        "tool_name": _as_str(tool_name, "command.exec"),
        "title": _as_str(title) or "执行命令",
        "command_run_id": _as_str(data_payload.get("command_run_id") or run_payload.get("run_id") or exec_run_id),
        "status": _normalize_terminal_status(
            _as_str(data_payload.get("status") or run_payload.get("status"), "completed"),
            timed_out=timed_out,
            exit_code=exit_code,
        ),
        "command": _as_str(run_payload.get("command") or data_payload.get("command")),
        "purpose": _as_str(run_payload.get("purpose") or data_payload.get("purpose")),
        "exit_code": exit_code,
        "timed_out": timed_out,
        "output_truncated": bool(run_payload.get("output_truncated")),
        "stdout": _as_str(run_payload.get("stdout")),
        "stderr": _as_str(run_payload.get("stderr")),
        "duration_ms": _as_int(run_payload.get("duration_ms"), 0),
        "command_type": _as_str(run_payload.get("command_type"), "unknown"),
        "risk_level": _as_str(run_payload.get("risk_level"), "high"),
        "command_family": _as_str(run_payload.get("command_family"), "unknown"),
        "approval_policy": _as_str(run_payload.get("approval_policy"), "deny"),
        "executor_type": _as_str(run_payload.get("executor_type"), "sandbox_pod"),
        "executor_profile": _as_str(run_payload.get("executor_profile")),
        "target_kind": _as_str(run_payload.get("target_kind"), "unknown"),
        "target_identity": _as_str(run_payload.get("target_identity"), "unknown"),
        "target_cluster_id": _as_str(run_payload.get("target_cluster_id")),
        "target_namespace": _as_str(run_payload.get("target_namespace")),
        "target_node_name": _as_str(run_payload.get("target_node_name")),
        "resolved_target_context": (
            dict(run_payload.get("resolved_target_context"))
            if isinstance(run_payload.get("resolved_target_context"), dict)
            else {}
        ),
        "effective_executor_type": _as_str(run_payload.get("effective_executor_type")),
        "effective_executor_profile": _as_str(run_payload.get("effective_executor_profile")),
        "dispatch_backend": _as_str(run_payload.get("dispatch_backend"), "template_unavailable"),
        "dispatch_mode": _as_str(run_payload.get("dispatch_mode"), "blocked"),
        "dispatch_reason": _as_str(run_payload.get("dispatch_reason")),
        "backend_unavailable": bool(run_payload.get("backend_unavailable")),
        "backend_retry_count": _as_int(run_payload.get("backend_retry_count"), 0),
        "error_code": _as_str(run_payload.get("error_code")),
        "error_detail": _as_str(run_payload.get("error_detail")),
    }


def build_approval_required_payload(
    *,
    tool_call_id: str,
    action_id: str,
    command: str,
    purpose: str,
    precheck: Dict[str, Any],
) -> Dict[str, Any]:
    safe_precheck = precheck if isinstance(precheck, dict) else {}
    return {
        "tool_call_id": _as_str(tool_call_id),
        "action_id": _as_str(action_id),
        "command": _as_str(command),
        "purpose": _as_str(purpose),
        "status": _as_str(safe_precheck.get("status"), "elevation_required"),
        "title": _as_str(safe_precheck.get("message"), "需要审批后继续执行"),
        "command_type": _as_str(safe_precheck.get("command_type"), "unknown"),
        "risk_level": _as_str(safe_precheck.get("risk_level"), "high"),
        "command_family": _as_str(safe_precheck.get("command_family"), "unknown"),
        "approval_policy": _as_str(safe_precheck.get("approval_policy"), "elevation_required"),
        "executor_type": _as_str(safe_precheck.get("executor_type"), "sandbox_pod"),
        "executor_profile": _as_str(safe_precheck.get("executor_profile")),
        "target_kind": _as_str(safe_precheck.get("target_kind"), "unknown"),
        "target_identity": _as_str(safe_precheck.get("target_identity"), "unknown"),
        "target_cluster_id": _as_str(safe_precheck.get("target_cluster_id")),
        "target_namespace": _as_str(safe_precheck.get("target_namespace")),
        "target_node_name": _as_str(safe_precheck.get("target_node_name")),
        "resolved_target_context": (
            dict(safe_precheck.get("resolved_target_context"))
            if isinstance(safe_precheck.get("resolved_target_context"), dict)
            else {}
        ),
        "effective_executor_type": _as_str(safe_precheck.get("effective_executor_type")),
        "effective_executor_profile": _as_str(safe_precheck.get("effective_executor_profile")),
        "dispatch_backend": _as_str(safe_precheck.get("dispatch_backend"), "template_unavailable"),
        "dispatch_mode": _as_str(safe_precheck.get("dispatch_mode"), "blocked"),
        "dispatch_reason": _as_str(safe_precheck.get("dispatch_reason")),
        "requires_confirmation": bool(safe_precheck.get("requires_confirmation")),
        "requires_elevation": bool(safe_precheck.get("requires_elevation")),
        "confirmation_ticket": _as_str(safe_precheck.get("confirmation_ticket")),
    }


def bridge_exec_run_stream_to_runtime(
    *,
    runtime_service: Any,
    run_id: str,
    exec_run_id: str,
    tool_call_id: str,
    title: str,
    tool_name: str = "command.exec",
) -> Dict[str, Any]:
    """
    Relay exec-service SSE events into canonical runtime events.
    """
    safe_title = _as_str(title) or "执行命令"
    finished_payload: Dict[str, Any] = {}
    terminal_observed = False
    last_seq = 0

    def _handle_event(event_name: str, data: Dict[str, Any], *, source: str) -> None:
        nonlocal finished_payload, terminal_observed
        safe_event_name = _as_str(event_name).strip().lower()
        safe_data = _event_payload(data if isinstance(data, dict) else {})

        if safe_event_name == "command_started":
            runtime_service.append_event(
                run_id,
                event_protocol.TOOL_CALL_STARTED,
                {
                    "tool_call_id": _as_str(tool_call_id),
                    "tool_name": _as_str(tool_name, "command.exec"),
                    "title": safe_title,
                    "command_run_id": _as_str(safe_data.get("command_run_id") or exec_run_id),
                    "command": _as_str(safe_data.get("command")),
                    "purpose": _as_str(safe_data.get("purpose")),
                    "command_type": _as_str(safe_data.get("command_type"), "unknown"),
                    "risk_level": _as_str(safe_data.get("risk_level"), "high"),
                    "command_family": _as_str(safe_data.get("command_family"), "unknown"),
                    "approval_policy": _as_str(safe_data.get("approval_policy"), "deny"),
                    "executor_type": _as_str(safe_data.get("executor_type"), "sandbox_pod"),
                    "executor_profile": _as_str(safe_data.get("executor_profile")),
                    "target_kind": _as_str(safe_data.get("target_kind"), "unknown"),
                    "target_identity": _as_str(safe_data.get("target_identity"), "unknown"),
                    "target_cluster_id": _as_str(safe_data.get("target_cluster_id")),
                    "target_namespace": _as_str(safe_data.get("target_namespace")),
                    "target_node_name": _as_str(safe_data.get("target_node_name")),
                    "resolved_target_context": (
                        dict(safe_data.get("resolved_target_context"))
                        if isinstance(safe_data.get("resolved_target_context"), dict)
                        else {}
                    ),
                    "effective_executor_type": _as_str(safe_data.get("effective_executor_type")),
                    "effective_executor_profile": _as_str(safe_data.get("effective_executor_profile")),
                    "dispatch_backend": _as_str(safe_data.get("dispatch_backend"), "template_unavailable"),
                    "dispatch_mode": _as_str(safe_data.get("dispatch_mode"), "blocked"),
                    "dispatch_reason": _as_str(safe_data.get("dispatch_reason")),
                    "status": _as_str(safe_data.get("status"), "running"),
                },
            )
            return

        if safe_event_name == "command_output_delta":
            runtime_service.append_event(
                run_id,
                event_protocol.TOOL_CALL_OUTPUT_DELTA,
                {
                    "tool_call_id": _as_str(tool_call_id),
                    "tool_name": _as_str(tool_name, "command.exec"),
                    "title": safe_title,
                    "command_run_id": _as_str(safe_data.get("command_run_id") or exec_run_id),
                    "stream": _as_str(safe_data.get("stream"), "stdout"),
                    "text": _as_str(safe_data.get("text")),
                    "output_truncated": bool(safe_data.get("output_truncated")),
                },
            )
            return

        if safe_event_name not in {"command_finished", "command_cancelled"}:
            return

        run_payload = safe_data.get("run") if isinstance(safe_data.get("run"), dict) else {}
        finished_payload = _build_terminal_payload(
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            title=safe_title,
            exec_run_id=exec_run_id,
            data_payload=safe_data,
            run_payload=run_payload,
        )
        if source and source != "stream":
            finished_payload["terminal_reconciled_from"] = _as_str(source)
        runtime_service.append_event(
            run_id,
            event_protocol.TOOL_CALL_FINISHED,
            finished_payload,
        )
        terminal_observed = True

    for item in iter_command_run_stream(exec_run_id, after_seq=0):
        if not isinstance(item, dict):
            continue
        raw_data = item.get("data") if isinstance(item.get("data"), dict) else {}
        last_seq = max(last_seq, _as_int(raw_data.get("seq"), 0))
        _handle_event(
            _as_str(item.get("event")),
            raw_data if isinstance(raw_data, dict) else {},
            source="stream",
        )

    if not terminal_observed:
        try:
            events_payload = list_command_run_events_sync(
                exec_run_id,
                after_seq=last_seq,
                limit=5000,
                timeout_seconds=10,
            )
            for event in (events_payload.get("events") if isinstance(events_payload, dict) else []) or []:
                if not isinstance(event, dict):
                    continue
                last_seq = max(last_seq, _as_int(event.get("seq"), 0))
                _handle_event(
                    _as_str(event.get("event_type")),
                    event,
                    source="events_backfill",
                )
                if terminal_observed:
                    break
        except ExecServiceClientError:
            pass
        except Exception:
            pass

    if not terminal_observed:
        try:
            snapshot = get_command_run_sync(exec_run_id, timeout_seconds=8)
            run_payload = snapshot.get("run") if isinstance(snapshot, dict) and isinstance(snapshot.get("run"), dict) else {}
            if run_payload and _is_terminal_exec_status(_as_str(run_payload.get("status"))):
                finished_payload = _build_terminal_payload(
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    title=safe_title,
                    exec_run_id=exec_run_id,
                    data_payload={
                        "command_run_id": _as_str(run_payload.get("run_id") or exec_run_id),
                        "status": _as_str(run_payload.get("status")),
                        "command": _as_str(run_payload.get("command")),
                        "purpose": _as_str(run_payload.get("purpose")),
                    },
                    run_payload=run_payload,
                )
                finished_payload["terminal_reconciled_from"] = "run_snapshot"
                runtime_service.append_event(
                    run_id,
                    event_protocol.TOOL_CALL_FINISHED,
                    finished_payload,
                )
                terminal_observed = True
        except ExecServiceClientError:
            pass
        except Exception:
            pass

    return finished_payload


__all__ = [
    "bridge_exec_run_stream_to_runtime",
    "build_approval_required_payload",
]
