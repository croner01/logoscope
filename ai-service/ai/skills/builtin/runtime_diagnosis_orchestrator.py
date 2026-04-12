"""Runtime diagnosis orchestrator skill."""

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


def _clickhouse_query(sql: str, *, database: str = "logs", timeout_s: int = 45) -> dict:
    return {
        "tool": "kubectl_clickhouse_query",
        "args": {
            "target_kind": "clickhouse_cluster",
            "target_identity": f"database:{database}",
            "query": sql,
            "timeout_s": timeout_s,
        },
        "command": f"clickhouse-client --query {sql!r}",
        "timeout_s": timeout_s,
    }


@register_skill
class RuntimeDiagnosisOrchestratorSkill(DiagnosticSkill):
    """
    Cross-layer runtime diagnosis orchestrator.

    This skill targets evidence gaps and blocked replan scenarios that require
    k8s + ClickHouse read-only evidence collection in a single pass.
    """

    name = "runtime_diagnosis_orchestrator"
    display_name = "运行时诊断编排"
    description = (
        "针对证据不足、重规划阻塞等场景，执行跨层只读排查："
        "服务日志、ClickHouse 慢查询、当前进程与系统指标。"
    )
    applicable_components = ["clickhouse", "database", "k8s", "service", "log", "query", "runtime"]
    trigger_patterns = [
        re.compile(r"clickhouse", re.IGNORECASE),
        re.compile(r"slow.*query", re.IGNORECASE),
        re.compile(r"query.*log", re.IGNORECASE),
        re.compile(r"system\.tables", re.IGNORECASE),
        re.compile(r"code[:= ]?184", re.IGNORECASE),
        re.compile(r"慢查询"),
        re.compile(r"证据不足"),
        re.compile(r"缺证据"),
        re.compile(r"replan|重规划", re.IGNORECASE),
        re.compile(r"blocked|阻断|阻塞", re.IGNORECASE),
        re.compile(r"排查|诊断"),
    ]
    risk_level = "low"
    max_steps = 4

    def plan_steps(self, context: SkillContext) -> List[SkillStep]:
        ns = _as_str(context.namespace) or "islap"
        svc = _as_str(context.service_name)
        label_flag = f"-l app={svc}" if svc else ""

        steps = [
            SkillStep(
                step_id="runtime-log-tail",
                title="拉取服务日志证据",
                command_spec=_generic_exec(
                    f"kubectl -n {ns} logs {label_flag} --since=15m --tail=200".strip(),
                    timeout_s=20,
                ),
                purpose="补齐服务侧错误日志与慢查询告警上下文",
                parse_hints={"extract": ["ERROR", "WARN", "CH_QUERY_SLOW", "timeout"]},
            ),
            SkillStep(
                step_id="runtime-ch-query-log",
                title="查询 ClickHouse query_log 慢查询样本",
                command_spec=_clickhouse_query(
                    "SELECT event_time, query_id, exception_code, exception, query "
                    "FROM system.query_log "
                    "WHERE event_time >= now() - INTERVAL 30 MINUTE "
                    "ORDER BY event_time DESC LIMIT 20 "
                    "FORMAT PrettyCompact",
                    timeout_s=45,
                ),
                purpose="补齐慢查询明细，定位异常 SQL 和 query_id",
                parse_hints={"extract": ["query_id", "exception", "exception_code"]},
                depends_on=["runtime-log-tail"],
            ),
            SkillStep(
                step_id="runtime-ch-processes",
                title="查看 ClickHouse 当前运行查询",
                command_spec=_clickhouse_query(
                    "SELECT now() AS ts, query_id, elapsed, read_rows, read_bytes, memory_usage, query "
                    "FROM system.processes ORDER BY elapsed DESC LIMIT 20 "
                    "FORMAT PrettyCompact",
                    timeout_s=30,
                ),
                purpose="确认是否存在长耗时或资源占用异常的查询",
                parse_hints={"extract": ["elapsed", "memory_usage", "read_rows"]},
                depends_on=["runtime-ch-query-log"],
            ),
            SkillStep(
                step_id="runtime-ch-metrics",
                title="查看 ClickHouse 关键运行指标",
                command_spec=_clickhouse_query(
                    "SELECT metric, value FROM system.metrics "
                    "WHERE metric IN ('Query','Merge','BackgroundMergesAndMutationsPoolTask','DelayedInserts') "
                    "ORDER BY metric "
                    "FORMAT PrettyCompact",
                    timeout_s=20,
                ),
                purpose="确认后台合并与并发压力是否异常",
                parse_hints={"extract": ["metric", "value"]},
                depends_on=["runtime-ch-processes"],
            ),
        ]
        return steps
