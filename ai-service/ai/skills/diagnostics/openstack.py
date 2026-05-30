"""OpenStack diagnostic skill for OpenStack component issues."""

from __future__ import annotations

import re
from typing import Any, Dict, List

from ai.skills.base import DiagnosticSkill, SkillContext, SkillStep


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _generic_exec(command: str, *, timeout_s: int = 30) -> Dict[str, Any]:
    return {
        "tool": "generic_exec",
        "args": {
            "command": command,
            "target_kind": "runtime_node",
            "target_identity": "runtime:local",
            "timeout_s": timeout_s,
        },
        "command": command,
        "timeout_s": timeout_s,
    }


@DiagnosticSkill.register
class OpenStackDiagnosticSkill(DiagnosticSkill):
    """
    OpenStack 组件诊断技能。

    适用于 Nova（实例）、Neutron（网络）、Cinder（存储）、Glance（镜像）等
    组件的常见故障定位。
    """

    name = "openstack_diagnostic"
    display_name = "OpenStack 组件诊断"
    description = (
        "针对 OpenStack 各组件（Nova、Neutron、Cinder、Glance 等）故障，"
        "如实例启动失败、网络不通、存储挂载异常、镜像拉取失败等，"
        "执行组件状态、日志、联动关系诊断。"
    )
    applicable_components = [
        "nova", "neutron", "cinder", "glance", "keystone", "horizon",
        "openstack", "instance", "vm", "volume", "image", "network",
    ]
    trigger_patterns = [
        re.compile(r"nova", re.IGNORECASE),
        re.compile(r"neutron", re.IGNORECASE),
        re.compile(r"cinder", re.IGNORECASE),
        re.compile(r"glance", re.IGNORECASE),
        re.compile(r"openstack", re.IGNORECASE),
        re.compile(r"instance.*fail", re.IGNORECASE),
        re.compile(r"vm.*error", re.IGNORECASE),
        re.compile(r"volume.*attach", re.IGNORECASE),
        re.compile(r"network.*error", re.IGNORECASE),
        re.compile(r"hypervisor", re.IGNORECASE),
        re.compile(r"scheduler.*fail", re.IGNORECASE),
        re.compile(r"connection.*refused.*nova", re.IGNORECASE),
        re.compile(r"error.*500", re.IGNORECASE),
    ]
    risk_level = "medium"
    max_steps = 4

    def plan_steps(self, context: SkillContext) -> List[SkillStep]:
        ns = _as_str(context.namespace) or "islap"
        svc = _as_str(context.service_name)
        label_flag = f"-l app={svc}" if svc else ""

        steps = [
            SkillStep(
                step_id="os-describe-pod",
                title="查看 OpenStack 组件 Pod",
                command_spec=_generic_exec(
                    f"kubectl get pods -n {ns} {label_flag} -o wide".strip(),
                    timeout_s=15,
                ),
                purpose="查看 OpenStack 组件 Pod 状态和所在节点",
                parse_hints={"extract": ["STATUS", "READY", "RESTARTS"]},
            ),
            SkillStep(
                step_id="os-logs-tail",
                title="拉取 OpenStack 组件日志",
                command_spec=_generic_exec(
                    f"kubectl logs -n {ns} {label_flag} --tail=100 --previous 2>/dev/null || "
                    f"kubectl logs -n {ns} {label_flag} --tail=100".strip(),
                    timeout_s=25,
                ),
                purpose="定位 OpenStack 组件错误日志和异常",
                depends_on=["os-describe-pod"],
                parse_hints={"extract": ["ERROR", "WARNING", "Exception", "Failed"]},
            ),
            SkillStep(
                step_id="os-describe-svc",
                title="查看 K8s Service 配置",
                command_spec=_generic_exec(
                    f"kubectl describe svc -n {ns} {label_flag} || kubectl get svc -n {ns} | head -20",
                    timeout_s=15,
                ),
                purpose="查看 OpenStack 组件的 Service 配置",
                depends_on=["os-describe-pod"],
                parse_hints={"extract": ["Endpoints", "Selector", "Ports"]},
            ),
            SkillStep(
                step_id="os-get-events",
                title="查看相关 Events",
                command_spec=_generic_exec(
                    f"kubectl get events -n {ns} --sort-by=.lastTimestamp | tail -30",
                    timeout_s=15,
                ),
                purpose="查看相关集群事件",
                depends_on=["os-describe-pod"],
                parse_hints={"extract": ["Warning", "Failed", "Error"]},
            ),
        ]
        return steps


@DiagnosticSkill.register
class OpenStackNetworkDiagnosticSkill(DiagnosticSkill):
    """
    OpenStack 网络诊断技能。

    适用于 Neutron 网络问题，如实例无法获取 IP、路由器异常、安全组规则等。
    """

    name = "openstack_network_diagnostic"
    display_name = "OpenStack 网络诊断"
    description = (
        "针对 Neutron 网络问题（实例无法获取 IP、路由器异常、安全组阻断、"
        "DHCP 问题、VLAN/VXLAN 隧道故障等），执行网络诊断。"
    )
    applicable_components = ["neutron", "network", "router", "subnet", "port", "security-group"]
    trigger_patterns = [
        re.compile(r"neutron", re.IGNORECASE),
        re.compile(r"dhcp.*fail", re.IGNORECASE),
        re.compile(r"router.*error", re.IGNORECASE),
        re.compile(r"security.*group", re.IGNORECASE),
        re.compile(r"port.*down", re.IGNORECASE),
        re.compile(r"network.*unreachable", re.IGNORECASE),
        re.compile(r"ip.*assign", re.IGNORECASE),
        re.compile(r"vxlan", re.IGNORECASE),
        re.compile(r"vlan", re.IGNORECASE),
    ]
    risk_level = "low"
    max_steps = 3

    def plan_steps(self, context: SkillContext) -> List[SkillStep]:
        ns = _as_str(context.namespace) or "islap"

        steps = [
            SkillStep(
                step_id="os-net-pods",
                title="查看 Neutron Pod 状态",
                command_spec=_generic_exec(
                    f"kubectl get pods -n {ns} | grep -E 'neutron|openvswitch|openvswitch-agent' | head -10",
                    timeout_s=15,
                ),
                purpose="确认 Neutron 组件运行状态",
                parse_hints={"extract": ["STATUS", "READY"]},
            ),
            SkillStep(
                step_id="os-net-logs",
                title="查看 Neutron 日志",
                command_spec=_generic_exec(
                    f"kubectl logs -n {ns} -l app=neutron-server --tail=100 2>/dev/null || "
                    f"kubectl logs -n {ns} -l component=neutron --tail=100 2>/dev/null || "
                    f"echo 'Neutron logs not found'",
                    timeout_s=25,
                ),
                purpose="查看 Neutron 错误日志",
                depends_on=["os-net-pods"],
                parse_hints={"extract": ["ERROR", "WARNING", "Exception", "CRITICAL"]},
            ),
            SkillStep(
                step_id="os-net-agent",
                title="查看网络 Agent 状态",
                command_spec=_generic_exec(
                    f"kubectl get pods -n {ns} -o wide | grep -E 'openvswitch|ovs|agent' || "
                    f"kubectl get nodes",
                    timeout_s=15,
                ),
                purpose="查看网络 Agent 分布和状态",
                parse_hints={"extract": ["STATUS", "NODE"]},
            ),
        ]
        return steps
