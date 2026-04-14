"""
Tests for AI agent runtime run/event APIs.
"""

import asyncio
from types import SimpleNamespace

from fastapi import HTTPException
import pytest

from api.ai import (
    AIRunApproveRequest,
    AIRunCancelRequest,
    AIRunCommandRequest,
    AIRunCreateRequest,
    AIRunInputRequest,
    AIRunInterruptRequest,
    approve_ai_run,
    cancel_ai_run,
    continue_ai_run_with_user_input,
    create_ai_run,
    ensure_runtime_input_context_ready,
    execute_ai_run_command,
    get_ai_run,
    get_ai_run_events,
    interrupt_ai_run,
    stream_ai_run,
    _RuntimePauseForPendingAction,
    _build_followup_request_from_ai_run,
    _emit_followup_runtime_event,
    _run_followup_runtime_task,
    runtime_v1_api_guard_bypass,
)
from ai.agent_runtime import event_protocol
from ai.agent_runtime.service import AgentRuntimeService


def _build_runtime_service() -> AgentRuntimeService:
    return AgentRuntimeService(storage_adapter=None)


def _generic_exec_spec(
    command: str,
    *,
    target_kind: str = "runtime_node",
    target_identity: str = "runtime:local",
    timeout_s: int = 20,
) -> dict:
    return {
        "tool": "generic_exec",
        "args": {
            "command": command,
            "target_kind": target_kind,
            "target_identity": target_identity,
            "timeout_s": timeout_s,
        },
        "target_kind": target_kind,
        "target_identity": target_identity,
        "timeout_s": timeout_s,
    }


def test_create_ai_run_returns_run_snapshot(monkeypatch):
    runtime_service = _build_runtime_service()
    monkeypatch.setattr("api.ai.get_agent_runtime_service", lambda *_args, **_kwargs: runtime_service)

    async def _run():
        return await create_ai_run(
            AIRunCreateRequest(
                session_id="sess-001",
                question="排查 query-service 超时",
                analysis_context={"analysis_type": "log", "service_name": "query-service"},
                runtime_options={"max_iterations": 3, "conversation_id": "conv-001"},
            )
        )

    result = asyncio.run(_run())

    run = result["run"]
    assert run["run_id"].startswith("run-")
    assert run["session_id"] == "sess-001"
    assert run["conversation_id"] == "conv-001"
    assert run["assistant_message_id"].startswith("msg-")
    assert run["status"] == "running"
    assert run["summary_json"]["current_phase"] == "planning"


def test_create_ai_run_auto_downgrades_trace_without_trace_id(monkeypatch):
    runtime_service = _build_runtime_service()
    monkeypatch.setattr("api.ai.get_agent_runtime_service", lambda *_args, **_kwargs: runtime_service)

    async def _run():
        return await create_ai_run(
            AIRunCreateRequest(
                session_id="sess-trace-downgrade",
                question="排查 trace 模式但缺少 trace_id",
                analysis_context={"analysis_type": "trace"},
                runtime_options={"conversation_id": "conv-trace-downgrade"},
            )
        )

    result = asyncio.run(_run())
    run = result["run"]
    assert run["analysis_type"] == "log"
    assert run["context_json"]["analysis_type_downgraded"] is True
    assert run["context_json"]["analysis_type_original"] == "trace"
    assert run["context_json"]["analysis_type_downgrade_reason"] == "trace_id_missing"
    assert run["summary_json"]["analysis_type_downgraded"] is True
    assert run["summary_json"]["analysis_type_original"] == "trace"
    assert run["summary_json"]["analysis_type_downgrade_reason"] == "trace_id_missing"

    created = runtime_service.create_run(
        session_id="sess-trace-downgrade-2",
        question="再次校验 service 层 downgrade",
        analysis_context={"analysis_type": "trace"},
        runtime_options={"conversation_id": "conv-trace-downgrade-2"},
    )
    assert created.analysis_type == "log"
    assert created.summary_json["analysis_type_downgraded"] is True
    assert created.summary_json["analysis_type_original"] == "trace"
    assert created.summary_json["analysis_type_downgrade_reason"] == "trace_id_missing"


def test_create_ai_run_rejects_when_runtime_v1_disabled(monkeypatch):
    runtime_service = _build_runtime_service()
    monkeypatch.setattr("api.ai.get_agent_runtime_service", lambda *_args, **_kwargs: runtime_service)
    monkeypatch.setenv("AI_RUNTIME_V1_API_ENABLED", "false")

    async def _run():
        return await create_ai_run(
            AIRunCreateRequest(
                session_id="sess-v1-disabled",
                question="排查 query-service 超时",
                analysis_context={"analysis_type": "log", "service_name": "query-service"},
            )
        )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(_run())
    assert exc_info.value.status_code == 410
    assert exc_info.value.detail["code"] == "RUNTIME_V1_DISABLED"


def test_create_ai_run_allows_internal_bypass_when_runtime_v1_disabled(monkeypatch):
    runtime_service = _build_runtime_service()
    monkeypatch.setattr("api.ai.get_agent_runtime_service", lambda *_args, **_kwargs: runtime_service)
    monkeypatch.setenv("AI_RUNTIME_V1_API_ENABLED", "false")

    async def _run():
        with runtime_v1_api_guard_bypass():
            return await create_ai_run(
                AIRunCreateRequest(
                    session_id="sess-v1-bypass",
                    question="内部 Temporal 活动调用",
                    analysis_context={"analysis_type": "log", "service_name": "query-service"},
                    runtime_options={"max_iterations": 1},
                )
            )

    result = asyncio.run(_run())
    assert result["run"]["run_id"].startswith("run-")


def test_get_ai_run_events_returns_initial_runtime_events(monkeypatch):
    runtime_service = _build_runtime_service()
    monkeypatch.setattr("api.ai.get_agent_runtime_service", lambda *_args, **_kwargs: runtime_service)

    async def _run():
        created = await create_ai_run(
            AIRunCreateRequest(
                session_id="sess-002",
                question="排查 payment-service 错误",
                analysis_context={"analysis_type": "log", "service_name": "payment-service"},
            )
        )
        run_id = created["run"]["run_id"]
        payload = await get_ai_run_events(run_id, after_seq=0, limit=20)
        return run_id, payload

    run_id, payload = asyncio.run(_run())

    assert payload["run_id"] == run_id
    event_types = [item["event_type"] for item in payload["events"]]
    assert "run_started" in event_types
    assert "message_started" in event_types
    assert "reasoning_step" in event_types
    assert payload["next_after_seq"] >= 4


def test_create_ai_run_does_not_auto_select_low_confidence_service_skill(monkeypatch):
    runtime_service = _build_runtime_service()
    monkeypatch.setattr("api.ai.get_agent_runtime_service", lambda *_args, **_kwargs: runtime_service)

    async def _run():
        created = await create_ai_run(
            AIRunCreateRequest(
                session_id="sess-skill-gating-001",
                question="service health check looks unstable",
                analysis_context={
                    "analysis_type": "log",
                    "service_name": "query-service",
                    "component_type": "service",
                    "runtime_profile": "ai-runtime-lab",
                },
            )
        )
        return created["run"]["run_id"]

    run_id = asyncio.run(_run())
    events = runtime_service.list_events(run_id, after_seq=0, limit=50)
    assert "skill_matched" not in [item.event_type for item in events]


def test_create_ai_run_followup_mode_emits_final_answer_events(monkeypatch):
    runtime_service = _build_runtime_service()
    monkeypatch.setattr("api.ai.get_agent_runtime_service", lambda *_args, **_kwargs: runtime_service)

    async def _fake_run_follow_up_analysis_core(request, event_callback=None):
        assert request.question == "继续分析 query-service 5xx"
        if callable(event_callback):
            await event_callback("plan", {"stage": "history_load"})
            await event_callback(
                "thought",
                {
                    "phase": "plan",
                    "title": "梳理上下文",
                    "detail": "已完成历史与关联线索加载。",
                    "status": "completed",
                    "iteration": 1,
                },
            )
            await event_callback("token", {"text": "定位到问题根因。"})
            await event_callback(
                "action",
                {
                    "message_id": "msg-followup-001",
                    "actions": [{"id": "act-001", "title": "检查 query-service 配置"}],
                },
            )
        return {
            "analysis_session_id": "sess-followup-001",
            "conversation_id": "conv-followup-001",
            "analysis_method": "langchain",
            "followup_engine": "langchain",
            "answer": "定位到问题根因。",
            "references": [{"id": "ref-1", "type": "log", "title": "错误日志"}],
            "actions": [{"id": "act-001", "title": "检查 query-service 配置"}],
            "action_observations": [],
            "react_loop": {},
            "react_iterations": [],
            "subgoals": [{"id": "sg-1", "title": "确认错误来源"}],
            "reflection": {"final_confidence": 0.82},
            "thoughts": [],
            "context_pills": [{"key": "service", "value": "query-service"}],
        }

    monkeypatch.setattr("api.ai._run_follow_up_analysis_core", _fake_run_follow_up_analysis_core)

    async def _run():
        created = await create_ai_run(
            AIRunCreateRequest(
                session_id="sess-followup-001",
                question="继续分析 query-service 5xx",
                analysis_context={"analysis_type": "log", "service_name": "query-service"},
                runtime_options={
                    "mode": "followup_analysis",
                    "conversation_id": "conv-followup-001",
                    "history": [{"role": "user", "content": "继续分析 query-service 5xx"}],
                },
            )
        )
        run_id = created["run"]["run_id"]
        fetched = None
        for _ in range(20):
            await asyncio.sleep(0.01)
            fetched = await get_ai_run(run_id)
            if fetched["run"]["status"] == "completed":
                break
        events = await get_ai_run_events(run_id, after_seq=0, limit=50)
        return fetched, events

    fetched, events = asyncio.run(_run())

    assert fetched["run"]["status"] == "completed"
    assert fetched["run"]["conversation_id"] == "conv-followup-001"
    assert fetched["run"]["summary_json"]["knowledge_pack_version"] == "2026-04-14.v2"
    assert fetched["run"]["summary_json"]["knowledge_primary_service"] == "query-service"
    assert fetched["run"]["summary_json"]["knowledge_primary_path"] == "log-ingest-query"
    assert "service=query-service" in fetched["run"]["summary_json"]["knowledge_selection_reason"]
    event_types = [item["event_type"] for item in events["events"]]
    assert "assistant_delta" in event_types
    assert "assistant_message_finalized" in event_types
    assert "run_finished" in event_types
    final_event = next(item for item in events["events"] if item["event_type"] == "assistant_message_finalized")
    assert "定位到问题根因。" in final_event["payload"]["content"]
    assert final_event["payload"]["metadata"]["analysis_method"] == "langchain"


def test_create_ai_run_followup_mode_blocks_when_react_replan_needed(monkeypatch):
    runtime_service = _build_runtime_service()
    monkeypatch.setattr("api.ai.get_agent_runtime_service", lambda *_args, **_kwargs: runtime_service)

    async def _fake_run_follow_up_analysis_core(_request, event_callback=None):
        if callable(event_callback):
            await event_callback("token", {"text": "需要继续补充证据。"})
        return {
            "analysis_session_id": "sess-followup-replan-001",
            "conversation_id": "conv-followup-replan-001",
            "analysis_method": "langchain",
            "followup_engine": "langchain",
            "answer": "当前证据不足，建议继续执行下一轮动作。",
            "references": [],
            "actions": [{"id": "act-replan-001", "title": "补查日志"}],
            "action_observations": [
                {
                    "action_id": "act-replan-001",
                    "status": "skipped",
                    "command": "kubectl -n islap logs deploy/query-service --tail=50",
                    "reason_code": "backend_unready",
                    "message": "执行网关未就绪，命令未自动执行。",
                }
            ],
            "react_loop": {
                "phase": "replan",
                "replan": {
                    "needed": True,
                    "items": [
                        {
                            "action_id": "act-replan-001",
                            "summary": "执行网关未就绪，建议稍后重试",
                            "execution_disposition": "backend_unready",
                        }
                    ],
                    "next_actions": ["执行网关未就绪，建议稍后重试"],
                },
            },
            "react_iterations": [],
            "subgoals": [],
            "reflection": {},
            "thoughts": [],
            "context_pills": [],
        }

    monkeypatch.setattr("api.ai._run_follow_up_analysis_core", _fake_run_follow_up_analysis_core)

    async def _run():
        created = await create_ai_run(
            AIRunCreateRequest(
                session_id="sess-followup-replan-001",
                question="继续分析 query-service 错误",
                analysis_context={"analysis_type": "log", "service_name": "query-service"},
                runtime_options={
                    "mode": "followup_analysis",
                    "conversation_id": "conv-followup-replan-001",
                    "history": [{"role": "user", "content": "继续分析 query-service 错误"}],
                },
            )
        )
        run_id = created["run"]["run_id"]
        fetched = None
        for _ in range(30):
            await asyncio.sleep(0.01)
            fetched = await get_ai_run(run_id)
            if fetched["run"]["status"] == "blocked":
                break
        events = await get_ai_run_events(run_id, after_seq=0, limit=80)
        return fetched, events

    fetched, events = asyncio.run(_run())

    assert fetched["run"]["status"] == "blocked"
    assert fetched["run"]["summary_json"]["blocked_reason"] == "react_replan_needed"
    event_types = [item["event_type"] for item in events["events"]]
    assert "assistant_message_finalized" in event_types
    assert "run_finished" in event_types
    run_finished_payload = next(item for item in events["events"] if item["event_type"] == "run_finished")["payload"]
    assert run_finished_payload["status"] == "blocked"
    assert run_finished_payload["blocked_reason"] == "react_replan_needed"


def test_create_ai_run_followup_mode_blocks_when_planning_incomplete(monkeypatch):
    runtime_service = _build_runtime_service()
    monkeypatch.setattr("api.ai.get_agent_runtime_service", lambda *_args, **_kwargs: runtime_service)

    async def _fake_run_follow_up_analysis_core(_request, event_callback=None):
        if callable(event_callback):
            await event_callback("token", {"text": "当前命令计划大多不可执行。"})
        return {
            "analysis_session_id": "sess-followup-planning-001",
            "conversation_id": "conv-followup-planning-001",
            "analysis_method": "langchain",
            "followup_engine": "langchain",
            "answer": "已识别到证据缺口，但当前命令计划大多不可执行。",
            "references": [],
            "actions": [{"id": "act-plan-001", "title": "补齐结构化命令"}],
            "action_observations": [],
            "react_loop": {
                "phase": "replan",
                "plan": {
                    "total_actions": 4,
                    "executable_actions": 1,
                    "spec_blocked_actions": 3,
                },
                "plan_quality": {
                    "planning_blocked": True,
                    "planning_blocked_reason": "当前命令计划中有 3/4 条动作仍不可执行，应先修复结构化命令再继续闭环。",
                    "spec_blocked_ratio": 0.75,
                },
                "replan": {
                    "needed": True,
                    "items": [],
                    "next_actions": ["当前命令计划中有 3/4 条动作仍不可执行，应先修复结构化命令再继续闭环。"],
                },
                "observe": {"coverage": 0.0, "confidence": 0.2},
            },
            "react_iterations": [],
            "subgoals": [],
            "reflection": {},
            "thoughts": [],
            "context_pills": [],
        }

    monkeypatch.setattr("api.ai._run_follow_up_analysis_core", _fake_run_follow_up_analysis_core)

    async def _run():
        created = await create_ai_run(
            AIRunCreateRequest(
                session_id="sess-followup-planning-001",
                question="继续分析 query-service 慢查询",
                analysis_context={"analysis_type": "log", "service_name": "query-service"},
                runtime_options={
                    "mode": "followup_analysis",
                    "conversation_id": "conv-followup-planning-001",
                    "history": [{"role": "user", "content": "继续分析 query-service 慢查询"}],
                },
            )
        )
        run_id = created["run"]["run_id"]
        fetched = None
        for _ in range(30):
            await asyncio.sleep(0.01)
            fetched = await get_ai_run(run_id)
            if fetched["run"]["status"] == "blocked":
                break
        events = await get_ai_run_events(run_id, after_seq=0, limit=80)
        return fetched, events

    fetched, events = asyncio.run(_run())

    assert fetched["run"]["status"] == "blocked"
    assert fetched["run"]["summary_json"]["blocked_reason"] == "planning_incomplete"
    gate_decision = fetched["run"]["summary_json"]["gate_decision"]
    assert gate_decision.get("reason") == "planning_incomplete"
    assert gate_decision.get("metrics", {}).get("planning_blocked") is True
    run_finished_payload = next(item for item in events["events"] if item["event_type"] == "run_finished")["payload"]
    assert run_finished_payload["status"] == "blocked"
    assert run_finished_payload["blocked_reason"] == "planning_incomplete"


