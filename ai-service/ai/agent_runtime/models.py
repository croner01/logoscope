"""
AI agent runtime datamodels.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional
import uuid


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def build_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


@dataclass
class AgentRun:
    run_id: str
    session_id: str
    conversation_id: str
    analysis_type: str
    engine: str
    runtime_version: str
    user_message_id: str
    assistant_message_id: str
    service_name: str = ""
    trace_id: str = ""
    status: str = "queued"
    question: str = ""
    input_json: Dict[str, Any] = field(default_factory=dict)
    context_json: Dict[str, Any] = field(default_factory=dict)
    summary_json: Dict[str, Any] = field(default_factory=dict)
    error_code: str = ""
    error_detail: str = ""
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    ended_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "session_id": self.session_id,
            "conversation_id": self.conversation_id,
            "analysis_type": self.analysis_type,
            "engine": self.engine,
            "runtime_version": self.runtime_version,
            "user_message_id": self.user_message_id,
            "assistant_message_id": self.assistant_message_id,
            "service_name": self.service_name,
            "trace_id": self.trace_id,
            "status": self.status,
            "question": self.question,
            "input_json": self.input_json,
            "context_json": self.context_json,
            "summary_json": self.summary_json,
            "error_code": self.error_code,
            "error_detail": self.error_detail,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "ended_at": self.ended_at,
        }


@dataclass
class RunEvent:
    run_id: str
    seq: int
    event_type: str
    payload: Dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now_iso)
    event_id: str = field(default_factory=lambda: build_id("evt"))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "run_id": self.run_id,
            "seq": self.seq,
            "event_type": self.event_type,
            "created_at": self.created_at,
            "payload": self.payload,
        }
