"""Risk 数据模型。"""
from dataclasses import dataclass, field


@dataclass
class RiskProfile:
    """
    风险画像——三层风险评估结果。

    - business_risk: 业务风险（操作对业务的影响）
    - execution_risk: 执行风险（操作本身的失败概率）
    - operational_risk: 运维风险（环境、依赖、约束）
    - final_risk: 综合风险（加权汇总）
    """
    business_risk: int = 0
    execution_risk: int = 0
    operational_risk: int = 0
    final_risk: int = 0
