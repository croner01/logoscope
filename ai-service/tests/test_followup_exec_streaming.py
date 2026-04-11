"""
Tests for follow-up readonly auto-exec streaming bridge.
"""

import asyncio

import pytest

from ai.agent_runtime.exec_client import ExecServiceClientError
from ai.followup_orchestration_helpers import (
    _build_non_executable_command_templates,
    _emit_followup_event,
    _run_followup_auto_exec_react_loop,
    _run_followup_readonly_auto_exec,
    _select_followup_react_iteration_actions,
    _summarize_iteration_actions,
)


def test_summarize_iteration_actions_repairs_glued_kubectl_logs_namespace():
    summary = _summarize_iteration_actions(
        [
            {
                "title": "kubectl logs-nislap --tail=100 -l app=query-service | grep -i error",
            }
        ]
    )
    assert summary == "kubectl logs -n islap --tail=100 -l app=query-service | grep -i error"


def test_non_executable_templates_use_anchor_window_when_available():
    templates = _build_non_executable_command_templates(
        [
            {
                "id": "act-template-log-001",
                "title": "补齐 trace 相关日志证据",
                "purpose": "查询日志",
                "executable": False,
            },
            {
                "id": "act-template-sql-001",
                "title": "检查 clickhouse 慢查询",
                "purpose": "query_log",
                "executable": False,
            },
        ],
        analysis_context={
            "namespace": "islap",
            "service_name": "query-service",
            "source_log_timestamp": "2026-04-11T13:03:33Z",
            "request_flow_window_minutes": 5,
        },
        max_items=3,
    )
    assert any("--since-time=2026-04-11T12:58:33Z" in item for item in templates)
    assert any("toDateTime64('2026-04-11T12:58:33Z', 9, 'UTC')" in item for item in templates)
    assert any("toDateTime64('2026-04-11T13:08:33Z', 9, 'UTC')" in item for item in templates)


def test_followup_readonly_auto_exec_streams_exec_runtime_events(monkeypatch):
    monkeypatch.setenv("AI_FOLLOWUP_AUTO_EXEC_READONLY_ENABLED", "true")
    monkeypatch.setenv("AI_FOLLOWUP_AUTO_EXEC_READONLY_MAX_ACTIONS", "1")
    monkeypatch.setenv("AI_FOLLOWUP_COMMAND_EXEC_ENABLED", "true")

    events = []

    async def _emit(event_name: str, payload: dict):
        events.append((event_name, payload))

    async def _fake_precheck_command(**_kwargs):
        return {
            "status": "ok",
            "command": "echo stream-ok",
            "command_type": "query",
            "risk_level": "low",
            "requires_write_permission": False,
            "requires_elevation": False,
            "dispatch_requires_template": False,
            "dispatch_degraded": False,
        }

    async def _fake_create_command_run(**_kwargs):
        return {
            "run": {
                "run_id": "cmdrun-001",
                "status": "running",
            }
        }

    monkeypatch.setattr("ai.followup_orchestration_helpers.precheck_command", _fake_precheck_command)
    monkeypatch.setattr("ai.followup_orchestration_helpers.create_command_run", _fake_create_command_run)
    monkeypatch.setattr(
        "ai.followup_orchestration_helpers.iter_command_run_stream",
        lambda *_args, **_kwargs: iter(
            [
                {
                    "event": "command_started",
                    "data": {
                        "command_run_id": "cmdrun-001",
                    },
                },
                {
                    "event": "command_output_delta",
                    "data": {
                        "command_run_id": "cmdrun-001",
                        "stream": "stdout",
                        "text": "stream-ok\n",
                    },
                },
                {
                    "event": "command_finished",
                    "data": {
                        "command_run_id": "cmdrun-001",
                        "status": "completed",
                        "run": {
                            "command_run_id": "cmdrun-001",
                            "status": "completed",
                            "exit_code": 0,
                            "stdout": "stream-ok\n",
                            "stderr": "",
                            "duration_ms": 8,
                        },
                    },
                },
            ]
        ),
    )

    async def _run():
        return await _run_followup_readonly_auto_exec(
            session_id="sess-stream-001",
            message_id="msg-stream-001",
            actions=[
                {
                    "id": "act-stream-001",
                    "command": "echo stream-ok",
                    "command_spec": {
                        "tool": "generic_exec",
                        "args": {
                            "command_argv": ["echo", "stream-ok"],
                            "target_kind": "runtime_node",
                            "target_identity": "runtime:local",
                            "timeout_s": 20,
                        },
                    },
                    "command_type": "query",
                    "risk_level": "low",
                    "executable": True,
                }
            ],
            run_blocking=None,
            event_callback=_emit,
            logger=None,
        )

    observations = asyncio.run(_run())

    assert len(observations) == 1
    assert observations[0]["status"] == "executed"
    assert observations[0]["auto_executed"] is True
    assert observations[0]["command_run_id"] == "cmdrun-001"
    thought_events = [payload for event_name, payload in events if event_name == "thought"]
    assert thought_events
    assert thought_events[0]["title"] == "执行前计划"
    assert "echo stream-ok" in str(thought_events[0]["detail"])
    assert [payload["status"] for event_name, payload in events if event_name == "observation"] == [
        "running",
        "running",
        "executed",
    ]
    stream_updates = [payload for event_name, payload in events if event_name == "observation" and payload.get("stream") == "stdout"]
    assert stream_updates
    assert stream_updates[0]["text"] == "stream-ok\n"


