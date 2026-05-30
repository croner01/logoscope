"""
Tests for exec-service streaming APIs.
"""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

from fastapi import HTTPException
import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import core.runtime_service as runtime_service_module
import core.run_store as run_store_module
import core.event_store as event_store_module
from core import ticket_store as ticket_store_module
from core import audit_store as audit_store_module
from core import policy_decision_store as policy_decision_store_module
from api.execute import (
    _build_clickhouse_runtime_preflight_command,
    CommandRunCreateRequest,
    ExecuteRequest,
    create_command_run,
    get_policy_decision,
    get_policy_decisions,
    get_run_replay,
    get_executors,
    execute_command,
    get_audit,
    get_run,
    get_run_events,
    precheck_command,
    PrecheckRequest,
    stream_run,
)


@pytest.fixture(autouse=True)
def _configure_default_controlled_executor_templates(monkeypatch):
    """默认测试环境启用受控执行模板，避免触发已下线的 local fallback。"""
    policy_decision_store_module.clear_policy_decisions()
    monkeypatch.setenv(
        "EXEC_EXECUTOR_TEMPLATE__BUSYBOX_READONLY",
        'python3 -c "import sys; print(sys.argv[1])" {command_quoted}',
    )
    monkeypatch.setenv(
        "EXEC_EXECUTOR_TEMPLATE__TOOLBOX_K8S_READONLY",
        "curl -sS http://toolbox-gateway/exec?cmd={command_quoted}",
    )
    monkeypatch.setenv(
        "EXEC_EXECUTOR_TEMPLATE__TOOLBOX_K8S_MUTATING",
        "curl -sS http://toolbox-gateway/exec?cmd={command_quoted}",
    )
    monkeypatch.setenv(
        "EXEC_EXECUTOR_TEMPLATE__TOOLBOX_CLICKHOUSE_READONLY",
        "curl -sS http://toolbox-gateway/exec?cmd={command_quoted}",
    )
    monkeypatch.setenv(
        "EXEC_EXECUTOR_TEMPLATE__TOOLBOX_HTTP_READONLY",
        "curl -sS http://toolbox-gateway/exec?cmd={command_quoted}",
    )


async def _collect_stream_text(run_id: str) -> str:
    response = await stream_run(run_id, after_seq=0)
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk.decode() if isinstance(chunk, bytes) else str(chunk))
    return "".join(chunks)


def test_stream_run_does_not_cutoff_before_terminal_event(monkeypatch):
    class _FakeRuntime:
        def __init__(self):
            self._queue = asyncio.Queue()
            self._list_calls = 0
            self.unsubscribed = False

        def get_run(self, _run_id):
            return {"run_id": "cmdrun-stream-gap-001", "status": "completed"}

        def list_events(self, _run_id, after_seq=0, limit=5000):
            _ = limit
            self._list_calls += 1
            safe_after = int(after_seq or 0)
            if safe_after < 1:
                return [
                    {
                        "seq": 1,
                        "event_type": "command_output_delta",
                        "payload": {
                            "command_run_id": "cmdrun-stream-gap-001",
                            "stream": "stdout",
                            "text": "partial\n",
                        },
                    }
                ]
            if safe_after < 2 and self._list_calls >= 2:
                return [
                    {
                        "seq": 2,
                        "event_type": "command_finished",
                        "payload": {
                            "command_run_id": "cmdrun-stream-gap-001",
                            "status": "completed",
                            "run": {
                                "run_id": "cmdrun-stream-gap-001",
                                "status": "completed",
                                "exit_code": 0,
                            },
                        },
                    }
                ]
            return []

        def subscribe(self, _run_id):
            return self._queue

        def unsubscribe(self, _run_id, _queue):
            self.unsubscribed = True

    fake_runtime = _FakeRuntime()
    monkeypatch.setattr("api.execute.get_exec_runtime_service", lambda: fake_runtime)

    async def _run() -> str:
        response = await stream_run("cmdrun-stream-gap-001", after_seq=0)
        chunks: list[str] = []
        async for chunk in response.body_iterator:
            chunks.append(chunk.decode() if isinstance(chunk, bytes) else str(chunk))
        return "".join(chunks)

    stream_text = asyncio.run(_run())

    assert "event: command_output_delta" in stream_text
    assert "event: command_finished" in stream_text
    assert fake_runtime.unsubscribed is True


def test_create_run_stream_and_events(monkeypatch):
    monkeypatch.setenv("EXEC_ALLOWED_HEADS", "echo")
    runtime_service_module._exec_runtime_service = None
    audit_store_module.AUDIT_LOGS.clear()

    async def _run() -> None:
        created = await create_command_run(
            CommandRunCreateRequest(
                session_id="sess-001",
                message_id="msg-001",
                action_id="act-001",
                command="echo hello-stream",
                purpose="验证流式执行链路",
                timeout_seconds=5,
            )
        )
        run = created["run"]
        run_id = run["run_id"]
        decision_id = str(run.get("policy_decision_id") or "")
        assert decision_id.startswith("dec-")
        runtime = runtime_service_module.get_exec_runtime_service()
        await runtime.wait_for_run(run_id)
        fetched = await get_run(run_id)
        terminal_run = fetched["run"]
        assert terminal_run["status"] == "completed"
        assert "hello-stream" in terminal_run["stdout"]
        assert terminal_run["dispatch_backend"] == "template_executor"
        assert terminal_run["effective_executor_type"] == "sandbox_pod"
        assert terminal_run["effective_executor_profile"] == "busybox-readonly"
        assert terminal_run["policy_decision_id"] == decision_id

        events_payload = await get_run_events(run_id, after_seq=0, limit=20)
        event_types = [item["event_type"] for item in events_payload["events"]]
        assert "command_dispatch_resolved" in event_types
        assert "command_started" in event_types
        assert "command_output_delta" in event_types
        assert "command_finished" in event_types
        started = next(item for item in events_payload["events"] if item["event_type"] == "command_started")
        assert started["payload"]["policy_decision_id"] == decision_id

        stream_text = await _collect_stream_text(run_id)
        assert "event: command_dispatch_resolved" in stream_text
        assert "event: command_started" in stream_text
        assert "event: command_output_delta" in stream_text
        assert "event: command_finished" in stream_text

        audit_payload = await get_audit(limit=10)
        assert audit_payload["total"] == 1
        assert audit_payload["rows"][0]["run_id"] == run_id
        assert audit_payload["rows"][0]["dispatch_backend"] == "template_executor"
        assert audit_payload["rows"][0]["policy_decision_id"] == decision_id

    asyncio.run(_run())


def test_execute_command_compat_returns_final_payload(monkeypatch):
    monkeypatch.setenv("EXEC_ALLOWED_HEADS", "echo")
    runtime_service_module._exec_runtime_service = None
    audit_store_module.AUDIT_LOGS.clear()

    async def _run() -> None:
        payload = await execute_command(
            ExecuteRequest(
                session_id="sess-compat-001",
                message_id="msg-compat-001",
                action_id="act-compat-001",
                command="echo compatibility",
                purpose="验证兼容 execute 接口",
                timeout_seconds=5,
            )
        )
        assert payload["status"] == "executed"
        assert "compatibility" in payload["stdout"]

        audit_payload = await get_audit(limit=10)
        assert audit_payload["total"] == 1
        assert audit_payload["rows"][0]["command"] == "echo compatibility"

    asyncio.run(_run())


def test_precheck_returns_executor_metadata(monkeypatch):
    monkeypatch.setenv("EXEC_ALLOWED_HEADS", "kubectl")

    async def _run() -> None:
        payload = await precheck_command(
            PrecheckRequest(
                session_id="sess-meta-001",
                message_id="msg-meta-001",
                action_id="act-meta-001",
                command="kubectl -n islap get pods",
            )
        )
        assert payload["status"] == "ok"
        assert payload["command_type"] == "query"
        assert payload["command_family"] == "kubernetes"
        assert payload["executor_profile"] == "toolbox-k8s-readonly"
        assert payload["target_identity"] == "namespace:islap"
        assert payload["effective_executor_profile"] == "toolbox-k8s-readonly"
        assert payload["dispatch_backend"] == "template_executor"
        assert payload["dispatch_requires_template"] is True
        assert payload["dispatch_ready"] is True
        assert payload["dispatch_degraded"] is False
        assert isinstance((payload.get("target_registry") or {}).get("metadata_contract"), dict)
        assert isinstance((payload.get("target_registry") or {}).get("resolved_target_context"), dict)
        assert isinstance(payload.get("resolved_target_context"), dict)
        assert str(payload.get("decision_id") or "").startswith("dec-")
        assert str((payload.get("policy_decision") or {}).get("decision_id") or "") == str(payload.get("decision_id") or "")

    asyncio.run(_run())


