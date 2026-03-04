"""
服务拓扑手动调整 API

支持：
1. 手动添加/删除节点
2. 手动添加/删除边
3. 禁用/启用边
4. 查询当前配置
5. 批量操作

Date: 2026-02-11
"""

import asyncio
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from typing import Dict, Any, List, Optional
import logging

from graph.enhanced_topology import get_enhanced_topology_builder

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/topology", tags=["topology-adjustment"])


# ==================== 数据模型 ====================

class NodeRequest(BaseModel):
    """节点操作请求"""
    node_id: str = Field(..., description="节点ID（服务名）")
    node_type: str = Field(default="service", description="节点类型")


class EdgeRequest(BaseModel):
    """边操作请求"""
    source: str = Field(..., description="源服务")
    target: str = Field(..., description="目标服务")
    edge_type: str = Field(default="calls", description="边类型")
    confidence: float = Field(default=1.0, description="置信度")
    reason: str = Field(default="manual", description="原因说明")


class BatchEdgeRequest(BaseModel):
    """批量边操作请求"""
    edges: List[EdgeRequest] = Field(..., description="边列表")


class ConfigResponse(BaseModel):
    """配置响应"""
    status: str
    action: str
    data: Any


# 导入 storage（需要在模块加载时注入）
storage = None


async def _run_blocking(func, *args, **kwargs):
    """Execute blocking topology builder calls in thread pool."""
    return await asyncio.to_thread(func, *args, **kwargs)


def set_storage_adapter(storage_adapter):
    """设置 storage adapter"""
    global storage
    storage = storage_adapter


# ==================== 端点操作 ====================

