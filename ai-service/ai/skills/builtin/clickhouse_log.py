"""ClickHouse log deep query skill."""

from __future__ import annotations

import re
from typing import List

from ai.skills.base import DiagnosticSkill, SkillContext, SkillStep
from ai.skills.builtin._helpers import _as_str, _clickhouse_query
from ai.skills.registry import register_skill


@register_skill
class ClickHouseLogQuerySkill(DiagnosticSkill):
    """
    ClickHouse 日志深度查询技能。

    针对错误频率分析、时间窗口内异常详情、慢查询模式聚合等场景，
    依次执行：错误计数 → 时间窗口明细 → 错误模式聚合。
    """

    name = "clickhouse_log_query"
    display_name = "ClickHouse 日志深查"
    description = (
        "通过 ClickHouse SQL 对日志进行深度分析：统计错误频率、"
        "查询指定时间窗口内的异常明细、聚合错误模式，适用于大批量"
        "日志中定位高频错误和慢操作。"
    )
    applicable_components = ["clickhouse", "database", "log", "query"]
    trigger_patterns = [
        # FIX: 收紧 trigger_patterns，避免 \berror\b / \btimeout\b 过于宽泛
        re.compile(r"\bclickhouse\b", re.IGNORECASE),
        re.compile(r"clickhouse.*error", re.IGNORECASE),
        re.compile(r"clickhouse.*timeout", re.IGNORECASE),
        re.compile(r"slow.*query", re.IGNORECASE),
        re.compile(r"query.*fail", re.IGNORECASE),
        re.compile(r"db.*error", re.IGNORECASE),
        re.compile(r"sql.*exception", re.IGNORECASE),
    ]
    risk_level = "low"
    max_steps = 3

    def plan_steps(self, context: SkillContext) -> List[SkillStep]:
        ns = _as_str(context.namespace) or "islap"
        svc = _as_str(context.service_name)
        svc_filter = f"AND service_name = '{svc}'" if svc else ""

        # Step 1: error frequency in last 1h
        count_sql = (
            "SELECT level, count() AS cnt "
            "FROM logs.events "
            f"WHERE timestamp >= now() - INTERVAL 1 HOUR {svc_filter} "
            "GROUP BY level ORDER BY cnt DESC LIMIT 10 "
            "FORMAT PrettyCompact"
        )

        # Step 2: recent error details
        detail_sql = (
            "SELECT timestamp, service_name, level, message "
            "FROM logs.events "
            f"WHERE timestamp >= now() - INTERVAL 30 MINUTE "
            f"AND level IN ('ERROR','WARN','FATAL') {svc_filter} "
            "ORDER BY timestamp DESC LIMIT 30 "
            "FORMAT PrettyCompact"
        )

        # Step 3: error pattern grouping
        pattern_sql = (
            "SELECT "
            "  replaceRegexpAll(message, '\\\\d+', '?') AS msg_pattern, "
            "  count() AS cnt "
            "FROM logs.events "
            f"WHERE timestamp >= now() - INTERVAL 1 HOUR "
            f"AND level = 'ERROR' {svc_filter} "
            "GROUP BY msg_pattern ORDER BY cnt DESC LIMIT 10 "
            "FORMAT PrettyCompact"
        )

        return [
            SkillStep(
                step_id="ch-error-count",
                title="统计近 1 小时各级别日志数量",
                command_spec=_clickhouse_query(count_sql),
                purpose="快速定位错误日志级别分布，判断故障严重程度",
                parse_hints={"extract": ["ERROR", "WARN", "count"]},
            ),
            SkillStep(
                step_id="ch-error-detail",
                title="查询近 30 分钟错误日志明细",
                command_spec=_clickhouse_query(detail_sql),
                purpose="获取具体错误消息和发生时序，定位根因时间点",
                depends_on=["ch-error-count"],
                parse_hints={"extract": ["message", "timestamp", "level"]},
            ),
            SkillStep(
                step_id="ch-error-pattern",
                title="聚合错误消息模式",
                command_spec=_clickhouse_query(pattern_sql),
                purpose="将相似错误归类，找出最高频错误模式",
                # pattern 与 detail 都依赖 count，但彼此无依赖，可并行
                depends_on=["ch-error-count"],
                parse_hints={"extract": ["pattern", "count", "msg_pattern"]},
            ),
        ]