def test_precheck_opa_enforced_fail_closed_when_opa_unreachable(monkeypatch):
    monkeypatch.setenv("EXEC_ALLOWED_HEADS", "echo")
    monkeypatch.setenv("EXEC_POLICY_DECISION_MODE", "opa_enforced")
    monkeypatch.setenv("EXEC_POLICY_OPA_URL", "http://127.0.0.1:9/v1/data/runtime/command/v1")
    monkeypatch.setenv("EXEC_POLICY_OPA_TIMEOUT_MS", "20")

    async def _run() -> None:
        payload = await precheck_command(
            PrecheckRequest(
                session_id="sess-opa-enforced-001",
                message_id="msg-opa-enforced-001",
                action_id="act-opa-enforced-001",
                command="echo opa-enforced",
            )
        )
        assert payload["status"] == "permission_required"
        assert payload["approval_policy"] == "deny"
        assert payload["requires_confirmation"] is False
        assert "fail-closed" in str(payload.get("message") or "").lower()
        decision = payload.get("policy_decision") or {}
        assert decision["engine"] == "opa"
        assert decision["result"] == "deny"
        assert decision["mode"] == "opa_enforced"
        assert decision["opa_available"] is False

    asyncio.run(_run())


def test_precheck_opa_shadow_keeps_local_decision_when_opa_unreachable(monkeypatch):
    monkeypatch.setenv("EXEC_ALLOWED_HEADS", "echo")
    monkeypatch.setenv("EXEC_POLICY_DECISION_MODE", "opa_shadow")
    monkeypatch.setenv("EXEC_POLICY_ALLOW_NON_ENFORCED_MODES", "true")
    monkeypatch.setenv("EXEC_POLICY_OPA_URL", "http://127.0.0.1:9/v1/data/runtime/command/v1")
    monkeypatch.setenv("EXEC_POLICY_OPA_TIMEOUT_MS", "20")

    async def _run() -> None:
        payload = await precheck_command(
            PrecheckRequest(
                session_id="sess-opa-shadow-001",
                message_id="msg-opa-shadow-001",
                action_id="act-opa-shadow-001",
                command="echo opa-shadow",
            )
        )
        assert payload["status"] == "ok"
        decision = payload.get("policy_decision") or {}
        assert decision["engine"] == "python-inline"
        assert decision["result"] == "allow"
        assert decision["mode"] == "opa_shadow"
        assert decision["opa_available"] is False

    asyncio.run(_run())


def test_precheck_opa_shadow_falls_back_to_enforced_when_non_enforced_modes_disabled(monkeypatch):
    monkeypatch.setenv("EXEC_ALLOWED_HEADS", "echo")
    monkeypatch.setenv("EXEC_POLICY_DECISION_MODE", "opa_shadow")
    monkeypatch.setenv("EXEC_POLICY_ALLOW_NON_ENFORCED_MODES", "false")
    monkeypatch.setenv("EXEC_POLICY_OPA_URL", "http://127.0.0.1:9/v1/data/runtime/command/v1")
    monkeypatch.setenv("EXEC_POLICY_OPA_TIMEOUT_MS", "20")

    async def _run() -> None:
        payload = await precheck_command(
            PrecheckRequest(
                session_id="sess-opa-shadow-denied-001",
                message_id="msg-opa-shadow-denied-001",
                action_id="act-opa-shadow-denied-001",
                command="echo opa-shadow-disabled",
            )
        )
        assert payload["status"] == "permission_required"
        decision = payload.get("policy_decision") or {}
        assert decision["engine"] == "opa"
        assert decision["result"] == "deny"
        assert decision["mode"] == "opa_enforced"
        assert decision["opa_available"] is False

    asyncio.run(_run())


def test_precheck_manual_required_converts_whitelisted_query_to_confirmation(monkeypatch):
    monkeypatch.setenv("EXEC_ALLOWED_HEADS", "echo")
    monkeypatch.setattr(
        "api.execute.evaluate_policy_decision",
        lambda **_: {
            "result": "manual_required",
            "reason": "unknown target must be manually approved",
            "engine": "opa",
            "package": "runtime.command.v1",
            "mode": "opa_enforced",
            "source": "opa",
            "opa_available": True,
            "opa_result": "manual_required",
            "opa_reason": "unknown target must be manually approved",
            "opa_package": "runtime.command.v1",
            "local_result": "allow",
            "local_reason": "",
        },
    )

    async def _run() -> None:
        payload = await precheck_command(
            PrecheckRequest(
                session_id="sess-manual-required-001",
                message_id="msg-manual-required-001",
                action_id="act-manual-required-001",
                command="echo manual-required",
            )
        )
        assert payload["status"] == "confirmation_required"
        assert payload["approval_policy"] == "manual_required"
        assert payload["requires_confirmation"] is True
        assert payload["requires_elevation"] is False
        assert str(payload.get("confirmation_ticket") or "").startswith("exec-ticket-")
        decision = payload.get("policy_decision") or {}
        assert decision["engine"] == "opa"
        assert decision["result"] == "manual_required"

    asyncio.run(_run())


def test_precheck_manual_required_keeps_write_command_on_elevation_flow(monkeypatch):
    monkeypatch.setenv("EXEC_ALLOWED_HEADS", "kubectl")
    monkeypatch.setattr(
        "api.execute.evaluate_policy_decision",
        lambda **_: {
            "result": "manual_required",
            "reason": "mutating command requires manual review",
            "engine": "opa",
            "package": "runtime.command.v1",
            "mode": "opa_enforced",
            "source": "opa",
            "opa_available": True,
            "opa_result": "manual_required",
            "opa_reason": "mutating command requires manual review",
            "opa_package": "runtime.command.v1",
            "local_result": "elevate",
            "local_reason": "",
        },
    )

    async def _run() -> None:
        payload = await precheck_command(
            PrecheckRequest(
                session_id="sess-manual-required-write-001",
                message_id="msg-manual-required-write-001",
                action_id="act-manual-required-write-001",
                command="kubectl -n islap rollout restart deployment/query-service",
            )
        )
        assert payload["status"] == "elevation_required"
        assert payload["approval_policy"] == "manual_required"
        assert payload["requires_confirmation"] is True
        assert payload["requires_elevation"] is True
        assert str(payload.get("confirmation_ticket") or "").startswith("exec-ticket-")
        decision = payload.get("policy_decision") or {}
        assert decision["engine"] == "opa"
        assert decision["result"] == "manual_required"

    asyncio.run(_run())


def test_precheck_unknown_target_forces_manual_required_even_if_policy_returns_allow(monkeypatch):
    monkeypatch.setenv("EXEC_ALLOWED_HEADS", "echo")
    monkeypatch.setattr(
        "api.execute.classify_command_with_auto_rewrite",
        lambda _command: {
            "supported": True,
            "command": "echo unknown-target",
            "command_type": "query",
            "risk_level": "low",
            "requires_write_permission": False,
            "reason": "synthetic command meta",
            "command_family": "shell",
            "approval_policy": "auto_execute",
            "executor_type": "sandbox_pod",
            "executor_profile": "busybox-readonly",
            "target_kind": "host_node",
            "target_identity": "host:unknown",
        },
    )
    monkeypatch.setattr(
        "api.execute.evaluate_query_whitelist",
        lambda _command, _meta: {"whitelisted": True, "reason": "synthetic whitelist"},
    )

    async def _run() -> None:
        payload = await precheck_command(
            PrecheckRequest(
                session_id="sess-unknown-target-001",
                message_id="msg-unknown-target-001",
                action_id="act-unknown-target-001",
                command="echo unknown-target",
            )
        )
        assert payload["status"] == "confirmation_required"
        assert payload["approval_policy"] == "manual_required"
        assert payload["requires_confirmation"] is True
        assert payload["requires_elevation"] is False
        assert "unknown target" in str(payload.get("message") or "").lower()
        assert str(payload.get("confirmation_ticket") or "").startswith("exec-ticket-")
        decision = payload.get("policy_decision") or {}
        assert decision["result"] == "manual_required"

    asyncio.run(_run())


