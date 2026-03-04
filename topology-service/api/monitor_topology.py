"""
系统监控拓扑图 API

提供用于前端可视化的拓扑数据，支持：
- 三栏泳道布局（客户端层 | 业务服务层 | 基础设施层）
- 节点状态健康度
- 连线动态样式
- 实时数据刷新

Date: 2026-02-11
"""

import asyncio
from fastapi import APIRouter, Query, HTTPException
from typing import Dict, List, Any, Optional, Literal
import logging
from datetime import datetime, timedelta, timezone

from graph.enhanced_topology import get_enhanced_topology_builder

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/monitor", tags=["monitor-topology"])

# 服务层级分类规则
SERVICE_LAYER_RULES = {
    "client": ["frontend", "web", "mobile", "app", "ui", "portal", "dashboard"],
    "business": ["service", "api", "backend", "worker", "processor", "handler", "controller"],
    "infrastructure": ["database", "db", "cache", "redis", "mysql", "postgres", "mongodb", "clickhouse", "kafka", "rabbitmq", "nats", "collector", "gateway"]
}

# 健康状态阈值
HEALTH_THRESHOLDS = {
    "healthy": {"error_rate": 0.01, "min_instances": 1},      # 错误率 < 1%, 至少1个健康实例
    "warning": {"error_rate": 0.05, "min_instances": 1},      # 错误率 1-5%
    "error": {"error_rate": 0.10, "min_instances": 0},        # 错误率 > 5%
    "unknown": {"error_rate": None, "min_instances": 0}       # 无数据
}

# 颜色配置
STATUS_COLORS = {
    "healthy": "#1890ff",    # 蓝色
    "warning": "#fa8c16",    # 橙色
    "error": "#f5222d",      # 红色
    "unknown": "#d9d9d9"     # 灰色
}

# 全局 storage adapter
storage = None


async def _run_blocking(func, *args, **kwargs):
    """Execute blocking topology builder calls in thread pool."""
    return await asyncio.to_thread(func, *args, **kwargs)


def set_storage_adapter(storage_adapter):
    """设置 storage adapter"""
    global storage
    storage = storage_adapter


def _classify_service_layer(service_name: str) -> str:
    """
    将服务分类到三层架构

    Args:
        service_name: 服务名称

    Returns:
        "client" | "business" | "infrastructure"
    """
    name_lower = service_name.lower()

    # 优先检查基础设施层
    for keyword in SERVICE_LAYER_RULES["infrastructure"]:
        if keyword in name_lower:
            return "infrastructure"

    # 检查客户端层
    for keyword in SERVICE_LAYER_RULES["client"]:
        if keyword in name_lower:
            return "client"

    # 默认业务层
    return "business"


def _calculate_node_health(node: Dict[str, Any]) -> str:
    """
    计算节点健康状态

    Args:
        node: 节点数据

    Returns:
        "healthy" | "warning" | "error" | "unknown"
    """
    metrics = node.get("metrics", {})
    error_rate = metrics.get("error_rate", 0)
    total_instances = metrics.get("instance_count", 1)
    healthy_instances = metrics.get("healthy_instance_count", total_instances)

    # 检查是否有任何实例运行
    if healthy_instances == 0:
        return "error"

    # 基于错误率判断
    if error_rate < HEALTH_THRESHOLDS["healthy"]["error_rate"]:
        return "healthy"
    elif error_rate < HEALTH_THRESHOLDS["warning"]["error_rate"]:
        return "warning"
    else:
        return "error"


def _calculate_edge_health(edge: Dict[str, Any]) -> str:
    """
    计算边的健康状态

    Args:
        edge: 边数据

    Returns:
        "healthy" | "warning" | "error" | "unknown"
    """
    metrics = edge.get("metrics", {})
    error_rate = metrics.get("error_rate", 0)
    p99_latency = metrics.get("p99_latency_ms", 0)

    # 综合错误率和延迟判断
    if error_rate < 0.01 and p99_latency < 1000:
        return "healthy"
    elif error_rate < 0.05 and p99_latency < 2000:
        return "warning"
    else:
        return "error"