def test_followup_readonly_auto_exec_stream_envelope_backfills_terminal_run(monkeypatch):
    monkeypatch.setenv("AI_FOLLOWUP_AUTO_EXEC_READONLY_ENABLED", "true")
    monkeypatch.setenv("AI_FOLLOWUP_AUTO_EXEC_READONLY_MAX_ACTIONS", "1")
    monkeypatch.setenv("AI_FOLLOWUP_COMMAND_EXEC_ENABLED", "true")

    events = []

    async def _emit(event_name: str, payload: dict):
        events.append((event_name, payload))

    async def _fake_precheck_command(**_kwargs):
        return {
            "status": "ok",
            "command": "echo stream-envelope",
            "command_type": "query",
            "risk_level": "low",
            "requires_write_permission": False,
            "requires_elevation": False,
            "dispatch_requires_template": False,
            "dispatch_degraded": False,
        }

    async def _fake_create_command_run(**_kwargs):
        return {"run": {"run_id": "cmdrun-envelope-001", "status": "running"}}

    async def _fake_get_command_run(_run_id: str, timeout_seconds: int = 10):
        _ = timeout_seconds
        return {
            "run": {
                "run_id": "cmdrun-envelope-001",
                "command_run_id": "cmdrun-envelope-001",
                "status": "completed",
                "exit_code": 0,
                "stdout": "stream-envelope\n",
                "stderr": "",
                "duration_ms": 7,
                "timed_out": False,
            }
        }

    monkeypatch.setattr("ai.followup_orchestration_helpers.precheck_command", _fake_precheck_command)
    monkeypatch.setattr("ai.followup_orchestration_helpers.create_command_run", _fake_create_command_run)
    monkeypatch.setattr("ai.followup_orchestration_helpers.get_command_run", _fake_get_command_run)
    monkeypatch.setattr(
        "ai.followup_orchestration_helpers.iter_command_run_stream",
        lambda *_args, **_kwargs: iter(
            [
                {
                    "event": "command_started",
                    "data": {
                        "event_type": "command_started",
                        "payload": {
                            "command_run_id": "cmdrun-envelope-001",
                        },
                    },
                },
                {
                    "event": "command_output_delta",
                    "data": {
                        "event_type": "command_output_delta",
                        "payload": {
                            "command_run_id": "cmdrun-envelope-001",
                            "stream": "stdout",
                            "text": "stream-envelope\n",
                            "output_truncated": False,
                        },
                    },
                },
            ]
        ),
    )

    async def _run():
        return await _run_followup_readonly_auto_exec(
            session_id="sess-stream-envelope-001",
            message_id="msg-stream-envelope-001",
            actions=[
                {
                    "id": "act-stream-envelope-001",
                    "command": "echo stream-envelope",
                    "command_spec": {
                        "tool": "generic_exec",
                        "args": {
                            "command_argv": ["echo", "stream-envelope"],
                            "target_kind": "runtime_node",
                            "target_identity": "runtime:local",
                            "timeout_s": 20,
                        },
                    },
                    "command_type": "query",
                    "risk_level": "low",
                    "executable": True,
                }
            ],
            run_blocking=None,
            event_callback=_emit,
            logger=None,
        )

    observations = asyncio.run(_run())
    assert len(observations) == 1
    assert observations[0]["status"] == "executed"
    assert observations[0]["command_run_id"] == "cmdrun-envelope-001"
    assert [payload["status"] for event_name, payload in events if event_name == "observation"] == [
        "running",
        "running",
        "executed",
    ]
    stream_updates = [payload for event_name, payload in events if event_name == "observation" and payload.get("stream") == "stdout"]
    assert stream_updates
    assert stream_updates[0]["text"] == "stream-envelope\n"


def test_followup_readonly_auto_exec_emits_failed_observation_when_stream_missing_terminal(monkeypatch):
    monkeypatch.setenv("AI_FOLLOWUP_AUTO_EXEC_READONLY_ENABLED", "true")
    monkeypatch.setenv("AI_FOLLOWUP_AUTO_EXEC_READONLY_MAX_ACTIONS", "1")
    monkeypatch.setenv("AI_FOLLOWUP_COMMAND_EXEC_ENABLED", "true")

    events = []

    async def _emit(event_name: str, payload: dict):
        events.append((event_name, payload))

    async def _fake_precheck_command(**_kwargs):
        return {
            "status": "ok",
            "command": "echo stream-missing-terminal",
            "command_type": "query",
            "risk_level": "low",
            "requires_write_permission": False,
            "requires_elevation": False,
            "dispatch_requires_template": False,
            "dispatch_degraded": False,
        }

    async def _fake_create_command_run(**_kwargs):
        return {"run": {"run_id": "cmdrun-missing-terminal-001", "status": "running"}}

    async def _fake_get_command_run(_run_id: str, timeout_seconds: int = 10):
        _ = timeout_seconds
        raise ExecServiceClientError("stream snapshot unavailable")

    monkeypatch.setattr("ai.followup_orchestration_helpers.precheck_command", _fake_precheck_command)
    monkeypatch.setattr("ai.followup_orchestration_helpers.create_command_run", _fake_create_command_run)
    monkeypatch.setattr("ai.followup_orchestration_helpers.get_command_run", _fake_get_command_run)
    monkeypatch.setattr(
        "ai.followup_orchestration_helpers.iter_command_run_stream",
        lambda *_args, **_kwargs: iter(
            [
                {
                    "event": "command_started",
                    "data": {
                        "event_type": "command_started",
                        "payload": {
                            "command_run_id": "cmdrun-missing-terminal-001",
                        },
                    },
                }
            ]
        ),
    )

    async def _run():
        return await _run_followup_readonly_auto_exec(
            session_id="sess-stream-missing-terminal-001",
            message_id="msg-stream-missing-terminal-001",
            actions=[
                {
                    "id": "act-stream-missing-terminal-001",
                    "command": "echo stream-missing-terminal",
                    "command_spec": {
                        "tool": "generic_exec",
                        "args": {
                            "command_argv": ["echo", "stream-missing-terminal"],
                            "target_kind": "runtime_node",
                            "target_identity": "runtime:local",
                            "timeout_s": 20,
                        },
                    },
                    "command_type": "query",
                    "risk_level": "low",
                    "executable": True,
                }
            ],
            run_blocking=None,
            event_callback=_emit,
            logger=None,
        )

    observations = asyncio.run(_run())
    assert len(observations) == 1
    assert observations[0]["status"] == "failed"
    assert "未收到最终状态" in str(observations[0]["message"])
    assert [payload["status"] for event_name, payload in events if event_name == "observation"] == [
        "running",
        "failed",
    ]


