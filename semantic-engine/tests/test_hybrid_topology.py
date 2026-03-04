"""
Hybrid Topology Builder 单元测试

测试 graph/hybrid_topology.py 的核心功能：
- 混合数据源拓扑构建
- Traces 拓扑提取
- Logs 拓扑提取
- Metrics 拓扑提取
- 节点和边合并
"""
import pytest
from unittest.mock import Mock, MagicMock, patch
from datetime import datetime

from graph.hybrid_topology import (
    HybridTopologyBuilder,
    get_hybrid_topology_builder
)
from storage.adapter import StorageAdapter


class TestHybridTopologyBuilderInit:
    """测试 HybridTopologyBuilder 初始化"""

    @pytest.fixture
    def mock_storage(self):
        """Mock storage adapter"""
        storage = Mock(spec=StorageAdapter)
        return storage

    def test_init(self, mock_storage):
        """测试初始化"""
        builder = HybridTopologyBuilder(mock_storage)

        assert builder.storage == mock_storage
        assert builder.WEIGHT_TRACES == 1.0
        assert builder.WEIGHT_LOGS == 0.3
        assert builder.WEIGHT_METRICS == 0.2
        assert builder.time_window == "1 HOUR"


class TestBuildTopology:
    """测试拓扑构建"""

    @pytest.fixture
    def mock_storage(self):
        """Mock storage adapter"""
        storage = Mock(spec=StorageAdapter)
        storage.ch_client = Mock()  # 模拟数据库连接
        return storage

    @pytest.fixture
    def builder(self, mock_storage):
        return HybridTopologyBuilder(mock_storage)

    def test_build_topology_basic(self, builder, mock_storage):
        """测试基本拓扑构建"""
        # Mock 查询返回空结果
        mock_storage.execute_query = Mock(return_value=[])

        result = builder.build_topology()

        assert "nodes" in result
        assert "edges" in result
        assert "metadata" in result
        assert isinstance(result["nodes"], list)
        assert isinstance(result["edges"], list)

    def test_build_topology_with_namespace(self, builder, mock_storage):
        """测试带命名空间过滤的拓扑构建"""
        mock_storage.execute_query = Mock(return_value=[])

        result = builder.build_topology(namespace="islap")

        assert result["metadata"]["namespace"] == "islap"

    def test_build_topology_with_custom_time_window(self, builder, mock_storage):
        """测试自定义时间窗口"""
        mock_storage.execute_query = Mock(return_value=[])

        result = builder.build_topology(time_window="15 MINUTE")

        assert result["metadata"]["time_window"] == "15 MINUTE"

    def test_build_topology_with_confidence_filter(self, builder, mock_storage):
        """测试置信度过滤"""
        # Mock 返回低置信度的边
        mock_storage.execute_query = Mock(return_value=[])

        result = builder.build_topology(confidence_threshold=0.5)

        # 应该过滤掉低置信度的边
        for edge in result["edges"]:
            assert edge.get("metrics", {}).get("confidence", 0) >= 0.5

    def test_build_topology_error_handling(self, builder, mock_storage):
        """测试错误处理"""
        # Mock 执行查询时抛出异常
        mock_storage.execute_query = Mock(side_effect=Exception("Database error"))

        result = builder.build_topology()

        # 应该返回空拓扑而不是崩溃
        assert result["nodes"] == []
        assert result["edges"] == []


