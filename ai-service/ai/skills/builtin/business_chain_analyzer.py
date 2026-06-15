"""business_chain_analyzer.py — part 1: imports, constants, SQL builders"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from typing import Any, Dict, List, Optional

from ai.skills.base import DiagnosticSkill, SkillContext, SkillStep
from ai.skills.builtin._helpers import (
    _as_str,
    _clickhouse_query,
    _escape_sql_string,
    _generic_exec,
)
from ai.skills.registry import register_skill

logger = logging.getLogger(__name__)

# Maximum services to include in the chain (truncate by confidence)
_MAX_CHAIN_SERVICES = 15

# Default LLM analysis prompt (overridable via context["chain_analysis_prompt"])
DEFAULT_CHAIN_PROMPT = """你是一个云平台业务链分析专家。

## 业务链日志数据

### 时间窗口
{time_window}

### 锚点信息
- req-xxx: {req_xxx}
- trace_id: {trace_id}

### 服务调用链（Trace 树）
{trace_tree}

### 各服务日志摘要
{service_logs}

### 分析要求
请基于以上日志数据，给出完整的业务链分析报告，包含：

1. **业务链全貌** — 这个请求经过了哪些服务，调用顺序如何（无论是否有错误）
2. **每个环节的行为** — 每个服务在链中承担的角色，关键操作，耗时
3. **错误/异常定位** — 准确指出哪里出了问题，错误的根因是什么
4. **瓶颈分析** — 最慢的环节在哪里，是否存在等待链
5. **缺失环节** — 预期应该出现但日志中未发现的服务，以及可能的原因

