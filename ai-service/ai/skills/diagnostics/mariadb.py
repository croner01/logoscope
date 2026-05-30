"""MariaDB/MySQL diagnostic skill for database issues."""

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


@DiagnosticSkill.register
class MariaDBDiagnosticSkill(DiagnosticSkill):
    """
    MariaDB/MySQL 数据库诊断技能。

    适用于数据库连接失败、查询超时、死锁、主从同步异常、
    慢查询、索引问题等场景。
    """

    name = "mariadb_diagnostic"
    display_name = "MariaDB 数据库诊断"
    description = (
        "针对 MariaDB/MySQL 数据库故障（连接失败、查询超时、死锁、"
        "主从同步异常、慢查询、索引问题等），执行数据库状态和日志诊断。"
    )
    applicable_components = [
        "mariadb", "mysql", "database", "db", "table", "index",
        "replication", "slave", "master", "connection",
    ]
    trigger_patterns = [
        re.compile(r"mariadb", re.IGNORECASE),
        re.compile(r"mysql", re.IGNORECASE),
        re.compile(r"database.*error", re.IGNORECASE),
        re.compile(r"connection.*fail", re.IGNORECASE),
        re.compile(r"timeout.*query", re.IGNORECASE),
        re.compile(r"deadlock", re.IGNORECASE),
        re.compile(r"replication.*error", re.IGNORECASE),
        re.compile(r"slow.*query", re.IGNORECASE),
        re.compile(r"table.*lock", re.IGNORECASE),
        re.compile(r"innodb.*error", re.IGNORECASE),
        re.compile(r"access.*denied", re.IGNORECASE),
        re.compile(r"too.*many.*connections", re.IGNORECASE),
    ]
    risk_level = "medium"
    max_steps = 4

    def plan_steps(self, context: SkillContext) -> List[SkillStep]:
        ns = _as_str(context.namespace) or "islap"

        steps = [
            SkillStep(
                step_id="mariadb-describe-pod",
                title="查看 MariaDB Pod 状态",
                command_spec=_generic_exec(
                    f"kubectl get pods -n {ns} | grep -E 'mariadb|mysql' | head -10",
                    timeout_s=15,
                ),
                purpose="查看数据库 Pod 运行状态",
                parse_hints={"extract": ["STATUS", "READY", "RESTARTS"]},
            ),
            SkillStep(
                step_id="mariadb-logs",
                title="查看数据库错误日志",
                command_spec=_generic_exec(
                    f"kubectl logs -n {ns} -l app=mariadb --tail=200 --previous 2>/dev/null || "
                    f"kubectl logs -n {ns} -l app=mariadb --tail=200 || "
                    f"kubectl logs -n {ns} -l tier=mariadb --tail=200 2>/dev/null || "
                    f"echo 'MariaDB logs not accessible via kubectl'",
                    timeout_s=30,
                ),
                purpose="查看数据库错误日志",
                depends_on=["mariadb-describe-pod"],
                parse_hints={"extract": ["ERROR", "WARNING", "[ERROR]", "InnoDB"]},
            ),
            SkillStep(
                step_id="mariadb-describe",
                title="查看 Pod 详情",
                command_spec=_generic_exec(
                    f"kubectl describe pod -n {ns} | grep -A 50 'mariadb\\|mysql' | head -60 || "
                    f"kubectl describe pod -n {ns} | tail -50",
                    timeout_s=15,
                ),
                purpose="查看 Pod 事件和配置",
                depends_on=["mariadb-describe-pod"],
                parse_hints={"extract": ["Events", "Last State", "OOMKilled"]},
            ),
            SkillStep(
                step_id="mariadb-processlist",
                title="查看数据库进程列表（SQL）",
                command_spec=_clickhouse_query(
                    "SELECT 'Use kubectl exec to MariaDB pod for: SHOW PROCESSLIST;' as action FORMAT PrettyCompact",
                    namespace=ns,
                    timeout_s=20,
                ),
                purpose="提示需要执行 SHOW PROCESSLIST",
                parse_hints={"extract": ["processlist"]},
            ),
        ]
        return steps


