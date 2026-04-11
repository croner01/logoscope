"""
API v2 models for AI runtime v4.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ThreadCreateRequest(BaseModel):
    session_id: str = ""
    conversation_id: str = ""
    title: str = ""


class RunCreateRequest(BaseModel):
    question: str
    analysis_context: Dict[str, Any] = Field(default_factory=dict)
    runtime_options: Dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = ""
    client_deadline_ms: int = 0
    pipeline_steps: List[Dict[str, Any]] = Field(default_factory=list)


class ApprovalResolveRequest(BaseModel):
    decision: str = "approved"
    comment: str = ""
    confirmed: bool = True
    elevated: bool = False


class InputSubmitRequest(BaseModel):
    text: str
    source: str = "user"


class CommandActionRequest(BaseModel):
    action_id: str = ""
    step_id: str = ""
    command: str = ""
    command_spec: Dict[str, Any] = Field(default_factory=dict)
    diagnosis_contract: Dict[str, Any] = Field(default_factory=dict)
    purpose: str
    title: str = ""
    tool_name: str = "command.exec"
    confirmed: bool = False
    elevated: bool = False
    approval_token: str = ""
    client_deadline_ms: int = 0
    timeout_seconds: int = 20


class InterruptRequest(BaseModel):
    reason: str = "user_interrupt_esc"


class CancelRequest(BaseModel):
    reason: str = "user_cancelled"


class TargetRegisterRequest(BaseModel):
    target_id: str
    target_kind: str = "unknown"
    target_identity: str = ""
    display_name: str = ""
    description: str = ""
    capabilities: List[str] = Field(default_factory=list)
    credential_scope: Dict[str, Any] = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    updated_by: str = "system"
    reason: str = ""
    run_id: str = ""
    action_id: str = ""


class TargetResolveRequest(BaseModel):
    required_capabilities: List[str] = Field(default_factory=list)
    run_id: str = ""
    action_id: str = ""
    reason: str = ""


class TargetResolveByIdentityRequest(BaseModel):
    target_kind: str = "unknown"
    target_identity: str = ""
    required_capabilities: List[str] = Field(default_factory=list)
    run_id: str = ""
    action_id: str = ""
    reason: str = ""


class TargetDeactivateRequest(BaseModel):
    updated_by: str = "system"
    reason: str = ""
    run_id: str = ""
    action_id: str = ""


class ThreadSnapshot(BaseModel):
    thread_id: str
    session_id: str
    conversation_id: str
    title: str
    status: str
    created_at: str
    updated_at: str


class EngineSnapshot(BaseModel):
    outer: str
    inner: str


class RunSnapshot(BaseModel):
    run_id: str
    thread_id: str
    status: str
    engine: EngineSnapshot
    assistant_message_id: str
    user_message_id: str
    summary: Dict[str, Any] = Field(default_factory=dict)
    created_at: str
    updated_at: str
    ended_at: Optional[str] = None
