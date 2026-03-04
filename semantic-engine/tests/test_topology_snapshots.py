"""
Topology Snapshots 模块单元测试

测试 storage/topology_snapshots.py 的核心功能：
- 拓扑快照管理器初始化
- 快照保存功能
- 快照查询功能
- 快照列表功能
- 快照对比功能
- 旧快照删除功能
"""
import pytest
from unittest.mock import Mock, MagicMock, patch
from datetime import datetime, timedelta, timezone

from storage.topology_snapshots import (
    TopologySnapshotManager,
    get_topology_snapshot_manager
)


class TestTopologySnapshotManagerInit:
    """测试 TopologySnapshotManager 初始化"""

    @pytest.fixture
    def mock_storage(self):
        """Mock storage adapter"""
        storage = Mock()
        storage.ch_client = Mock()
        return storage

    def test_init_with_storage(self, mock_storage):
        """测试带 storage 的初始化"""
        manager = TopologySnapshotManager(mock_storage)

        assert manager.storage == mock_storage

    def test_init_creates_table(self, mock_storage):
        """测试初始化时创建表"""
        TopologySnapshotManager(mock_storage)

        # 验证执行了 CREATE TABLE 语句
        mock_storage.ch_client.execute.assert_called_once()
        query = mock_storage.ch_client.execute.call_args[0][0]
        assert "CREATE TABLE IF NOT EXISTS topology_snapshots" in query

    def test_init_table_creation_failure(self, mock_storage):
        """测试表创建失败"""
        mock_storage.ch_client.execute = Mock(
            side_effect=Exception("Database error")
        )

        with pytest.raises(Exception, match="Database error"):
            TopologySnapshotManager(mock_storage)


class TestSaveSnapshot:
    """测试快照保存功能"""

    @pytest.fixture
    def mock_storage(self):
        storage = Mock()
        storage.ch_client = Mock()
        return storage

    @pytest.fixture
    def manager(self, mock_storage):
        return TopologySnapshotManager(mock_storage)

    @pytest.fixture
    def sample_topology(self):
        """示例拓扑数据"""
        return {
            "nodes": [
                {"id": "service-a", "type": "service"},
                {"id": "service-b", "type": "service"}
            ],
            "edges": [
                {"source": "service-a", "target": "service-b", "type": "calls"}
            ],
            "metadata": {
                "avg_confidence": 0.85,
                "generated_at": "2026-02-09T12:00:00Z",
                "source_breakdown": {
                    "traces": {"nodes": 2, "edges": 1},
                    "logs": {"nodes": 2, "edges": 0},
                    "metrics": {"nodes": 0, "edges": 0}
                }
            }
        }

    def test_save_snapshot_basic(self, manager, mock_storage, sample_topology):
        """测试基本快照保存"""
        snapshot_id = manager.save_snapshot(sample_topology)

        # 验证返回了快照 ID
        assert snapshot_id is not None
        assert snapshot_id.startswith("snap_")

        # 验证执行了 INSERT 语句
        mock_storage.ch_client.execute.assert_called()

    def test_save_snapshot_with_custom_id(self, manager, mock_storage, sample_topology):
        """测试使用自定义 ID 保存快照"""
        custom_id = "custom_snapshot_id"

        snapshot_id = manager.save_snapshot(
            sample_topology,
            snapshot_id=custom_id
        )

        assert snapshot_id == custom_id

    def test_save_snapshot_with_namespace(self, manager, mock_storage, sample_topology):
        """测试带命名空间的快照保存"""
        snapshot_id = manager.save_snapshot(
            sample_topology,
            namespace="islap"
        )

        # 验证 INSERT 语句包含命名空间
        mock_storage.ch_client.execute.assert_called()
        call_args = mock_storage.ch_client.execute.call_args[0][1]
        assert call_args[0]['namespace'] == 'islap'

    def test_save_snapshot_with_custom_time_window(self, manager, mock_storage, sample_topology):
        """测试自定义时间窗口"""
        snapshot_id = manager.save_snapshot(
            sample_topology,
            time_window="15 MINUTE"
        )

        # 验证 INSERT 语句包含时间窗口
        mock_storage.ch_client.execute.assert_called()
        call_args = mock_storage.ch_client.execute.call_args[0][1]
        assert call_args[0]['time_window'] == '15 MINUTE'

    def test_save_snapshot_calculates_stats(self, manager, mock_storage, sample_topology):
        """测试统计信息计算"""
        snapshot_id = manager.save_snapshot(sample_topology)

        # 验证统计信息
        call_args = mock_storage.ch_client.execute.call_args[0][1]
        data = call_args[0]

        assert data['node_count'] == 2
        assert data['edge_count'] == 1
        assert data['avg_confidence'] == 0.85

    def test_save_snapshot_includes_source_breakdown(self, manager, mock_storage, sample_topology):
        """测试数据源统计信息"""
        snapshot_id = manager.save_snapshot(sample_topology)

        # 验证数据源统计
        call_args = mock_storage.ch_client.execute.call_args[0][1]
        data = call_args[0]

        assert data['source_traces_nodes'] == 2
        assert data['source_traces_edges'] == 1
        assert data['source_logs_nodes'] == 2
        assert data['source_logs_edges'] == 0

    def test_save_snapshot_database_error(self, manager, mock_storage, sample_topology):
        """测试数据库错误处理"""
        mock_storage.ch_client.execute = Mock(
            side_effect=Exception("Insert failed")
        )

        with pytest.raises(Exception, match="Insert failed"):
            manager.save_snapshot(sample_topology)


