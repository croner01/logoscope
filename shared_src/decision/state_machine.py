"""DecisionStateMachine — 纯生命周期管理（v15: 与 Orchestrator 分离）。"""
from enum import Enum
from typing import Dict, List, Optional, Tuple
from datetime import datetime
from shared_src.event.bus import EventBus
from shared_src.event.envelope import EventEnvelope
import json
import uuid


class DecisionStatus(Enum):
    CREATED = "created"
    PLANNING = "planning"
    PLANNED = "planned"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"
    CANCELLED = "cancelled"


class InvalidTransitionError(Exception):
    pass


class DecisionStateMachine:
    """
    Decision 状态机——纯生命周期管理。

    v15: 只负责状态转换 + Event 发布。
         不负责编排（编排由 DecisionOrchestrator 负责）。
    """

    TRANSITIONS = {
        DecisionStatus.CREATED: [DecisionStatus.PLANNING],
        DecisionStatus.PLANNING: [DecisionStatus.PLANNED],
        DecisionStatus.PLANNED: [DecisionStatus.PENDING_APPROVAL, DecisionStatus.APPROVED],
        DecisionStatus.PENDING_APPROVAL: [DecisionStatus.APPROVED, DecisionStatus.REJECTED],
        DecisionStatus.APPROVED: [DecisionStatus.EXECUTING, DecisionStatus.REJECTED],
        DecisionStatus.REJECTED: [],
        DecisionStatus.EXECUTING: [DecisionStatus.VERIFYING, DecisionStatus.FAILED, DecisionStatus.ROLLING_BACK],
        DecisionStatus.VERIFYING: [DecisionStatus.SUCCEEDED, DecisionStatus.FAILED, DecisionStatus.ROLLING_BACK],
        DecisionStatus.SUCCEEDED: [],
        DecisionStatus.FAILED: [DecisionStatus.ROLLING_BACK],
        DecisionStatus.ROLLING_BACK: [DecisionStatus.ROLLED_BACK, DecisionStatus.FAILED],
        DecisionStatus.ROLLED_BACK: [],
        DecisionStatus.CANCELLED: [],
    }

    def __init__(self, bus: EventBus):
        self.bus = bus

    def transition(self, decision, to: DecisionStatus):
        """执行状态转换。"""
        current = decision.status
        allowed = self.TRANSITIONS.get(current, [])
        if to not in allowed:
            raise InvalidTransitionError(
                f"Cannot transition from {current.value} to {to.value}"
            )
        decision.status = to
        decision.status_history.append((to, datetime.utcnow()))
        if to in (DecisionStatus.SUCCEEDED, DecisionStatus.FAILED,
                  DecisionStatus.ROLLED_BACK, DecisionStatus.CANCELLED):
            decision.completed_at = datetime.utcnow()
        # 发布状态变更事件
        self._publish_event(decision, to)
        return decision

    def _publish_event(self, decision, to: DecisionStatus):
        env = EventEnvelope(
            event_type="decision.state_changed",
            producer="decision-state-machine",
            event_id=uuid.uuid4().hex,
            payload=json.dumps({
                "decision_id": decision.decision_id,
                "from": decision.status_history[-2][0].value if len(decision.status_history) >= 2 else "",
                "to": to.value,
                "timestamp": datetime.utcnow().isoformat(),
            }).encode("utf-8"),
        )
        self.bus.publish("platform.decision.state", env)
