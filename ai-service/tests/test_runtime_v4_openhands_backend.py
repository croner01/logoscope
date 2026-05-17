"""Tests for the runtime v4 OpenHands backend skeleton."""

import sys
from types import ModuleType

from ai.runtime_v4.backend.base import RuntimeBackendRequest
from ai.runtime_v4.backend.openhands_backend import OpenHandsBackend


def test_openhands_backend_returns_readonly_planning_payload():
    backend = OpenHandsBackend()

    result = backend.run(
        RuntimeBackendRequest(
            run_id="run-oh-001",
            question="排查 query-service timeout",
            analysis_context={"service_name": "query-service", "runtime_profile": "ai_runtime_lab"},
            runtime_options={"auto_exec_readonly": True},
        )
    )

    assert result.inner_engine == "openhands-v1"
    assert result.payload["mode"] == "readonly"
    assert result.payload["analysis_context"]["service_name"] == "query-service"
    assert len(result.payload["tool_calls"]) == 1
    assert result.payload["tool_calls"][0]["tool_name"] == "command.exec"
    assert result.payload["tool_calls"][0]["command"].startswith("kubectl -n islap get pods")


def test_openhands_backend_non_readonly_mode_stays_approval_gated():
    backend = OpenHandsBackend()

    result = backend.run(
        RuntimeBackendRequest(
            run_id="run-oh-002",
            question="重启 query-service",
            analysis_context={"service_name": "query-service"},
            runtime_options={"auto_exec_readonly": False},
        )
    )

    assert result.inner_engine == "openhands-v1"
    assert result.payload["mode"] == "approval_gated"
    assert result.payload["tool_calls"] == []


def test_openhands_backend_selects_registered_skills_and_exports_tool_intents():
    backend = OpenHandsBackend()

    result = backend.run(
        RuntimeBackendRequest(
            run_id="run-oh-skill-001",
            question="query-service query timeout and slow query",
            analysis_context={
                "service_name": "query-service",
                "component_type": "query",
                "namespace": "islap",
            },
            runtime_options={
                "auto_exec_readonly": False,
                "enable_skills": True,
                "max_skills": 3,
            },
        )
    )

    assert result.inner_engine == "openhands-v1"
    assert result.payload["mode"] == "approval_gated"
    assert result.payload["selected_skills"]
    assert result.payload["tool_calls"]
    first = result.payload["tool_calls"][0]
    assert first["tool_name"] == "command.exec"
    assert first["skill_name"] in result.payload["selected_skills"]
    assert first["step_id"]
    assert first["command"]
    assert first["confirmed"] is False
    assert first["elevated"] is False
    assert first["command_spec"]["tool"] in {"generic_exec", "kubectl_clickhouse_query"}


def test_openhands_backend_uses_provider_output_for_thoughts_and_tool_calls(monkeypatch):
    fake_provider_module = ModuleType("ai.runtime_v4.backend.openhands_provider")

    class _FakeProvider:
        def run(self, request):
            assert request.run_id == "run-oh-provider-001"
            assert request.question == "排查 query-service timeout"
            return {
                "mode": "approval_gated",
                "thoughts": [
                    "先确认 query-service 是否普遍超时",
                    "再检查 clickhouse query_log",
                ],
                "tool_calls": [
                    {
                        "tool_name": "kubectl_clickhouse_query",
                        "tool_args": {
                            "query": "SELECT count() FROM system.query_log",
                            "namespace": "islap",
                            "pod_name": "clickhouse-0",
                            "purpose": "检查慢查询",
                            "title": "查看 clickhouse query_log",
                            "timeout_s": 30,
                        },
                    }
                ],
            }

    fake_provider_module.get_openhands_provider = lambda: _FakeProvider()
    monkeypatch.setitem(sys.modules, "ai.runtime_v4.backend.openhands_provider", fake_provider_module)

    backend = OpenHandsBackend()
    result = backend.run(
        RuntimeBackendRequest(
            run_id="run-oh-provider-001",
            question="排查 query-service timeout",
            analysis_context={"service_name": "query-service"},
            runtime_options={"auto_exec_readonly": False, "enable_skills": False},
        )
    )

    assert result.inner_engine == "openhands-v1"
    assert result.payload["mode"] == "approval_gated"
    assert result.payload["thoughts"] == [
        "先确认 query-service 是否普遍超时",
        "再检查 clickhouse query_log",
    ]
    assert len(result.payload["tool_calls"]) == 1
    first = result.payload["tool_calls"][0]
    assert first["tool_name"] == "command.exec"
    assert first["command_spec"]["tool"] == "kubectl_clickhouse_query"
    assert first["command_spec"]["args"]["query"] == "SELECT count() FROM system.query_log"
    assert first["title"] == "查看 clickhouse query_log"


