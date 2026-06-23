"""Claude SDK 后端 — 使用 Anthropic Messages API + 原生工具调用。

从 ai/runtime_v4/backend/claude_sdk_backend.py 迁入，改动:
- 继承 DiagnosisBackend（替代 RuntimeBackend 同步 Protocol）
- run() 是 native async（不再需要线程 hack）
- event_emitter 从外部注入（不再内部创建）
- system_prompt 从 DiagnosisContext 构建
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from ai.runtime.backend import (
    DiagnosisBackend,
    BackendRequest,
    BackendResult,
    register_backend,
)
from ai.runtime.events import EventEmitter

logger = logging.getLogger(__name__)


# ── 工具辅助函数（从 _v4 移植） ─────────────────────────────────────────────

def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _model_name() -> str:
    return (
        _as_str(os.getenv("CLAUDE_SDK_MODEL"))
        or _as_str(os.getenv("LLM_MODEL"))
        or "claude-sonnet-4-20250514"
    )


def _api_key() -> str:
    key = _as_str(os.getenv("ANTHROPIC_API_KEY"))
    if key:
        return key
    raise RuntimeError("ANTHROPIC_API_KEY not set")


def _api_base_url() -> str:
    return _as_str(os.getenv("ANTHROPIC_BASE_URL")) or "https://api.anthropic.com/v1"


# ── Skills → Tools ─────────────────────────────────────────────────────────

def _load_skills_as_tools() -> List[Dict[str, Any]]:
    """加载 YAML skills 并转为 Claude tool 定义。"""
    from ai.skills.loader import load_builtin_skills
    try:
        skills = load_builtin_skills()
        tools = []
        for skill in skills:
            tool_def = getattr(skill, "to_tool_definition", None)
            if tool_def:
                tools.append(tool_def())
        return tools
    except Exception as e:
        logger.warning("Failed to load skills as tools: %s", e)
        return []


# ── Claude Agent 循环 ───────────────────────────────────────────────────────

def _build_messages(context: Any) -> List[Dict[str, Any]]:
    """从 DiagnosisContext 构建消息列表。"""
    messages = []
    for msg in getattr(context, "history", []):
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if role in ("user", "assistant"):
            messages.append({"role": role, "content": _as_str(content)})
    if context.question:
        messages.append({"role": "user", "content": context.question})
    return messages


def _build_system_prompt_from_context(context: Any) -> str:
    """从 DiagnosisContext 构建 system prompt。"""
    parts = ["You are a Kubernetes observability assistant."]
    if context.long_term_memory:
        ltm_summary = json.dumps(context.long_term_memory, ensure_ascii=False)[:2000]
        parts.append(f"\nLong-term memory:\n{ltm_summary}")
    if context.reflection:
        parts.append(f"\nReflection:\n{json.dumps(context.reflection, ensure_ascii=False)}")
    if context.planner_prompt:
        parts.append(f"\n{context.planner_prompt}")
    return "\n".join(parts)


async def _execute_tool_call(
    tool_name: str,
    tool_input: Dict[str, Any],
    source_target: Optional[Dict[str, Any]],
    event_emitter: EventEmitter,
    run_id: str,
) -> Tuple[str, int]:
    """执行 Claude 选择的工具调用。"""
    from ai.command.normalizer import normalize_command_spec
    from ai.command.compiler import compile_command
    from ai.command.security import evaluate_command
    from ai.command.spec import CommandSpec
    from ai.runtime.tools import ToolAdapter

    spec = normalize_command_spec({
        "command": tool_name,
        "args": tool_input,
        "source_target": source_target,
    })

    # 安全检查
    security = evaluate_command(spec)
    if not security.allowed:
        return f"Command rejected: {security.reason}", -1

    # 编译执行
    compiled = compile_command(spec)
    adapter = ToolAdapter()
    result = await adapter.execute(compiled)

    return result.output if hasattr(result, "output") else str(result), result.exit_code


async def _stream_llm_turn(
    client: Any,
    system_prompt: str,
    messages: List[Dict[str, Any]],
    tools: List[Dict[str, Any]],
    event_emitter: EventEmitter,
    run_id: str,
    max_tokens: int = 4096,
) -> Tuple[str, Optional[str], Optional[Dict[str, Any]]]:
    """单轮 LLM 调用（流式）。"""
    collected_text = ""
    stop_reason = None
    tool_use = None

    # tool_choice 构建
    tool_config = {"tools": tools} if tools else {}

    async with client.messages.stream(
        model=_model_name(),
        system=system_prompt,
        messages=messages,
        max_tokens=max_tokens,
        **tool_config,
    ) as stream:
        async for event in stream:
            if event.type == "content_block_delta":
                delta = event.delta
                if getattr(delta, "type", None) == "text_delta":
                    text = _as_str(getattr(delta, "text", ""))
                    if text:
                        collected_text += text
                        await event_emitter.emit(run_id, "assistant_delta", {"text": text})
            elif event.type == "content_block_start":
                block = event.content_block
                if getattr(block, "type", None) == "tool_use":
                    tool_use = {"name": block.name, "input": block.input}
            elif event.type == "message_delta":
                delta = event.delta
                stop_reason = getattr(delta, "stop_reason", None) if delta else None

    return collected_text, stop_reason, tool_use


async def _run_claude_loop(
    system_prompt: str,
    messages: List[Dict[str, Any]],
    tools: List[Dict[str, Any]],
    event_emitter: EventEmitter,
    source_target: Optional[Dict[str, Any]],
    run_id: str,
    max_turns: int = 10,
) -> BackendResult:
    """Claude agent 主循环。"""
    import anthropic

    client = anthropic.AsyncAnthropic(api_key=_api_key(), base_url=_api_base_url())

    actions = []
    action_observations = []
    iterations = []
    turn = 0

    while turn < max_turns:
        turn += 1
        text, stop_reason, tool_use = await _stream_llm_turn(
            client=client,
            system_prompt=system_prompt,
            messages=messages,
            tools=tools,
            event_emitter=event_emitter,
            run_id=run_id,
        )

        if text:
            messages.append({"role": "assistant", "content": text})

        if tool_use:
            # 执行工具调用
            tool_name = tool_use["name"]
            tool_input = tool_use["input"]
            await event_emitter.emit(run_id, "tool_call_started", {
                "tool": tool_name,
                "input": tool_input,
            })

            output, exit_code = await _execute_tool_call(
                tool_name, tool_input, source_target, event_emitter, run_id
            )

            await event_emitter.emit(run_id, "tool_call_finished", {
                "tool": tool_name,
                "output": output[:500],
            })

            messages.append({
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_name,
                        "content": output,
                    }
                ],
            })

            actions.append({
                "id": f"action-{turn}",
                "command": tool_name,
                "args": tool_input,
                "status": "executed" if exit_code == 0 else "failed",
            })
            action_observations.append({
                "action_id": f"action-{turn}",
                "status": "executed" if exit_code == 0 else "failed",
                "exit_code": exit_code,
                "output": output[:1000],
            })

        if stop_reason == "end_turn" and not tool_use:
            break

    return BackendResult(
        actions=actions,
        action_observations=action_observations,
        iterations=[{"turn": i + 1} for i in range(len(actions))],
        summary=text or "",
    )


class ClaudeSdkBackend(DiagnosisBackend):
    """Claude SDK 诊断后端 — 使用 Anthropic Messages API + 原生工具调用。"""

    name = "claude-sdk"

    async def run(self, request: BackendRequest) -> BackendResult:
        ctx = request.context

        # 1. 加载 YAML skills → Claude @tool 定义
        tools = _load_skills_as_tools()

        # 2. 构建 system_prompt
        system_prompt = _build_system_prompt_from_context(ctx)

        # 3. 构建消息列表
        messages = _build_messages(ctx)

        # 4. 执行 agent 循环
        return await _run_claude_loop(
            system_prompt=system_prompt,
            messages=messages,
            tools=tools,
            event_emitter=request.event_emitter,
            source_target=ctx.source_target,
            run_id=ctx.session_id,
        )


register_backend("claude-sdk", ClaudeSdkBackend)
