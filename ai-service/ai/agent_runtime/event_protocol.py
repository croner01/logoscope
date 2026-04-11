"""
Canonical AI agent runtime event protocol.
"""

from typing import Final, Set


RUN_STARTED: Final[str] = "run_started"
RUN_STATUS_CHANGED: Final[str] = "run_status_changed"
MESSAGE_STARTED: Final[str] = "message_started"
REASONING_SUMMARY_DELTA: Final[str] = "reasoning_summary_delta"
REASONING_STEP: Final[str] = "reasoning_step"
TOOL_CALL_STARTED: Final[str] = "tool_call_started"
TOOL_CALL_PROGRESS: Final[str] = "tool_call_progress"
TOOL_CALL_OUTPUT_DELTA: Final[str] = "tool_call_output_delta"
TOOL_CALL_FINISHED: Final[str] = "tool_call_finished"
TOOL_CALL_SKIPPED_DUPLICATE: Final[str] = "tool_call_skipped_duplicate"
APPROVAL_REQUIRED: Final[str] = "approval_required"
APPROVAL_RESOLVED: Final[str] = "approval_resolved"
ACTION_WAITING_APPROVAL: Final[str] = "action_waiting_approval"
ACTION_WAITING_USER_INPUT: Final[str] = "action_waiting_user_input"
ACTION_RESUMED: Final[str] = "action_resumed"
ACTION_REPLANNED: Final[str] = "action_replanned"
ACTION_TIMEOUT_RECOVERY_SCHEDULED: Final[str] = "action_timeout_recovery_scheduled"
APPROVAL_TIMEOUT: Final[str] = "approval_timeout"
ASSISTANT_DELTA: Final[str] = "assistant_delta"
ASSISTANT_MESSAGE_FINALIZED: Final[str] = "assistant_message_finalized"
RUN_FINISHED: Final[str] = "run_finished"
RUN_FAILED: Final[str] = "run_failed"
RUN_CANCELLED: Final[str] = "run_cancelled"
RUN_INTERRUPTED: Final[str] = "run_interrupted"

EVENT_TYPES: Set[str] = {
    RUN_STARTED,
    RUN_STATUS_CHANGED,
    MESSAGE_STARTED,
    REASONING_SUMMARY_DELTA,
    REASONING_STEP,
    TOOL_CALL_STARTED,
    TOOL_CALL_PROGRESS,
    TOOL_CALL_OUTPUT_DELTA,
    TOOL_CALL_FINISHED,
    TOOL_CALL_SKIPPED_DUPLICATE,
    APPROVAL_REQUIRED,
    APPROVAL_RESOLVED,
    ACTION_WAITING_APPROVAL,
    ACTION_WAITING_USER_INPUT,
    ACTION_RESUMED,
    ACTION_REPLANNED,
    ACTION_TIMEOUT_RECOVERY_SCHEDULED,
    APPROVAL_TIMEOUT,
    ASSISTANT_DELTA,
    ASSISTANT_MESSAGE_FINALIZED,
    RUN_FINISHED,
    RUN_FAILED,
    RUN_CANCELLED,
    RUN_INTERRUPTED,
}