def test_openhands_backend_preserves_local_skill_actions_when_provider_returns_tool_calls(monkeypatch):
    fake_provider_module = ModuleType("ai.runtime_v4.backend.openhands_provider")

    class _FakeProvider:
        def run(self, request):
            assert request.run_id == "run-oh-merge-001"
            return {
                "provider": "fake-provider",
                "mode": "approval_gated",
                "thoughts": ["先确认 query-service timeout 范围"],
                "tool_calls": [
                    {
                        "tool_name": "generic_exec",
                        "tool_args": {
                            "command": "kubectl -n islap get pods -l app=query-service",
                            "purpose": "check query-service pods",
                            "target_kind": "k8s_workload",
                            "target_identity": "deployment:query-service",
                            "timeout_s": 20,
                        },
                    }
                ],
            }

    fake_provider_module.get_openhands_provider = lambda: _FakeProvider()
    monkeypatch.setitem(sys.modules, "ai.runtime_v4.backend.openhands_provider", fake_provider_module)

    backend = OpenHandsBackend()
    result = backend.run(
        RuntimeBackendRequest(
            run_id="run-oh-merge-001",
            question="query-service query timeout and slow query",
            analysis_context={
                "service_name": "query-service",
                "component_type": "query",
                "namespace": "islap",
            },
            runtime_options={"auto_exec_readonly": False, "enable_skills": True},
        )
    )

    assert result.payload["provider"] == "fake-provider"
    assert result.payload["selected_skills"]
    assert len(result.payload["tool_calls"]) >= 2
    assert result.payload["tool_calls"][0]["purpose"] == "check query-service pods"
    assert any(item.get("skill_name") for item in result.payload["tool_calls"][1:])


def test_openhands_backend_can_load_provider_from_factory_env(monkeypatch):
    fake_provider_module = ModuleType("fake_openhands_factory")

    class _FactoryProvider:
        def run(self, request):
            return {
                "provider": "factory-provider",
                "mode": "approval_gated",
                "thoughts": ["factory thought"],
                "tool_calls": [
                    {
                        "tool_name": "generic_exec",
                        "tool_args": {
                            "command": "kubectl -n islap get pods",
                            "purpose": "collect inventory",
                            "target_kind": "k8s_cluster",
                            "target_identity": "namespace:islap",
                            "timeout_s": 20,
                        },
                    }
                ],
            }

    fake_provider_module.create_provider = lambda: _FactoryProvider()
    monkeypatch.setitem(sys.modules, "fake_openhands_factory", fake_provider_module)
    monkeypatch.setenv(
        "AI_RUNTIME_V4_OPENHANDS_PROVIDER_FACTORY",
        "fake_openhands_factory:create_provider",
    )

    backend = OpenHandsBackend()
    result = backend.run(
        RuntimeBackendRequest(
            run_id="run-oh-factory-001",
            question="排查 query-service timeout",
            analysis_context={"service_name": "query-service"},
            runtime_options={"auto_exec_readonly": False, "enable_skills": False},
        )
    )

    assert result.payload["provider"] == "factory-provider"
    assert result.payload["thoughts"] == ["factory thought"]
    assert result.payload["tool_calls"][0]["command"] == "kubectl -n islap get pods"
