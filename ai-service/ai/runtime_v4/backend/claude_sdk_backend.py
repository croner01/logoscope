"""
Claude SDK backend — uses Anthropic Messages API with native tool calling.

Loads skills from YAML definitions, converts them to Claude tool definitions,
and runs an agent loop: Claude decides which tool to call → ToolAdapter
executes → results fed back → Claude decides next action → till done.

Backend name: ``claude-sdk-v1``
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from ai.agent_runtime import event_protocol
from ai.runtime_v4.backend.base import RuntimeBackend, RuntimeBackendRequest, RuntimeBackendResult

logger = logging.getLogger(__name__)


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


# ── Configuration helpers ─────────────────────────────────────────────────

def _model_name() -> str:
    return (
        _as_str(os.getenv("CLAUDE_SDK_MODEL"))
        or _as_str(os.getenv("LLM_MODEL"))
        or "claude-sonnet-4-20250514"
    )


def _max_tokens() -> int:
    return int(os.getenv("CLAUDE_SDK_MAX_TOKENS", "4096"))


def _max_turns() -> int:
    return max(1, min(int(os.getenv("CLAUDE_SDK_MAX_TURNS", "8")), 20))


def _api_key() -> str:
    """返回符合当前 provider 的 API key。

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

    SDK 内部会自动追加 ``/v1/messages`` 路径，所以此处返回的 base URL
    **不能**包含 ``/v1`` 后缀，否则会拼出 ``/v1/v1/messages`` 双路径。

    优先级：
    1. ANTHROPIC_API_BASE（由 Settings 页面设置或手动配置）
    2. LLM_API_BASE（Settings 页面的通用 API Base 字段）
    3. 模型名以 ``deepseek`` 开头时自动使用 DeepSeek 兼容端点
    """
    url = _as_str(os.getenv("ANTHROPIC_API_BASE"))
    if not url:
        url = _as_str(os.getenv("LLM_API_BASE"))
    if not url:
        model = _model_name()
        if model.startswith("deepseek"):
            url = "https://api.deepseek.com/anthropic"
    # 去掉可能残留的 /v1 后缀（SDK 会自动拼接 /v1/messages）
    if url:
        url = url.rstrip("/")
        if url.endswith("/v1"):
            url = url[:-3]
    return url


# ── Skill → Tool definitions ──────────────────────────────────────────────

def _load_skills_as_tools(skill_names: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Load YAML skill files as Claude tool definitions.

    Scans all three skill directories (builtin / installed / custom) via
    ``SkillManager``.  When *skill_names* is None, returns **all** visible
    skills respecting priority (custom > installed > builtin).
    """
    from ai.skills.loader import load_tool_definitions
    from ai.skills.manager import SkillManager

    mgr = SkillManager()
    tools: List[Dict[str, Any]] = []

    if skill_names:
        # Resolve each requested name across the three directories
        for name in skill_names:
            source = mgr.get_skill(name)
            if source and os.path.isfile(source.file_path):
                try:
                    tool_defs = load_tool_definitions(source.file_path)
                    tools.extend(tool_defs)
                    logger.debug("Loaded skill tool: %s (%s)", name, source.source_dir)
                except Exception:
                    logger.warning("Failed to load skill: %s", name, exc_info=True)
    else:
        # All visible skills (priority: custom > installed > builtin)
        for skill in mgr.list_all():
            try:
                tool_defs = load_tool_definitions(skill.file_path)
                tools.extend(tool_defs)
                logger.debug("Loaded skill tool: %s (%s)", skill.name, skill.source_dir)
            except Exception:
                logger.warning("Failed to load skill: %s", skill.name, exc_info=True)

    return tools


# ── System prompt ─────────────────────────────────────────────────────────

def _build_system_prompt(request: RuntimeBackendRequest) -> str:
    """Build system prompt for the Claude agent."""
    ctx = request.analysis_context or {}

    parts = [
        "你是一个专业的故障诊断 AI 助手。请使用提供的诊断工具来排查问题。",
        "",
        "## 工作原则",
        "1. 按需使用工具收集证据，不要一次性调用所有工具",
        "2. 每次工具调用后，分析输出结果，决定下一步",
        "3. 读操作自动执行，写操作需要说明理由",
        "4. 当收集到足够证据时，给出诊断结论",
        "5. 不要猜测——基于实际输出进行分析",
        "",
        "## 当前上下文",
    ]

    for key in ("service_name", "namespace", "component_type", "trace_id"):
        val = _as_str(ctx.get(key))
        if val:
            parts.append(f"- {key}: {val}")

    question = _as_str(request.question)
    if question:
        parts.extend(["", f"## 诊断问题", question])

    return "\n".join(parts)