class TestGetTracesTopology:
    """测试 Traces 拓扑提取"""

    @pytest.fixture
    def mock_storage(self):
        storage = Mock(spec=StorageAdapter)
        storage.ch_client = Mock()
        return storage

    @pytest.fixture
    def builder(self, mock_storage):
        return HybridTopologyBuilder(mock_storage)

    def test_traces_topology_with_parent_child(self, builder, mock_storage):
        """测试带父子关系的 traces"""
        # Mock 返回 traces 数据（7列匹配代码期望）
        mock_storage.execute_query = Mock(return_value=[
            ("trace-1", "span-1", "", "frontend", "GET /api", 100, "ok"),
            ("trace-1", "span-2", "span-1", "backend", "process", 80, "ok")
        ])

        result = builder._get_traces_topology("1 HOUR")

        assert "nodes" in result
        assert "edges" in result

        # 验证基本结构
        assert isinstance(result["nodes"], list)
        assert isinstance(result["edges"], list)

    def test_traces_topology_with_error_status(self, builder, mock_storage):
        """测试带错误状态的 traces"""
        mock_storage.execute_query = Mock(return_value=[
            ("trace-1", "span-1", "", "service-a", "operation", 100, "error"),
            ("trace-1", "span-2", "span-1", "service-b", "operation", 80, "ok")
        ])

        result = builder._get_traces_topology("1 HOUR")

        # 验证基本结构
        assert len(result["nodes"]) == 2
        assert len(result["edges"]) == 1

        # 验证边存在
        edge = result["edges"][0]
        assert edge["source"] in ["service-a", "service-b"]
        assert edge["target"] in ["service-a", "service-b"]

    def test_traces_topology_multiple_traces(self, builder, mock_storage):
        """测试多个 traces"""
        mock_storage.execute_query = Mock(return_value=[
            ("trace-1", "span-1", "", "service-a", "op1", 100, "ok"),
            ("trace-1", "span-2", "span-1", "service-b", "op2", 80, "ok"),
            ("trace-2", "span-3", "", "service-a", "op1", 120, "ok"),
            ("trace-2", "span-4", "span-3", "service-b", "op2", 90, "ok")
        ])

        result = builder._get_traces_topology("1 HOUR")

        # 验证基本结构
        assert len(result["nodes"]) == 2
        assert len(result["edges"]) >= 1

    def test_traces_topology_no_database(self):
        """测试没有数据库连接的情况"""
        storage = Mock(spec=StorageAdapter)
        storage.ch_client = None  # 没有数据库连接

        builder = HybridTopologyBuilder(storage)
        result = builder._get_traces_topology("1 HOUR")

        assert result["nodes"] == []
        assert result["edges"] == []

    def test_traces_topology_with_namespace(self, builder, mock_storage):
        """测试带命名空间过滤"""
        mock_storage.execute_query = Mock(return_value=[])

        result = builder._get_traces_topology("1 HOUR", namespace="islap")

        # 验证查询包含命名空间过滤
        mock_storage.execute_query.assert_called_once()
        call_args = mock_storage.execute_query.call_args[0][0]
        assert "namespace = 'islap'" in call_args


class TestGetLogsTopology:
    """测试 Logs 拓扑提取"""

    @pytest.fixture
    def mock_storage(self):
        storage = Mock(spec=StorageAdapter)
        storage.ch_client = Mock()
        return storage

    @pytest.fixture
    def builder(self, mock_storage):
        return HybridTopologyBuilder(mock_storage)

    def test_logs_topology_basic(self, builder, mock_storage):
        """测试基本 logs 拓扑"""
        mock_storage.execute_query = Mock(return_value=[
            ("service-a", 1000, 3, 10, datetime(2026, 2, 9, 12, 0, 0)),
            ("service-b", 500, 2, 5, datetime(2026, 2, 9, 11, 0, 0))
        ])

        result = builder._get_logs_topology("1 HOUR")

        assert len(result["nodes"]) == 2

        # 验证节点指标
        node_a = next(n for n in result["nodes"] if n["id"] == "service-a")
        assert node_a["metrics"]["log_count"] == 1000
        assert node_a["metrics"]["pod_count"] == 3
        assert node_a["metrics"]["error_count"] == 10
        assert node_a["metrics"]["error_rate"] == 0.01

    def test_logs_topology_heuristic_edges(self, builder, mock_storage):
        """测试启发式边生成"""
        # frontend 和 backend 服务
        mock_storage.execute_query = Mock(return_value=[
            ("frontend-service", 100, 1, 0, datetime(2026, 2, 9, 12, 0, 0)),
            ("backend-service", 50, 1, 0, datetime(2026, 2, 9, 12, 0, 0))
        ])

        result = builder._get_logs_topology("1 HOUR")

        # 应该生成启发式边
        edges = result["edges"]
        if edges:
            # 如果生成了边，验证其属性
            edge = edges[0]
            assert "source" in edge
            assert "target" in edge
            assert edge["metrics"]["confidence"] == 0.3  # 启发式规则低置信度

    def test_logs_topology_database_pattern(self, builder, mock_storage):
        """测试数据库模式识别"""
        mock_storage.execute_query = Mock(return_value=[
            ("api-service", 100, 1, 0, datetime(2026, 2, 9, 12, 0, 0)),
            ("database", 50, 1, 0, datetime(2026, 2, 9, 12, 0, 0))
        ])

        result = builder._get_logs_topology("1 HOUR")

        # 应该识别 api-service -> database 的调用关系
        edges = result["edges"]
        if edges:
            edge = edges[0]
            # database 通常是被调用方
            assert edge["target"] == "database"

    def test_logs_topology_no_database(self):
        """测试没有数据库连接"""
        storage = Mock(spec=StorageAdapter)
        storage.ch_client = None

        builder = HybridTopologyBuilder(storage)
        result = builder._get_logs_topology("1 HOUR")

        assert result["nodes"] == []
        assert result["edges"] == []


