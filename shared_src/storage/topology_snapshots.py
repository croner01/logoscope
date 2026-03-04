"""
拓扑历史快照存储模块

提供拓扑快照的保存和查询功能：
- 定期保存拓扑快照到 ClickHouse
- 支持时间范围查询
- 支持快照对比

Date: 2026-02-09
"""

from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta, timezone
import logging
import json

logger = logging.getLogger(__name__)


class TopologySnapshotManager:
    """
    拓扑快照管理器

    负责拓扑快照的保存、查询和对比
    """

    def __init__(self, storage_adapter):
        """
        初始化快照管理器

        Args:
            storage_adapter: StorageAdapter 实例
        """
        self.storage = storage_adapter
        self._ensure_snapshot_table()

    def _ensure_snapshot_table(self):
        """创建拓扑快照表（如果不存在）"""
        create_table_sql = """
        CREATE TABLE IF NOT EXISTS topology_snapshots (
            snapshot_id String,
            timestamp DateTime64(9, 'UTC'),
            time_window String,
            namespace String,
            confidence_threshold Float64,

            -- 拓扑数据（JSON 格式）
            nodes_json String,
            edges_json String,
            metadata_json String,

            -- 统计信息（用于快速查询）
            node_count UInt32,
            edge_count UInt32,
            avg_confidence Float64,

            -- 数据源统计
            source_traces_nodes UInt32,
            source_traces_edges UInt32,
            source_logs_nodes UInt32,
            source_logs_edges UInt32,
            source_metrics_nodes UInt32,
            source_metrics_edges UInt32,

            -- 快照大小（字节）
            snapshot_size UInt64,

            -- 创建时间
            created_at DateTime64(9, 'UTC') DEFAULT now64(9, 'UTC')
        )
        ENGINE = MergeTree()
        ORDER BY (timestamp, snapshot_id)
        SETTINGS index_granularity = 8192
        """

        try:
            self.storage.ch_client.execute(create_table_sql)
            logger.info("✅ Topology snapshots table ready")
        except Exception as e:
            logger.error(f"Failed to create topology_snapshots table: {e}")
            raise

    def save_snapshot(
        self,
        topology: Dict[str, Any],
        time_window: str = "1 HOUR",
        namespace: Optional[str] = None,
        confidence_threshold: float = 0.3,
        snapshot_id: Optional[str] = None
    ) -> str:
        """
        保存拓扑快照

        Args:
            topology: 拓扑数据（包含 nodes, edges, metadata）
            time_window: 时间窗口
            namespace: 命名空间
            confidence_threshold: 置信度阈值
            snapshot_id: 快照 ID（如果为 None 则自动生成）

        Returns:
            str: 快照 ID
        """
        import time
        import uuid

        # 生成快照 ID
        if snapshot_id is None:
            snapshot_id = f"snap_{int(time.time())}_{uuid.uuid4().hex[:8]}"

        # 提取数据
        nodes = topology.get("nodes", [])
        edges = topology.get("edges", [])
        metadata = topology.get("metadata", {})

        # 序列化为 JSON
        nodes_json = json.dumps(nodes)
        edges_json = json.dumps(edges)
        metadata_json = json.dumps(metadata)

        # 计算统计信息
        node_count = len(nodes)
        edge_count = len(edges)
        avg_confidence = metadata.get("avg_confidence", 0.0)

        # 提取数据源统计
        source_breakdown = metadata.get("source_breakdown", {})
        source_traces = source_breakdown.get("traces", {})
        source_logs = source_breakdown.get("logs", {})
        source_metrics = source_breakdown.get("metrics", {})

        source_traces_nodes = source_traces.get("nodes", 0)
        source_traces_edges = source_traces.get("edges", 0)
        source_logs_nodes = source_logs.get("nodes", 0)
        source_logs_edges = source_logs.get("edges", 0)
        source_metrics_nodes = source_metrics.get("nodes", 0)
        source_metrics_edges = source_metrics.get("edges", 0)

        # 计算快照大小
        snapshot_size = len(nodes_json) + len(edges_json) + len(metadata_json)

        # 使用拓扑数据中的时间戳，如果没有则使用当前时间
        topology_time = metadata.get("generated_at")
        if topology_time:
            # 解析 ISO 8601 时间戳
            try:
                timestamp = datetime.fromisoformat(topology_time.replace('Z', '+00:00'))
                # 转换为 UTC
                if timestamp.tzinfo is not None:
                    timestamp = timestamp.astimezone(timezone.utc).replace(tzinfo=None)
            except:
                timestamp = datetime.now(timezone.utc).replace(tzinfo=None)
        else:
            timestamp = datetime.now(timezone.utc).replace(tzinfo=None)

        # 插入数据库
        insert_sql = """
        INSERT INTO topology_snapshots (
            snapshot_id, timestamp, time_window, namespace, confidence_threshold,
            nodes_json, edges_json, metadata_json,
            node_count, edge_count, avg_confidence,
            source_traces_nodes, source_traces_edges,
            source_logs_nodes, source_logs_edges,
            source_metrics_nodes, source_metrics_edges,
            snapshot_size
        ) VALUES
        """

        try:
            self.storage.ch_client.execute(insert_sql, [{
                'snapshot_id': snapshot_id,
                'timestamp': timestamp,
                'time_window': time_window,
                'namespace': namespace or '',
                'confidence_threshold': confidence_threshold,
                'nodes_json': nodes_json,
                'edges_json': edges_json,
                'metadata_json': metadata_json,
                'node_count': node_count,
                'edge_count': edge_count,
                'avg_confidence': avg_confidence,
                'source_traces_nodes': source_traces_nodes,
                'source_traces_edges': source_traces_edges,
                'source_logs_nodes': source_logs_nodes,
                'source_logs_edges': source_logs_edges,
                'source_metrics_nodes': source_metrics_nodes,
                'source_metrics_edges': source_metrics_edges,
                'snapshot_size': snapshot_size
            }])

            logger.info(
                f"✅ Saved topology snapshot: {snapshot_id} "
                f"({node_count} nodes, {edge_count} edges)"
            )

            return snapshot_id

        except Exception as e:
            logger.error(f"Failed to save topology snapshot: {e}")
            raise

    def get_snapshot(
        self,
        snapshot_id: str
    ) -> Optional[Dict[str, Any]]:
        """
        获取单个快照

        Args:
            snapshot_id: 快照 ID

        Returns:
            Dict: 快照数据，如果不存在则返回 None
        """
        query = """
        SELECT
            snapshot_id,
            timestamp,
            time_window,
            namespace,
            confidence_threshold,
            nodes_json,
            edges_json,
            metadata_json,
            node_count,
            edge_count,
            avg_confidence,
            source_traces_nodes,
            source_traces_edges,
            source_logs_nodes,
            source_logs_edges,
            source_metrics_nodes,
            source_metrics_edges,
            snapshot_size,
            created_at
        FROM topology_snapshots
        WHERE snapshot_id = %(snapshot_id)s
        """

        try:
            result = self.storage.ch_client.execute(
                query,
                {'snapshot_id': snapshot_id}
            )

            if not result:
                return None

            row = result[0]

            # 解析 JSON
            nodes = json.loads(row[5])
            edges = json.loads(row[6])
            metadata = json.loads(row[7])

            return {
                'snapshot_id': row[0],
                'timestamp': row[1].isoformat() if row[1] else None,
                'time_window': row[2],
                'namespace': row[3],
                'confidence_threshold': row[4],
                'topology': {
                    'nodes': nodes,
                    'edges': edges,
                    'metadata': metadata
                },
                'statistics': {
                    'node_count': row[8],
                    'edge_count': row[9],
                    'avg_confidence': row[10],
                    'source_traces_nodes': row[11],
                    'source_traces_edges': row[12],
                    'source_logs_nodes': row[13],
                    'source_logs_edges': row[14],
                    'source_metrics_nodes': row[15],
                    'source_metrics_edges': row[16],
                    'snapshot_size': row[17]
                },
                'created_at': row[18].isoformat() if row[18] else None
            }

        except Exception as e:
            logger.error(f"Failed to get snapshot {snapshot_id}: {e}")
            return None

    def list_snapshots(
        self,
        from_time: Optional[datetime] = None,
        to_time: Optional[datetime] = None,
        namespace: Optional[str] = None,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """
        列出快照

        Args:
            from_time: 起始时间
            to_time: 结束时间
            namespace: 命名空间过滤
            limit: 返回数量限制

        Returns:
            List[Dict]: 快照列表
        """
        where_conditions = []
        params = {}

        if from_time:
            where_conditions.append("timestamp >= %(from_time)s")
            params['from_time'] = from_time

        if to_time:
            where_conditions.append("timestamp <= %(to_time)s")
            params['to_time'] = to_time

        if namespace:
            where_conditions.append("namespace = %(namespace)s")
            params['namespace'] = namespace

        where_clause = " AND ".join(where_conditions) if where_conditions else "1=1"

        query = f"""
        SELECT
            snapshot_id,
            timestamp,
            time_window,
            namespace,
            confidence_threshold,
            node_count,
            edge_count,
            avg_confidence,
            snapshot_size,
            created_at
        FROM topology_snapshots
        WHERE {where_clause}
        ORDER BY timestamp DESC
        LIMIT %(limit)s
        """

        params['limit'] = limit

        try:
            result = self.storage.ch_client.execute(query, params)

            snapshots = []
            for row in result:
                snapshots.append({
                    'snapshot_id': row[0],
                    'timestamp': row[1].isoformat() if row[1] else None,
                    'time_window': row[2],
                    'namespace': row[3],
                    'confidence_threshold': row[4],
                    'node_count': row[5],
                    'edge_count': row[6],
                    'avg_confidence': row[7],
                    'snapshot_size': row[8],
                    'created_at': row[9].isoformat() if row[9] else None
                })

            return snapshots

        except Exception as e:
            logger.error(f"Failed to list snapshots: {e}")
            return []

    def compare_snapshots(
        self,
        snapshot_id_1: str,
        snapshot_id_2: str
    ) -> Dict[str, Any]:
        """
        对比两个快照

        Args:
            snapshot_id_1: 快照 1 ID
            snapshot_id_2: 快照 2 ID

        Returns:
            Dict: 对比结果
        """
        snap1 = self.get_snapshot(snapshot_id_1)
        snap2 = self.get_snapshot(snapshot_id_2)

        if not snap1 or not snap2:
            return {
                'error': 'One or both snapshots not found',
                'snapshot_1_found': snap1 is not None,
                'snapshot_2_found': snap2 is not None
            }

        # 提取节点和边
        nodes1 = set(n['id'] for n in snap1['topology']['nodes'])
        nodes2 = set(n['id'] for n in snap2['topology']['nodes'])

        edges1 = set((e['source'], e['target']) for e in snap1['topology']['edges'])
        edges2 = set((e['source'], e['target']) for e in snap2['topology']['edges'])

        # 计算差异
        added_nodes = nodes2 - nodes1
        removed_nodes = nodes1 - nodes2

        added_edges = edges2 - edges1
        removed_edges = edges1 - edges2

        # 统计对比
        stats1 = snap1['statistics']
        stats2 = snap2['statistics']

        return {
            'snapshot_1': {
                'id': snapshot_id_1,
                'timestamp': snap1['timestamp'],
                'node_count': stats1['node_count'],
                'edge_count': stats1['edge_count'],
                'avg_confidence': stats1['avg_confidence']
            },
            'snapshot_2': {
                'id': snapshot_id_2,
                'timestamp': snap2['timestamp'],
                'node_count': stats2['node_count'],
                'edge_count': stats2['edge_count'],
                'avg_confidence': stats2['avg_confidence']
            },
            'changes': {
                'nodes': {
                    'added': list(added_nodes),
                    'removed': list(removed_nodes),
                    'count': len(added_nodes) + len(removed_nodes)
                },
                'edges': {
                    'added': [list(e) for e in added_edges],
                    'removed': [list(e) for e in removed_edges],
                    'count': len(added_edges) + len(removed_edges)
                },
                'statistics': {
                    'node_count_diff': stats2['node_count'] - stats1['node_count'],
                    'edge_count_diff': stats2['edge_count'] - stats1['edge_count'],
                    'confidence_diff': stats2['avg_confidence'] - stats1['avg_confidence']
                }
            }
        }

    def delete_old_snapshots(self, retention_days: int = 30):
        """
        删除旧快照（保留指定天数内的快照）

        Args:
            retention_days: 保留天数
        """
        cutoff_time = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=retention_days)

        delete_sql = """
        DELETE FROM topology_snapshots
        WHERE timestamp < %(cutoff_time)s
        """

        try:
            result = self.storage.ch_client.execute(
                delete_sql,
                {'cutoff_time': cutoff_time}
            )

            deleted_count = result[0][0] if result else 0
            logger.info(f"Deleted {deleted_count} old topology snapshots (older than {retention_days} days)")

            return deleted_count

        except Exception as e:
            logger.error(f"Failed to delete old snapshots: {e}")
            return 0


# 全局实例
_snapshot_manager: Optional[TopologySnapshotManager] = None


def get_topology_snapshot_manager(storage_adapter) -> TopologySnapshotManager:
    """
    获取拓扑快照管理器实例

    Args:
        storage_adapter: StorageAdapter 实例

    Returns:
        TopologySnapshotManager: 快照管理器实例
    """
    global _snapshot_manager

    if _snapshot_manager is None:
        _snapshot_manager = TopologySnapshotManager(storage_adapter)

    return _snapshot_manager