def test_followup_readonly_auto_exec_passes_command_spec_to_create_command_run(monkeypatch):
    monkeypatch.setenv("AI_FOLLOWUP_AUTO_EXEC_READONLY_ENABLED", "true")
    monkeypatch.setenv("AI_FOLLOWUP_AUTO_EXEC_READONLY_MAX_ACTIONS", "1")
    monkeypatch.setenv("AI_FOLLOWUP_COMMAND_EXEC_ENABLED", "true")

    create_calls = []

    async def _fake_precheck_command(**_kwargs):
        return {
            "status": "ok",
            "command": "kubectl -n islap get pods",
            "command_type": "query",
            "risk_level": "low",
            "requires_write_permission": False,
            "requires_elevation": False,
            "dispatch_requires_template": False,
            "dispatch_degraded": False,
        }

    async def _fake_create_command_run(**kwargs):
        create_calls.append(dict(kwargs))
        return {
            "status": "executed",
            "command": "kubectl -n islap get pods",
            "command_type": "query",
            "risk_level": "low",
            "exit_code": 0,
            "duration_ms": 5,
            "stdout": "ok\n",
            "stderr": "",
            "output_truncated": False,
            "timed_out": False,
        }

    monkeypatch.setattr("ai.followup_orchestration_helpers.precheck_command", _fake_precheck_command)
    monkeypatch.setattr("ai.followup_orchestration_helpers.create_command_run", _fake_create_command_run)

    async def _run():
        return await _run_followup_readonly_auto_exec(
            session_id="sess-spec-pass-001",
            message_id="msg-spec-pass-001",
            actions=[
                {
                    "id": "act-spec-pass-001",
                    "command": "kubectl get pods -n islap",
                    "command_spec": {
                        "tool": "generic_exec",
                        "args": {
                            "command_argv": ["kubectl", "get", "pods", "-n", "islap"],
                            "target_kind": "k8s_cluster",
                            "target_identity": "namespace:islap",
                            "timeout_s": 20,
                        },
                    },
                    "command_type": "query",
                    "risk_level": "low",
                    "executable": True,
                }
            ],
            run_blocking=None,
            event_callback=None,
            logger=None,
        )

    observations = asyncio.run(_run())

    assert len(observations) == 1
    assert observations[0]["status"] == "executed"
    assert len(create_calls) == 1
    sent_spec = create_calls[0].get("command_spec")
    assert isinstance(sent_spec, dict)
    assert sent_spec
    assert sent_spec.get("tool") == "generic_exec"


def test_followup_readonly_auto_exec_gate_payload_includes_command_spec(monkeypatch):
    monkeypatch.setenv("AI_FOLLOWUP_AUTO_EXEC_READONLY_ENABLED", "true")
    monkeypatch.setenv("AI_FOLLOWUP_AUTO_EXEC_READONLY_MAX_ACTIONS", "1")
    monkeypatch.setenv("AI_FOLLOWUP_COMMAND_EXEC_ENABLED", "true")

    events = []

    async def _emit(event_name: str, payload: dict):
        events.append((event_name, payload))

    async def _fake_precheck_command(**_kwargs):
        return {
            "status": "confirmation_required",
            "message": "need confirmation",
            "command": "kubectl get pods -n islap",
            "command_type": "query",
            "risk_level": "low",
            "requires_confirmation": True,
            "requires_elevation": False,
        }

    async def _fail_create_command_run(**_kwargs):
        raise AssertionError("create_command_run should not be called when gate requires confirmation")

    monkeypatch.setattr("ai.followup_orchestration_helpers.precheck_command", _fake_precheck_command)
    monkeypatch.setattr("ai.followup_orchestration_helpers.create_command_run", _fail_create_command_run)

    async def _run():
        return await _run_followup_readonly_auto_exec(
            session_id="sess-gate-spec-001",
            message_id="msg-gate-spec-001",
            actions=[
                {
                    "id": "act-gate-spec-001",
                    "command": "kubectl get pods -n islap",
                    "command_spec": {
                        "tool": "generic_exec",
                        "args": {
                            "command_argv": ["kubectl", "get", "pods", "-n", "islap"],
                            "target_kind": "k8s_cluster",
                            "target_identity": "namespace:islap",
                            "timeout_s": 20,
                        },
                    },
                    "command_type": "query",
                    "risk_level": "low",
                    "executable": True,
                }
            ],
            run_blocking=None,
            event_callback=_emit,
            logger=None,
        )

    observations = asyncio.run(_run())

    assert len(observations) == 1
    assert observations[0]["status"] == "confirmation_required"
    assert observations[0]["command_spec_present"] is True
    assert observations[0]["command_spec"]["tool"] == "generic_exec"
    observation_events = [payload for event_name, payload in events if event_name == "observation"]
    assert len(observation_events) == 1
    assert observation_events[0]["command_spec_present"] is True