def test_precheck_target_registry_manual_required_overrides_allow(monkeypatch):
    monkeypatch.setenv("EXEC_ALLOWED_HEADS", "kubectl")
    monkeypatch.setenv("EXEC_TARGET_REGISTRY_MODE", "enforced")
    monkeypatch.setattr(
        "api.execute.classify_command_with_auto_rewrite",
        lambda _command: {
            "supported": True,
            "command": "kubectl -n islap get pods",
            "command_type": "query",
            "risk_level": "low",
            "requires_write_permission": False,
            "reason": "synthetic command meta",
            "command_family": "kubernetes",
            "approval_policy": "auto_execute",
            "executor_type": "sandbox_pod",
            "executor_profile": "toolbox-k8s-readonly",
            "target_kind": "k8s_cluster",
            "target_identity": "namespace:islap",
        },
    )
    monkeypatch.setattr(
        "api.execute.evaluate_query_whitelist",
        lambda _command, _meta: {"whitelisted": True, "reason": "synthetic whitelist"},
    )
    monkeypatch.setattr(
        "api.execute.evaluate_policy_decision",
        lambda **_: {
            "result": "allow",
            "reason": "opa allow",
            "engine": "opa",
            "package": "runtime.command.v1",
            "mode": "opa_enforced",
            "source": "opa",
            "opa_available": True,
            "opa_result": "allow",
            "opa_reason": "opa allow",
            "opa_package": "runtime.command.v1",
            "local_result": "manual_required",
            "local_reason": "target capability mismatch",
        },
    )
    monkeypatch.setattr(
        "api.execute.evaluate_target_registry_gate",
        lambda **_: {
            "enabled": True,
            "mode": "enforced",
            "applied": True,
            "result": "manual_required",
            "reason": "target capability mismatch",
            "target_id": "tgt-k8s-islap",
            "registered": True,
            "status": "active",
            "required_capabilities": ["read_logs"],
            "missing_capabilities": ["read_logs"],
            "matched_capabilities": [],
            "lookup_error": "",
            "resolve_error": "",
        },
    )

    async def _run() -> None:
        payload = await precheck_command(
            PrecheckRequest(
                session_id="sess-target-registry-manual-001",
                message_id="msg-target-registry-manual-001",
                action_id="act-target-registry-manual-001",
                command="kubectl -n islap get pods",
            )
        )
        assert payload["status"] == "confirmation_required"
        assert payload["approval_policy"] == "manual_required"
        assert payload["requires_confirmation"] is True
        assert payload["target_id"] == "tgt-k8s-islap"
        assert payload["target_registry"]["result"] == "manual_required"
        assert payload["target_registry"]["missing_capabilities"] == ["read_logs"]

        decision_payload = await get_policy_decision(str(payload.get("decision_id")))
        decision = decision_payload["decision"]
        target_registry = decision["input_payload"]["target_registry"]
        assert target_registry["mode"] == "enforced"
        assert target_registry["result"] == "manual_required"
        assert target_registry["missing_capabilities"] == ["read_logs"]

    asyncio.run(_run())


def test_precheck_target_registry_unavailable_failsafe_manual_required(monkeypatch):
    monkeypatch.setenv("EXEC_ALLOWED_HEADS", "echo")
    monkeypatch.setenv("EXEC_TARGET_REGISTRY_MODE", "enforced")
    monkeypatch.setattr(
        "api.execute.evaluate_query_whitelist",
        lambda _command, _meta: {"whitelisted": True, "reason": "synthetic whitelist"},
    )
    monkeypatch.setattr(
        "api.execute.evaluate_target_registry_gate",
        lambda **_: {
            "enabled": True,
            "mode": "enforced",
            "applied": True,
            "result": "manual_required",
            "reason": "target registry lookup unavailable: unreachable",
            "target_id": "",
            "registered": False,
            "status": "unknown",
            "required_capabilities": ["shell_read"],
            "missing_capabilities": ["shell_read"],
            "matched_capabilities": [],
            "lookup_error": "unreachable",
            "resolve_error": "",
        },
    )

    async def _run() -> None:
        payload = await precheck_command(
            PrecheckRequest(
                session_id="sess-target-registry-unavailable-001",
                message_id="msg-target-registry-unavailable-001",
                action_id="act-target-registry-unavailable-001",
                command="echo registry-down",
            )
        )
        assert payload["status"] == "confirmation_required"
        assert payload["approval_policy"] == "manual_required"
        assert payload["requires_confirmation"] is True
        assert "registry" in str(payload.get("message") or "").lower()
        assert payload["target_registry"]["lookup_error"] == "unreachable"
        decision = payload.get("policy_decision") or {}
        assert decision["result"] == "manual_required"

    asyncio.run(_run())


def test_precheck_target_registry_allow_keeps_auto_execute(monkeypatch):
    monkeypatch.setenv("EXEC_ALLOWED_HEADS", "kubectl")
    monkeypatch.setenv("EXEC_TARGET_REGISTRY_MODE", "enforced")
    monkeypatch.setattr(
        "api.execute.classify_command_with_auto_rewrite",
        lambda _command: {
            "supported": True,
            "command": "kubectl -n islap get pods",
            "command_type": "query",
            "risk_level": "low",
            "requires_write_permission": False,
            "reason": "synthetic command meta",
            "command_family": "kubernetes",
            "approval_policy": "auto_execute",
            "executor_type": "sandbox_pod",
            "executor_profile": "toolbox-k8s-readonly",
            "target_kind": "k8s_cluster",
            "target_identity": "namespace:islap",
        },
    )
    monkeypatch.setattr(
        "api.execute.evaluate_query_whitelist",
        lambda _command, _meta: {"whitelisted": True, "reason": "synthetic whitelist"},
    )
    monkeypatch.setattr(
        "api.execute.evaluate_policy_decision",
        lambda **_: {
            "result": "allow",
            "reason": "opa allow",
            "engine": "opa",
            "package": "runtime.command.v1",
            "mode": "opa_enforced",
            "source": "opa",
            "opa_available": True,
            "opa_result": "allow",
            "opa_reason": "opa allow",
            "opa_package": "runtime.command.v1",
            "local_result": "allow",
            "local_reason": "",
        },
    )
    monkeypatch.setattr(
        "api.execute.evaluate_target_registry_gate",
        lambda **_: {
            "enabled": True,
            "mode": "enforced",
            "applied": True,
            "result": "allow",
            "reason": "target registered and capabilities matched",
            "target_id": "tgt-k8s-islap",
            "registered": True,
            "status": "active",
            "required_capabilities": ["read_logs"],
            "missing_capabilities": [],
            "matched_capabilities": ["read_logs"],
            "lookup_error": "",
            "resolve_error": "",
        },
    )

    async def _run() -> None:
        payload = await precheck_command(
            PrecheckRequest(
                session_id="sess-target-registry-allow-001",
                message_id="msg-target-registry-allow-001",
                action_id="act-target-registry-allow-001",
                command="kubectl -n islap get pods",
            )
        )
        assert payload["status"] == "ok"
        assert payload["approval_policy"] == "auto_execute"
        assert payload["target_registry"]["result"] == "allow"
        assert payload["target_registry"]["matched_capabilities"] == ["read_logs"]

        decision_payload = await get_policy_decision(str(payload.get("decision_id")))
        decision = decision_payload["decision"]
        target_registry = decision["input_payload"]["target_registry"]
        assert target_registry["registered"] is True
        assert target_registry["result"] == "allow"
        assert target_registry["target_id"] == "tgt-k8s-islap"

    asyncio.run(_run())


