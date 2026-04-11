"""
Security regression tests for follow-up command execution and auto-exec guard.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest


@pytest.fixture(autouse=True)
def _patch_run_blocking():
    """单测中直连同步调用，避免线程池在 CI 环境偶发卡住。"""
    from ai.followup_command import _normalize_followup_command_line, _resolve_followup_command_meta

    ticket_store: dict[str, dict[str, str]] = {}
    ticket_seq = {"value": 0}
    run_seq = {"value": 0}

    async def _direct_run_blocking(func, *args, **kwargs):
        return func(*args, **kwargs)

    async def _fake_precheck_controlled_command(**kwargs):
        command = _normalize_followup_command_line(str(kwargs.get("command") or ""))
        command_meta, _ = _resolve_followup_command_meta(command)
        payload = {
            "status": "ok",
            "session_id": str(kwargs.get("session_id") or ""),
            "message_id": str(kwargs.get("message_id") or ""),
            "action_id": str(kwargs.get("action_id") or ""),
            "command": command,
            "command_type": str(command_meta.get("command_type") or "unknown"),
            "risk_level": str(command_meta.get("risk_level") or "high"),
            "requires_write_permission": bool(command_meta.get("requires_write_permission")),
            "requires_elevation": bool(command_meta.get("requires_write_permission")),
            "requires_confirmation": False,
            "message": str(command_meta.get("reason") or ""),
        }
        if not bool(command_meta.get("supported")):
            payload["status"] = "permission_required"
            payload["requires_elevation"] = False
            return payload
        if bool(command_meta.get("requires_write_permission")):
            ticket_seq["value"] += 1
            ticket_id = f"exec-ticket-test-{ticket_seq['value']:04d}"
            ticket_store[ticket_id] = {
                "session_id": payload["session_id"],
                "message_id": payload["message_id"],
                "action_id": payload["action_id"],
                "command": command,
            }
            payload["status"] = "elevation_required"
            payload["requires_confirmation"] = True
            payload["requires_elevation"] = True
            payload["confirmation_ticket"] = ticket_id
        return payload

    async def _fake_execute_controlled_command(**kwargs):
        precheck = await _fake_precheck_controlled_command(**kwargs)
        status = str(precheck.get("status") or "").lower()
        if status == "permission_required":
            return precheck
        if status == "elevation_required":
            if not bool(kwargs.get("confirmed")) or not bool(kwargs.get("elevated")):
                return precheck
            provided_ticket = str(kwargs.get("confirmation_ticket") or "")
            ticket_payload = ticket_store.pop(provided_ticket, None)
            if not ticket_payload or str(ticket_payload.get("command")) != str(precheck.get("command")):
                failed = dict(precheck)
                failed["status"] = "confirmation_required"
                failed["message"] = "confirmation ticket invalid: ticket_not_found"
                return failed
        run_seq["value"] += 1
        command = str(precheck.get("command") or "")
        stdout = ""
        if command.startswith("echo "):
            stdout = f"{command.split(' ', 1)[1]}\n"
        return {
            **precheck,
            "status": "executed",
            "run_id": f"cmdrun-test-{run_seq['value']:04d}",
            "exit_code": 0,
            "duration_ms": 12,
            "stdout": stdout,
            "stderr": "",
            "output_truncated": False,
            "timed_out": False,
        }

    with patch("api.ai._run_blocking", new=_direct_run_blocking), patch(
        "api.ai.precheck_controlled_command",
        new=_fake_precheck_controlled_command,
    ), patch(
        "api.ai.execute_controlled_command",
        new=_fake_execute_controlled_command,
    ):
        yield


def _build_store(command_block: str, metadata: dict | None = None):
    class _StoreStub:
        def __init__(self):
            self.context = {}

        def get_session_with_messages(self, session_id: str):
            return {
                "session": {"session_id": session_id, "context": dict(self.context)},
                "messages": [],
            }

        def get_message_by_id(self, session_id: str, message_id: str):
            return {
                "role": "assistant",
                "content": command_block,
                "metadata": metadata or {},
            }

        def update_session(self, session_id: str, **changes):
            context = changes.get("context")
            self.context = dict(context) if isinstance(context, dict) else {}
            return True

    return _StoreStub()


def test_execute_followup_command_awk_system_requires_write_permission():
    from api.ai import FollowUpCommandExecuteRequest, execute_followup_command

    mock_store = _build_store("```bash\nawk 'BEGIN {system(\"echo test\")}' /dev/null\n```")
    request = FollowUpCommandExecuteRequest(
        command="awk 'BEGIN {system(\"echo test\")}' /dev/null",
        confirmed=True,
        timeout_seconds=10,
    )

    with patch("api.ai.get_ai_session_store", return_value=mock_store):
        result = asyncio.run(execute_followup_command("sess-cmd-001", "msg-cmd-awk-001", request))

    assert result["status"] == "elevation_required"
    assert result["command_type"] == "repair"
    assert result["requires_write_permission"] is True


def test_execute_followup_command_sed_write_clause_requires_write_permission():
    from api.ai import FollowUpCommandExecuteRequest, execute_followup_command

    mock_store = _build_store("```bash\nsed -n '1w /tmp/sed-out' /etc/hosts\n```")
    request = FollowUpCommandExecuteRequest(
        command="sed -n '1w /tmp/sed-out' /etc/hosts",
        confirmed=True,
        timeout_seconds=10,
    )

    with patch("api.ai.get_ai_session_store", return_value=mock_store):
        result = asyncio.run(execute_followup_command("sess-cmd-001", "msg-cmd-sed-001", request))

    assert result["status"] == "elevation_required"
    assert result["command_type"] == "repair"
    assert result["requires_write_permission"] is True


def test_execute_followup_command_curl_output_file_requires_write_permission():
    from api.ai import FollowUpCommandExecuteRequest, execute_followup_command

    mock_store = _build_store("```bash\ncurl -s https://example.com -o /tmp/health.txt\n```")
    request = FollowUpCommandExecuteRequest(
        command="curl -s https://example.com -o /tmp/health.txt",
        confirmed=True,
        timeout_seconds=10,
    )

    with patch("api.ai.get_ai_session_store", return_value=mock_store):
        result = asyncio.run(execute_followup_command("sess-cmd-001", "msg-cmd-curl-001", request))

    assert result["status"] == "elevation_required"
    assert result["command_type"] == "repair"
    assert result["requires_write_permission"] is True


def test_execute_followup_command_test_permissive_allows_subshell(monkeypatch):
    from api.ai import FollowUpCommandExecuteRequest, execute_followup_command

    monkeypatch.setenv("AI_FOLLOWUP_COMMAND_TEST_PERMISSIVE", "true")
    mock_store = _build_store(
        "```bash\nkubectl -nislapexec -it$(kubectl -nislapgetpods -lapp=clickhouse -ojsonpath='{.items[0].metadata.name}') --clickhouse -client --query \"SHOWCREATETABLElogs.traces\"\n```"
    )
    request = FollowUpCommandExecuteRequest(
        command="kubectl -nislapexec -it$(kubectl -nislapgetpods -lapp=clickhouse -ojsonpath='{.items[0].metadata.name}') --clickhouse -client --query \"SHOWCREATETABLElogs.traces\"",
        confirmed=False,
        timeout_seconds=10,
    )

    with patch("api.ai.get_ai_session_store", return_value=mock_store):
        result = asyncio.run(execute_followup_command("sess-cmd-001", "msg-cmd-permissive-001", request))

    assert result["status"] != "permission_required"


def test_followup_command_meta_repairs_clickhouse_placeholder_flag_spacing():
    from ai.followup_command import _normalize_followup_command_line

    raw = "clickhouse-client --host<HOST>--port<PORT>--user<USER>--password<PASSWORD>--database<DATABASE>--query \"SHOWCREATETABLElogs.traces\""
    normalized = _normalize_followup_command_line(raw)

    assert "--host <HOST>" in normalized
    assert "--port <PORT>" in normalized
    assert "--database <DATABASE>" in normalized
    assert "SHOW CREATE TABLE logs.traces" in normalized


def test_followup_command_meta_repairs_compact_clickhouse_query_keywords():
    from ai.followup_command import _normalize_followup_command_line

    raw = (
        "kubectl -n islap exec -it $(kubectl -n islap get pods -l app=clickhouse "
        "-o jsonpath='{.items[0].metadata.name}') -- clickhouse-client --query "
        "\"SELECTpartition,name,rows,bytes_on_diskFROMsystem.partsWHEREtable='traces'"
        "ANDdatabase='logs'ORDERBYpartitionDESCLIMIT10\""
    )
    normalized = _normalize_followup_command_line(raw)

    assert "-n islap exec -i $(" in normalized
    assert (
        "--query \"SELECT partition,name,rows,bytes_on_disk FROM system.parts "
        "WHERE table='traces' AND database='logs' ORDER BY partition DESC LIMIT 10\""
    ) in normalized


def test_followup_command_meta_repairs_kubectl_pipeline_spacing():
    from ai.followup_command import _normalize_followup_command_line

    raw = "kubectl logs--tail=100 -l app=query-service|grep-ierror"
    normalized = _normalize_followup_command_line(raw)

    assert normalized == "kubectl logs --tail=100 -l app=query-service | grep -i error"


def test_followup_command_meta_repairs_kubectl_logs_namespace_glued():
    from ai.followup_command import _normalize_followup_command_line

    raw = "kubectl logs-nislap --tail=100 -l app=query-service | grep -i error"
    normalized = _normalize_followup_command_line(raw)

    assert normalized == "kubectl logs -n islap --tail=100 -l app=query-service | grep -i error"


def test_followup_command_meta_repairs_kubectldescribepod_compact():
    from ai.followup_command import _normalize_followup_command_line

    raw = "kubectldescribepod $(kubectl get pods -l app=query-service -o jsonpath='{.items[0].metadata.name}')"
    normalized = _normalize_followup_command_line(raw)

    assert normalized.startswith("kubectl describe pod $(")


def test_followup_command_meta_repairs_compact_pipeline_tokens():
    from ai.followup_command import _normalize_followup_command_line

    raw = (
        "kubectl logs--tail=50$(kubectl get pods -l app=query-service "
        "-o jsonpath='{.items[0].metadata.name}')|grep -A20'Events:'|head-20"
    )
    normalized = _normalize_followup_command_line(raw)

    assert "kubectl logs --tail=50 $(" in normalized
    assert "grep -A20 'Events:'" in normalized
    assert "| head -20" in normalized


def test_followup_command_meta_repairs_glued_selector_flags():
    from ai.followup_command import _normalize_followup_command_line

    raw = "kubectl logs -l app=query-service--timestamps | tail-50"
    normalized = _normalize_followup_command_line(raw)
    assert "-l app=query-service --timestamps" in normalized
    assert "| tail -50" in normalized

    raw_get = "kubectl get pods -l app=query-service-owide"
    normalized_get = _normalize_followup_command_line(raw_get)
    assert normalized_get.endswith("-l app=query-service -o wide")


def test_followup_command_meta_repairs_namespace_plus_getpods_glued_after_space():
    from ai.followup_command import _normalize_followup_command_line

    raw = (
        "kubectl -n islap exec -i $(kubectl -n islapgetpods -l app=clickhouse "
        "-o jsonpath='{.items[0].metadata.name}') -- clickhouse-client --query "
        "\"DESCRIBE TABLE logs.traces\""
    )
    normalized = _normalize_followup_command_line(raw)

    assert "$(kubectl -n islap get pods -l app=clickhouse" in normalized
    assert "\"DESCRIBE TABLE logs.traces\"" in normalized


def test_followup_command_meta_repairs_compact_long_flags_for_kubectl_logs():
    from ai.followup_command import _normalize_followup_command_line

    raw = "kubectl logs --namespaceislap --selectorapp=query-service --tail50 --no-headers"
    normalized = _normalize_followup_command_line(raw)

    assert normalized == "kubectl logs --namespace islap --selector app=query-service --tail=50 --no-headers"


def test_follow_up_auto_exec_allows_safe_curl_query_command(monkeypatch):
    from api.ai import FollowUpRequest, follow_up_analysis

    monkeypatch.setenv("AI_FOLLOWUP_ENGINE", "langchain")
    monkeypatch.setenv("AI_FOLLOWUP_AUTO_EXEC_READONLY_ENABLED", "true")
    monkeypatch.setenv("AI_FOLLOWUP_AUTO_EXEC_READONLY_MAX_ACTIONS", "1")
    monkeypatch.setenv("AI_FOLLOWUP_COMMAND_EXEC_ENABLED", "true")

    mock_store = Mock()
    mock_store.create_session.return_value = SimpleNamespace(session_id="sess-followup-autoexec-skip-001")
    mock_store.get_session.return_value = None
    mock_store.get_messages.return_value = []
    mock_store.append_messages.return_value = False
    mock_store.update_session.return_value = True

    mock_runtime = AsyncMock(
        return_value={
            "answer": "执行步骤：\n- P1 curl -G https://example.com/health",
            "analysis_method": "langchain",
            "llm_timeout_fallback": False,
            "actions": [
                {
                    "id": "langchain-act-1",
                    "priority": 1,
                    "title": "curl -G https://example.com/health",
                    "action": "curl -G https://example.com/health",
                    "command": "curl -G https://example.com/health",
                    "command_spec": {
                        "tool": "generic_exec",
                        "args": {
                            "command_argv": ["curl", "-G", "https://example.com/health"],
                            "target_kind": "runtime_node",
                            "target_identity": "runtime:local",
                            "timeout_s": 20,
                        },
                    },
                    "expected_outcome": "返回健康状态",
                }
            ],
        }
    )

    request = FollowUpRequest(
        question="自动执行受控命令测试",
        use_llm=True,
        analysis_context={
            "analysis_type": "log",
            "service_name": "query-service",
            "input_text": "health check",
            "result": {"overview": {"description": "健康检查"}},
        },
    )

    async def _fake_precheck_command(**kwargs):
        command = str(kwargs.get("command") or "")
        return {
            "status": "ok",
            "command": command,
            "command_type": "query",
            "risk_level": "low",
            "requires_write_permission": False,
            "requires_elevation": False,
            "dispatch_requires_template": False,
            "dispatch_degraded": False,
        }

    async def _fake_create_command_run(**kwargs):
        command = str(kwargs.get("command") or "")
        return {
            "status": "executed",
            "command": command,
            "command_type": "query",
            "risk_level": "low",
            "exit_code": 0,
            "duration_ms": 12,
            "stdout": "{\"status\":\"ok\"}",
            "stderr": "",
            "output_truncated": False,
            "timed_out": False,
        }

    with patch("api.ai.get_ai_session_store", return_value=mock_store), patch(
        "api.ai._is_llm_configured", return_value=True
    ), patch("api.ai.get_llm_service", return_value=Mock()), patch(
        "api.ai.run_followup_langchain",
        mock_runtime,
    ), patch(
        "ai.followup_orchestration_helpers.precheck_command",
        _fake_precheck_command,
    ), patch(
        "ai.followup_orchestration_helpers.create_command_run",
        _fake_create_command_run,
    ):
        result = asyncio.run(follow_up_analysis(request))

    observations = result.get("action_observations") or []
    assert observations
    assert observations[0].get("status") == "executed"
    assert observations[0].get("auto_executed") is False
    assert "ok" in str(observations[0].get("stdout") or "")