def test_followup_readonly_auto_exec_returns_failed_when_exec_runtime_unavailable(monkeypatch):
    monkeypatch.setenv("AI_FOLLOWUP_AUTO_EXEC_READONLY_ENABLED", "true")
    monkeypatch.setenv("AI_FOLLOWUP_AUTO_EXEC_READONLY_MAX_ACTIONS", "1")
    monkeypatch.setenv("AI_FOLLOWUP_COMMAND_EXEC_ENABLED", "true")

    events = []

    async def _emit(event_name: str, payload: dict):
        events.append((event_name, payload))

    async def _fake_precheck_command(**_kwargs):
        return {
            "status": "ok",
            "command": "echo fallback-ok",
            "command_type": "query",
            "risk_level": "low",
            "requires_write_permission": False,
            "requires_elevation": False,
            "dispatch_requires_template": False,
            "dispatch_degraded": False,
        }

    async def _fake_create_command_run(**_kwargs):
        raise ExecServiceClientError("exec-service unavailable")

    monkeypatch.setattr("ai.followup_orchestration_helpers.precheck_command", _fake_precheck_command)
    monkeypatch.setattr("ai.followup_orchestration_helpers.create_command_run", _fake_create_command_run)

    async def _run():
        return await _run_followup_readonly_auto_exec(
            session_id="sess-fallback-001",
            message_id="msg-fallback-001",
            actions=[
                {
                    "id": "act-fallback-001",
                    "command": "echo fallback-ok",
                    "command_spec": {
                        "tool": "generic_exec",
                        "args": {
                            "command_argv": ["echo", "fallback-ok"],
                            "target_kind": "runtime_node",
                            "target_identity": "runtime:local",
                            "timeout_s": 20,
                        },
                    },
                    "command_type": "query",
                    "risk_level": "low",
                    "executable": True,
                }
            ],
            run_blocking=None,
            event_callback=_emit,
            logger=None,
        )

    observations = asyncio.run(_run())

    assert len(observations) == 1
    assert observations[0]["status"] == "failed"
    assert observations[0]["auto_executed"] is False
    assert "exec-service" in str(observations[0]["message"])
    thought_events = [payload for event_name, payload in events if event_name == "thought"]
    assert thought_events
    assert thought_events[0]["title"] == "执行前计划"
    observation_events = [payload for event_name, payload in events if event_name == "observation"]
    assert len(observation_events) == 1
    assert observation_events[0]["status"] == "failed"


def test_followup_readonly_auto_exec_allows_clickhouse_select_query(monkeypatch):
    monkeypatch.setenv("AI_FOLLOWUP_AUTO_EXEC_READONLY_ENABLED", "true")
    monkeypatch.setenv("AI_FOLLOWUP_AUTO_EXEC_READONLY_MAX_ACTIONS", "1")
    monkeypatch.setenv("AI_FOLLOWUP_COMMAND_EXEC_ENABLED", "true")

    events = []

    async def _emit(event_name: str, payload: dict):
        events.append((event_name, payload))

    async def _fake_precheck_command(**_kwargs):
        return {
            "status": "ok",
            "command": 'clickhouse-client --query "SELECT 1"',
            "command_type": "query",
            "risk_level": "low",
            "requires_write_permission": False,
            "requires_elevation": False,
            "dispatch_requires_template": False,
            "dispatch_degraded": False,
        }

    async def _fake_create_command_run(**_kwargs):
        return {
            "status": "executed",
            "command": 'clickhouse-client --query "SELECT 1"',
            "command_type": "query",
            "risk_level": "low",
            "exit_code": 0,
            "duration_ms": 6,
            "stdout": "1\n",
            "stderr": "",
            "output_truncated": False,
            "timed_out": False,
        }

    monkeypatch.setattr("ai.followup_orchestration_helpers.precheck_command", _fake_precheck_command)
    monkeypatch.setattr("ai.followup_orchestration_helpers.create_command_run", _fake_create_command_run)

    async def _run():
        return await _run_followup_readonly_auto_exec(
            session_id="sess-clickhouse-001",
            message_id="msg-clickhouse-001",
            actions=[
                {
                    "id": "act-clickhouse-001",
                    "command": 'clickhouse-client --query "SELECT 1"',
                    "command_spec": {
                        "tool": "kubectl_clickhouse_query",
                        "args": {
                            "query": "SELECT 1",
                            "namespace": "islap",
                            "pod_name": "clickhouse-0",
                            "target_kind": "clickhouse_cluster",
                            "target_identity": "database:default",
                            "timeout_s": 30,
                        },
                    },
                    "command_type": "query",
                    "risk_level": "low",
                    "executable": True,
                }
            ],
            run_blocking=None,
            event_callback=_emit,
            logger=None,
        )

    observations = asyncio.run(_run())

    assert len(observations) == 1
    assert observations[0]["status"] == "executed"
    assert observations[0]["auto_executed"] is False
    assert observations[0]["command"] == 'clickhouse-client --query "SELECT 1"'
    assert "1" in str(observations[0]["stdout"])
    thought_events = [payload for event_name, payload in events if event_name == "thought"]
    assert thought_events
    assert thought_events[0]["title"] == "执行前计划"
    assert "SELECT 1" in str(thought_events[0]["detail"])
    observation_events = [payload for event_name, payload in events if event_name == "observation"]
    assert len(observation_events) == 1
    assert observation_events[0]["status"] == "executed"


