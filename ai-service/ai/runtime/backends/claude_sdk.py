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
from ai.runtime.tools import ToolAdapter
from ai.runtime.memory import SessionMemory

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
    """按优先级查找 API key。

    优先级规则：
    - 当使用 DeepSeek（模型名以 ``deepseek`` 开头或 LLM_PROVIDER=deepseek）
      时：DEEPSEEK_API_KEY → ANTHROPIC_API_KEY → CLAUDE_SDK_API_KEY → LLM_API_KEY
    - 其他情况：ANTHROPIC_API_KEY → CLAUDE_SDK_API_KEY → DEEPSEEK_API_KEY → LLM_API_KEY

    Secret 中可能同时存在 ANTHROPIC_API_KEY（旧）和 DEEPSEEK_API_KEY（新），
    如果不区分 provider 优先级，旧 ANTHROPIC_API_KEY 会一直劫持新 DeepSeek key。
    """
    provider = _as_str(os.getenv("LLM_PROVIDER"))
    model = _model_name()
    using_deepseek = model.startswith("deepseek") or provider == "deepseek"

    if using_deepseek:
        key = _as_str(os.getenv("DEEPSEEK_API_KEY"))
        if not key:
            key = _as_str(os.getenv("ANTHROPIC_API_KEY"))
        if not key:
            key = _as_str(os.getenv("CLAUDE_SDK_API_KEY"))
        if not key:
            key = _as_str(os.getenv("LLM_API_KEY"))
    else:
        key = _as_str(os.getenv("ANTHROPIC_API_KEY"))
        if not key:
            key = _as_str(os.getenv("CLAUDE_SDK_API_KEY"))
        if not key:
            key = _as_str(os.getenv("DEEPSEEK_API_KEY"))
        if not key:
            key = _as_str(os.getenv("LLM_API_KEY"))
    if not key:
        raise RuntimeError(
            "Claude SDK backend 需要设置 ANTHROPIC_API_KEY、CLAUDE_SDK_API_KEY、"
            "DEEPSEEK_API_KEY 或 LLM_API_KEY 其中一个环境变量"
        )
    return key


def _api_base_url() -> str:
    """返回自定义 API base URL。

    SDK 内部会自动追加 ``/v1/messages`` 路径（anthropic>=0.100.0），
    所以返回的 base URL **不能**包含 ``/v1`` 后缀，否则会拼出
    ``/v1/v1/messages`` 双路径。

    优先级：
    1. ANTHROPIC_API_BASE（由 Settings 页面设置或手动配置）
    2. ANTHROPIC_BASE_URL（兼容旧环境变量）
    3. LLM_API_BASE（Settings 页面的通用 API Base 字段）
    4. 模型名以 ``deepseek`` 开头时自动使用 DeepSeek 兼容端点
    5. 默认 ``https://api.anthropic.com``
    """
    url = _as_str(os.getenv("ANTHROPIC_API_BASE"))
    if not url:
        url = _as_str(os.getenv("ANTHROPIC_BASE_URL"))
    if not url:
        url = _as_str(os.getenv("LLM_API_BASE"))
    if not url:
        model = _model_name()
        if model.startswith("deepseek"):
            url = "https://api.deepseek.com/anthropic"
        else:
            url = "https://api.anthropic.com"
    if url:
        url = url.rstrip("/")
        if url.endswith("/v1"):
            url = url[:-3]
    return url


# ── Skills → Tools ─────────────────────────────────────────────────────────

