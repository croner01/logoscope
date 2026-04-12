"""K8s Pod diagnostics skill."""

from __future__ import annotations

import re
from typing import Any, List

from ai.skills.base import DiagnosticSkill, SkillContext, SkillStep
from ai.skills.registry import register_skill


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip() if not isinstance(value, str) else value.strip()


def _generic_exec(command: str, *, timeout_s: int = 30) -> dict:
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
    max_steps = 4

    def plan_steps(self, context: SkillContext) -> List[SkillStep]:
        ns = _as_str(context.namespace) or "islap"
        svc = _as_str(context.service_name)
        # Use service name as label selector if available
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
                parse_hints={
                    "extract": ["Restart Count", "Last State", "Reason", "OOMKilled", "Events"]
                },
            ),
            SkillStep(
                step_id="k8s-logs-tail",
                title="获取容器最近日志",
                command_spec=_generic_exec(
                    f"kubectl logs {label_flag} -n {ns} --tail=100 --previous 2>/dev/null || "
                    f"kubectl logs {label_flag} -n {ns} --tail=100".strip(),
                    timeout_s=25,
                ),
                purpose="定位崩溃时刻的错误栈和关键异常",
                depends_on=["k8s-describe-pod"],
                parse_hints={"extract": ["ERROR", "Exception", "FATAL", "panic", "signal"]},
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
                parse_hints={"extract": ["Warning", "Killing", "BackOff", "FailedScheduling"]},
            ),
            SkillStep(
                step_id="k8s-top-pod",
                title="查看 Pod 资源用量",
                command_spec=_generic_exec(
                    f"kubectl top pod -n {ns} --no-headers 2>/dev/null | sort -k3 -hr | head -20",
                    timeout_s=15,
                ),
                purpose="确认内存/CPU 是否已达到 limit，辅助 OOM 判断",
                depends_on=["k8s-describe-pod"],
                parse_hints={"extract": ["CPU", "MEMORY", "Mi", "Gi"]},
            ),
        ]
        return steps