def test_precheck_dispatch_recovery_after_target_profile_override_keeps_auto_execute(monkeypatch):
    monkeypatch.setenv("EXEC_ALLOWED_HEADS", "clickhouse-client")
    monkeypatch.setenv("EXEC_TARGET_REGISTRY_MODE", "enforced")
    monkeypatch.setenv("EXEC_POLICY_DECISION_MODE", "local")
    monkeypatch.delenv("EXEC_EXECUTOR_TEMPLATE__TOOLBOX_CLICKHOUSE_READONLY", raising=False)
    monkeypatch.setenv(
        "EXEC_EXECUTOR_TEMPLATE__TOOLBOX_K8S_READONLY",
        "curl -sS http://toolbox-gateway/exec?cmd={command_quoted}",
    )
    monkeypatch.setattr(
        "api.execute.classify_command_with_auto_rewrite",
        lambda _command: {
            "supported": True,
            "command": "clickhouse-client --query \"SELECT 1\"",
            "command_type": "query",
            "risk_level": "low",
            "requires_write_permission": False,
            "reason": "synthetic clickhouse query",
            "command_family": "clickhouse",
            "approval_policy": "auto_execute",
            "executor_type": "sandbox_pod",
            "executor_profile": "toolbox-clickhouse-readonly",
            "target_kind": "clickhouse_cluster",
            "target_identity": "database:default",
        },
    )
    monkeypatch.setattr(
        "api.execute.evaluate_query_whitelist",
        lambda _command, _meta: {"whitelisted": True, "reason": "synthetic whitelist"},
    )
    monkeypatch.setattr(
        "api.execute.evaluate_target_registry_gate",
        lambda **_: {
            "enabled": True,
            "mode": "enforced",
            "applied": True,
            "result": "allow",
            "reason": "target registered and capabilities matched",
            "target_id": "tgt-clickhouse-default",
            "registered": True,
            "status": "active",
            "required_capabilities": ["run_query"],
            "missing_capabilities": [],
            "matched_capabilities": ["run_query"],
            "lookup_error": "",
            "resolve_error": "",
            "metadata_contract": {
                "required_keys": ["cluster_id", "preferred_executor_profiles", "risk_tier"],
                "missing_required_keys": [],
                "metadata": {
                    "cluster_id": "cluster-local",
                    "risk_tier": "high",
                    "preferred_executor_profiles": ["toolbox-k8s-readonly", "toolbox-k8s-mutating"],
                },
                "execution_scope": {
                    "cluster_id": "cluster-local",
                    "target_kind": "clickhouse_cluster",
                    "target_identity": "database:default",
                },
            },
            "resolved_target_context": {
                "target_id": "tgt-clickhouse-default",
                "target_kind": "clickhouse_cluster",
                "target_identity": "database:default",
                "metadata": {
                    "cluster_id": "cluster-local",
                    "risk_tier": "high",
                    "preferred_executor_profiles": ["toolbox-k8s-readonly", "toolbox-k8s-mutating"],
                },
                "execution_scope": {
                    "cluster_id": "cluster-local",
                    "target_kind": "clickhouse_cluster",
                    "target_identity": "database:default",
                },
            },
        },
    )

    async def _run() -> None:
        payload = await precheck_command(
            PrecheckRequest(
                session_id="sess-dispatch-recover-001",
                message_id="msg-dispatch-recover-001",
                action_id="act-dispatch-recover-001",
                command="clickhouse-client --query \"SELECT 1\"",
            )
        )
        assert payload["status"] == "ok"
        assert payload["approval_policy"] == "auto_execute"
        assert payload["dispatch_ready"] is True
        assert payload["dispatch_degraded"] is False
        assert payload["effective_executor_profile"] == "toolbox-k8s-readonly"
        assert payload["dispatch_template_env"] == "EXEC_EXECUTOR_TEMPLATE__TOOLBOX_K8S_READONLY"

        decision_payload = await get_policy_decision(str(payload.get("decision_id")))
        decision = decision_payload["decision"]
        preview = decision["input_payload"]["dispatch_preview"]
        assert preview["dispatch_ready"] is True
        assert preview["dispatch_degraded"] is False
        assert preview["dispatch_template_env"] == "EXEC_EXECUTOR_TEMPLATE__TOOLBOX_K8S_READONLY"

    asyncio.run(_run())


def test_precheck_target_registry_ambiguous_identity_requires_manual(monkeypatch):
    monkeypatch.setenv("EXEC_ALLOWED_HEADS", "kubectl")
    monkeypatch.setenv("EXEC_TARGET_REGISTRY_MODE", "enforced")
    monkeypatch.setattr(
        "api.execute.classify_command_with_auto_rewrite",
        lambda _command: {
            "supported": True,
            "command": "kubectl -n islap get pods",
            "command_type": "query",
            "risk_level": "low",
            "requires_write_permission": False,
            "reason": "synthetic command meta",
            "command_family": "kubernetes",
            "approval_policy": "auto_execute",
            "executor_type": "sandbox_pod",
            "executor_profile": "toolbox-k8s-readonly",
            "target_kind": "k8s_cluster",
            "target_identity": "namespace:islap",
        },
    )
    monkeypatch.setattr(
        "api.execute.evaluate_query_whitelist",
        lambda _command, _meta: {"whitelisted": True, "reason": "synthetic whitelist"},
    )
    monkeypatch.setattr(
        "api.execute.evaluate_target_registry_gate",
        lambda **_: {
            "enabled": True,
            "mode": "enforced",
            "applied": True,
            "result": "manual_required",
            "reason": "target identity matched multiple targets",
            "target_id": "",
            "registered": True,
            "status": "ambiguous",
            "required_capabilities": ["read_logs"],
            "missing_capabilities": ["read_logs"],
            "matched_capabilities": [],
            "ambiguous_targets": ["tgt-k8s-a", "tgt-k8s-b"],
            "lookup_error": "",
            "resolve_error": "",
        },
    )

    async def _run() -> None:
        payload = await precheck_command(
            PrecheckRequest(
                session_id="sess-target-registry-ambiguous-001",
                message_id="msg-target-registry-ambiguous-001",
                action_id="act-target-registry-ambiguous-001",
                command="kubectl -n islap get pods",
            )
        )
        assert payload["status"] == "confirmation_required"
        assert payload["approval_policy"] == "manual_required"
        assert payload["target_registry"]["status"] == "ambiguous"
        assert payload["target_registry"]["ambiguous_targets"] == ["tgt-k8s-a", "tgt-k8s-b"]

        decision_payload = await get_policy_decision(str(payload.get("decision_id")))
        decision = decision_payload["decision"]
        target_registry = decision["input_payload"]["target_registry"]
        assert target_registry["status"] == "ambiguous"
        assert target_registry["ambiguous_targets"] == ["tgt-k8s-a", "tgt-k8s-b"]

    asyncio.run(_run())


def test_precheck_blocks_when_controlled_gateway_required_and_template_missing(monkeypatch):
    monkeypatch.setenv("EXEC_ALLOWED_HEADS", "kubectl")
    monkeypatch.setenv("EXEC_CONTROLLED_GATEWAY_REQUIRED", "true")
    monkeypatch.delenv("EXEC_EXECUTOR_TEMPLATE__TOOLBOX_K8S_READONLY", raising=False)

    async def _run() -> None:
        payload = await precheck_command(
            PrecheckRequest(
                session_id="sess-gateway-guard-001",
                message_id="msg-gateway-guard-001",
                action_id="act-gateway-guard-001",
                command="kubectl -n islap get pods",
            )
        )
        assert payload["status"] == "permission_required"
        assert payload["dispatch_requires_template"] is True
        assert payload["dispatch_degraded"] is True
        assert "controlled executor unavailable for current executor profile" in str(payload["message"])

    asyncio.run(_run())


def test_precheck_returns_dispatch_preview_when_template_is_configured(monkeypatch):
    monkeypatch.setenv("EXEC_ALLOWED_HEADS", "kubectl")
    monkeypatch.setenv(
        "EXEC_EXECUTOR_TEMPLATE__TOOLBOX_K8S_READONLY",
        "curl -sS http://toolbox-gateway/exec?cmd={command_quoted}",
    )

    async def _run() -> None:
        payload = await precheck_command(
            PrecheckRequest(
                session_id="sess-preview-001",
                message_id="msg-preview-001",
                action_id="act-preview-001",
                command="kubectl -n islap get pods",
            )
        )
        assert payload["status"] == "ok"
        assert payload["executor_profile"] == "toolbox-k8s-readonly"
        assert payload["effective_executor_profile"] == "toolbox-k8s-readonly"
        assert payload["effective_executor_type"] == "sandbox_pod"
        assert payload["dispatch_backend"] == "template_executor"
        assert payload["dispatch_mode"] == "remote_template"
        assert payload["dispatch_template_env"] == "EXEC_EXECUTOR_TEMPLATE__TOOLBOX_K8S_READONLY"
        assert payload["dispatch_requires_template"] is True
        assert payload["dispatch_ready"] is True
        assert payload["dispatch_degraded"] is False

    asyncio.run(_run())


def test_precheck_permissive_mode_does_not_bypass_missing_template(monkeypatch):
    monkeypatch.setenv("EXEC_ALLOWED_HEADS", "kubectl")
    monkeypatch.setenv("EXEC_COMMAND_TEST_PERMISSIVE", "true")
    monkeypatch.delenv("EXEC_EXECUTOR_TEMPLATE__TOOLBOX_K8S_READONLY", raising=False)

    async def _run() -> None:
        payload = await precheck_command(
            PrecheckRequest(
                session_id="sess-permissive-gateway-001",
                message_id="msg-permissive-gateway-001",
                action_id="act-permissive-gateway-001",
                command="kubectl -n islap get pods",
                purpose="验证测试模式下网关降级放通",
            )
        )
        assert payload["status"] == "permission_required"
        assert payload["command_type"] == "query"
        assert payload["dispatch_requires_template"] is True
        assert payload["dispatch_degraded"] is True
        assert payload["dispatch_backend"] == "template_unavailable"

    asyncio.run(_run())


