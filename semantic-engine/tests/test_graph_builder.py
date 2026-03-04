"""
Graph Builder 模块单元测试

测试 graph/builder.py 的核心功能：
- 图构建器初始化
- 节点和边的添加
- 从事件列表构建拓扑
- 从 traces 表构建拓扑
- 图结构获取
"""
import pytest
from unittest.mock import Mock, patch

from graph.builder import GraphBuilder, build_graph


class TestGraphBuilderInit:
    """测试 GraphBuilder 初始化"""

    def test_init_without_storage(self):
        """测试无 storage adapter 初始化"""
        builder = GraphBuilder()
        assert builder.nodes == set()
        assert builder.edges == []
        assert builder.storage is None

    def test_init_with_storage(self):
        """测试带 storage adapter 初始化"""
        mock_storage = Mock()
        builder = GraphBuilder(mock_storage)
        assert builder.storage == mock_storage
        assert builder.nodes == set()
        assert builder.edges == []


class TestAddNode:
    """测试节点添加"""

    @pytest.fixture
    def builder(self):
        return GraphBuilder()

    def test_add_node_with_defaults(self, builder):
        """测试使用默认类型添加节点"""
        builder.add_node("service-a")

        assert ("service-a", "service") in builder.nodes
        assert len(builder.nodes) == 1

    def test_add_node_with_custom_type(self, builder):
        """测试添加自定义类型节点"""
        builder.add_node("database-1", "database")

        assert ("database-1", "database") in builder.nodes

    def test_add_duplicate_node(self, builder):
        """测试添加重复节点（自动去重）"""
        builder.add_node("service-a")
        builder.add_node("service-a")  # 添加相同节点

        assert len(builder.nodes) == 1  # 集合自动去重

    def test_add_multiple_nodes(self, builder):
        """测试添加多个节点"""
        builder.add_node("service-a")
        builder.add_node("service-b")
        builder.add_node("service-c")

        assert len(builder.nodes) == 3


class TestAddEdge:
    """测试边添加"""

    @pytest.fixture
    def builder(self):
        return GraphBuilder()

    def test_add_edge(self, builder):
        """测试添加边"""
        builder.add_edge("service-a", "service-b", "calls")

        assert len(builder.edges) == 1
        assert builder.edges[0] == ("service-a", "service-b", "calls")

    def test_add_duplicate_edge(self, builder):
        """测试添加重复边（自动去重）"""
        builder.add_edge("service-a", "service-b", "calls")
        builder.add_edge("service-a", "service-b", "calls")  # 添加相同边

        assert len(builder.edges) == 1  # 自动去重

    def test_add_different_edges(self, builder):
        """测试添加不同的边"""
        builder.add_edge("service-a", "service-b", "calls")
        builder.add_edge("service-b", "service-c", "calls")

        assert len(builder.edges) == 2

    def test_add_edge_with_different_types(self, builder):
        """测试添加不同类型的边"""
        builder.add_edge("service-a", "service-b", "calls")
        builder.add_edge("service-a", "service-b", "depends_on")

        # 相同节点对，不同类型应该都被添加
        assert len(builder.edges) == 2


class TestBuildFromEvents:
    """测试从事件列表构建拓扑"""

    @pytest.fixture
    def builder(self):
        return GraphBuilder()

    @pytest.fixture
    def sample_events(self):
        """示例事件列表"""
        return [
            {
                "entity": {"name": "service-a"},
                "relations": [
                    {"target": "service-b", "type": "calls"}
                ]
            },
            {
                "entity": {"name": "service-b"},
                "relations": [
                    {"target": "service-c", "type": "calls"}
                ]
            },
            {
                "entity": {"name": "service-c"},
                "relations": []
            }
        ]

    def test_build_from_events_basic(self, builder, sample_events):
        """测试基本的事件构建"""
        builder.build_from_events(sample_events)

        # 应该有3个节点
        assert len(builder.nodes) == 3
        assert ("service-a", "service") in builder.nodes
        assert ("service-b", "service") in builder.nodes
        assert ("service-c", "service") in builder.nodes

        # 应该有2条边
        assert len(builder.edges) == 2

    def test_build_from_events_without_entity(self, builder):
        """测试没有 entity 字段的事件"""
        events = [
            {
                "relations": [
                    {"target": "service-b", "type": "calls"}
                ]
            }
        ]

        builder.build_from_events(events)

        # 没有源服务，不应该添加节点或边
        assert len(builder.nodes) == 0
        assert len(builder.edges) == 0

    def test_build_from_events_without_relations(self, builder):
        """测试没有 relations 的事件"""
        events = [
            {
                "entity": {"name": "service-a"},
                "relations": []
            }
        ]

        builder.build_from_events(events)

        # 应该有节点但没有边
        assert len(builder.nodes) == 1
        assert len(builder.edges) == 0

    def test_build_from_events_empty_list(self, builder):
        """测试空事件列表"""
        builder.build_from_events([])

        assert len(builder.nodes) == 0
        assert len(builder.edges) == 0

    def test_build_from_events_multiple_relations(self, builder):
        """测试一个事件有多个关系"""
        events = [
            {
                "entity": {"name": "service-a"},
                "relations": [
                    {"target": "service-b", "type": "calls"},
                    {"target": "service-c", "type": "calls"},
                    {"target": "service-d", "type": "depends_on"}
                ]
            }
        ]

        builder.build_from_events(events)

        # 应该有4个节点（service-a + 3个目标）
        assert len(builder.nodes) == 4

        # 应该有3条边
        assert len(builder.edges) == 3