def test_followup_readonly_auto_exec_blocks_invalid_command_spec_before_precheck(monkeypatch):
    monkeypatch.setenv("AI_FOLLOWUP_AUTO_EXEC_READONLY_ENABLED", "true")
    monkeypatch.setenv("AI_FOLLOWUP_AUTO_EXEC_READONLY_MAX_ACTIONS", "1")
    monkeypatch.setenv("AI_FOLLOWUP_COMMAND_EXEC_ENABLED", "true")

    precheck_calls = []

    async def _fake_precheck_command(**kwargs):
        precheck_calls.append(kwargs)
        raise AssertionError("precheck_command should not be called when command_spec is invalid")

    async def _fake_create_command_run(**_kwargs):
        raise AssertionError("create_command_run should not be called when command_spec is invalid")

    monkeypatch.setattr("ai.followup_orchestration_helpers.precheck_command", _fake_precheck_command)
    monkeypatch.setattr("ai.followup_orchestration_helpers.create_command_run", _fake_create_command_run)

    async def _run():
        return await _run_followup_readonly_auto_exec(
            session_id="sess-sql-recover-001",
            message_id="msg-sql-recover-001",
            actions=[
                {
                    "id": "act-sql-recover-001",
                    "title": "获取表结构",
                    "purpose": "验证表结构",
                    "command_type": "query",
                    "risk_level": "low",
                    "executable": True,
                    "command_spec": {
                        "tool": "kubectl_clickhouse_query",
                        "args": {
                            "query": "EXPLAINPIPELINE SELECT service_nameASsource_service FROM logs.tracesPREWHERE timestamp > now() - INTERVAL1HOUR",
                            "target_kind": "clickhouse_cluster",
                            "target_identity": "database:logs",
                            "timeout_s": 30,
                        },
                    },
                }
            ],
            run_blocking=None,
            event_callback=None,
            logger=None,
        )

    observations = asyncio.run(_run())
    assert len(observations) == 1
    assert observations[0]["status"] == "semantic_incomplete"
    semantic_message = str(observations[0]["message"])
    assert "glued_sql_tokens" in semantic_message or "unsupported_clickhouse_readonly_query" in semantic_message
    assert precheck_calls == []


def test_followup_readonly_auto_exec_requires_command_spec(monkeypatch):
    monkeypatch.setenv("AI_FOLLOWUP_AUTO_EXEC_READONLY_ENABLED", "true")
    monkeypatch.setenv("AI_FOLLOWUP_AUTO_EXEC_READONLY_MAX_ACTIONS", "1")
    monkeypatch.setenv("AI_FOLLOWUP_COMMAND_EXEC_ENABLED", "true")

    async def _fake_precheck_command(**_kwargs):
        raise AssertionError("precheck_command should not run without command_spec")

    async def _fake_create_command_run(**_kwargs):
        raise AssertionError("create_command_run should not run without command_spec")

    monkeypatch.setattr("ai.followup_orchestration_helpers.precheck_command", _fake_precheck_command)
    monkeypatch.setattr("ai.followup_orchestration_helpers.create_command_run", _fake_create_command_run)

    async def _run():
        return await _run_followup_readonly_auto_exec(
            session_id="sess-spec-required-001",
            message_id="msg-spec-required-001",
            actions=[
                {
                    "id": "act-spec-required-001",
                    "command": "kubectl get pods -n islap",
                    "command_type": "query",
                    "risk_level": "low",
                    "executable": True,
                }
            ],
            run_blocking=None,
            event_callback=None,
            logger=None,
        )

    observations = asyncio.run(_run())
    assert len(observations) == 1
    assert observations[0]["status"] == "semantic_incomplete"
    assert "command_spec is required for readonly auto-exec" in str(observations[0]["message"])


def test_emit_followup_event_re_raises_runtime_pause_signal():
    class _RuntimePauseForPendingAction(RuntimeError):
        is_runtime_pause_signal = True

    async def _raise_pause(_event_name: str, _payload: dict):
        raise _RuntimePauseForPendingAction("pause for pending action")

    with pytest.raises(_RuntimePauseForPendingAction):
        asyncio.run(_emit_followup_event(_raise_pause, "observation", {"status": "confirmation_required"}))


def test_followup_readonly_auto_exec_skips_already_executed_command(monkeypatch):
    monkeypatch.setenv("AI_FOLLOWUP_AUTO_EXEC_READONLY_ENABLED", "true")
    monkeypatch.setenv("AI_FOLLOWUP_AUTO_EXEC_READONLY_MAX_ACTIONS", "1")
    monkeypatch.setenv("AI_FOLLOWUP_COMMAND_EXEC_ENABLED", "true")

    async def _fake_precheck_command(**_kwargs):
        return {
            "status": "ok",
            "command": "echo stream-ok",
            "command_type": "query",
            "risk_level": "low",
            "requires_write_permission": False,
            "requires_elevation": False,
            "dispatch_requires_template": False,
            "dispatch_degraded": False,
        }

    async def _fail_create_command_run(**_kwargs):
        raise AssertionError("create_command_run should not be called for duplicated executed command")

    monkeypatch.setattr("ai.followup_orchestration_helpers.precheck_command", _fake_precheck_command)
    monkeypatch.setattr("ai.followup_orchestration_helpers.create_command_run", _fail_create_command_run)

    async def _run():
        executed = {"echo stream-ok"}
        observations = await _run_followup_readonly_auto_exec(
            session_id="sess-stream-dup-001",
            message_id="msg-stream-dup-001",
            actions=[
                {
                    "id": "act-stream-dup-001",
                    "command": "echo stream-ok",
                    "command_spec": {
                        "tool": "generic_exec",
                        "args": {
                            "command_argv": ["echo", "stream-ok"],
                            "target_kind": "runtime_node",
                            "target_identity": "runtime:local",
                            "timeout_s": 20,
                        },
                    },
                    "command_type": "query",
                    "risk_level": "low",
                    "executable": True,
                }
            ],
            run_blocking=None,
            executed_commands=executed,
            event_callback=None,
            logger=None,
        )
        return observations

    observations = asyncio.run(_run())

    assert len(observations) == 1
    assert observations[0]["status"] == "skipped"
    assert "同一 run 已执行过该命令" in str(observations[0]["message"])
    assert observations[0]["reason_code"] == "duplicate_skipped"


