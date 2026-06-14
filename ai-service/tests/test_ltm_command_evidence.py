"""
LTM 负向事实传递测试 — 验证跨会话命令级记忆增强。

测试 _build_followup_long_term_memory() 从消息 metadata 中提取
action_observations 并正确分离成功/失败命令到 LTM summary 中。
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from ai.followup_runtime_helpers import _build_followup_long_term_memory
from ai.session_history import AISessionMessage


def _make_message(
    session_id: str,
    role: str,
    content: str,
    action_observations: Optional[List[Dict[str, Any]]] = None,
    msg_index: int = 0,
) -> AISessionMessage:
    """Helper: 构造带 action_observations 的测试消息。"""
    metadata: Dict[str, Any] = {}
    if action_observations:
        metadata["action_observations"] = action_observations
    return AISessionMessage(
        session_id=session_id,
        message_id=f"msg-{msg_index}",
        msg_index=msg_index,
        role=role,
        content=content,
        metadata=metadata,
        created_at="2026-06-14T00:00:00Z",
    )


def _make_session(session_id: str, service_name: str = "test-svc", summary_text: str = "测试结论"):
    """Helper: 构造 session mock，支持 getattr。"""
    session = MagicMock()
    session.session_id = session_id
    session.service_name = service_name
    session.trace_id = ""
    session.updated_at = "2026-06-14T00:00:00Z"
    session.title = ""
    session.summary_text = summary_text
    session.input_text = ""
    return session


class MockSessionStore:
    """模拟 AISessionStore，返回受控的 session + message 数据。"""

    def __init__(self):
        self._sessions: Dict[str, List[AISessionMessage]] = {}

    def set_messages(self, session_id: str, messages: List[AISessionMessage]) -> None:
        self._sessions[session_id] = messages

    def get_messages(self, session_id: str, limit: int = 500) -> List[AISessionMessage]:
        return (self._sessions.get(session_id) or [])[:limit]

    def get_messages_light(self, session_id: str, limit: int = 500) -> List[AISessionMessage]:
        return self.get_messages(session_id, limit)

    def list_sessions(
        self,
        limit: int = 10,
        offset: int = 0,
        analysis_type: str = "",
        service_name: str = "",
        include_archived: bool = False,
        search_query: str = "",
        pinned_first: bool = True,
        sort_by: str = "updated_at",
        sort_order: str = "desc",
    ) -> List[Any]:
        key = service_name or search_query or "default"
        fakes = {
            "test-svc": [
                _make_session("ais-001"),
                _make_session("ais-002"),
            ],
            "peak-cache": [
                _make_session("ais-pc-001", "peak-cache", "peak-cache 认证配置问题"),
                _make_session("ais-pc-002", "peak-cache", "peak-cache 认证超时分析"),
            ],
        }
        return fakes.get(key, [])[:limit]


@pytest.fixture
def store():
    return MockSessionStore()


@pytest.fixture
def run_blocking():
    async def wrapper(func, *args, **kwargs):
        return func(*args, **kwargs)
    return wrapper


async def _run_ltm(store, run_blocking, **overrides):
    """Helper: 调用 LTM 并返回结果。"""
    params = dict(
        session_store=store,
        run_blocking=run_blocking,
        analysis_session_id="ais-current",
        analysis_context={"service_name": "test-svc"},
        question="测试问题",
    )
    params.update(overrides)
    return await _build_followup_long_term_memory(**params)


# ─── 测试用例 ───


@pytest.mark.asyncio
async def test_ltm_extracts_successful_commands(store, run_blocking):
    """成功命令 (exit_code=0, status=completed) 应出现在 LTM summary 的 ✓成功命令 中。"""
    store.set_messages("ais-001", [
        _make_message("ais-001", "user", "诊断问题", msg_index=1),
        _make_message("ais-001", "assistant", "正在执行诊断", msg_index=2, action_observations=[
            {"command": "ls /etc/config/", "exit_code": 0, "status": "completed"},
            {"command": "cat /etc/config/peak-cache.yaml", "exit_code": 0, "status": "completed"},
        ]),
    ])
    store.set_messages("ais-002", [
        _make_message("ais-002", "user", "另一个问题", msg_index=1),
        _make_message("ais-002", "assistant", "诊断完毕", msg_index=2, action_observations=[
            {"command": "kubectl logs peak-cache-0 -n islap", "exit_code": 0, "status": "completed"},
        ]),
    ])

    result = await _run_ltm(store, run_blocking)

    assert result["enabled"] is True
    assert result["hits"] == 2
    summary = result["summary"]
    assert "✓成功命令" in summary
    assert "ls /etc/config/" in summary
    assert "cat /etc/config/peak-cache.yaml" in summary
    assert "kubectl logs peak-cache-0" in summary


@pytest.mark.asyncio
async def test_ltm_extracts_failed_commands(store, run_blocking):
    """失败命令 (exit_code≠0 或 blocked/permission_required) 应出现在 ✗失败/被拒命令 中。"""
    store.set_messages("ais-001", [
        _make_message("ais-001", "user", "问题", msg_index=1),
        _make_message("ais-001", "assistant", "诊断中", msg_index=2, action_observations=[
            {"command": "ls /etc/peak-cache/", "exit_code": 1, "status": "completed"},
            {"command": "clickhouse_query SELECT ...", "exit_code": 0, "status": "permission_required"},
        ]),
    ])

    result = await _run_ltm(store, run_blocking)

    summary = result["summary"]
    assert "✗失败/被拒命令" in summary
    assert "ls /etc/peak-cache/" in summary
    assert "exit=1" in summary
    assert "clickhouse_query" in summary
    assert "permission_required" in summary


@pytest.mark.asyncio
async def test_ltm_mixed_commands(store, run_blocking):
    """同一会话同时有成功和失败命令，应正确分离。"""
    store.set_messages("ais-001", [
        _make_message("ais-001", "user", "问题", msg_index=1),
        _make_message("ais-001", "assistant", "诊断中", msg_index=2, action_observations=[
            {"command": "ls /etc/peak-cache/", "exit_code": 1, "status": "completed"},
            {"command": "cat /tmp/peak-cache/config.yaml", "exit_code": 0, "status": "completed"},
            {"command": "kubectl logs peak-cache-0 -n islap", "exit_code": 0, "status": "completed"},
        ]),
    ])

    result = await _run_ltm(store, run_blocking)

    summary = result["summary"]
    assert "✗失败/被拒命令" in summary
    assert "ls /etc/peak-cache/" in summary
    assert "✓成功命令" in summary
    assert "cat /tmp/peak-cache/config.yaml" in summary
    assert "kubectl logs peak-cache-0" in summary


@pytest.mark.asyncio
async def test_ltm_no_observations(store, run_blocking):
    """没有 action_observations 的会话不应包含命令摘要，也不应报错。"""
    store.set_messages("ais-001", [
        _make_message("ais-001", "user", "问题", msg_index=1),
        _make_message("ais-001", "assistant", "纯文本回答，无命令", msg_index=2),
    ])

    result = await _run_ltm(store, run_blocking)

    summary = result["summary"]
    assert "summary=" in summary
    assert "✓成功命令" not in summary
    assert "✗失败/被拒命令" not in summary


@pytest.mark.asyncio
async def test_ltm_dedup_same_command_in_multiple_messages(store, run_blocking):
    """同一命令出现在多条消息中应去重。"""
    store.set_messages("ais-001", [
        _make_message("ais-001", "assistant", "第一次诊断", msg_index=1, action_observations=[
            {"command": "ls /etc/peak-cache/", "exit_code": 1, "status": "completed"},
        ]),
        _make_message("ais-001", "assistant", "第二次诊断", msg_index=2, action_observations=[
            {"command": "ls /etc/peak-cache/", "exit_code": 1, "status": "completed"},
        ]),
    ])

    result = await _run_ltm(store, run_blocking)

    summary = result["summary"]
    assert summary.count("ls /etc/peak-cache/") == 1


@pytest.mark.asyncio
async def test_ltm_with_empty_metadata(store, run_blocking):
    """metadata 为空或格式异常不应影响 LTM 构建。"""
    msg = _make_message("ais-001", "assistant", "回答", msg_index=1)
    msg.metadata = None  # 异常空值
    store.set_messages("ais-001", [msg])
    store.set_messages("ais-002", [])

    result = await _run_ltm(store, run_blocking)

    assert result["hits"] >= 1
    assert "summary=" in result["summary"]


@pytest.mark.asyncio
async def test_ltm_blocked_status_classified_as_failed(store, run_blocking):
    """blocked/permission_required/skipped_duplicate 状态应归为失败命令。"""
    store.set_messages("ais-001", [
        _make_message("ais-001", "assistant", "诊断", msg_index=1, action_observations=[
            {"command": "clickhouse_query SELECT ...", "exit_code": 0, "status": "permission_required"},
            {"command": "kubectl exec ...", "exit_code": 0, "status": "blocked"},
        ]),
    ])

    result = await _run_ltm(store, run_blocking)

    summary = result["summary"]
    assert "✗失败/被拒命令" in summary
    assert "permission_required" in summary
    assert "blocked" in summary


@pytest.mark.asyncio
async def test_ltm_summary_has_both_hint_and_commands(store, run_blocking):
    """同时有 assistant_hint 和命令摘要的场景。"""
    store.set_messages("ais-001", [
        _make_message("ais-001", "user", "问题", msg_index=1),
        _make_message("ais-001", "assistant", "基于分析发现，认证配置存在问题", msg_index=2, action_observations=[
            {"command": "ls /etc/peak-cache/", "exit_code": 1, "status": "completed"},
            {"command": "cat /etc/peak-cache/config.yaml", "exit_code": 0, "status": "completed"},
        ]),
    ])

    result = await _run_ltm(store, run_blocking)

    summary = result["summary"]
    assert "assistant_hint" in summary
    assert "认证配置存在问题" in summary
    assert "✓成功命令" in summary
    assert "✗失败/被拒命令" in summary


@pytest.mark.asyncio
async def test_ltm_excludes_current_session(store, run_blocking):
    """当前会话不应出现在 LTM 结果中。"""
    store.set_messages("ais-current", [
        _make_message("ais-current", "assistant", "当前会话", msg_index=1),
    ])
    store.set_messages("ais-001", [
        _make_message("ais-001", "assistant", "历史会话", msg_index=1, action_observations=[
            {"command": "kubectl logs pod-1", "exit_code": 0, "status": "completed"},
        ]),
    ])

    # 把 ais-current 也注入到 list_sessions 结果中
    def list_sessions_include_current(**kwargs):
        return [_make_session("ais-current"), _make_session("ais-001")]
    store.list_sessions = list_sessions_include_current

    result = await _run_ltm(store, run_blocking)

    assert result["hits"] == 1
    assert "ais-current" not in result["summary"]
    assert "ais-001" in result["summary"]
    assert "kubectl logs pod-1" in result["summary"]


@pytest.mark.asyncio
async def test_ltm_disabled(run_blocking):
    """AI_FOLLOWUP_LONG_TERM_MEMORY_ENABLED=false 应返回空结果。"""
    with patch.dict(os.environ, {"AI_FOLLOWUP_LONG_TERM_MEMORY_ENABLED": "false"}):
        result = await _run_ltm(MagicMock(), run_blocking)
    assert result["enabled"] is False
    assert result["summary"] == ""


@pytest.mark.asyncio
async def test_ltm_multiple_sessions_all_preserved(store, run_blocking):
    """多个历史会话的失败命令都应出现在 summary 中。"""
    store.set_messages("ais-001", [
        _make_message("ais-001", "assistant", "诊断1", msg_index=1, action_observations=[
            {"command": "ls /fail/path", "exit_code": 1, "status": "completed"},
        ]),
    ])
    store.set_messages("ais-002", [
        _make_message("ais-002", "assistant", "诊断2", msg_index=1, action_observations=[
            {"command": "cat /success/file", "exit_code": 0, "status": "completed"},
        ]),
    ])

    result = await _run_ltm(store, run_blocking)

    summary = result["summary"]
    assert "ais-001" in summary
    assert "ais-002" in summary
    assert "ls /fail/path" in summary
    assert "cat /success/file" in summary


@pytest.mark.asyncio
async def test_ltm_items_include_command_lists(store, run_blocking):
    """memory_items 的每个条目应包含 successful_commands 和 failed_commands。"""
    store.set_messages("ais-001", [
        _make_message("ais-001", "assistant", "诊断", msg_index=1, action_observations=[
            {"command": "ls /fail/path", "exit_code": 1, "status": "completed"},
            {"command": "cat /success/file", "exit_code": 0, "status": "completed"},
        ]),
    ])

    result = await _run_ltm(store, run_blocking)

    # 至少有一个 item 包含命令字段
    items_with_data = [it for it in result["items"] if it.get("successful_commands") or it.get("failed_commands")]
    assert len(items_with_data) >= 1
    item = items_with_data[0]
    assert "successful_commands" in item
    assert "failed_commands" in item
    assert "cat /success/file" in item["successful_commands"]
    # exit_code=1 的命令会在末尾附加原因
    assert any("ls /fail/path" in c for c in item["failed_commands"])