def test_create_ai_run_followup_mode_marks_policy_block_when_templates_exist(monkeypatch):
    runtime_service = _build_runtime_service()
    monkeypatch.setattr("api.ai.get_agent_runtime_service", lambda *_args, **_kwargs: runtime_service)

    async def _fake_run_follow_up_analysis_core(_request, event_callback=None):
        if callable(event_callback):
            await event_callback("token", {"text": "已生成排查命令，但本轮未自动执行。"})
        return {
            "analysis_session_id": "sess-policy-001",
            "conversation_id": "conv-policy-001",
            "analysis_method": "langchain",
            "followup_engine": "langchain",
            "answer": "已生成排查命令，但本轮未自动执行。",
            "references": [],
            "actions": [
                {
                    "id": "tmpl-1",
                    "source": "template_command",
                    "title": "自动补证据命令：kubectl -n islap logs -l app=query-service --since-time=2026-04-12T13:26:14Z --tail=200",
                    "command": "kubectl -n islap logs -l app=query-service --since-time=2026-04-12T13:26:14Z --tail=200",
                    "command_type": "query",
                    "executable": True,
                    "reason": "structured_template_ready_for_auto_exec",
                }
            ],
            "action_observations": [],
            "react_loop": {
                "phase": "replan",
                "plan": {"ready_template_actions": 1},
                "replan": {"needed": True, "next_actions": ["本轮只生成命令，不会自动执行"]},
                "plan_quality": {"planning_blocked": False},
                "execute": {"observed_actions": 0},
            },
            "react_iterations": [],
            "subgoals": [],
            "reflection": {},
            "thoughts": [],
            "context_pills": [],
        }

    monkeypatch.setattr("api.ai._run_follow_up_analysis_core", _fake_run_follow_up_analysis_core)

    async def _run():
        created = await create_ai_run(
            AIRunCreateRequest(
                session_id="sess-policy-001",
                question="继续分析 query-service 慢查询",
                analysis_context={"analysis_type": "log", "service_name": "query-service"},
                runtime_options={
                    "mode": "followup_analysis",
                    "conversation_id": "conv-policy-001",
                    "auto_exec_readonly": False,
                    "history": [{"role": "user", "content": "继续分析 query-service 慢查询"}],
                },
            )
        )
        run_id = created["run"]["run_id"]
        fetched = None
        for _ in range(30):
            await asyncio.sleep(0.01)
            fetched = await get_ai_run(run_id)
            if fetched["run"]["status"] == "blocked":
                break
        events = await get_ai_run_events(run_id, after_seq=0, limit=80)
        return fetched, events

    fetched, events = asyncio.run(_run())

    assert fetched["run"]["status"] == "blocked"
    assert fetched["run"]["summary_json"]["blocked_reason"] == "readonly_auto_exec_disabled"
    assert "只读自动执行" in str(fetched["run"]["summary_json"].get("blocked_reason_detail") or "")
    run_finished_payload = next(item for item in events["events"] if item["event_type"] == "run_finished")["payload"]
    assert run_finished_payload["status"] == "blocked"
    assert run_finished_payload["blocked_reason"] == "readonly_auto_exec_disabled"


def test_create_ai_run_followup_mode_marks_backend_unready_when_templates_cannot_run(monkeypatch):
    runtime_service = _build_runtime_service()
    monkeypatch.setattr("api.ai.get_agent_runtime_service", lambda *_args, **_kwargs: runtime_service)

    async def _fake_run_follow_up_analysis_core(_request, event_callback=None):
        if callable(event_callback):
            await event_callback("token", {"text": "已生成排查命令，但执行网关未就绪。"})
        return {
            "analysis_session_id": "sess-backend-001",
            "conversation_id": "conv-backend-001",
            "analysis_method": "langchain",
            "followup_engine": "langchain",
            "answer": "已生成排查命令，但执行网关未就绪。",
            "references": [],
            "actions": [
                {
                    "id": "tmpl-backend-1",
                    "source": "template_command",
                    "title": "自动补证据命令：kubectl -n islap logs -l app=query-service --since-time=2026-04-12T13:26:14Z --tail=200",
                    "command": "kubectl -n islap logs -l app=query-service --since-time=2026-04-12T13:26:14Z --tail=200",
                    "command_type": "query",
                    "executable": True,
                    "reason": "structured_template_ready_for_auto_exec",
                }
            ],
            "action_observations": [
                {
                    "action_id": "tmpl-backend-1",
                    "status": "skipped",
                    "command": "kubectl -n islap logs -l app=query-service --since-time=2026-04-12T13:26:14Z --tail=200",
                    "reason_code": "backend_unready",
                    "message": "执行网关未就绪，命令未自动执行。",
                }
            ],
            "react_loop": {
                "phase": "replan",
                "plan": {"ready_template_actions": 1},
                "replan": {
                    "needed": True,
                    "items": [{"execution_disposition": "backend_unready"}],
                    "next_actions": ["执行网关未就绪，建议稍后重试"],
                },
                "plan_quality": {"planning_blocked": False},
                "execute": {"observed_actions": 0},
            },
            "react_iterations": [],
            "subgoals": [],
            "reflection": {},
            "thoughts": [],
            "context_pills": [],
        }

    monkeypatch.setattr("api.ai._run_follow_up_analysis_core", _fake_run_follow_up_analysis_core)

    async def _run():
        created = await create_ai_run(
            AIRunCreateRequest(
                session_id="sess-backend-001",
                question="继续分析 query-service 慢查询",
                analysis_context={"analysis_type": "log", "service_name": "query-service"},
                runtime_options={
                    "mode": "followup_analysis",
                    "conversation_id": "conv-backend-001",
                    "auto_exec_readonly": True,
                    "history": [{"role": "user", "content": "继续分析 query-service 慢查询"}],
                },
            )
        )
        run_id = created["run"]["run_id"]
        fetched = None
        for _ in range(30):
            await asyncio.sleep(0.01)
            fetched = await get_ai_run(run_id)
            if fetched["run"]["status"] == "blocked":
                break
        events = await get_ai_run_events(run_id, after_seq=0, limit=80)
        return fetched, events

    fetched, events = asyncio.run(_run())

    assert fetched["run"]["status"] == "blocked"
    assert fetched["run"]["summary_json"]["blocked_reason"] == "backend_unready"
    assert "执行网关" in str(fetched["run"]["summary_json"].get("blocked_reason_detail") or "")
    run_finished_payload = next(item for item in events["events"] if item["event_type"] == "run_finished")["payload"]
    assert run_finished_payload["status"] == "blocked"
    assert run_finished_payload["blocked_reason"] == "backend_unready"


def test_create_ai_run_followup_mode_marks_observation_missing_when_templates_exist_without_observations(monkeypatch):
    runtime_service = _build_runtime_service()
    monkeypatch.setattr("api.ai.get_agent_runtime_service", lambda *_args, **_kwargs: runtime_service)

    async def _fake_run_follow_up_analysis_core(_request, event_callback=None):
        if callable(event_callback):
            await event_callback("token", {"text": "已生成排查命令，但当前还没有观测结果。"})
        return {
            "analysis_session_id": "sess-observe-001",
            "conversation_id": "conv-observe-001",
            "analysis_method": "langchain",
            "followup_engine": "langchain",
            "answer": "已生成排查命令，但当前还没有观测结果。",
            "references": [],
            "actions": [
                {
                    "id": "tmpl-observe-1",
                    "source": "template_command",
                    "title": "自动补证据命令：kubectl -n islap logs -l app=query-service --since-time=2026-04-12T13:26:14Z --tail=200",
                    "command": "kubectl -n islap logs -l app=query-service --since-time=2026-04-12T13:26:14Z --tail=200",
                    "command_type": "query",
                    "executable": True,
                    "reason": "structured_template_ready_for_auto_exec",
                }
            ],
            "action_observations": [],
            "react_loop": {
                "phase": "replan",
                "plan": {"ready_template_actions": 1},
                "replan": {"needed": True, "items": [], "next_actions": ["继续执行模板命令并观察结果"]},
                "plan_quality": {"planning_blocked": False},
                "execute": {"observed_actions": 0},
            },
            "react_iterations": [],
            "subgoals": [],
            "reflection": {},
            "thoughts": [],
            "context_pills": [],
        }

    monkeypatch.setattr("api.ai._run_follow_up_analysis_core", _fake_run_follow_up_analysis_core)

    async def _run():
        created = await create_ai_run(
            AIRunCreateRequest(
                session_id="sess-observe-001",
                question="继续分析 query-service 慢查询",
                analysis_context={"analysis_type": "log", "service_name": "query-service"},
                runtime_options={
                    "mode": "followup_analysis",
                    "conversation_id": "conv-observe-001",
                    "auto_exec_readonly": True,
                    "history": [{"role": "user", "content": "继续分析 query-service 慢查询"}],
                },
            )
        )
        run_id = created["run"]["run_id"]
        fetched = None
        for _ in range(30):
            await asyncio.sleep(0.01)
            fetched = await get_ai_run(run_id)
            if fetched["run"]["status"] == "blocked":
                break
        events = await get_ai_run_events(run_id, after_seq=0, limit=80)
        return fetched, events

    fetched, events = asyncio.run(_run())

    assert fetched["run"]["status"] == "blocked"
    assert fetched["run"]["summary_json"]["blocked_reason"] == "observation_missing"
    assert "尚未获得执行观察结果" in str(fetched["run"]["summary_json"].get("blocked_reason_detail") or "")
    run_finished_payload = next(item for item in events["events"] if item["event_type"] == "run_finished")["payload"]
    assert run_finished_payload["status"] == "blocked"
    assert run_finished_payload["blocked_reason"] == "observation_missing"


def test_create_ai_run_followup_mode_does_not_leak_blocked_reason_when_completed(monkeypatch):
    runtime_service = _build_runtime_service()
    monkeypatch.setattr("api.ai.get_agent_runtime_service", lambda *_args, **_kwargs: runtime_service)

    async def _fake_run_follow_up_analysis_core(_request, event_callback=None):
        if callable(event_callback):
            await event_callback("token", {"text": "证据已补齐，本轮完成。"})
        return {
            "analysis_session_id": "sess-complete-001",
            "conversation_id": "conv-complete-001",
            "analysis_method": "langchain",
            "followup_engine": "langchain",
            "answer": "证据已补齐，本轮完成。",
            "references": [],
            "actions": [
                {
                    "id": "tmpl-complete-1",
                    "source": "template_command",
                    "title": "自动补证据命令",
                    "command": "kubectl -n islap logs -l app=query-service --since-time=2026-04-12T13:26:14Z --tail=200",
                    "command_type": "query",
                    "executable": True,
                    "reason": "structured_template_ready_for_auto_exec",
                }
            ],
            "action_observations": [
                {
                    "action_id": "tmpl-complete-1",
                    "status": "executed",
                    "command": "kubectl -n islap logs -l app=query-service --since-time=2026-04-12T13:26:14Z --tail=200",
                    "exit_code": 0,
                    "stdout": "ok",
                }
            ],
            "react_loop": {
                "phase": "finalized",
                "plan": {"ready_template_actions": 1},
                "execute": {"observed_actions": 1, "executed_success": 1, "executed_failed": 0},
                "observe": {"coverage": 1.0, "evidence_coverage": 1.0, "confidence": 0.95, "final_confidence": 0.95},
                "replan": {"needed": False, "items": [], "next_actions": []},
                "plan_quality": {"planning_blocked": False},
            },
            "react_iterations": [],
            "subgoals": [],
            "reflection": {},
            "thoughts": [],
            "context_pills": [],
        }

    monkeypatch.setattr("api.ai._run_follow_up_analysis_core", _fake_run_follow_up_analysis_core)

    async def _run():
        created = await create_ai_run(
            AIRunCreateRequest(
                session_id="sess-complete-001",
                question="继续分析 query-service 慢查询",
                analysis_context={"analysis_type": "log", "service_name": "query-service"},
                runtime_options={
                    "mode": "followup_analysis",
                    "conversation_id": "conv-complete-001",
                    "auto_exec_readonly": False,
                    "history": [{"role": "user", "content": "继续分析 query-service 慢查询"}],
                },
            )
        )
        run_id = created["run"]["run_id"]
        fetched = None
        for _ in range(30):
            await asyncio.sleep(0.01)
            fetched = await get_ai_run(run_id)
            if fetched["run"]["status"] == "completed":
                break
        events = await get_ai_run_events(run_id, after_seq=0, limit=80)
        return fetched, events

    fetched, events = asyncio.run(_run())

    assert fetched["run"]["status"] == "completed"
    assert fetched["run"]["summary_json"].get("blocked_reason") in {None, ""}
    assert fetched["run"]["summary_json"].get("blocked_reason_detail") in {None, ""}
    run_finished_payload = next(item for item in events["events"] if item["event_type"] == "run_finished")["payload"]
    assert run_finished_payload["status"] == "completed"
    assert run_finished_payload.get("blocked_reason") in {None, ""}


def test_create_ai_run_followup_mode_templates_prevent_planning_block_when_observed(monkeypatch):
    runtime_service = _build_runtime_service()
    monkeypatch.setattr("api.ai.get_agent_runtime_service", lambda *_args, **_kwargs: runtime_service)

    async def _fake_run_follow_up_analysis_core(_request, event_callback=None):
        if callable(event_callback):
            await event_callback("token", {"text": "模板命令已执行，当前无需再按 planning_incomplete 阻断。"})
        return {
            "analysis_session_id": "sess-template-plan-001",
            "conversation_id": "conv-template-plan-001",
            "analysis_method": "langchain",
            "followup_engine": "langchain",
            "answer": "模板命令已执行，当前无需再按 planning_incomplete 阻断。",
            "references": [],
            "actions": [
                {
                    "id": "tmpl-plan-1",
                    "source": "template_command",
                    "title": "自动补证据命令",
                    "command": "kubectl -n islap logs -l app=query-service --since-time=2026-04-12T13:26:14Z --tail=200",
                    "command_type": "query",
                    "executable": True,
                    "reason": "structured_template_ready_for_auto_exec",
                }
            ],
            "action_observations": [
                {
                    "action_id": "tmpl-plan-1",
                    "status": "executed",
                    "command": "kubectl -n islap logs -l app=query-service --since-time=2026-04-12T13:26:14Z --tail=200",
                    "exit_code": 0,
                    "stdout": "ok",
                }
            ],
            "react_loop": {
                "phase": "finalized",
                "plan": {"ready_template_actions": 1},
                "execute": {"observed_actions": 1, "executed_success": 1, "executed_failed": 0},
                "observe": {"coverage": 1.0, "evidence_coverage": 1.0, "confidence": 0.8, "final_confidence": 0.82},
                "replan": {"needed": False, "items": [], "next_actions": []},
                "plan_quality": {"planning_blocked": True, "planning_blocked_reason": "旧计划大多不可执行"},
            },
            "react_iterations": [],
            "subgoals": [],
            "reflection": {},
            "thoughts": [],
            "context_pills": [],
        }

    monkeypatch.setattr("api.ai._run_follow_up_analysis_core", _fake_run_follow_up_analysis_core)

    async def _run():
        created = await create_ai_run(
            AIRunCreateRequest(
                session_id="sess-template-plan-001",
                question="继续分析 query-service 慢查询",
                analysis_context={"analysis_type": "log", "service_name": "query-service"},
                runtime_options={
                    "mode": "followup_analysis",
                    "conversation_id": "conv-template-plan-001",
                    "auto_exec_readonly": True,
                    "history": [{"role": "user", "content": "继续分析 query-service 慢查询"}],
                },
            )
        )
        run_id = created["run"]["run_id"]
        fetched = None
        for _ in range(30):
            await asyncio.sleep(0.01)
            fetched = await get_ai_run(run_id)
            if fetched["run"]["status"] in {"completed", "blocked"}:
                break
        return fetched

    fetched = asyncio.run(_run())

    assert fetched["run"]["status"] == "completed"
    assert fetched["run"]["summary_json"].get("blocked_reason") in {None, ""}
    assert fetched["run"]["summary_json"]["gate_decision"]["reason"] == "ok"


def test_create_ai_run_followup_mode_softens_answer_when_evidence_is_weak(monkeypatch):
    runtime_service = _build_runtime_service()
    monkeypatch.setattr("api.ai.get_agent_runtime_service", lambda *_args, **_kwargs: runtime_service)

    async def _fake_run_follow_up_analysis_core(_request, event_callback=None):
        if callable(event_callback):
            await event_callback("token", {"text": "已初步定位问题。"})
        return {
            "analysis_session_id": "sess-followup-soften-001",
            "conversation_id": "conv-followup-soften-001",
            "analysis_method": "langchain",
            "followup_engine": "langchain",
            "answer": "结论：这是一个重复出现的系统性性能问题，而非偶发事件。\n根因分析：query-service 频繁触发 system.tables 元数据查询。",
            "fault_summary": "query-service 已确认存在系统性慢查询问题",
            "references": [],
            "actions": [{"id": "act-soften-001", "title": "补查日志"}],
            "action_observations": [],
            "react_loop": {
                "phase": "replan",
                "execute": {"observed_actions": 0, "executed_success": 0, "executed_failed": 0},
                "observe": {"coverage": 0.0, "evidence_coverage": 0.0, "confidence": 0.25, "final_confidence": 0.28},
                "replan": {"needed": True, "items": [], "next_actions": ["继续补证据"]},
            },
            "react_iterations": [],
            "subgoals": [],
            "reflection": {},
            "thoughts": [],
            "context_pills": [],
        }

    monkeypatch.setattr("api.ai._run_follow_up_analysis_core", _fake_run_follow_up_analysis_core)

    async def _run():
        created = await create_ai_run(
            AIRunCreateRequest(
                session_id="sess-followup-soften-001",
                question="继续分析 query-service 慢查询",
                analysis_context={"analysis_type": "log", "service_name": "query-service"},
                runtime_options={
                    "mode": "followup_analysis",
                    "conversation_id": "conv-followup-soften-001",
                    "history": [{"role": "user", "content": "继续分析 query-service 慢查询"}],
                },
            )
        )
        run_id = created["run"]["run_id"]
        fetched = None
        for _ in range(30):
            await asyncio.sleep(0.01)
            fetched = await get_ai_run(run_id)
            if fetched["run"]["status"] == "blocked":
                break
        events = await get_ai_run_events(run_id, after_seq=0, limit=80)
        return fetched, events

    fetched, events = asyncio.run(_run())

    final_event = next(item for item in events["events"] if item["event_type"] == "assistant_message_finalized")
    content = final_event["payload"]["content"]
    assert "当前仅为待验证判断" in content
    assert "初步判断（待验证）" in content
    assert "待验证假设：" in content
    assert "仍需继续验证" in content
    assert "当前仅为待验证判断" in fetched["run"]["summary_json"]["fault_summary"]


