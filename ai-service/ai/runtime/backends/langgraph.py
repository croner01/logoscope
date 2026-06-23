"""LangGraph 后端 — 包装 run_diagnosis() 实现 DiagnosisBackend。"""

from __future__ import annotations

import logging
from typing import Any

from ai.runtime.backend import DiagnosisBackend, BackendRequest, BackendResult
from ai.runtime.engine import run_diagnosis
from ai.runtime.state import RuntimeState, Action, Observation

logger = logging.getLogger(__name__)


class LangGraphBackend(DiagnosisBackend):
    """LangGraph 诊断后端。

    直接包装 ai.runtime.engine.run_diagnosis()。
    从 DiagnosisContext 注入历史、LTM、reflection 到 RuntimeState。
    """

    name = "langgraph"

    async def run(self, request: BackendRequest) -> BackendResult:
        ctx = request.context

        # 1. 从 DiagnosisContext 构建 RuntimeState
        state = RuntimeState(
            run_id=ctx.session_id,
            question=ctx.question,
            analysis_context=ctx.analysis_context,
            history=ctx.history,
            long_term_memory=ctx.long_term_memory,
            react_memory=ctx.react_memory,
            runtime_thread_memory=ctx.runtime_thread_memory,
            subgoals=ctx.subgoals,
            reflection=ctx.reflection,
            planner_prompt=ctx.planner_prompt,
            followup_actions=[
                Action.from_dict(a) for a in ctx.followup_actions
            ],
            executed_commands_set=ctx.executed_commands_set,
            prior_action_observations=ctx.prior_action_observations,
            evidence_gap_queue_for_execution=list(ctx.evidence_gap_queue_for_execution),
            answer_summary_seed=ctx.answer_summary_seed,
            llm_enabled=ctx.llm_enabled,
            llm_requested=ctx.llm_requested,
            token_budget=ctx.token_budget,
            token_estimation=ctx.token_estimation,
            followup_engine=ctx.followup_engine,
            timeout_profile=ctx.timeout_profile,
            deadline_ts=ctx.deadline_ts,
            show_thought=ctx.show_thought,
            event_callback=ctx.event_callback,
        )

        # 2. 构建 llm_call — 适配现有 run_diagnosis 的签名
        async def _llm_call(system_prompt: str, task_prompt: str, tool_schema: Any) -> Any:
            """内部 LLM 调用适配器 — 后续 Phase 2 改为流式。"""
            # 此函数从 DiagnosisContext 获取 LLM 配置
            # 暂时返回空计划（引擎内部处理降级）
            return None

        # 3. 调用 run_diagnosis
        result = await run_diagnosis(
            state=state,
            tools=request.tools,
            memory=request.memory,
            event_emitter=request.event_emitter,
            llm_call=_llm_call,
            logger=logger,
        )

        # 4. 转换为 BackendResult
        return BackendResult(
            actions=[a.to_dict() if hasattr(a, "to_dict") else {} for a in result.actions],
            action_observations=[o.to_dict() if hasattr(o, "to_dict") else {} for o in result.observations],
            iterations=result.iterations,
            summary=result.summary,
        )


# 注册到全局注册表
from ai.runtime.backend import register_backend
register_backend("langgraph", LangGraphBackend)
