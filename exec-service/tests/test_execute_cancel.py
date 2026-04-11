"""
Tests for exec-service run cancellation.
"""

import asyncio
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import core.runtime_service as runtime_service_module
from api.execute import CommandRunCreateRequest, cancel_run, create_command_run, get_run, get_run_events


@pytest.fixture(autouse=True)
def _configure_controlled_executor_templates(monkeypatch):
    """取消测试也必须走受控模板执行，禁止依赖 local fallback。"""
    monkeypatch.setenv(
        "EXEC_EXECUTOR_TEMPLATE__BUSYBOX_READONLY",
        'python3 -c "import sys; print(sys.argv[1])" {command_quoted}',
    )


def test_cancel_run_marks_run_cancelled(monkeypatch):
    monkeypatch.setenv("EXEC_ALLOWED_HEADS", "echo")
    runtime_service_module._exec_runtime_service = None

    async def _slow_dispatch_command(
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
        dispatch = {
            "effective_executor_type": executor_type,
            "effective_executor_profile": executor_profile,
            "dispatch_backend": "template_executor",
            "dispatch_mode": "remote_template",
            "dispatch_reason": "test slow dispatch",
            "dispatch_template_env": "EXEC_EXECUTOR_TEMPLATE__BUSYBOX_READONLY",
        }
        if on_dispatch_resolved is not None:
            await on_dispatch_resolved(dispatch)
        await asyncio.sleep(30)
        return {
            "exit_code": 0,
            "timed_out": False,
            "duration_ms": 30000,
            "dispatch": dispatch,
        }

    monkeypatch.setattr(runtime_service_module, "dispatch_command", _slow_dispatch_command)

    async def _exercise() -> None:
        created = await create_command_run(
            CommandRunCreateRequest(
                session_id="sess-cancel-001",
                message_id="msg-cancel-001",
                action_id="act-cancel-001",
                command="echo cancel-me",
                purpose="验证取消运行流程",
                timeout_seconds=10,
            )
        )
        run_id = created["run"]["run_id"]
        await cancel_run(run_id)

        runtime = runtime_service_module.get_exec_runtime_service()
        await runtime.wait_for_run(run_id)
        fetched = await get_run(run_id)
        assert fetched["run"]["status"] == "cancelled"

        events_payload = await get_run_events(run_id, after_seq=0, limit=20)
        event_types = [item["event_type"] for item in events_payload["events"]]
        assert "command_cancel_requested" in event_types
        assert "command_cancelled" in event_types

    asyncio.run(_exercise())