def test_create_ai_run_followup_mode_blocks_when_evidence_incomplete(monkeypatch):
    runtime_service = _build_runtime_service()
    monkeypatch.setattr("api.ai.get_agent_runtime_service", lambda *_args, **_kwargs: runtime_service)

    async def _fake_run_follow_up_analysis_core(_request, event_callback=None):
        if callable(event_callback):
            await event_callback("token", {"text": "证据仍不充分。"})
        return {
            "analysis_session_id": "sess-followup-evidence-001",
            "conversation_id": "conv-followup-evidence-001",
            "analysis_method": "langchain",
            "followup_engine": "langchain",
            "answer": "当前证据不完整，需要继续采证。",
            "references": [],
            "actions": [{"id": "act-evidence-001", "title": "补齐慢查询证据"}],
            "action_observations": [],
            "react_loop": {
                "phase": "finalized",
                "observe": {"coverage": 0.33, "confidence": 0.42},
                "replan": {"needed": False, "items": [], "next_actions": []},
            },
            "react_iterations": [],
            "subgoals": [
                {"id": "sg-root", "title": "定位根因", "status": "needs_data", "reason": "缺少 EXPLAIN 证据"},
            ],
            "reflection": {"gaps": ["缺少 EXPLAIN 证据"]},
            "thoughts": [],
            "context_pills": [],
        }

    monkeypatch.setattr("api.ai._run_follow_up_analysis_core", _fake_run_follow_up_analysis_core)

    async def _run():
        created = await create_ai_run(
            AIRunCreateRequest(
                session_id="sess-followup-evidence-001",
                question="继续分析 query-service 慢查询",
                analysis_context={"analysis_type": "log", "service_name": "query-service"},
                runtime_options={
                    "mode": "followup_analysis",
                    "conversation_id": "conv-followup-evidence-001",
                    "history": [{"role": "user", "content": "继续分析 query-service 慢查询"}],
                },
            )
        )
        run_id = created["run"]["run_id"]
        fetched = None
        for _ in range(30):
            await asyncio.sleep(0.01)
            fetched = await get_ai_run(run_id)
            if fetched["run"]["status"] == "blocked":
                break
        events = await get_ai_run_events(run_id, after_seq=0, limit=80)
        return fetched, events

    fetched, events = asyncio.run(_run())

    assert fetched["run"]["status"] == "blocked"
    assert fetched["run"]["summary_json"]["blocked_reason"] == "evidence_incomplete"
    assert fetched["run"]["summary_json"]["evidence_needs_data_subgoal"] is True
    assert float(fetched["run"]["summary_json"]["evidence_coverage"]) == pytest.approx(0.33, rel=1e-3)
    assert fetched["run"]["summary_json"]["diagnosis_status"] == "blocked"
    gate_decision = fetched["run"]["summary_json"].get("gate_decision") or {}
    assert gate_decision.get("result") == "blocked"
    assert gate_decision.get("reason") == "evidence_incomplete"
    assert float(((gate_decision.get("metrics") or {}).get("evidence_coverage") or -1.0)) == pytest.approx(0.33, rel=1e-3)
    run_finished_payload = next(item for item in events["events"] if item["event_type"] == "run_finished")["payload"]
    assert run_finished_payload["status"] == "blocked"
    assert run_finished_payload["blocked_reason"] == "evidence_incomplete"


def test_create_ai_run_followup_mode_blocks_when_final_confidence_below_threshold(monkeypatch):
    runtime_service = _build_runtime_service()
    monkeypatch.setattr("api.ai.get_agent_runtime_service", lambda *_args, **_kwargs: runtime_service)
    monkeypatch.setenv("AI_RUNTIME_MIN_FINAL_CONFIDENCE", "0.55")

    async def _fake_run_follow_up_analysis_core(_request, event_callback=None):
        if callable(event_callback):
            await event_callback("token", {"text": "结论可信度不足。"})
        return {
            "analysis_session_id": "sess-followup-confidence-001",
            "conversation_id": "conv-followup-confidence-001",
            "analysis_method": "langchain",
            "followup_engine": "langchain",
            "answer": "当前证据有一定覆盖，但结论置信度偏低。",
            "references": [],
            "actions": [{"id": "act-confidence-001", "title": "补齐置信度证据"}],
            "action_observations": [],
            "react_loop": {
                "phase": "finalized",
                "observe": {
                    "coverage": 0.8,
                    "evidence_coverage": 0.8,
                    "confidence": 0.5,
                    "final_confidence": 0.4,
                },
                "replan": {"needed": False, "items": [], "next_actions": []},
            },
            "react_iterations": [],
            "subgoals": [{"id": "sg-1", "title": "定位根因", "status": "done"}],
            "reflection": {"gaps": []},
            "thoughts": [],
            "context_pills": [],
        }

    monkeypatch.setattr("api.ai._run_follow_up_analysis_core", _fake_run_follow_up_analysis_core)

    async def _run():
        created = await create_ai_run(
            AIRunCreateRequest(
                session_id="sess-followup-confidence-001",
                question="继续分析 query-service 慢查询",
                analysis_context={"analysis_type": "log", "service_name": "query-service"},
                runtime_options={
                    "mode": "followup_analysis",
                    "conversation_id": "conv-followup-confidence-001",
                    "history": [{"role": "user", "content": "继续分析 query-service 慢查询"}],
                },
            )
        )
        run_id = created["run"]["run_id"]
        fetched = None
        for _ in range(30):
            await asyncio.sleep(0.01)
            fetched = await get_ai_run(run_id)
            if fetched["run"]["status"] == "blocked":
                break
        events = await get_ai_run_events(run_id, after_seq=0, limit=80)
        return fetched, events

    fetched, events = asyncio.run(_run())
    assert fetched["run"]["status"] == "blocked"
    assert fetched["run"]["summary_json"]["blocked_reason"] == "evidence_incomplete"
    assert float(fetched["run"]["summary_json"]["final_confidence"]) == pytest.approx(0.4, rel=1e-3)
    assert float(fetched["run"]["summary_json"]["evidence_confidence_threshold"]) == pytest.approx(0.55, rel=1e-3)
    gate_decision = fetched["run"]["summary_json"].get("gate_decision") or {}
    assert gate_decision.get("result") == "blocked"
    assert gate_decision.get("reason") == "evidence_incomplete"
    thresholds = gate_decision.get("thresholds") or {}
    assert float(thresholds.get("min_final_confidence") or 0.0) == pytest.approx(0.55, rel=1e-3)
    run_finished_payload = next(item for item in events["events"] if item["event_type"] == "run_finished")["payload"]
    assert run_finished_payload["status"] == "blocked"
    assert run_finished_payload["blocked_reason"] == "evidence_incomplete"


def test_create_ai_run_followup_mode_blocks_when_answer_declares_insufficient_evidence(monkeypatch):
    runtime_service = _build_runtime_service()
    monkeypatch.setattr("api.ai.get_agent_runtime_service", lambda *_args, **_kwargs: runtime_service)
    monkeypatch.setenv("AI_RUNTIME_MIN_FINAL_CONFIDENCE", "0.0")

    async def _fake_run_follow_up_analysis_core(_request, event_callback=None):
        if callable(event_callback):
            await event_callback("token", {"text": "证据不足。"})
        return {
            "analysis_session_id": "sess-followup-conflict-001",
            "conversation_id": "conv-followup-conflict-001",
            "analysis_method": "langchain",
            "followup_engine": "langchain",
            "answer": "当前证据不足以定位根本原因，请补充更多日志。",
            "references": [],
            "actions": [{"id": "act-conflict-001", "title": "补证据"}],
            "action_observations": [],
            "react_loop": {
                "phase": "finalized",
                "observe": {
                    "coverage": 1.0,
                    "evidence_coverage": 1.0,
                    "confidence": 0.9,
                    "final_confidence": 0.9,
                    "missing_evidence_slots": [],
                },
                "replan": {"needed": False, "items": [], "next_actions": []},
            },
            "react_iterations": [],
            "subgoals": [{"id": "sg-1", "title": "定位根因", "status": "done"}],
            "reflection": {"gaps": []},
            "thoughts": [],
            "context_pills": [],
        }

    monkeypatch.setattr("api.ai._run_follow_up_analysis_core", _fake_run_follow_up_analysis_core)

    async def _run():
        created = await create_ai_run(
            AIRunCreateRequest(
                session_id="sess-followup-conflict-001",
                question="继续分析 query-service 慢查询",
                analysis_context={"analysis_type": "log", "service_name": "query-service"},
                runtime_options={
                    "mode": "followup_analysis",
                    "conversation_id": "conv-followup-conflict-001",
                    "history": [{"role": "user", "content": "继续分析 query-service 慢查询"}],
                },
            )
        )
        run_id = created["run"]["run_id"]
        fetched = None
        for _ in range(30):
            await asyncio.sleep(0.01)
            fetched = await get_ai_run(run_id)
            if fetched["run"]["status"] == "blocked":
                break
        events = await get_ai_run_events(run_id, after_seq=0, limit=80)
        return fetched, events

    fetched, events = asyncio.run(_run())
    assert fetched["run"]["status"] == "blocked"
    assert fetched["run"]["summary_json"]["blocked_reason"] == "evidence_incomplete"
    gate_decision = fetched["run"]["summary_json"].get("gate_decision") or {}
    assert gate_decision.get("result") == "blocked"
    assert gate_decision.get("reason") == "evidence_incomplete"
    metrics = gate_decision.get("metrics") or {}
    assert metrics.get("answer_declares_insufficient") is True
    assert (gate_decision.get("gate_conflict_reasons") or [])[0] == "answer_declares_insufficient_evidence"
    run_finished_payload = next(item for item in events["events"] if item["event_type"] == "run_finished")["payload"]
    assert run_finished_payload["status"] == "blocked"
    assert run_finished_payload["blocked_reason"] == "evidence_incomplete"


def test_create_ai_run_followup_mode_pauses_on_approval_required_observation(monkeypatch):
    runtime_service = _build_runtime_service()
    monkeypatch.setattr("api.ai.get_agent_runtime_service", lambda *_args, **_kwargs: runtime_service)

    async def _fake_run_follow_up_analysis_core(_request, event_callback=None):
        if callable(event_callback):
            await event_callback(
                "observation",
                {
                    "action_id": "act-approval-runtime-001",
                    "command": "kubectl -n islap exec pod/clickhouse-0 -- clickhouse-client --query \"DESCRIBE TABLE logs.traces\"",
                    "purpose": "获取表字段、类型、默认表达式等信息",
                    "status": "confirmation_required",
                    "message": "命令不在免审批白名单模板内，需人工确认后执行。",
                    "command_type": "query",
                    "risk_level": "high",
                    "requires_confirmation": True,
                    "requires_elevation": False,
                    "confirmation_ticket": "exec-ticket-runtime-001",
                    "command_family": "kubernetes",
                    "approval_policy": "confirmation_required",
                },
            )
        return {
            "analysis_session_id": "sess-followup-approval-001",
            "conversation_id": "conv-followup-approval-001",
            "analysis_method": "langchain",
            "followup_engine": "langchain",
            "answer": "should not be finalized while waiting approval",
            "references": [],
            "actions": [],
            "action_observations": [],
            "react_loop": {},
            "react_iterations": [],
            "subgoals": [],
            "reflection": {},
            "thoughts": [],
            "context_pills": [],
        }

    monkeypatch.setattr("api.ai._run_follow_up_analysis_core", _fake_run_follow_up_analysis_core)

    async def _run():
        created = await create_ai_run(
            AIRunCreateRequest(
                session_id="sess-followup-approval-001",
                question="继续分析并执行命令",
                analysis_context={"analysis_type": "log", "service_name": "query-service"},
                runtime_options={
                    "mode": "followup_analysis",
                    "conversation_id": "conv-followup-approval-001",
                    "history": [{"role": "user", "content": "继续分析并执行命令"}],
                },
            )
        )
        run_id = created["run"]["run_id"]
        fetched = None
        for _ in range(20):
            await asyncio.sleep(0.01)
            fetched = await get_ai_run(run_id)
            if fetched["run"]["status"] == "waiting_approval":
                break
        events = await get_ai_run_events(run_id, after_seq=0, limit=80)
        return fetched, events

    fetched, events = asyncio.run(_run())

    assert fetched["run"]["status"] == "waiting_approval"
    event_types = [item["event_type"] for item in events["events"]]
    assert "approval_required" in event_types
    assert "action_waiting_approval" in event_types
    assert "run_finished" not in event_types


def test_create_ai_run_followup_mode_keeps_waiting_approval_when_core_returns_without_pause(monkeypatch):
    runtime_service = _build_runtime_service()
    monkeypatch.setattr("api.ai.get_agent_runtime_service", lambda *_args, **_kwargs: runtime_service)

    async def _fake_run_follow_up_analysis_core(_request, event_callback=None):
        del event_callback
        run_ids = list(runtime_service.store._runs.keys())  # noqa: SLF001 - test-only inspection
        assert run_ids
        run_id = run_ids[-1]
        runtime_service.request_approval(
            run_id,
            approval_id="apr-runtime-guard-001",
            title="执行 kubectl 查询",
            reason="命令需要审批",
            command="kubectl -n islap get pods",
            purpose="查询 pod 状态",
            command_type="query",
            risk_level="high",
            requires_confirmation=True,
            requires_elevation=False,
        )
        return {
            "analysis_session_id": "sess-followup-guard-001",
            "conversation_id": "conv-followup-guard-001",
            "analysis_method": "langchain",
            "followup_engine": "langchain",
            "answer": "guard should keep waiting_approval",
            "references": [],
            "actions": [],
            "action_observations": [],
            "react_loop": {},
            "react_iterations": [],
            "subgoals": [],
            "reflection": {},
            "thoughts": [],
            "context_pills": [],
        }

    monkeypatch.setattr("api.ai._run_follow_up_analysis_core", _fake_run_follow_up_analysis_core)

    async def _run():
        created = await create_ai_run(
            AIRunCreateRequest(
                session_id="sess-followup-guard-001",
                question="继续分析并执行命令",
                analysis_context={"analysis_type": "log", "service_name": "query-service"},
                runtime_options={
                    "mode": "followup_analysis",
                    "conversation_id": "conv-followup-guard-001",
                    "history": [{"role": "user", "content": "继续分析并执行命令"}],
                },
            )
        )
        run_id = created["run"]["run_id"]
        fetched = None
        for _ in range(30):
            await asyncio.sleep(0.01)
            fetched = await get_ai_run(run_id)
            if fetched["run"]["status"] == "waiting_approval":
                break
        events = await get_ai_run_events(run_id, after_seq=0, limit=80)
        return fetched, events

    fetched, events = asyncio.run(_run())

    assert fetched["run"]["status"] == "waiting_approval"
    assert fetched["run"]["summary_json"]["pending_approval_count"] == 1
    event_types = [item["event_type"] for item in events["events"]]
    assert "approval_required" in event_types
    assert "run_finished" not in event_types


def test_emit_followup_runtime_event_pauses_when_pending_action_exists(monkeypatch):
    runtime_service = _build_runtime_service()
    monkeypatch.setattr("api.ai.get_agent_runtime_service", lambda *_args, **_kwargs: runtime_service)

    run = runtime_service.create_run(
        session_id="sess-runtime-pause-001",
        question="排查 clickhouse 元数据",
        analysis_context={"analysis_type": "trace", "service_name": "query-service"},
        runtime_options={"mode": "followup_analysis"},
    )
    runtime_service.request_approval(
        run.run_id,
        approval_id="apr-runtime-pause-001",
        title="执行 kubectl 查询",
        reason="命令需要审批",
        command="kubectl -n islap get pods",
        purpose="查询 pod 状态",
        command_type="query",
        risk_level="high",
        requires_confirmation=True,
        requires_elevation=False,
    )
    before_count = len(runtime_service.list_events(run.run_id, after_seq=0, limit=200))

    async def _run():
        with pytest.raises(_RuntimePauseForPendingAction):
            await _emit_followup_runtime_event(
                runtime_service,
                run.run_id,
                "observation",
                {
                    "action_id": "act-runtime-pause-001",
                    "command": "kubectl -n islap get svc",
                    "status": "running",
                    "command_type": "query",
                },
                {"tool_call_ids": {}, "started_tool_calls": set()},
            )

    asyncio.run(_run())

    events = runtime_service.list_events(run.run_id, after_seq=0, limit=400)
    assert len(events) == before_count
    assert all(item.event_type != "tool_call_started" for item in events)


