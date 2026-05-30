"""
Temporal workflow definitions for runtime v4 outer-loop orchestration.

This module must remain import-safe when Temporal SDK is unavailable because
`temporal_local` mode is still supported for local/dev environments.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
from typing import Any, Dict, List, Optional


logger = logging.getLogger(__name__)


TERMINAL_STATUSES = {
    "completed",
    "blocked",
    "failed",
    "cancelled",
    "timeout",
}

WORKFLOW_DEFINITION_NAME = "ai_runtime_v4_run_workflow"
SIGNAL_APPROVAL = "approval"
SIGNAL_USER_INPUT = "user_input"
SIGNAL_INTERRUPT = "interrupt"
QUERY_SNAPSHOT = "snapshot"


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class RunWorkflowState:
    workflow_id: str
    thread_id: str
    run_id: str
    status: str = "running"
    # Keep defaults deterministic for Temporal workflow object construction.
    created_at: str = ""
    updated_at: str = ""
    last_signal_at: str = ""
    signals: List[Dict[str, Any]] = field(default_factory=list)

    def append_signal(
        self,
        signal_type: str,
        payload: Dict[str, Any],
        *,
        observed_at: str = "",
    ) -> None:
        now_iso = observed_at or _utc_now_iso()
        safe_payload = payload if isinstance(payload, dict) else {}
        self.signals.append(
            {
                "signal_type": _as_str(signal_type),
                "payload": safe_payload,
                "observed_at": now_iso,
            }
        )
        self.last_signal_at = now_iso
        self.updated_at = now_iso

    def apply_run_status(self, status: str, *, observed_at: str = "") -> None:
        normalized = _as_str(status, self.status).strip().lower() or self.status
        now_iso = observed_at or _utc_now_iso()
        self.status = normalized
        self.updated_at = now_iso

    def to_dict(self) -> Dict[str, Any]:
        return {
            "workflow_id": self.workflow_id,
            "thread_id": self.thread_id,
            "run_id": self.run_id,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_signal_at": self.last_signal_at,
            "signals": list(self.signals),
        }


try:
    from temporalio import workflow
    from temporalio.common import RetryPolicy

    _TEMPORAL_SDK_AVAILABLE = True
except Exception:
    workflow = None  # type: ignore[assignment]
    RetryPolicy = None  # type: ignore[assignment]
    _TEMPORAL_SDK_AVAILABLE = False


def temporal_workflow_available() -> bool:
    return _TEMPORAL_SDK_AVAILABLE


if _TEMPORAL_SDK_AVAILABLE:
    from datetime import timedelta

    from ai.runtime_v4.temporal import activities

    @workflow.defn(name=WORKFLOW_DEFINITION_NAME)
    class AIRuntimeRunWorkflow:
        """Temporal workflow that executes runtime-v4 outer-loop activities."""

        def __init__(self) -> None:
            self._state = RunWorkflowState(
                workflow_id="",
                thread_id="",
                run_id="",
                status="running",
            )
            self._queue: List[Dict[str, Any]] = []

        @workflow.query(name=QUERY_SNAPSHOT)
        def snapshot(self) -> Dict[str, Any]:
            return self._state.to_dict()

        @workflow.signal(name=SIGNAL_APPROVAL)
        async def signal_approval(self, payload: Dict[str, Any]) -> None:
            self._queue.append(
                {
                    "signal_type": SIGNAL_APPROVAL,
                    "payload": payload if isinstance(payload, dict) else {},
                }
            )

        @workflow.signal(name=SIGNAL_USER_INPUT)
        async def signal_user_input(self, payload: Dict[str, Any]) -> None:
            self._queue.append(
                {
                    "signal_type": SIGNAL_USER_INPUT,
                    "payload": payload if isinstance(payload, dict) else {},
                }
            )

        @workflow.signal(name=SIGNAL_INTERRUPT)
        async def signal_interrupt(self, payload: Dict[str, Any]) -> None:
            self._queue.append(
                {
                    "signal_type": SIGNAL_INTERRUPT,
                    "payload": payload if isinstance(payload, dict) else {},
                }
            )

        async def _run_activity(self, signal_type: str, payload: Dict[str, Any]) -> Dict[str, Any]:
            timeout = timedelta(seconds=90)
            retry = RetryPolicy(initial_interval=timedelta(seconds=1), maximum_attempts=2)
            if signal_type == SIGNAL_APPROVAL:
                return await workflow.execute_activity(
                    activities.resolve_approval_activity,
                    payload,
                    start_to_close_timeout=timeout,
                    retry_policy=retry,
                )
            if signal_type == SIGNAL_USER_INPUT:
                return await workflow.execute_activity(
                    activities.submit_user_input_activity,
                    payload,
                    start_to_close_timeout=timeout,
                    retry_policy=retry,
                )
            if signal_type == SIGNAL_INTERRUPT:
                return await workflow.execute_activity(
                    activities.interrupt_run_activity,
                    payload,
                    start_to_close_timeout=timeout,
                    retry_policy=retry,
                )
            return {}

        @workflow.run
        async def run(self, payload: Dict[str, Any]) -> Dict[str, Any]:
            start_payload = payload if isinstance(payload, dict) else {}
            now_iso = workflow.now().isoformat().replace("+00:00", "Z")

            workflow_id = workflow.info().workflow_id
            self._state = RunWorkflowState(
                workflow_id=workflow_id,
                thread_id=_as_str(start_payload.get("thread_id")),
                run_id="",
                status="running",
                created_at=now_iso,
                updated_at=now_iso,
            )

            start_result = await workflow.execute_activity(
                activities.start_run_activity,
                {
                    "session_id": _as_str(start_payload.get("session_id")),
                    "question": _as_str(start_payload.get("question")),
                    "analysis_context": start_payload.get("analysis_context")
                    if isinstance(start_payload.get("analysis_context"), dict)
                    else {},
                    "runtime_options": start_payload.get("runtime_options")
                    if isinstance(start_payload.get("runtime_options"), dict)
                    else {},
                },
                start_to_close_timeout=timedelta(seconds=120),
                retry_policy=RetryPolicy(initial_interval=timedelta(seconds=1), maximum_attempts=2),
            )
            run_payload = start_result.get("run") if isinstance(start_result, dict) else {}
            self._state.run_id = _as_str((run_payload or {}).get("run_id"))
            self._state.apply_run_status(
                _as_str((run_payload or {}).get("status"), "running"),
                observed_at=workflow.now().isoformat().replace("+00:00", "Z"),
            )

            while self._state.status not in TERMINAL_STATUSES:
                await workflow.wait_condition(lambda: bool(self._queue))
                while self._queue:
                    item = self._queue.pop(0)
                    signal_type = _as_str(item.get("signal_type"))
                    signal_payload = item.get("payload")
                    safe_signal_payload = signal_payload if isinstance(signal_payload, dict) else {}
                    if self._state.run_id and not _as_str(safe_signal_payload.get("run_id")):
                        safe_signal_payload["run_id"] = self._state.run_id
                    self._state.append_signal(
                        signal_type,
                        safe_signal_payload,
                        observed_at=workflow.now().isoformat().replace("+00:00", "Z"),
                    )
                    result = await self._run_activity(signal_type, safe_signal_payload)
                    result_run = result.get("run") if isinstance(result, dict) else {}
                    if isinstance(result_run, dict):
                        next_status = _as_str(result_run.get("status"), self._state.status)
                        self._state.apply_run_status(
                            next_status,
                            observed_at=workflow.now().isoformat().replace("+00:00", "Z"),
                        )
                        resolved_run_id = _as_str(result_run.get("run_id"), self._state.run_id)
                        if resolved_run_id:
                            self._state.run_id = resolved_run_id

            return self._state.to_dict()

else:

    class AIRuntimeRunWorkflow:  # pragma: no cover - compatibility shim without Temporal SDK
        """Shim class used when temporalio is not installed."""

        pass
