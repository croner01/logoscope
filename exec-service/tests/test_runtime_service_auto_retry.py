"""
Tests for runtime-service safe auto-rewrite retry behavior.
"""

import asyncio
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import core.runtime_service as runtime_service_module


@pytest.fixture(autouse=True)
def _configure_controlled_executor_templates(monkeypatch):
    """自动重试测试统一使用受控模板，避免依赖本地执行回退。"""
    monkeypatch.setenv(
        "EXEC_EXECUTOR_TEMPLATE__BUSYBOX_READONLY",
        "bash -lc {command_quoted}",
    )
    monkeypatch.setenv(
        "EXEC_EXECUTOR_TEMPLATE__BUSYBOX_MUTATING",
        "bash -lc {command_quoted}",
    )


def test_derive_failed_error_detail_prefers_stdout_for_wrapped_http_error():
    detail = runtime_service_module._derive_failed_error_detail(
        "curl: (22) The requested URL returned error: 500\n",
        (
            "Error from server (Forbidden): deployments.apps \"query-service\" is forbidden: "
            "User \"system:serviceaccount:islap:toolbox-gateway\" cannot get resource \"deployments\" "
            "in API group \"apps\" in the namespace \"islap\"\n"
        ),
    )
    assert "Forbidden" in detail
    assert "requested URL returned error: 500" not in detail


def test_derive_failed_error_detail_falls_back_to_stderr_for_non_wrapped_errors():
    detail = runtime_service_module._derive_failed_error_detail(
        "kubectl: command not found\n",
        "some stdout\n",
    )
    assert detail == "kubectl: command not found"


def _create_runtime_run(
    *,
    runtime,
    command: str,
    purpose: str,
    command_type: str = "query",
    risk_level: str = "low",
    command_family: str = "shell",
    approval_policy: str = "auto_execute",
    executor_type: str = "sandbox_pod",
    executor_profile: str = "busybox-readonly",
    target_kind: str = "runtime_workspace",
    target_identity: str = "workspace:local",
):
    return runtime.create_run(
        session_id="sess-retry-001",
        message_id="msg-retry-001",
        action_id="act-retry-001",
        command=command,
        purpose=purpose,
        command_type=command_type,
        risk_level=risk_level,
        command_family=command_family,
        approval_policy=approval_policy,
        executor_type=executor_type,
        executor_profile=executor_profile,
        target_kind=target_kind,
        target_identity=target_identity,
        timeout_seconds=5,
    )


def test_runtime_auto_rewrite_retry_for_readonly_command(monkeypatch):
    monkeypatch.setenv("EXEC_ALLOWED_HEADS", "echo")
    runtime_service_module._exec_runtime_service = None

    async def _run() -> None:
        runtime = runtime_service_module.get_exec_runtime_service()
        created = _create_runtime_run(
            runtime=runtime,
            command="请执行命令：`echo retry-ok`",
            purpose="验证失败后自动纠偏重试",
        )
        run_id = created["run_id"]
        await runtime.wait_for_run(run_id)
        terminal = runtime.get_run(run_id) or {}

        assert terminal["status"] == "completed"
        assert terminal["command"] == "echo retry-ok"
        assert terminal["original_command"] == "请执行命令：`echo retry-ok`"
        assert int(terminal.get("auto_retry_count") or 0) == 1
        assert "retry-ok" in str(terminal.get("stdout") or "")
        assert "auto-rewrite-retry" in str(terminal.get("stderr") or "")

        events = runtime.list_events(run_id, after_seq=0, limit=100)
        event_types = [event.get("event_type") for event in events]
        assert "command_dispatch_resolved" in event_types
        assert "command_started" in event_types
        assert "command_output_delta" in event_types
        assert "command_finished" in event_types

    asyncio.run(_run())


