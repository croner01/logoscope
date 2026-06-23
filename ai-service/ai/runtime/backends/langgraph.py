"""LangGraph 后端 — 包装 run_diagnosis() 实现 DiagnosisBackend。

将 DiagnosisContext 中的历史、LTM、reflection 等数据嵌入
analysis_context 传给 RuntimeState，由 PromptBuilder 消费。
"""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator

from ai.llm_service import get_llm_service
from ai.runtime.backend import DiagnosisBackend, BackendRequest, BackendResult
from ai.runtime.engine import run_diagnosis, _stream_llm_plan
from ai.runtime.prompt import PromptBuilder
from ai.runtime.state import RuntimeState

logger = logging.getLogger(__name__)


class LangGraphBackend(DiagnosisBackend):
    """LangGraph 诊断后端。

    直接包装 ai.runtime.engine.run_diagnosis()。
    将 DiagnosisContext 额外字段嵌入 analysis_context 传递给 RuntimeState。
    """

    name = "langgraph"

    async def run(self, request: BackendRequest) -> BackendResult:
        ctx = request.context

        # 1. 将 DiagnosisContext 额外数据嵌入 analysis_context
        merged_context = dict(ctx.analysis_context)
        merged_context["_diagnosis_ctx"] = {
            "history": ctx.history,
            "long_term_memory": ctx.long_term_memory,
            "react_memory": ctx.react_memory,
            "runtime_thread_memory": ctx.runtime_thread_memory,
            "subgoals": ctx.subgoals,
            "reflection": ctx.reflection,
            "planner_prompt": ctx.planner_prompt,
            "followup_actions": ctx.followup_actions,
            "compacted_summary": ctx.compacted_summary,
            "executed_commands_set": list(ctx.executed_commands_set),
            "prior_action_observations": ctx.prior_action_observations,
            "evidence_gap_queue": ctx.evidence_gap_queue_for_execution,
            "answer_summary_seed": ctx.answer_summary_seed,
            "llm_enabled": ctx.llm_enabled,
            "llm_requested": ctx.llm_requested,
            "token_budget": ctx.token_budget,
            "token_estimation": ctx.token_estimation,
            "followup_engine": ctx.followup_engine,
            "deadline_ts": ctx.deadline_ts,
            "show_thought": ctx.show_thought,
        }

        # 2. 构建 RuntimeState（仅使用实际存在的字段）
        timeout_seconds = int(
            ctx.timeout_profile.get("request_deadline_seconds", 300)
        )
        state = RuntimeState(
            run_id=ctx.session_id,
            question=ctx.question,
            analysis_context=merged_context,
            source_target=ctx.source_target,
            timeout_seconds=timeout_seconds,
            max_iterations=6,
        )

        # 3. 构建流式 LLM generator + streaming plan function
        async def _stream_llm_generator(
            system_prompt: str, task_prompt: str, tool_schema: Any
        ) -> AsyncIterator[str]:
            """Async generator yielding tokens from LLMService.chat_stream()."""
            llm = get_llm_service()
            async for chunk in llm.chat_stream(
                task_prompt,
                system_prompt=system_prompt,
                response_format={"type": "json_object"},
            ):
                yield chunk

        async def _streaming_plan_fn(
            system_prompt: str,
            task_prompt: str,
            tool_schema: dict,
            state: RuntimeState,
            memory: Any,
            llm_call: Any,
        ) -> Any:
            """Streaming plan: emit token events, collect full result, parse actions."""
            return await _stream_llm_plan(
                system_prompt=system_prompt,
                task_prompt=task_prompt,
                tool_schema=tool_schema,
                state=state,
                memory=memory,
                llm_call=_stream_llm_generator,
                event_emitter=request.event_emitter,
            )

        # 4. 调用 run_diagnosis（使用流式 plan function）
        result = await run_diagnosis(
            state=state,
            tools=request.tools,
            prompt_builder=PromptBuilder(),
            memory=request.memory,
            event_emitter=request.event_emitter,
            llm_plan=_streaming_plan_fn,
            logger=logger,
        )

        # 5. 转换为 BackendResult
        return BackendResult(
            actions=[dict(a) if isinstance(a, dict) else a for a in result.actions],
            action_observations=[
                {"action_id": o.action_id, "status": o.status, "exit_code": o.exit_code}
                for o in result.observations
            ],
            iterations=[{"iteration": i} for i in range(1, state.iteration + 1)],
            summary=result.summary,
        )


# 注册到全局注册表
from ai.runtime.backend import register_backend
register_backend("langgraph", LangGraphBackend)
