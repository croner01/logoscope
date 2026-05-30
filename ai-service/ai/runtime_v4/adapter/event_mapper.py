"""Runtime v4 API/event mapping helpers."""

from __future__ import annotations

from typing import Any, Dict


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def map_run_snapshot(
    run_payload: Dict[str, Any],
    *,
    thread_id: str,
    outer_engine: str,
    inner_engine: str,
) -> Dict[str, Any]:
    safe_run = run_payload if isinstance(run_payload, dict) else {}
    summary = safe_run.get("summary") if isinstance(safe_run.get("summary"), dict) else {}
    if not summary:
        summary = safe_run.get("summary_json") if isinstance(safe_run.get("summary_json"), dict) else {}

    return {
        "run_id": _as_str(safe_run.get("run_id")),
        "thread_id": _as_str(thread_id),
        "status": _as_str(safe_run.get("status"), "queued"),
        "engine": {
            "outer": _as_str(outer_engine, "temporal-local-v1"),
            "inner": _as_str(inner_engine, "langgraph-local-v1"),
        },
        "assistant_message_id": _as_str(safe_run.get("assistant_message_id")),
        "user_message_id": _as_str(safe_run.get("user_message_id")),
        "summary": summary,
        "created_at": _as_str(safe_run.get("created_at")),
        "updated_at": _as_str(safe_run.get("updated_at")),
        "ended_at": safe_run.get("ended_at"),
    }
