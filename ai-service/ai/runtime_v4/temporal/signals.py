"""
Temporal outer-loop signal payload models.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict


@dataclass
class ApprovalSignal:
    run_id: str
    approval_id: str
    decision: str
    comment: str = ""
    confirmed: bool = True
    elevated: bool = False


@dataclass
class UserInputSignal:
    run_id: str
    text: str
    source: str = "user"


@dataclass
class InterruptSignal:
    run_id: str
    reason: str = "user_interrupt_esc"


@dataclass
class SignalEnvelope:
    workflow_id: str
    signal_type: str
    run_id: str
    payload: Dict[str, object]
