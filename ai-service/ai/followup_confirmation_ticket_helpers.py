"""
Follow-up command confirmation ticket helpers.

Ticket is stored in session context and consumed once on confirmed execution.
"""

from __future__ import annotations

import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

from ai.followup_command import _normalize_followup_command_match_key


_FOLLOWUP_CONFIRMATION_TICKET_CONTEXT_KEY = "followup_command_confirmation_tickets"


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return float(default)
        return float(value)
    except Exception:
        return float(default)


def _as_list(value: Any) -> List[Any]:
    if isinstance(value, list):
        return value
    return []


def _now_ts() -> float:
    return float(time.time())


def _epoch_to_iso(epoch_seconds: float) -> str:
    return datetime.fromtimestamp(max(0.0, float(epoch_seconds)), tz=timezone.utc).isoformat()


def _resolve_followup_confirmation_ticket_ttl_seconds() -> int:
    return max(
        30,
        min(
            1800,
            int(
                _as_float(
                    os.getenv("AI_FOLLOWUP_COMMAND_CONFIRMATION_TTL_SECONDS"),
                    180,
                )
            ),
        ),
    )


def _normalize_ticket_context(context: Dict[str, Any]) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    base_context = dict(context if isinstance(context, dict) else {})
    normalized: List[Dict[str, Any]] = []
    for item in _as_list(base_context.get(_FOLLOWUP_CONFIRMATION_TICKET_CONTEXT_KEY)):
        if isinstance(item, dict):
            normalized.append(dict(item))
    return base_context, normalized


def _prune_confirmation_tickets(
    tickets: List[Dict[str, Any]],
    *,
    now_ts: float,
    max_items: int = 200,
) -> List[Dict[str, Any]]:
    alive: List[Dict[str, Any]] = []
    for item in tickets:
        expires_at_epoch = _as_float(item.get("expires_at_epoch"), 0)
        if expires_at_epoch <= now_ts:
            continue
        ticket_text = _as_str(item.get("ticket"))
        if not ticket_text:
            continue
        alive.append(item)
    return alive[-max(1, int(max_items or 200)) :]


def _issue_followup_confirmation_ticket(
    *,
    session_context: Dict[str, Any],
    message_id: str,
    command: str,
    requires_elevation: bool,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Issue ticket and persist it into session context."""
    normalized_command = _as_str(command).strip()
    match_key = _normalize_followup_command_match_key(normalized_command)
    if not normalized_command or not match_key:
        return dict(session_context if isinstance(session_context, dict) else {}), {}

    now = _now_ts()
    ttl_seconds = _resolve_followup_confirmation_ticket_ttl_seconds()
    expires_at_epoch = now + float(ttl_seconds)
    ticket = f"fct-{uuid.uuid4().hex[:16]}"

    context, tickets = _normalize_ticket_context(session_context)
    tickets = _prune_confirmation_tickets(tickets, now_ts=now)
    tickets.append(
        {
            "ticket": ticket,
            "message_id": _as_str(message_id),
            "command": normalized_command,
            "command_key": match_key,
            "requires_elevation": bool(requires_elevation),
            "issued_at_epoch": now,
            "expires_at_epoch": expires_at_epoch,
            "issued_at": _epoch_to_iso(now),
            "expires_at": _epoch_to_iso(expires_at_epoch),
        }
    )
    context[_FOLLOWUP_CONFIRMATION_TICKET_CONTEXT_KEY] = tickets[-200:]
    return context, {
        "confirmation_ticket": ticket,
        "ticket_expires_at": _epoch_to_iso(expires_at_epoch),
        "ticket_ttl_seconds": ttl_seconds,
    }


def _consume_followup_confirmation_ticket(
    *,
    session_context: Dict[str, Any],
    provided_ticket: str,
    message_id: str,
    command: str,
    requires_elevation: bool,
) -> Tuple[bool, Dict[str, Any], str]:
    """
    Validate and consume ticket once.

    Returns: (ok, updated_context, reason)
    """
    ticket = _as_str(provided_ticket).strip()
    normalized_command = _as_str(command).strip()
    match_key = _normalize_followup_command_match_key(normalized_command)
    if not ticket:
        return False, dict(session_context if isinstance(session_context, dict) else {}), "ticket_missing"
    if not normalized_command or not match_key:
        return False, dict(session_context if isinstance(session_context, dict) else {}), "command_invalid"

    now = _now_ts()
    context, tickets = _normalize_ticket_context(session_context)
    tickets = _prune_confirmation_tickets(tickets, now_ts=now)
    remaining: List[Dict[str, Any]] = []
    matched = False
    matched_record: Dict[str, Any] = {}
    for item in tickets:
        if _as_str(item.get("ticket")) == ticket and not matched:
            matched = True
            matched_record = item
            continue
        remaining.append(item)

    if not matched:
        context[_FOLLOWUP_CONFIRMATION_TICKET_CONTEXT_KEY] = remaining[-200:]
        return False, context, "ticket_not_found_or_expired"

    same_message = _as_str(matched_record.get("message_id")) == _as_str(message_id)
    same_command = _as_str(matched_record.get("command_key")) == match_key
    ticket_requires_elevation = bool(matched_record.get("requires_elevation"))
    elevation_ok = bool(requires_elevation) == ticket_requires_elevation
    context[_FOLLOWUP_CONFIRMATION_TICKET_CONTEXT_KEY] = remaining[-200:]

    if not same_message:
        return False, context, "ticket_message_mismatch"
    if not same_command:
        return False, context, "ticket_command_mismatch"
    if not elevation_ok:
        return False, context, "ticket_elevation_mismatch"
    return True, context, ""
