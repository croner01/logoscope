"""Outer/inner orchestration bridge for runtime v4 API layer."""

from __future__ import annotations

from typing import Any, Dict, List

from ai.runtime_v4.backend import RuntimeBackend, get_runtime_backend, validate_runtime_backend_readiness
from ai.runtime_v4.backend.base import RuntimeBackendRequest
from ai.runtime_v4.store import RuntimeV4ThreadStore
from ai.runtime_v4.temporal.client import TemporalOuterClient


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def _safe_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _safe_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _build_backend_summary(backend_result: Dict[str, Any], *, inner_engine: str) -> Dict[str, Any]:
    safe_payload = _safe_dict(backend_result)
    raw_tool_calls = _safe_list(safe_payload.get("tool_calls"))
    raw_thoughts = [str(item) for item in _safe_list(safe_payload.get("thoughts")) if _as_str(item).strip()]
    tool_calls_preview: List[Dict[str, Any]] = []
    for item in raw_tool_calls[:3]:
        safe_item = _safe_dict(item)
        tool_calls_preview.append(
            {
                "action_id": _as_str(safe_item.get("action_id")),
                "tool_name": _as_str(safe_item.get("tool_name")),
                "skill_name": _as_str(safe_item.get("skill_name")),
                "step_id": _as_str(safe_item.get("step_id")),
                "command": _as_str(safe_item.get("command")),
                "purpose": _as_str(safe_item.get("purpose")),
                "title": _as_str(safe_item.get("title")),
                "command_spec": _safe_dict(safe_item.get("command_spec")),
                "target_kind": _as_str(_safe_dict(safe_item.get("command_spec")).get("args", {}).get("target_kind")),
                "target_identity": _as_str(_safe_dict(safe_item.get("command_spec")).get("args", {}).get("target_identity")),
            }
        )
    return {
        "backend": _as_str(inner_engine),
        "provider": _as_str(safe_payload.get("provider")),
        "mode": _as_str(safe_payload.get("mode")),
        "selected_skills": [str(item) for item in _safe_list(safe_payload.get("selected_skills"))[:5]],
        "thoughts_preview": raw_thoughts[:5],
        "tool_call_count": len(raw_tool_calls),
        "tool_calls_preview": tool_calls_preview,
    }


def _persist_backend_summary(run_id: str, *, inner_engine: str, backend_payload: Dict[str, Any]) -> None:
    if not _as_str(run_id).strip():
        return
    try:
        from api.ai import get_agent_runtime_service

        runtime_service = get_agent_runtime_service()
        run = runtime_service.get_run(run_id)
        if run is None:
            return
        runtime_service._update_run_summary(  # noqa: SLF001
            run,
            inner_backend=_build_backend_summary(backend_payload, inner_engine=inner_engine),
        )
    except Exception:
        return


def _emit_backend_preview_events(run_id: str, *, inner_engine: str, backend_payload: Dict[str, Any]) -> None:
    if not _as_str(run_id).strip():
        return
    try:
        from ai.agent_runtime import event_protocol
        from api.ai import get_agent_runtime_service

        safe_payload = _safe_dict(backend_payload)
        mode = _as_str(safe_payload.get("mode"), "unknown")
        tool_calls = _safe_list(safe_payload.get("tool_calls"))
        thoughts = [str(item) for item in _safe_list(safe_payload.get("thoughts")) if _as_str(item).strip()]
        preview_text = f"{inner_engine} 已规划 {len(tool_calls)} 个动作，当前模式：{mode}。"

        runtime_service = get_agent_runtime_service()
        runtime_service.append_event(
            run_id,
            event_protocol.REASONING_STEP,
            {
                "step_id": "inner-backend-preview",
                "phase": "planning",
                "title": f"{inner_engine} planning preview",
                "status": "info",
                "iteration": 0,
            },
        )
        runtime_service.append_event(
            run_id,
            event_protocol.REASONING_SUMMARY_DELTA,
            {
                "step_id": "inner-backend-preview",
                "phase": "planning",
                "text": preview_text,
            },
        )
        for index, thought in enumerate(thoughts[:5], start=1):
            runtime_service.append_event(
                run_id,
                event_protocol.REASONING_SUMMARY_DELTA,
                {
                    "step_id": f"inner-backend-thought-{index}",
                    "phase": "planning",
                    "text": thought,
                },
            )
    except Exception:
        return


