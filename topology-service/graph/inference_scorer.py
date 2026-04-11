"""
P1: 拓扑推断打分器（模块化）

用于 hybrid_score 模式的概率化融合打分：
- 支持度 / 主导证据比例
- 多证据多样性
- 命名空间一致性
- 时间稳定性
"""

import math
from typing import Dict, List, Tuple


class InferenceScorer:
    """推断边打分器（P1）。"""

    METHOD_PRIOR = {
        "request_id": 1.35,
        "trace_id": 1.0,
        "message_target": 0.92,
        "time_window": 0.28,
    }

    @staticmethod
    def _clamp(value: float, lower: float, upper: float) -> float:
        return max(lower, min(upper, value))

    @staticmethod
    def temporal_stability(gaps_sec: List[float]) -> float:
        """
        根据时间间隔稳定性返回 [0,1]。
        间隔越小越一致，稳定性越高。
        """
        if not gaps_sec:
            return 0.45
        values = [max(0.0, float(gap)) for gap in gaps_sec if gap is not None]
        if not values:
            return 0.45

        mean_gap = sum(values) / len(values)
        variance = sum((value - mean_gap) ** 2 for value in values) / len(values)
        stdev = math.sqrt(variance)

        compactness = 1.0 / (1.0 + mean_gap * 2.5)
        consistency = 1.0 / (1.0 + stdev * 6.0)
        return InferenceScorer._clamp(compactness * 0.58 + consistency * 0.42, 0.0, 1.0)

    def score_hybrid_edge(
        self,
        method: str,
        support_value: float,
        min_support: float,
        dominant_ratio: float,
        diversity_ratio: float,
        namespace_consistency: float,
        temporal_stability: float,
        weighted_density: float,
        directional_consistency: float = 1.0,
    ) -> Dict[str, float]:
        """
        输出：
        - confidence: [0, 0.98]
        - evidence_score: [0, 100]
        """
        method_prior = self.METHOD_PRIOR.get(str(method or "").strip().lower(), 0.25)
        support_ratio = support_value / max(1.0, float(min_support or 1.0))
        support_term = math.log1p(max(0.0, support_ratio))

        z = (
            method_prior
            + support_term * 0.90
            + self._clamp(dominant_ratio, 0.0, 1.0) * 0.62
            + self._clamp(diversity_ratio, 0.0, 1.0) * 0.26
            + self._clamp(namespace_consistency, 0.0, 1.0) * 0.32
            + self._clamp(temporal_stability, 0.0, 1.0) * 0.22
            + self._clamp(directional_consistency, 0.0, 1.0) * 0.24
            + self._clamp(weighted_density, 0.0, 2.0) * 0.14
        )

        confidence = 1.0 / (1.0 + math.exp(-z))
        confidence = self._clamp(confidence, 0.0, 0.98)

        evidence_score = (
            self._clamp(support_ratio, 0.0, 2.0) / 2.0 * 52.0
            + self._clamp(dominant_ratio, 0.0, 1.0) * 20.0
            + self._clamp(diversity_ratio, 0.0, 1.0) * 9.0
            + self._clamp(namespace_consistency, 0.0, 1.0) * 9.0
            + self._clamp(temporal_stability, 0.0, 1.0) * 8.0
            + self._clamp(directional_consistency, 0.0, 1.0) * 2.0
        )
        evidence_score = self._clamp(evidence_score, 0.0, 100.0)

        return {
            "confidence": round(confidence, 3),
            "evidence_score": round(evidence_score, 2),
            "support_ratio": round(support_ratio, 3),
        }
