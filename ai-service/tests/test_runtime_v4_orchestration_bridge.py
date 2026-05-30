"""
Tests for runtime v4 orchestration bridge guard bypass behavior.
"""

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
import sys
from types import ModuleType, SimpleNamespace

import pytest

from ai.runtime_v4.backend.base import RuntimeBackendResult
from ai.runtime_v4.adapter.orchestration_bridge import RuntimeV4OrchestrationBridge


def _build_bridge() -> RuntimeV4OrchestrationBridge:
    return RuntimeV4OrchestrationBridge(
        temporal_client=SimpleNamespace(),
        thread_store=SimpleNamespace(),
        runtime_backend=SimpleNamespace(
            backend_name=lambda: "stub-backend-v1",
            run=lambda request: RuntimeBackendResult(inner_engine="stub-backend-v1", payload={"run_id": request.run_id}),
        ),
    )


def _install_fake_ai_module(monkeypatch):
    fake = ModuleType("api.ai")
    bypass_flag = ContextVar("runtime_v1_api_guard_bypass_test", default=False)

    @contextmanager
    def _bypass():
        token = bypass_flag.set(True)
        try:
            yield
        finally:
            bypass_flag.reset(token)

    class _AIRunCommandRequest:
        def __init__(self, **kwargs):
            self.purpose = str(kwargs.get("purpose") or "")
            self.command = str(kwargs.get("command") or "")

    fake.runtime_v1_api_guard_bypass = _bypass
    fake.AIRunCommandRequest = _AIRunCommandRequest
    monkeypatch.setitem(sys.modules, "api.ai", fake)
    return fake, bypass_flag


def test_bridge_get_run_events_calls_impl_directly_without_bypass(monkeypatch):
    bridge = _build_bridge()
    seen = {"bypass": False, "called": False}
    fake_ai, bypass_flag = _install_fake_ai_module(monkeypatch)

    async def _fake_get_ai_run_events_impl(run_id, *, after_seq, limit, visibility):
        assert run_id == "run-bridge-001"
        assert after_seq == 0
        assert limit == 20
        assert visibility == "default"
        seen["bypass"] = bool(bypass_flag.get())
        seen["called"] = True
        return {"run_id": run_id, "next_after_seq": 0, "events": []}

    fake_ai._get_ai_run_events_impl = _fake_get_ai_run_events_impl

    result = asyncio.run(bridge.get_run_events("run-bridge-001", after_seq=0, limit=20))

    assert seen["called"] is True
    assert seen["bypass"] is False  # bridge calls _impl directly, no bypass needed
    assert result["run_id"] == "run-bridge-001"


def test_bridge_execute_command_calls_impl_directly_without_bypass(monkeypatch):
    bridge = _build_bridge()
    seen = {"bypass": False, "called": False}
    fake_ai, bypass_flag = _install_fake_ai_module(monkeypatch)

    async def _fake_execute_ai_run_command(run_id, request):
        assert run_id == "run-bridge-002"
        assert request.purpose == "smoke purpose"
        assert request.command == "kubectl get pods"
        seen["bypass"] = bool(bypass_flag.get())
        seen["called"] = True
        return {"status": "permission_required", "run": {"run_id": run_id, "status": "running"}}

    fake_ai.execute_ai_run_command = _fake_execute_ai_run_command

    result = asyncio.run(
        bridge.execute_command(
            run_id="run-bridge-002",
            request_payload={
                "purpose": "smoke purpose",
                "command": "kubectl get pods",
            },
        )
    )

    assert seen["called"] is True
    assert seen["bypass"] is False  # bridge calls execute_ai_run_command directly
    assert result["status"] == "permission_required"


