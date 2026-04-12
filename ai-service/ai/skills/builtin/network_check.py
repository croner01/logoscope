"""Network connectivity diagnostics skill."""

from __future__ import annotations

import re
from typing import Any, List

from ai.skills.base import DiagnosticSkill, SkillContext, SkillStep
from ai.skills.registry import register_skill


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip() if not isinstance(value, str) else value.strip()


def _generic_exec(command: str, *, timeout_s: int = 20) -> dict:
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
class NetworkConnectivitySkill(DiagnosticSkill):
    """
    网络连通性检测技能。

    针对服务间网络不通、DNS 解析失败、连接超时等场景，
    依次检测：服务健康端点 → DNS 解析 → 端口连通性。
    """

    name = "network_connectivity"
    display_name = "网络连通性检测"
    description = (
        "检测服务间网络连通性：curl 健康检查端点、kubectl exec nslookup DNS 解析、"
        "wget 端口探测。适用于连接拒绝、超时、DNS 不通等网络类故障。"
    )
    applicable_components = ["network", "service", "endpoint", "dns", "pod", "ingress"]
    trigger_patterns = [
        re.compile(r"connection refused", re.IGNORECASE),
        re.compile(r"ECONNREFUSED", re.IGNORECASE),
        re.compile(r"\btimeout\b", re.IGNORECASE),
        re.compile(r"DNS", re.IGNORECASE),
        re.compile(r"unreachable", re.IGNORECASE),
        re.compile(r"no route to host", re.IGNORECASE),
        re.compile(r"network.*fail", re.IGNORECASE),
        re.compile(r"connect.*fail", re.IGNORECASE),
    ]
    risk_level = "low"
    max_steps = 3

    def plan_steps(self, context: SkillContext) -> List[SkillStep]:
        ns = _as_str(context.namespace) or "islap"
        svc = _as_str(context.service_name)
        svc_host = f"{svc}.{ns}.svc.cluster.local" if svc else f"semantic-engine.{ns}.svc.cluster.local"

        steps = [
            SkillStep(
                step_id="net-health-check",
                title="curl 服务健康端点",
                command_spec=_generic_exec(
                    f"curl -sS --max-time 5 http://{svc_host}/health 2>&1 | head -20",
                    timeout_s=15,
                ),
                purpose="验证目标服务 HTTP 健康端点是否可达，检测 4xx/5xx 响应",
                parse_hints={"extract": ["HTTP", "status", "healthy", "error", "refused"]},
            ),
            SkillStep(
                step_id="net-dns-resolve",
                title="DNS 解析验证",
                command_spec=_generic_exec(
                    f"kubectl exec -n {ns} deploy/{svc or 'semantic-engine'} -- "
                    f"nslookup {svc_host} 2>&1 | head -15",
                    timeout_s=15,
                ),
                purpose="确认 Kubernetes DNS 解析是否正常",
                depends_on=["net-health-check"],
                parse_hints={"extract": ["Address", "NXDOMAIN", "server", "failed"]},
            ),
            SkillStep(
                step_id="net-k8s-endpoints",
                title="检查 Service Endpoints",
                command_spec=_generic_exec(
                    f"kubectl get endpoints -n {ns} {svc or ''} -o wide 2>&1".strip(),
                    timeout_s=10,
                ),
                purpose="确认 Service 是否有健康的 Endpoint，排除 Pod 未就绪问题",
                depends_on=["net-health-check"],
                parse_hints={"extract": ["ENDPOINTS", "none", "NotReady"]},
            ),
        ]
        return steps