def test_runtime_auto_rewrite_retry_does_not_bypass_write_permission(monkeypatch):
    monkeypatch.setenv("EXEC_ALLOWED_HEADS", "sed")
    runtime_service_module._exec_runtime_service = None

    async def _run() -> None:
        runtime = runtime_service_module.get_exec_runtime_service()
        created = _create_runtime_run(
            runtime=runtime,
            command='请执行命令：`sed -i "s/a/b/" /tmp/retry-write.txt`',
            purpose="验证写命令不会被自动纠偏重试",
            command_type="repair",
            risk_level="high",
            approval_policy="elevation_required",
            executor_type="privileged_sandbox_pod",
            executor_profile="busybox-mutating",
        )
        run_id = created["run_id"]
        await runtime.wait_for_run(run_id)
        terminal = runtime.get_run(run_id) or {}

        assert terminal["status"] == "failed"
        assert int(terminal.get("auto_retry_count") or 0) == 0
        assert terminal["command"] == '请执行命令：`sed -i "s/a/b/" /tmp/retry-write.txt`'
        assert "auto-rewrite-retry" not in str(terminal.get("stderr") or "")

    asyncio.run(_run())


def test_runtime_passes_resolved_target_context_to_dispatch(monkeypatch):
    monkeypatch.setenv("EXEC_ALLOWED_HEADS", "echo")
    runtime_service_module._exec_runtime_service = None
    captured: dict[str, object] = {}

    async def _dispatch_probe(
        *,
        command: str,
        executor_type: str,
        executor_profile: str,
        target_kind: str,
        target_identity: str,
        resolved_target_context=None,
        timeout_seconds: int,
        on_output=None,
        on_process_started=None,
        on_dispatch_resolved=None,
    ):
        captured["resolved_target_context"] = dict(resolved_target_context or {})
        dispatch = {
            "effective_executor_type": executor_type,
            "effective_executor_profile": executor_profile,
            "dispatch_backend": "template_executor",
            "dispatch_mode": "remote_template",
            "dispatch_reason": "dispatch probe",
            "dispatch_template_env": "EXEC_EXECUTOR_TEMPLATE__BUSYBOX_READONLY",
            "dispatch_ready": True,
            "dispatch_degraded": False,
            "target_node_name": "worker-01",
        }
        if on_dispatch_resolved is not None:
            await on_dispatch_resolved(dispatch)
        if on_output is not None:
            await on_output("stdout", "probe-ok\n")
        return {
            "exit_code": 0,
            "timed_out": False,
            "duration_ms": 1,
            "dispatch": dispatch,
        }

    monkeypatch.setattr(runtime_service_module, "dispatch_command", _dispatch_probe)

    async def _run() -> None:
        runtime = runtime_service_module.get_exec_runtime_service()
        created = runtime.create_run(
            session_id="sess-node-001",
            message_id="msg-node-001",
            action_id="act-node-001",
            command="echo node-routing",
            purpose="verify resolved target context forwarding",
            command_type="query",
            risk_level="low",
            command_family="linux",
            approval_policy="auto_execute",
            executor_type="sandbox_pod",
            executor_profile="toolbox-node-readonly",
            target_kind="host_node",
            target_identity="host:worker-01",
            resolved_target_context={
                "target_id": "tgt-host-worker-01",
                "target_kind": "host_node",
                "target_identity": "host:worker-01",
                "metadata": {
                    "cluster_id": "cluster-dev",
                    "node_name": "worker-01",
                    "preferred_executor_profiles": ["toolbox-node-readonly"],
                    "risk_tier": "high",
                },
                "execution_scope": {
                    "cluster_id": "cluster-dev",
                    "node_name": "worker-01",
                    "target_kind": "host_node",
                    "target_identity": "host:worker-01",
                },
            },
            timeout_seconds=5,
        )
        run_id = created["run_id"]
        await runtime.wait_for_run(run_id)
        terminal = runtime.get_run(run_id) or {}

        forwarded = captured.get("resolved_target_context")
        assert isinstance(forwarded, dict)
        assert forwarded.get("target_identity") == "host:worker-01"
        assert terminal["status"] == "completed"
        assert terminal.get("target_node_name") == "worker-01"

    asyncio.run(_run())