def test_emit_followup_runtime_event_preserves_pending_command_spec_on_confirmation_gate(monkeypatch):
    runtime_service = _build_runtime_service()
    monkeypatch.setattr("api.ai.get_agent_runtime_service", lambda *_args, **_kwargs: runtime_service)

    run = runtime_service.create_run(
        session_id="sess-runtime-pending-merge-001",
        question="排查 clickhouse 表结构",
        analysis_context={"analysis_type": "trace", "service_name": "query-service"},
        runtime_options={"mode": "followup_analysis"},
    )
    existing_spec = _generic_exec_spec("kubectl get pods -n islap")
    runtime_service._update_run_summary(  # noqa: SLF001
        run,
        pending_command_request={
            "tool_call_id": "tool-existing-001",
            "action_id": "act-existing-001",
            "command": "kubectl get pods -n islap",
            "command_spec": existing_spec,
            "purpose": "检查 pod 状态",
            "title": "检查 pod 状态",
            "tool_name": "command.exec",
        },
    )

    async def _run():
        with pytest.raises(_RuntimePauseForPendingAction):
            await _emit_followup_runtime_event(
                runtime_service,
                run.run_id,
                "observation",
                {
                    "action_id": "act-existing-001",
                    "command": "kubectl get pods -n islap",
                    "purpose": "检查 pod 状态",
                    "status": "confirmation_required",
                    "message": "命令需要人工确认后执行",
                    "command_type": "query",
                    "risk_level": "high",
                    "requires_confirmation": True,
                    "requires_elevation": False,
                    "confirmation_ticket": "ticket-merge-001",
                },
                {"tool_call_ids": {}, "started_tool_calls": set()},
            )

    asyncio.run(_run())

    snapshot = runtime_service.get_run(run.run_id)
    summary = snapshot.summary_json if isinstance(snapshot.summary_json, dict) else {}
    pending = summary.get("pending_command_request") if isinstance(summary.get("pending_command_request"), dict) else {}
    assert pending.get("confirmation_ticket") == "ticket-merge-001"
    assert pending.get("command_spec") == existing_spec


def test_emit_followup_runtime_event_sets_pending_command_spec_from_confirmation_payload(monkeypatch):
    runtime_service = _build_runtime_service()
    monkeypatch.setattr("api.ai.get_agent_runtime_service", lambda *_args, **_kwargs: runtime_service)

    run = runtime_service.create_run(
        session_id="sess-runtime-pending-create-001",
        question="审批前保留结构化命令",
        analysis_context={"analysis_type": "trace", "service_name": "query-service"},
        runtime_options={"mode": "followup_analysis"},
    )
    payload_spec = _generic_exec_spec("kubectl get pods -n islap")

    async def _run():
        with pytest.raises(_RuntimePauseForPendingAction):
            await _emit_followup_runtime_event(
                runtime_service,
                run.run_id,
                "observation",
                {
                    "action_id": "act-create-001",
                    "command": "kubectl get pods -n islap",
                    "purpose": "检查 pod 状态",
                    "status": "confirmation_required",
                    "message": "命令需要人工确认后执行",
                    "command_type": "query",
                    "risk_level": "low",
                    "requires_confirmation": True,
                    "requires_elevation": False,
                    "confirmation_ticket": "ticket-create-001",
                    "command_spec": payload_spec,
                },
                {"tool_call_ids": {}, "started_tool_calls": set()},
            )

    asyncio.run(_run())

    snapshot = runtime_service.get_run(run.run_id)
    summary = snapshot.summary_json if isinstance(snapshot.summary_json, dict) else {}
    pending = summary.get("pending_command_request") if isinstance(summary.get("pending_command_request"), dict) else {}
    assert pending.get("confirmation_ticket") == "ticket-create-001"
    assert pending.get("command_spec") == payload_spec


def test_emit_followup_runtime_event_dedupes_terminal_observation_and_preserves_reason_code(monkeypatch):
    runtime_service = _build_runtime_service()
    monkeypatch.setattr("api.ai.get_agent_runtime_service", lambda *_args, **_kwargs: runtime_service)

    run = runtime_service.create_run(
        session_id="sess-runtime-event-dedupe-001",
        question="检查 observation 去重",
        analysis_context={"analysis_type": "trace", "service_name": "query-service"},
        runtime_options={"mode": "followup_analysis"},
    )
    state = {"tool_call_ids": {}, "started_tool_calls": set(), "finished_tool_calls": set()}

    async def _run():
        payload = {
            "action_id": "act-ob-001",
            "command": "kubectl -n islap get pods",
            "status": "skipped",
            "message": "同一 run 已执行过该命令，跳过重复执行。",
            "reason_code": "duplicate_skipped",
            "command_type": "query",
            "risk_level": "low",
        }
        await _emit_followup_runtime_event(runtime_service, run.run_id, "observation", payload, state)
        await _emit_followup_runtime_event(runtime_service, run.run_id, "observation", payload, state)

    asyncio.run(_run())

    events = runtime_service.list_events(run.run_id, after_seq=0, limit=400)
    skipped_events = [item for item in events if item.event_type == "tool_call_skipped_duplicate"]
    assert len(skipped_events) == 1
    assert skipped_events[0].payload.get("reason_code") == "duplicate_skipped"
    assert skipped_events[0].payload.get("action_id") == "act-ob-001"


def test_emit_followup_runtime_event_skips_duplicate_approval_when_command_already_completed(monkeypatch):
    runtime_service = _build_runtime_service()
    monkeypatch.setattr("api.ai.get_agent_runtime_service", lambda *_args, **_kwargs: runtime_service)

    run = runtime_service.create_run(
        session_id="sess-runtime-approval-dedupe-001",
        question="检查重复审批去重",
        analysis_context={"analysis_type": "log", "service_name": "query-service"},
        runtime_options={"mode": "followup_analysis"},
    )
    runtime_service._update_run_summary(  # noqa: SLF001
        run,
        command_run_index={
            "approval-dedupe-key-001": {
                "status": "completed",
                "command_run_id": "cmdrun-existing-001",
                "action_id": "act-approval-runtime-001",
                "command": "kubectl -n islap exec pod/clickhouse-0 -- clickhouse-client --query \"DESCRIBE TABLE logs.traces\"",
                "purpose": "获取表字段、类型、默认表达式等信息",
                "exit_code": 0,
            },
        },
    )
    state = {"tool_call_ids": {}, "started_tool_calls": set(), "finished_tool_calls": set()}

    async def _run():
        await _emit_followup_runtime_event(
            runtime_service,
            run.run_id,
            "observation",
            {
                "action_id": "act-approval-runtime-001",
                "command": "kubectl -n islap exec pod/clickhouse-0 -- clickhouse-client --query \"DESCRIBE TABLE logs.traces\"",
                "purpose": "获取表字段、类型、默认表达式等信息",
                "status": "confirmation_required",
                "message": "命令不在免审批白名单模板内，需人工确认后执行。",
                "command_type": "query",
                "risk_level": "high",
                "requires_confirmation": True,
                "requires_elevation": False,
                "confirmation_ticket": "exec-ticket-runtime-dup-001",
                "command_family": "kubernetes",
                "approval_policy": "confirmation_required",
            },
            state,
        )

    asyncio.run(_run())

    events = runtime_service.list_events(run.run_id, after_seq=0, limit=80)
    event_types = [item.event_type for item in events]
    assert "approval_required" not in event_types
    skipped_events = [item for item in events if item.event_type == "tool_call_skipped_duplicate"]
    assert len(skipped_events) == 1
    assert skipped_events[0].payload.get("command_run_id") == "cmdrun-existing-001"
    assert skipped_events[0].payload.get("reason_code") == "duplicate_skipped"


def test_finalize_assistant_message_skips_when_waiting_approval():
    runtime_service = _build_runtime_service()
    run = runtime_service.create_run(
        session_id="sess-runtime-finalize-guard-001",
        question="排查 clickhouse 表结构",
        analysis_context={"analysis_type": "trace", "service_name": "query-service"},
        runtime_options={"mode": "followup_analysis"},
    )
    runtime_service.request_approval(
        run.run_id,
        approval_id="apr-runtime-finalize-guard-001",
        title="执行 kubectl 查询",
        reason="命令需要审批",
        command="kubectl -n islap get pods",
        purpose="查询 pod 状态",
        command_type="query",
        risk_level="high",
        requires_confirmation=True,
        requires_elevation=False,
    )

    event = runtime_service.finalize_assistant_message(
        run.run_id,
        content="不应在 waiting_approval 状态写入最终答案",
    )

    assert event is None
    events = runtime_service.list_events(run.run_id, after_seq=0, limit=400)
    assert all(item.event_type != "assistant_message_finalized" for item in events)


def test_build_followup_request_from_ai_run_falls_back_to_input_json_question():
    run = SimpleNamespace(
        question="",
        session_id="sess-runtime-fallback-001",
        context_json={"analysis_type": "trace", "service_name": "query-service"},
        input_json={"question": "请继续排查 logs.traces DDL"},
    )

    request = _build_followup_request_from_ai_run(
        run,
        {
            "mode": "followup_analysis",
            "conversation_id": "conv-runtime-fallback-001",
            "history": [{"role": "user", "content": "请继续排查"}],
        },
    )

    assert request.question == "请继续排查 logs.traces DDL"


def test_build_followup_request_from_ai_run_uses_run_conversation_id_when_runtime_options_missing():
    run = SimpleNamespace(
        question="继续分析 query-service",
        session_id="sess-runtime-conversation-001",
        conversation_id="conv-runtime-stable-001",
        context_json={"analysis_type": "trace", "service_name": "query-service"},
        input_json={"question": "继续分析 query-service"},
    )

    request = _build_followup_request_from_ai_run(
        run,
        {
            "mode": "followup_analysis",
            "history": [{"role": "user", "content": "继续分析"}],
        },
    )

    assert request.conversation_id == "conv-runtime-stable-001"


def test_cancel_ai_run_marks_run_cancelled(monkeypatch):
    runtime_service = _build_runtime_service()
    monkeypatch.setattr("api.ai.get_agent_runtime_service", lambda *_args, **_kwargs: runtime_service)

    async def _run():
        created = await create_ai_run(
            AIRunCreateRequest(
                session_id="sess-003",
                question="排查 topology-service",
                analysis_context={"analysis_type": "trace", "service_name": "topology-service"},
            )
        )
        run_id = created["run"]["run_id"]
        cancelled = await cancel_ai_run(run_id, AIRunCancelRequest(reason="user_cancelled"))
        fetched = await get_ai_run(run_id)
        events = await get_ai_run_events(run_id, after_seq=0, limit=20)
        return cancelled, fetched, events

    cancelled, fetched, events = asyncio.run(_run())

    assert cancelled["run"]["status"] == "cancelled"
    assert fetched["run"]["status"] == "cancelled"
    assert any(item["event_type"] == "run_cancelled" for item in events["events"])
    assert any(item["event_type"] == "run_status_changed" for item in events["events"])


def test_approve_ai_run_resumes_waiting_approval_run(monkeypatch):
    runtime_service = _build_runtime_service()
    monkeypatch.setattr("api.ai.get_agent_runtime_service", lambda *_args, **_kwargs: runtime_service)

    async def _run():
        created = await create_ai_run(
            AIRunCreateRequest(
                session_id="sess-approval-001",
                question="执行修复前需要确认",
                analysis_context={"analysis_type": "log", "service_name": "query-service"},
            )
        )
        run_id = created["run"]["run_id"]
        runtime_service.request_approval(
            run_id,
            approval_id="apr-001",
            title="执行 kubectl rollout restart",
            reason="该动作会影响目标服务实例",
            command="kubectl rollout restart deployment/query-service",
            requires_confirmation=True,
            requires_elevation=True,
        )
        approved = await approve_ai_run(
            run_id,
            AIRunApproveRequest(
                approval_id="apr-001",
                decision="approved",
                comment="允许继续",
                confirmed=True,
                elevated=True,
            ),
        )
        fetched = await get_ai_run(run_id)
        events = await get_ai_run_events(run_id, after_seq=0, limit=20)
        return approved, fetched, events

    approved, fetched, events = asyncio.run(_run())

    assert approved["run"]["status"] == "running"
    assert approved["approval"]["decision"] == "approved"
    approval_feedback = fetched["run"]["context_json"]["approval_feedback"]
    assert approval_feedback[-1]["comment"] == "允许继续"
    assert fetched["run"]["summary_json"]["pending_approval_count"] == 0
    event_types = [item["event_type"] for item in events["events"]]
    assert "approval_required" in event_types
    assert "approval_resolved" in event_types
    assert event_types.count("run_status_changed") >= 2


def test_approve_ai_run_is_idempotent_after_first_resolution(monkeypatch):
    runtime_service = _build_runtime_service()
    monkeypatch.setattr("api.ai.get_agent_runtime_service", lambda *_args, **_kwargs: runtime_service)
    schedule_calls = []

    def _fake_schedule(runtime_service_arg, run_id: str, *, wait_for_active_command: bool):
        assert runtime_service_arg is runtime_service
        schedule_calls.append((run_id, wait_for_active_command))

    monkeypatch.setattr("api.ai._schedule_followup_runtime_resume", _fake_schedule)

    async def _run():
        created = await create_ai_run(
            AIRunCreateRequest(
                session_id="sess-approval-idempotent-001",
                question="审批应支持幂等重试",
                analysis_context={"analysis_type": "log", "service_name": "query-service"},
            )
        )
        run_id = created["run"]["run_id"]
        runtime_service.request_approval(
            run_id,
            approval_id="apr-idempotent-001",
            title="执行 kubectl 查询",
            reason="需要审批",
            command="kubectl -n islap get pods",
            requires_confirmation=True,
            requires_elevation=False,
        )
        first = await approve_ai_run(
            run_id,
            AIRunApproveRequest(
                approval_id="apr-idempotent-001",
                decision="approved",
                comment="允许执行",
                confirmed=True,
                elevated=False,
            ),
        )
        second = await approve_ai_run(
            run_id,
            AIRunApproveRequest(
                approval_id="apr-idempotent-001",
                decision="approved",
                comment="重复点击",
                confirmed=True,
                elevated=False,
            ),
        )
        return run_id, first, second

    run_id, first, second = asyncio.run(_run())

    assert first["approval"]["decision"] == "approved"
    assert second.get("idempotent") is True
    assert second["approval"]["decision"] == "approved"
    assert schedule_calls == [(run_id, False)]


def test_reject_ai_run_triggers_auto_replan_by_default(monkeypatch):
    runtime_service = _build_runtime_service()
    monkeypatch.setattr("api.ai.get_agent_runtime_service", lambda *_args, **_kwargs: runtime_service)

    async def _run():
        created = await create_ai_run(
            AIRunCreateRequest(
                session_id="sess-approval-reject-001",
                question="执行修复前需要确认",
                analysis_context={"analysis_type": "log", "service_name": "query-service"},
                runtime_options={"conversation_id": "conv-approval-reject-001"},
            )
        )
        run_id = created["run"]["run_id"]
        runtime_service.request_approval(
            run_id,
            approval_id="apr-reject-001",
            title="执行 kubectl rollout restart",
            reason="该动作会影响目标服务实例",
            command="kubectl rollout restart deployment/query-service",
            requires_confirmation=True,
            requires_elevation=True,
        )
        rejected = await approve_ai_run(
            run_id,
            AIRunApproveRequest(
                approval_id="apr-reject-001",
                decision="rejected",
                comment="暂不允许",
                confirmed=False,
                elevated=False,
            ),
        )
        fetched = await get_ai_run(run_id)
        events = await get_ai_run_events(run_id, after_seq=0, limit=20)
        return rejected, fetched, events

    rejected, fetched, events = asyncio.run(_run())

    assert rejected["run"]["status"] == "running"
    assert rejected["approval"]["decision"] == "rejected"
    assert rejected["replan"]["outcome"] == "replanned"
    assert fetched["run"]["status"] == "running"
    assert fetched["run"]["conversation_id"] == "conv-approval-reject-001"
    assert fetched["run"]["summary_json"]["current_phase"] == "planning"
    assert fetched["run"]["summary_json"]["replan_count"] == 1
    assert fetched["run"]["summary_json"].get("blocked_reason") is None
    assert fetched["run"]["ended_at"] is None
    event_types = [item["event_type"] for item in events["events"]]
    assert "approval_required" in event_types
    assert "approval_resolved" in event_types
    assert "action_replanned" in event_types


def test_reject_ai_run_blocks_after_single_default_replan(monkeypatch):
    runtime_service = _build_runtime_service()
    monkeypatch.setattr("api.ai.get_agent_runtime_service", lambda *_args, **_kwargs: runtime_service)

    async def _run():
        created = await create_ai_run(
            AIRunCreateRequest(
                session_id="sess-approval-reject-max1-001",
                question="默认仅允许一次 replan",
                analysis_context={"analysis_type": "log", "service_name": "query-service"},
            )
        )
        run_id = created["run"]["run_id"]

        runtime_service.request_approval(
            run_id,
            approval_id="apr-reject-max1-001",
            title="第一次审批",
            reason="第一次拒绝触发 replan",
            command="kubectl rollout restart deployment/query-service",
            requires_confirmation=True,
            requires_elevation=True,
        )
        first = await approve_ai_run(
            run_id,
            AIRunApproveRequest(
                approval_id="apr-reject-max1-001",
                decision="rejected",
                comment="第一次拒绝",
                confirmed=False,
                elevated=False,
            ),
        )

        runtime_service.request_approval(
            run_id,
            approval_id="apr-reject-max1-002",
            title="第二次审批",
            reason="第二次拒绝应 blocked",
            command="kubectl rollout restart deployment/query-service",
            requires_confirmation=True,
            requires_elevation=True,
        )
        second = await approve_ai_run(
            run_id,
            AIRunApproveRequest(
                approval_id="apr-reject-max1-002",
                decision="rejected",
                comment="第二次拒绝",
                confirmed=False,
                elevated=False,
            ),
        )
        fetched = await get_ai_run(run_id)
        return first, second, fetched

    first, second, fetched = asyncio.run(_run())

    assert first["run"]["status"] == "running"
    assert first["replan"]["outcome"] == "replanned"
    assert second["run"]["status"] == "blocked"
    assert second["replan"]["outcome"] == "terminated"
    assert second["replan"]["replan_count"] == 1
    assert second["replan"]["replan_max_rounds"] == 1
    assert fetched["run"]["status"] == "blocked"
    assert fetched["run"]["summary_json"]["blocked_reason"] == "approval_rejected_replan_limit"