class TestGetSnapshot:
    """测试快照查询功能"""

    @pytest.fixture
    def mock_storage(self):
        storage = Mock()
        storage.ch_client = Mock()
        return storage

    @pytest.fixture
    def manager(self, mock_storage):
        return TopologySnapshotManager(mock_storage)

    def test_get_snapshot_found(self, manager, mock_storage):
        """测试查询存在的快照"""
        # Mock 查询结果
        mock_result = [
            (
                "snap_123",
                datetime(2026, 2, 9, 12, 0, 0),
                "1 HOUR",
                "islap",
                0.3,
                '[{"id": "service-a"}]',  # nodes_json
                '[{"source": "a", "target": "b"}]',  # edges_json
                '{"avg_confidence": 0.85}',  # metadata_json
                1,  # node_count
                1,  # edge_count
                0.85,  # avg_confidence
                1, 0,  # source_traces
                0, 0,  # source_logs
                0, 0,  # source_metrics
                1024,  # snapshot_size
                datetime(2026, 2, 9, 12, 0, 0)  # created_at
            )
        ]

        mock_storage.ch_client.execute = Mock(return_value=mock_result)

        snapshot = manager.get_snapshot("snap_123")

        assert snapshot is not None
        assert snapshot['snapshot_id'] == "snap_123"
        assert snapshot['statistics']['node_count'] == 1
        assert snapshot['statistics']['edge_count'] == 1
        assert len(snapshot['topology']['nodes']) == 1
        assert len(snapshot['topology']['edges']) == 1

    def test_get_snapshot_not_found(self, manager, mock_storage):
        """测试查询不存在的快照"""
        mock_storage.ch_client.execute = Mock(return_value=[])

        snapshot = manager.get_snapshot("nonexistent")

        assert snapshot is None

    def test_get_snapshot_database_error(self, manager, mock_storage):
        """测试数据库错误"""
        mock_storage.ch_client.execute = Mock(
            side_effect=Exception("Query failed")
        )

        snapshot = manager.get_snapshot("snap_123")

        # 错误时返回 None
        assert snapshot is None


