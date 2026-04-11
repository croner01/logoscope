"""
history_route_helpers 边界行为测试
"""
from dataclasses import dataclass
from typing import Any

from ai.history_route_helpers import _build_history_list_items, _build_history_list_response


@dataclass
class _Session:
    session_id: str
    analysis_type: str = "log"
    title: str = "title"
    service_name: str = "svc"
    trace_id: str = ""
    summary_text: str = ""
    analysis_method: str = "rule-based"
    llm_model: str = ""
    llm_provider: str = ""
    source: str = "ai-analysis"
    status: str = "completed"
    created_at: str = "2026-01-01T00:00:00Z"
    updated_at: str = "2026-01-01T00:00:00Z"
    is_pinned: bool = False
    is_archived: bool = False
    input_text: Any = ""
    result: Any = None


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def test_build_history_list_response_tolerates_none_total_all():
    payload = _build_history_list_response(
        items=[],
        total_all=None,
        safe_limit=20,
        safe_offset=0,
        safe_sort_by="updated_at",
        safe_sort_order="desc",
        pinned_first=True,
    )

    assert payload["total_all"] == 0
    assert payload["has_more"] is False


def test_build_history_list_response_tolerates_invalid_total_all_text():
    payload = _build_history_list_response(
        items=[{"session_id": "ais-1"}],
        total_all="not-a-number",
        safe_limit=20,
        safe_offset=0,
        safe_sort_by="updated_at",
        safe_sort_order="desc",
        pinned_first=True,
    )

    assert payload["total"] == 1
    assert payload["total_all"] == 1
    assert payload["has_more"] is False


def test_build_history_list_items_tolerates_none_input_text_and_bad_message_count():
    sessions = [_Session(session_id="ais-1", input_text=None, result={})]
    items = _build_history_list_items(
        sessions,
        message_counts={"ais-1": None},
        as_str=_as_str,
    )

    assert len(items) == 1
    assert items[0]["summary"] == ""
    assert items[0]["message_count"] == 0
