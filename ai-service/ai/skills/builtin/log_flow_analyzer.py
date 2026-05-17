"""
Phase 1 skill: Log Flow Analyzer

This skill is **always injected first** (priority=100, mandatory_first=True).
It maps the full call-chain for the triggering event and establishes the
correlation anchor that every subsequent skill builds on.

Step chain:
  lfa-call-chain   → ClickHouse: reconstruct full request call-chain using
                      the best anchor (trace_id → os_request_id →
                      request_id → ±5 min time window)
  lfa-svc-topology → kubectl: snapshot service topology in the namespace
  lfa-error-volume → ClickHouse: error volume around the event time window
                      (always runs in parallel with lfa-svc-topology)

Outputs written to SkillContext (via data_flow / evidence):
  - data_flow:            list of {service, timestamp, level, message} dicts
  - correlation_anchor:   which anchor strategy was used
  - evidence_window_*:    ±5-min ISO boundaries used for time-window fallback
"""

from __future__ import annotations

import re
from typing import List

from ai.skills.base import DiagnosticSkill, SkillContext, SkillStep
from ai.skills.builtin._helpers import (
    _as_str,
    _clickhouse_query,
    _escape_sql_string,
    _generic_exec,
)
from ai.skills.registry import register_skill

# ──────────────────────────────────────────────────────────────────────────────
# Time window: ±5 minutes from the triggering log entry.
# Used when no trace_id / request_id anchor is available.
# ──────────────────────────────────────────────────────────────────────────────
_TW_MINUTES = 5


def _build_call_chain_sql(ctx: SkillContext) -> str:
    """
    Build the ClickHouse SQL that reconstructs the full call-chain.

    Priority of anchor:
      1. trace_id          – exact match, best coverage
      2. os_request_id     – OpenStack X-Request-ID (req-xxx…)
      3. request_id        – generic request UUID
      4. time_window       – ±5 min around log_timestamp (or now())

    The query always returns columns:
      timestamp, service_name, level, trace_id, request_id, message
    sorted by timestamp ASC so callers can read the flow in order.
    """
    anchor = _as_str(ctx.correlation_anchor)
    anchor_value = _as_str(ctx.correlation_anchor_value)
    ns = _as_str(ctx.namespace) or "islap"
    svc = _as_str(ctx.service_name)

    select_cols = (
        "timestamp, service_name, level, "
        "trace_id, request_id, message"
    )
    base_from = "FROM logs.events"
    order_limit = "ORDER BY timestamp ASC LIMIT 200 FORMAT PrettyCompact"

    svc_filter = (
        f" AND service_name = '{_escape_sql_string(svc)}'" if svc else ""
    )

    if anchor == "trace_id" and anchor_value:
        safe_val = _escape_sql_string(anchor_value)
        where = f"WHERE trace_id = '{safe_val}'"
    elif anchor in {"os_request_id", "request_id"} and anchor_value:
        safe_val = _escape_sql_string(anchor_value)
        # request_id may appear in either the request_id column or message body
        where = (
            f"WHERE (request_id = '{safe_val}' "
            f"OR message LIKE '%{safe_val}%')"
        )
    else:
        # time-window fallback
        if ctx.log_timestamp:
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

    return (
        f"SELECT {select_cols} "
        f"{base_from} "
        f"{where}{svc_filter} "
        f"{order_limit}"
    )


def _build_error_volume_sql(ctx: SkillContext) -> str:
    """
    Aggregate error/warn/fatal counts per service in the evidence window.
    Uses the same time boundaries as the call-chain query.
    """
    anchor = _as_str(ctx.correlation_anchor)
    ns = _as_str(ctx.namespace) or "islap"

    if ctx.log_timestamp and anchor != "time_window":
        safe_ts = _escape_sql_string(ctx.log_timestamp)
        time_cond = (
            f"timestamp BETWEEN "
            f"toDateTime('{safe_ts}') - INTERVAL {_TW_MINUTES} MINUTE "
            f"AND toDateTime('{safe_ts}') + INTERVAL {_TW_MINUTES} MINUTE"
        )
    elif ctx.evidence_window_start and ctx.evidence_window_end:
        safe_start = _escape_sql_string(ctx.evidence_window_start)
        safe_end = _escape_sql_string(ctx.evidence_window_end)
        time_cond = (
            f"timestamp BETWEEN "
            f"toDateTime('{safe_start}') AND toDateTime('{safe_end}')"
        )
    else:
        time_cond = f"timestamp >= now() - INTERVAL {_TW_MINUTES} MINUTE"

    return (
        "SELECT service_name, level, count() AS cnt "
        "FROM logs.events "
        f"WHERE {time_cond} "
        "AND level IN ('ERROR', 'WARN', 'FATAL', 'CRITICAL') "
        "GROUP BY service_name, level "
        "ORDER BY cnt DESC LIMIT 30 "
        "FORMAT PrettyCompact"
    )


