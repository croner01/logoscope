"""
Tests for ai.agent_runtime.command_bridge.
"""

from ai.agent_runtime import event_protocol
from ai.agent_runtime.command_bridge import (
    bridge_exec_run_stream_to_runtime,
    build_approval_required_payload,
)


class DummyRuntimeService:
    def __init__(self):
        self.events = []

    def append_event(self, run_id, event_type, payload=None):
        self.events.append(
            {
                "run_id": run_id,
                "event_type": event_type,
                "payload": payload if isinstance(payload, dict) else {},
            }
        )


def test_build_approval_required_payload_normalizes_precheck():
    payload = build_approval_required_payload(
        tool_call_id="tool-001",
        action_id="act-001",
        command="kubectl rollout restart deployment/query-service",
        purpose="重启 query-service 以恢复流量",
        precheck={
            "status": "elevation_required",
            "message": "write command requires elevation and confirmation",
            "command_type": "repair",
            "risk_level": "high",
            "executor_profile": "toolbox-k8s-mutating",
            "effective_executor_profile": "toolbox-k8s-mutating",
            "dispatch_backend": "template_executor",
            "target_identity": "namespace:islap",
            "requires_confirmation": True,
            "requires_elevation": True,
            "confirmation_ticket": "ticket-001",
        },
    )

    assert payload["tool_call_id"] == "tool-001"
    assert payload["purpose"] == "重启 query-service 以恢复流量"
    assert payload["status"] == "elevation_required"
    assert payload["command_type"] == "repair"
    assert payload["executor_profile"] == "toolbox-k8s-mutating"
    assert payload["effective_executor_profile"] == "toolbox-k8s-mutating"
    assert payload["dispatch_backend"] == "template_executor"
    assert payload["target_identity"] == "namespace:islap"
    assert payload["resolved_target_context"] == {}
    assert payload["confirmation_ticket"] == "ticket-001"


def test_bridge_exec_run_stream_to_runtime_emits_tool_call_events(monkeypatch):
    runtime_service = DummyRuntimeService()

    monkeypatch.setattr(
        "ai.agent_runtime.command_bridge.iter_command_run_stream",
        lambda *args, **kwargs: iter(
            [
                {
                    "event": "command_started",
                    "data": {
                        "command_run_id": "cmdrun-001",
                        "command": "echo hello",
                        "command_type": "query",
                        "risk_level": "low",
                        "executor_profile": "busybox-readonly",
                        "target_identity": "workspace:local",
                        "target_cluster_id": "cluster-dev",
                        "target_namespace": "islap",
                        "target_node_name": "worker-01",
                        "resolved_target_context": {
                            "execution_scope": {
                                "cluster_id": "cluster-dev",
                                "namespace": "islap",
                                "node_name": "worker-01",
                            }
                        },
                        "status": "running",
                    },
                },
                {
                    "event": "command_output_delta",
                    "data": {
                        "command_run_id": "cmdrun-001",
                        "stream": "stdout",
                        "text": "hello\n",
                    },
                },
                {
                    "event": "command_finished",
                    "data": {
                        "command_run_id": "cmdrun-001",
                        "status": "completed",
                        "run": {
                            "status": "completed",
                            "exit_code": 0,
                            "stdout": "hello\n",
                            "stderr": "",
                            "timed_out": False,
                            "output_truncated": False,
                            "duration_ms": 12,
                            "command_type": "query",
                            "risk_level": "low",
                            "executor_profile": "busybox-readonly",
                            "target_identity": "workspace:local",
                            "target_cluster_id": "cluster-dev",
                            "target_namespace": "islap",
                            "target_node_name": "worker-01",
                            "resolved_target_context": {
                                "execution_scope": {
                                    "cluster_id": "cluster-dev",
                                    "namespace": "islap",
                                    "node_name": "worker-01",
                                }
                            },
                        },
                    },
                },
            ]
        ),
    )

    result = bridge_exec_run_stream_to_runtime(
        runtime_service=runtime_service,
        run_id="run-001",
        exec_run_id="cmdrun-001",
        tool_call_id="tool-001",
        title="执行 echo hello",
    )

    event_types = [item["event_type"] for item in runtime_service.events]
    assert event_types == [
        event_protocol.TOOL_CALL_STARTED,
        event_protocol.TOOL_CALL_OUTPUT_DELTA,
        event_protocol.TOOL_CALL_FINISHED,
    ]
    assert result["status"] == "completed"
    assert result["stdout"] == "hello\n"
    assert runtime_service.events[0]["payload"]["executor_profile"] == "busybox-readonly"
    assert runtime_service.events[0]["payload"]["target_node_name"] == "worker-01"
    assert runtime_service.events[-1]["payload"]["target_identity"] == "workspace:local"
    assert runtime_service.events[-1]["payload"]["target_namespace"] == "islap"


