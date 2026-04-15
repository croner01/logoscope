"""Observability log correlation-gap diagnostics skill."""

from __future__ import annotations

import re
from typing import Any, List, Tuple

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


def _escape_sql_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


@register_skill
class ObservabilityLogCorrelationGapSkill(DiagnosticSkill):
    """Targeted diagnostics for incomplete log correlation anchors."""

    name = "observability_log_correlation_gap"
    display_name = "日志相关性补锚"
    description = (
        "针对 `trace_id` / `request_id` / 时间窗不完整导致日志诊断难以收敛的场景，"
        "优先选择最强可用锚点并确认读路径仍能回捞证据。"
    )
    applicable_components = ["log", "query", "runtime", "trace", "request"]
    trigger_patterns = [
        re.compile(r"missing.*trace[_ -]?id", re.IGNORECASE),
        re.compile(r"request[_ -]?id.*present", re.IGNORECASE),
        re.compile(r"trace[_ -]?id.*missing", re.IGNORECASE),
        re.compile(r"time window.*(anchor|trace|request)", re.IGNORECASE),
        re.compile(r"(anchor|trace|request).*(time window)", re.IGNORECASE),
        re.compile(r"anchor.*(trace|request|log)", re.IGNORECASE),
        re.compile(r"(trace|request|log).*anchor", re.IGNORECASE),
        re.compile(r"related log.*(trace|request)", re.IGNORECASE),
        re.compile(r"补锚"),
        re.compile(r"时间窗.*(锚点|trace|request|关联)"),
        re.compile(r"(锚点|trace|request|关联).*(时间窗)"),
        re.compile(r"缺少.*trace"),
    ]
    risk_level = "low"
    max_steps = 4

    def _resolve_window(self, context: SkillContext) -> Tuple[str, str]:
        extra = context.extra if isinstance(context.extra, dict) else {}
        start = (
            _as_str(extra.get("request_flow_window_start"))
            or _as_str(extra.get("followup_related_start_time"))
            or _as_str(extra.get("evidence_window_start"))
        )
        end = (
            _as_str(extra.get("request_flow_window_end"))
            or _as_str(extra.get("followup_related_end_time"))
            or _as_str(extra.get("evidence_window_end"))
        )
        return start, end

    def _resolve_anchor(self, context: SkillContext) -> Tuple[str, str]:
        extra = context.extra if isinstance(context.extra, dict) else {}
        request_id = _as_str(extra.get("request_id")) or _as_str(extra.get("source_request_id"))
        trace_id = _as_str(extra.get("trace_id")) or _as_str(context.trace_id)
        if request_id:
            return "request_id", request_id
        if trace_id:
            return "trace_id", trace_id
        return "time_window", ""

    def plan_steps(self, context: SkillContext) -> List[SkillStep]:
        ns = _as_str(context.namespace) or "islap"
        svc = _as_str(context.service_name) or "query-service"
        start, end = self._resolve_window(context)
        anchor_kind, anchor_value = self._resolve_anchor(context)

        log_window_command = (
            f"kubectl -n {ns} logs -l app={svc} --since-time={start} --tail=200"
            if start
            else f"kubectl -n {ns} logs -l app={svc} --since=15m --tail=200"
        )

        where_clauses = []
        if start:
            where_clauses.append(f"timestamp >= parseDateTimeBestEffort('{_escape_sql_string(start)}')")
        if end:
            where_clauses.append(f"timestamp <= parseDateTimeBestEffort('{_escape_sql_string(end)}')")
        if anchor_kind == "request_id" and anchor_value:
            where_clauses.append(f"request_id = '{_escape_sql_string(anchor_value)}'")
        elif anchor_kind == "trace_id" and anchor_value:
            where_clauses.append(f"trace_id = '{_escape_sql_string(anchor_value)}'")

        where_sql = " AND ".join(where_clauses) if where_clauses else "timestamp >= now() - INTERVAL 15 MINUTE"
        anchor_sql = (
            "SELECT timestamp, service_name, level, request_id, trace_id, message "
            "FROM logs.logs "
            f"WHERE {where_sql} "
            "ORDER BY timestamp DESC LIMIT 50 "
            "FORMAT PrettyCompact"
        )

        query_service_command = (
            f"kubectl -n {ns} logs -l app=query-service --since-time={start} --tail=200"
            if start
            else f"kubectl -n {ns} logs -l app=query-service --since=15m --tail=200"
        )

        anchor_title = "使用时间窗确认候选日志锚点"
        if anchor_kind == "request_id":
            anchor_title = "使用 request_id 确认候选日志锚点"
        elif anchor_kind == "trace_id":
            anchor_title = "使用 trace_id 确认候选日志锚点"

        return [
            SkillStep(
                step_id="corr-window-log-tail",
                title="确认显式时间窗内的服务日志",
                command_spec=_generic_exec(log_window_command, timeout_s=20),
                purpose="先使用显式时间窗缩小日志范围，避免无锚点时直接扩大搜索面",
                parse_hints={"extract": ["request_id", "trace_id", "timestamp", "anchor"]},
            ),
            SkillStep(
                step_id="corr-anchor-query",
                title=anchor_title,
                command_spec=_clickhouse_query(anchor_sql, namespace=ns, timeout_s=45),
                purpose="选择最强可用锚点继续回捞日志证据，而不是因为缺少另一种锚点就停住",
                depends_on=["corr-window-log-tail"],
                parse_hints={"extract": ["request_id", "trace_id", "timestamp", "service_name"]},
            ),
            SkillStep(
                step_id="corr-query-service-confirm",
                title="确认 query-service 读路径是否保留锚点字段",
                command_spec=_generic_exec(query_service_command, timeout_s=20),
                purpose="确认 query-service 读面是否仍能观察到 request_id / trace_id / 时间窗相关证据",
                depends_on=["corr-anchor-query"],
                parse_hints={"extract": ["request_id", "trace", "anchor_time", "time_window"]},
            ),
        ]