def test_precheck_auto_rewrites_wrapped_unknown_command(monkeypatch):
    monkeypatch.setenv("EXEC_ALLOWED_HEADS", "kubectl")

    async def _run() -> None:
        raw_command = "请执行命令：`kubectl -n islap get pods`"
        payload = await precheck_command(
            PrecheckRequest(
                session_id="sess-rewrite-001",
                message_id="msg-rewrite-001",
                action_id="act-rewrite-001",
                command=raw_command,
                purpose="自动纠偏 unknown 命令",
            )
        )
        assert payload["status"] == "ok"
        assert payload["rewrite_applied"] is True
        assert payload["original_command"] == raw_command
        assert payload["command"] == "kubectl -n islap get pods"
        assert payload["command_type"] == "query"
        assert payload["risk_level"] == "low"
        assert "auto rewritten" in str(payload["message"]).lower()

    asyncio.run(_run())


def test_precheck_auto_rewrite_does_not_bypass_blocked_redirection(monkeypatch):
    monkeypatch.setenv("EXEC_ALLOWED_HEADS", "kubectl")

    async def _run() -> None:
        raw_command = "```bash\nkubectl get pods > /tmp/pods.txt\n```"
        payload = await precheck_command(
            PrecheckRequest(
                session_id="sess-rewrite-blocked-001",
                message_id="msg-rewrite-blocked-001",
                action_id="act-rewrite-blocked-001",
                command=raw_command,
                purpose="验证自动纠偏不绕过风控",
            )
        )
        assert payload["status"] == "permission_required"
        assert payload["rewrite_applied"] is False
        assert "blocked" in str(payload["message"]).lower()

    asyncio.run(_run())


def test_precheck_non_whitelist_query_requires_confirmation(monkeypatch):
    monkeypatch.setenv("EXEC_ALLOWED_HEADS", "curl")

    async def _run() -> None:
        payload = await precheck_command(
            PrecheckRequest(
                session_id="sess-confirm-query-001",
                message_id="msg-confirm-query-001",
                action_id="act-confirm-query-001",
                command="curl https://example.com/api/health",
                purpose="非白名单查询命令需审批",
            )
        )
        assert payload["status"] == "confirmation_required"
        assert payload["command_type"] == "query"
        assert payload["requires_confirmation"] is True
        assert payload["requires_elevation"] is False
        assert payload["whitelist_match"] is False
        assert "white" in str(payload["message"]).lower() or "白名单" in str(payload["message"])
        assert str(payload.get("confirmation_ticket") or "").startswith("exec-ticket-")

    asyncio.run(_run())


def test_precheck_kubectl_exec_query_requires_confirmation(monkeypatch):
    monkeypatch.setenv("EXEC_ALLOWED_HEADS", "kubectl,clickhouse-client")

    async def _run() -> None:
        payload = await precheck_command(
            PrecheckRequest(
                session_id="sess-kubectl-exec-approval-001",
                message_id="msg-kubectl-exec-approval-001",
                action_id="act-kubectl-exec-approval-001",
                command=(
                    "kubectl -n islap exec pod/clickhouse-0 -- "
                    "clickhouse-client --query \"DESCRIBE TABLE logs.traces\""
                ),
                purpose="kubectl exec 必须审批",
            )
        )
        assert payload["status"] == "confirmation_required"
        assert payload["command_type"] == "query"
        assert payload["requires_confirmation"] is True
        assert payload["requires_elevation"] is False
        assert payload["whitelist_match"] is False
        assert "kubectl exec" in str(payload.get("whitelist_reason") or "").lower()

    asyncio.run(_run())


def test_build_clickhouse_runtime_preflight_command_wraps_query_with_explain_syntax():
    command = (
        "kubectl -n islap exec -i $(kubectl -n islap get pods -l app=clickhouse "
        "-o jsonpath='{.items[0].metadata.name}') -- clickhouse-client --query "
        "\"DESCRIBE TABLE logs.traces\""
    )

    preflight = _build_clickhouse_runtime_preflight_command(command)

    assert "EXPLAIN SYNTAX DESCRIBE TABLE logs.traces" in preflight
    assert "kubectl -n islap exec -i" in preflight


def test_precheck_clickhouse_query_blocks_when_runtime_preflight_fails(monkeypatch):
    monkeypatch.setenv("EXEC_ALLOWED_HEADS", "kubectl,clickhouse-client")

    async def _fake_dispatch_command(**kwargs):
        assert "EXPLAIN SYNTAX DESCRIBE TABLE logs.traces" in str(kwargs.get("command") or "")
        on_output = kwargs.get("on_output")
        if on_output is not None:
            await on_output("stderr", "clickhouse syntax error near DESCRIBE")
        return {
            "exit_code": 1,
            "timed_out": False,
            "duration_ms": 12,
            "dispatch": {
                "dispatch_backend": "template_executor",
                "dispatch_ready": True,
                "dispatch_degraded": False,
                "dispatch_reason": "",
            },
        }

    monkeypatch.setattr("api.execute.dispatch_command", _fake_dispatch_command)

    async def _run() -> None:
        payload = await precheck_command(
            PrecheckRequest(
                session_id="sess-clickhouse-preflight-001",
                message_id="msg-clickhouse-preflight-001",
                action_id="act-clickhouse-preflight-001",
                command=(
                    "kubectl -n islap exec -i $(kubectl -n islap get pods -l app=clickhouse "
                    "-o jsonpath='{.items[0].metadata.name}') -- clickhouse-client --query "
                    "\"DESCRIBE TABLE logs.traces\""
                ),
                purpose="验证 runtime preflight 在执行域失败时阻断",
            )
        )
        assert payload["status"] == "permission_required"
        assert payload["command_type"] == "query"
        assert "runtime preflight failed" in str(payload.get("message") or "").lower()
        assert "clickhouse syntax error" in str(payload.get("message") or "").lower()
        assert "DESCRIBE TABLE logs.traces" in str(payload.get("command") or "")
        runtime_preflight = payload.get("runtime_preflight") if isinstance(payload.get("runtime_preflight"), dict) else {}
        assert runtime_preflight.get("applicable") is True
        assert runtime_preflight.get("ok") is False
        assert "EXPLAIN SYNTAX DESCRIBE TABLE logs.traces" in str(runtime_preflight.get("command") or "")

    asyncio.run(_run())


def test_precheck_accepts_explicit_clickhouse_target_override(monkeypatch):
    monkeypatch.setenv("EXEC_ALLOWED_HEADS", "clickhouse-client")

    async def _fake_dispatch_command(**kwargs):
        return {
            "exit_code": 0,
            "timed_out": False,
            "duration_ms": 8,
            "dispatch": {
                "dispatch_backend": "template_executor",
                "dispatch_ready": True,
                "dispatch_degraded": False,
                "dispatch_reason": "",
            },
        }

    monkeypatch.setattr("api.execute.dispatch_command", _fake_dispatch_command)

    async def _run() -> None:
        payload = await precheck_command(
            PrecheckRequest(
                session_id="sess-clickhouse-target-001",
                message_id="msg-clickhouse-target-001",
                action_id="act-clickhouse-target-001",
                command='clickhouse-client --query "DESCRIBE TABLE logs.obs_traces_1m"',
                purpose="显式声明 clickhouse 目标",
                target_kind="clickhouse_cluster",
                target_identity="database:logs",
            )
        )
        assert payload["status"] in {"ok", "confirmation_required"}
        assert payload["target_kind"] == "clickhouse_cluster"
        assert payload["target_identity"] == "database:logs"

    asyncio.run(_run())


def test_precheck_non_permissive_allows_subshell_shape_for_kubectl_exec(monkeypatch):
    monkeypatch.setenv("EXEC_ALLOWED_HEADS", "kubectl,clickhouse-client")
    monkeypatch.setenv("EXEC_COMMAND_TEST_PERMISSIVE", "false")

    async def _run() -> None:
        payload = await precheck_command(
            PrecheckRequest(
                session_id="sess-kubectl-subshell-001",
                message_id="msg-kubectl-subshell-001",
                action_id="act-kubectl-subshell-001",
                command=(
                    "kubectl -n islap exec -it $(kubectl get pods -n islap -l app=clickhouse "
                    "-o jsonpath='{.items[0].metadata.name}') -- clickhouse-client --query "
                    "\"DESCRIBE TABLE logs.traces\""
                ),
                purpose="$() 形态应进入审批而非直接拦截",
            )
        )
        assert payload["status"] == "confirmation_required"
        assert payload["command_type"] == "query"
        assert payload["requires_elevation"] is False
        assert "blocked fragments/operators" not in str(payload.get("message") or "").lower()
        assert "kubectl exec" in str(payload.get("whitelist_reason") or "").lower()

    asyncio.run(_run())


