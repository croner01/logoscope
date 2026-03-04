"""
Semantic Engine Graph 模块
负责从事件关系构建服务拓扑图
"""
from typing import Dict, Any, List, Set, Optional
import logging
import os
import re


logger = logging.getLogger(__name__)


def _sanitize_interval(time_window: str, default_value: str = "24 HOUR") -> str:
    """规范化 INTERVAL 参数，避免 SQL 注入。"""
    pattern = re.compile(r"^\s*(\d+)\s+([A-Za-z]+)\s*$")
    match = pattern.match(str(time_window or ""))
    if not match:
        return default_value
    amount = int(match.group(1))
    unit_raw = match.group(2).upper()
    valid_units = {
        "MINUTE": "MINUTE",
        "MINUTES": "MINUTE",
        "HOUR": "HOUR",
        "HOURS": "HOUR",
        "DAY": "DAY",
        "DAYS": "DAY",
        "WEEK": "WEEK",
        "WEEKS": "WEEK",
    }
    if amount <= 0 or unit_raw not in valid_units:
        return default_value
    return f"{amount} {valid_units[unit_raw]}"


def _sanitize_limit(value: Any, default_value: int = 1000, max_value: int = 20000) -> int:
    """限制 LIMIT 范围，避免异常值扩大查询压力。"""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default_value
    if parsed < 1:
        return 1
    return min(parsed, max_value)


class GraphBuilder:
    """
    拓扑图构建器

    基于事件关系构建服务拓扑图，管理节点和边
    """

    def __init__(self, storage_adapter=None):
        """
        初始化拓扑图构建器

        Args:
            storage_adapter: StorageAdapter实例，用于访问数据库
        """
        # 节点集合，存储 (node_id, node_type) 元组
        self.nodes = set()
        # 边列表，存储 (source, target, edge_type) 元组
        self.edges = []
        # 存储适配器（用于访问ClickHouse traces表）
        self.storage = storage_adapter
    
    def add_node(self, node_id: str, node_type: str = "service"):
        """
        添加节点到图中
        
        Args:
            node_id: 节点ID（通常是服务名）
            node_type: 节点类型，默认为 "service"
        """
        # 将节点添加到集合中（自动去重）
        self.nodes.add((node_id, node_type))
    
    def add_edge(self, source: str, target: str, edge_type: str):
        """
        添加边到图中
        
        Args:
            source: 源节点ID
            target: 目标节点ID
            edge_type: 边类型（如 depends_on、calls、error）
        """
        # 创建边元组
        edge = (source, target, edge_type)
        # 如果边不存在，则添加（自动去重）
        if edge not in self.edges:
            self.edges.append(edge)
    
    def build_from_events(self, events: List[Dict[str, Any]]):
        """
        从事件列表构建拓扑图

        Args:
            events: 事件列表，每个事件可能包含关系信息
        """
        # 遍历所有事件
        for event in events:
            # 提取源服务名
            source_service = event.get("entity", {}).get("name")

            # 如果有源服务名，添加为节点
            if source_service:
                self.add_node(source_service)

            # 遍历事件中的所有关系
            for relation in event.get("relations", []):
                # 提取目标服务名和关系类型
                target = relation.get("target")
                relation_type = relation.get("type")

                # 如果源服务和目标都存在，添加节点和边
                if source_service and target:
                    self.add_node(target)
                    self.add_edge(source_service, target, relation_type)

    def build_from_traces(self, limit: int = 1000) -> Dict[str, Any]:
        """
        ⭐ P1新增：从ClickHouse traces表构建服务拓扑图

        基于Trace ID分析服务调用关系，自动构建服务拓扑

        Args:
            limit: 读取的traces数量限制

        Returns:
            Dict[str, Any]: 拓扑图结构，包含节点和边
        """
        if not self.storage or not self.storage.ch_client:
            # 返回空图
            return {"nodes": [], "edges": []}

        try:
            safe_limit = _sanitize_limit(limit, default_value=1000, max_value=20000)
            safe_time_window = _sanitize_interval(
                os.getenv("TOPOLOGY_BUILDER_TIME_WINDOW", "24 HOUR"),
                default_value="24 HOUR",
            )
            # 从traces表查询服务调用关系
            query = f"""
                SELECT
                    service_name as from_service,
                    operation_name as to_service,
                    COUNT(*) as call_count,
                    AVG(duration_ms) as avg_duration_ms,
                    SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) * 100.0 / COUNT(*) as error_rate
                FROM logs.traces
                PREWHERE timestamp > now() - INTERVAL {safe_time_window}
                    AND length(trace_id) > 0
                GROUP BY service_name, operation_name
                ORDER BY call_count DESC
                LIMIT {safe_limit}
            """

            result = self.storage.ch_client.execute(query)

            # 构建节点和边
            services_seen = set()

            for row in result:
                from_service, to_service, call_count, avg_duration, error_rate = row

                # 添加服务节点
                self.add_node(from_service)
                self.add_node(to_service)

                # 添加调用关系边
                self.add_edge(from_service, to_service, "calls")

                # 记录服务
                services_seen.add(from_service)
                services_seen.add(to_service)

            # 增强节点元数据
            enhanced_nodes = []
            for node_id, node_type in self.nodes:
                node = {"id": node_id, "type": node_type}

                # 添加节点元数据（如果需要）
                if node_type == "service":
                    # 可以从traces统计中获取更多信息
                    node["name"] = node_id
                    node["metadata"] = {
                        "type": "service",
                        "label": node_id
                    }

                enhanced_nodes.append(node)

            return {
                "nodes": enhanced_nodes,
                "edges": self.edges
            }

        except Exception as e:
            # 记录错误但不中断服务
            logger.warning(f"Failed to build topology FROM logs.traces: {e}")
            return {"nodes": [], "edges": []}
    
    def get_graph(self) -> Dict[str, Any]:
        """
        获取构建的图
        
        Returns:
            Dict[str, Any]: 图结构，包含：
                - nodes: 节点列表，每个节点包含 id 和 type
                - edges: 边列表，每条边包含 source、target 和 type
        """
        # 构建节点列表
        nodes = [
            {"id": node_id, "type": node_type}
            for node_id, node_type in self.nodes
        ]
        
        # 构建边列表
        edges = [
            {
                "source": source,
                "target": target,
                "type": edge_type
        }
            for source, target, edge_type in self.edges
        ]
        
        # 返回图结构
        return {
                "nodes": nodes,
                "edges": edges
        }


def build_graph(events: List[Dict[str, Any]], storage_adapter=None) -> Dict[str, Any]:
    """
    从事件列表构建拓扑图

    Args:
        events: 事件列表，每个事件可能包含关系信息
        storage_adapter: StorageAdapter实例（可选，用于访问traces表）

    Returns:
        Dict[str, Any]: 拓扑图结构，包含节点和边
    """
    # 创建拓扑图构建器实例
    builder = GraphBuilder(storage_adapter)

    # 从事件列表构建拓扑图
    builder.build_from_events(events)

    # ⭐ P1新增：尝试从traces表增强拓扑图
    if storage_adapter:
        trace_graph = builder.build_from_traces()
        if trace_graph and trace_graph["nodes"]:
            # 合并traces图的节点和边
            for node in trace_graph["nodes"]:
                builder.add_node(node["id"], node.get("type", "service"))
            for edge in trace_graph["edges"]:
                builder.add_edge(edge["source"], edge["target"], edge["type"])

    # 获取并返回构建的图
    return builder.get_graph()
