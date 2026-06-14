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
from typing import Any, Dict, List, Optional

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
    return _as_str(os.getenv("CLAUDE_SDK_MODEL"), "claude-sonnet-4-20250514")


def _max_tokens() -> int:
    return int(os.getenv("CLAUDE_SDK_MAX_TOKENS", "4096"))


def _max_turns() -> int:
    return max(1, min(int(os.getenv("CLAUDE_SDK_MAX_TURNS", "8")), 20))


def _api_key() -> str:
    key = _as_str(os.getenv("ANTHROPIC_API_KEY"))
    if not key:
        key = _as_str(os.getenv("CLAUDE_SDK_API_KEY"))
    if not key:
        raise RuntimeError("Claude SDK backend requires ANTHROPIC_API_KEY or CLAUDE_SDK_API_KEY")
    return key


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
) -> str:
    """Execute a single tool call via ToolAdapter and return output text.

    Maps skill tool names to actual command execution via the shared
    ToolAdapter → exec-service pipeline.
    """
    from ai.runtime.tools import ToolAdapter
    from ai.command.spec import CommandSpec, ToolType, CommandType
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
                command_spec = CommandSpec(
                    tool=ToolType(spec.get("tool", "generic_exec")),
                    command=spec.get("command", ""),
                    target_kind=_as_str(spec.get("target_kind", "k8s_cluster")),
                    target_identity=_as_str(spec.get("target_identity", "")),
                    timeout_seconds=int(spec.get("timeout_seconds", 20)),
                )
                try:
                    result = await adapter.execute(
                        command_spec,
                        session_id=run_id,
                        message_id="",
                        action_id=step.step_id,
                    )
                    out = _as_str(result.get("stdout") or result.get("output") or "")
                    err = _as_str(result.get("stderr"))
                    if err:
                        out += f"\n[stderr] {err}" if out else f"[stderr] {err}"
                    outputs.append(f"### {step.title}\n```\n{out[:3000]}\n```")
                except Exception as e:
                    outputs.append(f"### {step.title}\n执行失败: {e}")

            return "\n\n".join(outputs)

    return f"Tool '{tool_name}' executed (no matching YAML skill found)"


# ── Main loop ──────────────────────────────────────────────────────────────

async def _run_claude_loop(
    request: RuntimeBackendRequest,
) -> RuntimeBackendResult:
    """Run the Claude agent loop: plan → tool_call → observe → continue → done."""
    import anthropic

    client = anthropic.AsyncAnthropic(api_key=_api_key())
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

    while turn_count < max_turns:
        turn_count += 1
        logger.debug("Claude SDK turn %d/%d", turn_count, max_turns)

        response = await client.messages.create(
            model=_model_name(),
            max_tokens=_max_tokens(),
            system=system_prompt,
            messages=messages,
            tools=tools,
        )

        # Collect assistant text
        for block in response.content:
            if block.type == "text":
                text = _as_str(block.text)
                if text:
                    thoughts.append(text)

        # Check for tool calls
        tool_blocks = [b for b in response.content if b.type == "tool_use"]
        if not tool_blocks:
            # No more tool calls → done
            for block in response.content:
                if block.type == "text":
                    final_answer = _as_str(block.text)
            break

        # Process each tool call
        assistant_msg: Dict[str, Any] = {"role": "assistant", "content": []}
        for block in response.content:
            if block.type == "text":
                assistant_msg["content"].append({"type": "text", "text": block.text})
            elif block.type == "tool_use":
                assistant_msg["content"].append({
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
                })
        messages.append(assistant_msg)

        # Execute tool calls and collect results
        for block in tool_blocks:
            tool_name = _as_str(block.name)
            tool_input = dict(block.input or {})
            tool_calls.append({"tool_name": tool_name, "tool_args": tool_input})

            logger.debug("Executing tool: %s", tool_name)
            output = await _execute_tool_call(tool_name, tool_input, request.run_id)

            messages.append({
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": output[:10000],
                    }
                ],
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

        try:
            asyncio.get_running_loop()
            # Already in an event loop — run in a new thread to avoid nesting
            result: List[RuntimeBackendResult] = []

            def _target():
                new_loop = asyncio.new_event_loop()
                asyncio.set_event_loop(new_loop)
                try:
                    r = new_loop.run_until_complete(_run())
                    result.append(r)
                finally:
                    new_loop.close()

            thread = threading.Thread(target=_target, daemon=True)
            thread.start()
            thread.join(timeout=300)
            if not result:
                raise RuntimeError("Claude SDK backend timed out or failed to start")
            return result[0]
        except RuntimeError:
            # No running event loop — safe to asyncio.run()
            return asyncio.run(_run())


__all__ = ["ClaudeSdkBackend"]
