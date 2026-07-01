"""DecisionOrchestrator — v15: 纯编排（与 StateMachine 分离）。"""
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional
from datetime import datetime
from .state_machine import DecisionStateMachine, DecisionStatus


@dataclass
class DecisionResult:
    """编排结果。"""
    decision: Any = None
    status: str = "pending"


class DecisionOrchestrator:
    """
    Decision 编排器——v15 从 DecisionManager 拆分。

    职责：串联 5 阶段流程
    1. PLAN: Planner → Goal Tree → Intents
    2. EVALUATE: ExecutionPlanner → Candidates + RiskEngine → Risk
    3. POLICY: PolicyEngine → Utility sorting + Decision
    4. EXECUTE: WorkflowEngine → Execution
    5. LEARN: Episode 记录

    不负责：生命周期状态管理（DecisionStateMachine 负责）
    """

    def __init__(self, planner, exec_planner, risk_engine,
                 blast_analyzer, policy_engine,
                 state_machine: DecisionStateMachine,
                 workflow_engine, episode_store):
        self.planner = planner
        self.exec_planner = exec_planner
        self.risk_engine = risk_engine
        self.blast_analyzer = blast_analyzer
        self.policy_engine = policy_engine
        self.state_machine = state_machine
        self.workflow_engine = workflow_engine
        self.episode_store = episode_store

    def execute(self, finding, context, goal=None) -> DecisionResult:
        """完整决策执行流程。"""
        decision_id = uuid.uuid4().hex

        # 模拟 DecisionRecord（简化版——生产用完整 DecisionRecord）
        decision = type("Decision", (), {
            "decision_id": decision_id,
            "finding_id": getattr(finding, "id", "") or getattr(finding, "category", ""),
            "status": DecisionStatus.CREATED,
            "status_history": [],
            "completed_at": None,
        })()

        try:
            # === Phase 1: PLAN ===
            self.state_machine.transition(decision, DecisionStatus.PLANNING)
            plan_result = self.planner.plan(finding, context, goal)
            decision.goal = plan_result.goal
            self.state_machine.transition(decision, DecisionStatus.PLANNED)

            # === Phase 2: EVALUATE ===
            candidates = self.exec_planner.plan(plan_result.intents, context)
            for intent, candidate in zip(plan_result.intents, candidates):
                candidate.risk_profile = self.risk_engine.compute(
                    intent.action,
                    intent.entity_type,
                    intent.entity_name,
                    candidate.base_risk,
                )

            # === Phase 3: POLICY ===
            action = plan_result.intents[0].action if plan_result.intents else ""
            et = plan_result.intents[0].entity_type if plan_result.intents else ""
            en = plan_result.intents[0].entity_name if plan_result.intents else ""
            result = self.policy_engine.evaluate(
                candidates, action, et, en,
                finding_id=getattr(finding, "id", ""),
            )

            if result.decision.name == "CANDIDATE_SELECTED":
                self.state_machine.transition(decision, DecisionStatus.APPROVED)
            elif result.decision.name == "PENDING_APPROVAL":
                self.state_machine.transition(decision, DecisionStatus.PENDING_APPROVAL)
                return DecisionResult(decision=decision, status="pending_approval")
            else:
                self.state_machine.transition(decision, DecisionStatus.REJECTED)
                return DecisionResult(decision=decision, status="rejected")

            # === Phase 4: EXECUTE ===
            self.state_machine.transition(decision, DecisionStatus.EXECUTING)
            if result.selected_candidate and result.selected_candidate.workflow:
                exec_result = self.workflow_engine.execute(
                    result.selected_candidate.workflow,
                    type("Ctx", (), {"trigger": "orchestrator"})(),
                )
                self.state_machine.transition(decision, DecisionStatus.VERIFYING)
                outcome = "success" if exec_result.outcome == "success" else "failed"
            else:
                outcome = "failed"

            if outcome == "success":
                self.state_machine.transition(decision, DecisionStatus.SUCCEEDED)
            else:
                self.state_machine.transition(decision, DecisionStatus.FAILED)

            # === Phase 5: LEARN ===
            self._record_episode(decision, plan_result, result)

            return DecisionResult(decision=decision, status=outcome)

        except Exception as e:
            try:
                self.state_machine.transition(decision, DecisionStatus.FAILED)
            except Exception:
                pass
            return DecisionResult(decision=decision, status="failed")

    def _record_episode(self, decision, plan_result, policy_result):
        """记录 Episode。"""
        episode = type("Episode", (), {
            "episode_id": uuid.uuid4().hex,
            "finding_id": getattr(decision, "finding_id", ""),
            "decision_id": getattr(decision, "decision_id", ""),
            "steps": [],
        })()
        episode.steps.append({"step_type": "observation", "data": {
            "finding_id": decision.finding_id,
        }})
        episode.steps.append({"step_type": "decision", "data": {
            "candidates_scores": getattr(policy_result, "utility_scores", {}),
        }})
        self.episode_store.save(episode)
