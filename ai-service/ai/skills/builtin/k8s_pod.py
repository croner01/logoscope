"""K8s Pod diagnostics skill."""

from __future__ import annotations

import re
from typing import List

from ai.skills.base import DiagnosticSkill, SkillContext, SkillStep
from ai.skills.builtin._helpers import _as_str, _generic_exec
from ai.skills.registry import register_skill


@register_skill
class K8sPodDiagnosticsSkill(DiagnosticSkill):
    """
    K8s Pod 深度诊断技能。

    适用于 Pod 崩溃、重启、镜像拉取失败等场景，自动执行
    describe → logs → events → top 完整诊断链。
    """

    name = "k8s_pod_diagnostics"
    display_name = "K8s Pod 诊断"
    description = (
        "针对 Pod 崩溃、CrashLoopBackOff、OOMKilled、ImagePullBackOff 等场景，"
        "依次执行 kubectl describe pod、kubectl logs、kubectl get events、"
        "kubectl top pod，收集完整故障证据。"
    )
    applicable_components = ["pod", "deployment", "container", "k8s", "kubernetes"]
    trigger_patterns = [
        re.compile(r"CrashLoopBackOff", re.IGNORECASE),
        re.compile(r"OOMKilled", re.IGNORECASE),
        re.compile(r"ImagePullBackOff", re.IGNORECASE),
        re.compile(r"pod.*restart", re.IGNORECASE),
        re.compile(r"container.*fail", re.IGNORECASE),
        re.compile(r"Back-off restarting", re.IGNORECASE),
        re.compile(r"Error.*container", re.IGNORECASE),
    ]
    risk_level = "low"
    max_steps = 5

    def plan_steps(self, context: SkillContext) -> List[SkillStep]:
        svc = _as_str(context.service_name)
        label_flag = f"-l app={svc}" if svc else ""
        # -A covers all namespaces; works with both get and describe
        ns_flag = "-A"

        steps = [
            SkillStep(
                step_id="k8s-locate-pod",
                title="查找 Pod 所在的命名空间",
                command_spec=_generic_exec(
                    f"kubectl get pods {ns_flag} {label_flag} -o wide 2>/dev/null | head -20".strip(),
                    timeout_s=15,
                ),
                purpose="确认目标 Pod 是否存在及其正确的命名空间",
                parse_hints={
                    "extract": ["NAMESPACE", "NAME", "STATUS", "RESTARTS"]
                },
            ),
            SkillStep(
                step_id="k8s-describe-pod",
                title="Describe Pod 详情",
                # FIX: kubectl describe does NOT support --tail; removed invalid flag
                command_spec=_generic_exec(
                    f"kubectl describe pod {label_flag} {ns_flag}".strip(),
                    timeout_s=20,
                ),
                purpose="查看 Pod 状态、重启原因、资源限制、最近 Events",
                depends_on=["k8s-locate-pod"],
                parse_hints={
                    "extract": ["Restart Count", "Last State", "Reason", "OOMKilled", "Events"]
                },
            ),
            SkillStep(
                step_id="k8s-logs-tail",
                title="获取容器最近日志",
                command_spec=_generic_exec(
                    f"kubectl logs {label_flag} {ns_flag} --tail=100 --previous 2>/dev/null || "
                    f"kubectl logs {label_flag} {ns_flag} --tail=100".strip(),
                    timeout_s=25,
                ),
                purpose="定位崩溃时刻的错误栈和关键异常",
                depends_on=["k8s-describe-pod"],
                parse_hints={"extract": ["ERROR", "Exception", "FATAL", "panic", "signal"]},
            ),
            SkillStep(
                step_id="k8s-get-events",
                title="获取集群 Events（跨命名空间）",
                command_spec=_generic_exec(
                    f"kubectl get events {ns_flag} --sort-by=.lastTimestamp | tail -30",
                    timeout_s=15,
                ),
                purpose="查看最近集群事件，关注 Warning 和 Killing",
                # events 不依赖 describe，可与 logs 并行
                depends_on=["k8s-locate-pod"],
                parse_hints={"extract": ["Warning", "Killing", "BackOff", "FailedScheduling"]},
            ),
            SkillStep(
                step_id="k8s-top-pod",
                title="查看各命名空间 Pod 资源用量",
                command_spec=_generic_exec(
                    f"kubectl top pod {ns_flag} --no-headers 2>/dev/null | sort -k3 -hr | head -20",
                    timeout_s=15,
                ),
                purpose="确认内存/CPU 是否已达到 limit，辅助 OOM 判断",
                depends_on=["k8s-locate-pod"],
                parse_hints={"extract": ["CPU", "MEMORY", "Mi", "Gi"]},
            ),
        ]
        return steps