def _calculate_edge_width(edge: Dict[str, Any]) -> float:
    """
    根据调用量计算边宽度

    Args:
        edge: 边数据

    Returns:
        宽度 (0.5 - 4.0 px)
    """
    metrics = edge.get("metrics", {})
    call_count = metrics.get("call_count", 0)
    qps = metrics.get("qps", call_count / 60)  # 假设1分钟窗口

    # 使用对数缩放，避免高QPS导致线条过粗
    if qps <= 0:
        return 0.5

    width = 0.5 + min(3.5, (qps ** 0.3) * 0.8)
    return round(width, 2)


def _calculate_node_position(
    service_name: str,
    layer: str,
    layer_index: int,
    layer_count: int
) -> Dict[str, float]:
    """
    计算节点在泳道布局中的位置

    Args:
        service_name: 服务名
        layer: 所属层
        layer_index: 层内索引
        layer_count: 层内节点数

    Returns:
        {"x": x, "y": y} 坐标
    """
    # 三栏布局 x 坐标
    layer_x_positions = {
        "client": 200,           # 客户端层
        "business": 600,         # 业务层
        "infrastructure": 1000   # 基础设施层
    }

    x = layer_x_positions.get(layer, 600)

    # y 坐标均匀分布
    y_spacing = 150
    y_start = 100
    y = y_start + (layer_index * y_spacing)

    return {"x": x, "y": y}


