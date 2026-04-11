"""
Tests for runtime v4 orchestration bridge guard bypass behavior.
"""

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
import sys
from types import ModuleType, SimpleNamespace

from ai.runtime_v4.adapter.orchestration_bridge import RuntimeV4OrchestrationBridge


def _build_bridge() -> RuntimeV4OrchestrationBridge:
    return RuntimeV4OrchestrationBridge(
        temporal_client=SimpleNamespace(),
        thread_store=SimpleNamespace(),
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


def test_bridge_get_run_events_uses_runtime_v1_guard_bypass(monkeypatch):
    bridge = _build_bridge()
    seen = {"bypass": False}
    fake_ai, bypass_flag = _install_fake_ai_module(monkeypatch)

    async def _fake_get_ai_run_events(run_id, *, after_seq, limit, visibility):
        assert run_id == "run-bridge-001"
        assert after_seq == 0
        assert limit == 20
        assert visibility == "default"
        seen["bypass"] = bool(bypass_flag.get())
        return {"run_id": run_id, "next_after_seq": 0, "events": []}

    fake_ai.get_ai_run_events = _fake_get_ai_run_events

    result = asyncio.run(bridge.get_run_events("run-bridge-001", after_seq=0, limit=20))

    assert seen["bypass"] is True
    assert result["run_id"] == "run-bridge-001"


def test_bridge_execute_command_uses_runtime_v1_guard_bypass(monkeypatch):
    bridge = _build_bridge()
    seen = {"bypass": False}
    fake_ai, bypass_flag = _install_fake_ai_module(monkeypatch)

    async def _fake_execute_ai_run_command(run_id, request):
        assert run_id == "run-bridge-002"
        assert request.purpose == "smoke purpose"
        assert request.command == "kubectl get pods"
        seen["bypass"] = bool(bypass_flag.get())
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

    assert seen["bypass"] is True
    assert result["status"] == "permission_required"
