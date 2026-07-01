"""DecisionOrchestrator — v15: 纯编排（与 StateMachine 分离）。"""
import uuid
import logging
from dataclasses import dataclass, field
from typing import Any, Optional
from datetime import datetime, timezone
from .state_machine import DecisionStateMachine, DecisionStatus
from ..policy.decision_record import DecisionRecord

logger = logging.getLogger(__name__)


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
                 policy_engine,
                 state_machine: DecisionStateMachine,
                 workflow_engine, episode_store,
                 blast_analyzer=None):
        """
        Args:
            blast_analyzer: 已废弃——保留仅用于向后兼容，不再使用。
                            风险分析的 blast radius 部分由 RiskEngine 内部处理。
        """
        self.planner = planner
        self.exec_planner = exec_planner
        self.risk_engine = risk_engine
        self.policy_engine = policy_engine
        self.state_machine = state_machine
        self.workflow_engine = workflow_engine
        self.episode_store = episode_store
        if blast_analyzer is not None:
            logger.warning(
                "blast_analyzer parameter is deprecated and unused "
                "in DecisionOrchestrator. RiskEngine handles blast radius analysis."
            )

    def execute(self, finding, context, goal=None) -> DecisionResult:
        """完整决策执行流程。"""
        decision_id = uuid.uuid4().hex
        decision = DecisionRecord(
            decision_id=decision_id,
            finding_id=getattr(finding, "id", "") or getattr(finding, "category", ""),
            status=DecisionStatus.CREATED,
        )

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
            logger.error(
                "DecisionOrchestrator.execute failed: %s (decision_id=%s)",
                str(e), decision_id, exc_info=True,
            )
            # 仅在状态机状态允许时 FAILED 转换
            current_status = DecisionStatus(decision.status) if hasattr(decision, "status") else None
            if current_status and current_status not in (
                DecisionStatus.SUCCEEDED, DecisionStatus.FAILED,
                DecisionStatus.ROLLED_BACK, DecisionStatus.CANCELLED,
            ):
                try:
                    self.state_machine.transition(decision, DecisionStatus.FAILED)
                except Exception:
                    pass  # 状态机拒绝转换时不做任何事——记录即可
            return DecisionResult(decision=decision, status="failed")

    def _record_episode(self, decision, plan_result, policy_result):
        """记录 Episode。"""
        from ..episode.models import Episode, EpisodeStep

        episode = Episode(
            episode_id=uuid.uuid4().hex,
            finding_id=getattr(decision, "finding_id", ""),
            decision_id=getattr(decision, "decision_id", ""),
        )
        episode.add_step("observation", {
            "finding_id": decision.finding_id,
            "decision_id": decision.decision_id,
        })
        episode.add_step("decision", {
            "candidates_scores": getattr(policy_result, "utility_scores", {}),
            "selected_candidate_id": getattr(
                getattr(policy_result, "selected_candidate", None),
                "workflow", None
            ),
            "reject_reasons": getattr(decision, "rejected_candidates", []),
        })
        self.episode_store.save(episode)
