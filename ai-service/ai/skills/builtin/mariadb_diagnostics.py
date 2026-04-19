"""MariaDB diagnostics skill."""

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
class MariaDBDiagnosticsSkill(DiagnosticSkill):
    """MariaDB 日志与慢查询排障技能。"""

    name = "mariadb_diagnostics"
    display_name = "MariaDB 诊断"
    description = "针对连接失败、慢查询、锁等待、复制异常等问题，采集错误日志和运行状态。"
    applicable_components = ["mariadb", "mysql", "database", "sql"]
    trigger_patterns = [
        re.compile(r"mariadb|mysql", re.IGNORECASE),
        re.compile(r"slow query", re.IGNORECASE),
        re.compile(r"deadlock", re.IGNORECASE),
        re.compile(r"lock wait timeout", re.IGNORECASE),
        re.compile(r"too many connections", re.IGNORECASE),
        re.compile(r"replication|slave|binlog", re.IGNORECASE),
    ]
    risk_level = "low"
    max_steps = 4

    def plan_steps(self, context: SkillContext) -> List[SkillStep]:
        ns = _as_str(context.namespace) or "islap"
        db_app = _as_str(context.service_name) or "mariadb"
        return [
            SkillStep(
                step_id="mariadb-error-log",
                title="拉取 MariaDB 错误日志",
                command_spec=_generic_exec(
                    f"kubectl -n {ns} logs -l app={db_app} --since=20m --tail=200",
                    timeout_s=20,
                ),
                purpose="定位连接失败、崩溃、复制失败等错误",
            ),
            SkillStep(
                step_id="mariadb-processlist",
                title="查看活跃会话与阻塞",
                command_spec=_generic_exec(
                    f"kubectl -n {ns} exec deploy/{db_app} -- mysql -e \"SHOW FULL PROCESSLIST;\"",
                    timeout_s=20,
                ),
                purpose="确认是否存在长事务、锁等待、连接堆积",
                depends_on=["mariadb-error-log"],
            ),
            SkillStep(
                step_id="mariadb-engine-status",
                title="查看 InnoDB 锁等待状态",
                command_spec=_generic_exec(
                    f"kubectl -n {ns} exec deploy/{db_app} -- mysql -e \"SHOW ENGINE INNODB STATUS\\G\"",
                    timeout_s=25,
                ),
                purpose="定位死锁、事务冲突和行锁竞争",
                depends_on=["mariadb-processlist"],
            ),
            SkillStep(
                step_id="mariadb-replica-status",
                title="查看主从复制状态",
                command_spec=_generic_exec(
                    f"kubectl -n {ns} exec deploy/{db_app} -- mysql -e \"SHOW SLAVE STATUS\\G\"",
                    timeout_s=20,
                ),
                purpose="确认复制延迟或中断原因",
                depends_on=["mariadb-engine-status"],
            ),
        ]
