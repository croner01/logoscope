#!/usr/bin/env python3
"""
清除 ClickHouse 和 Neo4j 中的 unknown 数据
"""
import sys
import logging
import os

# 添加项目根目录到路径
sys.path.append('/root/logoscope/semantic-engine')

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.abspath(os.path.dirname(__file__))))

from storage.adapter import StorageAdapter
from config import config
from graph.service_sync import sync_services_from_logs

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def clean_clickhouse_unknown():
    """清除 ClickHouse logs.logs 表中的 unknown 数据"""
    try:
        storage = StorageAdapter(config.get_storage_config())

        # 查询 unknown 数据总数
        count_query = "SELECT COUNT(*) FROM logs.logs WHERE service_name = 'unknown'"
        result = storage.ch_client.execute(count_query)
        count = result[0][0] if result else 0
        logger.info(f"Found {count} unknown records in logs.logs")

        if count > 0:
            # 删除 unknown 数据
            delete_query = "ALTER TABLE logs.logs DELETE WHERE service_name = 'unknown'"
            storage.ch_client.execute(delete_query)
            logger.info(f"Deleted {count} unknown records from logs.logs")
            return {"status": "success", "deleted_count": count}
        else:
            return {"status": "success", "deleted_count": 0}

    except Exception as e:
        logger.error(f"Error cleaning ClickHouse: {e}")
        return {"status": "error", "error": str(e)}


def clean_neo4j_unknown():
    """清除 Neo4j 中的 unknown 服务节点"""
    try:
        storage = StorageAdapter(config.get_storage_config())

        if not storage.neo4j_driver:
            return {"status": "error", "error": "Neo4j driver not available"}

        with storage.neo4j_driver.session() as session:
            # 删除 unknown 服务节点
            result = session.run("""
                MATCH (s:Service {id: 'unknown'})
                DETACH DELETE s
                RETURN count(s) as deleted_count
            """)

            deleted_count = result.single()["deleted_count"]
            logger.info(f"Deleted {deleted_count} unknown service nodes from Neo4j")

            # 删除与 unknown 相关的关系
            result2 = session.run("""
                MATCH ()-[r:CALLS]->(s:Service {id: 'unknown'})
                DELETE r
                RETURN count(r) as deleted_relations
            """)

            deleted_relations = result2.single()["deleted_relations"]
            logger.info(f"Deleted {deleted_relations} relations involving unknown from Neo4j")

            return {
                "status": "success",
                "deleted_nodes": deleted_count,
                "deleted_relations": deleted_relations
            }

    except Exception as e:
        logger.error(f"Error cleaning Neo4j: {e}")
        return {"status": "error", "error": str(e)}


def sync_cleaned_services():
    """同步清理后的服务节点到 Neo4j"""
    try:
        storage = StorageAdapter(config.get_storage_config())
        result = sync_services_from_logs(storage)
        return result
    except Exception as e:
        logger.error(f"Error syncing services: {e}")
        return {"status": "error", "error": str(e)}


def main():
    """主函数"""
    import asyncio

    print("\n=========================================")
    print("清除 unknown 数据并重新同步")
    print("=========================================\n")

    # 1. 清除 ClickHouse unknown 数据
    print("[1/4] 清除 ClickHouse unknown 数据...")
    ch_result = clean_clickhouse_unknown()
    print(f"     状态: {ch_result['status']}")
    print(f"     删除: {ch_result.get('deleted_count', 0)} 条")
    print()

    # 2. 清除 Neo4j unknown 节点
    print("[2/4] 清除 Neo4j unknown 节点...")
    neo_result = clean_neo4j_unknown()
    print(f"     状态: {neo_result['status']}")
    print(f"     删除节点: {neo_result.get('deleted_nodes', 0)}")
    print(f"     删除关系: {neo_result.get('deleted_relations', 0)}")
    print()

    # 3. 同步服务节点（不包含 unknown）
    print("[3/4] 同步服务节点到 Neo4j...")
    sync_result = sync_cleaned_services()
    print(f"     状态: {sync_result['status']}")
    print(f"     同步服务数: {sync_result.get('synced_count', 0)}")
    print()

    print("=========================================")
    print("清理完成！")
    print("=========================================\n")

    print("建议：等待5分钟后运行以下命令验证数据质量：")
    print("  curl -s http://10.43.190.62:8080/api/v1/quality/overview | jq .")
    print("")


if __name__ == "__main__":
    main()