def test_select_followup_react_iteration_actions_does_not_retry_duplicate_skipped():
    actions = [
        {
            "id": "act-skip-dup-001",
            "command": "echo dup",
            "command_type": "query",
            "executable": True,
        },
        {
            "id": "act-skip-backend-001",
            "command": "echo backend",
            "command_type": "query",
            "executable": True,
        },
    ]
    observations = [
        {
            "action_id": "act-skip-dup-001",
            "command": "echo dup",
            "status": "skipped",
            "reason_code": "duplicate_skipped",
        },
        {
            "action_id": "act-skip-backend-001",
            "command": "echo backend",
            "status": "skipped",
            "reason_code": "backend_unready",
        },
    ]

    selected = _select_followup_react_iteration_actions(
        actions=actions,
        action_observations=observations,
        retry_per_command=1,
    )
    selected_commands = [str((item or {}).get("command") or "") for item in selected]
    assert "echo dup" not in selected_commands
    assert "echo backend" in selected_commands


def test_followup_react_loop_first_round_filters_non_executable_actions(monkeypatch):
    monkeypatch.setenv("AI_FOLLOWUP_REACT_MAX_ITERATIONS", "1")

    captured_action_ids = []

    async def _fake_run_followup_readonly_auto_exec(**kwargs):
        action_ids = [
            str((item or {}).get("id") or "").strip()
            for item in (kwargs.get("actions") or [])
            if isinstance(item, dict)
        ]
        captured_action_ids.append(action_ids)
        return [
            {
                "action_id": "act-query",
                "status": "executed",
                "exit_code": 0,
                "command": "kubectl -n islap get pods",
            }
        ]

    monkeypatch.setattr(
        "ai.followup_orchestration_helpers._run_followup_readonly_auto_exec",
        _fake_run_followup_readonly_auto_exec,
    )

    async def _run():
        return await _run_followup_auto_exec_react_loop(
            session_id="sess-react-001",
            message_id="msg-react-001",
            actions=[
                {
                    "id": "act-invalid",
                    "command": "kubectl exec-nislapclickhouse-pod --query \"EXPLAINPLANSELECT 1\"",
                    "command_type": "unknown",
                    "executable": False,
                    "reason": "glued_sql_tokens",
                },
                {
                    "id": "act-query",
                    "command": "kubectl -n islap get pods",
                    "command_type": "query",
                    "executable": True,
                },
            ],
            run_blocking=None,
            build_react_loop_fn=lambda **_kwargs: {
                "execute": {"observed_actions": 1, "executed_success": 1, "executed_failed": 0},
                "observe": {"confidence": 0.9, "unresolved_actions": 0},
                "replan": {"needed": False, "next_actions": []},
                "summary": "ok",
            },
            allow_auto_exec_readonly=True,
            executed_commands=set(),
            initial_evidence_gaps=[],
            initial_summary="",
            emit_iteration_thoughts=False,
            event_callback=None,
            logger=None,
        )

    result = asyncio.run(_run())
    assert captured_action_ids == [["act-query"]]
    assert len(result["action_observations"]) == 1
    assert result["action_observations"][0]["action_id"] == "act-query"


def test_followup_react_loop_skips_round_when_no_executable_query_candidates(monkeypatch):
    monkeypatch.setenv("AI_FOLLOWUP_REACT_MAX_ITERATIONS", "1")

    async def _fail_run_followup_readonly_auto_exec(**_kwargs):
        raise AssertionError("auto-exec should not run when no executable query candidates exist")

    monkeypatch.setattr(
        "ai.followup_orchestration_helpers._run_followup_readonly_auto_exec",
        _fail_run_followup_readonly_auto_exec,
    )

    async def _run():
        return await _run_followup_auto_exec_react_loop(
            session_id="sess-react-002",
            message_id="msg-react-002",
            actions=[
                {
                    "id": "act-manual",
                    "command": "kubectl exec-nislapclickhouse-pod --query \"EXPLAINPLANSELECT 1\"",
                    "command_type": "unknown",
                    "executable": False,
                }
            ],
            run_blocking=None,
            build_react_loop_fn=lambda **_kwargs: {
                "execute": {"observed_actions": 0, "executed_success": 0, "executed_failed": 0},
                "observe": {"confidence": 0.0, "unresolved_actions": 1},
                "replan": {"needed": True, "next_actions": ["补充结构化命令"]},
                "summary": "",
            },
            allow_auto_exec_readonly=True,
            executed_commands=set(),
            initial_evidence_gaps=[],
            initial_summary="",
            emit_iteration_thoughts=False,
            event_callback=None,
            logger=None,
        )

    result = asyncio.run(_run())
    assert result["action_observations"] == []