def _load_skills_as_tools() -> List[Dict[str, Any]]:
    """加载 YAML skills 并转为 Claude tool 定义。"""
    from ai.skills.loader import list_skill_names, load_skill_by_name
    try:
        names = list_skill_names()
        tools = []
        for name in names:
            result = load_skill_by_name(name, backend="claude_sdk")
            if result:
                if isinstance(result, list):
                    tools.extend(result)
                else:
                    tools.append(result)
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
        # 避免重复: 检查最后一条 user 消息是否已包含 question
        if not messages or messages[-1].get("content") != context.question:
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
    tools_adapter: ToolAdapter,
    memory: SessionMemory,
) -> Tuple[str, int]:
    """执行 Claude 选择的工具调用。"""
    from ai.command.normalizer import normalize_command_spec
    from ai.command.compiler import compile_command
    from ai.command.security import evaluate_command

    spec = normalize_command_spec({
        "command": tool_name,
        "args": tool_input,
        "source_target": source_target,
    })

    # 去重检查
    if memory and memory.is_duplicate(spec):
        return f"Command skipped: already executed in this session", 0

    # 安全检查
    security = evaluate_command(spec)
    if not security.allowed:
        return f"Command rejected: {security.reason}", -1

    # 编译执行
    compiled = compile_command(spec)
    result = await tools_adapter.execute(compiled)

    # 记录到 session memory
    if memory:
        memory.record(
            spec,
            exit_code=result.exit_code,
            summary=result.stdout[:120] if result.exit_code == 0 else f"failed: {result.stderr[:120]}",
            output_preview=result.stdout[:2000],
        )

    return result.stdout if hasattr(result, "stdout") else str(result), result.exit_code


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

    try:
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
                    if getattr(block, "type", None) == "text":
                        text = _as_str(getattr(block, "text", ""))
                        if text:
                            collected_text += text
                            await event_emitter.emit(run_id, "assistant_delta", {"text": text})
                    elif getattr(block, "type", None) == "tool_use":
                        tool_use = {"id": block.id, "name": block.name, "input": block.input}
                elif event.type == "message_delta":
                    delta = event.delta
                    stop_reason = getattr(delta, "stop_reason", None) if delta else None
    except Exception:
        logger.warning("_stream_llm_turn: streaming failed, falling back to non-streaming")
        response = await client.messages.create(
            model=_model_name(),
            system=system_prompt,
            messages=messages,
            max_tokens=max_tokens,
            **tool_config,
        )
        collected_text = ""
        for block in response.content:
            if getattr(block, "type", None) == "text":
                text = _as_str(getattr(block, "text", ""))
                if text:
                    collected_text += text
                    await event_emitter.emit(run_id, "assistant_delta", {"text": text})
            elif getattr(block, "type", None) == "tool_use":
                tool_use = {"id": block.id, "name": block.name, "input": block.input}
        stop_reason = getattr(response, "stop_reason", None)

    return collected_text, stop_reason, tool_use


async def _run_claude_loop(
    system_prompt: str,
    messages: List[Dict[str, Any]],
    tools: List[Dict[str, Any]],
    event_emitter: EventEmitter,
    source_target: Optional[Dict[str, Any]],
    run_id: str,
    tools_adapter: ToolAdapter,
    memory: SessionMemory,
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
                tool_name, tool_input, source_target, event_emitter, run_id,
                tools_adapter, memory,
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
                        "tool_use_id": tool_use["id"],
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

        # 检查 API key 是否配置 — 未配置时优雅降级
        try:
            _ = _api_key()
        except RuntimeError:
            logger.warning("No API key configured — ClaudeSdkBackend returns empty result")
            return BackendResult(summary="Claude SDK 未配置 (未找到 API Key)")

        # 1. 加载 YAML skills → Claude @tool 定义
        tools = _load_skills_as_tools()

        # 2. 构建 system_prompt
        system_prompt = _build_system_prompt_from_context(ctx)

        # 3. 构建消息列表
        messages = _build_messages(ctx)

        # 4. 初始化 tools 和 memory
        tools_adapter = ToolAdapter()
        memory = SessionMemory()

        # 5. 执行 agent 循环
        return await _run_claude_loop(
            system_prompt=system_prompt,
            messages=messages,
            tools=tools,
            event_emitter=request.event_emitter,
            source_target=ctx.source_target,
            run_id=ctx.session_id,
            tools_adapter=tools_adapter,
            memory=memory,
        )


register_backend("claude-sdk", ClaudeSdkBackend)