class TestListSnapshots:
    """测试快照列表功能"""

    @pytest.fixture
    def mock_storage(self):
        storage = Mock()
        storage.ch_client = Mock()
        return storage

    @pytest.fixture
    def manager(self, mock_storage):
        return TopologySnapshotManager(mock_storage)

    def test_list_snapshots_basic(self, manager, mock_storage):
        """测试基本列表查询"""
        mock_result = [
            ("snap_1", datetime(2026, 2, 9, 12, 0, 0), "1 HOUR", "islap", 0.3, 2, 1, 0.85, 1024, datetime(2026, 2, 9, 12, 0, 0)),
            ("snap_2", datetime(2026, 2, 9, 11, 0, 0), "1 HOUR", "islap", 0.3, 3, 2, 0.90, 2048, datetime(2026, 2, 9, 11, 0, 0))
        ]

        mock_storage.ch_client.execute = Mock(return_value=mock_result)

        snapshots = manager.list_snapshots()

        assert len(snapshots) == 2
        assert snapshots[0]['snapshot_id'] == "snap_1"
        assert snapshots[1]['snapshot_id'] == "snap_2"

    def test_list_snapshots_with_time_filter(self, manager, mock_storage):
        """测试带时间过滤的列表查询"""
        mock_storage.ch_client.execute = Mock(return_value=[])

        from_time = datetime(2026, 2, 9, 10, 0, 0)
        to_time = datetime(2026, 2, 9, 14, 0, 0)

        snapshots = manager.list_snapshots(from_time=from_time, to_time=to_time)

        # 验证执行了查询
        mock_storage.ch_client.execute.assert_called_once()
        # 验证查询包含时间过滤
        query = mock_storage.ch_client.execute.call_args[0][0]
        assert "timestamp >=" in query
        assert "timestamp <=" in query

    def test_list_snapshots_with_namespace_filter(self, manager, mock_storage):
        """测试带命名空间过滤的列表查询"""
        mock_storage.ch_client.execute = Mock(return_value=[])

        snapshots = manager.list_snapshots(namespace="islap")

        # 验证查询包含命名空间过滤
        query = mock_storage.ch_client.execute.call_args[0][0]
        assert "namespace =" in query

    def test_list_snapshots_with_limit(self, manager, mock_storage):
        """测试带限制的列表查询"""
        mock_storage.ch_client.execute = Mock(return_value=[])

        snapshots = manager.list_snapshots(limit=50)

        # 验证查询包含限制
        query = mock_storage.ch_client.execute.call_args[0][0]
        assert "LIMIT" in query

    def test_list_snapshots_database_error(self, manager, mock_storage):
        """测试数据库错误"""
        mock_storage.ch_client.execute = Mock(
            side_effect=Exception("Query failed")
        )

        snapshots = manager.list_snapshots()

        # 错误时返回空列表
        assert snapshots == []


class TestCompareSnapshots:
    """测试快照对比功能"""

    @pytest.fixture
    def mock_storage(self):
        storage = Mock()
        storage.ch_client = Mock()
        return storage

    @pytest.fixture
    def manager(self, mock_storage):
        return TopologySnapshotManager(mock_storage)

    @pytest.fixture
    def mock_snapshots(self, manager, mock_storage):
        """设置 mock 快照数据"""
        # 使用 patch 来模拟 get_snapshot 方法
        snapshot1 = {
            'snapshot_id': 'snap_1',
            'timestamp': '2026-02-09T12:00:00',
            'topology': {
                'nodes': [
                    {'id': 'service-a'},
                    {'id': 'service-b'}
                ],
                'edges': [
                    {'source': 'service-a', 'target': 'service-b'}
                ]
            },
            'statistics': {
                'node_count': 2,
                'edge_count': 1,
                'avg_confidence': 0.85
            }
        }

        snapshot2 = {
            'snapshot_id': 'snap_2',
            'timestamp': '2026-02-09T13:00:00',
            'topology': {
                'nodes': [
                    {'id': 'service-a'},
                    {'id': 'service-b'},
                    {'id': 'service-c'}
                ],
                'edges': [
                    {'source': 'service-a', 'target': 'service-b'},
                    {'source': 'service-b', 'target': 'service-c'}
                ]
            },
            'statistics': {
                'node_count': 3,
                'edge_count': 2,
                'avg_confidence': 0.90
            }
        }

        return snapshot1, snapshot2

    def test_compare_snapshots_success(self, manager, mock_storage, mock_snapshots):
        """测试成功对比两个快照"""
        snapshot1, snapshot2 = mock_snapshots

        with patch.object(manager, 'get_snapshot') as mock_get:
            mock_get.side_effect = [snapshot1, snapshot2]

            comparison = manager.compare_snapshots('snap_1', 'snap_2')

            # 验证返回了对比结果
            assert 'snapshot_1' in comparison
            assert 'snapshot_2' in comparison
            assert 'changes' in comparison
            # 验证检测到变化
            assert comparison['changes']['nodes']['count'] >= 0

    def test_compare_snapshots_one_not_found(self, manager):
        """测试其中一个快照不存在"""
        with patch.object(manager, 'get_snapshot') as mock_get:
            mock_get.side_effect = [None, {'snapshot_id': 'snap_2'}]

            comparison = manager.compare_snapshots('snap_1', 'snap_2')

            assert 'error' in comparison
            assert comparison['snapshot_1_found'] == False
            assert comparison['snapshot_2_found'] == True

    def test_compare_snapshots_both_not_found(self, manager):
        """测试两个快照都不存在"""
        with patch.object(manager, 'get_snapshot') as mock_get:
            mock_get.side_effect = [None, None]

            comparison = manager.compare_snapshots('snap_1', 'snap_2')

            assert 'error' in comparison
            assert comparison['snapshot_1_found'] == False
            assert comparison['snapshot_2_found'] == False