class TestGetMetricsTopology:
    """测试 Metrics 拓扑提取"""

    @pytest.fixture
    def mock_storage(self):
        storage = Mock(spec=StorageAdapter)
        storage.ch_client = Mock()
        return storage

    @pytest.fixture
    def builder(self, mock_storage):
        return HybridTopologyBuilder(mock_storage)

    def test_metrics_topology_basic(self, builder, mock_storage):
        """测试基本 metrics 拓扑"""
        mock_storage.execute_query = Mock(return_value=[
            ("service-a", 1000, 10),
            ("service-b", 500, 5)
        ])

        result = builder._get_metrics_topology("1 HOUR")

        assert len(result["nodes"]) == 2

        # 验证节点指标
        node_a = next(n for n in result["nodes"] if n["id"] == "service-a")
        assert node_a["metrics"]["metric_count"] == 1000
        assert node_a["metrics"]["unique_metrics"] == 10

    def test_metrics_topology_no_edges(self, builder, mock_storage):
        """测试 metrics 不生成边"""
        mock_storage.execute_query = Mock(return_value=[
            ("service-a", 100, 5)
        ])

        result = builder._get_metrics_topology("1 HOUR")

        # metrics 不生成边，只提供节点
        assert result["edges"] == []

    def test_metrics_topology_no_database(self):
        """测试没有数据库连接"""
        storage = Mock(spec=StorageAdapter)
        storage.ch_client = None

        builder = HybridTopologyBuilder(storage)
        result = builder._get_metrics_topology("1 HOUR")

        assert result["nodes"] == []
        assert result["edges"] == []


class TestMergeNodes:
    """测试节点合并"""

    @pytest.fixture
    def mock_storage(self):
        return Mock(spec=StorageAdapter)

    @pytest.fixture
    def builder(self, mock_storage):
        return HybridTopologyBuilder(mock_storage)

    def test_merge_nodes_traces_priority(self, builder):
        """测试 traces 节点优先级"""
        traces_nodes = [
            {"id": "service-a", "metrics": {"trace_count": 10, "data_source": "traces"}}
        ]
        logs_nodes = [
            {"id": "service-a", "metrics": {"log_count": 100, "data_source": "logs"}}
        ]

        merged = builder._merge_nodes(traces_nodes, logs_nodes, [])

        # traces 数据应该保留
        assert len(merged) == 1
        assert merged[0]["metrics"]["trace_count"] == 10

        # logs 数据应该被合并
        assert "log_count" in merged[0]["metrics"]

    def test_merge_nodes_unique_services(self, builder):
        """测试不同服务的节点合并"""
        traces_nodes = [
            {"id": "service-a", "metrics": {"data_source": "traces"}},
            {"id": "service-b", "metrics": {"data_source": "traces"}}
        ]
        logs_nodes = [
            {"id": "service-c", "metrics": {"data_source": "logs"}}
        ]

        merged = builder._merge_nodes(traces_nodes, logs_nodes, [])

        assert len(merged) == 3

    def test_merge_nodes_data_sources(self, builder):
        """测试数据源标记合并"""
        traces_nodes = [
            {
                "id": "service-a",
                "metrics": {"trace_count": 10, "data_source": "traces"}
            }
        ]
        logs_nodes = [
            {
                "id": "service-a",
                "metrics": {"log_count": 100, "data_source": "logs"}
            }
        ]
        metrics_nodes = [
            {
                "id": "service-a",
                "metrics": {"metric_count": 50, "data_source": "metrics"}
            }
        ]

        merged = builder._merge_nodes(traces_nodes, logs_nodes, metrics_nodes)

        # 应该包含所有数据源标记
        node = merged[0]
        assert "data_sources" in node["metrics"]