def test_reject_ai_run_can_terminate_when_configured(monkeypatch):
    runtime_service = _build_runtime_service()
    monkeypatch.setattr("api.ai.get_agent_runtime_service", lambda *_args, **_kwargs: runtime_service)

    async def _run():
        created = await create_ai_run(
            AIRunCreateRequest(
                session_id="sess-approval-reject-terminate-001",
                question="执行修复前需要确认",
                analysis_context={"analysis_type": "log", "service_name": "query-service"},
                runtime_options={"approval_reject_strategy": "terminate"},
            )
        )
        run_id = created["run"]["run_id"]
        runtime_service.request_approval(
            run_id,
            approval_id="apr-reject-terminate-001",
            title="执行 kubectl rollout restart",
            reason="该动作会影响目标服务实例",
            command="kubectl rollout restart deployment/query-service",
            requires_confirmation=True,
            requires_elevation=True,
        )
        rejected = await approve_ai_run(
            run_id,
            AIRunApproveRequest(
                approval_id="apr-reject-terminate-001",
                decision="rejected",
                comment="终止执行",
                confirmed=False,
                elevated=False,
            ),
        )
        fetched = await get_ai_run(run_id)
        return rejected, fetched

    rejected, fetched = asyncio.run(_run())

    assert rejected["run"]["status"] == "blocked"
    assert rejected["replan"]["outcome"] == "terminated"
    assert fetched["run"]["summary_json"]["blocked_reason"] == "approval_rejected"
    assert fetched["run"]["ended_at"] is not None


def test_approval_timeout_blocks_run(monkeypatch):
    runtime_service = _build_runtime_service()
    monkeypatch.setattr("api.ai.get_agent_runtime_service", lambda *_args, **_kwargs: runtime_service)

    async def _run():
        created = await create_ai_run(
            AIRunCreateRequest(
                session_id="sess-approval-timeout-001",
                question="执行命令需要审批",
                analysis_context={"analysis_type": "log", "service_name": "query-service"},
                runtime_options={"approval_timeout_seconds": 1},
            )
        )
        run_id = created["run"]["run_id"]
        runtime_service.request_approval(
            run_id,
            approval_id="apr-timeout-001",
            title="执行高风险命令",
            reason="需要审批",
            command="kubectl rollout restart deployment/query-service",
            requires_confirmation=True,
            requires_elevation=True,
        )
        await asyncio.sleep(1.15)
        fetched = await get_ai_run(run_id)
        events = await get_ai_run_events(run_id, after_seq=0, limit=50)
        return fetched, events

    fetched, events = asyncio.run(_run())

    assert fetched["run"]["status"] == "blocked"
    assert fetched["run"]["summary_json"]["blocked_reason"] == "approval_timeout"
    event_types = [item["event_type"] for item in events["events"]]
    assert "approval_timeout" in event_types


def test_stream_ai_run_replays_existing_events_for_terminal_run(monkeypatch):
    runtime_service = _build_runtime_service()
    monkeypatch.setattr("api.ai.get_agent_runtime_service", lambda *_args, **_kwargs: runtime_service)

    async def _collect_stream():
        created = await create_ai_run(
            AIRunCreateRequest(
                session_id="sess-004",
                question="排查 query-service 数据库超时",
                analysis_context={"analysis_type": "log", "service_name": "query-service"},
            )
        )
        run_id = created["run"]["run_id"]
        await cancel_ai_run(run_id, AIRunCancelRequest(reason="user_cancelled"))
        response = await stream_ai_run(run_id, after_seq=0)
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk.decode() if isinstance(chunk, bytes) else str(chunk))
        return "".join(chunks)

    stream_text = asyncio.run(_collect_stream())

    assert "event: run_started" in stream_text
    assert "event: message_started" in stream_text
    assert "event: run_cancelled" in stream_text


def test_execute_ai_run_command_requires_approval_when_exec_service_requests_it(monkeypatch):
    runtime_service = _build_runtime_service()
    monkeypatch.setattr("api.ai.get_agent_runtime_service", lambda *_args, **_kwargs: runtime_service)
    
    async def _fake_create_command_run(**_kwargs):
        return {
            "status": "elevation_required",
            "message": "write command requires elevation and confirmation",
            "command_type": "repair",
            "risk_level": "high",
            "requires_confirmation": True,
            "requires_elevation": True,
            "confirmation_ticket": "ticket-001",
        }

    monkeypatch.setattr("ai.agent_runtime.service.create_command_run", _fake_create_command_run)

    async def _run():
        created = await create_ai_run(
            AIRunCreateRequest(
                session_id="sess-cmd-approval-001",
                question="执行重启命令",
                analysis_context={"analysis_type": "log", "service_name": "query-service"},
            )
        )
        run_id = created["run"]["run_id"]
        result = await execute_ai_run_command(
            run_id,
            AIRunCommandRequest(
                action_id="act-001",
                command="kubectl rollout restart deployment/query-service",
                command_spec=_generic_exec_spec("kubectl rollout restart deployment/query-service"),
                purpose="重启 query-service 以恢复服务",
                title="重启 query-service",
                diagnosis_contract={
                    "fault_summary": "query-service 5xx 持续升高，需执行受控重启恢复服务",
                    "evidence_gaps": ["需验证重启后错误率是否回落"],
                    "execution_plan": ["滚动重启 deployment/query-service", "观察告警与错误率变化"],
                    "why_command_needed": "配置核查无法直接恢复，需执行受控变更验证修复效果",
                },
            ),
        )
        fetched = await get_ai_run(run_id)
        events = await get_ai_run_events(run_id, after_seq=0, limit=20)
        return result, fetched, events

    result, fetched, events = asyncio.run(_run())

    assert result["status"] == "elevation_required"
    assert fetched["run"]["status"] == "waiting_approval"
    assert fetched["run"]["summary_json"]["pending_command_request"]["confirmation_ticket"] == "ticket-001"
    event_types = [item["event_type"] for item in events["events"]]
    assert "approval_required" in event_types
    pre_plan_events = [
        item
        for item in events["events"]
        if item.get("event_type") == "reasoning_step"
        and str((item.get("payload") or {}).get("title") or "").strip() == "执行前计划"
    ]
    assert pre_plan_events
    pre_plan_detail = str((pre_plan_events[-1].get("payload") or {}).get("detail") or "")
    assert "kubectl rollout restart deployment/query-service" in pre_plan_detail
    assert "重启 query-service 以恢复服务" in pre_plan_detail


def test_execute_ai_run_command_missing_command_spec_returns_blocked_with_recovery(monkeypatch):
    runtime_service = _build_runtime_service()
    monkeypatch.setattr("api.ai.get_agent_runtime_service", lambda *_args, **_kwargs: runtime_service)

    async def _run():
        created = await create_ai_run(
            AIRunCreateRequest(
                session_id="sess-cmd-spec-missing-001",
                question="执行只读命令",
                analysis_context={"analysis_type": "log", "service_name": "query-service"},
            )
        )
        run_id = created["run"]["run_id"]
        return await execute_ai_run_command(
            run_id,
            AIRunCommandRequest(
                action_id="act-cmd-spec-missing-001",
                command="kubectl get pods -n islap",
                purpose="检查 pod 状态",
                title="检查 pod",
            ),
        )

    result = asyncio.run(_run())

    assert result["status"] == "blocked"
    error = result.get("error") or {}
    recovery = error.get("recovery") or result.get("recovery") or {}
    assert error.get("code") == "missing_or_invalid_command_spec"
    assert recovery.get("fix_code") == "missing_or_invalid_command_spec"
    assert isinstance(recovery.get("suggested_command_spec"), dict)


def test_execute_ai_run_command_invalid_structured_spec_returns_recovery_hint(monkeypatch):
    runtime_service = _build_runtime_service()
    monkeypatch.setattr("api.ai.get_agent_runtime_service", lambda *_args, **_kwargs: runtime_service)

    async def _run():
        created = await create_ai_run(
            AIRunCreateRequest(
                session_id="sess-cmd-spec-invalid-001",
                question="执行 clickhouse 只读检查",
                analysis_context={"analysis_type": "log", "service_name": "query-service"},
            )
        )
        run_id = created["run"]["run_id"]
        return await execute_ai_run_command(
            run_id,
            AIRunCommandRequest(
                action_id="act-cmd-spec-invalid-001",
                command="clickhouse-client --query \"SELECT 1\"",
                purpose="检查 traces 表",
                title="检查 traces 表",
                command_spec={
                    "tool": "kubectl_clickhouse_query",
                    "args": {
                        "target_kind": "clickhouse_cluster",
                        "target_identity": "database:logs",
                        "query": "SELECTservice_nameASsvc FROM logs.tracesWHERE timestamp>now()-INTERVAL1HOUR",
                        "timeout_s": 20,
                    },
                },
            ),
        )

    result = asyncio.run(_run())

    assert result["status"] == "blocked"
    error = result.get("error") or {}
    recovery = error.get("recovery") or result.get("recovery") or {}
    assert "glued_sql_tokens" in str(error.get("message") or "")
    assert recovery.get("fix_code") == "glued_sql_tokens"
    suggested_spec = recovery.get("suggested_command_spec") or {}
    query_text = str((suggested_spec.get("args") or {}).get("query") or "")
    assert " AS " in query_text or " WHERE " in query_text


def test_execute_ai_run_command_write_requires_diagnosis_contract_before_dispatch(monkeypatch):
    runtime_service = _build_runtime_service()
    monkeypatch.setattr("api.ai.get_agent_runtime_service", lambda *_args, **_kwargs: runtime_service)

    async def _unexpected_create_command_run(**_kwargs):
        raise AssertionError("create_command_run should not be called before diagnosis_contract gate passes")

    monkeypatch.setattr("ai.agent_runtime.service.create_command_run", _unexpected_create_command_run)

    async def _run():
        created = await create_ai_run(
            AIRunCreateRequest(
                session_id="sess-cmd-contract-gate-001",
                question="执行写命令前必须补齐合同",
                analysis_context={"analysis_type": "log", "service_name": "query-service"},
            )
        )
        run_id = created["run"]["run_id"]
        result = await execute_ai_run_command(
            run_id,
            AIRunCommandRequest(
                action_id="act-contract-gate-001",
                command="kubectl rollout restart deployment/query-service",
                command_spec=_generic_exec_spec("kubectl rollout restart deployment/query-service"),
                purpose="重启 query-service 以恢复服务",
                title="重启 query-service",
            ),
        )
        fetched = await get_ai_run(run_id)
        return result, fetched

    result, fetched = asyncio.run(_run())

    assert result["status"] == "waiting_user_input"
    assert (result.get("error") or {}).get("code") == "diagnosis_contract_incomplete"
    assert fetched["run"]["status"] == "waiting_user_input"
    missing = fetched["run"]["summary_json"].get("diagnosis_contract_missing_fields") or []
    pending = fetched["run"]["summary_json"].get("pending_user_input") or {}
    assert "fault_summary" in missing
    assert "execution_plan" in missing
    assert pending.get("kind") == "business_question"
    assert pending.get("question_kind") == "write_safety_context"
    assert int(pending.get("recovery_attempts") or 0) >= 1
    assert "命令语义" not in str(pending.get("title") or "")
    assert "命令语义" not in str(pending.get("prompt") or "")
    assert "diagnosis_contract" not in str(pending.get("prompt") or "")


def test_execute_ai_run_command_write_blocks_after_diagnosis_contract_reask_limit(monkeypatch):
    runtime_service = _build_runtime_service()
    monkeypatch.setattr("api.ai.get_agent_runtime_service", lambda *_args, **_kwargs: runtime_service)

    async def _unexpected_create_command_run(**_kwargs):
        raise AssertionError("create_command_run should not be called when diagnosis_contract remains incomplete")

    monkeypatch.setattr("ai.agent_runtime.service.create_command_run", _unexpected_create_command_run)

    async def _run():
        created = await create_ai_run(
            AIRunCreateRequest(
                session_id="sess-cmd-contract-limit-001",
                question="诊断合同补齐最多重试一次",
                analysis_context={"analysis_type": "log", "service_name": "query-service"},
                runtime_options={"diagnosis_contract_reask_max_rounds": 1},
            )
        )
        run_id = created["run"]["run_id"]
        first = await execute_ai_run_command(
            run_id,
            AIRunCommandRequest(
                action_id="act-contract-limit-001",
                command="kubectl rollout restart deployment/query-service",
                command_spec=_generic_exec_spec("kubectl rollout restart deployment/query-service"),
                purpose="重启 query-service 以恢复服务",
                title="重启 query-service",
            ),
        )
        await continue_ai_run_with_user_input(
            run_id,
            AIRunInputRequest(text="先继续执行", source="user"),
        )
        second = await execute_ai_run_command(
            run_id,
            AIRunCommandRequest(
                action_id="act-contract-limit-002",
                command="kubectl rollout restart deployment/query-service",
                command_spec=_generic_exec_spec("kubectl rollout restart deployment/query-service"),
                purpose="重启 query-service 以恢复服务",
                title="重启 query-service",
            ),
        )
        fetched = await get_ai_run(run_id)
        return first, second, fetched

    first, second, fetched = asyncio.run(_run())

    assert first["status"] == "waiting_user_input"
    assert second["status"] == "blocked"
    assert (second.get("error") or {}).get("code") == "diagnosis_contract_incomplete"
    assert fetched["run"]["status"] == "blocked"
    assert fetched["run"]["summary_json"]["blocked_reason"] == "diagnosis_contract_incomplete"


def test_execute_ai_run_command_write_allows_dispatch_when_diagnosis_contract_complete(monkeypatch):
    runtime_service = _build_runtime_service()
    monkeypatch.setattr("api.ai.get_agent_runtime_service", lambda *_args, **_kwargs: runtime_service)
    captured = {"calls": 0}

    async def _fake_create_command_run(**_kwargs):
        captured["calls"] += 1
        return {
            "status": "elevation_required",
            "message": "write command requires elevation and confirmation",
            "command_type": "repair",
            "risk_level": "high",
            "requires_confirmation": True,
            "requires_elevation": True,
            "confirmation_ticket": "ticket-contract-ok-001",
        }

    monkeypatch.setattr("ai.agent_runtime.service.create_command_run", _fake_create_command_run)

    async def _run():
        created = await create_ai_run(
            AIRunCreateRequest(
                session_id="sess-cmd-contract-ok-001",
                question="合同完整后可进入审批",
                analysis_context={"analysis_type": "log", "service_name": "query-service"},
            )
        )
        run_id = created["run"]["run_id"]
        result = await execute_ai_run_command(
            run_id,
            AIRunCommandRequest(
                action_id="act-contract-ok-001",
                command="kubectl rollout restart deployment/query-service",
                command_spec=_generic_exec_spec("kubectl rollout restart deployment/query-service"),
                purpose="重启 query-service 以恢复服务",
                title="重启 query-service",
                diagnosis_contract={
                    "fault_summary": "query-service 多实例出现持续 5xx，需滚动重启恢复",
                    "evidence_gaps": ["需要确认重启后错误率是否下降"],
                    "execution_plan": ["滚动重启 deployment/query-service", "观察 5 分钟错误率与延迟"],
                    "why_command_needed": "当前仅通过配置检查无法恢复，需要执行受控重启验证修复效果",
                },
            ),
        )
        fetched = await get_ai_run(run_id)
        return result, fetched

    result, fetched = asyncio.run(_run())

    assert captured["calls"] == 1
    assert result["status"] == "elevation_required"
    assert fetched["run"]["status"] == "waiting_approval"
    assert fetched["run"]["summary_json"]["diagnosis_contract_missing_fields"] == []


def test_execute_ai_run_command_returns_running_existing_when_active_command_exists(monkeypatch):
    runtime_service = _build_runtime_service()
    monkeypatch.setattr("api.ai.get_agent_runtime_service", lambda *_args, **_kwargs: runtime_service)

    async def _fail_create_command_run(**_kwargs):
        raise AssertionError("create_command_run should not be called when active command exists")

    monkeypatch.setattr("ai.agent_runtime.service.create_command_run", _fail_create_command_run)

    async def _run():
        created = await create_ai_run(
            AIRunCreateRequest(
                session_id="sess-cmd-singleflight-001",
                question="执行命令时应保持单飞",
                analysis_context={"analysis_type": "log", "service_name": "query-service"},
            )
        )
        run_id = created["run"]["run_id"]
        runtime_service._update_run_summary(  # noqa: SLF001
            runtime_service.get_run(run_id),
            active_command_run_id="cmdrun-active-001",
            active_command_fingerprint="fp-001",
        )
        result = await execute_ai_run_command(
            run_id,
            AIRunCommandRequest(
                action_id="act-singleflight-001",
                command="kubectl -n islap get pods",
                command_spec=_generic_exec_spec("kubectl -n islap get pods"),
                purpose="查询 pod 状态",
                title="查询 pod 状态",
            ),
        )
        return result

    result = asyncio.run(_run())

    assert result["status"] == "running_existing"
    assert result["command_run_id"] == "cmdrun-active-001"