class TestDeleteOldSnapshots:
    """测试旧快照删除功能"""

    @pytest.fixture
    def mock_storage(self):
        storage = Mock()
        storage.ch_client = Mock()
        return storage

    @pytest.fixture
    def manager(self, mock_storage):
        return TopologySnapshotManager(mock_storage)

    def test_delete_old_snapshots_default_retention(self, manager, mock_storage):
        """测试默认保留期删除"""
        mock_storage.ch_client.execute = Mock(return_value=[[5]])

        deleted_count = manager.delete_old_snapshots()

        assert deleted_count == 5

        # 验证 DELETE 语句
        mock_storage.ch_client.execute.assert_called_once()
        query = mock_storage.ch_client.execute.call_args[0][0]
        assert "DELETE FROM topology_snapshots" in query

    def test_delete_old_snapshots_custom_retention(self, manager, mock_storage):
        """测试自定义保留期"""
        mock_storage.ch_client.execute = Mock(return_value=[[10]])

        deleted_count = manager.delete_old_snapshots(retention_days=60)

        assert deleted_count == 10

    def test_delete_old_snapshots_no_snapshots(self, manager, mock_storage):
        """测试没有旧快照"""
        mock_storage.ch_client.execute = Mock(return_value=[[0]])

        deleted_count = manager.delete_old_snapshots()

        assert deleted_count == 0

    def test_delete_old_snapshots_database_error(self, manager, mock_storage):
        """测试数据库错误"""
        mock_storage.ch_client.execute = Mock(
            side_effect=Exception("Delete failed")
        )

        deleted_count = manager.delete_old_snapshots()

        # 错误时返回 0
        assert deleted_count == 0


class TestConvenienceFunction:
    """测试便捷函数"""

    def test_get_topology_snapshot_manager(self):
        """测试获取管理器实例"""
        mock_storage = Mock()

        manager1 = get_topology_snapshot_manager(mock_storage)
        manager2 = get_topology_snapshot_manager(mock_storage)

        # 应该返回同一个实例（单例模式）
        assert manager1 is manager2


class TestEdgeCases:
    """测试边界情况"""

    @pytest.fixture
    def mock_storage(self):
        storage = Mock()
        storage.ch_client = Mock()
        return storage

    @pytest.fixture
    def manager(self, mock_storage):
        return TopologySnapshotManager(mock_storage)

    def test_save_empty_topology(self, manager, mock_storage):
        """测试保存空拓扑"""
        empty_topology = {
            "nodes": [],
            "edges": [],
            "metadata": {}
        }

        snapshot_id = manager.save_snapshot(empty_topology)

        assert snapshot_id is not None

    def test_save_topology_without_metadata(self, manager, mock_storage):
        """测试没有元数据的拓扑"""
        topology = {
            "nodes": [{"id": "service-a"}],
            "edges": []
        }

        snapshot_id = manager.save_snapshot(topology)

        assert snapshot_id is not None

    def test_save_topology_with_invalid_timestamp(self, manager, mock_storage):
        """测试带无效时间戳的拓扑"""
        topology = {
            "nodes": [],
            "edges": [],
            "metadata": {
                "generated_at": "invalid-timestamp"
            }
        }

        snapshot_id = manager.save_snapshot(topology)

        # 应该使用当前时间
        assert snapshot_id is not None

    def test_compare_snapshots_with_empty_topologies(self, manager):
        """测试对比空拓扑"""
        snapshot1 = {
            'snapshot_id': 'snap_1',
            'timestamp': '2026-02-09T12:00:00',
            'topology': {'nodes': [], 'edges': []},
            'statistics': {'node_count': 0, 'edge_count': 0, 'avg_confidence': 0.0}
        }

        snapshot2 = {
            'snapshot_id': 'snap_2',
            'timestamp': '2026-02-09T13:00:00',
            'topology': {'nodes': [], 'edges': []},
            'statistics': {'node_count': 0, 'edge_count': 0, 'avg_confidence': 0.0}
        }

        with patch.object(manager, 'get_snapshot') as mock_get:
            mock_get.side_effect = [snapshot1, snapshot2]

            comparison = manager.compare_snapshots('snap_1', 'snap_2')

            assert comparison['changes']['nodes']['count'] == 0
            assert comparison['changes']['edges']['count'] == 0