def test_precheck_allows_pipeline_operator(monkeypatch):
    monkeypatch.setenv("EXEC_ALLOWED_HEADS", "kubectl,cat")

    async def _run() -> None:
        payload = await precheck_command(
            PrecheckRequest(
                session_id="sess-pipeline-001",
                message_id="msg-pipeline-001",
                action_id="act-pipeline-001",
                command="kubectl get pods | cat",
                purpose="验证预检允许管道命令",
            )
        )
        assert payload["status"] == "ok"
        assert payload["command_type"] == "query"
        assert payload["requires_write_permission"] is False

    asyncio.run(_run())


def test_precheck_permissive_mode_subshell_enters_confirmation_and_repairs_spacing(monkeypatch):
    monkeypatch.setenv("EXEC_ALLOWED_HEADS", "kubectl,clickhouse-client")
    monkeypatch.setenv("EXEC_COMMAND_TEST_PERMISSIVE", "true")

    async def _run() -> None:
        payload = await precheck_command(
            PrecheckRequest(
                session_id="sess-permissive-001",
                message_id="msg-permissive-001",
                action_id="act-permissive-001",
                command="kubectl -nislapexec -it$(kubectl -nislapgetpods -lapp=clickhouse -ojsonpath='{.items[0].metadata.name}') --clickhouse -client --query \"SHOWCREATETABLElogs.traces\"",
                purpose="测试阶段放通与空格断句修复",
            )
        )
        assert payload["status"] == "confirmation_required"
        assert payload["command_type"] == "query"
        assert payload["requires_elevation"] is False
        assert payload["approval_policy"] == "confirmation_required"
        assert payload["rewrite_applied"] is False
        assert payload["executor_type"] == "sandbox_pod"
        assert payload["executor_profile"] == "toolbox-k8s-readonly"
        assert payload["target_kind"] == "k8s_cluster"
        assert payload["dispatch_requires_template"] is True
        assert "-n islap exec" in str(payload["command"])
        assert "-n islap get pods" in str(payload["command"])
        assert " exec $(" in str(payload["command"])
        assert " -i " not in str(payload["command"])
        assert "-- clickhouse-client" in str(payload["command"])

    asyncio.run(_run())


def test_precheck_permissive_mode_repairs_glued_kubectl_tokens(monkeypatch):
    monkeypatch.setenv("EXEC_ALLOWED_HEADS", "kubectl,clickhouse-client")
    monkeypatch.setenv("EXEC_COMMAND_TEST_PERMISSIVE", "true")

    async def _run() -> None:
        payload = await precheck_command(
            PrecheckRequest(
                session_id="sess-permissive-002",
                message_id="msg-permissive-002",
                action_id="act-permissive-002",
                command="kubectlexec-nislap-it $(kubectlgetpods-nislap-lapp=clickhouse-ojsonpath='{.items[0].metadata.name}')--clickhouse-client--query\"DESCRIBETABLElogs.traces\"",
                purpose="修复 kubectl 无空格断句",
            )
        )
        command = str(payload["command"])
        assert payload["status"] == "confirmation_required"
        assert payload["command_type"] == "query"
        assert payload["executor_profile"] == "toolbox-k8s-readonly"
        assert payload["dispatch_requires_template"] is True
        assert "kubectl exec -n islap $(" in command
        assert " -i " not in command
        assert "kubectl get pods -n islap -l app=clickhouse -o jsonpath=" in command
        assert "-- clickhouse-client --query \"DESCRIBE TABLE logs.traces\"" in command

    asyncio.run(_run())


def test_precheck_permissive_mode_repairs_kubectl_logs_namespace_glued(monkeypatch):
    monkeypatch.setenv("EXEC_ALLOWED_HEADS", "kubectl,grep")
    monkeypatch.setenv("EXEC_COMMAND_TEST_PERMISSIVE", "true")

    async def _run() -> None:
        payload = await precheck_command(
            PrecheckRequest(
                session_id="sess-permissive-logs-001",
                message_id="msg-permissive-logs-001",
                action_id="act-permissive-logs-001",
                command="kubectl logs-nislap --tail=100 -l app=query-service | grep -i error",
                purpose="修复 kubectl logs-n<namespace> 粘连",
            )
        )
        command = str(payload["command"])
        assert payload["status"] == "ok"
        assert payload["command_type"] == "query"
        assert payload["requires_write_permission"] is False
        assert command == "kubectl logs -n islap --tail=100 -l app=query-service | grep -i error"

    asyncio.run(_run())


def test_precheck_permissive_mode_repairs_namespace_and_subshell_glued_tokens(monkeypatch):
    monkeypatch.setenv("EXEC_ALLOWED_HEADS", "kubectl,clickhouse-client")
    monkeypatch.setenv("EXEC_COMMAND_TEST_PERMISSIVE", "true")

    async def _run() -> None:
        payload = await precheck_command(
            PrecheckRequest(
                session_id="sess-permissive-003",
                message_id="msg-permissive-003",
                action_id="act-permissive-003",
                command="kubectl -n islapexec -it $(kubectl-nislapgetpods-lapp=clickhouse-ojsonpath='{.items[0].metadata.name}') -- clickhouse-client --query \"DESCRIBETABLElogs.traces\"",
                purpose="修复 namespace+verb 粘连与子命令断句",
            )
        )
        command = str(payload["command"])
        assert payload["status"] == "confirmation_required"
        assert payload["command_type"] == "query"
        assert "kubectl -n islap exec $(" in command
        assert " -i " not in command
        assert "kubectl -n islap get pods -l app=clickhouse -o jsonpath=" in command
        assert "-- clickhouse-client --query \"DESCRIBE TABLE logs.traces\"" in command

    asyncio.run(_run())


def test_precheck_repairs_compact_clickhouse_query_keywords(monkeypatch):
    monkeypatch.setenv("EXEC_ALLOWED_HEADS", "kubectl,clickhouse-client")

    async def _run() -> None:
        payload = await precheck_command(
            PrecheckRequest(
                session_id="sess-clickhouse-sql-001",
                message_id="msg-clickhouse-sql-001",
                action_id="act-clickhouse-sql-001",
                command=(
                    "kubectl -n islap exec -it $(kubectl -n islap get pods -l app=clickhouse "
                    "-o jsonpath='{.items[0].metadata.name}') -- clickhouse-client --query "
                    "\"SELECTpartition,name,rows,bytes_on_diskFROMsystem.partsWHEREtable='traces'"
                    "ANDdatabase='logs'ORDERBYpartitionDESCLIMIT10\""
                ),
                purpose="验证 clickhouse SQL 关键字断句修复",
            )
        )
        command = str(payload["command"])
        assert payload["status"] == "confirmation_required"
        assert payload["command_type"] == "query"
        assert "-n islap exec $(" in command
        assert " -i " not in command
        assert (
            "--query \"SELECT partition,name,rows,bytes_on_disk FROM system.parts "
            "WHERE table='traces' AND database='logs' ORDER BY partition DESC LIMIT 10\""
        ) in command

    asyncio.run(_run())


def test_precheck_keeps_kubectl_exec_interactive_flag_without_clickhouse_query(monkeypatch):
    monkeypatch.setenv("EXEC_ALLOWED_HEADS", "kubectl,clickhouse-client")
    monkeypatch.setenv("EXEC_COMMAND_TEST_PERMISSIVE", "true")

    async def _run() -> None:
        payload = await precheck_command(
            PrecheckRequest(
                session_id="sess-clickhouse-interactive-001",
                message_id="msg-clickhouse-interactive-001",
                action_id="act-clickhouse-interactive-001",
                command="kubectl -n islap exec -it pod/clickhouse-0 -- clickhouse-client",
                purpose="验证未提供 --query 时保留交互标志",
            )
        )
        command = str(payload["command"])
        assert "kubectl -n islap exec -i pod/clickhouse-0 -- clickhouse-client" in command
        assert payload["status"] in {"permission_required", "elevation_required"}

    asyncio.run(_run())


def test_precheck_repairs_clickhouse_placeholder_flags_to_readonly_query(monkeypatch):
    monkeypatch.setenv("EXEC_ALLOWED_HEADS", "clickhouse-client")

    async def _run() -> None:
        payload = await precheck_command(
            PrecheckRequest(
                session_id="sess-clickhouse-placeholder-001",
                message_id="msg-clickhouse-placeholder-001",
                action_id="act-clickhouse-placeholder-001",
                command="clickhouse-client --host<HOST>--port<PORT>--user<USER>--password<PASSWORD>--database<DATABASE>--query \"SHOWCREATETABLElogs.traces\"",
                purpose="验证 clickhouse 占位符空格修复",
            )
        )
        command = str(payload["command"])
        assert payload["status"] == "permission_required"
        assert payload["command_type"] == "unknown"
        assert "unresolved template placeholders" in str(payload["message"])
        assert "--host <HOST>" in command
        assert "--port <PORT>" in command
        assert "--database <DATABASE>" in command
        assert "SHOW CREATE TABLE logs.traces" in command

    asyncio.run(_run())


