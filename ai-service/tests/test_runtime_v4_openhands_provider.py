"""Tests for the OpenHands provider helper integration."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from ai.runtime_v4.backend.base import RuntimeBackendRequest
from ai.runtime_v4.backend.openhands_provider import (
    get_openhands_provider,
    reset_openhands_provider,
    validate_openhands_provider_readiness,
)


def _sample_request() -> RuntimeBackendRequest:
    return RuntimeBackendRequest(
        run_id="run-oh-helper-001",
        question="排查 query-service timeout",
        analysis_context={"service_name": "query-service", "namespace": "islap"},
        runtime_options={"auto_exec_readonly": False, "enable_skills": True},
    )


def test_openhands_provider_requires_existing_helper_paths_when_enabled(monkeypatch, tmp_path):
    helper_script = tmp_path / "openhands_helper.py"
    helper_script.write_text("print('placeholder')\n", encoding="utf-8")

    monkeypatch.setenv("AI_RUNTIME_V4_OPENHANDS_HELPER_ENABLED", "true")
    monkeypatch.setenv("AI_RUNTIME_V4_OPENHANDS_HELPER_PYTHON", str(tmp_path / "missing-python"))
    monkeypatch.setenv("AI_RUNTIME_V4_OPENHANDS_HELPER_SCRIPT", str(helper_script))
    reset_openhands_provider()

    with pytest.raises(RuntimeError, match="OpenHands helper python not found"):
        validate_openhands_provider_readiness()


def test_openhands_provider_uses_subprocess_helper_when_enabled(monkeypatch, tmp_path):
    helper_script = tmp_path / "openhands_helper.py"
    helper_script.write_text("# helper placeholder\n", encoding="utf-8")

    captured = {}

    def _fake_run(args, *, input, capture_output, text, check, env, timeout):
        captured["args"] = list(args)
        captured["input"] = input
        captured["capture_output"] = capture_output
        captured["text"] = text
        captured["check"] = check
        captured["env_openhands_enabled"] = env.get("AI_RUNTIME_V4_OPENHANDS_ENABLED")
        captured["timeout"] = timeout
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout=json.dumps(
                {
                    "provider": "openharness-subprocess",
                    "mode": "approval_gated",
                    "thoughts": ["先看 query-service 是否普遍超时"],
                    "tool_calls": [
                        {
                            "tool_name": "generic_exec",
                            "tool_args": {
                                "command": "kubectl -n islap get pods",
                                "purpose": "collect pod inventory",
                                "target_kind": "k8s_cluster",
                                "target_identity": "namespace:islap",
                                "timeout_s": 20,
                            },
                        }
                    ],
                }
            ),
            stderr="",
        )

    monkeypatch.setenv("AI_RUNTIME_V4_OPENHANDS_HELPER_ENABLED", "true")
    monkeypatch.setenv("AI_RUNTIME_V4_OPENHANDS_HELPER_PYTHON", sys.executable)
    monkeypatch.setenv("AI_RUNTIME_V4_OPENHANDS_HELPER_SCRIPT", str(helper_script))
    monkeypatch.setattr("ai.runtime_v4.backend.openhands_provider.subprocess.run", _fake_run)
    reset_openhands_provider()

    provider = get_openhands_provider()
    result = provider.run(_sample_request())

    assert captured["args"] == [sys.executable, str(helper_script)]
    helper_input = json.loads(captured["input"])
    assert helper_input["request"]["run_id"] == "run-oh-helper-001"
    assert helper_input["request"]["analysis_context"]["service_name"] == "query-service"
    assert captured["capture_output"] is True
    assert captured["text"] is True
    assert captured["check"] is False
    assert captured["env_openhands_enabled"] == "true"
    assert captured["timeout"] > 0
    assert result["provider"] == "openharness-subprocess"
    assert result["tool_calls"][0]["tool_name"] == "generic_exec"


def test_openhands_provider_fails_closed_on_invalid_helper_output(monkeypatch, tmp_path):
    helper_script = tmp_path / "openhands_helper.py"
    helper_script.write_text("# helper placeholder\n", encoding="utf-8")

    def _fake_run(args, *, input, capture_output, text, check, env, timeout):
        _ = (args, input, capture_output, text, check, env, timeout)
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="not-json", stderr="")

    monkeypatch.setenv("AI_RUNTIME_V4_OPENHANDS_HELPER_ENABLED", "true")
    monkeypatch.setenv("AI_RUNTIME_V4_OPENHANDS_HELPER_PYTHON", sys.executable)
    monkeypatch.setenv("AI_RUNTIME_V4_OPENHANDS_HELPER_SCRIPT", str(helper_script))
    monkeypatch.setattr("ai.runtime_v4.backend.openhands_provider.subprocess.run", _fake_run)
    reset_openhands_provider()

    provider = get_openhands_provider()

    with pytest.raises(RuntimeError, match="invalid JSON"):
        provider.run(_sample_request())