class TestBuildFromTraces:
    """测试从 traces 表构建拓扑"""

    @pytest.fixture
    def mock_storage(self):
        """Mock storage adapter"""
        storage = Mock()
        storage.ch_client = Mock()
        return storage

    def test_build_from_traces_without_storage(self):
        """测试没有 storage adapter 的情况"""
        builder = GraphBuilder(None)
        result = builder.build_from_traces()

        assert result["nodes"] == []
        assert result["edges"] == []

    def test_build_from_traces_without_client(self, mock_storage):
        """测试没有数据库客户端的情况"""
        builder = GraphBuilder(mock_storage)
        mock_storage.ch_client = None

        result = builder.build_from_traces()

        assert result["nodes"] == []
        assert result["edges"] == []

    def test_build_from_traces_with_data(self, mock_storage):
        """测试有 traces 数据的情况"""
        # Mock 查询结果
        mock_storage.ch_client.execute = Mock(return_value=[
            ("service-a", "operation-1", 100, 50.0, 0.0),
            ("service-a", "operation-2", 50, 80.0, 2.0)
        ])

        builder = GraphBuilder(mock_storage)
        result = builder.build_from_traces()

        # 应该有节点和边
        assert "nodes" in result
        assert "edges" in result
        assert len(result["nodes"]) >= 2
        assert len(result["edges"]) >= 2

    def test_build_from_traces_enhances_nodes(self, mock_storage):
        """测试节点元数据增强"""
        mock_storage.ch_client.execute = Mock(return_value=[
            ("service-a", "operation-1", 100, 50.0, 0.0)
        ])

        builder = GraphBuilder(mock_storage)
        result = builder.build_from_traces()

        # 验证节点有元数据
        for node in result["nodes"]:
            assert "id" in node
            assert "type" in node
            if node["type"] == "service":
                assert "name" in node
                assert "metadata" in node

    def test_build_from_traces_handles_error(self, mock_storage):
        """测试错误处理"""
        # Mock 执行查询时抛出异常
        mock_storage.ch_client.execute = Mock(
            side_effect=Exception("Database error")
        )

        builder = GraphBuilder(mock_storage)
        result = builder.build_from_traces()

        # 应该返回空结果而不是崩溃
        assert result["nodes"] == []
        assert result["edges"] == []

    def test_build_from_traces_with_limit(self, mock_storage):
        """测试 limit 参数"""
        mock_storage.ch_client.execute = Mock(return_value=[])

        builder = GraphBuilder(mock_storage)
        result = builder.build_from_traces(limit=100)

        # 验证查询包含 limit
        mock_storage.ch_client.execute.assert_called_once()
        query = mock_storage.ch_client.execute.call_args[0][0]
        assert "LIMIT 100" in query


class TestGetGraph:
    """测试获取图结构"""

    @pytest.fixture
    def builder(self):
        builder = GraphBuilder()
        # 添加一些测试数据
        builder.add_node("service-a")
        builder.add_node("service-b")
        builder.add_edge("service-a", "service-b", "calls")
        return builder

    def test_get_graph_structure(self, builder):
        """测试返回的图结构"""
        graph = builder.get_graph()

        assert "nodes" in graph
        assert "edges" in graph

        # 验证节点结构
        assert len(graph["nodes"]) == 2
        # 使用更稳健的断言，不依赖顺序
        node_ids = {node["id"] for node in graph["nodes"]}
        assert node_ids == {"service-a", "service-b"}

        # 验证边结构
        assert len(graph["edges"]) == 1
        assert graph["edges"][0] == {
            "source": "service-a",
            "target": "service-b",
            "type": "calls"
        }

    def test_get_graph_empty(self):
        """测试空图的获取"""
        builder = GraphBuilder()
        graph = builder.get_graph()

        assert graph["nodes"] == []
        assert graph["edges"] == []


