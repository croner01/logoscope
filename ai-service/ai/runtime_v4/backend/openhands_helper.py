"""Isolated OpenHarness helper that emits planning thoughts and tool intents."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

from pydantic import BaseModel, Field

from openharness.api.client import ApiMessageCompleteEvent, ApiMessageRequest, ApiTextDeltaEvent
from openharness.api.openai_client import OpenAICompatibleClient
from openharness.engine.messages import ConversationMessage, ToolUseBlock
from openharness.engine.query_engine import QueryEngine
from openharness.engine.stream_events import (
    AssistantTextDelta,
    AssistantTurnComplete,
    ToolExecutionCompleted,
    ToolExecutionStarted,
)
from openharness.permissions.checker import PermissionChecker
from openharness.permissions.modes import PermissionMode
from openharness.config.settings import PermissionSettings
from openharness.tools.base import BaseTool, ToolExecutionContext, ToolRegistry, ToolResult


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _as_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return int(default)
        return int(str(value).strip())
    except Exception:
        return int(default)


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def _as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _normalize_text(value: Any) -> str:
    return " ".join(_as_str(value).split()).strip()


def _helper_model() -> str:
    return _as_str(os.getenv("AI_RUNTIME_V4_OPENHANDS_MODEL") or os.getenv("LLM_MODEL") or "gpt-5.4").strip()


def _helper_base_url() -> str | None:
    raw = _as_str(os.getenv("AI_RUNTIME_V4_OPENHANDS_BASE_URL") or os.getenv("LLM_API_BASE") or os.getenv("OPENAI_BASE_URL"))
    return raw.strip() or None


def _helper_api_key() -> str:
    for key in (
        "AI_RUNTIME_V4_OPENHANDS_API_KEY",
        "OPENAI_API_KEY",
        "LLM_API_KEY",
    ):
        value = _as_str(os.getenv(key)).strip()
        if value:
            return value
    raise RuntimeError("OpenHands helper requires AI_RUNTIME_V4_OPENHANDS_API_KEY or OPENAI_API_KEY")


def _helper_max_turns() -> int:
    return max(1, min(_as_int(os.getenv("AI_RUNTIME_V4_OPENHANDS_MAX_TURNS"), 3), 6))


def _helper_max_tokens() -> int:
    return max(512, min(_as_int(os.getenv("AI_RUNTIME_V4_OPENHANDS_MAX_TOKENS"), 2048), 8192))


def _render_context_markdown(analysis_context: Dict[str, Any], runtime_options: Dict[str, Any]) -> str:
    lines = ["## Runtime context"]
    for key in ("service_name", "namespace", "component_type", "runtime_profile", "thread_id", "trace_id"):
        value = _normalize_text(analysis_context.get(key))
        if value:
            lines.append(f"- {key}: {value}")
    if runtime_options:
        lines.append("## Runtime options")
        for key in ("auto_exec_readonly", "enable_skills", "max_skills"):
            if key in runtime_options:
                lines.append(f"- {key}: {_normalize_text(runtime_options.get(key))}")
    return "\n".join(lines)


def _build_system_prompt() -> str:
    return (
        "You are the OpenHarness planning backend embedded behind ai-service.\n"
        "Your job is to plan safe diagnostic actions, not to execute them directly.\n"
        "Use only the provided capture tools to describe intended actions.\n"
        "Prefer structured read-only diagnostics. High-risk or mutating commands must still be emitted as intents;\n"
        "they will be approval-gated by the caller.\n"
        "When a ClickHouse SQL diagnostic is needed, use the dedicated kubectl_clickhouse_query tool.\n"
        "Do not ask the user for confirmation in this helper. Do not reference unavailable tools."
    )


def _build_user_prompt(request_payload: Dict[str, Any]) -> str:
    question = _normalize_text(request_payload.get("question"))
    analysis_context = _as_dict(request_payload.get("analysis_context"))
    runtime_options = _as_dict(request_payload.get("runtime_options"))
    context_block = _render_context_markdown(analysis_context, runtime_options)
    return f"{question}\n\n{context_block}".strip()


class GenericExecInput(BaseModel):
    command: str = ""
    purpose: str = ""
    title: str = ""
    target_kind: str = "runtime_node"
    target_identity: str = "runtime:local"
    timeout_s: int = Field(default=20, ge=3, le=180)


class KubectlClickHouseQueryInput(BaseModel):
    query: str = ""
    namespace: str = "islap"
    pod_name: str = ""
    purpose: str = ""
    title: str = ""
    target_kind: str = "clickhouse_cluster"
    target_identity: str = "database:logs"
    timeout_s: int = Field(default=30, ge=3, le=180)


def _capture_output(tool_name: str, payload: Dict[str, Any]) -> str:
    return json.dumps({"captured": True, "tool_name": tool_name, "tool_args": payload}, ensure_ascii=False)


class CaptureGenericExecTool(BaseTool):
    name = "generic_exec"
    description = "Capture a structured shell diagnostic intent without executing it."
    input_model = GenericExecInput

    async def execute(self, arguments: BaseModel, context: ToolExecutionContext) -> ToolResult:
        _ = context
        payload = arguments.model_dump()
        return ToolResult(output=_capture_output(self.name, payload), is_error=False, metadata={"captured": True})

    def is_read_only(self, arguments: BaseModel) -> bool:
        command = _as_str(getattr(arguments, "command", "")).lower()
        readonly_markers = (
            " get ",
            " describe ",
            " logs ",
            " top ",
            " cat ",
            " grep ",
            " select ",
        )
        normalized = f" {command} "
        return any(marker in normalized for marker in readonly_markers)


class CaptureKubectlClickHouseQueryTool(BaseTool):
    name = "kubectl_clickhouse_query"
    description = "Capture a ClickHouse SQL diagnostic intent without executing it."
    input_model = KubectlClickHouseQueryInput

    async def execute(self, arguments: BaseModel, context: ToolExecutionContext) -> ToolResult:
        _ = context
        payload = arguments.model_dump()
        return ToolResult(output=_capture_output(self.name, payload), is_error=False, metadata={"captured": True})

    def is_read_only(self, arguments: BaseModel) -> bool:
        query = _as_str(getattr(arguments, "query", "")).strip().lower()
        return query.startswith("select") or query.startswith("show") or query.startswith("describe")


def _build_tool_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(CaptureGenericExecTool())
    registry.register(CaptureKubectlClickHouseQueryTool())
    return registry


def _permission_checker() -> PermissionChecker:
    return PermissionChecker(PermissionSettings(mode=PermissionMode.FULL_AUTO))


def _extract_tool_calls(message: ConversationMessage) -> List[Dict[str, Any]]:
    calls: List[Dict[str, Any]] = []
    for block in message.content:
        if isinstance(block, ToolUseBlock):
            calls.append(
                {
                    "action_id": _as_str(block.id),
                    "tool_name": _as_str(block.name),
                    "tool_args": dict(block.input or {}),
                }
            )
    return calls


async def _run_helper(request_payload: Dict[str, Any]) -> Dict[str, Any]:
    api_client = OpenAICompatibleClient(
        _helper_api_key(),
        base_url=_helper_base_url(),
        timeout=float(max(_helper_max_turns() * 30, 30)),
    )
    engine = QueryEngine(
        api_client=api_client,
        tool_registry=_build_tool_registry(),
        permission_checker=_permission_checker(),
        cwd=Path("/app"),
        model=_helper_model(),
        system_prompt=_build_system_prompt(),
        max_tokens=_helper_max_tokens(),
        max_turns=_helper_max_turns(),
    )

    thoughts: List[str] = []
    tool_calls: List[Dict[str, Any]] = []
    assistant_text_parts: List[str] = []

    async for event in engine.submit_message(_build_user_prompt(request_payload)):
        if isinstance(event, AssistantTextDelta):
            text = _normalize_text(event.text)
            if text:
                thoughts.append(text)
        elif isinstance(event, ToolExecutionStarted):
            tool_calls.append(
                {
                    "action_id": "",
                    "tool_name": _as_str(event.tool_name),
                    "tool_args": dict(event.tool_input or {}),
                }
            )
        elif isinstance(event, ToolExecutionCompleted):
            try:
                parsed = json.loads(_as_str(event.output))
            except json.JSONDecodeError:
                parsed = {}
            if tool_calls and isinstance(parsed, dict):
                last = tool_calls[-1]
                last["tool_name"] = _as_str(parsed.get("tool_name") or last.get("tool_name"))
                last["tool_args"] = _as_dict(parsed.get("tool_args")) or _as_dict(last.get("tool_args"))
        elif isinstance(event, AssistantTurnComplete):
            assistant_text = _normalize_text(event.message.text)
            if assistant_text:
                assistant_text_parts.append(assistant_text)
            extracted = _extract_tool_calls(event.message)
            for index, call in enumerate(extracted):
                if index < len(tool_calls) and not tool_calls[index].get("action_id"):
                    tool_calls[index]["action_id"] = _as_str(call.get("action_id"))
                elif call:
                    tool_calls.append(call)

    if assistant_text_parts and not thoughts:
        thoughts.extend(assistant_text_parts)

    return {
        "provider": "openharness-subprocess",
        "mode": "approval_gated",
        "thoughts": thoughts[:8],
        "tool_calls": tool_calls[:8],
        "selected_skills": [],
    }


def main() -> int:
    payload = json.load(sys.stdin)
    request_payload = _as_dict(payload.get("request"))
    result = asyncio.run(_run_helper(request_payload))
    sys.stdout.write(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
