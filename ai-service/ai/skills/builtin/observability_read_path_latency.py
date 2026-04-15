"""Observability read-path latency diagnostics skill."""

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


def _clickhouse_query(
    sql: str,
    *,
    namespace: str = "islap",
    database: str = "logs",
    timeout_s: int = 45,
) -> dict:
    return {
        "tool": "kubectl_clickhouse_query",
        "args": {
            "namespace": namespace,
            "target_kind": "clickhouse_cluster",
            "target_identity": f"database:{database}",
            "query": sql,
            "timeout_s": timeout_s,
        },
        "command": f"clickhouse-client --query {sql!r}",
        "timeout_s": timeout_s,
    }


@register_skill
class ObservabilityReadPathLatencySkill(DiagnosticSkill):
    """Targeted diagnostics for query/read-path latency incidents."""

    name = "observability_read_path_latency"
    display_name = "读路径延迟排查"
    description = (
        "针对 query-service / ClickHouse 读路径慢查询、超时、预览或聚合接口变慢等场景，"
        "优先采集服务日志、query_log、当前运行查询和关键运行指标。"
    )
    applicable_components = ["query", "clickhouse", "database", "read", "api"]
    trigger_patterns = [
        re.compile(r"slow.*query", re.IGNORECASE),
        re.compile(r"query.*timeout", re.IGNORECASE),
        re.compile(r"read.*timeout", re.IGNORECASE),
        re.compile(r"preview.*slow", re.IGNORECASE),
        re.compile(r"aggregation.*slow", re.IGNORECASE),
        re.compile(r"large.*read", re.IGNORECASE),
        re.compile(r"code[:= ]?241", re.IGNORECASE),
        re.compile(r"慢查询"),
        re.compile(r"超时"),
        re.compile(r"读路径"),
    ]
    risk_level = "low"
    max_steps = 4

    def plan_steps(self, context: SkillContext) -> List[SkillStep]:
        ns = _as_str(context.namespace) or "islap"
        svc = _as_str(context.service_name) or "query-service"

        query_log_sql = (
            "SELECT event_time, query_id, query_duration_ms, read_rows, read_bytes, memory_usage, exception_code, query "
            "FROM system.query_log "
            "WHERE event_time >= now() - INTERVAL 30 MINUTE "
            "ORDER BY event_time DESC LIMIT 20 "
            "FORMAT PrettyCompact"
        )
        processes_sql = (
            "SELECT now() AS ts, query_id, elapsed, read_rows, read_bytes, memory_usage, query "
            "FROM system.processes ORDER BY elapsed DESC LIMIT 20 "
            "FORMAT PrettyCompact"
        )
        metrics_sql = (
            "SELECT metric, value FROM system.metrics "
            "WHERE metric IN ('Query','Merge','BackgroundMergesAndMutationsPoolTask','DelayedInserts') "
            "ORDER BY metric FORMAT PrettyCompact"
        )

        return [
            SkillStep(
                step_id="read-latency-log-tail",
                title="拉取 query-service 读路径日志",
                command_spec=_generic_exec(
                    f"kubectl -n {ns} logs -l app={svc} --since=15m --tail=200",
                    timeout_s=20,
                ),
                purpose="确认超时、慢查询和预览/聚合接口的服务侧症状",
                parse_hints={"extract": ["timeout", "slow", "query", "preview", "aggregation"]},
            ),
            SkillStep(
                step_id="read-latency-query-log",
                title="查询 ClickHouse query_log 慢查询样本",
                command_spec=_clickhouse_query(query_log_sql, namespace=ns, timeout_s=45),
                purpose="确认慢查询样本、耗时、读放大量和异常 SQL",
                depends_on=["read-latency-log-tail"],
                parse_hints={"extract": ["query_id", "query_duration_ms", "read_rows", "read_bytes"]},
            ),
            SkillStep(
                step_id="read-latency-processes",
                title="查看 ClickHouse 当前运行查询",
                command_spec=_clickhouse_query(processes_sql, namespace=ns, timeout_s=30),
                purpose="确认当前是否存在长耗时、大内存或大读放大的运行查询",
                depends_on=["read-latency-query-log"],
                parse_hints={"extract": ["elapsed", "memory_usage", "read_rows", "read_bytes"]},
            ),
            SkillStep(
                step_id="read-latency-metrics",
                title="查看 ClickHouse 关键运行指标",
                command_spec=_clickhouse_query(metrics_sql, namespace=ns, timeout_s=20),
                purpose="确认查询并发、后台 merge 和延迟写入等运行压力指标",
                depends_on=["read-latency-processes"],
                parse_hints={"extract": ["metric", "value", "Merge", "DelayedInserts"]},
            ),
        ]
