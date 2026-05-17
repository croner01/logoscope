"""
Phase 2 skill: Cross-Component Log Correlation

Runs immediately after LogFlowAnalyzerSkill (Phase 1).
Uses the data_flow and correlation_anchor already established in Phase 1 to
perform deep cross-service correlation and identify the precise fault point.

3-level anchor strategy (mirroring SkillContext.correlation_anchor):
  Level 1  trace_id        – join via distributed tracing column (best)
  Level 2  os_request_id   – join via OpenStack X-Request-ID header pattern
           request_id      – join via generic UUID request_id field
  Level 3  time_window     – ±3–5 min scan when no ID anchor is available

Step chain:
  ccc-anchor-query   → ClickHouse: deep query using best anchor on all
                        services identified in data_flow
  ccc-k8s-pod-logs   → kubectl: tail recent logs from the error-hot pods
                        detected in Phase 1 evidence
  ccc-error-context  → ClickHouse: capture surrounding context rows for
                        each top error message (stack-trace neighbourhood)
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

from ai.skills.base import DiagnosticSkill, SkillContext, SkillStep
from ai.skills.builtin._helpers import (
    _as_str,
    _clickhouse_query,
    _escape_sql_string,
    _generic_exec,
)
from ai.skills.registry import register_skill

# How many lines to tail from k8s pod logs
_POD_LOG_TAIL = 200
# Time window (minutes) for the fallback time-window anchor
_TW_MINUTES = 5
# Max services to pull pod logs from (avoid too many steps)
_MAX_POD_LOG_SERVICES = 3


# ──────────────────────────────────────────────────────────────────────────────
# SQL builders
# ──────────────────────────────────────────────────────────────────────────────

def _build_anchor_query_sql(ctx: SkillContext) -> str:
    """
    Build the cross-service anchor SQL.

    Columns returned:
      timestamp, service_name, level, trace_id, request_id, message

    Covers ALL services seen in data_flow so we can see exactly where
    an error first appeared and how it propagated.
    """
    anchor = _as_str(ctx.correlation_anchor)
    anchor_value = _as_str(ctx.correlation_anchor_value)

    # Build service filter from data_flow if available
    services_in_flow: List[str] = []
    for row in ctx.data_flow:
        if isinstance(row, dict):
            svc = _as_str(row.get("service_name") or row.get("service"))
            if svc and svc not in services_in_flow:
                services_in_flow.append(svc)

    # Also include related_components declared in context
    for comp in ctx.related_components:
        comp_s = _as_str(comp)
        if comp_s and comp_s not in services_in_flow:
            services_in_flow.append(comp_s)

    svc_filter = ""
    if services_in_flow:
        escaped = ", ".join(
            f"'{_escape_sql_string(s)}'" for s in services_in_flow[:10]
        )
        svc_filter = f" AND service_name IN ({escaped})"

    select_cols = (
        "timestamp, service_name, level, "
        "trace_id, request_id, message"
    )
    base_from = "FROM logs.events"
    order_limit = "ORDER BY timestamp ASC LIMIT 300 FORMAT PrettyCompact"

    if anchor == "trace_id" and anchor_value:
        safe_val = _escape_sql_string(anchor_value)
        where = f"WHERE trace_id = '{safe_val}'"
    elif anchor in {"os_request_id", "request_id"} and anchor_value:
        safe_val = _escape_sql_string(anchor_value)
        where = (
            f"WHERE (request_id = '{safe_val}' "
            f"OR message LIKE '%{safe_val}%')"
        )
    else:
        # time-window fallback — use pre-computed window boundaries if available
        if ctx.evidence_window_start and ctx.evidence_window_end:
            safe_start = _escape_sql_string(ctx.evidence_window_start)
            safe_end = _escape_sql_string(ctx.evidence_window_end)
            where = (
                f"WHERE timestamp BETWEEN "
                f"toDateTime('{safe_start}') AND toDateTime('{safe_end}')"
            )
        elif ctx.log_timestamp:
            safe_ts = _escape_sql_string(ctx.log_timestamp)
            where = (
                f"WHERE timestamp BETWEEN "
                f"toDateTime('{safe_ts}') - INTERVAL {_TW_MINUTES} MINUTE "
                f"AND toDateTime('{safe_ts}') + INTERVAL {_TW_MINUTES} MINUTE"
            )
        else:
            where = (
                f"WHERE timestamp >= now() - INTERVAL {_TW_MINUTES} MINUTE"
            )

    # Restrict to errors/warnings for cross-component correlation
    level_filter = " AND level IN ('ERROR', 'WARN', 'FATAL', 'CRITICAL')"

    return (
        f"SELECT {select_cols} "
        f"{base_from} "
        f"{where}{level_filter}{svc_filter} "
        f"{order_limit}"
    )


def _build_error_context_sql(ctx: SkillContext) -> str:
    """
    For each top error message seen in Phase 1, fetch the ±10 surrounding log
    rows to capture stack-traces and cascading errors.

    We use a sub-select to find the earliest ERROR timestamp, then return
    all rows within ±30 seconds of that point.
    """
    anchor = _as_str(ctx.correlation_anchor)
    anchor_value = _as_str(ctx.correlation_anchor_value)
    svc = _as_str(ctx.service_name)

    svc_filter = (
        f" AND service_name = '{_escape_sql_string(svc)}'" if svc else ""
    )

    if anchor == "trace_id" and anchor_value:
        safe_val = _escape_sql_string(anchor_value)
        anchor_cond = f"AND trace_id = '{safe_val}'"
    elif anchor in {"os_request_id", "request_id"} and anchor_value:
        safe_val = _escape_sql_string(anchor_value)
        anchor_cond = (
            f"AND (request_id = '{safe_val}' "
            f"OR message LIKE '%{safe_val}%')"
        )
    else:
        anchor_cond = ""

    if ctx.log_timestamp:
        safe_ts = _escape_sql_string(ctx.log_timestamp)
        time_cond = (
            f"timestamp BETWEEN "
            f"toDateTime('{safe_ts}') - INTERVAL {_TW_MINUTES} MINUTE "
            f"AND toDateTime('{safe_ts}') + INTERVAL {_TW_MINUTES} MINUTE"
        )
    else:
        time_cond = f"timestamp >= now() - INTERVAL {_TW_MINUTES} MINUTE"

    # Find first ERROR timestamp, then fetch ±30s neighbourhood
    return (
        "WITH first_error AS ("
        "  SELECT min(timestamp) AS t "
        "  FROM logs.events "
        f" WHERE {time_cond} "
        "  AND level = 'ERROR' "
        f" {anchor_cond}{svc_filter}"
        ") "
        "SELECT timestamp, service_name, level, message "
        "FROM logs.events "
        "WHERE timestamp BETWEEN "
        "  (SELECT t FROM first_error) - INTERVAL 30 SECOND "
        "  AND (SELECT t FROM first_error) + INTERVAL 30 SECOND "
        f"{svc_filter} "
        "ORDER BY timestamp ASC LIMIT 80 "
        "FORMAT PrettyCompact"
    )


def _build_pod_log_commands(ctx: SkillContext) -> List[Dict[str, Any]]:
    """
    Build kubectl log commands for the top error-hot services identified in
    Phase 1.  Returns a list of (step_id, title, command_spec) tuples as dicts.
    """
    ns = _as_str(ctx.namespace) or "islap"
    results: List[Dict[str, Any]] = []

    # Prefer services from data_flow with ERROR entries
    error_services: List[str] = []
    for row in ctx.data_flow:
        if isinstance(row, dict):
            level = _as_str(row.get("level", "")).upper()
            if level in {"ERROR", "FATAL", "CRITICAL"}:
                svc = _as_str(row.get("service_name") or row.get("service"))
                if svc and svc not in error_services:
                    error_services.append(svc)

    # Fall back to related_components if data_flow has no errors
    if not error_services:
        for comp in ctx.related_components:
            comp_s = _as_str(comp)
            if comp_s and comp_s not in error_services:
                error_services.append(comp_s)

    # If still nothing, fall back to context.service_name
    if not error_services and ctx.service_name:
        error_services = [ctx.service_name]

    for svc in error_services[:_MAX_POD_LOG_SERVICES]:
        safe_svc = re.sub(r"[^a-zA-Z0-9_\-.]", "", svc)
        results.append({
            "step_id": f"ccc-pod-logs-{safe_svc}",
            "title": f"获取 {safe_svc} Pod 最近日志",
            "command": (
                f"kubectl logs -n {ns} -l app={safe_svc} "
                f"--tail={_POD_LOG_TAIL} --prefix --ignore-errors 2>&1 | tail -n {_POD_LOG_TAIL}"
            ),
        })

    # If we couldn't derive any service, fall back to a broad recent-errors check
    if not results:
        results.append({
            "step_id": "ccc-pod-logs-default",
            "title": "获取命名空间近期错误 Pod 日志",
            "command": (
                f"kubectl get events -n {ns} "
                "--field-selector=type=Warning "
                "--sort-by=.lastTimestamp "
                "| tail -n 40"
            ),
        })

    return results


@register_skill
class CrossComponentCorrelationSkill(DiagnosticSkill):
    """
    Phase 2 — 跨组件日志关联（锚点驱动）

    承接 Phase 1（日志流分析）的输出，使用 trace_id / OpenStack X-Request-ID /
    request_id / ±5 分钟时间窗口三级锚点策略，在所有相关服务中精准定位
    故障根源点，并抓取故障现场上下文日志。
    """

    name = "cross_component_correlation"
    display_name = "跨组件日志关联"
    description = (
        "Phase 2：以 trace_id → OpenStack X-Request-ID → request_id → "
        "±5 分钟时间窗口的优先级，跨所有相关服务进行精准日志关联查询，"
        "并拉取出错 Pod 的近期日志，锁定故障首发点和传播路径。"
    )

    # Wide component coverage — this skill works across all services
    applicable_components: List[str] = [
        "nova", "neutron", "cinder", "keystone", "glance",
        "heat", "swift", "octavia", "designate",
        "api", "backend", "database", "message_queue",
    ]

    trigger_patterns = [
        # Fires on any cross-service or correlation-related question
        re.compile(r"\b(trace|trace_?id|tracing)\b", re.IGNORECASE),
        re.compile(r"\b(request.?id|x-request-id|os.request)\b", re.IGNORECASE),
        re.compile(r"\bcross.?component\b", re.IGNORECASE),
        re.compile(r"\b(correlation|correlate)\b", re.IGNORECASE),
        re.compile(r"\b(call.?chain|调用链|链路)\b", re.IGNORECASE),
        re.compile(r"\b(root.?cause|根因|根源)\b", re.IGNORECASE),
        re.compile(r"\b(propagat|cascade|级联)\b", re.IGNORECASE),
        re.compile(r"\b(upstream|downstream|上游|下游)\b", re.IGNORECASE),
        re.compile(r"\b(service.?mesh|sidecar)\b", re.IGNORECASE),
        re.compile(r"\berror\b", re.IGNORECASE),
        re.compile(r"\bfail(ed|ure)?\b", re.IGNORECASE),
        re.compile(r"\btimeout\b", re.IGNORECASE),
    ]

    risk_level = "low"
    max_steps = 5

    # Runs right after log_flow_analyzer (priority=90 vs 100)
    priority: int = 90
    mandatory_first: bool = False  # Injected by planning only after Phase 1

    def plan_steps(self, context: SkillContext) -> List[SkillStep]:
        """
        Generate cross-component correlation steps.

        The pod-log commands are dynamic — derived from data_flow populated
        by Phase 1.  The step_ids are therefore variable, so we generate them
        here and wire up depends_on accordingly.
        """
        anchor_query_sql = _build_anchor_query_sql(context)
        error_context_sql = _build_error_context_sql(context)
        pod_log_cmds = _build_pod_log_commands(context)

        steps: List[SkillStep] = []

        # ── Step 1: deep cross-service anchor query ───────────────────────────
        steps.append(
            SkillStep(
                step_id="ccc-anchor-query",
                title="跨服务锚点日志深度关联",
                command_spec=_clickhouse_query(anchor_query_sql, timeout_s=60),
                purpose=(
                    f"以锚点 [{context.correlation_anchor or 'time_window'}="
                    f"{(context.correlation_anchor_value or '±5min')!r}] "
                    "在所有相关服务中联合查询 ERROR/WARN 日志，精确定位故障首发点"
                ),
                depends_on=[],  # can start immediately; Phase 1 already done
                parse_hints={
                    "extract": [
                        "ERROR", "WARN", "FATAL",
                        "timestamp", "service_name", "message",
                        "trace_id", "request_id",
                    ],
                    "identify_fault_origin": True,  # signal to observing.py
                },
            )
        )

        # ── Step 2+: dynamic pod log tailing ─────────────────────────────────
        for pod_cmd in pod_log_cmds:
            steps.append(
                SkillStep(
                    step_id=pod_cmd["step_id"],
                    title=pod_cmd["title"],
                    command_spec=_generic_exec(pod_cmd["command"], timeout_s=20),
                    purpose=(
                        "从 K8s Pod 直接获取实时日志，补充 ClickHouse "
                        "写入延迟期间可能缺失的最新错误信息"
                    ),
                    depends_on=["ccc-anchor-query"],
                    parse_hints={
                        "extract": ["ERROR", "WARN", "Exception", "Traceback", "panic"],
                    },
                )
            )

        # ── Final step: error context neighbourhood ───────────────────────────
        pod_step_ids = [p["step_id"] for p in pod_log_cmds]
        steps.append(
            SkillStep(
                step_id="ccc-error-context",
                title="获取首个 ERROR 前后 30 秒上下文日志",
                command_spec=_clickhouse_query(error_context_sql, timeout_s=45),
                purpose=(
                    "围绕最早 ERROR 时间戳的前后 30 秒抓取完整上下文，"
                    "捕获 stack-trace 及级联错误，辅助根因确认"
                ),
                # Depends on pod logs (or anchor query if no pods) so we have
                # a clearer picture of which service errored first
                depends_on=pod_step_ids if pod_step_ids else ["ccc-anchor-query"],
                parse_hints={
                    "extract": [
                        "Exception", "Traceback", "Error", "FATAL",
                        "stack", "caused by", "at line",
                    ],
                },
            )
        )

        return steps
