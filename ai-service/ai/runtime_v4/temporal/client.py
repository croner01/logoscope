"""
Temporal outer-loop client for runtime v4.

Supports two execution paths:
1) `temporal_local` (default): deterministic in-process fallback.
2) `temporal` / `temporal_required`: use Temporal server when SDK/config ready.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
import os
import threading
from typing import Any, Dict, Optional
import uuid

from ai.runtime_v4.temporal import activities
from ai.runtime_v4.temporal.workflows import (
    AIRuntimeRunWorkflow,
    QUERY_SNAPSHOT,
    SIGNAL_APPROVAL,
    SIGNAL_INTERRUPT,
    SIGNAL_USER_INPUT,
    WORKFLOW_DEFINITION_NAME,
    RunWorkflowState,
    temporal_workflow_available,
)


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def _as_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        return default
    return parsed


def _build_workflow_id(run_id: str) -> str:
    safe_run_id = _as_str(run_id).strip()
    if safe_run_id:
        return f"wf-{safe_run_id}"
    return f"wf-{uuid.uuid4().hex[:12]}"


def _outer_engine_mode() -> str:
    raw = _as_str(os.getenv("AI_RUNTIME_V4_OUTER_ENGINE"), "temporal_local").strip().lower()
    if raw in {"temporal_required", "temporal-strict", "temporal_strict"}:
        return "temporal_required"
    if raw in {"temporal", "temporal_v1"}:
        return "temporal"
    return "temporal_local"


def _is_missing_workflow_mapping_error(exc: Exception) -> bool:
    message = _as_str(exc).strip().lower()
    return "workflow_id for run is missing" in message


@dataclass(frozen=True)
class TemporalRuntimeConfig:
    address: str
    namespace: str
    task_queue: str
    connect_timeout_seconds: int
    workflow_execution_timeout_seconds: int
    remote_query_attempts: int
    remote_query_interval_ms: int


def get_temporal_runtime_config() -> TemporalRuntimeConfig:
    return TemporalRuntimeConfig(
        address=_as_str(os.getenv("AI_RUNTIME_V4_TEMPORAL_ADDRESS"), "").strip(),
        namespace=_as_str(os.getenv("AI_RUNTIME_V4_TEMPORAL_NAMESPACE"), "default").strip() or "default",
        task_queue=_as_str(os.getenv("AI_RUNTIME_V4_TEMPORAL_TASK_QUEUE"), "ai-runtime-v4").strip() or "ai-runtime-v4",
        connect_timeout_seconds=max(1, _as_int(os.getenv("AI_RUNTIME_V4_TEMPORAL_CONNECT_TIMEOUT_SECONDS"), 5)),
        workflow_execution_timeout_seconds=max(
            30,
            _as_int(os.getenv("AI_RUNTIME_V4_TEMPORAL_WORKFLOW_TIMEOUT_SECONDS"), 3600),
        ),
        remote_query_attempts=max(1, _as_int(os.getenv("AI_RUNTIME_V4_TEMPORAL_QUERY_ATTEMPTS"), 15)),
        remote_query_interval_ms=max(50, _as_int(os.getenv("AI_RUNTIME_V4_TEMPORAL_QUERY_INTERVAL_MS"), 200)),
    )


def _temporal_sdk_available() -> bool:
    try:
        import temporalio  # noqa: F401

        return True
    except Exception:
        return False


def validate_outer_engine_readiness() -> None:
    mode = _outer_engine_mode()
    if mode != "temporal_required":
        return
    if not _temporal_sdk_available():
        raise RuntimeError("temporal_required but temporalio SDK is unavailable")
    if not temporal_workflow_available():
        raise RuntimeError("temporal_required but workflow definitions are unavailable")
    cfg = get_temporal_runtime_config()
    if not cfg.address:
        raise RuntimeError("temporal_required but AI_RUNTIME_V4_TEMPORAL_ADDRESS is empty")


class TemporalOuterClient:
    """Signal-driven outer-loop client with local fallback runtime."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._workflows: Dict[str, RunWorkflowState] = {}
        self._run_index: Dict[str, str] = {}
        self._temporal_sdk_available = _temporal_sdk_available()
        self._remote_client: Any = None

    def _wants_remote(self) -> bool:
        mode = _outer_engine_mode()
        if mode not in {"temporal", "temporal_required"}:
            return False
        if not self._temporal_sdk_available:
            return False
        return bool(get_temporal_runtime_config().address)

    def outer_engine_name(self) -> str:
        mode = _outer_engine_mode()
        if mode in {"temporal", "temporal_required"} and self._wants_remote():
            return "temporal-v1"
        if mode in {"temporal", "temporal_required"} and not self._wants_remote():
            return "temporal-local-v1"
        return "temporal-local-v1"

    def sdk_available(self) -> bool:
        return self._temporal_sdk_available

    def clear(self) -> None:
        with self._lock:
            self._workflows.clear()
            self._run_index.clear()
        self._remote_client = None

    def get_workflow(self, workflow_id: str) -> Optional[RunWorkflowState]:
        with self._lock:
            return self._workflows.get(_as_str(workflow_id).strip())

    def get_workflow_for_run(self, run_id: str) -> Optional[RunWorkflowState]:
        safe_run_id = _as_str(run_id).strip()
        if not safe_run_id:
            return None
        with self._lock:
            workflow_id = self._run_index.get(safe_run_id)
            if not workflow_id:
                return None
            return self._workflows.get(workflow_id)

    async def _get_remote_client(self) -> Any:
        if self._remote_client is not None:
            return self._remote_client
        if not self._wants_remote():
            mode = _outer_engine_mode()
            if mode == "temporal_required":
                cfg = get_temporal_runtime_config()
                if not self._temporal_sdk_available:
                    raise RuntimeError("temporal_required but temporalio SDK is unavailable")
                if not cfg.address:
                    raise RuntimeError("temporal_required but AI_RUNTIME_V4_TEMPORAL_ADDRESS is empty")
            return None

        cfg = get_temporal_runtime_config()
        try:
            from temporalio.client import Client

            self._remote_client = await asyncio.wait_for(
                Client.connect(cfg.address, namespace=cfg.namespace),
                timeout=cfg.connect_timeout_seconds,
            )
            return self._remote_client
        except Exception as exc:
            if _outer_engine_mode() == "temporal_required":
                raise RuntimeError(f"temporal_required connect failed: {_as_str(exc)}") from exc
            return None

    async def start_run(
        self,
        *,
        thread_id: str,
        session_id: str,
        question: str,
        analysis_context: Dict[str, Any],
        runtime_options: Dict[str, Any],
    ) -> Dict[str, Any]:
        remote_client = await self._get_remote_client()
        if remote_client is not None:
            return await self._start_run_remote(
                remote_client=remote_client,
                thread_id=thread_id,
                session_id=session_id,
                question=question,
                analysis_context=analysis_context,
                runtime_options=runtime_options,
            )
        return await self._start_run_local(
            thread_id=thread_id,
            session_id=session_id,
            question=question,
            analysis_context=analysis_context,
            runtime_options=runtime_options,
        )

    async def _start_run_local(
        self,
        *,
        thread_id: str,
        session_id: str,
        question: str,
        analysis_context: Dict[str, Any],
        runtime_options: Dict[str, Any],
    ) -> Dict[str, Any]:
        if _outer_engine_mode() == "temporal_required" and not self._wants_remote():
            raise RuntimeError("temporal_required but Temporal backend is unavailable")

        start_payload = {
            "session_id": session_id,
            "question": question,
            "analysis_context": analysis_context,
            "runtime_options": runtime_options,
        }
        result = await activities.start_run_activity(start_payload)
        run = result.get("run") if isinstance(result, dict) else {}
        run_id = _as_str((run or {}).get("run_id"))
        workflow_id = _build_workflow_id(run_id)
        state = RunWorkflowState(
            workflow_id=workflow_id,
            thread_id=_as_str(thread_id),
            run_id=run_id,
            status=_as_str((run or {}).get("status"), "running"),
        )
        with self._lock:
            self._workflows[workflow_id] = state
            if run_id:
                self._run_index[run_id] = workflow_id
        return {
            "workflow_id": workflow_id,
            "outer_engine": self.outer_engine_name(),
            "run": run,
        }

    async def _start_run_remote(
        self,
        *,
        remote_client: Any,
        thread_id: str,
        session_id: str,
        question: str,
        analysis_context: Dict[str, Any],
        runtime_options: Dict[str, Any],
    ) -> Dict[str, Any]:
        if not temporal_workflow_available():
            if _outer_engine_mode() == "temporal_required":
                raise RuntimeError("temporal_required but workflow definitions are unavailable")
            return await self._start_run_local(
                thread_id=thread_id,
                session_id=session_id,
                question=question,
                analysis_context=analysis_context,
                runtime_options=runtime_options,
            )

        cfg = get_temporal_runtime_config()
        workflow_id = _build_workflow_id("")
        handle = await remote_client.start_workflow(
            WORKFLOW_DEFINITION_NAME,
            {
                "thread_id": _as_str(thread_id),
                "session_id": _as_str(session_id),
                "question": _as_str(question),
                "analysis_context": analysis_context if isinstance(analysis_context, dict) else {},
                "runtime_options": runtime_options if isinstance(runtime_options, dict) else {},
            },
            id=workflow_id,
            task_queue=cfg.task_queue,
            execution_timeout=timedelta(seconds=cfg.workflow_execution_timeout_seconds),
        )

        snapshot = await self._query_remote_snapshot(handle=handle, require_run_id=True)
        run_id = _as_str(snapshot.get("run_id"))
        if not run_id:
            raise RuntimeError("temporal start workflow did not return run_id")
        run_payload = await self._fetch_run_payload(run_id) if run_id else {}
        if not _as_str((run_payload or {}).get("run_id")).strip():
            run_payload = {
                "run_id": run_id,
                "status": _as_str(snapshot.get("status"), "running"),
                "context_json": {"thread_id": _as_str(thread_id)},
                "summary_json": {},
                "created_at": _as_str(snapshot.get("created_at")),
                "updated_at": _as_str(snapshot.get("updated_at")),
            }
        status = _as_str((run_payload or {}).get("status") or snapshot.get("status"), "running")
        state = RunWorkflowState(
            workflow_id=workflow_id,
            thread_id=_as_str(thread_id),
            run_id=run_id,
            status=status,
        )
        with self._lock:
            self._workflows[workflow_id] = state
            if run_id:
                self._run_index[run_id] = workflow_id

        return {
            "workflow_id": workflow_id,
            "outer_engine": "temporal-v1",
            "run": run_payload,
        }

    async def signal_approval(
        self,
        *,
        run_id: str,
        approval_id: str,
        decision: str,
        comment: str,
        confirmed: bool,
        elevated: bool,
    ) -> Dict[str, Any]:
        payload = {
            "run_id": run_id,
            "approval_id": approval_id,
            "decision": decision,
            "comment": comment,
            "confirmed": confirmed,
            "elevated": elevated,
        }
        remote_signaled = False
        try:
            remote_signaled = await self._signal_remote(
                run_id=run_id,
                signal_name=SIGNAL_APPROVAL,
                payload=payload,
            )
        except RuntimeError as exc:
            if not _is_missing_workflow_mapping_error(exc):
                raise
        if remote_signaled:
            run_payload = await self._fetch_run_payload(run_id)
            self._record_signal(
                run_id=run_id,
                signal_type=SIGNAL_APPROVAL,
                payload=payload,
                run_payload=run_payload,
            )
            return {
                "run": run_payload,
                "approval": {
                    "approval_id": approval_id,
                    "decision": decision,
                    "comment": comment,
                    "confirmed": confirmed,
                    "elevated": elevated,
                },
            }

        result = await activities.resolve_approval_activity(payload)
        self._record_signal(
            run_id=run_id,
            signal_type=SIGNAL_APPROVAL,
            payload=payload,
            run_payload=(result or {}).get("run") if isinstance(result, dict) else {},
        )
        return result

    async def signal_user_input(self, *, run_id: str, text: str, source: str) -> Dict[str, Any]:
        payload = {"run_id": run_id, "text": text, "source": source}
        remote_signaled = False
        try:
            remote_signaled = await self._signal_remote(
                run_id=run_id,
                signal_name=SIGNAL_USER_INPUT,
                payload=payload,
            )
        except RuntimeError as exc:
            if not _is_missing_workflow_mapping_error(exc):
                raise
        if remote_signaled:
            run_payload = await self._fetch_run_payload(run_id)
            self._record_signal(
                run_id=run_id,
                signal_type=SIGNAL_USER_INPUT,
                payload=payload,
                run_payload=run_payload,
            )
            return {
                "run": run_payload,
                "user_input": {
                    "text": text,
                    "source": source,
                },
            }

        result = await activities.submit_user_input_activity(payload)
        self._record_signal(
            run_id=run_id,
            signal_type=SIGNAL_USER_INPUT,
            payload=payload,
            run_payload=(result or {}).get("run") if isinstance(result, dict) else {},
        )
        return result

    async def signal_interrupt(self, *, run_id: str, reason: str) -> Dict[str, Any]:
        payload = {"run_id": run_id, "reason": reason}
        remote_signaled = False
        try:
            remote_signaled = await self._signal_remote(
                run_id=run_id,
                signal_name=SIGNAL_INTERRUPT,
                payload=payload,
            )
        except RuntimeError as exc:
            if not _is_missing_workflow_mapping_error(exc):
                raise
        if remote_signaled:
            run_payload = await self._fetch_run_payload(run_id)
            self._record_signal(
                run_id=run_id,
                signal_type=SIGNAL_INTERRUPT,
                payload=payload,
                run_payload=run_payload,
            )
            return {"run": run_payload}

        result = await activities.interrupt_run_activity(payload)
        self._record_signal(
            run_id=run_id,
            signal_type=SIGNAL_INTERRUPT,
            payload=payload,
            run_payload=(result or {}).get("run") if isinstance(result, dict) else {},
        )
        return result

    async def _signal_remote(self, *, run_id: str, signal_name: str, payload: Dict[str, Any]) -> bool:
        remote_client = await self._get_remote_client()
        if remote_client is None:
            if _outer_engine_mode() == "temporal_required":
                raise RuntimeError("temporal_required but Temporal backend is unavailable")
            return False
        workflow_id = self._resolve_workflow_id_for_run(run_id)
        if not workflow_id:
            if _outer_engine_mode() == "temporal_required":
                raise RuntimeError("temporal_required but workflow_id for run is missing")
            return False
        handle = remote_client.get_workflow_handle(workflow_id)
        await handle.signal(signal_name, payload)
        await self._query_remote_snapshot(handle=handle, require_run_id=False)
        return True

    async def _query_remote_snapshot(self, *, handle: Any, require_run_id: bool) -> Dict[str, Any]:
        cfg = get_temporal_runtime_config()
        snapshot: Dict[str, Any] = {}
        for _ in range(cfg.remote_query_attempts):
            try:
                queried = await handle.query(QUERY_SNAPSHOT)
                snapshot = queried if isinstance(queried, dict) else {}
                if not require_run_id:
                    return snapshot
                if _as_str(snapshot.get("run_id")).strip():
                    return snapshot
            except Exception:
                # workflow may still be initializing/query not registered yet.
                pass
            await asyncio.sleep(cfg.remote_query_interval_ms / 1000.0)
        return snapshot

    async def _fetch_run_payload(self, run_id: str) -> Dict[str, Any]:
        safe_run_id = _as_str(run_id).strip()
        if not safe_run_id:
            return {}
        try:
            from api.ai import get_ai_run

            result = await get_ai_run(safe_run_id)
            run_payload = result.get("run") if isinstance(result, dict) else {}
            return run_payload if isinstance(run_payload, dict) else {}
        except Exception:
            return {}

    def _resolve_workflow_id_for_run(self, run_id: str) -> str:
        safe_run_id = _as_str(run_id).strip()
        if not safe_run_id:
            return ""
        with self._lock:
            workflow_id = self._run_index.get(safe_run_id)
            if workflow_id:
                return workflow_id
            for candidate_id, state in self._workflows.items():
                if _as_str(getattr(state, "run_id", "")) == safe_run_id:
                    self._run_index[safe_run_id] = candidate_id
                    return candidate_id
        return ""

    def _record_signal(
        self,
        *,
        run_id: str,
        signal_type: str,
        payload: Dict[str, object],
        run_payload: Any,
    ) -> None:
        safe_run_id = _as_str(run_id).strip()
        if not safe_run_id:
            return
        with self._lock:
            workflow_id = self._run_index.get(safe_run_id)
            if not workflow_id:
                return
            state = self._workflows.get(workflow_id)
            if state is None:
                return
            state.append_signal(signal_type, payload)
            if isinstance(run_payload, dict):
                state.apply_run_status(_as_str(run_payload.get("status"), state.status))


_temporal_outer_client: Optional[TemporalOuterClient] = None


def get_temporal_outer_client() -> TemporalOuterClient:
    global _temporal_outer_client
    if _temporal_outer_client is None:
        _temporal_outer_client = TemporalOuterClient()
    return _temporal_outer_client
