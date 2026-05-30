"""
AI agent runtime status helpers.
"""

from typing import Any, Set


RUN_STATUS_QUEUED = "queued"
RUN_STATUS_RUNNING = "running"
RUN_STATUS_WAITING_APPROVAL = "waiting_approval"
RUN_STATUS_WAITING_USER_INPUT = "waiting_user_input"
RUN_STATUS_BLOCKED = "blocked"
RUN_STATUS_COMPLETED = "completed"
RUN_STATUS_FAILED = "failed"
RUN_STATUS_CANCELLED = "cancelled"

TERMINAL_RUN_STATUSES: Set[str] = {
    RUN_STATUS_BLOCKED,
    RUN_STATUS_COMPLETED,
    RUN_STATUS_FAILED,
    RUN_STATUS_CANCELLED,
}

ACTIVE_RUN_STATUSES: Set[str] = {
    RUN_STATUS_QUEUED,
    RUN_STATUS_RUNNING,
    RUN_STATUS_WAITING_APPROVAL,
    RUN_STATUS_WAITING_USER_INPUT,
}


def as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def is_terminal_run_status(value: Any) -> bool:
    return as_str(value).strip().lower() in TERMINAL_RUN_STATUSES