class RuntimeV4OrchestrationBridge:
    """Bridge API v2 contract to the current runtime implementation."""

    def __init__(
        self,
        *,
        temporal_client: TemporalOuterClient,
        thread_store: RuntimeV4ThreadStore,
        runtime_backend: RuntimeBackend | None = None,
    ) -> None:
        self.temporal_client = temporal_client
        self.thread_store = thread_store
        self.runtime_backend = runtime_backend

    async def create_run(
        self,
        *,
        thread_id: str,
        session_id: str,
        question: str,
        analysis_context: Dict[str, Any],
        runtime_options: Dict[str, Any],
    ) -> Dict[str, Any]:
        safe_context = dict(analysis_context or {})
        safe_context["thread_id"] = _as_str(thread_id)
        requested_backend = _as_str(
            runtime_options.get("runtime_backend") or safe_context.get("runtime_backend")
        ).strip()
        if self.runtime_backend is None:
            validate_runtime_backend_readiness(requested_backend)
        start_result = await self.temporal_client.start_run(
            thread_id=thread_id,
            session_id=session_id,
            question=question,
            analysis_context=safe_context,
            runtime_options=runtime_options,
        )
        run_payload = start_result.get("run") if isinstance(start_result, dict) else {}
        run_id = _as_str((run_payload or {}).get("run_id"))
        if run_id:
            self.thread_store.bind_run(thread_id=thread_id, run_id=run_id)

        backend = self.runtime_backend or get_runtime_backend(requested_backend=requested_backend)
        backend_result = backend.run(
            RuntimeBackendRequest(
                run_id=run_id,
                question=_as_str(question),
                analysis_context=safe_context,
                runtime_options=runtime_options,
            )
        )
        _persist_backend_summary(
            run_id,
            inner_engine=_as_str(backend_result.inner_engine),
            backend_payload=backend_result.payload,
        )
        _emit_backend_preview_events(
            run_id,
            inner_engine=_as_str(backend_result.inner_engine),
            backend_payload=backend_result.payload,
        )

        return {
            "workflow_id": _as_str((start_result or {}).get("workflow_id")),
            "outer_engine": _as_str((start_result or {}).get("outer_engine"), self.temporal_client.outer_engine_name()),
            "inner_engine": _as_str(backend_result.inner_engine),
            "run": run_payload,
        }

    async def get_run(self, run_id: str) -> Dict[str, Any]:
        from api.ai import _get_ai_run_impl

        return await _get_ai_run_impl(_as_str(run_id))

    async def get_run_events(
        self,
        run_id: str,
        *,
        after_seq: int,
        limit: int,
        visibility: str = "default",
    ) -> Dict[str, Any]:
        from api.ai import _get_ai_run_events_impl

        return await _get_ai_run_events_impl(
            _as_str(run_id),
            after_seq=after_seq,
            limit=limit,
            visibility=_as_str(visibility, "default"),
        )

    async def stream_run(self, run_id: str, *, after_seq: int, visibility: str = "default"):
        from api.ai import _stream_ai_run_impl

        return await _stream_ai_run_impl(
            _as_str(run_id),
            after_seq=after_seq,
            visibility=_as_str(visibility, "default"),
        )

    async def resolve_approval(
        self,
        *,
        run_id: str,
        approval_id: str,
        decision: str,
        comment: str,
        confirmed: bool,
        elevated: bool,
    ) -> Dict[str, Any]:
        return await self.temporal_client.signal_approval(
            run_id=_as_str(run_id),
            approval_id=_as_str(approval_id),
            decision=_as_str(decision),
            comment=_as_str(comment),
            confirmed=bool(confirmed),
            elevated=bool(elevated),
        )

    async def submit_user_input(self, *, run_id: str, text: str, source: str) -> Dict[str, Any]:
        return await self.temporal_client.signal_user_input(
            run_id=_as_str(run_id),
            text=_as_str(text),
            source=_as_str(source, "user"),
        )

    async def interrupt_run(self, *, run_id: str, reason: str) -> Dict[str, Any]:
        return await self.temporal_client.signal_interrupt(
            run_id=_as_str(run_id),
            reason=_as_str(reason, "user_interrupt_esc"),
        )

    async def cancel_run(self, *, run_id: str, reason: str) -> Dict[str, Any]:
        from api.ai import _cancel_ai_run_impl

        return await _cancel_ai_run_impl(
            _as_str(run_id),
            reason=_as_str(reason, "user_cancelled"),
        )

    async def execute_command(self, *, run_id: str, request_payload: Dict[str, Any]) -> Dict[str, Any]:
        from api.ai import AIRunCommandRequest, execute_ai_run_command

        request = AIRunCommandRequest(**(request_payload if isinstance(request_payload, dict) else {}))
        return await execute_ai_run_command(_as_str(run_id), request)