def test_bridge_exec_run_stream_to_runtime_accepts_event_envelope_payloads(monkeypatch):
    runtime_service = DummyRuntimeService()

    monkeypatch.setattr(
        "ai.agent_runtime.command_bridge.iter_command_run_stream",
        lambda *args, **kwargs: iter(
            [
                {
                    "event": "command_started",
                    "data": {
                        "event_type": "command_started",
                        "payload": {
                            "command_run_id": "cmdrun-002",
                            "command": "kubectl get pods",
                            "command_type": "query",
                            "risk_level": "low",
                            "command_family": "kubernetes",
                            "approval_policy": "auto_execute",
                            "executor_type": "sandbox_pod",
                            "executor_profile": "toolbox-k8s-readonly",
                            "target_kind": "k8s_cluster",
                            "target_identity": "namespace:islap",
                            "effective_executor_type": "sandbox_pod",
                            "effective_executor_profile": "toolbox-k8s-readonly",
                            "dispatch_backend": "template_executor",
                            "dispatch_mode": "remote_template",
                            "dispatch_reason": "configured",
                            "status": "running",
                        },
                    },
                },
                {
                    "event": "command_finished",
                    "data": {
                        "event_type": "command_finished",
                        "payload": {
                            "command_run_id": "cmdrun-002",
                            "status": "failed",
                            "run": {
                                "status": "failed",
                                "exit_code": 127,
                                "stdout": "",
                                "stderr": "command not found",
                                "timed_out": False,
                                "output_truncated": False,
                                "duration_ms": 46,
                                "command_type": "repair",
                                "risk_level": "high",
                                "command_family": "kubernetes",
                                "approval_policy": "elevation_required",
                                "executor_type": "privileged_sandbox_pod",
                                "executor_profile": "toolbox-k8s-mutating",
                                "target_kind": "k8s_cluster",
                                "target_identity": "namespace:islap",
                                "effective_executor_type": "local_process",
                                "effective_executor_profile": "local-fallback",
                                "dispatch_backend": "local_fallback",
                                "dispatch_mode": "local_process",
                                "dispatch_reason": "executor template not configured; using local fallback",
                            },
                        },
                    },
                },
            ]
        ),
    )

    result = bridge_exec_run_stream_to_runtime(
        runtime_service=runtime_service,
        run_id="run-002",
        exec_run_id="cmdrun-002",
        tool_call_id="tool-002",
        title="执行 kubectl get pods",
    )

    assert result["status"] == "failed"
    assert result["exit_code"] == 127
    assert result["stderr"] == "command not found"
    assert runtime_service.events[0]["payload"]["dispatch_backend"] == "template_executor"
    assert runtime_service.events[-1]["payload"]["dispatch_backend"] == "local_fallback"


def test_bridge_exec_run_stream_to_runtime_maps_sigkill_to_timed_out(monkeypatch):
    runtime_service = DummyRuntimeService()

    monkeypatch.setattr(
        "ai.agent_runtime.command_bridge.iter_command_run_stream",
        lambda *args, **kwargs: iter(
            [
                {
                    "event": "command_finished",
                    "data": {
                        "command_run_id": "cmdrun-timeout-001",
                        "status": "failed",
                        "run": {
                            "command": "kubectl -n islap get pods",
                            "status": "failed",
                            "exit_code": -9,
                            "stdout": "",
                            "stderr": "killed",
                            "timed_out": False,
                        },
                    },
                },
            ]
        ),
    )

    result = bridge_exec_run_stream_to_runtime(
        runtime_service=runtime_service,
        run_id="run-timeout-001",
        exec_run_id="cmdrun-timeout-001",
        tool_call_id="tool-timeout-001",
        title="执行超时命令",
    )

    assert result["status"] == "timed_out"
    assert result["timed_out"] is True
    assert result["command"] == "kubectl -n islap get pods"
    assert runtime_service.events[-1]["payload"]["status"] == "timed_out"