@router.get("/topology")
async def get_monitor_topology(
    time_window: str = Query("5 MINUTE", description="时间窗口（如 '5 MINUTE', '30 MINUTE', '1 HOUR'）"),
    namespace: str = Query(None, description="命名空间过滤"),
    include_metrics: bool = Query(True, description="是否包含详细指标"),
    auto_refresh: bool = Query(False, description="是否启用自动刷新模式")
) -> Dict[str, Any]:
    """
    获取监控拓扑图数据

    返回格式化的拓扑数据，包含：
    - 三栏泳道布局的节点位置
    - 节点健康状态和颜色
    - 边宽度和颜色
    - 实时指标

    参数:
        - time_window: 时间窗口
        - namespace: 命名空间过滤
        - include_metrics: 是否包含详细指标
        - auto_refresh: 是否启用自动刷新

    返回:
        {
            "layout": {
                "type": "swimlane",
                "lanes": [
                    {"id": "client", "label": "客户端层", "x": 200},
                    {"id": "business", "label": "业务服务层", "x": 600},
                    {"id": "infrastructure", "label": "基础设施层", "x": 1000}
                ]
            },
            "nodes": [
                {
                    "id": "frontend",
                    "label": "Frontend Service",
                    "layer": "client",
                    "position": {"x": 200, "y": 100},
                    "size": {"width": 200, "height": 120},
                    "status": "healthy",
                    "color": "#1890ff",
                    "metrics": {
                        "qps": 150,
                        "avg_rt": 45,
                        "error_rate": 0.002,
                        "instance_count": 3,
                        "healthy_instance_count": 3
                    },
                    "interactions": {
                        "hover": "show_details",
                        "click": "drill_down"
                    }
                }
            ],
            "edges": [
                {
                    "id": "frontend-backend",
                    "source": "frontend",
                    "target": "backend-api",
                    "label": "150 QPS",
                    "type": "calls",
                    "width": 2.5,
                    "color": "#52c41a",
                    "style": "solid",
                    "animated": false,
                    "metrics": {
                        "qps": 150,
                        "avg_latency_ms": 45,
                        "p99_latency_ms": 120,
                        "error_rate": 0.002
                    }
                }
            ],
            "metadata": {
                "time_window": "5 MINUTE",
                "generated_at": "2026-02-11T12:00:00Z",
                "node_count": 10,
                "edge_count": 15,
                "auto_refresh_interval": 5
            }
        }

    健康状态判定：
        - healthy: 错误率 < 1%, 至少1个健康实例 (蓝色 #1890ff)
        - warning: 错误率 1-5% (橙色 #fa8c16)
        - error: 错误率 > 5% 或无健康实例 (红色 #f5222d)
        - unknown: 无数据 (灰色 #d9d9d9)
    """
    try:
        global storage

        if not storage:
            raise HTTPException(status_code=500, detail="Storage adapter not initialized")

        # 构建拓扑数据
        builder = get_enhanced_topology_builder(storage)
        if not builder:
            raise HTTPException(status_code=500, detail="Topology builder not initialized")

        topology = await _run_blocking(
            builder.build_topology,
            time_window=time_window,
            namespace=namespace,
            confidence_threshold=0.3,
        )

        raw_nodes = topology.get("nodes", [])
        raw_edges = topology.get("edges", [])

        # 分类节点到层
        layers = {
            "client": [],
            "business": [],
            "infrastructure": []
        }

        for node in raw_nodes:
            layer = _classify_service_layer(node["id"])
            layers[layer].append(node)

        # 构建格式化的节点数据
        formatted_nodes = []
        for layer_name, layer_nodes in layers.items():
            for idx, node in enumerate(layer_nodes):
                health = _calculate_node_health(node)

                formatted_node = {
                    "id": node["id"],
                    "label": node.get("label", node["id"]),
                    "layer": layer_name,
                    "position": _calculate_node_position(
                        node["id"],
                        layer_name,
                        idx,
                        len(layer_nodes)
                    ),
                    "size": {"width": 200, "height": 120},
                    "status": health,
                    "color": STATUS_COLORS[health],
                    "metrics": {
                        "qps": node.get("metrics", {}).get("qps", 0),
                        "avg_rt": node.get("metrics", {}).get("avg_duration", 0),
                        "error_rate": node.get("metrics", {}).get("error_rate", 0),
                        "instance_count": node.get("metrics", {}).get("instance_count", 1),
                        "healthy_instance_count": node.get("metrics", {}).get("healthy_instance_count", 1)
                    },
                    "interactions": {
                        "hover": "show_details",
                        "click": "drill_down"
                    }
                }

                # 可选：包含更多详细指标
                if include_metrics:
                    formatted_node["metrics"].update({
                        "log_count": node.get("metrics", {}).get("log_count", 0),
                        "trace_count": node.get("metrics", {}).get("trace_count", 0),
                        "span_count": node.get("metrics", {}).get("span_count", 0)
                    })

                formatted_nodes.append(formatted_node)

        # 构建格式化的边数据
        formatted_edges = []
        for edge in raw_edges:
            edge_health = _calculate_edge_health(edge)
            edge_width = _calculate_edge_width(edge)

            # 根据健康状态决定颜色
            edge_color_map = {
                "healthy": "#52c41a",  # 绿色
                "warning": "#faad14",  # 黄色
                "error": "#ff4d4f",    # 红色
                "unknown": "#d9d9d9"   # 灰色
            }

            # 根据 P99 延迟调整颜色
            metrics = edge.get("metrics", {})
            p99_latency = metrics.get("p99_latency_ms", 0)
            if p99_latency > 2000:
                edge_color = "#ff4d4f"  # 高延迟红色
                animated = True
            elif p99_latency > 1000:
                edge_color = "#faad14"  # 中延迟黄色
                animated = False
            else:
                edge_color = edge_color_map.get(edge_health, "#52c41a")
                animated = False

            formatted_edge = {
                "id": edge.get("id", f"{edge['source']}-{edge['target']}"),
                "source": edge["source"],
                "target": edge["target"],
                "label": f"{metrics.get('call_count', 0)} calls",
                "type": edge.get("type", "calls"),
                "width": edge_width,
                "color": edge_color,
                "style": "solid" if not animated else "dashed",
                "animated": animated,
                "metrics": {
                    "qps": metrics.get("call_count", 0) / 60,  # 粗略估算QPS
                    "avg_latency_ms": metrics.get("avg_duration", 0),
                    "p99_latency_ms": p99_latency,
                    "error_rate": metrics.get("error_rate", 0)
                }
            }

            formatted_edges.append(formatted_edge)

        # 构建响应
        response = {
            "layout": {
                "type": "swimlane",
                "lanes": [
                    {"id": "client", "label": "客户端层", "x": 200, "color": "#e6f7ff"},
                    {"id": "business", "label": "业务服务层", "x": 600, "color": "#f6ffed"},
                    {"id": "infrastructure", "label": "基础设施层", "x": 1000, "color": "#fff2e8"}
                ]
            },
            "nodes": formatted_nodes,
            "edges": formatted_edges,
            "metadata": {
                "time_window": time_window,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "node_count": len(formatted_nodes),
                "edge_count": len(formatted_edges),
                "auto_refresh_interval": 5 if auto_refresh else 0,
                "layers": {
                    "client": len(layers["client"]),
                    "business": len(layers["business"]),
                    "infrastructure": len(layers["infrastructure"])
                }
            }
        }

        logger.info(
            f"Generated monitor topology: {len(formatted_nodes)} nodes, "
            f"{len(formatted_edges)} edges"
        )

        return response

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error generating monitor topology: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/topology/legend")
async def get_topology_legend() -> Dict[str, Any]:
    """
    获取拓扑图图例说明

    返回节点状态、连线样式、颜色含义的说明
    """
    return {
        "node_status": [
            {
                "status": "healthy",
                "label": "健康",
                "color": "#1890ff",
                "description": "错误率 < 1%, 至少1个健康实例"
            },
            {
                "status": "warning",
                "label": "警告",
                "color": "#fa8c16",
                "description": "错误率 1-5%"
            },
            {
                "status": "error",
                "label": "异常",
                "color": "#f5222d",
                "description": "错误率 > 5% 或无健康实例"
            },
            {
                "status": "unknown",
                "label": "未知",
                "color": "#d9d9d9",
                "description": "无数据"
            }
        ],
        "edge_style": [
            {
                "type": "healthy",
                "color": "#52c41a",
                "description": "正常调用链路"
            },
            {
                "type": "warning",
                "color": "#faad14",
                "description": "延迟较高 (P99 > 1s)"
            },
            {
                "type": "error",
                "color": "#ff4d4f",
                "animated": True,
                "description": "高延迟或高错误率 (P99 > 2s)"
            }
        ],
        "edge_width": {
            "min": 0.5,
            "max": 4.0,
            "description": "线宽表示调用量/QPS，使用对数缩放"
        },
        "layout": {
            "type": "swimlane",
            "lanes": [
                {"id": "client", "label": "客户端层"},
                {"id": "business", "label": "业务服务层"},
                {"id": "infrastructure", "label": "基础设施层"}
            ],
            "description": "三栏泳道布局，支持缩放(鼠标滚轮)和平移(画布拖拽)"
        },
        "interactions": {
            "hover": "显示服务详情浮窗",
            "click": "跳转到服务详情页",
            "drag": "手动调整节点位置",
            "zoom": "鼠标滚轮缩放画布"
        }
    }