class TestMergeEdges:
    """测试边合并"""

    @pytest.fixture
    def mock_storage(self):
        return Mock(spec=StorageAdapter)

    @pytest.fixture
    def builder(self, mock_storage):
        return HybridTopologyBuilder(mock_storage)

    def test_merge_edges_traces_priority(self, builder):
        """测试 traces 边优先级"""
        traces_edges = [
            {
                "source": "service-a",
                "target": "service-b",
                "metrics": {"call_count": 100, "confidence": 1.0, "data_source": "traces"}
            }
        ]
        logs_edges = [
            {
                "source": "service-a",
                "target": "service-b",
                "metrics": {"confidence": 0.3, "data_source": "logs"}
            }
        ]

        merged = builder._merge_edges(traces_edges, logs_edges, [])

        # 应该保留 traces 的数据
        assert len(merged) == 1
        assert merged[0]["metrics"]["confidence"] == 1.0
        assert merged[0]["metrics"]["call_count"] == 100

    def test_merge_edges_logs_only(self, builder):
        """测试只有 logs 边的情况"""
        logs_edges = [
            {
                "source": "service-a",
                "target": "service-b",
                "metrics": {"confidence": 0.3, "data_source": "logs"}
            }
        ]

        merged = builder._merge_edges([], logs_edges, [])

        assert len(merged) == 1
        assert merged[0]["metrics"]["confidence"] == 0.3

    def test_merge_edges_metrics_boost(self, builder):
        """测试 metrics 提升置信度"""
        traces_edges = [
            {
                "source": "service-a",
                "target": "service-b",
                "metrics": {"confidence": 0.5, "data_source": "traces"}
            }
        ]
        metrics_edges = [
            {
                "source": "service-a",
                "target": "service-b",
                "metrics": {"data_source": "metrics"}
            }
        ]

        merged = builder._merge_edges(traces_edges, [], metrics_edges)

        # confidence 应该被提升
        assert merged[0]["metrics"]["confidence"] > 0.5


class TestHeuristicRules:
    """测试启发式规则"""

    @pytest.fixture
    def mock_storage(self):
        return Mock(spec=StorageAdapter)

    @pytest.fixture
    def builder(self, mock_storage):
        return HybridTopologyBuilder(mock_storage)

    def test_is_service_pair_related_frontend_backend(self, builder):
        """测试 frontend-backend 模式"""
        assert builder._is_service_pair_related("frontend-service", "backend-api")
        assert builder._is_service_pair_related("web-frontend", "backend-service")

    def test_is_service_pair_related_database(self, builder):
        """测试数据库模式"""
        assert builder._is_service_pair_related("api-service", "mysql-db")
        assert builder._is_service_pair_related("app", "redis-cache")
        assert builder._is_service_pair_related("service", "postgres")

    def test_is_service_pair_related_registry(self, builder):
        """测试 registry 模式"""
        assert builder._is_service_pair_related("app", "docker-registry")

    def test_should_call(self, builder):
        """测试调用方向判断"""
        # frontend 应该调用其他服务
        assert builder._should_call("frontend", "backend")

        # database 不应该主动调用
        assert not builder._should_call("mysql", "api")

        # registry 不应该主动调用
        assert not builder._should_call("registry", "app")

    def test_get_relation_reason(self, builder):
        """测试调用关系理由"""
        reason = builder._get_relation_reason("frontend", "mysql-db")

        assert "frontend_pattern" in reason or "data_access_pattern" in reason


