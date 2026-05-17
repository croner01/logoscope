"""Tests for scripts/ai-runtime-manual-entry.py payload wiring."""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path


def _load_manual_entry_module():
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "ai-runtime-manual-entry.py"
    spec = importlib.util.spec_from_file_location("ai_runtime_manual_entry", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_create_run_v2_supports_openhands_backend(monkeypatch, tmp_path):
    module = _load_manual_entry_module()
    calls = []

    def _fake_request_json(url, method="GET", payload=None):
        calls.append({"url": url, "method": method, "payload": payload or {}})
        if url.endswith("/api/v2/threads"):
            return {"thread": {"thread_id": "thr-manual-openhands-001"}}
        if "/api/v2/threads/thr-manual-openhands-001/runs" in url:
            return {
                "run": {
                    "run_id": "run-manual-openhands-001",
                    "thread_id": "thr-manual-openhands-001",
                    "assistant_message_id": "msg-assistant-001",
                },
                "workflow_id": "wf-manual-openhands-001",
            }
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr(module, "request_json", _fake_request_json)
    monkeypatch.setattr(module, "STATE_FILE", tmp_path / "state.json")

    module.cmd_create_run(
        argparse.Namespace(
            api_version="v2",
            question="query-service query timeout and slow query",
            analysis_type="log",
            service="query-service",
            session_id="sess-manual-openhands-001",
            conversation_id="conv-manual-openhands-001",
            title="manual OpenHands check",
            mode="passive",
            runtime_backend="openhands",
            auto_exec_readonly=False,
            enable_skills=True,
            max_skills=1,
        )
    )

    assert calls[0]["url"].endswith("/api/v2/threads")
    assert calls[0]["payload"]["conversation_id"] == "conv-manual-openhands-001"
    run_payload = calls[1]["payload"]
    assert run_payload["runtime_backend"] == "openhands"
    assert run_payload["analysis_context"]["service_name"] == "query-service"
    assert run_payload["runtime_options"]["auto_exec_readonly"] is False
    assert run_payload["runtime_options"]["enable_skills"] is True
    assert run_payload["runtime_options"]["max_skills"] == 1
    assert module.load_state()["run_id"] == "run-manual-openhands-001"
    assert module.load_state()["thread_id"] == "thr-manual-openhands-001"


def test_exec_action_posts_preview_action_to_v2_endpoint(monkeypatch):
    module = _load_manual_entry_module()
    calls = []

    def _fake_request_json(url, method="GET", payload=None):
        calls.append({"url": url, "method": method, "payload": payload or {}})
        return {"status": "completed", "tool_call_id": "tool-manual-001"}

    monkeypatch.setattr(module, "request_json", _fake_request_json)
    monkeypatch.setattr(module, "resolve_run_id", lambda explicit: explicit or "run-from-state")

    module.cmd_exec_action(
        argparse.Namespace(
            run_id="run-manual-openhands-001",
            action_id="planned-preview-1",
            confirmed=False,
            elevated=False,
            approval_token="",
            timeout_seconds=20,
        )
    )

    assert calls == [
        {
            "url": "http://127.0.0.1:8090/api/v2/runs/run-manual-openhands-001/actions/command",
            "method": "POST",
            "payload": {
                "action_id": "planned-preview-1",
                "purpose": "",
                "title": "",
                "command": "",
                "command_spec": {},
                "confirmed": False,
                "elevated": False,
                "approval_token": "",
                "timeout_seconds": 20,
            },
        }
    ]