@DiagnosticSkill.register
class ClickHouseDiagnosticSkill(DiagnosticSkill):
    """
    ClickHouse 数据库诊断技能。

    适用于 ClickHouse 慢查询、连接异常、合并阻塞等场景。
    """

    name = "clickhouse_diagnostic"
    display_name = "ClickHouse 数据库诊断"
    description = (
        "针对 ClickHouse 数据库故障（慢查询、连接超时、合并阻塞、"
        "副本同步异常、OOM 等），执行查询日志和系统指标诊断。"
    )
    applicable_components = [
        "clickhouse", "ch", "query", "merge", "part", "replica",
        "zookeeper", "zk",
    ]
    trigger_patterns = [
        re.compile(r"clickhouse", re.IGNORECASE),
        re.compile(r"slow.*query", re.IGNORECASE),
        re.compile(r"query.*timeout", re.IGNORECASE),
        re.compile(r"merge.*slow", re.IGNORECASE),
        re.compile(r"too.*many.*parts", re.IGNORECASE),
        re.compile(r"code.*184", re.IGNORECASE),
        re.compile(r"exception.*code", re.IGNORECASE),
        re.compile(r"replica.*error", re.IGNORECASE),
        re.compile(r"zookeeper.*error", re.IGNORECASE),
    ]
    risk_level = "low"
    max_steps = 4

    def plan_steps(self, context: SkillContext) -> List[SkillStep]:
        ns = _as_str(context.namespace) or "islap"

        steps = [
            SkillStep(
                step_id="ch-query-log",
                title="查询 ClickHouse query_log",
                command_spec=_clickhouse_query(
                    "SELECT event_time, query_id, exception_code, exception, query "
                    "FROM system.query_log "
                    "WHERE event_time >= now() - INTERVAL 30 MINUTE "
                    "AND type != 'QueryStart' "
                    "ORDER BY event_time DESC LIMIT 20 "
                    "FORMAT PrettyCompact",
                    namespace=ns,
                    timeout_s=45,
                ),
                purpose="查看最近的慢查询和错误查询",
                parse_hints={"extract": ["query_id", "exception", "exception_code"]},
            ),
            SkillStep(
                step_id="ch-processes",
                title="查看 ClickHouse 正在执行的查询",
                command_spec=_clickhouse_query(
                    "SELECT now() AS ts, query_id, elapsed, read_rows, read_bytes, memory_usage, query "
                    "FROM system.processes ORDER BY elapsed DESC LIMIT 10 "
                    "FORMAT PrettyCompact",
                    namespace=ns,
                    timeout_s=30,
                ),
                purpose="查看当前长耗时查询",
                parse_hints={"extract": ["elapsed", "memory_usage", "read_rows"]},
            ),
            SkillStep(
                step_id="ch-metrics",
                title="查看 ClickHouse 关键指标",
                command_spec=_clickhouse_query(
                    "SELECT metric, value FROM system.metrics "
                    "WHERE metric IN ('Query','Merge','BackgroundMergesAndMutationsPoolTask',"
                    "'DelayedInserts','MemoryTracking','TCPConnection') "
                    "ORDER BY metric FORMAT PrettyCompact",
                    namespace=ns,
                    timeout_s=20,
                ),
                purpose="查看查询、合并、内存指标",
                depends_on=["ch-processes"],
                parse_hints={"extract": ["metric", "value"]},
            ),
            SkillStep(
                step_id="ch-parts",
                title="查看 Parts 数量异常",
                command_spec=_clickhouse_query(
                    "SELECT database, table, count() AS parts, sum(rows) AS rows, "
                    "sum(bytes) AS bytes FROM system.parts "
                    "WHERE active = 1 GROUP BY database, table "
                    "ORDER BY parts DESC LIMIT 10 FORMAT PrettyCompact",
                    namespace=ns,
                    timeout_s=30,
                ),
                purpose="查看 Parts 数量，辅助判断合并阻塞",
                parse_hints={"extract": ["parts", "too_many_parts"]},
            ),
        ]
        return steps