def test_followup_react_loop_emits_explicit_no_candidate_summary_when_gaps_unmatched(monkeypatch):
    monkeypatch.setenv("AI_FOLLOWUP_REACT_MAX_ITERATIONS", "1")

    events = []

    async def _emit(event_name: str, payload: dict):
        events.append((event_name, payload))

    async def _fail_run_followup_readonly_auto_exec(**_kwargs):
        raise AssertionError("auto-exec should not run when no executable query candidates exist")

    monkeypatch.setattr(
        "ai.followup_orchestration_helpers._run_followup_readonly_auto_exec",
        _fail_run_followup_readonly_auto_exec,
    )

    async def _run():
        return await _run_followup_auto_exec_react_loop(
            session_id="sess-react-003",
            message_id="msg-react-003",
            actions=[
                {
                    "id": "act-manual",
                    "command": "",
                    "command_type": "query",
                    "executable": False,
                    "reason": "missing_structured_spec",
                }
            ],
            run_blocking=None,
            build_react_loop_fn=lambda **_kwargs: {
                "execute": {"observed_actions": 0, "executed_success": 0, "executed_failed": 0},
                "observe": {"confidence": 0.0, "unresolved_actions": 1},
                "replan": {"needed": True, "next_actions": ["补充结构化命令"]},
                "summary": "",
            },
            allow_auto_exec_readonly=True,
            executed_commands=set(),
            initial_evidence_gaps=["clickhouse 慢查询时间窗口"],
            initial_summary="",
            emit_iteration_thoughts=True,
            event_callback=_emit,
            logger=None,
        )

    result = asyncio.run(_run())
    assert result["action_observations"] == []
    summary_titles = [
        str(payload.get("title") or "")
        for event_name, payload in events
        if event_name == "thought" and isinstance(payload, dict)
    ]
    summary_details = [
        str(payload.get("detail") or "")
        for event_name, payload in events
        if event_name == "thought" and isinstance(payload, dict)
    ]
    assert any("且暂无可执行候选命令" in item for item in summary_titles)
    assert any("建议先补全并执行" in item for item in summary_details)


def test_followup_react_loop_summary_skips_low_trust_answer_command_template(monkeypatch):
    monkeypatch.setenv("AI_FOLLOWUP_REACT_MAX_ITERATIONS", "1")

    events = []

    async def _emit(event_name: str, payload: dict):
        events.append((event_name, payload))

    async def _fail_run_followup_readonly_auto_exec(**_kwargs):
        raise AssertionError("auto-exec should not run when no executable query candidates exist")

    monkeypatch.setattr(
        "ai.followup_orchestration_helpers._run_followup_readonly_auto_exec",
        _fail_run_followup_readonly_auto_exec,
    )

    async def _run():
        return await _run_followup_auto_exec_react_loop(
            session_id="sess-react-004",
            message_id="msg-react-004",
            actions=[
                {
                    "id": "ans-1",
                    "source": "answer_command",
                    "command": "kubectl get pods -l app=que",
                    "command_type": "query",
                    "executable": False,
                    "reason": "answer_command_requires_structured_action",
                    "title": "查看 query-service pod",
                }
            ],
            run_blocking=None,
            build_react_loop_fn=lambda **_kwargs: {
                "execute": {"observed_actions": 0, "executed_success": 0, "executed_failed": 0},
                "observe": {"confidence": 0.0, "unresolved_actions": 1},
                "replan": {"needed": True, "next_actions": ["补充结构化命令"]},
                "summary": "",
            },
            allow_auto_exec_readonly=True,
            executed_commands=set(),
            initial_evidence_gaps=["query-service pod 状态"],
            initial_summary="",
            emit_iteration_thoughts=True,
            event_callback=_emit,
            logger=None,
        )

    result = asyncio.run(_run())
    assert result["action_observations"] == []
    summary_details = [
        str(payload.get("detail") or "")
        for event_name, payload in events
        if event_name == "thought" and isinstance(payload, dict)
    ]
    merged_detail = "\n".join(summary_details)
    assert "app=que" not in merged_detail
    assert "kubectl -n islap get pods --show-labels" in merged_detail


def test_followup_react_loop_promotes_structured_templates_to_auto_exec_candidates(monkeypatch):
    monkeypatch.setenv("AI_FOLLOWUP_REACT_MAX_ITERATIONS", "1")

    captured_actions = []

    async def _fake_run_followup_readonly_auto_exec(**kwargs):
        safe_actions = [item for item in (kwargs.get("actions") or []) if isinstance(item, dict)]
        captured_actions.extend(safe_actions)
        return [
            {
                "action_id": str(item.get("id") or ""),
                "status": "executed",
                "exit_code": 0,
                "command": str(item.get("command") or ""),
                "auto_executed": True,
            }
            for item in safe_actions
        ]

    monkeypatch.setattr(
        "ai.followup_orchestration_helpers._run_followup_readonly_auto_exec",
        _fake_run_followup_readonly_auto_exec,
    )

    async def _run():
        return await _run_followup_auto_exec_react_loop(
            session_id="sess-react-template-001",
            message_id="msg-react-template-001",
            actions=[
                {
                    "id": "act-manual-template",
                    "title": "查询ClickHouse慢查询日志",
                    "purpose": "获取失败查询样本",
                    "command": "",
                    "command_type": "unknown",
                    "executable": False,
                    "reason": "glued_sql_tokens",
                }
            ],
            analysis_context={"service_name": "query-service", "namespace": "islap"},
            run_blocking=None,
            build_react_loop_fn=lambda **kwargs: {
                "execute": {
                    "observed_actions": len(kwargs.get("action_observations") or []),
                    "executed_success": len(kwargs.get("action_observations") or []),
                    "executed_failed": 0,
                },
                "observe": {"confidence": 0.9, "unresolved_actions": 0},
                "replan": {"needed": False, "next_actions": []},
                "summary": "ok",
            },
            allow_auto_exec_readonly=True,
            executed_commands=set(),
            initial_evidence_gaps=["ClickHouse query_log"],
            initial_summary="",
            emit_iteration_thoughts=False,
            event_callback=None,
            logger=None,
        )

    result = asyncio.run(_run())
    assert captured_actions
    assert any(str(item.get("source") or "") == "template_command" for item in captured_actions)
    assert any(bool(item.get("executable")) for item in captured_actions)
    assert result["action_observations"]
    assert any(str(item.get("source") or "") == "template_command" for item in (result.get("actions") or []))


