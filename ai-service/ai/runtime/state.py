"""Runtime state models."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

from ai.command.spec import CommandSpec
from ai.command.security import SessionCostState


@dataclass
class Action:
    action_id: str
    command_spec: CommandSpec
    purpose: str = ""
    status: str = "pending"


@dataclass
class Observation:
    action_id: str
    status: str = ""
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0
    channel: str = ""


@dataclass
class EvidenceSlot:
    key: str
    status: str = "pending"


@dataclass
class RuntimeState:
    run_id: str
    question: str
    analysis_context: dict
    source_target: dict | None = None

    iteration: int = 0
    max_iterations: int = 4
    phase: str = "planning"
    timeout_seconds: int = 300

    actions: List[Action] = field(default_factory=list)
    observations: List[Observation] = field(default_factory=list)
    evidence_slots: Dict[str, EvidenceSlot] = field(default_factory=dict)

    cost: SessionCostState = field(default_factory=SessionCostState)

    evidence_sufficient: bool = False
    diagnosis_summary: str = ""

    def add_observation(self, action: Action, obs: Observation) -> None:
        self.observations.append(obs)
        action.status = obs.status

    def check_evidence_sufficient(self) -> bool:
        if not self.evidence_slots:
            return len(self.observations) >= 2
        return all(
            slot.status in ("filled", "reused")
            for slot in self.evidence_slots.values()
        )

    def build_summary(self) -> str:
        lines = [f"诊断完成：{len(self.observations)} 条观察结果"]
        for obs in self.observations[-5:]:
            status = "✓" if obs.exit_code == 0 else "✗"
            lines.append(f"  {status} {obs.action_id}: {obs.status} ({obs.duration_ms}ms)")
        return "\n".join(lines)


__all__ = ["RuntimeState", "Action", "Observation", "EvidenceSlot"]
