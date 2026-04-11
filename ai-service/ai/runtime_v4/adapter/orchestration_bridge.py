"""Outer/inner orchestration bridge for runtime v4 API layer."""

from __future__ import annotations

from typing import Any, Dict

from ai.runtime_v4.langgraph import InnerGraphState, inner_engine_name, run_inner_graph
from ai.runtime_v4.store import RuntimeV4ThreadStore
from ai.runtime_v4.temporal.client import TemporalOuterClient


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


class RuntimeV4OrchestrationBridge:
    """Bridge API v2 contract to the current runtime implementation."""

    def __init__(self, *, temporal_client: TemporalOuterClient, thread_store: RuntimeV4ThreadStore) -> None:
        self.temporal_client = temporal_client
        self.thread_store = thread_store

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

        state = InnerGraphState(
            run_id=run_id,
            question=_as_str(question),
            max_iterations=max(1, min(10, int(runtime_options.get("max_iterations") or 4))),
        )
        run_inner_graph(state)

        return {
            "workflow_id": _as_str((start_result or {}).get("workflow_id")),
            "outer_engine": _as_str((start_result or {}).get("outer_engine"), self.temporal_client.outer_engine_name()),
            "inner_engine": inner_engine_name(),
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
