"""Resource usage analysis skill."""

from __future__ import annotations

import re
from typing import List

from ai.skills.base import DiagnosticSkill, SkillContext, SkillStep
from ai.skills.builtin._helpers import _as_str, _generic_exec
from ai.skills.registry import register_skill


@register_skill
class ResourceUsageSkill(DiagnosticSkill):
    """
    资源用量分析技能。

    针对 OOM、内存/CPU 超限、Pod Eviction 等资源类故障，
    依次获取节点资源 → Pod 资源排序 → 资源配额检查。
    """

    name = "resource_usage"
    display_name = "资源用量分析"
    description = (
        "分析 Kubernetes 集群和 Pod 的 CPU/内存资源用量：kubectl top nodes、"
        "kubectl top pods 排序、resource quota 检查。"
        "适用于 OOM、CPU throttling、Pod Eviction 等资源类故障。"
    )
    applicable_components = ["pod", "node", "deployment", "resource", "memory", "cpu"]
    trigger_patterns = [
        re.compile(r"\bOOM\b", re.IGNORECASE),
        re.compile(r"OOMKilled", re.IGNORECASE),
        re.compile(r"memory.*limit", re.IGNORECASE),
        re.compile(r"CPU.*throttl", re.IGNORECASE),
        re.compile(r"resource.*limit", re.IGNORECASE),
        re.compile(r"\bevict", re.IGNORECASE),
        re.compile(r"out of memory", re.IGNORECASE),
        re.compile(r"Insufficient", re.IGNORECASE),
    ]
    risk_level = "low"
    max_steps = 3

    def plan_steps(self, context: SkillContext) -> List[SkillStep]:
        svc = _as_str(context.service_name)

        steps = [
            SkillStep(
                step_id="res-top-nodes",
                title="查看节点资源用量",
                command_spec=_generic_exec(
                    "kubectl top nodes --no-headers",
                    timeout_s=15,
                ),
                purpose="了解集群节点整体资源压力，判断是否存在节点级别的资源瓶颈",
                parse_hints={"extract": ["CPU", "MEMORY", "%", "Mi", "Gi"]},
            ),
            SkillStep(
                step_id="res-top-pods",
                title="查看各命名空间 Pod 资源用量排序",
                command_spec=_generic_exec(
                    "kubectl top pod -A --no-headers",
                    timeout_s=15,
                ),
                purpose="找出内存用量最高的 Pod，定位资源占用异常的进程",
                depends_on=["res-top-nodes"],
                parse_hints={"extract": ["CPU", "MEMORY", "Mi", "Gi", svc or ""]},
            ),
            SkillStep(
                step_id="res-quota-check",
                title="检查各命名空间 ResourceQuota",
                command_spec=_generic_exec(
                    "kubectl get resourcequota -A --no-headers",
                    timeout_s=15,
                ),
                purpose="确认命名空间资源配额是否已满，是否存在 requests / limits 约束压力",
                # quota 不依赖 top-pods，可与 top-pods 并行
                depends_on=["res-top-nodes"],
                parse_hints={"extract": ["Used", "Hard", "requests", "limits", "memory", "cpu"]},
            ),
        ]
        return steps
