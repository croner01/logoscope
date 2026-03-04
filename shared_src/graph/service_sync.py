"""
Neo4j 服务节点同步模块

从 ClickHouse 多张表同步服务节点到 Neo4j，提升覆盖率：
- logs
- traces
- events
- metrics
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional, Set, Tuple

# 添加父目录到路径
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if TYPE_CHECKING:
    from storage.adapter import StorageAdapter

logger = logging.getLogger(__name__)

CLICKHOUSE_DATABASE = "logs"
SERVICE_SOURCE_TABLES: Tuple[str, ...] = ("logs", "traces", "events", "metrics")
SERVICE_NAME_COLUMN_CANDIDATES: Dict[str, Tuple[str, ...]] = {
    "logs": ("service_name", "entity_name"),
    "traces": ("service_name",),
    "events": ("service_name", "entity_name"),
    "metrics": ("service_name",),
}
TIMESTAMP_COLUMN_CANDIDATES: Dict[str, Tuple[str, ...]] = {
    "logs": ("timestamp", "observed_timestamp"),
    "traces": ("timestamp", "start_time"),
    "events": ("timestamp",),
    "metrics": ("timestamp",),
}
INVALID_SERVICE_NAMES = {"", "unknown", "none", "null", "n/a"}
BATCH_SIZE = 500


def _normalize_service_name(raw_service_name: Any) -> str:
    """标准化服务名并过滤无效值。"""
    service_name = str(raw_service_name or "").strip()
    if not service_name:
        return ""
    if service_name.lower() in INVALID_SERVICE_NAMES:
        return ""
    return service_name


def _parse_timestamp(raw_timestamp: Any) -> Optional[datetime]:
    """将 ClickHouse 返回的时间戳转换为带时区的 UTC datetime。"""
    if raw_timestamp is None:
        return None

    if isinstance(raw_timestamp, datetime):
        if raw_timestamp.tzinfo is None:
            return raw_timestamp.replace(tzinfo=timezone.utc)
        return raw_timestamp.astimezone(timezone.utc)

    value = str(raw_timestamp).strip()
    if not value:
        return None

    if value.endswith("Z"):
        value = f"{value[:-1]}+00:00"

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _pick_first_existing(existing_columns: Set[str], candidates: Iterable[str]) -> Optional[str]:
    """从候选列中挑选第一个存在的列名。"""
    for candidate in candidates:
        if candidate in existing_columns:
            return candidate
    return None


def _fetch_clickhouse_table_columns(storage: StorageAdapter) -> Dict[str, Set[str]]:
    """读取 logs/traces/events/metrics 的列信息，兼容异构表结构。"""
    if not storage.ch_client:
        return {}

    tables_sql = ", ".join(f"'{table}'" for table in SERVICE_SOURCE_TABLES)
    query = f"""
        SELECT table, name
        FROM system.columns
        WHERE database = '{CLICKHOUSE_DATABASE}'
          AND table IN ({tables_sql})
    """
    rows = storage.ch_client.execute(query)

    columns_by_table: Dict[str, Set[str]] = {}
    for table, column_name in rows:
        columns_by_table.setdefault(str(table), set()).add(str(column_name))
    return columns_by_table


def _collect_clickhouse_service_inventory(storage: StorageAdapter) -> Dict[str, Dict[str, Any]]:
    """
    聚合 ClickHouse 多源服务清单。

    Returns:
        {
            "frontend": {
                "service_name": "frontend",
                "total_count": 1200,
                "logs_count": 1100,
                "traces_count": 80,
                "events_count": 15,
                "metrics_count": 5,
                "data_sources": {"logs", "traces"},
                "last_seen": datetime(...)
            }
        }
    """
    if not storage.ch_client:
        return {}

    columns_by_table = _fetch_clickhouse_table_columns(storage)
    inventory: Dict[str, Dict[str, Any]] = {}

    for table in SERVICE_SOURCE_TABLES:
        table_columns = columns_by_table.get(table, set())
        service_column = _pick_first_existing(
            table_columns,
            SERVICE_NAME_COLUMN_CANDIDATES.get(table, ()),
        )
        if not service_column:
            logger.info("Skip service sync source logs.%s: no service column", table)
            continue

        timestamp_column = _pick_first_existing(
            table_columns,
            TIMESTAMP_COLUMN_CANDIDATES.get(table, ()),
        )
        timestamp_expr = f"max({timestamp_column}) AS last_seen" if timestamp_column else "NULL AS last_seen"

        query = f"""
            SELECT
                {service_column} AS service_name,
                count() AS source_count,
                {timestamp_expr}
            FROM {CLICKHOUSE_DATABASE}.{table}
            WHERE {service_column} IS NOT NULL
              AND toString({service_column}) != ''
            GROUP BY service_name
        """

        try:
            rows = storage.ch_client.execute(query)
        except Exception as exc:
            logger.warning("Skip source logs.%s due to query failure: %s", table, exc)
            continue

        for service_name_raw, source_count, last_seen_raw in rows:
            service_name = _normalize_service_name(service_name_raw)
            if not service_name:
                continue

            current_last_seen = _parse_timestamp(last_seen_raw)
            source_count_int = int(source_count or 0)

            service_entry = inventory.setdefault(
                service_name,
                {
                    "service_name": service_name,
                    "total_count": 0,
                    "logs_count": 0,
                    "traces_count": 0,
                    "events_count": 0,
                    "metrics_count": 0,
                    "data_sources": set(),
                    "last_seen": None,
                },
            )
            source_count_key = f"{table}_count"
            service_entry[source_count_key] += source_count_int
            service_entry["total_count"] += source_count_int
            service_entry["data_sources"].add(table)

            existing_last_seen = service_entry.get("last_seen")
            if current_last_seen and (not existing_last_seen or current_last_seen > existing_last_seen):
                service_entry["last_seen"] = current_last_seen

    return inventory


def _fetch_neo4j_service_ids(storage: StorageAdapter) -> Set[str]:
    """读取 Neo4j 现有 Service 节点 ID 集合。"""
    if not storage.neo4j_driver:
        return set()

    with storage.neo4j_driver.session() as session:
        rows = session.run(
            """
            MATCH (s:Service)
            RETURN s.id AS service_id
            """
        )
        return {str(row["service_id"]) for row in rows if row.get("service_id")}


def _chunked(items: List[Dict[str, Any]], chunk_size: int) -> Iterable[List[Dict[str, Any]]]:
    """按固定大小切分列表。"""
    for index in range(0, len(items), chunk_size):
        yield items[index:index + chunk_size]


def _build_coverage_stats(clickhouse_service_ids: Set[str], neo4j_service_ids: Set[str]) -> Dict[str, Any]:
    """计算覆盖率统计。"""
    total = len(clickhouse_service_ids)
    covered = len(clickhouse_service_ids & neo4j_service_ids)
    missing_service_ids = sorted(clickhouse_service_ids - neo4j_service_ids)
    return {
        "clickhouse_services": total,
        "neo4j_services": len(neo4j_service_ids),
        "covered_services": covered,
        "missing_services": len(missing_service_ids),
        "coverage_percent": round(covered * 100 / total, 2) if total > 0 else 0.0,
        "missing_service_ids": missing_service_ids,
    }


def _build_source_summary(service_inventory: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, int]]:
    """统计各数据源的服务覆盖和样本量。"""
    service_counts = {table: 0 for table in SERVICE_SOURCE_TABLES}
    observation_counts = {table: 0 for table in SERVICE_SOURCE_TABLES}

    for service in service_inventory.values():
        for table in SERVICE_SOURCE_TABLES:
            source_key = f"{table}_count"
            source_count = int(service.get(source_key, 0) or 0)
            if source_count > 0:
                service_counts[table] += 1
                observation_counts[table] += source_count

    return {
        "service_counts": service_counts,
        "observation_counts": observation_counts,
    }


async def sync_services_from_logs(storage: StorageAdapter) -> Dict[str, Any]:
    """
    从 ClickHouse 多源同步服务节点到 Neo4j。

    兼容历史函数名，已不局限于 logs 表。
    """
    if not storage.ch_client:
        return {"status": "error", "error": "ClickHouse client not available"}
    if not storage.neo4j_driver:
        return {"status": "error", "error": "Neo4j driver not available"}

    try:
        service_inventory = _collect_clickhouse_service_inventory(storage)
        if not service_inventory:
            return {
                "status": "no_services",
                "total_services": 0,
                "synced_count": 0,
                "failed_count": 0,
            }

        clickhouse_service_ids = set(service_inventory.keys())
        source_summary = _build_source_summary(service_inventory)
        before_neo4j_service_ids = _fetch_neo4j_service_ids(storage)
        coverage_before = _build_coverage_stats(clickhouse_service_ids, before_neo4j_service_ids)

        payload = []
        for service in service_inventory.values():
            last_seen = service.get("last_seen")
            payload.append(
                {
                    "service_id": service["service_name"],
                    "service_name": service["service_name"],
                    "logs_count": int(service.get("logs_count", 0) or 0),
                    "traces_count": int(service.get("traces_count", 0) or 0),
                    "events_count": int(service.get("events_count", 0) or 0),
                    "metrics_count": int(service.get("metrics_count", 0) or 0),
                    "total_count": int(service.get("total_count", 0) or 0),
                    "data_sources": sorted(service.get("data_sources", set())),
                    "last_seen": last_seen.isoformat() if isinstance(last_seen, datetime) else None,
                }
            )

        sync_query = """
            UNWIND $services AS svc
            MERGE (s:Service {id: svc.service_id})
            ON CREATE SET s.created_at = timestamp()
            SET s.name = svc.service_name,
                s.type = 'service',
                s.last_sync = timestamp(),
                s.last_seen = coalesce(svc.last_seen, s.last_seen),
                s.data_sources = svc.data_sources,
                s.total_observations = svc.total_count,
                s.log_count = svc.logs_count,
                s.logs_count = svc.logs_count,
                s.traces_count = svc.traces_count,
                s.events_count = svc.events_count,
                s.metrics_count = svc.metrics_count,
                s.coverage_version = 'multi-source-v2'
        """

        synced_count = 0
        failed_count = 0
        with storage.neo4j_driver.session() as session:
            for chunk in _chunked(payload, BATCH_SIZE):
                try:
                    session.run(sync_query, services=chunk)
                    synced_count += len(chunk)
                except Exception as chunk_error:
                    failed_count += len(chunk)
                    logger.error("Failed syncing Neo4j services batch(size=%s): %s", len(chunk), chunk_error)

        after_neo4j_service_ids = _fetch_neo4j_service_ids(storage)
        coverage_after = _build_coverage_stats(clickhouse_service_ids, after_neo4j_service_ids)

        return {
            "status": "completed",
            "total_services": len(payload),
            "synced_count": synced_count,
            "failed_count": failed_count,
            "source_summary": source_summary,
            "coverage_before": coverage_before,
            "coverage_after": coverage_after,
            "newly_covered_services": max(
                0,
                int(coverage_after["covered_services"]) - int(coverage_before["covered_services"]),
            ),
        }
    except Exception as exc:
        logger.error("Error syncing services: %s", exc)
        return {"status": "error", "error": str(exc)}


async def get_sync_status(storage: StorageAdapter) -> Dict[str, Any]:
    """获取 ClickHouse 多源服务与 Neo4j 节点覆盖状态。"""
    if not storage.ch_client:
        return {"status": "error", "error": "ClickHouse client not available"}

    try:
        service_inventory = _collect_clickhouse_service_inventory(storage)
        clickhouse_service_ids = set(service_inventory.keys())
        neo4j_service_ids = _fetch_neo4j_service_ids(storage)
        coverage = _build_coverage_stats(clickhouse_service_ids, neo4j_service_ids)

        status = {
            "clickhouse_services": coverage["clickhouse_services"],
            "neo4j_services": coverage["neo4j_services"],
            "coverage_percent": coverage["coverage_percent"],
            "missing_services": coverage["missing_services"],
            "missing_service_ids": coverage["missing_service_ids"][:100],
            "source_summary": _build_source_summary(service_inventory),
        }
        return status
    except Exception as exc:
        logger.error("Error getting sync status: %s", exc)
        return {"status": "error", "error": str(exc)}


if __name__ == "__main__":
    import asyncio
    from config import config

    async def main():
        """主函数"""
        storage = StorageAdapter(config.get_storage_config())

        # 获取同步状态
        status = await get_sync_status(storage)
        print(f"\n=== Neo4j 服务节点同步状态 ===")
        print(f"ClickHouse 服务数: {status['clickhouse_services']}")
        print(f"Neo4j 服务节点数: {status['neo4j_services']}")
        print(f"覆盖率: {status['coverage_percent']}%")
        print(f"缺失服务数: {status['missing_services']}")

        if status["missing_services"] > 0:
            print(f"\n开始同步 {status['missing_services']} 个服务...")
            result = await sync_services_from_logs(storage)
            print(f"同步完成: {result['synced_count']} 个成功, {result.get('failed_count', 0)} 个失败")
        else:
            print(f"\n✅ Neo4j 服务节点已完整！")

        storage.close()

    asyncio.run(main())