def test_bridge_exec_run_stream_to_runtime_backfills_terminal_from_events(monkeypatch):
    runtime_service = DummyRuntimeService()
    captured = {}

    monkeypatch.setattr(
        "ai.agent_runtime.command_bridge.iter_command_run_stream",
        lambda *args, **kwargs: iter(
            [
                {
                    "event": "command_started",
                    "data": {
                        "seq": 1,
                        "command_run_id": "cmdrun-backfill-001",
                        "command": "echo backfill",
                        "status": "running",
                    },
                },
                {
                    "event": "command_output_delta",
                    "data": {
                        "seq": 2,
                        "command_run_id": "cmdrun-backfill-001",
                        "stream": "stdout",
                        "text": "partial\n",
                    },
                },
            ]
        ),
    )

    def _fake_list_events(run_id: str, *, after_seq: int = 0, limit: int = 200, timeout_seconds: int = 10):
        captured["run_id"] = run_id
        captured["after_seq"] = after_seq
        captured["limit"] = limit
        captured["timeout_seconds"] = timeout_seconds
        return {
            "events": [
                {
                    "seq": 3,
                    "event_type": "command_finished",
                    "payload": {
                        "command_run_id": "cmdrun-backfill-001",
                        "status": "completed",
                        "run": {
                            "run_id": "cmdrun-backfill-001",
                            "status": "completed",
                            "exit_code": 0,
                            "stdout": "partial\nfinal\n",
                            "stderr": "",
                            "timed_out": False,
                            "output_truncated": False,
                        },
                    },
                }
            ]
        }

    monkeypatch.setattr("ai.agent_runtime.command_bridge.list_command_run_events_sync", _fake_list_events)
    monkeypatch.setattr(
        "ai.agent_runtime.command_bridge.get_command_run_sync",
        lambda *_args, **_kwargs: {"run": {"run_id": "cmdrun-backfill-001", "status": "completed", "exit_code": 0}},
    )

    result = bridge_exec_run_stream_to_runtime(
        runtime_service=runtime_service,
        run_id="run-backfill-001",
        exec_run_id="cmdrun-backfill-001",
        tool_call_id="tool-backfill-001",
        title="回放补齐终态",
    )

    assert captured["run_id"] == "cmdrun-backfill-001"
    assert captured["after_seq"] == 2
    assert result["status"] == "completed"
    assert result["terminal_reconciled_from"] == "events_backfill"
    event_types = [item["event_type"] for item in runtime_service.events]
    assert event_types[-1] == event_protocol.TOOL_CALL_FINISHED


def test_bridge_exec_run_stream_to_runtime_recovers_terminal_from_snapshot(monkeypatch):
    runtime_service = DummyRuntimeService()

    monkeypatch.setattr(
        "ai.agent_runtime.command_bridge.iter_command_run_stream",
        lambda *args, **kwargs: iter(
            [
                {
                    "event": "command_started",
                    "data": {
                        "seq": 1,
                        "command_run_id": "cmdrun-snapshot-001",
                        "command": "echo snapshot",
                        "status": "running",
                    },
                },
            ]
        ),
    )
    monkeypatch.setattr(
        "ai.agent_runtime.command_bridge.list_command_run_events_sync",
        lambda *_args, **_kwargs: {"events": []},
    )
    monkeypatch.setattr(
        "ai.agent_runtime.command_bridge.get_command_run_sync",
        lambda *_args, **_kwargs: {
            "run": {
                "run_id": "cmdrun-snapshot-001",
                "status": "failed",
                "command": "echo snapshot",
                "purpose": "snapshot recover",
                "exit_code": 2,
                "stdout": "",
                "stderr": "snapshot failed",
                "timed_out": False,
            }
        },
    )

    result = bridge_exec_run_stream_to_runtime(
        runtime_service=runtime_service,
        run_id="run-snapshot-001",
        exec_run_id="cmdrun-snapshot-001",
        tool_call_id="tool-snapshot-001",
        title="snapshot 终态恢复",
    )

    assert result["status"] == "failed"
    assert result["exit_code"] == 2
    assert result["terminal_reconciled_from"] == "run_snapshot"
    assert runtime_service.events[-1]["event_type"] == event_protocol.TOOL_CALL_FINISHED