def test_create_run_accepts_confirmed_ticket_for_write_command(monkeypatch):
    monkeypatch.setenv("EXEC_ALLOWED_HEADS", "kubectl")
    monkeypatch.setenv(
        "EXEC_EXECUTOR_TEMPLATE__TOOLBOX_K8S_MUTATING",
        "curl -sS http://toolbox-gateway/exec?cmd={command_quoted}",
    )
    runtime_service_module._exec_runtime_service = None
    ticket_store_module.TICKET_STORE.clear()

    captured: dict[str, object] = {}

    def _fake_runtime():
        def _create_run(**kwargs):
            captured.update(kwargs)
            return {
                "run_id": "cmdrun-approved-001",
                "status": "running",
                **kwargs,
            }

        return SimpleNamespace(create_run=_create_run)

    monkeypatch.setattr("api.execute.get_exec_runtime_service", _fake_runtime)

    async def _run() -> None:
        initial = await create_command_run(
            CommandRunCreateRequest(
                session_id="sess-approve-001",
                message_id="msg-approve-001",
                action_id="act-approve-001",
                command="kubectl -n islap rollout restart deployment/query-service",
                purpose="执行重启变更",
                timeout_seconds=20,
            )
        )
        assert initial["status"] == "elevation_required"
        ticket = str(initial["confirmation_ticket"])

        resumed = await create_command_run(
            CommandRunCreateRequest(
                session_id="sess-approve-001",
                message_id="msg-approve-001",
                action_id="act-approve-001",
                command="kubectl -n islap rollout restart deployment/query-service",
                purpose="执行重启变更",
                confirmed=True,
                elevated=True,
                confirmation_ticket=ticket,
                timeout_seconds=20,
            )
        )

        assert resumed["run"]["run_id"] == "cmdrun-approved-001"
        assert captured["command"] == "kubectl -n islap rollout restart deployment/query-service"
        assert captured["command_type"] == "repair"
        assert captured["approval_policy"] == "elevation_required"
        assert not ticket_store_module.TICKET_STORE

    asyncio.run(_run())


def test_precheck_permissive_mode_keeps_write_command_as_elevation_required(monkeypatch):
    monkeypatch.setenv("EXEC_ALLOWED_HEADS", "kubectl")
    monkeypatch.setenv("EXEC_COMMAND_TEST_PERMISSIVE", "true")

    async def _run() -> None:
        payload = await precheck_command(
            PrecheckRequest(
                session_id="sess-permissive-write-001",
                message_id="msg-permissive-write-001",
                action_id="act-permissive-write-001",
                command="kubectl -n islap rollout restart deployment/query-service",
                purpose="验证 permissive 不绕过提权审批",
            )
        )
        assert payload["status"] == "elevation_required"
        assert payload["command_type"] == "repair"
        assert payload["requires_elevation"] is True
        assert payload["approval_policy"] == "elevation_required"

    asyncio.run(_run())


def test_precheck_blocks_write_command_when_dispatch_plane_degraded(monkeypatch):
    monkeypatch.setenv("EXEC_ALLOWED_HEADS", "kubectl")
    monkeypatch.delenv("EXEC_EXECUTOR_TEMPLATE__TOOLBOX_K8S_MUTATING", raising=False)

    async def _run() -> None:
        payload = await precheck_command(
            PrecheckRequest(
                session_id="sess-degraded-001",
                message_id="msg-degraded-001",
                action_id="act-degraded-001",
                command="kubectl -n islap rollout restart deployment/query-service",
                purpose="在执行平面降级时验证写命令阻断",
            )
        )
        assert payload["status"] == "permission_required"
        assert payload["dispatch_degraded"] is True
        assert payload["requires_elevation"] is False
        assert payload["approval_policy"] == "deny"
        assert "controlled executor unavailable for current executor profile" in str(payload["message"])

    asyncio.run(_run())


def test_create_run_requires_purpose(monkeypatch):
    monkeypatch.setenv("EXEC_ALLOWED_HEADS", "echo")
    runtime_service_module._exec_runtime_service = None

    async def _run() -> None:
        try:
            await create_command_run(
                CommandRunCreateRequest(
                    session_id="sess-purpose-001",
                    message_id="msg-purpose-001",
                    action_id="act-purpose-001",
                    command="echo no-purpose",
                    purpose="   ",
                    timeout_seconds=5,
                )
            )
        except HTTPException as exc:
            assert exc.status_code == 400
            assert exc.detail == "purpose is required"
            return
        raise AssertionError("expected HTTPException")

    asyncio.run(_run())


def test_policy_decision_endpoints_bind_decision_to_run(monkeypatch):
    monkeypatch.setenv("EXEC_ALLOWED_HEADS", "echo")
    runtime_service_module._exec_runtime_service = None

    async def _run() -> None:
        created = await create_command_run(
            CommandRunCreateRequest(
                session_id="sess-decision-001",
                message_id="msg-decision-001",
                action_id="act-decision-001",
                command="echo decision-link",
                purpose="验证 decision_id 与 run_id 绑定",
                timeout_seconds=5,
            )
        )
        run_id = str(created["run"]["run_id"])
        decision_id = str(created["run"].get("policy_decision_id") or "")
        assert decision_id.startswith("dec-")

        runtime = runtime_service_module.get_exec_runtime_service()
        await runtime.wait_for_run(run_id)

        single = await get_policy_decision(decision_id)
        row = single["decision"]
        assert row["decision_id"] == decision_id
        assert row["run_id"] == run_id
        assert row["command_run_id"] == run_id

        by_run = await get_policy_decisions(limit=10, run_id=run_id)
        assert by_run["total"] >= 1
        assert any(item["decision_id"] == decision_id for item in by_run["rows"])

    asyncio.run(_run())


def test_policy_decision_sqlite_backend_survives_memory_cache_reset(monkeypatch, tmp_path):
    monkeypatch.setenv("EXEC_ALLOWED_HEADS", "echo")
    monkeypatch.setenv("EXEC_POLICY_DECISION_STORE_BACKEND", "sqlite")
    monkeypatch.setenv("EXEC_POLICY_DECISION_SQLITE_PATH", str(tmp_path / "policy-decisions.sqlite3"))
    policy_decision_store_module.clear_policy_decisions()
    runtime_service_module._exec_runtime_service = None

    async def _run() -> None:
        created = await create_command_run(
            CommandRunCreateRequest(
                session_id="sess-sqlite-001",
                message_id="msg-sqlite-001",
                action_id="act-sqlite-001",
                command="echo sqlite-persist",
                purpose="验证 sqlite 决策持久化",
                timeout_seconds=5,
            )
        )
        run_id = str(created["run"]["run_id"])
        decision_id = str(created["run"].get("policy_decision_id") or "")
        assert decision_id.startswith("dec-")

        runtime = runtime_service_module.get_exec_runtime_service()
        await runtime.wait_for_run(run_id)

        policy_decision_store_module.clear_policy_decision_cache()

        fetched = await get_policy_decision(decision_id)
        row = fetched["decision"]
        assert row["decision_id"] == decision_id
        assert row["run_id"] == run_id
        assert row["command_run_id"] == run_id

    asyncio.run(_run())


def test_policy_decision_clickhouse_backend_survives_memory_cache_reset(monkeypatch):
    monkeypatch.setenv("EXEC_ALLOWED_HEADS", "echo")
    monkeypatch.setenv("EXEC_POLICY_DECISION_STORE_BACKEND", "clickhouse")
    runtime_service_module._exec_runtime_service = None

    fake_store: dict[str, dict[str, object]] = {}

    def _fake_persist(record: dict[str, object]) -> None:
        decision_id = str(record.get("decision_id") or "")
        if decision_id:
            fake_store[decision_id] = dict(record)

    def _fake_load(decision_id: str):
        row = fake_store.get(str(decision_id))
        return dict(row) if isinstance(row, dict) else None

    monkeypatch.setattr(policy_decision_store_module, "_persist_record_to_clickhouse", _fake_persist)
    monkeypatch.setattr(policy_decision_store_module, "_load_record_from_clickhouse", _fake_load)
    monkeypatch.setattr(
        policy_decision_store_module,
        "_list_records_from_clickhouse",
        lambda **kwargs: [dict(item) for item in fake_store.values()],
    )
    monkeypatch.setattr(policy_decision_store_module, "_clear_records_from_clickhouse", lambda: fake_store.clear())

    async def _run() -> None:
        created = await create_command_run(
            CommandRunCreateRequest(
                session_id="sess-clickhouse-001",
                message_id="msg-clickhouse-001",
                action_id="act-clickhouse-001",
                command="echo clickhouse-persist",
                purpose="验证 clickhouse 决策持久化",
                timeout_seconds=5,
            )
        )
        run_id = str(created["run"]["run_id"])
        decision_id = str(created["run"].get("policy_decision_id") or "")
        assert decision_id.startswith("dec-")

        runtime = runtime_service_module.get_exec_runtime_service()
        await runtime.wait_for_run(run_id)

        policy_decision_store_module.clear_policy_decision_cache()

        fetched = await get_policy_decision(decision_id)
        row = fetched["decision"]
        assert row["decision_id"] == decision_id
        assert row["run_id"] == run_id
        assert row["command_run_id"] == run_id

    asyncio.run(_run())


