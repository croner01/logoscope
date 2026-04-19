"""AI Analysis Workflow Orchestrator.

Implements the 4-step analysis process:
1. Log operation flow analysis
2. Fault cause analysis
3. Remediation plan output
4. Conversation continuation
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel


class AnalysisPhase(str, Enum):
    """Analysis phase enumeration."""
    INIT = "init"
    FLOW_ANALYSIS = "flow_analysis"
    FAULT_ANALYSIS = "fault_analysis"
    REMEDIATION = "remediation"
    CONVERSATION = "conversation"
    COMPLETED = "completed"


class CommandStatus(str, Enum):
    """Command execution status."""
    PENDING = "pending"
    APPROVED = "approved"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    REQUIRES_APPROVAL = "requires_approval"


@dataclass
class LogEntry:
    """A single log entry."""
    timestamp: str
    level: str
    message: str
    service_name: str
    trace_id: Optional[str] = None
    request_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class FlowNode:
    """A node in the flow diagram."""
    node_id: str
    component: str
    description: str
    logs: List[LogEntry] = field(default_factory=list)
    status: str = "normal"  # normal, warning, error


@dataclass
class FlowAnalysisResult:
    """Result of flow analysis."""
    flow_nodes: List[FlowNode]
    entry_point: str
    flow_path: List[str]
    error_nodes: List[str]
    summary: str


@dataclass
class FaultAnalysisResult:
    """Result of fault analysis."""
    direct_cause: str
    root_cause: str
    impact_scope: str
    severity: str  # minor, general, severe, critical
    affected_components: List[str]
    diffusion_path: str
    evidence: List[str]


@dataclass
class RemediationStep:
    """A single remediation step."""
    step_id: str
    title: str
    command: str
    command_type: str  # normal, dangerous
    risk_level: str
    purpose: str
    expected_result: str
    requires_approval: bool
    status: CommandStatus = CommandStatus.PENDING


@dataclass
class RemediationResult:
    """Result of remediation planning."""
    steps: List[RemediationStep]
    total_steps: int
    dangerous_steps: int
    estimated_duration: str


@dataclass
class CommandExecutionRecord:
    """Record of command execution."""
    command: str
    status: CommandStatus
    exit_code: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    executed_at: Optional[float] = None
    executed_by: Optional[str] = None
    approved_by: Optional[str] = None


@dataclass
class AnalysisSession:
    """Maintains state for an analysis session."""
    session_id: str
    phase: AnalysisPhase
    primary_log: LogEntry
    context_logs: List[LogEntry] = field(default_factory=list)
    flow_result: Optional[FlowAnalysisResult] = None
    fault_result: Optional[FaultAnalysisResult] = None
    remediation_result: Optional[RemediationResult] = None
    command_history: List[CommandExecutionRecord] = field(default_factory=list)
    conversation_history: List[Dict[str, Any]] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


class AnalysisWorkflowOrchestrator:
    """
    Orchestrates the 4-step AI analysis process.

    Coordinates between skills, manages session state,
    and produces structured analysis output.
    """

    def __init__(self):
        self._sessions: Dict[str, AnalysisSession] = {}

    def create_session(
        self,
        session_id: str,
        primary_log: Dict[str, Any],
        context_logs: Optional[List[Dict[str, Any]]] = None,
    ) -> AnalysisSession:
        """Create a new analysis session."""
        primary = self._parse_log(primary_log)
        context = [self._parse_log(l) for l in (context_logs or [])]

        session = AnalysisSession(
            session_id=session_id,
            phase=AnalysisPhase.INIT,
            primary_log=primary,
            context_logs=context,
        )
        self._sessions[session_id] = session
        return session

    def get_session(self, session_id: str) -> Optional[AnalysisSession]:
        """Get an existing session."""
        return self._sessions.get(session_id)

    def _parse_log(self, log_data: Dict[str, Any]) -> LogEntry:
        """Parse log data into LogEntry."""
        return LogEntry(
            timestamp=log_data.get("timestamp", ""),
            level=log_data.get("level", "INFO"),
            message=log_data.get("message", ""),
            service_name=log_data.get("service_name", ""),
            trace_id=log_data.get("trace_id"),
            request_id=log_data.get("request_id"),
            metadata=log_data.get("metadata", {}),
        )

    def transition_phase(self, session_id: str, new_phase: AnalysisPhase) -> bool:
        """Transition session to a new phase."""
        session = self._sessions.get(session_id)
        if not session:
            return False

        # Validate phase transition
        valid_transitions = {
            AnalysisPhase.INIT: [AnalysisPhase.FLOW_ANALYSIS],
            AnalysisPhase.FLOW_ANALYSIS: [AnalysisPhase.FAULT_ANALYSIS],
            AnalysisPhase.FAULT_ANALYSIS: [AnalysisPhase.REMEDIATION],
            AnalysisPhase.REMEDIATION: [AnalysisPhase.CONVERSATION],
            AnalysisPhase.CONVERSATION: [AnalysisPhase.CONVERSATION, AnalysisPhase.COMPLETED],
            AnalysisPhase.COMPLETED: [],
        }

        if new_phase not in valid_transitions.get(session.phase, []):
            return False

        session.phase = new_phase
        session.updated_at = time.time()
        return True

    def set_flow_result(
        self,
        session_id: str,
        flow_result: FlowAnalysisResult,
    ) -> bool:
        """Set flow analysis result."""
        session = self._sessions.get(session_id)
        if not session:
            return False
        session.flow_result = flow_result
        session.updated_at = time.time()
        return True

    def set_fault_result(
        self,
        session_id: str,
        fault_result: FaultAnalysisResult,
    ) -> bool:
        """Set fault analysis result."""
        session = self._sessions.get(session_id)
        if not session:
            return False
        session.fault_result = fault_result
        session.updated_at = time.time()
        return True

    def set_remediation_result(
        self,
        session_id: str,
        remediation_result: RemediationResult,
    ) -> bool:
        """Set remediation result."""
        session = self._sessions.get(session_id)
        if not session:
            return False
        session.remediation_result = remediation_result
        session.updated_at = time.time()
        return True

    def add_command_execution(
        self,
        session_id: str,
        record: CommandExecutionRecord,
    ) -> bool:
        """Add a command execution record."""
        session = self._sessions.get(session_id)
        if not session:
            return False
        session.command_history.append(record)
        session.updated_at = time.time()
        return True

    def add_conversation_message(
        self,
        session_id: str,
        role: str,
        content: str,
    ) -> bool:
        """Add a conversation message."""
        session = self._sessions.get(session_id)
        if not session:
            return False
        session.conversation_history.append({
            "role": role,
            "content": content,
            "timestamp": time.time(),
        })
        session.updated_at = time.time()
        return True

    def get_session_summary(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Get a summary of the session state."""
        session = self._sessions.get(session_id)
        if not session:
            return None

        return {
            "session_id": session.session_id,
            "phase": session.phase.value,
            "created_at": session.created_at,
            "updated_at": session.updated_at,
            "primary_log": {
                "service": session.primary_log.service_name,
                "level": session.primary_log.level,
                "message_preview": session.primary_log.message[:100],
            },
            "context_logs_count": len(session.context_logs),
            "command_history_count": len(session.command_history),
            "conversation_count": len(session.conversation_history),
            "flow_completed": session.flow_result is not None,
            "fault_completed": session.fault_result is not None,
            "remediation_completed": session.remediation_result is not None,
        }


# Global orchestrator instance
_global_orchestrator: Optional[AnalysisWorkflowOrchestrator] = None


def get_orchestrator() -> AnalysisWorkflowOrchestrator:
    """Get the global workflow orchestrator instance."""
    global _global_orchestrator
    if _global_orchestrator is None:
        _global_orchestrator = AnalysisWorkflowOrchestrator()
    return _global_orchestrator