def test_followup_react_loop_allows_low_signal_template_when_no_other_template(monkeypatch):
    monkeypatch.setenv("AI_FOLLOWUP_REACT_MAX_ITERATIONS", "1")

    captured_actions = []

    async def _fake_run_followup_readonly_auto_exec(**kwargs):
        safe_actions = [item for item in (kwargs.get("actions") or []) if isinstance(item, dict)]
        captured_actions.extend(safe_actions)
        return [
            {
                "action_id": str(item.get("id") or ""),
                "status": "executed",
                "exit_code": 0,
                "command": str(item.get("command") or ""),
                "auto_executed": True,
            }
            for item in safe_actions
        ]

    monkeypatch.setattr(
        "ai.followup_orchestration_helpers._run_followup_readonly_auto_exec",
        _fake_run_followup_readonly_auto_exec,
    )

    async def _run():
        return await _run_followup_auto_exec_react_loop(
            session_id="sess-react-template-low-signal-001",
            message_id="msg-react-template-low-signal-001",
            actions=[
                {
                    "id": "act-manual-low-signal-template",
                    "source": "reflection",
                    "title": "补充基础排查上下文",
                    "purpose": "补充基础目标状态",
                    "command": "",
                    "command_type": "unknown",
                    "executable": False,
                    "reason": "missing_structured_spec",
                }
            ],
            analysis_context={},
            run_blocking=None,
            build_react_loop_fn=lambda **kwargs: {
                "execute": {
                    "observed_actions": len(kwargs.get("action_observations") or []),
                    "executed_success": len(kwargs.get("action_observations") or []),
                    "executed_failed": 0,
                },
                "observe": {"confidence": 0.9, "unresolved_actions": 0},
                "replan": {"needed": False, "next_actions": []},
                "summary": "ok",
            },
            allow_auto_exec_readonly=True,
            executed_commands=set(),
            initial_evidence_gaps=["基础目标状态"],
            initial_summary="",
            emit_iteration_thoughts=False,
            event_callback=None,
            logger=None,
        )

    result = asyncio.run(_run())
    assert captured_actions
    assert any("kubectl -n islap get pods --show-labels" in str(item.get("command") or "") for item in captured_actions)
    assert result["action_observations"]


def test_followup_readonly_auto_exec_requires_permission_for_write_commands_even_when_precheck_ok(monkeypatch):
    monkeypatch.setenv("AI_FOLLOWUP_AUTO_EXEC_READONLY_ENABLED", "true")
    monkeypatch.setenv("AI_FOLLOWUP_AUTO_EXEC_READONLY_MAX_ACTIONS", "1")
    monkeypatch.setenv("AI_FOLLOWUP_COMMAND_EXEC_ENABLED", "true")

    async def _fake_precheck_command(**_kwargs):
        return {
            "status": "ok",
            "message": "",
            "command": "kubectl -n islap delete pod query-service-0",
            "command_type": "repair",
            "risk_level": "high",
            "requires_write_permission": True,
            "requires_elevation": True,
        }

    async def _fail_create_command_run(**_kwargs):
        raise AssertionError("write command should not auto execute before approval")

    monkeypatch.setattr("ai.followup_orchestration_helpers.precheck_command", _fake_precheck_command)
    monkeypatch.setattr("ai.followup_orchestration_helpers.create_command_run", _fail_create_command_run)

    async def _run():
        return await _run_followup_readonly_auto_exec(
            session_id="sess-write-gate-001",
            message_id="msg-write-gate-001",
            actions=[
                {
                    "id": "act-write-gate-001",
                    "command": "kubectl -n islap delete pod query-service-0",
                    "command_spec": {
                        "tool": "generic_exec",
                        "args": {
                            "command_argv": [
                                "kubectl",
                                "-n",
                                "islap",
                                "delete",
                                "pod",
                                "query-service-0",
                            ],
                            "target_kind": "k8s_cluster",
                            "target_identity": "namespace:islap",
                            "timeout_s": 20,
                        },
                    },
                    "command_type": "repair",
                    "risk_level": "high",
                    "executable": True,
                }
            ],
            run_blocking=None,
            event_callback=None,
            logger=None,
        )

    observations = asyncio.run(_run())
    assert len(observations) == 1
    assert observations[0]["status"] == "permission_required"
    assert observations[0]["requires_write_permission"] is True
    assert observations[0]["requires_elevation"] is True
    assert observations[0]["command_spec_present"] is True
