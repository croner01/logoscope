"""PolicyEngine — 策略引擎（OPA effects-based + 可配置 Utility 权重）。"""
import uuid
from typing import List, Optional, Any
from dataclasses import dataclass
from .models import UtilityWeights, PolicyEvaluationResult, PolicyDecision
from .decision_record import DecisionRecord, DecisionRecordStore


class PolicyEngine:
    """
    策略引擎。

    - 使用 Utility 权重对候选排序
    - 高风险（>=80）自动拒绝
    - 中风险（40-80）需要人工审批
    - 低风险（<40）自动批准
    - 自动创建 DecisionRecord
    """

    def __init__(self, weights: UtilityWeights,
                 blast_analyzer=None, risk_engine=None,
                 decision_store=None):
        self.weights = weights
        self.blast_analyzer = blast_analyzer
        self.risk_engine = risk_engine
        self.decision_store = decision_store or DecisionRecordStore()

    def evaluate(self, candidates: List, action: str,
                 entity_type: str, entity_name: str,
                 finding_id: str = "") -> PolicyEvaluationResult:
        if not candidates:
            return PolicyEvaluationResult(decision=PolicyDecision.DENY)

        # 1. 排序
        ranked = self._rank(candidates, entity_type, entity_name)

        # 2. 选择最佳
        best = ranked[0]
        utility_scores = {c.workflow.name: self._compute_utility(c, entity_type, entity_name)
                          for c in ranked}

        # 3. 决策
        if best.final_risk >= 80:
            decision = PolicyDecision.DENY
        elif best.final_risk >= 40:
            decision = PolicyDecision.PENDING_APPROVAL
        else:
            decision = PolicyDecision.CANDIDATE_SELECTED

        # 4. 记录 DecisionRecord
        record = DecisionRecord(
            decision_id=uuid.uuid4().hex,
            finding_id=finding_id,
            selected_candidate=best,
            policy_rules_matched=[f"final_risk={best.final_risk}"],
            rejected_candidates=[
                f"{c.workflow.name}: final_risk={c.final_risk}"
                for c in ranked[1:]
            ] if len(ranked) > 1 else [],
        )
        self.decision_store.save(record)

        return PolicyEvaluationResult(
            decision=decision,
            selected_candidate=best,
            utility_scores=utility_scores,
        )

    def _rank(self, candidates: List, entity_type: str, entity_name: str) -> List:
        """按 Utility 降序排列。"""
        return sorted(
            candidates,
            key=lambda c: self._compute_utility(c, entity_type, entity_name),
            reverse=True,
        )

    def _compute_utility(self, candidate, entity_type: str, entity_name: str) -> float:
        """计算候选方案的 Utility 分数。"""
        success = getattr(candidate, "estimated_success_rate", 0.5) * 100
        risk = getattr(candidate, "final_risk", 50)
        duration = getattr(candidate, "estimated_duration_minutes", 5)
        vm_count = 0
        if self.blast_analyzer:
            try:
                report = self.blast_analyzer.analyze(
                    None, entity_type, entity_name)
                vm_count = getattr(report, "estimated_vm_count", 0)
            except Exception:
                pass

        return (
            success * self.weights.success
            - risk * self.weights.risk
            - duration * self.weights.cost
            - vm_count * self.weights.blast
        )