请按照以上结构输出完整的分析。"""


def _build_anchor_resolve_sql(
    trace_id: str,
    os_request_id: str,
    time_window_start: str,
    time_window_end: str,
) -> str:
    """
    Build the initial anchor-resolve SQL query.

    Checks logs.logs using either trace_id or os_request_id (req-xxx) as anchor.
    Returns ALL events without level filtering, ordered by timestamp.
    """
    conditions: List[str] = []

    if trace_id:
        conditions.append(f"trace_id = '{_escape_sql_string(trace_id)}'")
    if os_request_id:
        safe_req = _escape_sql_string(os_request_id)
        conditions.append(f"message LIKE '%{safe_req}%'")

    where_clause = " OR ".join(conditions) if conditions else "1=1"

    return (
        "SELECT timestamp, service_name, level, message, trace_id "
        "FROM logs.logs "
        f"WHERE ({where_clause}) "
        f"  AND timestamp BETWEEN '{_escape_sql_string(time_window_start)}'"
        f"  AND '{_escape_sql_string(time_window_end)}' "
        "ORDER BY timestamp ASC "
        "LIMIT 2000 FORMAT PrettyCompact"
    )


def _build_events_anchor_sql(
    trace_id: str,
    os_request_id: str,
) -> str:
    """
    Build auxiliary events query — logs.events may have structured entity data.
    """
    conditions: List[str] = []

    if trace_id:
        conditions.append(f"trace_id = '{_escape_sql_string(trace_id)}'")
    if os_request_id:
        safe_req = _escape_sql_string(os_request_id)
        conditions.append(f"content LIKE '%{safe_req}%'")

    where_clause = " OR ".join(conditions) if conditions else "1=1"

    return (
        "SELECT timestamp, entity_name, event_type, level, content, "
        "       trace_id, span_id, labels "
        "FROM logs.events "
        f"WHERE ({where_clause}) "
        "ORDER BY timestamp ASC "
        "LIMIT 1000 FORMAT PrettyCompact"
    )


def _build_discovery_sql_channel1(trace_id: str, start: str, end: str) -> str:
    """Channel 1: services sharing the same trace_id."""
    return (
        "SELECT DISTINCT service_name FROM logs.logs "
        f"WHERE trace_id = '{_escape_sql_string(trace_id)}' "
        f"  AND timestamp BETWEEN '{_escape_sql_string(start)}' "
        f"  AND '{_escape_sql_string(end)}' "
        "ORDER BY service_name"
    )


def _build_discovery_sql_channel2(os_request_id: str, start: str, end: str) -> str:
    """Channel 2: services with req-xxx in message."""
    safe_req = _escape_sql_string(os_request_id)
    return (
        "SELECT DISTINCT service_name FROM logs.logs "
        f"WHERE timestamp BETWEEN '{_escape_sql_string(start)}' "
        f"  AND '{_escape_sql_string(end)}' "
        f"  AND message LIKE '%{safe_req}%' "
        "ORDER BY service_name"
    )


def _build_discovery_sql_channel3(start: str, end: str) -> str:
    """Channel 3: all services active in the time window."""
    return (
        "SELECT DISTINCT service_name FROM logs.logs "
        f"WHERE timestamp BETWEEN '{_escape_sql_string(start)}' "
        f"  AND '{_escape_sql_string(end)}' "
        "ORDER BY service_name"
    )


def _merge_service_channels(
    channel1: List[str],
    channel2: List[str],
    channel3: List[str],
    *,
    max_services: int = _MAX_CHAIN_SERVICES,
) -> Dict[str, str]:
    """
    Merge three discovery channels into a single service->anchor_type map.

    Priority: trace_id > req_xxx > time_window.
    Truncated to max_services (keep highest priority).
    """
    result: Dict[str, str] = {}

    for svc in channel1:
        result[svc] = "trace_id"
    for svc in channel2:
        if svc not in result:
            result[svc] = "req_xxx"
    for svc in channel3:
        if svc not in result:
            result[svc] = "time_window"

    # Sort: trace_id first, then req_xxx, then time_window; alpha within group
    priority = {"trace_id": 0, "req_xxx": 1, "time_window": 2}
    sorted_services = sorted(
        result.items(),
        key=lambda item: (priority.get(item[1], 99), item[0]),
    )

    return dict(sorted_services[:max_services])


def _build_supplement_sql(
    service_name: str,
    anchor_type: str,
    trace_id: str,
    os_request_id: str,
    start: str,
    end: str,
) -> str:
    """
    Build per-service supplement SQL.

    Strategy is selected by anchor_type:
      trace_id   -> WHERE trace_id=X AND service_name=Y
      req_xxx    -> WHERE message LIKE '%req-xxx%' AND service_name=Y
      time_window -> WHERE timestamp BETWEEN start AND end AND service_name=Y
    """
    safe_svc = _escape_sql_string(service_name)
    safe_start = _escape_sql_string(start)
    safe_end = _escape_sql_string(end)

    if anchor_type == "trace_id" and trace_id:
        cond = f"trace_id = '{_escape_sql_string(trace_id)}'"
    elif anchor_type == "req_xxx" and os_request_id:
        safe_req = _escape_sql_string(os_request_id)
        cond = f"message LIKE '%{safe_req}%'"
    else:
        cond = "1=1"

    return (
        "SELECT timestamp, level, message "
        "FROM logs.logs "
        f"WHERE {cond} "
        f"  AND service_name = '{safe_svc}' "
        f"  AND timestamp BETWEEN '{safe_start}' AND '{safe_end}' "
        "ORDER BY timestamp ASC "
        "LIMIT 300 FORMAT PrettyCompact"
    )


def _build_trace_tree_sql(trace_id: str) -> str:
    """Build SQL to fetch all spans for a trace_id."""
    return (
        "SELECT trace_id, span_id, parent_span_id, service_name, "
        "       operation_name, status, duration_ms, timestamp, span_kind "
        "FROM logs.traces "
        f"PREWHERE trace_id = '{_escape_sql_string(trace_id)}' "
        "ORDER BY timestamp ASC"
    )


def _build_span_tree(
    spans: List[Dict[str, Any]],
) -> List[str]:
    """
    Reconstruct a trace tree from span parent_span_id relationships.

    Returns indented text lines representing the call tree.
    """
    # Index spans by span_id
    span_index: Dict[str, Dict[str, Any]] = {}
    children: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

    for span in spans:
        sid = _as_str(span.get("span_id"))
        pid = _as_str(span.get("parent_span_id"))
        span_index[sid] = span
        children[pid].append(span)

    def _recurse(parent_id: str, depth: int) -> List[str]:
        lines: List[str] = []
        for span in children.get(parent_id, []):
            indent = "  " * depth
            s_id = _as_str(span.get("span_id"))
            svc = _as_str(span.get("service_name"))
            kind = _as_str(span.get("span_kind"))
            op = _as_str(span.get("operation_name"))
            ts = _as_str(span.get("timestamp"))
            dur = _as_str(span.get("duration_ms"))
            status = _as_str(span.get("status"))
            status_tag = f" [{status}]" if status in ("ERROR", "STATUS_CODE_ERROR") else ""
            lines.append(
                f"{indent}{svc} [{kind}] {op} "
                f"({ts}, {dur}ms){status_tag}"
            )
            lines.extend(_recurse(s_id, depth + 1))
        return lines

    return _recurse("", 0)


@register_skill
class BusinessChainAnalyzerSkill(DiagnosticSkill):
    """
    全业务链分析 — 多通道服务发现 + 调用树重建 + LLM 报告

    核心目标是解决现有跨组件日志关联只取 ERROR + top-3 服务 + 单层
    trace_id 查询导致链条不完整的问题。通过三通道服务发现（trace_id /
    req-xxx / time_window）获取完整服务列表，再逐层匹配锚点补充日志，
    最终由 LLM 生成全链路文字分析。
    """

    name = "business_chain_analyzer"
    display_name = "全业务链分析"
    description = (
        "以 trace_id → OpenStack X-Request-ID → 时间窗口的优先级，"
        "通过三通道 ClickHouse 服务发现获取完整服务列表，"
        "重建调用树并生成全链路业务分析报告。"
    )

    applicable_components: List[str] = [
        "nova", "neutron", "cinder", "keystone", "glance",
        "heat", "swift", "octavia", "designate", "proton",
        "api", "backend", "database", "message_queue",
    ]

    trigger_patterns = [
        re.compile(r"\b(业务链|全链路|完整流程|调用链|服务链|链条)\b"),
        re.compile(r"\b(flow|full.?trace|end.to.end)\b", re.IGNORECASE),
        re.compile(r"\b(business.?chain|service.?chain)\b", re.IGNORECASE),
    ]

    risk_level = "low"
    max_steps = 7  # 1 anchor + 1 discovery + up to 3 supplement + 1 trace + 1 analyze
    priority: int = 85
    mandatory_first: bool = False

    def plan_steps(self, context: SkillContext) -> List[SkillStep]:
        """
        Generate business chain analysis steps.

        See the design doc for the 5-step flow.
        Real implementation will be done in Tasks 2-4.
        """
        steps: List[SkillStep] = []

        # ── Step 1: anchor-resolve ──────────────────────────────────────────
        trace_id = _as_str(context.trace_id)
        os_request_id = _as_str(context.os_request_id or context.request_id)
        ts = _as_str(context.log_timestamp)

        # Derive time window from context
        _TW_FALLBACK_MINUTES = 5
        ew_start = _as_str(context.evidence_window_start)
        ew_end = _as_str(context.evidence_window_end)
        if not ew_start or not ew_end:
            if ts:
                # Expand single timestamp to ±5min window
                try:
                    from datetime import datetime, timedelta
                    clean_ts = ts.replace("Z", "+00:00").replace("T", " ")
                    if "T" not in ts and " " not in ts:
                        raise ValueError("unrecognized timestamp format")
                    dt = datetime.fromisoformat(clean_ts)
                    fmt = "%Y-%m-%d %H:%M:%S"
                    ew_start = (dt - timedelta(minutes=_TW_FALLBACK_MINUTES)).strftime(fmt)
                    ew_end = (dt + timedelta(minutes=_TW_FALLBACK_MINUTES)).strftime(fmt)
                except Exception:
                    # Fallback: use as-is (SQL BETWEEN will be narrow)
                    ew_start = ew_end = ts
                logger.info(
                    "No evidence_window; expanded log_timestamp=%s "
                    "to ±%dmin window: %s ~ %s",
                    ts, _TW_FALLBACK_MINUTES, ew_start, ew_end,
                )
            else:
                logger.info("No time window available; anchor query may return no results")

        anchor_sql = _build_anchor_resolve_sql(
            trace_id=trace_id,
            os_request_id=os_request_id,
            time_window_start=ew_start,
            time_window_end=ew_end,
        )

        steps.append(
            SkillStep(
                step_id="bca-anchor-resolve",
                title="锚点解析：初始日志查询",
                command_spec=_clickhouse_query(anchor_sql, timeout_s=60),
                purpose=(
                    f"以锚点 [trace_id={trace_id or 'N/A'} / "
                    f"req-xxx={os_request_id or 'N/A'}] "
                    "查询 logs.logs 获取初始事件流和时间窗口"
                ),
                depends_on=[],
                parse_hints={
                    "extract": ["timestamp", "service_name", "level", "message", "trace_id"],
                },
            )
        )

        # ── Step 2: service discovery (3 parallel queries) ─────────────────
        discovery_channel1_sql = _build_discovery_sql_channel1(
            trace_id, ew_start, ew_end
        )
        discovery_channel2_sql = _build_discovery_sql_channel2(
            os_request_id, ew_start, ew_end
        )
        discovery_channel3_sql = _build_discovery_sql_channel3(
            ew_start, ew_end
        )

        steps.append(
            SkillStep(
                step_id="bca-service-discovery-ch1",
                title="服务发现 — trace_id 通道",
                command_spec=_clickhouse_query(discovery_channel1_sql, timeout_s=30),
                purpose=f"所有与 trace_id={trace_id} 关联的服务",
                depends_on=["bca-anchor-resolve"],
                parse_hints={"extract": ["service_name"]},
            )
        )

        steps.append(
            SkillStep(
                step_id="bca-service-discovery-ch2",
                title="服务发现 — req-xxx 通道",
                command_spec=_clickhouse_query(discovery_channel2_sql, timeout_s=30),
                purpose=f"所有 message 中含 {os_request_id} 的服务",
                depends_on=["bca-anchor-resolve"],
                parse_hints={"extract": ["service_name"]},
            )
        )

        steps.append(
            SkillStep(
                step_id="bca-service-discovery-ch3",
                title="服务发现 — 时间窗口全服务",
                command_spec=_clickhouse_query(discovery_channel3_sql, timeout_s=30),
                purpose=f"时间窗口 {ew_start} ~ {ew_end} 内所有活跃服务",
                depends_on=["bca-anchor-resolve"],
                parse_hints={"extract": ["service_name"]},
            )
        )

        # ── Step 4: trace-tree-rebuild (only if trace_id available) ────────
        if trace_id:
            trace_tree_sql = _build_trace_tree_sql(trace_id)
            steps.append(
                SkillStep(
                    step_id="bca-trace-tree",
                    title="Trace 调用树重建",
                    command_spec=_clickhouse_query(trace_tree_sql, timeout_s=45),
                    purpose="从 logs.traces 的 parent_span_id 重建服务调用树",
                    depends_on=["bca-service-discovery-ch1"],
                    parse_hints={
                        "extract": [
                            "trace_id", "span_id", "parent_span_id",
                            "service_name", "operation_name", "duration_ms",
                        ],
                    },
                )
            )

        # ── Step 5: chain-analyze — LLM analysis step ─────────────────
        chain_prompt = _as_str(context.chain_analysis_prompt) or DEFAULT_CHAIN_PROMPT
        time_window_str = f"{ew_start} ~ {ew_end}"
        req_info = os_request_id or "N/A"
        trace_info = trace_id or "N/A"

        steps.append(
            SkillStep(
                step_id="bca-chain-analyze",
                title="全业务链分析报告生成",
                command_spec=_generic_exec(
                    f"echo 'Business chain analysis will be computed by the LLM "
                    f"based on Steps 1-4 results. "
                    f"Time window: {time_window_str}, "
                    f"req-xxx: {req_info}, trace_id: {trace_info}'",
                    timeout_s=5,
                ),
                purpose="基于 Steps 1-4 收集的日志数据，生成全业务链分析文字报告",
                depends_on=[
                    "bca-anchor-resolve",
                    "bca-service-discovery-ch1",
                    "bca-service-discovery-ch2",
                    "bca-service-discovery-ch3",
                ]
                + (["bca-trace-tree"] if trace_id else []),
                parse_hints={
                    "chain_analysis": True,
                    "chain_analysis_prompt": chain_prompt,
                },
            )
        )

        return steps
