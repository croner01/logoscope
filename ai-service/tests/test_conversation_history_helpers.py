"""
conversation_history_helpers 行为测试
"""

from ai.conversation_history_helpers import (
    _merge_conversation_history,
    _normalize_conversation_history,
    _session_messages_to_conversation_history,
)


class _Msg:
    def __init__(self, role: str, content: str, message_id: str = "", created_at: str = ""):
        self.role = role
        self.content = content
        self.message_id = message_id
        self.created_at = created_at


def test_normalize_conversation_history_filters_invalid_and_backfills_timestamp():
    normalized = _normalize_conversation_history(
        [
            {"role": "user", "content": "query", "message_id": "m1"},
            {"role": "system", "content": "ignored"},
            {"role": "assistant", "content": ""},
            "invalid",
        ],
        max_items=10,
    )

    assert len(normalized) == 1
    assert normalized[0]["role"] == "user"
    assert normalized[0]["content"] == "query"
    assert normalized[0]["message_id"] == "m1"
    assert normalized[0]["timestamp"]


def test_session_messages_to_conversation_history_supports_dict_and_object():
    history = _session_messages_to_conversation_history(
        [
            {"role": "ASSISTANT", "content": "step-1", "message_id": "a1", "created_at": "2026-01-01T00:00:00Z"},
            _Msg("user", "follow up", message_id="u1", created_at="2026-01-01T00:00:01Z"),
            {"role": "USER", "content": "no-ts", "message_id": "u2"},
            _Msg("system", "ignored", message_id="s1"),
        ],
        max_items=10,
    )

    assert [item["message_id"] for item in history] == ["a1", "u1", "u2"]
    assert history[0]["timestamp"] == "2026-01-01T00:00:00Z"
    assert history[1]["timestamp"] == "2026-01-01T00:00:01Z"
    assert history[2]["timestamp"]


def test_merge_conversation_history_dedupes_and_applies_max_items():
    base = [{"message_id": "m1", "role": "user", "content": "A", "timestamp": "t1"}]
    extra = [
        {"message_id": "m1", "role": "user", "content": "A", "timestamp": "t1"},
        {"message_id": "m2", "role": "assistant", "content": "B", "timestamp": "t2"},
        {"message_id": "m3", "role": "user", "content": "C", "timestamp": "t3"},
    ]

    merged = _merge_conversation_history(base, extra, max_items=2)

    assert len(merged) == 2
    assert [item["message_id"] for item in merged] == ["m2", "m3"]


def test_merge_conversation_history_dedupes_by_message_id_even_if_timestamp_differs():
    base = [{"message_id": "m1", "role": "user", "content": "A", "timestamp": "t1"}]
    extra = [{"message_id": "m1", "role": "user", "content": "A", "timestamp": "t2"}]

    merged = _merge_conversation_history(base, extra, max_items=10)

    assert len(merged) == 1
    assert merged[0]["message_id"] == "m1"