# ── Tool call execution ────────────────────────────────────────────────────

async def _execute_tool_call(
    tool_name: str,
    tool_input: Dict[str, Any],
    run_id: str,
    *,
    source_target: Optional[Dict[str, Any]] = None,
) -> str:
    """Execute a single tool call via ToolAdapter and return output text.

    Maps skill tool names to actual command execution via the shared
    ToolAdapter → exec-service pipeline.

    Args:
        tool_name: Name of the skill/tool to execute.
        tool_input: Input dict from the LLM tool call.
        run_id: Current agent run ID.
        source_target: Optional source_target metadata from analysis context.
            Passed to ``normalize_command_spec`` so kubectl/SSH commands
            get the correct ``target_identity`` for remote cluster routing.
    """
    from ai.runtime.tools import ToolAdapter
    from ai.command.normalizer import normalize_command_spec
    from ai.command.compiler import compile_command
    from ai.skills.loader import load_skill_steps, resolve_skill_path
    from ai.skills.base import SkillContext

    # Resolve context from tool input
    ctx_data = tool_input.get("context") or {}
    service_name = _as_str(ctx_data.get("service_name"))
    namespace = _as_str(ctx_data.get("namespace"), "islap")

    # Resolve skill YAML across builtin / installed / custom directories
    yaml_path = resolve_skill_path(tool_name)

    if yaml_path:
        skill_ctx = SkillContext(service_name=service_name, namespace=namespace)
        steps = load_skill_steps(yaml_path, context=skill_ctx)
        if steps:
            # Execute each step sequentially
            outputs = []
            adapter = ToolAdapter()
            for step in steps:
                spec = step.command_spec
                try:
                    # Use normalize_command_spec for alias mapping
                    # (kubectl_clickhouse_query → clickhouse_query, etc.)
                    # Pass source_target so remote cluster routing works:
                    # the normalizer builds target_identity from source_target
                    # (e.g. namespace:ems → openstack-cluster-01 via registry),
                    # and compile_command uses it to select the right executor.
                    command_spec = normalize_command_spec(
                        {
                            "tool": spec.get("tool", "generic_exec"),
                            "command": spec.get("command", ""),
                            "target_kind": _as_str(spec.get("target_kind", "k8s_cluster")),
                            "target_identity": _as_str(spec.get("target_identity", "")),
                            "purpose": _as_str(spec.get("purpose", "")),
                            "timeout_seconds": int(spec.get("timeout_seconds", 20)),
                        },
                        source_target=source_target,
                    )
                except Exception:
                    # Fallback: skip steps with invalid tool/command spec
                    outputs.append(f"### {step.title}\n执行失败: 无效的命令规范 (tool={spec.get('tool')})")
                    continue
                try:
                    # Compile CommandSpec → CompiledCommand (ToolAdapter requires
                    # CompiledCommand with .spec + .shell_command fields)
                    compiled = compile_command(command_spec, namespace=namespace)
                except Exception:
                    outputs.append(f"### {step.title}\n执行失败: 无法编译命令")
                    continue
                try:
                    result = await adapter.execute(
                        compiled,
                        session_id=run_id,
                        message_id="",
                        action_id=step.step_id,
                    )
                    out = _as_str(result.stdout)
                    err = _as_str(result.stderr)
                    if err:
                        out += f"\n[stderr] {err}" if out else f"[stderr] {err}"
                    outputs.append(f"### {step.title}\n```\n{out[:3000]}\n```")
                except Exception as e:
                    outputs.append(f"### {step.title}\n执行失败: {e}")

            return "\n\n".join(outputs)

    return f"Tool '{tool_name}' executed (no matching YAML skill found)"


# ── Main loop ──────────────────────────────────────────────────────────────


def _get_runtime_service():
    """Get AgentRuntimeService for event emission (lazy import avoids circular deps)."""
    try:
        from ai.agent_runtime.service import get_agent_runtime_service
        return get_agent_runtime_service()
    except Exception:
        return None