def test_bridge_create_run_uses_runtime_backend_result():
    async def _start_run(**_kwargs):
        return {
            "workflow_id": "wf-bridge-001",
            "outer_engine": "temporal-local-v1",
            "run": {"run_id": "run-bridge-003", "status": "running"},
        }

    captured = {}

    class _Backend:
        def backend_name(self):
            return "stub-backend-v1"

        def run(self, request):
            captured["run_id"] = request.run_id
            captured["question"] = request.question
            captured["analysis_context"] = dict(request.analysis_context or {})
            return RuntimeBackendResult(inner_engine="stub-backend-v1", payload={"ok": True})

    bridge = RuntimeV4OrchestrationBridge(
        temporal_client=SimpleNamespace(start_run=_start_run, outer_engine_name=lambda: "temporal-local-v1"),
        thread_store=SimpleNamespace(bind_run=lambda **_kwargs: None),
        runtime_backend=_Backend(),
    )

    result = asyncio.run(
        bridge.create_run(
            thread_id="thr-bridge-003",
            session_id="sess-bridge-003",
            question="query-service timeout",
            analysis_context={"service_name": "query-service"},
            runtime_options={"max_iterations": 2},
        )
    )

    assert captured["run_id"] == "run-bridge-003"
    assert captured["question"] == "query-service timeout"
    assert captured["analysis_context"]["thread_id"] == "thr-bridge-003"
    assert result["workflow_id"] == "wf-bridge-001"
    assert result["inner_engine"] == "stub-backend-v1"


def test_bridge_create_run_supports_openhands_backend_result():
    async def _start_run(**_kwargs):
        return {
            "workflow_id": "wf-openhands-001",
            "outer_engine": "temporal-local-v1",
            "run": {"run_id": "run-openhands-001", "status": "running"},
        }

    bridge = RuntimeV4OrchestrationBridge(
        temporal_client=SimpleNamespace(start_run=_start_run, outer_engine_name=lambda: "temporal-local-v1"),
        thread_store=SimpleNamespace(bind_run=lambda **_kwargs: None),
        runtime_backend=SimpleNamespace(
            backend_name=lambda: "openhands-v1",
            run=lambda request: RuntimeBackendResult(
                inner_engine="openhands-v1",
                payload={"mode": "readonly", "run_id": request.run_id},
            ),
        ),
    )

    result = asyncio.run(
        bridge.create_run(
            thread_id="thr-openhands-001",
            session_id="sess-openhands-001",
            question="排查 timeout",
            analysis_context={"runtime_profile": "ai_runtime_lab"},
            runtime_options={},
        )
    )

    assert result["workflow_id"] == "wf-openhands-001"
    assert result["run"]["run_id"] == "run-openhands-001"
    assert result["inner_engine"] == "openhands-v1"


def test_bridge_create_run_uses_requested_backend_hint(monkeypatch):
    async def _start_run(**_kwargs):
        return {
            "workflow_id": "wf-hint-001",
            "outer_engine": "temporal-local-v1",
            "run": {"run_id": "run-hint-001", "status": "running"},
        }

    monkeypatch.setenv("AI_RUNTIME_V4_OPENHANDS_ENABLED", "true")

    bridge = RuntimeV4OrchestrationBridge(
        temporal_client=SimpleNamespace(start_run=_start_run, outer_engine_name=lambda: "temporal-local-v1"),
        thread_store=SimpleNamespace(bind_run=lambda **_kwargs: None),
    )

    result = asyncio.run(
        bridge.create_run(
            thread_id="thr-hint-001",
            session_id="sess-hint-001",
            question="排查 timeout",
            analysis_context={},
            runtime_options={"runtime_backend": "openhands"},
        )
    )

    assert result["inner_engine"] == "openhands-v1"


def test_bridge_create_run_validates_openhands_backend_before_starting_outer_run(monkeypatch):
    started = {"count": 0}

    async def _start_run(**_kwargs):
        started["count"] += 1
        return {
            "workflow_id": "wf-invalid-provider-001",
            "outer_engine": "temporal-local-v1",
            "run": {"run_id": "run-invalid-provider-001", "status": "running"},
        }

    monkeypatch.setenv("AI_RUNTIME_V4_OPENHANDS_ENABLED", "true")
    monkeypatch.setenv(
        "AI_RUNTIME_V4_OPENHANDS_PROVIDER_FACTORY",
        "missing.module:create_provider",
    )

    bridge = RuntimeV4OrchestrationBridge(
        temporal_client=SimpleNamespace(start_run=_start_run, outer_engine_name=lambda: "temporal-local-v1"),
        thread_store=SimpleNamespace(bind_run=lambda **_kwargs: None),
    )

    with pytest.raises(RuntimeError, match="OpenHands provider factory import failed"):
        asyncio.run(
            bridge.create_run(
                thread_id="thr-invalid-provider-001",
                session_id="sess-invalid-provider-001",
                question="排查 timeout",
                analysis_context={},
                runtime_options={"runtime_backend": "openhands"},
            )
        )

    assert started["count"] == 0
