import pytest
from shared_src.decision.state_machine import DecisionStateMachine, DecisionStatus, InvalidTransitionError
from shared_src.event.bus import InMemoryEventBus
from dataclasses import dataclass, field
from typing import List, Tuple
from datetime import datetime


@dataclass
class DecisionRecord:
    decision_id: str
    status: DecisionStatus = DecisionStatus.CREATED
    status_history: List[Tuple[DecisionStatus, datetime]] = field(default_factory=list)
    completed_at: datetime = None


class TestDecisionStateMachine:
    def test_transition(self):
        sm = DecisionStateMachine(bus=InMemoryEventBus())
        d = DecisionRecord(decision_id="d1")
        d.status = DecisionStatus.CREATED
        sm.transition(d, DecisionStatus.PLANNING)
        assert d.status == DecisionStatus.PLANNING
        assert len(d.status_history) == 1

    def test_valid_transitions(self):
        sm = DecisionStateMachine(bus=InMemoryEventBus())
        d = DecisionRecord(decision_id="d1")
        d.status = DecisionStatus.CREATED
        sm.transition(d, DecisionStatus.PLANNING)
        sm.transition(d, DecisionStatus.PLANNED)
        assert d.status == DecisionStatus.PLANNED

    def test_invalid_transition(self):
        sm = DecisionStateMachine(bus=InMemoryEventBus())
        d = DecisionRecord(decision_id="d1")
        d.status = DecisionStatus.CREATED
        with pytest.raises(InvalidTransitionError):
            sm.transition(d, DecisionStatus.SUCCEEDED)  # CREATED → SUCCEEDED 非法

    def test_terminal_states_set_completed_at(self):
        """终止状态设置 completed_at"""
        sm = DecisionStateMachine(bus=InMemoryEventBus())
        d = DecisionRecord(decision_id="d1")
        d.status = DecisionStatus.EXECUTING
        sm.transition(d, DecisionStatus.VERIFYING)
        sm.transition(d, DecisionStatus.SUCCEEDED)
        assert d.completed_at is not None

    def test_terminal_states_failed(self):
        sm = DecisionStateMachine(bus=InMemoryEventBus())
        d = DecisionRecord(decision_id="d1")
        d.status = DecisionStatus.EXECUTING
        sm.transition(d, DecisionStatus.FAILED)
        assert d.completed_at is not None

    def test_terminal_states_rolled_back(self):
        sm = DecisionStateMachine(bus=InMemoryEventBus())
        d = DecisionRecord(decision_id="d1")
        d.status = DecisionStatus.ROLLING_BACK
        sm.transition(d, DecisionStatus.ROLLED_BACK)
        assert d.completed_at is not None

    def test_publishes_events(self):
        bus = InMemoryEventBus()
        sm = DecisionStateMachine(bus=bus)
        d = DecisionRecord(decision_id="d1")
        d.status = DecisionStatus.CREATED
        sm.transition(d, DecisionStatus.PLANNING)
        assert any("platform.decision.state" in topic
                   for topic in bus._history)

    def test_state_machine_pure_lifecycle(self):
        """DecisionStateMachine 只做生命周期管理，不做编排"""
        sm = DecisionStateMachine(bus=InMemoryEventBus())
        assert hasattr(sm, "transition")
        assert not hasattr(sm, "execute")  # 编排由 Orchestrator 负责

    def test_all_statuses_have_valid_transitions(self):
        """每个状态都定义了合法转换"""
        sm = DecisionStateMachine(bus=InMemoryEventBus())
        for status in DecisionStatus:
            assert status in sm.TRANSITIONS
            assert isinstance(sm.TRANSITIONS[status], list)