@router.get("/topology/search")
async def search_topology_nodes(
    query: str = Query(..., description="搜索关键词（服务名）"),
    time_window: str = Query("5 MINUTE", description="时间窗口")
) -> Dict[str, Any]:
    """
    搜索拓扑节点

    支持模糊匹配服务名，返回匹配的节点及其位置

    参数:
        - query: 搜索关键词
        - time_window: 时间窗口

    返回:
        {
            "query": "frontend",
            "matches": [
                {
                    "id": "frontend-service",
                    "label": "Frontend Service",
                    "layer": "client",
                    "position": {"x": 200, "y": 100},
                    "status": "healthy"
                }
            ],
            "count": 1
        }
    """
    try:
        global storage

        if not storage:
            raise HTTPException(status_code=500, detail="Storage adapter not initialized")

        # 获取完整拓扑
        builder = get_enhanced_topology_builder(storage)
        if not builder:
            raise HTTPException(500, detail="Topology builder not initialized")

        topology = await _run_blocking(builder.build_topology, time_window=time_window)

        raw_nodes = topology.get("nodes", [])

        # 搜索匹配节点
        matches = []
        query_lower = query.lower()

        for idx, node in enumerate(raw_nodes):
            if query_lower in node["id"].lower():
                layer = _classify_service_layer(node["id"])
                health = _calculate_node_health(node)

                matches.append({
                    "id": node["id"],
                    "label": node.get("label", node["id"]),
                    "layer": layer,
                    "position": _calculate_node_position(node["id"], layer, idx, len(raw_nodes)),
                    "status": health,
                    "color": STATUS_COLORS[health]
                })

        return {
            "query": query,
            "matches": matches,
            "count": len(matches)
        }

    except Exception as e:
        logger.error(f"Error searching topology: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/topology/views")