async def _stream_llm_turn(
    client: "anthropic.AsyncAnthropic",
    system_prompt: str,
    messages: List[Dict[str, Any]],
    tools: List[Dict[str, Any]],
    run_id: str,
    runtime_service: Any,
) -> Tuple[str, List[Dict[str, Any]], bool]:
    """Stream one LLM turn, emit assistant_delta tokens, return (text, tool_blocks, is_final).

    Falls back to non-streaming ``client.messages.create()`` if the streaming
    API raises (handles DeepSeek compatibility gaps gracefully).
    """
    collected_text = ""
    collected_tool_blocks: List[Dict[str, Any]] = []

    # ── Try streaming first ───────────────────────────────────────────
    try:
        async with client.messages.stream(
            model=_model_name(),
            max_tokens=_max_tokens(),
            system=system_prompt,
            messages=messages,
            tools=tools,
        ) as stream:
            async for event in stream:
                if event.type == "content_block_start":
                    cb = event.content_block
                    if getattr(cb, "type", None) == "text":
                        text = _as_str(getattr(cb, "text", ""))
                        if text:
                            collected_text += text
                            if runtime_service:
                                runtime_service.append_assistant_delta(
                                    run_id, text=text
                                )

                elif event.type == "content_block_delta":
                    delta = event.delta
                    if getattr(delta, "type", None) == "text_delta":
                        text = _as_str(getattr(delta, "text", ""))
                        if text:
                            collected_text += text
                            if runtime_service:
                                runtime_service.append_assistant_delta(
                                    run_id, text=text
                                )

            # Get the complete Message for tool_use blocks
            final_message = await stream.get_final_message()
            for block in getattr(final_message, "content", []):
                if getattr(block, "type", None) == "tool_use":
                    collected_tool_blocks.append({
                        "type": "tool_use",
                        "id": block.id,
                        "name": block.name,
                        "input": dict(block.input or {}),
                    })

    except Exception as exc:
        logger.warning("Streaming LLM call failed, falling back to non-streaming: %s", exc)
        # ── Fallback: non-streaming ───────────────────────────────────
        try:
            response = await client.messages.create(
                model=_model_name(),
                max_tokens=_max_tokens(),
                system=system_prompt,
                messages=messages,
                tools=tools,
            )
        except Exception as create_exc:
            logger.error("Non-streaming LLM call also failed: %s", create_exc)
            return collected_text, [], True  # treat as final to break the loop

        for block in response.content:
            if block.type == "text":
                text = _as_str(block.text)
                if text:
                    collected_text += text
                    if runtime_service:
                        runtime_service.append_assistant_delta(run_id, text=text)
            elif block.type == "tool_use":
                collected_tool_blocks.append({
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": dict(block.input or {}),
                })

    is_final = len(collected_tool_blocks) == 0
    return collected_text, collected_tool_blocks, is_final


