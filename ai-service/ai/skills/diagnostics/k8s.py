"""K8s diagnostic skill for Kubernetes cluster issues."""

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
class K8sDiagnosticSkill(DiagnosticSkill):
    """
    K8s 集群诊断技能。

    适用于 Pod 启动失败、调度异常、网络策略冲突、资源不足、
    CrashLoopBackOff、OOMKilled、ImagePullBackOff 等场景。
    """

    name = "k8s_diagnostic"
    display_name = "K8s 集群诊断"
    description = (
        "针对 K8s 集群问题（Pod 启动失败、CrashLoopBackOff、OOMKilled、"
        "ImagePullBackOff、调度异常、网络策略冲突等），执行 describe → logs "
        "→ events → top 完整诊断链。"
    )
    applicable_components = [
        "pod", "deployment", "service", "configmap", "secret",
        "ingress", "statefulset", "daemonset", "job", "cronjob",
        "k8s", "kubernetes", "container",
    ]
    trigger_patterns = [
        re.compile(r"CrashLoopBackOff", re.IGNORECASE),
        re.compile(r"OOMKilled", re.IGNORECASE),
        re.compile(r"ImagePullBackOff", re.IGNORECASE),
        re.compile(r"Evicted", re.IGNORECASE),
        re.compile(r"Terminating", re.IGNORECASE),
        re.compile(r"Pending", re.IGNORECASE),
        re.compile(r"ContainerCreating", re.IGNORECASE),
        re.compile(r"Back-off restarting", re.IGNORECASE),
        re.compile(r"pod.*restart", re.IGNORECASE),
        re.compile(r"FailedScheduling", re.IGNORECASE),
        re.compile(r"network.*policy", re.IGNORECASE),
        re.compile(r"insufficient.*cpu", re.IGNORECASE),
        re.compile(r"insufficient.*memory", re.IGNORECASE),
        re.compile(r"pvc.*pending", re.IGNORECASE),
        re.compile(r"volume.*mount", re.IGNORECASE),
        re.compile(r"Error.*container", re.IGNORECASE),
        re.compile(r"kubectl", re.IGNORECASE),
    ]
    risk_level = "low"
    max_steps = 5

    def plan_steps(self, context: SkillContext) -> List[SkillStep]:
        ns = _as_str(context.namespace) or "islap"
        svc = _as_str(context.service_name)
        label_flag = f"-l app={svc}" if svc else ""

        steps = [
            SkillStep(
                step_id="k8s-describe-pod",
                title="Describe Pod 详情",
                command_spec=_generic_exec(
                    f"kubectl describe pod {label_flag} -n {ns} --tail=50".strip(),
                    timeout_s=20,
                ),
                purpose="查看 Pod 状态、重启原因、资源限制、最近 Events",
                parse_hints={"extract": ["Restart Count", "Last State", "Reason", "OOMKilled", "Events"]},
            ),
            SkillStep(
                step_id="k8s-logs-current",
                title="获取当前容器日志",
                command_spec=_generic_exec(
                    f"kubectl logs {label_flag} -n {ns} --tail=100 --all-containers=true".strip(),
                    timeout_s=25,
                ),
                purpose="定位当前错误日志和异常信息",
                depends_on=["k8s-describe-pod"],
                parse_hints={"extract": ["ERROR", "Exception", "FATAL", "panic", "signal"]},
            ),
            SkillStep(
                step_id="k8s-logs-previous",
                title="获取前一次容器日志",
                command_spec=_generic_exec(
                    f"kubectl logs {label_flag} -n {ns} --previous --tail=100 --all-containers=true 2>/dev/null || echo 'No previous logs'".strip(),
                    timeout_s=25,
                ),
                purpose="查看崩溃前的日志，定位崩溃原因",
                depends_on=["k8s-describe-pod"],
                parse_hints={"extract": ["ERROR", "Exception", "FATAL"]},
            ),
            SkillStep(
                step_id="k8s-get-events",
                title="获取命名空间 Events",
                command_spec=_generic_exec(
                    f"kubectl get events -n {ns} --sort-by=.lastTimestamp | tail -30",
                    timeout_s=15,
                ),
                purpose="查看最近集群事件，关注 Warning 和 Killing",
                depends_on=["k8s-describe-pod"],
                parse_hints={"extract": ["Warning", "Killing", "BackOff", "FailedScheduling", "FailedCreatePodSandBox"]},
            ),
            SkillStep(
                step_id="k8s-top-pod",
                title="查看 Pod 资源用量",
                command_spec=_generic_exec(
                    f"kubectl top pod -n {ns} --no-headers 2>/dev/null | sort -k3 -hr | head -20 || echo 'Metrics not available'",
                    timeout_s=15,
                ),
                purpose="确认内存/CPU是否达到limit，辅助OOM判断",
                depends_on=["k8s-describe-pod"],
                parse_hints={"extract": ["CPU", "MEMORY", "Mi", "Gi"]},
            ),
        ]
        return steps