async def list_topology_views() -> Dict[str, Any]:
    """
    列出可用的拓扑视图模式

    返回支持的视图类型和切换参数
    """
    return {
        "views": [
            {
                "id": "topology",
                "label": "拓扑图",
                "description": "可视化服务调用关系",
                "icon": "apartment",
                "default": True
            },
            {
                "id": "list",
                "label": "列表视图",
                "description": "表格形式展示所有服务",
                "icon": "table"
            },
            {
                "id": "matrix",
                "label": "依赖矩阵",
                "description": "矩阵形式展示服务依赖关系",
                "icon": "border"
            }
        ],
        "time_ranges": [
            {"id": "5m", "label": "最近5分钟", "value": "5 MINUTE"},
            {"id": "30m", "label": "最近30分钟", "value": "30 MINUTE"},
            {"id": "1h", "label": "最近1小时", "value": "1 HOUR"},
            {"id": "custom", "label": "自定义", "value": "custom"}
        ],
        "refresh_intervals": [
            {"id": "off", "label": "关闭", "value": 0},
            {"id": "5s", "label": "5秒", "value": 5},
            {"id": "30s", "label": "30秒", "value": 30},
            {"id": "1m", "label": "1分钟", "value": 60}
        ],
        "aggregation_levels": [
            {"id": "service", "label": "按服务"},
            {"id": "instance", "label": "按实例"},
            {"id": "version", "label": "按版本"}
        ]
    }


@router.get("/topology/aggregated")
async def get_aggregated_topology(
    time_window: str = Query("5 MINUTE", description="时间窗口"),
    group_by: Literal["service", "instance", "version"] = Query("service", description="分组维度"),
    namespace: str = Query(None, description="命名空间过滤")
) -> Dict[str, Any]:
    """
    获取聚合拓扑数据

    支持按服务、实例、版本分组聚合数据

    参数:
        - time_window: 时间窗口
        - group_by: 分组维度 (service|instance|version)
        - namespace: 命名空间过滤

    返回:
        聚合后的拓扑数据
    """
    try:
        global storage

        if not storage:
            raise HTTPException(status_code=500, detail="Storage adapter not initialized")

        builder = get_enhanced_topology_builder(storage)
        if not builder:
            raise HTTPException(500, detail="Topology builder not initialized")

        # 获取原始拓扑
        topology = await _run_blocking(
            builder.build_topology,
            time_window=time_window,
            namespace=namespace,
        )

        # 根据 group_by 进行聚合
        if group_by == "service":
            # 默认已按服务聚合，直接返回
            aggregated_topology = topology
        elif group_by == "instance":
            # 按实例分组（需要从 metrics 中提取 instance 信息）
            aggregated_topology = _aggregate_by_instance(topology)
        elif group_by == "version":
            # 按版本分组
            aggregated_topology = _aggregate_by_version(topology)
        else:
            aggregated_topology = topology

        # 添加聚合元数据
        metadata = aggregated_topology.get("metadata", {})
        metadata["aggregation_level"] = group_by
        metadata["generated_at"] = datetime.now(timezone.utc).isoformat()

        aggregated_topology["metadata"] = metadata

        return aggregated_topology

    except Exception as e:
        logger.error(f"Error getting aggregated topology: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


def _aggregate_by_instance(topology: Dict[str, Any]) -> Dict[str, Any]:
    """按实例聚合拓扑数据"""
    # 简化实现：实际需要从 logs 表提取 pod/instance 信息
    nodes = topology.get("nodes", [])
    edges = topology.get("edges", [])

    # 为每个节点添加实例信息
    for node in nodes:
        metrics = node.get("metrics", {})
        pod_count = metrics.get("pod_count", 1)

        # 将节点拆分为实例节点
        node["instance_count"] = pod_count
        node["aggregation_type"] = "instance"

    return topology


def _aggregate_by_version(topology: Dict[str, Any]) -> Dict[str, Any]:
    """按版本聚合拓扑数据"""
    # 简化实现：实际需要从 traces/logs 提取版本信息
    nodes = topology.get("nodes", [])

    for node in nodes:
        # 假设版本信息从 label 或其他地方提取
        node["version"] = "latest"
        node["aggregation_type"] = "version"

    return topology