class TestBuildGraph:
    """测试便捷函数 build_graph"""

    def test_build_graph_from_events(self):
        """测试从事件列表构建图"""
        events = [
            {
                "entity": {"name": "service-a"},
                "relations": [
                    {"target": "service-b", "type": "calls"}
                ]
            }
        ]

        graph = build_graph(events)

        assert len(graph["nodes"]) == 2
        assert len(graph["edges"]) == 1

    def test_build_graph_with_storage(self):
        """测试带 storage 的图构建"""
        events = [
            {
                "entity": {"name": "service-a"},
                "relations": []
            }
        ]

        mock_storage = Mock()
        mock_storage.ch_client = Mock()
        mock_storage.ch_client.execute = Mock(return_value=[])

        graph = build_graph(events, mock_storage)

        # 应该包含事件构建的节点
        assert len(graph["nodes"]) >= 1

    def test_build_graph_empty_events(self):
        """测试空事件列表"""
        graph = build_graph([])

        assert graph["nodes"] == []
        assert graph["edges"] == []


class TestEdgeCases:
    """测试边界情况"""

    def test_event_with_missing_target(self):
        """测试关系缺少目标"""
        builder = GraphBuilder()
        events = [
            {
                "entity": {"name": "service-a"},
                "relations": [
                    {"type": "calls"}  # 缺少 target
                ]
            }
        ]

        builder.build_from_events(events)

        # 应该有节点但没有边
        assert len(builder.nodes) == 1
        assert len(builder.edges) == 0

    def test_event_with_missing_relation_type(self):
        """测试关系缺少类型"""
        builder = GraphBuilder()
        events = [
            {
                "entity": {"name": "service-a"},
                "relations": [
                    {"target": "service-b"}  # 缺少 type
                ]
            }
        ]

        builder.build_from_events(events)

        # 应该有节点和边（edge_type为None）
        assert len(builder.nodes) == 2
        assert len(builder.edges) == 1

    def test_node_types_variety(self):
        """测试不同类型的节点"""
        builder = GraphBuilder()
        builder.add_node("service-1", "service")
        builder.add_node("database-1", "database")
        builder.add_node("cache-1", "cache")

        assert len(builder.nodes) == 3
        assert ("service-1", "service") in builder.nodes
        assert ("database-1", "database") in builder.nodes
        assert ("cache-1", "cache") in builder.nodes

    def test_edge_types_variety(self):
        """测试不同类型的边"""
        builder = GraphBuilder()
        builder.add_edge("a", "b", "calls")
        builder.add_edge("a", "c", "depends_on")
        builder.add_edge("b", "d", "error")

        assert len(builder.edges) == 3

        # 验证每种类型的边
        edge_types = [edge[2] for edge in builder.edges]
        assert "calls" in edge_types
        assert "depends_on" in edge_types
        assert "error" in edge_types

    def test_self_referencing_edge(self):
        """测试自引用边"""
        builder = GraphBuilder()
        builder.add_edge("service-a", "service-a", "calls")

        assert len(builder.edges) == 1
        assert builder.edges[0] == ("service-a", "service-a", "calls")

    def test_large_number_of_nodes(self):
        """测试大量节点"""
        builder = GraphBuilder()
        node_count = 1000

        for i in range(node_count):
            builder.add_node(f"service-{i}")

        assert len(builder.nodes) == node_count

    def test_large_number_of_edges(self):
        """测试大量边"""
        builder = GraphBuilder()
        edge_count = 500

        for i in range(edge_count):
            builder.add_edge(f"service-{i}", f"service-{i+1}", "calls")

        assert len(builder.edges) == edge_count


class TestGraphIntegrity:
    """测试图完整性"""

    def test_orphan_nodes(self):
        """测试孤立节点（没有边的节点）"""
        builder = GraphBuilder()
        builder.add_node("service-a")
        builder.add_node("service-b")
        builder.add_edge("service-a", "service-b", "calls")
        builder.add_node("service-c")  # 孤立节点

        graph = builder.get_graph()

        # service-c 应该在节点列表中
        node_ids = [n["id"] for n in graph["nodes"]]
        assert "service-c" in node_ids

    def test_edge_to_nonexistent_node(self):
        """测试指向不存在节点的边"""
        builder = GraphBuilder()
        builder.add_edge("service-a", "service-b", "calls")

        # add_edge 不会自动添加节点到 nodes 集合
        # 需要手动添加节点
        assert len(builder.nodes) == 0

        # 边仍然被添加
        assert len(builder.edges) == 1
