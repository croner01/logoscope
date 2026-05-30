"""
In-memory confirmation ticket store.
"""

import os
import time
import uuid
from typing import Any, Dict, Tuple


TICKET_STORE: Dict[str, Dict[str, Any]] = {}
DEFAULT_TTL_SECONDS = max(30, int(os.getenv("EXEC_TICKET_TTL_SECONDS", "900")))


def as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def issue_ticket(
    *,
    session_id: str,
    message_id: str,
    action_id: str,
    command: str,
    requires_elevation: bool,
    decision_id: str = "",
) -> Dict[str, Any]:
    ticket_id = f"exec-ticket-{uuid.uuid4().hex[:16]}"
    expires_at = time.time() + DEFAULT_TTL_SECONDS
    payload = {
        "ticket_id": ticket_id,
        "session_id": as_str(session_id),
        "message_id": as_str(message_id),
        "action_id": as_str(action_id),
        "command": as_str(command),
        "requires_elevation": bool(requires_elevation),
        "decision_id": as_str(decision_id),
        "expires_at": expires_at,
    }
    TICKET_STORE[ticket_id] = payload
    return payload


def consume_ticket(
    *,
    ticket_id: str,
    session_id: str,
    message_id: str,
    action_id: str,
    command: str,
    requires_elevation: bool,
) -> Tuple[bool, str, Dict[str, Any]]:
    payload = TICKET_STORE.pop(as_str(ticket_id), None)
    if not isinstance(payload, dict):
        return False, "ticket_not_found", {}
    if float(payload.get("expires_at") or 0) < time.time():
        return False, "ticket_expired", {}
    if as_str(payload.get("session_id")) != as_str(session_id):
        return False, "ticket_session_mismatch", {}
    if as_str(payload.get("message_id")) != as_str(message_id):
        return False, "ticket_message_mismatch", {}
    if as_str(payload.get("action_id")) != as_str(action_id):
        return False, "ticket_action_mismatch", {}
    if as_str(payload.get("command")) != as_str(command):
        return False, "ticket_command_mismatch", {}
    if bool(payload.get("requires_elevation")) != bool(requires_elevation):
        return False, "ticket_elevation_mismatch", {}
    return True, "ok", dict(payload)


def revoke_ticket(ticket_id: str) -> None:
    """Drop a ticket without validation when a superseded precheck issued a new one."""
    TICKET_STORE.pop(as_str(ticket_id), None)
