"""Log analysis skill for general log pattern detection and analysis."""

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


def _clickhouse_query(
    sql: str,
    *,
    namespace: str = "islap",
    database: str = "logs",
    timeout_s: int = 45,
) -> Dict[str, Any]:
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


@DiagnosticSkill.register
class LogAnalysisDiagnosticSkill(DiagnosticSkill):
    """
    通用日志分析技能。

    适用于日志结构化解析、异常关键词提取、链路追踪、上下文关联分析，
    快速定位日志中的异常点、异常链路，区分偶发故障与批量故障。
    """

    name = "log_analysis_diagnostic"
    display_name = "日志分析诊断"
    description = (
        "通用日志分析技能，具备日志结构化解析、异常关键词提取、链路追踪、"
        "上下文关联分析能力，能快速定位日志中的异常点、异常链路，"
        "区分偶发故障与批量故障。"
    )
    applicable_components = [
        "log", "message", "event", "trace", "request",
        "error", "warn", "exception", "alert",
    ]
    trigger_patterns = [
        re.compile(r"error", re.IGNORECASE),
        re.compile(r"exception", re.IGNORECASE),
        re.compile(r"fail", re.IGNORECASE),
        re.compile(r"timeout", re.IGNORECASE),
        re.compile(r"denied", re.IGNORECASE),
        re.compile(r"refused", re.IGNORECASE),
        re.compile(r"warning", re.IGNORECASE),
        re.compile(r"critical", re.IGNORECASE),
        re.compile(r"fatal", re.IGNORECASE),
        re.compile(r"panic", re.IGNORECASE),
        re.compile(r"request.*id", re.IGNORECASE),
        re.compile(r"trace.*id", re.IGNORECASE),
        re.compile(r"链路", re.IGNORECASE),
        re.compile(r"异常", re.IGNORECASE),
    ]
    risk_level = "low"
    max_steps = 4

    def plan_steps(self, context: SkillContext) -> List[SkillStep]:
        ns = _as_str(context.namespace) or "islap"
        svc = _as_str(context.service_name)
        label_flag = f"-l app={svc}" if svc else ""

        steps = [
            SkillStep(
                step_id="log-tail-current",
                title="获取当前服务日志",
                command_spec=_generic_exec(
                    f"kubectl logs -n {ns} {label_flag} --tail=200 --all-containers=true 2>/dev/null || "
                    f"kubectl logs -n {ns} {label_flag} --tail=200".strip(),
                    timeout_s=30,
                ),
                purpose="获取完整日志用于分析",
                parse_hints={
                    "extract": [
                        "ERROR", "WARN", "Exception", "timeout",
                        "request_id", "trace_id", "failed", "refused"
                    ]
                },
            ),
            SkillStep(
                step_id="log-context-before",
                title="获取前序日志上下文",
                command_spec=_generic_exec(
                    f"kubectl logs -n {ns} {label_flag} --tail=50 --timestamps=true 2>/dev/null | "
                    f"head -50 || echo 'No previous logs available'".strip(),
                    timeout_s=20,
                ),
                purpose="获取异常发生前的日志上下文",
                depends_on=["log-tail-current"],
                parse_hints={"extract": ["ERROR", "WARN", "before"]},
            ),
            SkillStep(
                step_id="log-ch-correlate",
                title="关联 ClickHouse 日志查询",
                command_spec=_clickhouse_query(
                    "SELECT timestamp, level, message, service_name, trace_id, request_id "
                    "FROM logs "
                    f"WHERE service_name = '{svc}' AND timestamp >= now() - INTERVAL 30 MINUTE "
                    "ORDER BY timestamp DESC LIMIT 50 FORMAT PrettyCompact",
                    namespace=ns,
                    timeout_s=45,
                ),
                purpose="跨组件关联日志，定位完整链路",
                parse_hints={
                    "extract": [
                        "trace_id", "request_id", "ERROR", "level"
                    ]
                },
            ),
            SkillStep(
                step_id="log-anomaly-count",
                title="统计异常频率",
                command_spec=_clickhouse_query(
                    "SELECT level, count() AS cnt "
                    "FROM logs "
                    f"WHERE service_name = '{svc}' AND timestamp >= now() - INTERVAL 30 MINUTE "
                    "GROUP BY level ORDER BY cnt DESC FORMAT PrettyCompact",
                    namespace=ns,
                    timeout_s=30,
                ),
                purpose="判断是偶发故障还是批量故障",
                depends_on=["log-ch-correlate"],
                parse_hints={"extract": ["cnt", "ERROR", "WARN"]},
            ),
        ]
        return steps