def test_execute_ai_run_command_returns_running_existing_from_index_when_active_missing(monkeypatch):
    runtime_service = _build_runtime_service()
    monkeypatch.setattr("api.ai.get_agent_runtime_service", lambda *_args, **_kwargs: runtime_service)
    create_calls: list[dict] = []

    async def _fake_create_command_run(**kwargs):
        create_calls.append(dict(kwargs))
        return {
            "run": {
                "run_id": "cmdrun-index-001",
                "status": "running",
            }
        }

    async def _fake_bridge_command_run(self, **_kwargs):
        return None

    monkeypatch.setattr("ai.agent_runtime.service.create_command_run", _fake_create_command_run)
    monkeypatch.setattr(
        "ai.agent_runtime.service.AgentRuntimeService._bridge_command_run",
        _fake_bridge_command_run,
    )

    async def _run():
        created = await create_ai_run(
            AIRunCreateRequest(
                session_id="sess-cmd-index-001",
                question="active 丢失后依然需要命中命令索引",
                analysis_context={"analysis_type": "log", "service_name": "query-service"},
            )
        )
        run_id = created["run"]["run_id"]
        first = await execute_ai_run_command(
            run_id,
            AIRunCommandRequest(
                action_id="act-index-001",
                command="kubectl -n islap get pods",
                command_spec=_generic_exec_spec("kubectl -n islap get pods"),
                purpose="查询 pod 状态",
                title="查询 pod 状态",
            ),
        )
        runtime_service._update_run_summary(  # noqa: SLF001
            runtime_service.get_run(run_id),
            active_command_run_id="",
            active_command_fingerprint="",
            active_command_execution_key="",
        )
        second = await execute_ai_run_command(
            run_id,
            AIRunCommandRequest(
                action_id="act-index-001",
                command="kubectl -n islap get pods",
                command_spec=_generic_exec_spec("kubectl -n islap get pods"),
                purpose="查询 pod 状态",
                title="查询 pod 状态",
            ),
        )
        snapshot = await get_ai_run(run_id)
        return first, second, snapshot

    first, second, snapshot = asyncio.run(_run())

    assert first["status"] == "running"
    assert second["status"] == "running_existing"
    assert second["command_run_id"] == "cmdrun-index-001"
    assert len(create_calls) == 1
    command_run_index = (snapshot["run"]["summary_json"] or {}).get("command_run_index") or {}
    assert isinstance(command_run_index, dict)
    assert any(
        isinstance(item, dict) and str(item.get("command_run_id") or "") == "cmdrun-index-001"
        for item in command_run_index.values()
    )


def test_execute_ai_run_command_normalizes_compact_pipeline_before_dispatch(monkeypatch):
    runtime_service = _build_runtime_service()
    monkeypatch.setattr("api.ai.get_agent_runtime_service", lambda *_args, **_kwargs: runtime_service)
    captured: dict[str, str] = {}

    async def _fake_create_command_run(**kwargs):
        captured["command"] = str(kwargs.get("command") or "")
        return {
            "status": "permission_required",
            "command_type": "unknown",
            "message": "blocked by policy",
        }

    monkeypatch.setattr("ai.agent_runtime.service.create_command_run", _fake_create_command_run)

    async def _run():
        created = await create_ai_run(
            AIRunCreateRequest(
                session_id="sess-cmd-normalize-001",
                question="执行命令前归一化",
                analysis_context={"analysis_type": "log", "service_name": "query-service"},
            )
        )
        run_id = created["run"]["run_id"]
        result = await execute_ai_run_command(
            run_id,
            AIRunCommandRequest(
                action_id="act-normalize-001",
                command="kubectl logs--tail=100 -l app=query-service|grep-ierror",
                command_spec=_generic_exec_spec("kubectl logs --tail=100 -l app=query-service"),
                purpose="校验归一化",
                title="归一化测试",
            ),
        )
        return result

    result = asyncio.run(_run())

    assert result["status"] == "waiting_user_input"
    assert captured["command"] == "kubectl logs --tail=100 -l app=query-service"


def test_approve_ai_run_resumes_pending_command_execution(monkeypatch):
    runtime_service = _build_runtime_service()
    monkeypatch.setattr("api.ai.get_agent_runtime_service", lambda *_args, **_kwargs: runtime_service)
    
    async def _fake_create_command_run(**_kwargs):
        return {
            "run": {
                "run_id": "cmdrun-001",
                "status": "running",
            }
        }

    monkeypatch.setattr("ai.agent_runtime.service.create_command_run", _fake_create_command_run)
    
    def _fake_bridge_exec_run_stream_to_runtime(**kwargs):
        kwargs["runtime_service"].append_event(
            kwargs["run_id"],
            "tool_call_finished",
            {
                "tool_call_id": kwargs["tool_call_id"],
                "status": "completed",
                "command_run_id": kwargs["exec_run_id"],
            },
        )
        return {
            "status": "completed",
            "stdout": "ok",
        }

    monkeypatch.setattr(
        "ai.agent_runtime.service.bridge_exec_run_stream_to_runtime",
        _fake_bridge_exec_run_stream_to_runtime,
    )

    async def _run():
        created = await create_ai_run(
            AIRunCreateRequest(
                session_id="sess-cmd-resume-001",
                question="执行修复命令",
                analysis_context={"analysis_type": "log", "service_name": "query-service"},
            )
        )
        run_id = created["run"]["run_id"]
        runtime_service._update_run_summary(  # noqa: SLF001
            runtime_service.get_run(run_id),
            pending_command_request={
                "tool_call_id": "tool-001",
                "action_id": "act-001",
                "command": "kubectl rollout restart deployment/query-service",
                "command_spec": _generic_exec_spec("kubectl rollout restart deployment/query-service"),
                "purpose": "重启 query-service 以恢复服务",
                "title": "重启 query-service",
                "tool_name": "command.exec",
                "timeout_seconds": 20,
                "confirmation_ticket": "ticket-001",
                "diagnosis_contract": {
                    "fault_summary": "query-service 5xx 持续升高，需执行受控重启恢复服务",
                    "evidence_gaps": ["需验证重启后错误率是否回落"],
                    "execution_plan": ["滚动重启 deployment/query-service", "观察告警与错误率变化"],
                    "why_command_needed": "配置核查无法直接恢复，需执行受控变更验证修复效果",
                },
            },
            pending_approval={"approval_id": "ticket-001"},
            pending_approval_count=1,
        )
        runtime_service.request_approval(
            run_id,
            approval_id="ticket-001",
            title="重启 query-service",
            reason="需要审批",
            command="kubectl rollout restart deployment/query-service",
            requires_confirmation=True,
            requires_elevation=True,
        )
        approved = await approve_ai_run(
            run_id,
            AIRunApproveRequest(
                approval_id="ticket-001",
                decision="approved",
                confirmed=True,
                elevated=True,
            ),
        )
        events = None
        for _ in range(10):
            await asyncio.sleep(0.01)
            candidate = await get_ai_run_events(run_id, after_seq=0, limit=50)
            event_types = [item["event_type"] for item in candidate["events"]]
            if "tool_call_finished" in event_types:
                events = candidate
                break
        if events is None:
            events = await get_ai_run_events(run_id, after_seq=0, limit=50)
        return approved, events

    approved, events = asyncio.run(_run())

    assert approved["run"]["status"] == "running"
    assert approved["command"]["status"] == "running"
    event_types = [item["event_type"] for item in events["events"]]
    assert "approval_resolved" in event_types
    assert "tool_call_finished" in event_types


def test_approve_ai_run_schedules_followup_resume_after_command_started(monkeypatch):
    runtime_service = _build_runtime_service()
    monkeypatch.setattr("api.ai.get_agent_runtime_service", lambda *_args, **_kwargs: runtime_service)
    schedule_calls = []

    def _fake_schedule(runtime_service_arg, run_id: str, *, wait_for_active_command: bool):
        assert runtime_service_arg is runtime_service
        schedule_calls.append((run_id, wait_for_active_command))

    monkeypatch.setattr("api.ai._schedule_followup_runtime_resume", _fake_schedule)

    async def _fake_execute_command_tool(**_kwargs):
        run = runtime_service.get_run(_kwargs["run_id"])
        return {
            "status": "running",
            "tool_call_id": "tool-cmd-001",
            "command_run_id": "cmdrun-001",
            "run": run,
            "command_run": {"run_id": "cmdrun-001", "status": "running"},
        }

    monkeypatch.setattr(runtime_service, "execute_command_tool", _fake_execute_command_tool)

    async def _run():
        created = await create_ai_run(
            AIRunCreateRequest(
                session_id="sess-cmd-schedule-001",
                question="审批后应继续 followup runtime",
                analysis_context={"analysis_type": "log", "service_name": "query-service"},
                runtime_options={"mode": "followup_analysis"},
            )
        )
        run_id = created["run"]["run_id"]
        runtime_service._update_run_summary(  # noqa: SLF001
            runtime_service.get_run(run_id),
            pending_command_request={
                "tool_call_id": "tool-001",
                "action_id": "act-001",
                "command": "kubectl -n islap get pods",
                "command_spec": _generic_exec_spec("kubectl -n islap get pods"),
                "purpose": "查询 pod 状态",
                "title": "查询 pod 状态",
                "tool_name": "command.exec",
                "timeout_seconds": 20,
                "confirmation_ticket": "ticket-schedule-001",
            },
            pending_approval={"approval_id": "ticket-schedule-001"},
            pending_approval_count=1,
        )
        runtime_service.request_approval(
            run_id,
            approval_id="ticket-schedule-001",
            title="查询 pod 状态",
            reason="需要审批",
            command="kubectl -n islap get pods",
            requires_confirmation=True,
            requires_elevation=False,
        )
        await approve_ai_run(
            run_id,
            AIRunApproveRequest(
                approval_id="ticket-schedule-001",
                decision="approved",
                confirmed=True,
                elevated=False,
            ),
        )
        return run_id

    run_id = asyncio.run(_run())

    assert schedule_calls == [(run_id, True)]


def test_approve_ai_run_returns_latest_waiting_approval_when_command_still_requires_confirmation(monkeypatch):
    runtime_service = _build_runtime_service()
    monkeypatch.setattr("api.ai.get_agent_runtime_service", lambda *_args, **_kwargs: runtime_service)
    schedule_calls = []

    def _fake_schedule(runtime_service_arg, run_id: str, *, wait_for_active_command: bool):
        assert runtime_service_arg is runtime_service
        schedule_calls.append((run_id, wait_for_active_command))

    monkeypatch.setattr("api.ai._schedule_followup_runtime_resume", _fake_schedule)

    async def _fake_execute_command_tool(**kwargs):
        approval_result = runtime_service.request_approval(
            kwargs["run_id"],
            approval_id="ticket-resume-next-001",
            title="再次确认命令",
            reason="命令需要再次确认",
            command="kubectl -n islap get pods",
            requires_confirmation=True,
            requires_elevation=False,
        )
        return {
            "status": "confirmation_required",
            "tool_call_id": "tool-cmd-next-001",
            "approval": (approval_result or {}).get("approval", {}),
            "run": (approval_result or {}).get("run"),
        }

    monkeypatch.setattr(runtime_service, "execute_command_tool", _fake_execute_command_tool)

    async def _run():
        created = await create_ai_run(
            AIRunCreateRequest(
                session_id="sess-cmd-resume-next-001",
                question="审批后命令仍需确认时应保持 waiting_approval",
                analysis_context={"analysis_type": "log", "service_name": "query-service"},
                runtime_options={"mode": "followup_analysis"},
            )
        )
        run_id = created["run"]["run_id"]
        runtime_service._update_run_summary(  # noqa: SLF001
            runtime_service.get_run(run_id),
            pending_command_request={
                "tool_call_id": "tool-001",
                "action_id": "act-001",
                "command": "kubectl -n islap get pods",
                "command_spec": _generic_exec_spec("kubectl -n islap get pods"),
                "purpose": "查询 pod 状态",
                "title": "查询 pod 状态",
                "tool_name": "command.exec",
                "timeout_seconds": 20,
                "confirmation_ticket": "ticket-resume-001",
            },
            pending_approval={"approval_id": "ticket-resume-001"},
            pending_approval_count=1,
        )
        runtime_service.request_approval(
            run_id,
            approval_id="ticket-resume-001",
            title="查询 pod 状态",
            reason="需要审批",
            command="kubectl -n islap get pods",
            requires_confirmation=True,
            requires_elevation=False,
        )
        approved = await approve_ai_run(
            run_id,
            AIRunApproveRequest(
                approval_id="ticket-resume-001",
                decision="approved",
                confirmed=True,
                elevated=False,
            ),
        )
        return run_id, approved

    run_id, approved = asyncio.run(_run())

    assert approved["run"]["run_id"] == run_id
    assert approved["run"]["status"] == "waiting_approval"
    assert approved["command"]["status"] == "confirmation_required"
    assert isinstance(approved.get("next_approval"), dict)
    assert approved["next_approval"]["approval_id"] == "ticket-resume-next-001"
    assert schedule_calls == []


def test_approve_ai_run_auto_retries_when_ticket_context_mismatch(monkeypatch):
    runtime_service = _build_runtime_service()
    monkeypatch.setattr("api.ai.get_agent_runtime_service", lambda *_args, **_kwargs: runtime_service)
    execute_calls = []

    async def _fake_execute_command_tool(**kwargs):
        execute_calls.append(dict(kwargs))
        if len(execute_calls) == 1:
            approval_result = runtime_service.request_approval(
                kwargs["run_id"],
                approval_id="ticket-retry-001",
                title="重新确认命令",
                reason="confirmation ticket invalid: ticket_message_mismatch",
                command="kubectl -n islap get pods",
                requires_confirmation=True,
                requires_elevation=False,
            )
            return {
                "status": "confirmation_required",
                "tool_call_id": "tool-cmd-retry-001",
                "approval": (approval_result or {}).get("approval", {}),
                "run": (approval_result or {}).get("run"),
            }
        run = runtime_service.get_run(kwargs["run_id"])
        return {
            "status": "running",
            "tool_call_id": "tool-cmd-retry-001",
            "command_run_id": "cmdrun-retry-001",
            "run": run,
            "command_run": {"run_id": "cmdrun-retry-001", "status": "running"},
        }

    monkeypatch.setattr(runtime_service, "execute_command_tool", _fake_execute_command_tool)

    async def _run():
        created = await create_ai_run(
            AIRunCreateRequest(
                session_id="sess-cmd-retry-001",
                question="审批后自动重试 ticket mismatch",
                analysis_context={"analysis_type": "log", "service_name": "query-service"},
                runtime_options={"mode": "followup_analysis"},
            )
        )
        run_id = created["run"]["run_id"]
        runtime_service._update_run_summary(  # noqa: SLF001
            runtime_service.get_run(run_id),
            pending_command_request={
                "tool_call_id": "tool-cmd-retry-001",
                "action_id": "act-retry-001",
                "command": "kubectl -n islap get pods",
                "command_spec": _generic_exec_spec("kubectl -n islap get pods"),
                "purpose": "查询 pod 状态",
                "title": "查询 pod 状态",
                "tool_name": "command.exec",
                "timeout_seconds": 20,
                "confirmation_ticket": "ticket-initial-001",
            },
            pending_approval={"approval_id": "ticket-initial-001"},
            pending_approval_count=1,
        )
        runtime_service.request_approval(
            run_id,
            approval_id="ticket-initial-001",
            title="查询 pod 状态",
            reason="需要审批",
            command="kubectl -n islap get pods",
            requires_confirmation=True,
            requires_elevation=False,
        )
        approved = await approve_ai_run(
            run_id,
            AIRunApproveRequest(
                approval_id="ticket-initial-001",
                decision="approved",
                confirmed=True,
                elevated=False,
            ),
        )
        return approved

    approved = asyncio.run(_run())

    assert len(execute_calls) == 2
    assert execute_calls[1]["confirmation_ticket"] == "ticket-retry-001"
    assert approved["command"]["status"] == "running"
    assert approved["command"]["auto_retried"] is True


def test_approve_ai_run_retries_ticket_mismatch_inside_execute_command_tool(monkeypatch):
    runtime_service = _build_runtime_service()
    monkeypatch.setattr("api.ai.get_agent_runtime_service", lambda *_args, **_kwargs: runtime_service)
    create_calls = []

    async def _fake_create_command_run(**kwargs):
        create_calls.append(dict(kwargs))
        if len(create_calls) == 1:
            return {
                "status": "confirmation_required",
                "message": "confirmation ticket invalid: ticket_message_mismatch",
                "confirmation_ticket": "ticket-retry-inner-001",
                "command_type": "query",
                "risk_level": "low",
                "requires_confirmation": True,
                "requires_elevation": False,
                "approval_policy": "confirmation_required",
                "executor_type": "sandbox_pod",
                "executor_profile": "toolbox-k8s-readonly",
                "target_kind": "k8s_cluster",
                "target_identity": "namespace:islap",
            }
        return {
            "run": {
                "run_id": "cmdrun-inner-retry-001",
                "status": "running",
            }
        }

    monkeypatch.setattr("ai.agent_runtime.service.create_command_run", _fake_create_command_run)

    def _fake_bridge_exec_run_stream_to_runtime(**kwargs):
        kwargs["runtime_service"].append_event(
            kwargs["run_id"],
            "tool_call_finished",
            {
                "tool_call_id": kwargs["tool_call_id"],
                "status": "completed",
                "command_run_id": kwargs["exec_run_id"],
                "exit_code": 0,
            },
        )
        return {
            "status": "completed",
            "exit_code": 0,
            "stdout": "ok",
            "stderr": "",
        }

    monkeypatch.setattr(
        "ai.agent_runtime.service.bridge_exec_run_stream_to_runtime",
        _fake_bridge_exec_run_stream_to_runtime,
    )

    async def _run():
        created = await create_ai_run(
            AIRunCreateRequest(
                session_id="sess-cmd-inner-retry-001",
                question="审批后自动重试 mismatch（service 内部）",
                analysis_context={"analysis_type": "log", "service_name": "query-service"},
                runtime_options={"mode": "followup_analysis"},
            )
        )
        run_id = created["run"]["run_id"]
        runtime_service._update_run_summary(  # noqa: SLF001
            runtime_service.get_run(run_id),
            pending_command_request={
                "tool_call_id": "tool-inner-retry-001",
                "action_id": "act-inner-retry-001",
                "command": "kubectl -n islap get pods",
                "command_spec": _generic_exec_spec("kubectl -n islap get pods"),
                "purpose": "查询 pod 状态",
                "title": "查询 pod 状态",
                "tool_name": "command.exec",
                "timeout_seconds": 20,
                "confirmation_ticket": "ticket-initial-inner-001",
            },
            pending_approval={"approval_id": "ticket-initial-inner-001"},
            pending_approval_count=1,
        )
        runtime_service.request_approval(
            run_id,
            approval_id="ticket-initial-inner-001",
            title="查询 pod 状态",
            reason="需要审批",
            command="kubectl -n islap get pods",
            requires_confirmation=True,
            requires_elevation=False,
        )
        approved = await approve_ai_run(
            run_id,
            AIRunApproveRequest(
                approval_id="ticket-initial-inner-001",
                decision="approved",
                confirmed=True,
                elevated=False,
            ),
        )
        events = await get_ai_run_events(run_id, after_seq=0, limit=80)
        return approved, events

    approved, events = asyncio.run(_run())

    assert len(create_calls) == 2
    assert create_calls[0]["confirmation_ticket"] == "ticket-initial-inner-001"
    assert create_calls[1]["confirmation_ticket"] == "ticket-retry-inner-001"
    assert approved["command"]["status"] == "running"
    assert approved["command"]["auto_retried"] is True
    approval_reasons = [
        str((item.get("payload") or {}).get("reason") or "")
        for item in events["events"]
        if item.get("event_type") == "approval_required"
    ]
    assert not any("ticket_message_mismatch" in reason for reason in approval_reasons)


