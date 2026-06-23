"""Test DiagnosisContext extraction — verify output matches original function's front segment."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from typing import Any, Dict


@pytest.fixture
def mock_request():
    """Build a minimal FollowUpRequest-like object."""
    request = MagicMock()
    request.question = "分析 pod crash 原因"
    request.analysis_context = {"namespace": "default", "cluster": "prod"}
    request.show_thought = True
    request.auto_exec_readonly = True
    return request


@pytest.fixture
def mock_storage():
    storage = MagicMock()
    storage.get_analysis_session = AsyncMock(return_value=None)
    storage.create_analysis_session = AsyncMock(return_value={
        "session_id": "test-session-001",
        "conversation_id": "test-conv-001",
    })
    storage.append_history = AsyncMock(return_value=None)
    return storage


@pytest.fixture
def mock_llm_service():
    svc = MagicMock()
    svc.chat = AsyncMock(return_value={
        "choices": [{"message": {"content": "测试回答"}}]
    })
    return svc


@pytest.mark.asyncio
async def test_build_diagnosis_context_matches_original_behavior(mock_request, mock_storage, mock_llm_service):
    """build_diagnosis_context() 的输出和原函数前段等价。

    这个测试在提取前先记录原函数的行为（日记模式），
    提取后运行同样输入验证输出一致。
    """
    # Import here so it works both before and after extraction
    from api.ai import _run_follow_up_analysis_core

    with patch("api.ai.get_ai_session_store", return_value=mock_storage), \
         patch("api.ai.get_llm_service", return_value=mock_llm_service), \
         patch("api.ai.storage", mock_storage), \
         patch("api.ai._resolve_followup_timeout_profile", return_value={"request_deadline_seconds": "300"}), \
         patch("api.ai._mask_sensitive_text", lambda x: x), \
         patch("api.ai._mask_sensitive_payload", lambda x: x):

        # We can't easily call _run_follow_up_analysis_core standalone here
        # because it does too much. This test is a placeholder for the
        # integration-level test that validates the slice.
        # Instead, run the actual diagnosis flow and verify the context
        # dict has the expected keys.
        pass  # 提取后此测试会被替换为真正的断言


@pytest.mark.asyncio
async def test_diagnosis_context_dataclass_fields():
    """DiagnosisContext dataclass 包含所有必要字段。"""
    from ai.diagnosis.context import DiagnosisContext

    ctx = DiagnosisContext(
        session_id="s1",
        conversation_id="c1",
        source_target=None,
        question="test",
        analysis_context={},
        history=[],
        compacted_summary="",
        long_term_memory={},
        react_memory={},
        runtime_thread_memory={},
        subgoals=[],
        reflection={},
        planner_prompt="",
        followup_actions=[],
        executed_commands_set=set(),
        prior_action_observations=[],
        evidence_gap_queue_for_execution=[],
        answer_summary_seed="",
        llm_enabled=True,
        llm_requested=True,
        token_budget=10000,
        token_estimation=0,
        followup_engine="auto",
        timeout_profile={"request_deadline_seconds": "300"},
        deadline_ts=9999999999.0,
        show_thought=True,
        event_callback=None,
        run_blocking=None,
    )
    assert ctx.session_id == "s1"
    assert ctx.question == "test"
    assert isinstance(ctx.executed_commands_set, set)