async def _run_claude_loop(
    request: RuntimeBackendRequest,
) -> RuntimeBackendResult:
    """Run the Claude agent loop: plan → tool_call → observe → continue → done.

    Uses streaming for real-time token output (``assistant_delta`` events),
    with per-turn fallback to non-streaming if the API does not support it.
    Emits ``tool_call_started`` / ``tool_call_finished`` events for every
    tool execution so the frontend can show live progress.
    """
    import anthropic

    base_url = _api_base_url()
    client = anthropic.AsyncAnthropic(api_key=_api_key(), base_url=base_url) if base_url else anthropic.AsyncAnthropic(api_key=_api_key())
    tools = _load_skills_as_tools()
    system_prompt = _build_system_prompt(request)

    messages: List[Dict[str, Any]] = [
        {"role": "user", "content": _as_str(request.question) or "请进行诊断"}
    ]

    thoughts: List[str] = []
    tool_calls: List[Dict[str, Any]] = []
    turn_count = 0
    max_turns = _max_turns()
    final_answer = ""

    # Extract source_target from analysis context for remote cluster routing.
    # source_target is set by the semantic engine (e.g. -> {namespace: "ems", pod_name: "..."}),
    # and normalize_command_spec uses it to build the correct target_identity
    # matching the remote target registry (e.g. openstack-cluster-01).
    ctx = request.analysis_context or {}
    source_target: Optional[Dict[str, Any]] = ctx.get("source_target")
    source_target = source_target if isinstance(source_target, dict) else None

    runtime_service = _get_runtime_service()

    while turn_count < max_turns:
        turn_count += 1
        logger.debug("Claude SDK turn %d/%d", turn_count, max_turns)

        # ── Stream LLM response ──────────────────────────────────────
        collected_text, tool_blocks, is_final = await _stream_llm_turn(
            client, system_prompt, messages, tools,
            request.run_id, runtime_service,
        )

        if collected_text:
            thoughts.append(collected_text)

        if is_final:
            final_answer = collected_text
            break

        # ── Build assistant message for history ───────────────────────
        assistant_msg: Dict[str, Any] = {"role": "assistant", "content": []}
        if collected_text:
            assistant_msg["content"].append({"type": "text", "text": collected_text})
        for tb in tool_blocks:
            assistant_msg["content"].append(tb)
        messages.append(assistant_msg)

        # ── Execute tool calls with streaming events ──────────────────
        tool_results: List[Dict[str, Any]] = []
        for tb in tool_blocks:
            tool_name = _as_str(tb.get("name", ""))
            tool_input = dict(tb.get("input", {}) or {})
            tool_calls.append({"tool_name": tool_name, "tool_args": tool_input})

            # Emit tool_call_started
            if runtime_service:
                runtime_service.append_event(
                    request.run_id,
                    event_protocol.TOOL_CALL_STARTED,
                    {"tool_name": tool_name, "tool_input": tool_input},
                )

            logger.debug("Executing tool: %s", tool_name)
            try:
                output = await _execute_tool_call(
                    tool_name, tool_input, request.run_id,
                    source_target=source_target,
                )
                success = True
            except Exception as exc:
                output = f"Tool execution failed: {exc}"
                success = False
                logger.warning("Tool %s failed: %s", tool_name, exc)

            # Emit tool_call_finished
            if runtime_service:
                runtime_service.append_event(
                    request.run_id,
                    event_protocol.TOOL_CALL_FINISHED,
                    {
                        "tool_name": tool_name,
                        "success": success,
                        "output": output[:500],
                    },
                )

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tb.get("id", ""),
                "content": output[:10000],
            })

        # All tool_results from the same assistant turn must be placed in a
        # single user message.  DeepSeek's Anthropic-compatible API enforces
        # this strictly — multiple consecutive user messages with tool_result
        # blocks cause 400 errors:
        #   "tool_use ids were found without tool_result blocks immediately after"
        if tool_results:
            messages.append({
                "role": "user",
                "content": tool_results,
            })

    return RuntimeBackendResult(
        inner_engine="claude-sdk-v1",
        payload={
            "mode": "approval_gated",
            "thoughts": thoughts[:16],
            "tool_calls": tool_calls[:16],
            "answer": final_answer,
            "turn_count": turn_count,
        },
    )


# ── Backend class ─────────────────────────────────────────────────────────

class ClaudeSdkBackend(RuntimeBackend):
    """Runtime backend using Anthropic's Messages API with native tool calling.

    Loads diagnostic skills from YAML definitions, converts them to Claude
    tool definitions, and runs an agent loop.
    """

    def backend_name(self) -> str:
        return "claude-sdk-v1"

    def run(self, request: RuntimeBackendRequest) -> RuntimeBackendResult:
        """Run the Claude SDK backend.

        Wraps the async Claude loop for synchronous callers. If called from
        an already-running event loop, creates a new loop in a dedicated thread.
        """
        import asyncio
        import threading

        async def _run():
            return await _run_claude_loop(request)

        # Check if we're already in an event loop
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # No running event loop — safe to asyncio.run()
            return asyncio.run(_run())

        # Already in an event loop — run in a new thread to avoid nesting
        result: List[RuntimeBackendResult] = []
        thread_error: List[Exception] = []

        def _target():
            new_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(new_loop)
            try:
                r = new_loop.run_until_complete(_run())
                result.append(r)
            except Exception as exc:
                thread_error.append(exc)
            finally:
                new_loop.close()

        thread = threading.Thread(target=_target, daemon=True)
        thread.start()
        thread.join(timeout=300)

        if thread_error:
            raise RuntimeError(f"Claude SDK backend failed: {thread_error[0]}") from thread_error[0]
        if not result:
            raise RuntimeError("Claude SDK backend timed out or failed to start")
        return result[0]


__all__ = ["ClaudeSdkBackend"]