@DiagnosticSkill.register
class K8sNetworkDiagnosticSkill(DiagnosticSkill):
    """
    K8s 网络诊断技能。

    适用于 Service 无法访问、Ingress 错误、网络策略阻断等场景。
    """

    name = "k8s_network_diagnostic"
    display_name = "K8s 网络诊断"
    description = (
        "针对 K8s 网络问题（Service 无法访问、Ingress 错误、网络策略阻断、"
        "DNS 解析失败等），执行网络连通性和策略诊断。"
    )
    applicable_components = ["service", "ingress", "endpoint", "networkpolicy", "k8s"]
    trigger_patterns = [
        re.compile(r"service.*unavailable", re.IGNORECASE),
        re.compile(r"ingress.*error", re.IGNORECASE),
        re.compile(r"network.*policy", re.IGNORECASE),
        re.compile(r"connection.*refused", re.IGNORECASE),
        re.compile(r"timeout", re.IGNORECASE),
        re.compile(r"dns.*fail", re.IGNORECASE),
        re.compile(r"503.*service.*unavailable", re.IGNORECASE),
        re.compile(r"502.*bad.*gateway", re.IGNORECASE),
    ]
    risk_level = "low"
    max_steps = 4

    def plan_steps(self, context: SkillContext) -> List[SkillStep]:
        ns = _as_str(context.namespace) or "islap"
        svc = _as_str(context.service_name)

        steps = [
            SkillStep(
                step_id="k8s-net-describe-svc",
                title="查看 Service 配置",
                command_spec=_generic_exec(
                    f"kubectl describe svc {svc} -n {ns}" if svc else f"kubectl get svc -n {ns}",
                    timeout_s=15,
                ),
                purpose="查看 Service 配置、selector 和 endpoints",
                parse_hints={"extract": ["Endpoints", "Selector", "Ports"]},
            ),
            SkillStep(
                step_id="k8s-net-get-endpoints",
                title="查看 Endpoints 状态",
                command_spec=_generic_exec(
                    f"kubectl get endpoints {svc} -n {ns} -o wide",
                    timeout_s=10,
                ),
                purpose="确认 Endpoints 是否正确关联到 Pod",
                depends_on=["k8s-net-describe-svc"],
                parse_hints={"extract": ["ADDRESSES"]},
            ),
            SkillStep(
                step_id="k8s-net-get-pods",
                title="查看 Pod 网络状态",
                command_spec=_generic_exec(
                    f"kubectl get pods -n {ns} -l app={svc} -o wide" if svc else f"kubectl get pods -n {ns} -o wide",
                    timeout_s=15,
                ),
                purpose="查看 Pod 状态和 IP 分配",
                depends_on=["k8s-net-describe-svc"],
                parse_hints={"extract": ["STATUS", "IP", "NODE"]},
            ),
            SkillStep(
                step_id="k8s-net-describe-ingress",
                title="查看 Ingress 配置",
                command_spec=_generic_exec(
                    f"kubectl describe ingress -n {ns} | grep -A 20 '{svc}' || kubectl get ingress -n {ns}",
                    timeout_s=15,
                ),
                purpose="查看 Ingress 规则和后端配置",
                parse_hints={"extract": ["Host", "Backend", "Rules"]},
            ),
        ]
        return steps