def test_execute_ai_run_command_skips_duplicate_attempt_after_failed_terminal(monkeypatch):
    runtime_service = _build_runtime_service()
    monkeypatch.setattr("api.ai.get_agent_runtime_service", lambda *_args, **_kwargs: runtime_service)
    create_calls = []

    async def _fake_create_command_run(**kwargs):
        create_calls.append(dict(kwargs))
        return {
            "run": {
                "run_id": f"cmdrun-failed-{len(create_calls)}",
                "status": "running",
            }
        }

    monkeypatch.setattr("ai.agent_runtime.service.create_command_run", _fake_create_command_run)

    def _fake_bridge_exec_run_stream_to_runtime(**kwargs):
        kwargs["runtime_service"].append_event(
            kwargs["run_id"],
            "tool_call_finished",
            {
                "tool_call_id": kwargs["tool_call_id"],
                "status": "failed",
                "command_run_id": kwargs["exec_run_id"],
                "exit_code": 22,
                "stderr": "mock failed",
            },
        )
        return {
            "status": "failed",
            "exit_code": 22,
            "stdout": "",
            "stderr": "mock failed",
        }

    monkeypatch.setattr(
        "ai.agent_runtime.service.bridge_exec_run_stream_to_runtime",
        _fake_bridge_exec_run_stream_to_runtime,
    )

    async def _run():
        created = await create_ai_run(
            AIRunCreateRequest(
                session_id="sess-cmd-dup-attempt-001",
                question="同一 run 失败命令应避免重复执行",
                analysis_context={"analysis_type": "log", "service_name": "query-service"},
            )
        )
        run_id = created["run"]["run_id"]
        first = await execute_ai_run_command(
            run_id,
            AIRunCommandRequest(
                action_id="lc-1",
                command="kubectl -n islap get pods",
                command_spec=_generic_exec_spec("kubectl -n islap get pods"),
                purpose="查询 pod 状态",
                title="查询 pod 状态",
                confirmed=True,
                elevated=False,
            ),
        )
        for _ in range(20):
            await asyncio.sleep(0.01)
            snapshot = await get_ai_run(run_id)
            if str(snapshot["run"]["summary_json"].get("last_command_status") or "").lower() == "failed":
                break
        second = await execute_ai_run_command(
            run_id,
            AIRunCommandRequest(
                action_id="lc-1",
                command="kubectl -n islap get pods",
                command_spec=_generic_exec_spec("kubectl -n islap get pods"),
                purpose="查询 pod 状态",
                title="查询 pod 状态",
                confirmed=True,
                elevated=False,
            ),
        )
        events = await get_ai_run_events(run_id, after_seq=0, limit=120)
        return first, second, events

    first, second, events = asyncio.run(_run())

    assert first["status"] == "running"
    assert second["status"] == "skipped_duplicate_attempt"
    assert len(create_calls) == 1
    duplicate_msgs = [
        str((item.get("payload") or {}).get("message") or "")
        for item in events["events"]
        if item.get("event_type") == "tool_call_skipped_duplicate"
    ]
    assert any("同一 run 已尝试过该命令" in item for item in duplicate_msgs)


def test_execute_ai_run_command_keeps_active_when_bridge_missing_terminal(monkeypatch):
    runtime_service = _build_runtime_service()
    monkeypatch.setattr("api.ai.get_agent_runtime_service", lambda *_args, **_kwargs: runtime_service)
    create_calls = []

    async def _fake_create_command_run(**kwargs):
        create_calls.append(dict(kwargs))
        return {
            "run": {
                "run_id": "cmdrun-missing-terminal-001",
                "status": "running",
            }
        }

    def _fake_bridge_exec_run_stream_to_runtime(**_kwargs):
        # simulate stream closed without terminal event
        return {
            "status": "running",
            "exit_code": 0,
            "stdout": "",
            "stderr": "",
        }

    monkeypatch.setattr("ai.agent_runtime.service.create_command_run", _fake_create_command_run)
    monkeypatch.setattr(
        "ai.agent_runtime.service.bridge_exec_run_stream_to_runtime",
        _fake_bridge_exec_run_stream_to_runtime,
    )

    async def _run():
        created = await create_ai_run(
            AIRunCreateRequest(
                session_id="sess-cmd-active-retain-001",
                question="桥接未观察到终态时不应清空 active",
                analysis_context={"analysis_type": "log", "service_name": "query-service"},
            )
        )
        run_id = created["run"]["run_id"]
        first = await execute_ai_run_command(
            run_id,
            AIRunCommandRequest(
                action_id="lc-active-001",
                command="kubectl -n islap get pods",
                command_spec=_generic_exec_spec("kubectl -n islap get pods"),
                purpose="查询 pod 状态",
                title="查询 pod 状态",
                confirmed=True,
                elevated=False,
            ),
        )
        for _ in range(20):
            await asyncio.sleep(0.01)
            snapshot = await get_ai_run(run_id)
            if str(snapshot["run"]["summary_json"].get("last_command_status") or "").lower() in {"running", "submitted"}:
                break
        snapshot = await get_ai_run(run_id)
        second = await execute_ai_run_command(
            run_id,
            AIRunCommandRequest(
                action_id="lc-active-001",
                command="kubectl -n islap get pods",
                command_spec=_generic_exec_spec("kubectl -n islap get pods"),
                purpose="查询 pod 状态",
                title="查询 pod 状态",
                confirmed=True,
                elevated=False,
            ),
        )
        return first, second, snapshot

    first, second, snapshot = asyncio.run(_run())

    assert first["status"] == "running"
    assert second["status"] == "running_existing"
    assert second["command_run_id"] == "cmdrun-missing-terminal-001"
    assert len(create_calls) == 1
    assert snapshot["run"]["summary_json"]["active_command_run_id"] == "cmdrun-missing-terminal-001"


def test_execute_ai_run_command_timeout_auto_recovers_with_degraded_structured_spec(monkeypatch):
    runtime_service = _build_runtime_service()
    monkeypatch.setattr("api.ai.get_agent_runtime_service", lambda *_args, **_kwargs: runtime_service)
    create_calls = []
    bridge_calls = []

    async def _fake_create_command_run(**kwargs):
        create_calls.append(dict(kwargs))
        return {
            "run": {
                "run_id": f"cmdrun-timeout-{len(create_calls)}",
                "status": "running",
            }
        }

    monkeypatch.setattr("ai.agent_runtime.service.create_command_run", _fake_create_command_run)

    def _fake_bridge_exec_run_stream_to_runtime(**kwargs):
        bridge_calls.append(dict(kwargs))
        if len(bridge_calls) == 1:
            kwargs["runtime_service"].append_event(
                kwargs["run_id"],
                "tool_call_finished",
                {
                    "tool_call_id": kwargs["tool_call_id"],
                    "status": "timed_out",
                    "command_run_id": kwargs["exec_run_id"],
                    "exit_code": -9,
                    "timed_out": True,
                    "stderr": "mock timeout",
                    "command": create_calls[0]["command"],
                    "purpose": create_calls[0]["purpose"],
                },
            )
            return {
                "status": "timed_out",
                "exit_code": -9,
                "timed_out": True,
                "stdout": "",
                "stderr": "mock timeout",
                "command": create_calls[0]["command"],
                "purpose": create_calls[0]["purpose"],
            }
        kwargs["runtime_service"].append_event(
            kwargs["run_id"],
            "tool_call_finished",
            {
                "tool_call_id": kwargs["tool_call_id"],
                "status": "completed",
                "command_run_id": kwargs["exec_run_id"],
                "exit_code": 0,
                "command": create_calls[-1]["command"],
                "purpose": create_calls[-1]["purpose"],
            },
        )
        return {
            "status": "completed",
            "exit_code": 0,
            "timed_out": False,
            "stdout": "ok",
            "stderr": "",
            "command": create_calls[-1]["command"],
            "purpose": create_calls[-1]["purpose"],
        }

    monkeypatch.setattr(
        "ai.agent_runtime.service.bridge_exec_run_stream_to_runtime",
        _fake_bridge_exec_run_stream_to_runtime,
    )

    async def _run():
        created = await create_ai_run(
            AIRunCreateRequest(
                session_id="sess-timeout-recovery-001",
                question="结构化查询超时后应自动降级",
                analysis_context={"analysis_type": "log", "service_name": "query-service"},
            )
        )
        run_id = created["run"]["run_id"]
        first = await execute_ai_run_command(
            run_id,
            AIRunCommandRequest(
                action_id="act-timeout-001",
                command_spec={
                    "tool": "clickhouse_query",
                    "args": {
                        "namespace": "islap",
                        "pod_name": "clickhouse-0",
                        "target_kind": "clickhouse_cluster",
                        "target_identity": "database:logs",
                        "query": "SELECT count() FROM otel_logs LIMIT 1000",
                        "timeout_s": 60,
                    },
                },
                purpose="确认日志总量",
                title="查询日志总量",
                confirmed=True,
            ),
        )
        for _ in range(40):
            await asyncio.sleep(0.01)
            snapshot = await get_ai_run(run_id)
            if len(create_calls) >= 2 and str(snapshot["run"]["summary_json"].get("last_command_status") or "").lower() == "completed":
                break
        snapshot = await get_ai_run(run_id)
        events = await get_ai_run_events(run_id, after_seq=0, limit=160)
        return first, snapshot, events

    first, snapshot, events = asyncio.run(_run())

    assert first["status"] == "running"
    assert len(create_calls) == 2
    assert "LIMIT 1000" in create_calls[0]["command"]
    assert "LIMIT 200" in create_calls[1]["command"]
    assert snapshot["run"]["summary_json"]["last_command_status"] == "completed"
    assert snapshot["run"]["summary_json"]["last_timeout_recovery_attempts"] == 1
    event_types = [item.get("event_type") for item in events["events"]]
    assert "action_timeout_recovery_scheduled" in event_types


def test_execute_ai_run_command_timeout_without_structured_spec_asks_business_question(monkeypatch):
    runtime_service = _build_runtime_service()
    monkeypatch.setattr("api.ai.get_agent_runtime_service", lambda *_args, **_kwargs: runtime_service)
    create_calls = []

    async def _fake_create_command_run(**kwargs):
        create_calls.append(dict(kwargs))
        return {
            "run": {
                "run_id": "cmdrun-timeout-raw-001",
                "status": "running",
            }
        }

    monkeypatch.setattr("ai.agent_runtime.service.create_command_run", _fake_create_command_run)

    def _fake_bridge_exec_run_stream_to_runtime(**kwargs):
        kwargs["runtime_service"].append_event(
            kwargs["run_id"],
            "tool_call_finished",
            {
                "tool_call_id": kwargs["tool_call_id"],
                "status": "timed_out",
                "command_run_id": kwargs["exec_run_id"],
                "exit_code": -9,
                "timed_out": True,
                "stderr": "mock timeout",
                "command": create_calls[0]["command"],
                "purpose": create_calls[0]["purpose"],
            },
        )
        return {
            "status": "timed_out",
            "exit_code": -9,
            "timed_out": True,
            "stdout": "",
            "stderr": "mock timeout",
            "command": create_calls[0]["command"],
            "purpose": create_calls[0]["purpose"],
        }

    monkeypatch.setattr(
        "ai.agent_runtime.service.bridge_exec_run_stream_to_runtime",
        _fake_bridge_exec_run_stream_to_runtime,
    )

    async def _run():
        created = await create_ai_run(
            AIRunCreateRequest(
                session_id="sess-timeout-question-001",
                question="普通查询超时后应转成业务问题",
                analysis_context={"analysis_type": "log", "service_name": "query-service"},
            )
        )
        run_id = created["run"]["run_id"]
        first = await execute_ai_run_command(
            run_id,
            AIRunCommandRequest(
                action_id="act-timeout-question-001",
                command="kubectl -n islap get pods",
                command_spec=_generic_exec_spec("kubectl -n islap get pods"),
                purpose="查看 query-service pod 状态",
                title="查看 pod 状态",
                confirmed=True,
            ),
        )
        for _ in range(40):
            await asyncio.sleep(0.01)
            snapshot = await get_ai_run(run_id)
            if snapshot["run"]["status"] == "waiting_user_input":
                break
        snapshot = await get_ai_run(run_id)
        return first, snapshot

    first, snapshot = asyncio.run(_run())

    pending = snapshot["run"]["summary_json"]["pending_user_input"]
    assert first["status"] == "running"
    assert len(create_calls) == 1
    assert snapshot["run"]["status"] == "waiting_user_input"
    assert pending["kind"] == "business_question"
    assert pending["question_kind"] == "timeout_scope"
    assert "命令语义" not in pending["prompt"]


def test_continue_ai_run_with_user_input_schedules_followup_resume(monkeypatch):
    runtime_service = _build_runtime_service()
    monkeypatch.setattr("api.ai.get_agent_runtime_service", lambda *_args, **_kwargs: runtime_service)
    schedule_calls = []

    def _fake_schedule(runtime_service_arg, run_id: str, *, wait_for_active_command: bool):
        assert runtime_service_arg is runtime_service
        schedule_calls.append((run_id, wait_for_active_command))

    monkeypatch.setattr("api.ai._schedule_followup_runtime_resume", _fake_schedule)

    async def _run():
        created = await create_ai_run(
            AIRunCreateRequest(
                session_id="sess-user-input-resume-001",
                question="unknown 命令后补充一句话并继续",
                analysis_context={"analysis_type": "log", "service_name": "query-service"},
                runtime_options={"mode": "followup_analysis"},
            )
        )
        run_id = created["run"]["run_id"]
        runtime_service.request_user_input(
            run_id,
            action_id="act-input-001",
            title="补充命令语义",
            prompt="请补充一句话说明要执行的具体命令语义。",
            reason="当前动作未提供可执行命令",
            command="unknown command",
            purpose="补全语义",
        )
        resumed = await continue_ai_run_with_user_input(
            run_id,
            AIRunInputRequest(text="改为只读检查 query-service 最近 5 分钟错误日志。"),
        )
        return run_id, resumed

    run_id, resumed = asyncio.run(_run())

    assert resumed["run"]["status"] == "running"
    assert schedule_calls == [(run_id, False)]


def test_ensure_runtime_input_context_ready_retries_once_then_times_out(monkeypatch):
    run = SimpleNamespace(
        status="waiting_user_input",
        context_json={"runtime_mode": "followup_analysis"},
        summary_json={"runtime_options": {}},
        session_id="sess-hydrate-timeout-001",
        conversation_id="conv-hydrate-timeout-001",
    )

    class _RuntimeService:
        def get_run_fresh(self, _run_id):
            return run

        def get_run(self, _run_id):
            return run

    monkeypatch.setattr("api.ai.get_agent_runtime_service", lambda *_args, **_kwargs: _RuntimeService())
    monkeypatch.setattr("api.ai._resolve_runtime_input_context_hydrate_retry_max", lambda: 1)

    async def _always_timeout(**_kwargs):
        raise asyncio.TimeoutError()

    monkeypatch.setattr("api.ai._hydrate_runtime_input_context_once", _always_timeout)

    async def _run():
        with pytest.raises(HTTPException) as exc_info:
            await ensure_runtime_input_context_ready("run-hydrate-timeout-001")
        return exc_info.value

    error = asyncio.run(_run())

    assert error.status_code == 409
    assert isinstance(error.detail, dict)
    assert error.detail["code"] == "context_hydration_timeout"
    assert error.detail["attempts"] == 2
    assert error.detail["retryable"] is True


def test_ensure_runtime_input_context_ready_second_attempt_success(monkeypatch):
    run = SimpleNamespace(
        status="waiting_user_input",
        context_json={"runtime_mode": "followup_analysis"},
        summary_json={"runtime_options": {}},
        session_id="sess-hydrate-retry-001",
        conversation_id="conv-hydrate-retry-001",
    )

    class _RuntimeService:
        def get_run_fresh(self, _run_id):
            return run

        def get_run(self, _run_id):
            return run

    monkeypatch.setattr("api.ai.get_agent_runtime_service", lambda *_args, **_kwargs: _RuntimeService())
    monkeypatch.setattr("api.ai._resolve_runtime_input_context_hydrate_retry_max", lambda: 1)

    attempts = {"count": 0}

    async def _timeout_then_success(**_kwargs):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise asyncio.TimeoutError()
        return {"status": "ok", "history_items": 8}

    monkeypatch.setattr("api.ai._hydrate_runtime_input_context_once", _timeout_then_success)

    async def _run():
        return await ensure_runtime_input_context_ready("run-hydrate-retry-001")

    result = asyncio.run(_run())

    assert result["status"] == "ok"
    assert result["attempt"] == 2
    assert attempts["count"] == 2


