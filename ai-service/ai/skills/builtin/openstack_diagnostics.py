"""OpenStack diagnostics skill."""

from __future__ import annotations

import re
from typing import List

from ai.skills.base import DiagnosticSkill, SkillContext, SkillStep
from ai.skills.builtin._helpers import _as_str, _generic_exec
from ai.skills.registry import register_skill


@register_skill
class OpenStackDiagnosticsSkill(DiagnosticSkill):
    """OpenStack 组件日志排障技能。"""

    name = "openstack_diagnostics"
    display_name = "OpenStack 诊断"
    description = "针对 Nova/Neutron/Cinder/Glance 常见故障，拉取组件日志与服务状态进行快速定位。"
    applicable_components = ["openstack", "nova", "neutron", "cinder", "glance"]
    trigger_patterns = [
        re.compile(r"\bnova\b", re.IGNORECASE),
        re.compile(r"\bneutron\b", re.IGNORECASE),
        re.compile(r"\bcinder\b", re.IGNORECASE),
        re.compile(r"\bglance\b", re.IGNORECASE),
        re.compile(r"instance.*(fail|error)", re.IGNORECASE),
        re.compile(r"network.*(fail|down|unreachable)", re.IGNORECASE),
        re.compile(r"volume.*(attach|mount).*(fail|error)", re.IGNORECASE),
    ]
    risk_level = "low"
    max_steps = 4

    def plan_steps(self, context: SkillContext) -> List[SkillStep]:
        return [
            SkillStep(
                step_id="openstack-nova-log",
                title="查找并拉取 Nova 关键日志",
                # FIX: kubectl logs -A (not kubectl -A logs) is the correct syntax
                command_spec=_generic_exec(
                    "kubectl logs -A -l app=nova-api --since=20m --tail=200 2>/dev/null || "
                    "kubectl logs -A -l 'app in (nova-api,api)' --since=20m --tail=200",
                    timeout_s=25,
                ),
                purpose="定位实例创建/调度失败的关键报错，自动跨命名空间查找 Nova Pod",
            ),
            SkillStep(
                step_id="openstack-neutron-log",
                title="拉取 Neutron 关键日志（跨命名空间）",
                command_spec=_generic_exec(
                    "kubectl logs -A -l app=neutron-server --since=20m --tail=200 2>/dev/null || "
                    "kubectl logs -A -l 'app in (neutron-server,neutron)' --since=20m --tail=200",
                    timeout_s=25,
                ),
                purpose="定位网络不通、端口绑定失败、租户网络异常",
                depends_on=["openstack-nova-log"],
            ),
            SkillStep(
                step_id="openstack-cinder-log",
                title="拉取 Cinder 关键日志（跨命名空间）",
                command_spec=_generic_exec(
                    "kubectl logs -A -l app=cinder-volume --since=20m --tail=200 2>/dev/null || "
                    "kubectl logs -A -l 'app in (cinder-volume,cinder)' --since=20m --tail=200",
                    timeout_s=25,
                ),
                purpose="定位卷挂载失败、存储后端连接异常",
                # cinder 不依赖 neutron，可并行
                depends_on=["openstack-nova-log"],
            ),
            SkillStep(
                step_id="openstack-service-status",
                title="查看 OpenStack 组件 Pod 状态（跨命名空间）",
                command_spec=_generic_exec(
                    "kubectl get pods -A -l 'app in (nova-api,neutron-server,cinder-volume,glance-api)' -o wide",
                    timeout_s=15,
                ),
                purpose="确认核心组件运行状态与重启情况",
                depends_on=["openstack-nova-log"],
            ),
        ]