def test_runtime_history_clickhouse_backend_survives_runtime_reset(monkeypatch):
    monkeypatch.setenv("EXEC_ALLOWED_HEADS", "echo")
    monkeypatch.setenv("EXEC_RUNTIME_HISTORY_STORE_BACKEND", "clickhouse")
    runtime_service_module._exec_runtime_service = None
    audit_store_module.AUDIT_LOGS.clear()

    fake_runs: dict[str, dict[str, object]] = {}
    fake_events: dict[str, list[dict[str, object]]] = {}
    fake_audits: list[dict[str, object]] = []

    monkeypatch.setattr(run_store_module, "runtime_history_clickhouse_enabled", lambda: True)
    monkeypatch.setattr(
        run_store_module,
        "persist_run_record",
        lambda record: fake_runs.__setitem__(str((record or {}).get("run_id") or ""), dict(record or {})),
    )
    monkeypatch.setattr(
        run_store_module,
        "load_run_record",
        lambda run_id: dict(fake_runs.get(str(run_id)) or {}) if str(run_id) in fake_runs else None,
    )
    monkeypatch.setattr(
        run_store_module,
        "list_run_records",
        lambda limit=100: [dict(item) for item in list(fake_runs.values())[-max(1, min(int(limit or 100), 1000)) :]],
    )

    monkeypatch.setattr(event_store_module, "runtime_history_clickhouse_enabled", lambda: True)
    monkeypatch.setattr(
        event_store_module,
        "persist_event_record",
        lambda record: fake_events.setdefault(str((record or {}).get("run_id") or ""), []).append(dict(record or {})),
    )
    monkeypatch.setattr(
        event_store_module,
        "list_event_records",
        lambda run_id, after_seq=0, limit=500: [
            dict(item)
            for item in fake_events.get(str(run_id), [])
            if int((item or {}).get("seq") or 0) > max(0, int(after_seq or 0))
        ][: max(1, min(int(limit or 500), 5000))],
    )

    monkeypatch.setattr(audit_store_module, "runtime_history_clickhouse_enabled", lambda: True)
    monkeypatch.setattr(
        audit_store_module,
        "persist_audit_record",
        lambda record: fake_audits.append(dict(record or {})),
    )
    monkeypatch.setattr(
        audit_store_module,
        "list_audit_records",
        lambda limit=100, run_id="": [
            dict(item)
            for item in fake_audits
            if not str(run_id) or str((item or {}).get("run_id") or "") == str(run_id)
        ][-max(1, min(int(limit or 100), 5000)) :],
    )

    async def _run() -> None:
        created = await create_command_run(
            CommandRunCreateRequest(
                session_id="sess-runtime-history-001",
                message_id="msg-runtime-history-001",
                action_id="act-runtime-history-001",
                command="echo runtime-history",
                purpose="验证 run/event/audit clickhouse 持久化恢复",
                timeout_seconds=5,
            )
        )
        run_id = str(created["run"]["run_id"])
        runtime = runtime_service_module.get_exec_runtime_service()
        await runtime.wait_for_run(run_id)

        runtime_service_module._exec_runtime_service = None
        audit_store_module.AUDIT_LOGS.clear()

        fetched = await get_run(run_id)
        assert fetched["run"]["run_id"] == run_id
        assert fetched["run"]["status"] == "completed"

        events_payload = await get_run_events(run_id, after_seq=0, limit=50)
        event_types = [item.get("event_type") for item in events_payload["events"]]
        assert "command_started" in event_types
        assert "command_finished" in event_types

        replay = await get_run_replay(run_id)
        assert replay["run"]["run_id"] == run_id
        assert any(str((item or {}).get("run_id") or "") == run_id for item in replay["audit_rows"])

    asyncio.run(_run())


def test_get_run_replay_aggregates_run_events_audit_and_policy_decisions(monkeypatch):
    monkeypatch.setenv("EXEC_ALLOWED_HEADS", "echo")
    runtime_service_module._exec_runtime_service = None
    audit_store_module.AUDIT_LOGS.clear()

    async def _run() -> None:
        created = await create_command_run(
            CommandRunCreateRequest(
                session_id="sess-replay-001",
                message_id="msg-replay-001",
                action_id="act-replay-001",
                command="echo replay-check",
                purpose="验证 replay 聚合接口",
                timeout_seconds=5,
            )
        )
        run_id = str(created["run"]["run_id"])
        decision_id = str(created["run"].get("policy_decision_id") or "")
        assert decision_id.startswith("dec-")

        runtime = runtime_service_module.get_exec_runtime_service()
        await runtime.wait_for_run(run_id)

        replay = await get_run_replay(run_id)
        assert replay["run_id"] == run_id
        assert replay["run"]["run_id"] == run_id
        assert replay["run"]["policy_decision_id"] == decision_id

        event_types = [item.get("event_type") for item in replay["events"]]
        assert "command_started" in event_types
        assert "command_finished" in event_types

        assert any(item.get("run_id") == run_id for item in replay["audit_rows"])
        assert any(item.get("decision_id") == decision_id for item in replay["policy_decisions"])

    asyncio.run(_run())


def test_get_executors_reports_template_readiness(monkeypatch):
    monkeypatch.setenv(
        "EXEC_EXECUTOR_TEMPLATE__TOOLBOX_K8S_READONLY",
        "curl -sS http://toolbox-gateway/exec?cmd={command_quoted}",
    )

    async def _run() -> None:
        payload = await get_executors()
        assert payload["total"] >= 1
        ready_row = next(
            item for item in payload["rows"] if item["executor_profile"] == "toolbox-k8s-readonly"
        )
        assert ready_row["dispatch_ready"] is True
        assert ready_row["rollout_stage"] == "phase-3"
        assert "toolbox-gateway-url" in ready_row["example_template"]
        assert ready_row["dispatch_backend"] == "template_executor"
        assert ready_row["dispatch_template_env"] == "EXEC_EXECUTOR_TEMPLATE__TOOLBOX_K8S_READONLY"

        degraded_row = next(
            item for item in payload["rows"] if item["executor_profile"] == "host-ssh-readonly"
        )
        assert degraded_row["dispatch_requires_template"] is True
        assert degraded_row["dispatch_ready"] is False
        assert degraded_row["dispatch_degraded"] is True

    asyncio.run(_run())


def test_create_run_uses_executor_template_when_profile_is_configured(monkeypatch):
    monkeypatch.setenv("EXEC_ALLOWED_HEADS", "echo")
    monkeypatch.setenv(
        "EXEC_EXECUTOR_TEMPLATE__BUSYBOX_READONLY",
        'python3 -c "import sys; print(sys.argv[1])" {command_quoted}',
    )
    runtime_service_module._exec_runtime_service = None

    async def _run() -> None:
        created = await create_command_run(
            CommandRunCreateRequest(
                session_id="sess-template-001",
                message_id="msg-template-001",
                action_id="act-template-001",
                command="echo template-dispatch",
                purpose="验证模板执行器链路",
                timeout_seconds=5,
            )
        )
        run_id = created["run"]["run_id"]
        runtime = runtime_service_module.get_exec_runtime_service()
        await runtime.wait_for_run(run_id)
        fetched = await get_run(run_id)
        terminal_run = fetched["run"]
        assert terminal_run["status"] == "completed"
        assert terminal_run["dispatch_backend"] == "template_executor"
        assert terminal_run["dispatch_mode"] == "remote_template"
        assert terminal_run["effective_executor_type"] == "sandbox_pod"
        assert terminal_run["effective_executor_profile"] == "busybox-readonly"
        assert "echo template-dispatch" in terminal_run["stdout"]

        events_payload = await get_run_events(run_id, after_seq=0, limit=20)
        dispatch_event = next(
            item for item in events_payload["events"] if item["event_type"] == "command_dispatch_resolved"
        )
        assert dispatch_event["payload"]["dispatch_backend"] == "template_executor"
        assert dispatch_event["payload"]["dispatch_template_env"] == "EXEC_EXECUTOR_TEMPLATE__BUSYBOX_READONLY"

    asyncio.run(_run())