def test_execute_ai_run_command_requires_purpose(monkeypatch):
    runtime_service = _build_runtime_service()
    monkeypatch.setattr("api.ai.get_agent_runtime_service", lambda *_args, **_kwargs: runtime_service)

    async def _run():
        created = await create_ai_run(
            AIRunCreateRequest(
                session_id="sess-cmd-purpose-001",
                question="执行排查命令",
                analysis_context={"analysis_type": "log", "service_name": "query-service"},
            )
        )
        run_id = created["run"]["run_id"]
        try:
            await execute_ai_run_command(
                run_id,
                AIRunCommandRequest(
                    action_id="act-purpose-001",
                    command="echo runtime-check",
                    purpose="   ",
                    title="执行命令",
                ),
            )
        except HTTPException as exc:
            return exc
        raise AssertionError("expected HTTPException")

    error = asyncio.run(_run())

    assert error.status_code == 400
    assert error.detail == "purpose is required"


def test_execute_ai_run_command_unknown_semantics_enters_waiting_user_input(monkeypatch):
    runtime_service = _build_runtime_service()
    monkeypatch.setattr("api.ai.get_agent_runtime_service", lambda *_args, **_kwargs: runtime_service)
    safe_unknown_semantics_command = "echo runtime-check"

    async def _fake_create_command_run(**_kwargs):
        return {
            "status": "permission_required",
            "message": "unknown command semantics",
            "command_type": "unknown",
            "risk_level": "high",
        }

    monkeypatch.setattr("ai.agent_runtime.service.create_command_run", _fake_create_command_run)

    async def _run():
        created = await create_ai_run(
            AIRunCreateRequest(
                session_id="sess-cmd-unknown-001",
                question="执行命令语义不完整",
                analysis_context={"analysis_type": "log", "service_name": "query-service"},
            )
        )
        run_id = created["run"]["run_id"]
        result = await execute_ai_run_command(
            run_id,
            AIRunCommandRequest(
                action_id="act-unknown-001",
                command=safe_unknown_semantics_command,
                command_spec=_generic_exec_spec(safe_unknown_semantics_command),
                purpose="尝试执行排查动作",
                title="执行未知命令",
            ),
        )
        fetched = await get_ai_run(run_id)
        events = await get_ai_run_events(run_id, after_seq=0, limit=50)
        resumed = await continue_ai_run_with_user_input(
            run_id,
            AIRunInputRequest(text="请改为只读检查 query-service 最近 5 分钟错误日志。"),
        )
        resumed_snapshot = await get_ai_run(run_id)
        resumed_events = await get_ai_run_events(run_id, after_seq=0, limit=80)
        return result, fetched, events, resumed, resumed_snapshot, resumed_events

    result, fetched, events, resumed, resumed_snapshot, resumed_events = asyncio.run(_run())

    assert result["status"] == "waiting_user_input"
    assert fetched["run"]["status"] == "waiting_user_input"
    pending = fetched["run"]["summary_json"].get("pending_user_input") or {}
    assert resumed["run"]["status"] == "running"
    assert resumed["user_input"]["text"] == "请改为只读检查 query-service 最近 5 分钟错误日志。"
    assert resumed["user_input"]["question_kind"] == "diagnosis_goal"
    assert resumed_snapshot["run"]["status"] == "running"
    assert pending.get("kind") == "business_question"
    assert pending.get("question_kind") == "diagnosis_goal"
    assert int(pending.get("recovery_attempts") or 0) >= 1
    assert "命令语义" not in str(pending.get("title") or "")
    assert "命令语义" not in str(pending.get("prompt") or "")
    event_types = [item["event_type"] for item in events["events"]]
    assert "action_waiting_user_input" in event_types
    resumed_event_types = [item["event_type"] for item in resumed_events["events"]]
    assert "action_resumed" in resumed_event_types


def test_execute_ai_run_command_sql_preflight_uses_llm_repair_before_waiting_user_input(monkeypatch):
    runtime_service = _build_runtime_service()
    monkeypatch.setattr("api.ai.get_agent_runtime_service", lambda *_args, **_kwargs: runtime_service)
    monkeypatch.setenv("AI_RUNTIME_SQL_LLM_REPAIR_ENABLED", "true")
    create_calls = []

    async def _fake_create_command_run(**kwargs):
        create_calls.append(dict(kwargs))
        if len(create_calls) == 1:
            return {
                "status": "permission_required",
                "message": "sql_preflight_failed: syntax error near 'DESCRIBETABLE'",
                "command_type": "unknown",
                "risk_level": "high",
            }
        return {
            "run": {
                "run_id": "cmdrun-llm-repair-001",
                "status": "running",
            }
        }

    monkeypatch.setattr("ai.agent_runtime.service.create_command_run", _fake_create_command_run)

    def _fake_attempt_command_recovery(**kwargs):
        return {
            "status": "ask_user",
            "command": kwargs.get("command") or "",
            "command_spec": kwargs.get("command_spec") if isinstance(kwargs.get("command_spec"), dict) else {},
            "failure_code": "sql_preflight_failed",
            "failure_message": "sql_preflight_failed: syntax error",
            "recovery_attempts": [],
        }

    monkeypatch.setattr("ai.agent_runtime.service.attempt_command_recovery", _fake_attempt_command_recovery)

    class _FakeLLMService:
        async def chat(self, message: str, context=None) -> str:
            assert "SQL 修复器" in message
            return '{"query":"DESCRIBE TABLE logs.obs_traces_1m"}'

    monkeypatch.setattr("ai.agent_runtime.service.get_llm_service", lambda: _FakeLLMService())

    def _fake_bridge_exec_run_stream_to_runtime(**kwargs):
        kwargs["runtime_service"].append_event(
            kwargs["run_id"],
            "tool_call_finished",
            {
                "tool_call_id": kwargs["tool_call_id"],
                "status": "completed",
                "command_run_id": kwargs["exec_run_id"],
                "exit_code": 0,
            },
        )
        return {
            "status": "completed",
            "exit_code": 0,
            "stdout": "ok",
            "stderr": "",
        }

    monkeypatch.setattr(
        "ai.agent_runtime.service.bridge_exec_run_stream_to_runtime",
        _fake_bridge_exec_run_stream_to_runtime,
    )

    async def _run():
        created = await create_ai_run(
            AIRunCreateRequest(
                session_id="sess-cmd-llm-repair-001",
                question="bad sql spec 应先尝试 llm 修复",
                analysis_context={"analysis_type": "log", "service_name": "query-service"},
            )
        )
        run_id = created["run"]["run_id"]
        result = await execute_ai_run_command(
            run_id,
            AIRunCommandRequest(
                action_id="act-llm-repair-001",
                command="kubectl -n islap exec -i POD -- clickhouse-client --query \"DESCRIBETABLElogs.obs_traces_1m\"",
                purpose="查询表结构",
                title="查询表结构",
                command_spec={
                    "tool": "kubectl_clickhouse_query",
                    "args": {
                        "namespace": "islap",
                        "pod_name": "clickhouse-0",
                        "target_kind": "clickhouse_cluster",
                        "target_identity": "database:logs",
                        "query": "SELECT * FROM logs.obs_traces_1m LIMIT 10",
                        "timeout_s": 30,
                    },
                },
            ),
        )
        snapshot = await get_ai_run(run_id)
        return result, snapshot

    result, snapshot = asyncio.run(_run())

    assert len(create_calls) == 2
    assert result["status"] == "running"
    assert snapshot["run"]["status"] == "running"
    second_command = str(create_calls[1].get("command") or "")
    assert "DESCRIBE TABLE logs.obs_traces_1m" in second_command
    pending = snapshot["run"]["summary_json"].get("pending_user_input")
    assert not isinstance(pending, dict)


def test_execute_ai_run_command_missing_target_identity_enters_waiting_user_input(monkeypatch):
    runtime_service = _build_runtime_service()
    monkeypatch.setattr("api.ai.get_agent_runtime_service", lambda *_args, **_kwargs: runtime_service)

    async def _unexpected_create_command_run(**_kwargs):
        raise AssertionError("create_command_run should not be called when target_identity is missing")

    monkeypatch.setattr("ai.agent_runtime.service.create_command_run", _unexpected_create_command_run)

    async def _run():
        created = await create_ai_run(
            AIRunCreateRequest(
                session_id="sess-missing-target-001",
                question="数据库目标不明确时应先询问用户",
                analysis_context={"analysis_type": "log", "service_name": "query-service"},
            )
        )
        run_id = created["run"]["run_id"]
        result = await execute_ai_run_command(
            run_id,
            AIRunCommandRequest(
                action_id="act-missing-target-001",
                purpose="确认慢查询影响范围",
                title="执行数据库查询",
                command_spec={
                    "tool": "kubectl_clickhouse_query",
                    "args": {
                        "query": "SELECT count() FROM otel_logs LIMIT 10",
                        "timeout_s": 30,
                    },
                },
            ),
        )
        fetched = await get_ai_run(run_id)
        return result, fetched

    result, fetched = asyncio.run(_run())

    assert result["status"] == "blocked"
    assert fetched["run"]["status"] == "running"
    recovery = (result.get("error") or {}).get("recovery") or result.get("recovery") or {}
    assert str((result.get("error") or {}).get("code") or "").strip() == "missing_or_invalid_command_spec"
    assert str(recovery.get("fix_code") or "").strip() == "missing_target_identity"


def test_execute_ai_run_command_unknown_semantics_exceeds_retry_limit_blocks_run(monkeypatch):
    runtime_service = _build_runtime_service()
    monkeypatch.setattr("api.ai.get_agent_runtime_service", lambda *_args, **_kwargs: runtime_service)
    safe_unknown_semantics_command = "echo runtime-check"

    async def _fake_create_command_run(**_kwargs):
        return {
            "status": "permission_required",
            "message": "unknown command semantics from precheck",
            "command_type": "unknown",
            "risk_level": "high",
        }

    monkeypatch.setattr("ai.agent_runtime.service.create_command_run", _fake_create_command_run)

    async def _run():
        created = await create_ai_run(
            AIRunCreateRequest(
                session_id="sess-cmd-unknown-limit-001",
                question="连续两次 unknown 语义应终止",
                analysis_context={"analysis_type": "log", "service_name": "query-service"},
                runtime_options={"unknown_semantics_max_retries": 1},
            )
        )
        run_id = created["run"]["run_id"]
        first = await execute_ai_run_command(
            run_id,
            AIRunCommandRequest(
                action_id="act-unknown-limit-001",
                command=safe_unknown_semantics_command,
                command_spec=_generic_exec_spec(safe_unknown_semantics_command),
                purpose="第一次 unknown，进入用户补充语义",
                title="第一次执行",
            ),
        )
        await continue_ai_run_with_user_input(
            run_id,
            AIRunInputRequest(text="请补充具体命令语义"),
        )
        second = await execute_ai_run_command(
            run_id,
            AIRunCommandRequest(
                action_id="act-unknown-limit-002",
                command=safe_unknown_semantics_command,
                command_spec=_generic_exec_spec(safe_unknown_semantics_command),
                purpose="第二次仍 unknown，应直接终止",
                title="第二次执行",
            ),
        )
        fetched = await get_ai_run(run_id)
        events = await get_ai_run_events(run_id, after_seq=0, limit=80)
        return first, second, fetched, events

    first, second, fetched, events = asyncio.run(_run())

    assert first["status"] == "waiting_user_input"
    assert second["status"] == "blocked"
    assert fetched["run"]["status"] == "blocked"
    assert fetched["run"]["error_code"] == "unknown_semantics_exceeded"
    assert "unknown command semantics from precheck" in str(fetched["run"]["error_detail"])
    event_types = [item["event_type"] for item in events["events"]]
    assert "action_waiting_user_input" in event_types
    assert "run_status_changed" in event_types


def test_run_enforces_single_pending_action(monkeypatch):
    runtime_service = _build_runtime_service()
    monkeypatch.setattr("api.ai.get_agent_runtime_service", lambda *_args, **_kwargs: runtime_service)

    async def _run():
        created = await create_ai_run(
            AIRunCreateRequest(
                session_id="sess-single-pending-001",
                question="执行命令需要审批",
                analysis_context={"analysis_type": "log", "service_name": "query-service"},
            )
        )
        run_id = created["run"]["run_id"]
        runtime_service.request_approval(
            run_id,
            approval_id="apr-single-pending-001",
            title="执行高风险命令",
            reason="需要审批",
            command="kubectl rollout restart deployment/query-service",
            requires_confirmation=True,
            requires_elevation=True,
        )
        try:
            await execute_ai_run_command(
                run_id,
                AIRunCommandRequest(
                    action_id="act-single-pending-001",
                    command="echo should-not-run",
                    command_spec=_generic_exec_spec("echo should-not-run"),
                    purpose="验证串行 pending 约束",
                    title="执行命令",
                ),
            )
        except HTTPException as exc:
            return exc
        raise AssertionError("expected HTTPException")

    error = asyncio.run(_run())

    assert error.status_code == 409
    assert "pending action" in str(error.detail)


def test_interrupt_ai_run_cancels_active_command_and_run(monkeypatch):
    runtime_service = _build_runtime_service()
    monkeypatch.setattr("api.ai.get_agent_runtime_service", lambda *_args, **_kwargs: runtime_service)
    cancel_calls = []

    async def _fake_cancel_command_run(command_run_id: str, timeout_seconds: int = 10):
        cancel_calls.append((command_run_id, timeout_seconds))
        return {"run": {"run_id": command_run_id, "status": "cancelled"}}

    monkeypatch.setattr("ai.agent_runtime.service.cancel_command_run", _fake_cancel_command_run)

    async def _run():
        created = await create_ai_run(
            AIRunCreateRequest(
                session_id="sess-interrupt-001",
                question="执行命令并允许 Esc 中断",
                analysis_context={"analysis_type": "log", "service_name": "query-service"},
            )
        )
        run_id = created["run"]["run_id"]
        runtime_service._update_run_summary(  # noqa: SLF001
            runtime_service.get_run(run_id),
            active_command_run_id="cmdrun-interrupt-001",
        )
        interrupted = await interrupt_ai_run(run_id, AIRunInterruptRequest(reason="user_interrupt_esc"))
        fetched = await get_ai_run(run_id)
        events = await get_ai_run_events(run_id, after_seq=0, limit=50)
        return interrupted, fetched, events

    interrupted, fetched, events = asyncio.run(_run())

    assert cancel_calls[0][0] == "cmdrun-interrupt-001"
    assert interrupted["run"]["status"] == "cancelled"
    assert fetched["run"]["status"] == "cancelled"
    event_types = [item["event_type"] for item in events["events"]]
    assert "run_interrupted" in event_types
    assert "run_cancelled" in event_types

def test_build_followup_request_from_ai_run_enriches_project_knowledge_metadata():
    run = SimpleNamespace(
        question="继续分析 query-service Code:241",
        session_id="sess-knowledge-001",
        conversation_id="conv-knowledge-001",
        context_json={
            "analysis_type": "log",
            "service_name": "query-service",
            "input_text": "ERROR query-service Code:241",
        },
        input_json={"question": "继续分析 query-service Code:241"},
    )

    request = _build_followup_request_from_ai_run(
        run,
        {
            "mode": "followup_analysis",
            "conversation_id": "conv-knowledge-001",
            "history": [{"role": "user", "content": "继续分析 query-service Code:241"}],
        },
    )

    analysis_context = request.analysis_context
    assert analysis_context["knowledge_pack_version"] == "2026-04-14.v2"
    assert analysis_context["knowledge_primary_service"] == "query-service"
    assert analysis_context["knowledge_primary_path"] == "log-ingest-query"
    assert analysis_context["project_knowledge_prompt"]


def test_run_followup_runtime_task_marks_run_failed_when_followup_request_build_crashes(monkeypatch):
    runtime_service = _build_runtime_service()
    run = runtime_service.create_run(
        session_id="sess-followup-crash-001",
        question="继续分析 ai-service follow-up runtime 中断",
        analysis_context={"analysis_type": "log", "service_name": "ai-service"},
        runtime_options={"mode": "followup_analysis", "conversation_id": "conv-followup-crash-001"},
    )

    def _raise_build_failure(_run, _runtime_options):
        raise FileNotFoundError("/docs/superpowers/knowledge/services/ai-service.md")

    monkeypatch.setattr("api.ai._build_followup_request_from_ai_run", _raise_build_failure)

    async def _run_task():
        await _run_followup_runtime_task(
            runtime_service,
            run.run_id,
            {"mode": "followup_analysis", "conversation_id": "conv-followup-crash-001"},
        )
        latest = runtime_service.get_run(run.run_id)
        events = runtime_service.list_events(run.run_id, after_seq=0, limit=100)
        return latest, events

    latest, events = asyncio.run(_run_task())

    assert latest is not None
    assert latest.status == "failed"
    assert latest.error_code == "followup_runtime_failed"
    assert "ai-service.md" in str(latest.error_detail)
    assert latest.summary_json["current_phase"] == "failed"
    assert latest.summary_json["followup_runtime_worker"] == "idle"
    event_types = [item.event_type for item in events]
    assert event_protocol.RUN_FAILED in event_types
