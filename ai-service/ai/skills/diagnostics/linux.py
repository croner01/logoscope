"""Linux system diagnostic skill for host-level issues."""

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
class LinuxDiagnosticSkill(DiagnosticSkill):
    """
    Linux 系统诊断技能。

    适用于系统层面故障（进程异常、端口占用、磁盘满、权限问题、
    内存不足、CPU 负载高等）诊断。
    """

    name = "linux_diagnostic"
    display_name = "Linux 系统诊断"
    description = (
        "针对 Linux 系统故障（进程异常、端口占用、磁盘满、权限问题、"
        "内存不足、CPU 负载高、内核错误等），执行系统状态和日志诊断。"
    )
    applicable_components = [
        "linux", "host", "node", "server", "disk", "memory",
        "cpu", "process", "port", "systemd", "kernel",
    ]
    trigger_patterns = [
        re.compile(r"disk.*full", re.IGNORECASE),
        re.compile(r"no.*space.*left", re.IGNORECASE),
        re.compile(r"out.*of.*memory", re.IGNORECASE),
        re.compile(r"oom.*kill", re.IGNORECASE),
        re.compile(r"high.*cpu", re.IGNORECASE),
        re.compile(r"load.*average", re.IGNORECASE),
        re.compile(r"port.*already.*in.*use", re.IGNORECASE),
        re.compile(r"bind.*fail", re.IGNORECASE),
        re.compile(r"permission.*denied", re.IGNORECASE),
        re.compile(r"cannot.*open.*file", re.IGNORECASE),
        re.compile(r"systemd.*fail", re.IGNORECASE),
        re.compile(r"kernel.*panic", re.IGNORECASE),
        re.compile(r"segfault", re.IGNORECASE),
        re.compile(r"core.*dump", re.IGNORECASE),
        re.compile(r"file.*descriptor", re.IGNORECASE),
        re.compile(r"too.*many.*open.*files", re.IGNORECASE),
    ]
    risk_level = "low"
    max_steps = 4

    def plan_steps(self, context: SkillContext) -> List[SkillStep]:
        steps = [
            SkillStep(
                step_id="linux-disk-usage",
                title="查看磁盘使用情况",
                command_spec=_generic_exec(
                    "df -h | sort -k5 -hr | head -10",
                    timeout_s=15,
                ),
                purpose="检查磁盘空间是否已满",
                parse_hints={"extract": ["Use%", "Avail", "Filesystem"]},
            ),
            SkillStep(
                step_id="linux-memory",
                title="查看内存使用情况",
                command_spec=_generic_exec(
                    "free -h && cat /proc/meminfo | head -10",
                    timeout_s=10,
                ),
                purpose="检查内存使用率和可用内存",
                parse_hints={"extract": ["Mem:", "Swap:", "available", "MemAvailable"]},
            ),
            SkillStep(
                step_id="linux-cpu-load",
                title="查看 CPU 负载",
                command_spec=_generic_exec(
                    "top -bn1 | head -20 && uptime",
                    timeout_s=15,
                ),
                purpose="检查 CPU 负载和运行进程",
                parse_hints={"extract": ["load average", "Cpu(s)", "%Cpu"]},
            ),
            SkillStep(
                step_id="linux-top-processes",
                title="查看高占用进程",
                command_spec=_generic_exec(
                    "ps aux --sort=-%mem | head -15 && ps aux --sort=-%cpu | head -15",
                    timeout_s=15,
                ),
                purpose="找出内存和 CPU 占用最高的进程",
                parse_hints={"extract": ["%MEM", "%CPU", "COMMAND"]},
            ),
        ]
        return steps


@DiagnosticSkill.register
class LinuxNetworkDiagnosticSkill(DiagnosticSkill):
    """
    Linux 网络诊断技能。

    适用于网络连接问题（端口监听异常、网络不通、DNS 解析失败等）诊断。
    """

    name = "linux_network_diagnostic"
    display_name = "Linux 网络诊断"
    description = (
        "针对 Linux 网络问题（端口监听异常、网络连接失败、DNS 解析失败、"
        "路由异常、网络丢包等），执行网络状态诊断。"
    )
    applicable_components = ["network", "socket", "port", "firewall", "iptables", "dns"]
    trigger_patterns = [
        re.compile(r"connection.*refused", re.IGNORECASE),
        re.compile(r"connection.*timeout", re.IGNORECASE),
        re.compile(r"connection.*reset", re.IGNORECASE),
        re.compile(r"port.*listen", re.IGNORECASE),
        re.compile(r"socket.*error", re.IGNORECASE),
        re.compile(r"network.*unreachable", re.IGNORECASE),
        re.compile(r"no.*route.*to.*host", re.IGNORECASE),
        re.compile(r"dns.*fail", re.IGNORECASE),
        re.compile(r"iptables", re.IGNORECASE),
        re.compile(r"firewall.*block", re.IGNORECASE),
    ]
    risk_level = "low"
    max_steps = 3

    def plan_steps(self, context: SkillContext) -> List[SkillStep]:
        steps = [
            SkillStep(
                step_id="linux-net-stat",
                title="查看端口监听状态",
                command_spec=_generic_exec(
                    "ss -tulnp | head -30 && netstat -tulnp 2>/dev/null | head -20",
                    timeout_s=15,
                ),
                purpose="检查关键端口是否正常监听",
                parse_hints={"extract": ["LISTEN", "State", "Local Address"]},
            ),
            SkillStep(
                step_id="linux-net-connections",
                title="查看网络连接状态",
                command_spec=_generic_exec(
                    "ss -s && netstat -an | awk '/^tcp/ {print $6}' | sort | uniq -c",
                    timeout_s=15,
                ),
                purpose="统计连接状态分布",
                parse_hints={"extract": ["ESTABLISHED", "TIME_WAIT", "CLOSE_WAIT"]},
            ),
            SkillStep(
                step_id="linux-dns",
                title="查看 DNS 解析",
                command_spec=_generic_exec(
                    "cat /etc/resolv.conf && nslookup localhost 2>&1 | head -10",
                    timeout_s=15,
                ),
                purpose="检查 DNS 配置和解析状态",
                parse_hints={"extract": ["nameserver", "Server:", "Address:"]},
            ),
        ]
        return steps