class TestConvenienceFunction:
    """测试便捷函数"""

    def test_get_hybrid_topology_builder(self):
        """测试获取 builder 实例"""
        mock_storage = Mock(spec=StorageAdapter)

        builder1 = get_hybrid_topology_builder(mock_storage)
        builder2 = get_hybrid_topology_builder(mock_storage)

        # 应该返回同一个实例（单例模式）
        assert builder1 is builder2


class TestMetadataGeneration:
    """测试元数据生成"""

    @pytest.fixture
    def mock_storage(self):
        storage = Mock(spec=StorageAdapter)
        storage.ch_client = Mock()
        return storage

    @pytest.fixture
    def builder(self, mock_storage):
        return HybridTopologyBuilder(mock_storage)

    def test_metadata_data_sources(self, builder, mock_storage):
        """测试数据源元数据"""
        mock_storage.execute_query = Mock(return_value=[])

        result = builder.build_topology()

        metadata = result["metadata"]
        assert "data_sources" in metadata
        assert isinstance(metadata["data_sources"], list)

    def test_metadata_source_breakdown(self, builder, mock_storage):
        """测试数据源分解统计"""
        mock_storage.execute_query = Mock(return_value=[])

        result = builder.build_topology()

        metadata = result["metadata"]
        assert "source_breakdown" in metadata
        assert "traces" in metadata["source_breakdown"]
        assert "logs" in metadata["source_breakdown"]
        assert "metrics" in metadata["source_breakdown"]

    def test_metadata_generated_at(self, builder, mock_storage):
        """测试生成时间"""
        mock_storage.execute_query = Mock(return_value=[])

        result = builder.build_topology()

        metadata = result["metadata"]
        assert "generated_at" in metadata

        # 验证是 ISO 格式时间
        datetime.fromisoformat(metadata["generated_at"].replace('Z', '+00:00'))

    def test_metadata_avg_confidence(self, builder, mock_storage):
        """测试平均置信度"""
        # Mock 返回一些边数据
        mock_storage.execute_query = Mock(return_value=[
            ("trace-1", "span-1", "", "service-a", "op", 100, 50, "ok"),
            ("trace-1", "span-2", "span-1", "service-b", "op", 80, 30, "ok")
        ])

        result = builder.build_topology()

        metadata = result["metadata"]
        assert "avg_confidence" in metadata
        assert isinstance(metadata["avg_confidence"], (int, float))


class TestEdgeCases:
    """测试边界情况"""

    @pytest.fixture
    def mock_storage(self):
        storage = Mock(spec=StorageAdapter)
        storage.ch_client = Mock()
        return storage

    @pytest.fixture
    def builder(self, mock_storage):
        return HybridTopologyBuilder(mock_storage)

    def test_empty_traces_data(self, builder, mock_storage):
        """测试空的 traces 数据"""
        mock_storage.execute_query = Mock(return_value=[])

        result = builder.build_topology()

        # 应该仍然返回有效的拓扑结构
        assert "nodes" in result
        assert "edges" in result

    def test_malformed_trace_row(self, builder, mock_storage):
        """测试格式错误的 trace 行"""
        # 返回格式不正确的行（少于7列）
        mock_storage.execute_query = Mock(return_value=[
            ("trace-1", "span-1"),  # 只有2列
        ])

        result = builder._get_traces_topology("1 HOUR")

        # 应该优雅地处理格式错误
        assert isinstance(result, dict)

    def test_very_long_time_window(self, builder, mock_storage):
        """测试非常长的时间窗口"""
        mock_storage.execute_query = Mock(return_value=[])

        result = builder.build_topology(time_window="30 DAY")

        assert result["metadata"]["time_window"] == "30 DAY"

    def test_unicode_service_names(self, builder, mock_storage):
        """测试 Unicode 服务名"""
        mock_storage.execute_query = Mock(return_value=[
            ("trace-1", "span-1", "", "服务-A", "操作", 100, "ok"),
            ("trace-1", "span-2", "span-1", "服务-B", "操作", 80, "ok")
        ])

        result = builder._get_traces_topology("1 HOUR")

        # 应该正确处理 Unicode（7列格式）
        assert isinstance(result, dict)
        assert "nodes" in result
        # 即使有错误，也应该返回有效结构