@register_skill
class LogFlowAnalyzerSkill(DiagnosticSkill):
    """
    Phase 1 — 日志流路径分析（强制首步）

    重构触发事件的完整调用链，建立后续所有诊断技能共享的关联锚点。
    本技能始终作为第一组步骤注入（priority=100, mandatory_first=True）。
    """

    name = "log_flow_analyzer"
    display_name = "日志流路径分析"
    description = (
        "强制首步：依据最优关联锚点（trace_id → OpenStack X-Request-ID → "
        "request_id → ±5 分钟时间窗口）重构完整调用链，快照服务拓扑，"
        "并统计事件窗口内各服务的错误量，为后续跨组件关联诊断打好基础。"
    )

    # This skill applies universally — no component restriction
    applicable_components: List[str] = []

    # ── Trigger: any non-trivial diagnostic question ──────────────────────────
    # Wide but not totally open: requires at least a log-related keyword.
    trigger_patterns = [
        re.compile(r"\berror\b", re.IGNORECASE),
        re.compile(r"\bfail(ed|ure)?\b", re.IGNORECASE),
        re.compile(r"\btimeout\b", re.IGNORECASE),
        re.compile(r"\b(warn|warning)\b", re.IGNORECASE),
        re.compile(r"\b(critical|fatal)\b", re.IGNORECASE),
        re.compile(r"\b(exception|traceback|panic)\b", re.IGNORECASE),
        re.compile(r"\b(慢|异常|告警|故障|超时|报错|问题)\b"),
        re.compile(r"\b(diagnos|troubleshoot|check|inspect)\b", re.IGNORECASE),
        re.compile(r"\b(排查|诊断|检查|定位)\b"),
    ]

    risk_level = "low"
    max_steps = 3

    # ── Skill metadata (used by planning node for forced injection) ───────────
    priority: int = 100        # Higher = run first
    mandatory_first: bool = True  # planning.py will always inject this

    def plan_steps(self, context: SkillContext) -> List[SkillStep]:
        """
        Generate the 3 Phase-1 steps:

        1. lfa-call-chain   – ClickHouse call-chain reconstruction
        2. lfa-svc-topology – kubectl service topology snapshot
        3. lfa-error-volume – ClickHouse error volume per service
                              (parallel with topology; both depend on chain)
        """
        call_chain_sql = _build_call_chain_sql(context)
        error_volume_sql = _build_error_volume_sql(context)

        ns = _as_str(context.namespace) or "islap"

        return [
            # ── Step 1: reconstruct call chain ────────────────────────────────
            SkillStep(
                step_id="lfa-call-chain",
                title="重构完整调用链日志",
                command_spec=_clickhouse_query(call_chain_sql, timeout_s=60),
                purpose=(
                    f"使用关联锚点 [{context.correlation_anchor or 'time_window'}="
                    f"{(context.correlation_anchor_value or '±5min')!r}] "
                    "从 ClickHouse 重构跨服务调用链，明确故障传播路径"
                ),
                depends_on=[],
                parse_hints={
                    "extract": [
                        "ERROR", "WARN", "FATAL", "CRITICAL",
                        "timestamp", "service_name", "message", "trace_id",
                    ],
                    "populate_data_flow": True,   # signal to observing.py
                },
            ),

            # ── Step 2: service topology snapshot (parallel) ──────────────────
            SkillStep(
                step_id="lfa-svc-topology",
                title="快照命名空间服务拓扑",
                command_spec=_generic_exec(
                    f"kubectl get services -n {ns} -o wide --no-headers",
                    timeout_s=15,
                ),
                purpose="获取命名空间内所有 Service 的端口和 Selector 映射，辅助判断流量路径",
                depends_on=["lfa-call-chain"],
                parse_hints={
                    "extract": ["ClusterIP", "PORT", "SELECTOR", "NAME"],
                },
            ),

            # ── Step 3: error volume (parallel with topology) ─────────────────
            SkillStep(
                step_id="lfa-error-volume",
                title="统计事件窗口各服务错误量",
                command_spec=_clickhouse_query(error_volume_sql, timeout_s=45),
                purpose=(
                    "在事件时间窗口内统计各服务 ERROR/WARN/FATAL 数量，"
                    "识别异常热点服务"
                ),
                depends_on=["lfa-call-chain"],  # parallel with svc-topology
                parse_hints={
                    "extract": ["service_name", "level", "cnt", "ERROR", "WARN"],
                },
            ),
        ]