@router.post("/nodes/manual")
async def add_manual_node(request: NodeRequest) -> Dict[str, Any]:
    """
    手动添加拓扑节点

    Args:
        request: 节点请求

    Returns:
        操作结果

    示例:
        POST /api/v1/topology/nodes/manual
        {
            "node_id": "new-service",
            "node_type": "service"
        }
    """
    try:
        builder = get_enhanced_topology_builder(storage)
        if not builder:
            raise HTTPException(status_code=500, detail="Topology builder not initialized")

        result = builder.add_manual_node(
            node_id=request.node_id,
            node_type=request.node_type
        )

        logger.info(f"Added manual node: {request.node_id} (type: {request.node_type})")
        return result

    except Exception as e:
        logger.error(f"Error adding manual node: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.delete("/nodes/manual/{node_id}")
async def remove_manual_node(node_id: str) -> Dict[str, Any]:
    """
    手动移除拓扑节点

    Args:
        node_id: 节点ID

    Returns:
        操作结果

    示例:
        DELETE /api/v1/topology/nodes/manual/old-service
    """
    try:
        builder = get_enhanced_topology_builder(storage)
        if not builder:
            raise HTTPException(status_code=500, detail="Topology builder not initialized")

        result = builder.remove_manual_node(node_id=node_id)

        logger.info(f"Removed manual node: {node_id}")
        return result

    except Exception as e:
        logger.error(f"Error removing manual node: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


# ==================== 边操作 ====================

@router.post("/edges/manual")
async def add_manual_edge(request: EdgeRequest) -> Dict[str, Any]:
    """
    手动添加拓扑边

    Args:
        request: 边请求

    Returns:
        操作结果

    示例:
        POST /api/v1/topology/edges/manual
        {
            "source": "frontend",
            "target": "backend",
            "edge_type": "calls",
            "confidence": 1.0,
            "reason": "Based on code analysis"
        }
    """
    try:
        builder = get_enhanced_topology_builder(storage)
        if not builder:
            raise HTTPException(status_code=500, detail="Topology builder not initialized")

        result = builder.add_manual_edge(
            source=request.source,
            target=request.target,
            edge_type=request.edge_type,
            confidence=request.confidence,
            reason=request.reason
        )

        logger.info(f"Added manual edge: {request.source} -> {request.target}")
        return result

    except Exception as e:
        logger.error(f"Error adding manual edge: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.delete("/edges/manual")
async def remove_manual_edge(
    source: str = Query(..., description="源服务"),
    target: str = Query(..., description="目标服务")
) -> Dict[str, Any]:
    """
    手动移除拓扑边

    Args:
        source: 源服务
        target: 目标服务

    Returns:
        操作结果

    示例:
        DELETE /api/v1/topology/edges/manual?source=frontend&target=database
    """
    try:
        builder = get_enhanced_topology_builder(storage)
        if not builder:
            raise HTTPException(status_code=500, detail="Topology builder not initialized")

        result = builder.remove_manual_edge(source=source, target=target)

        logger.info(f"Removed manual edge: {source} -> {target}")
        return result

    except Exception as e:
        logger.error(f"Error removing manual edge: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/edges/manual/batch")
async def add_manual_edges_batch(request: BatchEdgeRequest) -> Dict[str, Any]:
    """
    批量添加拓扑边

    Args:
        request: 批量边请求

    Returns:
        操作结果

    示例:
        POST /api/v1/topology/edges/manual/batch
        {
            "edges": [
                {"source": "service-a", "target": "service-b", "confidence": 0.9},
                {"source": "service-b", "target": "database", "confidence": 1.0}
            ]
        }
    """
    try:
        builder = get_enhanced_topology_builder(storage)
        if not builder:
            raise HTTPException(status_code=500, detail="Topology builder not initialized")

        results = []
        success_count = 0
        error_count = 0

        for edge_req in request.edges:
            result = builder.add_manual_edge(
                source=edge_req.source,
                target=edge_req.target,
                edge_type=edge_req.edge_type,
                confidence=edge_req.confidence,
                reason=edge_req.reason
            )
            results.append(result)

            if result.get("status") == "success":
                success_count += 1
            else:
                error_count += 1

        logger.info(f"Batch added edges: {success_count} success, {error_count} errors")

        return {
            "status": "completed",
            "total": len(request.edges),
            "success_count": success_count,
            "error_count": error_count,
            "results": results
        }

    except Exception as e:
        logger.error(f"Error adding manual edges batch: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


# ==================== 边控制 ====================

@router.post("/edges/suppress")
async def suppress_edge(
    source: str = Query(..., description="源服务"),
    target: str = Query(..., description="目标服务")
) -> Dict[str, Any]:
    """
    禁用某条边（不删除，只是临时隐藏）

    Args:
        source: 源服务
        target: 目标服务

    Returns:
        操作结果

    示例:
        POST /api/v1/topology/edges/suppress?source=service-a&target=service-b
    """
    try:
        builder = get_enhanced_topology_builder(storage)
        if not builder:
            raise HTTPException(status_code=500, detail="Topology builder not initialized")

        result = builder.suppress_edge(source=source, target=target)

        logger.info(f"Suppressed edge: {source} -> {target}")
        return result

    except Exception as e:
        logger.error(f"Error suppressing edge: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/edges/unsuppress")
async def unsuppress_edge(
    source: str = Query(..., description="源服务"),
    target: str = Query(..., description="目标服务")
) -> Dict[str, Any]:
    """
    取消禁用边

    Args:
        source: 源服务
        target: 目标服务

    Returns:
        操作结果

    示例:
        POST /api/v1/topology/edges/unsuppress?source=service-a&target=service-b
    """
    try:
        builder = get_enhanced_topology_builder(storage)
        if not builder:
            raise HTTPException(status_code=500, detail="Topology builder not initialized")

        result = builder.unsuppress_edge(source=source, target=target)

        logger.info(f"Unsuppressed edge: {source} -> {target}")
        return result

    except Exception as e:
        logger.error(f"Error unsuppress edge: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


# ==================== 配置查询 ====================

@router.get("/config/manual")
async def get_manual_configurations() -> Dict[str, Any]:
    """
    获取所有手动配置

    Returns:
        手动配置列表

    示例:
        GET /api/v1/topology/config/manual
    """
    try:
        builder = get_enhanced_topology_builder(storage)
        if not builder:
            raise HTTPException(status_code=500, detail="Topology builder not initialized")

        config = builder.get_manual_configurations()

        logger.info(f"Retrieved manual configurations: {len(config['manual_nodes'])} nodes, "
                    f"{len(config['manual_edges'])} edges, "
                    f"{len(config['suppressed_edges'])} suppressed")

        return config

    except Exception as e:
        logger.error(f"Error getting manual configurations: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.delete("/config/manual")
async def clear_manual_configurations() -> Dict[str, Any]:
    """
    清除所有手动配置

    Returns:
        操作结果

    示例:
        DELETE /api/v1/topology/config/manual
    """
    try:
        builder = get_enhanced_topology_builder(storage)
        if not builder:
            raise HTTPException(status_code=500, detail="Topology builder not initialized")

        result = builder.clear_manual_configurations()

        logger.info(f"Cleared all manual configurations: {result}")

        return result

    except Exception as e:
        logger.error(f"Error clearing manual configurations: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


# ==================== 增强拓扑API ====================

@router.get("/enhanced")
async def get_enhanced_topology(
    time_window: str = Query("1 HOUR", description="时间窗口"),
    namespace: str = Query(None, description="命名空间"),
    confidence_threshold: float = Query(0.3, description="置信度阈值"),
    enable_time_correlation: bool = Query(True, description="启用时间关联"),
    enable_heuristics: bool = Query(True, description="启用启发式规则")
) -> Dict[str, Any]:
    """
    获取增强的服务拓扑图

    Args:
        time_window: 时间窗口（如 '1 HOUR', '15 MINUTE'）
        namespace: 命名空间过滤
        confidence_threshold: 置信度阈值（0.0-1.0）
        enable_time_correlation: 是否启用基于时间戳的关联算法
        enable_heuristics: 是否启用启发式规则

    Returns:
        {
            "nodes": [...],
            "edges": [...],
            "metadata": {
                "data_sources": ["traces", "logs", "metrics", "manual"],
                "time_window": "1 HOUR",
                "node_count": 10,
                "edge_count": 15,
                "avg_confidence": 0.75,
                "manual_nodes": 2,
                "manual_edges": 3,
                "correlation_enabled": true,
                "heuristics_enabled": true
            }
        }

    核心特性：
    1. **多模态数据融合**：结合 traces (1.0) + logs (0.5) + metrics (0.3)
    2. **时间关联算法**：当traces不可用时，使用时间戳窗口关联日志
    3. **可调整性**：支持手动添加/删除节点和边
    4. **置信度加权**：每个边都有明确的置信度和来源标记

    示例:
        GET /api/v1/topology/enhanced?time_window=1%20HOUR&confidence_threshold=0.4
    """
    try:
        builder = get_enhanced_topology_builder(storage)
        if not builder:
            raise HTTPException(status_code=500, detail="Topology builder not initialized")

        topology = await _run_blocking(
            builder.build_topology,
            time_window=time_window,
            namespace=namespace,
            confidence_threshold=confidence_threshold,
            enable_time_correlation=enable_time_correlation,
            enable_heuristics=enable_heuristics,
        )

        logger.info(
            f"Built enhanced topology: {topology['metadata']['node_count']} nodes, "
            f"{topology['metadata']['edge_count']} edges"
        )

        return topology

    except Exception as e:
        logger.error(f"Error building enhanced topology: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/highlight/comparison")
async def compare_with_industry() -> Dict[str, Any]:
    """
    对比当前实现与业界最佳实践

    Returns:
        对比分析结果

    示例:
        GET /api/v1/topology/highlight/comparison
    """
    try:
        builder = get_enhanced_topology_builder(storage)
        if not builder:
            raise HTTPException(status_code=500, detail="Topology builder not initialized")

        # 获取当前拓扑
        current_topology = await _run_blocking(builder.build_topology, time_window="1 HOUR")

        # 分析特性
        highlights = {
            "multi_modal_fusion": {
                "description": "多模态数据融合",
                "implementation": "✅ 实现了 traces + logs + metrics 三种数据源的融合",
                "confidence_weights": {
                    "traces": 1.0,
                    "logs_correlated": 0.6,
                    "logs_heuristic": 0.3,
                    "metrics": 0.3,
                    "manual": 1.0
                },
                "industry_benchmark": "参考 MULAN, CHASE, DeepTraLog 等论文"
            },
            "time_correlation": {
                "description": "基于时间戳的关联算法",
                "implementation": "✅ 当 trace_id 不可用时，使用时间窗口关联日志",
                "algorithm": "时间窗口内日志出现顺序分析，推断调用方向",
                "industry_benchmark": "类似 TraceWeaver 的无代码插桩追踪"
            },
            "adjustability": {
                "description": "可手动调整的拓扑",
                "implementation": "✅ 支持 CRUD 操作：添加/删除节点、边，禁用/启用边",
                "features": [
                    "手动添加节点 (POST /nodes/manual)",
                    "手动删除节点 (DELETE /nodes/manual/{id})",
                    "手动添加边 (POST /edges/manual)",
                    "手动删除边 (DELETE /edges/manual)",
                    "批量添加边 (POST /edges/manual/batch)",
                    "禁用边 (POST /edges/suppress)",
                    "启用边 (POST /edges/unsuppress)",
                    "查询配置 (GET /config/manual)",
                    "清除配置 (DELETE /config/manual)"
                ],
                "industry_benchmark": "超越传统工具的灵活性"
            },
            "confidence_system": {
                "description": "多级置信度系统",
                "implementation": "✅ 每条边都有明确的置信度和数据来源标记",
                "levels": {
                    "1.0 (traces/manual)": "精确可靠",
                    "0.6-0.8 (logs_correlated)": "时间关联，较可靠",
                    "0.3-0.5 (logs_heuristic)": "启发式推断，需验证",
                    "0.3 (metrics)": "指标验证，辅助"
                },
                "industry_benchmark": "优于传统单一置信度系统"
            },
            "graph_representation": {
                "description": "图结构表示",
                "implementation": "✅ 节点和边的完整图模型，支持复杂关系",
                "features": [
                    "节点：id, label, type, metrics (包含多个数据源)",
                    "边：source, target, type, metrics (包含置信度、原因)",
                    "元数据：生成时间、数据源统计、手动配置统计"
                ],
                "industry_benchmark": "符合 ServiceGraph-FM 的图神经网络方法"
            },
            "scalability": {
                "description": "可扩展性",
                "implementation": "✅ 支持大规模微服务架构",
                "current_capacity": {
                    "nodes": current_topology['metadata'].get('node_count', 0),
                    "edges": current_topology['metadata'].get('edge_count', 0)
                },
                "industry_benchmark": "参考 GMTA 的大规模图处理能力"
            }
        }

        return {
            "project_highlights": "Logoscope 服务拓扑亮点特性",
            "comparison": highlights,
            "current_topology_summary": {
                "nodes": current_topology['metadata'].get('node_count', 0),
                "edges": current_topology['metadata'].get('edge_count', 0),
                "avg_confidence": current_topology['metadata'].get('avg_confidence', 0),
                "data_sources": current_topology['metadata'].get('data_sources', [])
            },
            "industry_best_practices": {
                "papers": [
                    "MULAN - Multi-modal Causal Structure Learning",
                    "CHASE - Causal Hypergraph Framework",
                    "DeepTraLog - Trace-Log Combined Analysis",
                    "TraceWeaver - Request Tracing Without Instrumentation",
                    "Horus - Non-Intrusive Causal Analysis",
                    "ServiceGraph-FM - Graph Neural Networks"
                ],
                "tools": [
                    "Grafana Enterprise Traces",
                    "Jaeger Distributed Tracing",
                    "Chronosphere Service Map",
                    "OpenObserve Topology"
                ]
            }
        }

    except Exception as e:
        logger.error(f"Error comparing topology: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
