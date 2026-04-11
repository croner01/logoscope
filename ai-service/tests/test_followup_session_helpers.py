"""
Tests for ai.followup_session_helpers.
"""

from types import SimpleNamespace

import pytest

from ai.followup_session_helpers import (
    _seed_followup_runtime_history_session,
    _upsert_followup_user_message,
)


async def _run_blocking(func, *args, **kwargs):
    return func(*args, **kwargs)


def _extract_overview_summary(_payload):
    return ""


@pytest.mark.asyncio
async def test_seed_followup_runtime_history_session_creates_running_session_and_initial_user_message():
    calls = []

    class SessionStore:
        def __init__(self):
            self.session = None
            self.messages = []

        def create_session(self, **kwargs):
            calls.append(("create_session", kwargs))
            self.session = SimpleNamespace(**kwargs)
            return self.session

        def get_session(self, session_id):
            calls.append(("get_session", session_id))
            return self.session if self.session and self.session.session_id == session_id else None

        def update_session(self, session_id, **changes):
            calls.append(("update_session", session_id, changes))
            current = self.session
            payload = current.__dict__.copy()
            payload.update(changes)
            payload["session_id"] = session_id
            self.session = SimpleNamespace(**payload)
            return self.session

        def get_messages(self, session_id, limit):
            calls.append(("get_messages", session_id, limit))
            return list(self.messages)[:limit]

        def append_messages(self, session_id, messages):
            calls.append(("append_messages", session_id, messages))
            appended = [
                SimpleNamespace(
                    session_id=session_id,
                    message_id=item["message_id"],
                    role=item["role"],
                    content=item["content"],
                    metadata=item.get("metadata", {}),
                )
                for item in messages
            ]
            self.messages.extend(appended)
            return appended

    session_store = SessionStore()

    session_id = await _seed_followup_runtime_history_session(
        session_store=session_store,
        run_blocking=_run_blocking,
        analysis_session_id="sess-new",
        analysis_context={"analysis_type": "log", "service_name": "checkout-service"},
        question="排查 checkout-service 超时",
        user_message_id="msg-u-001",
        conversation_id="conv-001",
        extract_overview_summary=_extract_overview_summary,
        llm_provider="deepseek",
        utc_now_iso=lambda: "2026-03-19T07:30:00Z",
    )

    assert session_id == "sess-new"
    assert session_store.session is not None
    assert session_store.session.status == "running"
    assert session_store.session.source == "api:/follow-up:runtime-init"
    assert session_store.session.context["conversation_id"] == "conv-001"
    assert len(session_store.messages) == 1
    assert session_store.messages[0].message_id == "msg-u-001"
    assert session_store.messages[0].content == "排查 checkout-service 超时"
    assert any(call[0] == "create_session" for call in calls)
    assert any(call[0] == "append_messages" for call in calls)


@pytest.mark.asyncio
async def test_seed_followup_runtime_history_session_skips_duplicate_user_message():
    existing_session = SimpleNamespace(
        session_id="sess-existing",
        context={"analysis_type": "log"},
        status="completed",
        source="api:/follow-up:init",
    )
    existing_messages = [
        SimpleNamespace(
            session_id="sess-existing",
            message_id="msg-u-dup",
            role="user",
            content="已有问题",
            metadata={},
        )
    ]

    class SessionStore:
        def create_session(self, **kwargs):
            return SimpleNamespace(context=kwargs.get("context", {}), **kwargs)

        def get_session(self, session_id):
            return existing_session if session_id == "sess-existing" else None

        def update_session(self, session_id, **changes):
            payload = existing_session.__dict__.copy()
            payload.update(changes)
            payload["session_id"] = session_id
            return SimpleNamespace(**payload)

        def get_messages(self, session_id, limit):
            return list(existing_messages)[:limit] if session_id == "sess-existing" else []

        def append_messages(self, session_id, messages):
            raise AssertionError(f"append_messages should not be called, got: {session_id}, {messages}")

    session_id = await _seed_followup_runtime_history_session(
        session_store=SessionStore(),
        run_blocking=_run_blocking,
        analysis_session_id="sess-existing",
        analysis_context={"analysis_type": "log", "service_name": "checkout-service"},
        question="已有问题",
        user_message_id="msg-u-dup",
        conversation_id="conv-001",
        extract_overview_summary=_extract_overview_summary,
        llm_provider="deepseek",
        utc_now_iso=lambda: "2026-03-19T07:30:00Z",
    )

    assert session_id == "sess-existing"


@pytest.mark.asyncio
async def test_seed_followup_runtime_history_session_skips_duplicate_last_user_content():
    existing_session = SimpleNamespace(
        session_id="sess-existing",
        context={"analysis_type": "log"},
        status="completed",
        source="api:/follow-up:init",
    )
    existing_messages = [
        SimpleNamespace(
            session_id="sess-existing",
            message_id="msg-u-old",
            role="user",
            content="排查 checkout-service timeout",
            metadata={},
        )
    ]

    class SessionStore:
        def create_session(self, **kwargs):
            return SimpleNamespace(context=kwargs.get("context", {}), **kwargs)

        def get_session(self, session_id):
            return existing_session if session_id == "sess-existing" else None

        def update_session(self, session_id, **changes):
            payload = existing_session.__dict__.copy()
            payload.update(changes)
            payload["session_id"] = session_id
            return SimpleNamespace(**payload)

        def get_messages(self, session_id, limit):
            return list(existing_messages)[:limit] if session_id == "sess-existing" else []

        def append_messages(self, session_id, messages):
            raise AssertionError(f"append_messages should not be called, got: {session_id}, {messages}")

    session_id = await _seed_followup_runtime_history_session(
        session_store=SessionStore(),
        run_blocking=_run_blocking,
        analysis_session_id="sess-existing",
        analysis_context={"analysis_type": "log", "service_name": "checkout-service"},
        question="排查   checkout-service   timeout",
        user_message_id="msg-u-new",
        conversation_id="conv-001",
        extract_overview_summary=_extract_overview_summary,
        llm_provider="deepseek",
        utc_now_iso=lambda: "2026-03-19T07:30:00Z",
    )

    assert session_id == "sess-existing"


def test_upsert_followup_user_message_reuses_last_user_message_without_mutating_history():
    history = [
        {
            "message_id": "msg-seed-001",
            "role": "user",
            "content": "排查 checkout-service timeout",
            "timestamp": "2026-03-19T07:30:00Z",
        }
    ]

    updated_history, user_message, persist_user_message = _upsert_followup_user_message(
        history,
        "排查   checkout-service   timeout",
        trim_conversation_history=lambda items, max_items=20: items[-max_items:],
        utc_now_iso=lambda: "2026-03-19T07:35:00Z",
    )

    assert persist_user_message is False
    assert updated_history[0]["message_id"] == "msg-seed-001"
    assert updated_history[0]["content"] == "排查 checkout-service timeout"
    assert user_message["message_id"] == "msg-seed-001"
    assert user_message["timestamp"] == "2026-03-19T07:30:00Z"