@DiagnosticSkill.register
class TraceAnalysisDiagnosticSkill(DiagnosticSkill):
    """
    链路追踪分析技能。

    适用于基于 request-id、trace-id 的跨组件链路追踪和关联分析。
    """

    name = "trace_analysis_diagnostic"
    display_name = "链路追踪分析"
    description = (
        "基于 request-id 和 trace-id 的跨组件链路追踪技能，"
        "自动聚合所有关联日志，构建完整请求链路，定位链路中断点。"
    )
    applicable_components = [
        "trace", "request", "span", "链路", "调用链",
        "distributed", "microservice",
    ]
    trigger_patterns = [
        re.compile(r"trace.*id", re.IGNORECASE),
        re.compile(r"request.*id", re.IGNORECASE),
        re.compile(r"链路", re.IGNORECASE),
        re.compile(r"调用链", re.IGNORECASE),
        re.compile(r"span.*id", re.IGNORECASE),
        re.compile(r"distributed.*trace", re.IGNORECASE),
        re.compile(r"upstream.*fail", re.IGNORECASE),
        re.compile(r"downstream.*error", re.IGNORECASE),
    ]
    risk_level = "low"
    max_steps = 3

    def plan_steps(self, context: SkillContext) -> List[SkillStep]:
        ns = _as_str(context.namespace) or "islap"

        steps = [
            SkillStep(
                step_id="trace-query-ch",
                title="查询链路相关日志",
                command_spec=_clickhouse_query(
                    "SELECT timestamp, service_name, level, message, trace_id, request_id "
                    "FROM logs "
                    "WHERE timestamp >= now() - INTERVAL 1 HOUR "
                    "AND (trace_id != '' OR request_id != '') "
                    "ORDER BY timestamp DESC LIMIT 100 FORMAT PrettyCompact",
                    namespace=ns,
                    timeout_s=60,
                ),
                purpose="获取所有带 trace_id/request_id 的日志",
                parse_hints={
                    "extract": ["trace_id", "request_id", "service_name", "ERROR"]
                },
            ),
            SkillStep(
                step_id="trace-timeline",
                title="构建链路时间线",
                command_spec=_clickhouse_query(
                    "SELECT service_name, min(timestamp) AS start_time, max(timestamp) AS end_time, "
                    "count() AS log_count, "
                    "groupArray(10)(message) AS samples "
                    "FROM logs "
                    "WHERE timestamp >= now() - INTERVAL 30 MINUTE "
                    "AND trace_id != '' "
                    "GROUP BY service_name "
                    "ORDER BY start_time FORMAT PrettyCompact",
                    namespace=ns,
                    timeout_s=45,
                ),
                purpose="按服务聚合日志，构建时间线",
                depends_on=["trace-query-ch"],
                parse_hints={
                    "extract": ["service_name", "start_time", "end_time", "log_count"]
                },
            ),
            SkillStep(
                step_id="trace-gap",
                title="识别链路断点",
                command_spec=_clickhouse_query(
                    "SELECT service_name, "
                    "countIf(level = 'ERROR') AS error_count, "
                    "count() AS total_count, "
                    "min(timestamp) AS first_seen, "
                    "max(timestamp) AS last_seen "
                    "FROM logs "
                    "WHERE timestamp >= now() - INTERVAL 30 MINUTE "
                    "GROUP BY service_name "
                    "HAVING error_count > 0 OR total_count < 5 "
                    "ORDER BY error_count DESC FORMAT PrettyCompact",
                    namespace=ns,
                    timeout_s=45,
                ),
                purpose="识别缺失日志的服务和错误节点",
                depends_on=["trace-timeline"],
                parse_hints={
                    "extract": ["service_name", "error_count", "gap"]
                },
            ),
        ]
        return steps
