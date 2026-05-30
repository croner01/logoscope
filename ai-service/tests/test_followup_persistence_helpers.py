"""
Tests for ai.followup_persistence_helpers.
"""

import asyncio
from types import SimpleNamespace

from ai.followup_persistence_helpers import _persist_followup_messages_and_history


async def _run_blocking(func, *args, **kwargs):
    return func(*args, **kwargs)


def test_persist_followup_messages_and_history_can_skip_user_message_persist():
    captured = {}

    class SessionStore:
        def append_messages(self, session_id, messages):
            captured["session_id"] = session_id
            captured["messages"] = messages
            return [
                SimpleNamespace(
                    message_id=item.get("message_id", ""),
                    role=item.get("role", ""),
                    content=item.get("content", ""),
                    created_at=item.get("timestamp", ""),
                    metadata=item.get("metadata", {}),
                )
                for item in messages
            ]

        def get_messages(self, session_id, limit):
            return [
                SimpleNamespace(
                    message_id="msg-u-seed",
                    role="user",
                    content="已有用户问题",
                    created_at="2026-03-19T07:30:00Z",
                    metadata={},
                ),
                SimpleNamespace(
                    message_id="msg-a-001",
                    role="assistant",
                    content="助手回复",
                    created_at="2026-03-19T07:31:00Z",
                    metadata={},
                ),
            ]

    history = [
        {
            "message_id": "msg-u-seed",
            "role": "user",
            "content": "已有用户问题",
            "timestamp": "2026-03-19T07:30:00Z",
        }
    ]
    user_message = {
        "message_id": "msg-u-seed",
        "role": "user",
        "content": "已有用户问题",
        "timestamp": "2026-03-19T07:30:00Z",
        "metadata": {"kind": "follow_up_question"},
    }
    assistant_message = {
        "message_id": "msg-a-001",
        "role": "assistant",
        "content": "助手回复",
        "timestamp": "2026-03-19T07:31:00Z",
        "metadata": {},
    }
    conversation_history_store = {}

    result = asyncio.run(
        _persist_followup_messages_and_history(
            session_store=SessionStore(),
            run_blocking=_run_blocking,
            analysis_session_id="sess-001",
            history=history,
            conversation_id="conv-001",
            user_message=user_message,
            persist_user_message=False,
            assistant_message=assistant_message,
            trim_conversation_history=lambda items, max_items=40: items[-max_items:],
            set_conversation_history=lambda cid, items: conversation_history_store.update({cid: items}),
        )
    )

    assert captured["session_id"] == "sess-001"
    assert len(captured["messages"]) == 1
    assert captured["messages"][0]["role"] == "assistant"
    assert "conv-001" in conversation_history_store
    assert result[-1]["role"] == "assistant"
