"""Episode — 完整决策轨迹（v15: + DecisionStep）。"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


class AppendOnlyList(list):
    """
    只追加列表——禁止索引赋值和删除。

    仅允许 list.append()、list.extend()、list.__iadd__() 等追加操作。
    用于确保 Episode.steps 不可修改（immutable after append）。
    """

    def __setitem__(self, index, value):
        raise TypeError("AppendOnlyList does not support item assignment")

    def __delitem__(self, index):
        raise TypeError("AppendOnlyList does not support item deletion")

    def insert(self, index, value):
        raise TypeError("AppendOnlyList does not support insert")

    def pop(self, index=-1):
        raise TypeError("AppendOnlyList does not support pop")

    def remove(self, value):
        raise TypeError("AppendOnlyList does not support remove")

    def clear(self):
        raise TypeError("AppendOnlyList does not support clear")

    def sort(self, *, key=None, reverse=False):
        raise TypeError("AppendOnlyList does not support sort")

    def reverse(self):
        raise TypeError("AppendOnlyList does not support reverse")


@dataclass
class EpisodeStep:
    """Episode 步骤基类。"""
    order: int = 0
    step_type: str = ""  # observation, hypothesis, goal_choice, decision, intent, workflow, execution, outcome, user_feedback
    data: Dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class DecisionStep(EpisodeStep):
    """
    决策步骤——v15 新增。记录候选方案评分和拒绝理由。

    这是 LLM 训练最宝贵的数据——记录了"为什么不选 A"。
    """
    step_type: str = "decision"
    candidates_scores: Dict[str, float] = field(default_factory=dict)
    selected_candidate_id: str = ""
    reject_reasons: List[str] = field(default_factory=list)
    selected_reason: str = ""


@dataclass
class Episode:
    """
    完整决策轨迹——Event Sourcing 事实（append-only）。

    - 记录从观察到决策到执行的完整路径
    - 包含 DecisionStep（v15 新增）
    - 不可修改的 steps 列表
    """
    episode_id: str = ""
    finding_id: str = ""
    decision_id: str = ""
    context_hash: str = ""
    steps: AppendOnlyList = field(default_factory=AppendOnlyList)
    final_outcome: str = ""
    total_duration_ms: int = 0
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def add_step(self, step_type: str, data: Dict[str, Any]) -> EpisodeStep:
        """添加一个步骤（append-only）。

        当 step_type == "decision" 时创建 DecisionStep，
        将 candidates_scores/reject_reasons 等字段提取到专用属性中。
        """
        if step_type == "decision":
            step = DecisionStep(
                order=len(self.steps),
                step_type="decision",
                candidates_scores=data.get("candidates_scores", {}),
                selected_candidate_id=data.get("selected_candidate_id", ""),
                reject_reasons=data.get("reject_reasons", []),
                selected_reason=data.get("selected_reason", ""),
                data=data,
            )
        else:
            step = EpisodeStep(
                order=len(self.steps),
                step_type=step_type,
                data=data,
            )
        self.steps.append(step)
        return step
