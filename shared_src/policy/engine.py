"""PolicyEngine — 策略引擎（Utility 权重可配置 + OPA 集成）。"""
import uuid
import logging
from typing import List, Optional, Any
from dataclasses import dataclass
from .models import UtilityWeights, PolicyEvaluationResult, PolicyDecision
from .decision_record import DecisionRecord, DecisionRecordStore

logger = logging.getLogger(__name__)


class PolicyEngine:
    """
    策略引擎。

    - 使用 Utility 权重对候选排序
    - 高风险（>=80）自动拒绝
    - 中风险（40-80）需要人工审批
    - 低风险（<40）自动批准
    - 自动创建 DecisionRecord
    - 支持 OPA 集成（通过 opa_endpoint 配置）
    """

    # Python 内建的风险阈值（与 deploy/policies/high_risk.rego 保持同步）
    RISK_DENY = 80
    RISK_PENDING = 40

    def __init__(self, weights: UtilityWeights,
                 blast_analyzer=None, risk_engine=None,
                 decision_store=None,
                 opa_endpoint: Optional[str] = None):
        """
        Args:
            opa_endpoint: OPA 服务地址（可选）。
                          未配置时使用 Python 内建逻辑，
                          配置后额外使用 OPA Rego 策略评估。
        """
        self.weights = weights
        self.blast_analyzer = blast_analyzer
        self.risk_engine = risk_engine
        self.decision_store = decision_store or DecisionRecordStore()
        self.opa_endpoint = opa_endpoint

        if opa_endpoint:
            logger.info("OPA integration enabled at %s", opa_endpoint)
            try:
                import requests
                self._opa_session = requests.Session()
            except ImportError:
                logger.warning(
                    "OPA endpoint configured but 'requests' not installed. "
                    "OPA evaluation disabled. Install with: pip install requests"
                )
                self.opa_endpoint = None
        else:
            logger.info(
                "OPA not configured — using Python built-in risk thresholds "
                "(deny>=%d, pending>=%d). Set opa_endpoint for OPA Rego policies.",
                self.RISK_DENY, self.RISK_PENDING,
            )

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

        # 3. OPA 评估（如已配置）
        opa_match = self._evaluate_opa(candidates, action, entity_type, entity_name)

        # 4. 决策（OPA 结果优先，OPA 未配置时使用 Python 内建逻辑）
        if opa_match is not None:
            decision = opa_match
        elif best.final_risk >= self.RISK_DENY:
            decision = PolicyDecision.DENY
        elif best.final_risk >= self.RISK_PENDING:
            decision = PolicyDecision.PENDING_APPROVAL
        else:
            decision = PolicyDecision.CANDIDATE_SELECTED

        # 5. 记录 DecisionRecord
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
                logger.exception("Blast radius analysis failed in utility "
                                 "computation for %s/%s", entity_type, entity_name)

        return (
            success * self.weights.success
            - risk * self.weights.risk
            - duration * self.weights.cost
            - vm_count * self.weights.blast
        )

    def _evaluate_opa(self, candidates, action: str,
                       entity_type: str, entity_name: str
                       ) -> Optional[PolicyDecision]:
        """
        向 OPA 服务评估策略（需配置 opa_endpoint）。

        未配置 opa_endpoint 时返回 None —— 回退到 Python 内建逻辑。
        """
        if not self.opa_endpoint:
            return None

        try:
            import requests
            input_data = {
                "candidate": {
                    "estimated_success_rate": candidates[0].estimated_success_rate if candidates else 0,
                    "risk": {
                        "final_risk": candidates[0].final_risk if candidates else 0,
                    },
                    "estimated_duration_minutes": candidates[0].estimated_duration_minutes if candidates else 0,
                },
                "action": action,
                "resource": {
                    "type": entity_type,
                    "id": entity_name,
                },
            }
            resp = self._opa_session.post(
                f"{self.opa_endpoint}/v1/data/logoscope/policy",
                json={"input": input_data},
                timeout=5,
            )
            if resp.status_code == 200:
                result = resp.json()
                decision_str = result.get("result", {}).get("decision", "deny")
                for d in PolicyDecision:
                    if d.value == decision_str:
                        return d
            logger.warning("OPA returned non-200: %s", resp.status_code)
        except requests.RequestException as e:
            logger.error("OPA request failed: %s — falling back to built-in", str(e))
        except Exception:
            logger.exception("Unexpected OPA error — falling back to built-in")

        return None  # fallback to built-in logic
