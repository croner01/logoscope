"""Linux system diagnostics skill."""

from __future__ import annotations

import re
from typing import List

from ai.skills.base import DiagnosticSkill, SkillContext, SkillStep
from ai.skills.builtin._helpers import _as_str, _generic_exec
from ai.skills.registry import register_skill


@register_skill
class LinuxSystemDiagnosticsSkill(DiagnosticSkill):
    """Linux 系统层故障排查技能。"""

    name = "linux_system_diagnostics"
    display_name = "Linux 系统诊断"
    description = "针对进程异常、端口占用、磁盘满、权限问题，执行系统只读排查命令。"
    applicable_components = ["linux", "os", "system", "host", "node"]
    trigger_patterns = [
        re.compile(r"/var/log/(messages|syslog)", re.IGNORECASE),
        re.compile(r"disk.*(full|space)", re.IGNORECASE),
        re.compile(r"permission denied", re.IGNORECASE),
        re.compile(r"address already in use|port.*in use", re.IGNORECASE),
        re.compile(r"process.*(killed|crash|exit)", re.IGNORECASE),
    ]
    risk_level = "low"
    max_steps = 4

    def plan_steps(self, context: SkillContext) -> List[SkillStep]:
        process_hint = _as_str(context.service_name) or ""
        process_grep = f" | grep -i {process_hint}" if process_hint else ""
        return [
            SkillStep(
                step_id="linux-syslog-tail",
                title="查看系统日志",
                command_spec=_generic_exec(
                    "cat /var/log/messages 2>/dev/null | tail -n 120 || "
                    "cat /var/log/syslog 2>/dev/null | tail -n 120",
                    timeout_s=15,
                ),
                purpose="快速确认内核、systemd、权限相关错误",
            ),
            SkillStep(
                step_id="linux-process-check",
                title="查看异常进程",
                command_spec=_generic_exec(
                    f"ps -ef{process_grep}",
                    timeout_s=10,
                ),
                purpose="确认关键进程是否存在异常退出/重复拉起",
                depends_on=["linux-syslog-tail"],
            ),
            SkillStep(
                step_id="linux-port-check",
                title="查看端口监听与占用",
                command_spec=_generic_exec(
                    "ss -lntp | head -n 80",
                    timeout_s=10,
                ),
                purpose="确认服务端口冲突或未监听",
                # port-check 不依赖 process-check，可并行
                depends_on=["linux-syslog-tail"],
            ),
            SkillStep(
                step_id="linux-disk-check",
                title="查看磁盘容量",
                command_spec=_generic_exec(
                    "df -h",
                    timeout_s=10,
                ),
                purpose="确认磁盘是否已满导致服务异常",
                depends_on=["linux-syslog-tail"],
            ),
        ]
