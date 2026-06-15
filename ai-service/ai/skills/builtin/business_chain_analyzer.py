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
        ew_start = _as_str(context.evidence_window_start)
        ew_end = _as_str(context.evidence_window_end)
        if not ew_start or not ew_end:
            ew_start = ew_end = ts

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

        # Placeholder steps for Tasks 2-4
        steps.append(
            SkillStep(
                step_id="bca-service-discovery",
                title="服务发现（占位 — Task 2 实现）",
                command_spec=_clickhouse_query(
                    "SELECT 1 FORMAT PrettyCompact", timeout_s=10
                ),
                purpose="占位步骤，Task 2 将替换为三通道服务发现 SQL",
                depends_on=["bca-anchor-resolve"],
            )
        )

        return steps
