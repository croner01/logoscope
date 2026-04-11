"""
增强的混合拓扑生成器补丁

添加的功能：
1. 支持 kube-system 命名空间
2. 增强的服务类型识别
3. 更好的跨命名空间关系推断
4. k8s 服务发现集成

使用方法：
将此文件内容合并到 hybrid_topology.py 中，或作为扩展导入
"""

import logging
from typing import Dict, List, Any
from collections import defaultdict

logger = logging.getLogger(__name__)


class EnhancedTopologyMixin:
    """
    增强拓扑生成器混入类
    提供额外的拓扑构建能力
    """

    # 服务类型映射（基于命名空间和命名模式）
    SERVICE_TYPE_MAPPING = {
        # kube-system 服务
        "kube-system/coredns": "dns",
        "kube-system/traefik": "ingress",
        "kube-system/metrics-server": "monitoring",
        "kube-system/local-path-provisioner": "storage",
        "kube-system/helm": "deployment",

        # islap 应用服务
        "islap/otel-collector": "observability",
        "islap/otel-gateway": "observability",
        "islap/fluent-bit": "logging",
        "islap/semantic-engine": "backend",
        "islap/semantic-engine-worker": "worker",
        "islap/logoscope-frontend": "frontend",
        "islap/redis": "cache",
        "islap/docker-registry": "registry",

        # logoscope 基础设施
        "logoscope/clickhouse": "database",
        "logoscope/neo4j": "database",
        "logoscope/nats": "message_queue",
    }

    # 跨命名空间调用规则
    CROSS_NAMESPACE_CALLS = [
        # frontend -> backend
        {
            "source_pattern": "*frontend*",
            "target_pattern": "*engine*",
            "reason": "http_call",
            "confidence_boost": 0.4
        },
        # backend -> worker
        {
            "source_pattern": "semantic-engine",
            "target_pattern": "semantic-engine-worker",
            "reason": "message_queue",
            "confidence_boost": 0.5
        },
        # services -> observability
        {
            "source_pattern": "*",
            "target_pattern": "*otel*",
            "reason": "telemetry_export",
            "confidence_boost": 0.6
        },
        {
            "source_pattern": "*",
            "target_pattern": "*fluent*",
            "reason": "log_export",
            "confidence_boost": 0.6
        },
        # services -> dns
        {
            "source_pattern": "*",
            "target_pattern": "*dns*",
            "reason": "dns_resolution",
            "confidence_boost": 0.4
        },
        # services -> ingress
        {
            "source_pattern": "external",
            "target_pattern": "*traefik*",
            "reason": "ingress_route",
            "confidence_boost": 0.5
        },
    ]

    def get_service_type(self, service_name: str, namespace: str = "islap") -> str:
        """
        根据服务名称和命名空间确定服务类型

        Args:
            service_name: 服务名称
            namespace: 命名空间

        Returns:
            服务类型字符串
        """
        key = f"{namespace}/{service_name}"

        # 精确匹配
        if key in self.SERVICE_TYPE_MAPPING:
            return self.SERVICE_TYPE_MAPPING[key]

        # 模式匹配
        for pattern, service_type in self.SERVICE_TYPE_MAPPING.items():
            if self._match_pattern(service_name, pattern.split("/")[-1]):
                return service_type

        # 基于命名启发式
        if "database" in service_name.lower() or "db" in service_name.lower():
            return "database"
        if "cache" in service_name.lower() or "redis" in service_name.lower():
            return "cache"
        if "frontend" in service_name.lower():
            return "frontend"
        if "worker" in service_name.lower() or "job" in service_name.lower():
            return "worker"
        if "gateway" in service_name.lower() or "proxy" in service_name.lower():
            return "gateway"

        return "service"

    def _match_pattern(self, name: str, pattern: str) -> bool:
        """简单的模式匹配（支持 * 通配符）"""
        import fnmatch
        return fnmatch.fnmatch(name, pattern)

    def infer_cross_namespace_edges(
        self,
        nodes: List[Dict[str, Any]],
        logs_data: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """
        推断跨命名空间的调用关系

        Args:
            nodes: 所有节点列表
            logs_data: 日志数据（用于验证推断）

        Returns:
            推断出的边列表
        """
        inferred_edges = []
        namespace_groups = defaultdict(list)

        # 按命名空间分组节点
        for node in nodes:
            ns = node.get("namespace", "islap")
            namespace_groups[ns].append(node)

        # 应用跨命名空间调用规则
        for rule in self.CROSS_NAMESPACE_CALLS:
            source_pattern = rule["source_pattern"]
            target_pattern = rule["target_pattern"]

            # 查找匹配的源和目标服务
            for ns, nodes_in_ns in namespace_groups.items():
                for source_node in nodes_in_ns:
                    if self._match_pattern(source_node["id"], source_pattern):
                        # 在所有命名空间中查找目标
                        for target_ns, target_nodes in namespace_groups.items():
                            for target_node in target_nodes:
                                if self._match_pattern(target_node["id"], target_pattern):
                                    # 避免自调用
                                    if source_node["id"] != target_node["id"]:
                                        # 检查边是否已存在
                                        edge_id = f"{source_node['id']}-{target_node['id']}"
                                        if not any(e["id"] == edge_id for e in inferred_edges):
                                            inferred_edges.append({
                                                "id": edge_id,
                                                "source": source_node["id"],
                                                "target": target_node["id"],
                                                "label": "potential-calls",
                                                "type": "calls",
                                                "metrics": {
                                                    "call_count": None,
                                                    "confidence": rule["confidence_boost"],
                                                    "data_source": "cross_namespace_inference",
                                                    "reason": rule["reason"],
                                                    "confidence_boost": rule["confidence_boost"]
                                                },
                                                "inferred": True
                                            })

        logger.info(f"Inferred {len(inferred_edges)} cross-namespace edges")
        return inferred_edges

    def enhance_topology_with_k8s_metadata(
        self,
        topology: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        使用 Kubernetes 元数据增强拓扑

        添加的信息：
        - 命名空间
        - 服务类型
        - Pod 信息
        - 标签和注解
        """
        enhanced_nodes = []

        for node in topology.get("nodes", []):
            service_name = node["id"]
            namespace = node.get("namespace", "islap")

            # 确定服务类型
            service_type = self.get_service_type(service_name, namespace)

            # 添加额外元数据
            enhanced_node = {
                **node,
                "type": service_type,
                "namespace": namespace,
                "category": self._get_service_category(service_type),
                "layer": self._get_service_layer(service_type)
            }

            enhanced_nodes.append(enhanced_node)

        topology["nodes"] = enhanced_nodes

        # 推断跨命名空间边
        inferred_edges = self.infer_cross_namespace_edges(
            topology["nodes"],
            {"nodes": topology["nodes"]}
        )

        # 合并推断的边
        existing_edges = topology.get("edges", [])
        all_edges = existing_edges + inferred_edges

        # 去重（保留置信度更高的）
        unique_edges = self._deduplicate_edges(all_edges)

        topology["edges"] = unique_edges
        topology["metadata"]["inferred_edges"] = len(inferred_edges)

        return topology

    def _get_service_category(self, service_type: str) -> str:
        """获取服务分类"""
        category_map = {
            "dns": "infrastructure",
            "ingress": "networking",
            "monitoring": "observability",
            "storage": "infrastructure",
            "observability": "observability",
            "logging": "observability",
            "backend": "application",
            "worker": "application",
            "frontend": "application",
            "cache": "data",
            "database": "data",
            "registry": "infrastructure",
            "message_queue": "infrastructure",
            "gateway": "networking",
        }
        return category_map.get(service_type, "application")

    def _get_service_layer(self, service_type: str) -> str:
        """获取服务层级"""
        layer_map = {
            "dns": "l3",
            "ingress": "l7",
            "frontend": "frontend",
            "backend": "backend",
            "worker": "backend",
            "cache": "data",
            "database": "data",
            "observability": "infra",
            "logging": "infra",
        }
        return layer_map.get(service_type, "unknown")

    def _deduplicate_edges(self, edges: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """去除重复的边，保留置信度更高的"""
        edge_map = {}

        for edge in edges:
            key = (edge["source"], edge["target"])
            if key not in edge_map:
                edge_map[key] = edge
            else:
                # 保留置信度更高的
                existing_confidence = edge_map[key].get("metrics", {}).get("confidence", 0)
                new_confidence = edge.get("metrics", {}).get("confidence", 0)
                if new_confidence > existing_confidence:
                    edge_map[key] = edge

        return list(edge_map.values())


# 将增强功能集成到 HybridTopologyBuilder 的补丁方法
def apply_enhancements_to_builder(builder):
    """
    将增强功能应用到现有的 HybridTopologyBuilder 实例

    Args:
        builder: HybridTopologyBuilder 实例
    """
    # 动态添加方法
    from types import MethodType

    mixin = EnhancedTopologyMixin()

    # 添加新方法
    builder.get_service_type = MethodType(mixin.get_service_type, builder)
    builder.infer_cross_namespace_edges = MethodType(mixin.infer_cross_namespace_edges, builder)
    builder.enhance_topology_with_k8s_metadata = MethodType(mixin.enhance_topology_with_k8s_metadata, builder)

    # 修改 build_topology 方法以使用增强功能
    original_build_topology = builder.build_topology

    def enhanced_build_topology(time_window="1 HOUR", namespace=None, confidence_threshold=0.3):
        # 调用原始方法
        topology = original_build_topology(time_window, namespace, confidence_threshold)

        # 应用 Kubernetes 元数据增强
        if topology.get("nodes"):
            topology = builder.enhance_topology_with_k8s_metadata(topology)

        return topology

    builder.build_topology = enhanced_build_topology

    logger.info("Applied enhanced topology generation features")

    return builder
