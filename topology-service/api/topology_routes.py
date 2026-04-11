"""
Topology Service API 路由 - 统一的拓扑查询接口
"""
import asyncio
import logging
import re
from typing import Dict, Any, Optional, List
from fastapi import APIRouter, HTTPException, Query
from datetime import datetime

from storage.adapter import StorageAdapter
from graph.hybrid_topology import HybridTopologyBuilder
from graph.enhanced_topology import EnhancedTopologyBuilder
from api.topology_build_coordinator import build_hybrid_topology_coalesced

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/topology", tags=["topology"])

# 全局实例
_STORAGE_ADAPTER: StorageAdapter = None
_HYBRID_BUILDER: HybridTopologyBuilder = None
_ENHANCED_BUILDER: EnhancedTopologyBuilder = None


async def _run_blocking(func, *args, **kwargs):
    """Execute blocking topology/storage calls inline."""
    return func(*args, **kwargs)


def _init_storage(adapter: StorageAdapter):
    """初始化存储适配器"""
    global _STORAGE_ADAPTER
    _STORAGE_ADAPTER = adapter


def _init_builders(hybrid_builder, enhanced_builder):
    """初始化拓扑构建器"""
    global _HYBRID_BUILDER, _ENHANCED_BUILDER
    _HYBRID_BUILDER = hybrid_builder
    _ENHANCED_BUILDER = enhanced_builder


def set_storage_and_builders(adapter: StorageAdapter, hybrid_builder, enhanced_builder):
    """设置存储适配器和拓扑构建器"""
    _init_storage(adapter)
    _init_builders(hybrid_builder, enhanced_builder)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return float(default)
        return float(value)
    except Exception:
        return float(default)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return int(default)
        return int(value)
    except Exception:
        return int(default)


def _to_risk_level(issue_score: float) -> str:
    if issue_score >= 70:
        return "高风险"
    if issue_score >= 35:
        return "中风险"
    return "低风险"


def _sanitize_interval(time_window: str, default_value: str = "1 HOUR") -> str:
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


def _build_edge_problem_summary(edge: Dict[str, Any]) -> Dict[str, Any]:
    metrics = edge.get("metrics", {}) if isinstance(edge.get("metrics"), dict) else {}
    error_rate = _safe_float(metrics.get("error_rate"), _safe_float(edge.get("error_rate"), 0.0))
    timeout_rate = _safe_float(metrics.get("timeout_rate"), _safe_float(edge.get("timeout_rate"), 0.0))
    p99 = _safe_float(metrics.get("p99"), _safe_float(edge.get("p99"), 0.0))
    quality_score = _safe_float(metrics.get("quality_score"), _safe_float(edge.get("quality_score"), 100.0))
    evidence = str(metrics.get("evidence_type", edge.get("evidence_type", "observed"))).strip().lower() or "observed"

    error_score = min(error_rate * 100.0, 1.0) * 50.0
    timeout_score = min(timeout_rate * 100.0, 1.0) * 25.0
    latency_score = min(p99 / 1500.0, 1.0) * 20.0
    quality_penalty = max(0.0, (80.0 - quality_score) / 80.0) * 25.0
    inferred_penalty = 3.0 if evidence == "inferred" else 0.0

    issue_score = round(error_score + timeout_score + latency_score + quality_penalty + inferred_penalty, 2)
    risk_level = _to_risk_level(issue_score)

    reasons: List[str] = []
    if error_rate >= 0.08:
        reasons.append("error_rate_high")
    elif error_rate >= 0.03:
        reasons.append("error_rate_elevated")
    if timeout_rate >= 0.05:
        reasons.append("timeout_rate_high")
    elif timeout_rate >= 0.02:
        reasons.append("timeout_rate_elevated")
    if p99 >= 1200:
        reasons.append("latency_p99_high")
    elif p99 >= 650:
        reasons.append("latency_p99_elevated")
    if quality_score < 70:
        reasons.append("quality_score_low")
    if evidence == "inferred":
        reasons.append("inferred_evidence")

    has_issue = (
        issue_score >= 35.0
        or error_rate >= 0.03
        or timeout_rate >= 0.02
        or p99 >= 650
        or quality_score < 80
    )

    headline = (
        f"{edge.get('source', 'unknown')} -> {edge.get('target', 'unknown')} "
        f"{risk_level}，错误率 {error_rate:.2%}，超时率 {timeout_rate:.2%}，P99 {p99:.0f}ms"
    )
    suggestion = (
        "优先查看源服务错误日志与对应 trace，确认下游依赖、重试与超时配置是否异常。"
        if has_issue
        else "链路指标稳定，继续观察趋势即可。"
    )

    return {
        "has_issue": has_issue,
        "risk_level": risk_level,
        "issue_score": issue_score,
        "headline": headline,
        "reasons": reasons,
        "suggestion": suggestion,
    }


def _build_node_problem_summary(node: Dict[str, Any]) -> Dict[str, Any]:
    metrics = node.get("metrics", {}) if isinstance(node.get("metrics"), dict) else {}
    error_count = _safe_int(metrics.get("error_count"), 0)
    error_rate = _safe_float(metrics.get("error_rate"), 0.0)
    timeout_rate = _safe_float(metrics.get("timeout_rate"), 0.0)
    quality_score = _safe_float(metrics.get("quality_score"), _safe_float(node.get("quality_score"), 100.0))
    log_count = _safe_int(metrics.get("log_count"), 0)

    error_count_score = min(float(error_count), 8.0) * 4.0
    error_rate_score = min(error_rate * 100.0, 1.0) * 40.0
    timeout_score = min(timeout_rate * 100.0, 1.0) * 20.0
    quality_penalty = max(0.0, (85.0 - quality_score) / 85.0) * 25.0
    noise_penalty = 8.0 if log_count > 5000 else (4.0 if log_count > 1500 else 0.0)

    issue_score = round(error_count_score + error_rate_score + timeout_score + quality_penalty + noise_penalty, 2)
    risk_level = _to_risk_level(issue_score)

    reasons: List[str] = []
    if error_count > 0:
        reasons.append("error_count_detected")
    if error_rate >= 0.03:
        reasons.append("error_rate_elevated")
    if timeout_rate >= 0.02:
        reasons.append("timeout_rate_elevated")
    if quality_score < 80:
        reasons.append("quality_score_low")
    if log_count > 1500:
        reasons.append("high_log_volume")

    has_issue = (
        issue_score >= 35.0
        or error_count > 0
        or error_rate >= 0.03
        or timeout_rate >= 0.02
        or quality_score < 80
    )

    service_name = node.get("label") or node.get("id") or "unknown"
    headline = (
        f"{service_name} {risk_level}，错误数 {error_count}，错误率 {error_rate:.2%}，质量分 {quality_score:.1f}"
    )
    suggestion = (
        "优先排查该服务最近错误日志与上下游高风险链路。"
        if has_issue
        else "节点运行平稳，建议继续监控关键 RED 指标。"
    )

    return {
        "has_issue": has_issue,
        "risk_level": risk_level,
        "issue_score": issue_score,
        "headline": headline,
        "reasons": reasons,
        "suggestion": suggestion,
    }


def _enrich_problem_summary(nodes: List[Dict[str, Any]], edges: List[Dict[str, Any]]) -> Dict[str, Any]:
    for node in nodes:
        node_summary = _build_node_problem_summary(node)
        node["problem_summary"] = node_summary
        metrics = node.setdefault("metrics", {})
        if isinstance(metrics, dict):
            metrics["problem_summary"] = node_summary

    for edge in edges:
        edge_summary = _build_edge_problem_summary(edge)
        edge["problem_summary"] = edge_summary
        metrics = edge.setdefault("metrics", {})
        if isinstance(metrics, dict):
            metrics["problem_summary"] = edge_summary

    high_risk_nodes = sum(
        1 for node in nodes
        if isinstance(node.get("problem_summary"), dict)
        and node["problem_summary"].get("risk_level") == "高风险"
    )
    medium_risk_nodes = sum(
        1 for node in nodes
        if isinstance(node.get("problem_summary"), dict)
        and node["problem_summary"].get("risk_level") == "中风险"
    )
    high_risk_edges = sum(
        1 for edge in edges
        if isinstance(edge.get("problem_summary"), dict)
        and edge["problem_summary"].get("risk_level") == "高风险"
    )
    medium_risk_edges = sum(
        1 for edge in edges
        if isinstance(edge.get("problem_summary"), dict)
        and edge["problem_summary"].get("risk_level") == "中风险"
    )

    top_problem_edges = sorted(
        (
            {
                "source": edge.get("source"),
                "target": edge.get("target"),
                "issue_score": _safe_float(
                    (edge.get("problem_summary") or {}).get("issue_score"),
                    0.0,
                ),
                "risk_level": (edge.get("problem_summary") or {}).get("risk_level", "低风险"),
            }
            for edge in edges
        ),
        key=lambda item: item["issue_score"],
        reverse=True,
    )[:5]

    return {
        "unhealthy_nodes": high_risk_nodes + medium_risk_nodes,
        "unhealthy_edges": high_risk_edges + medium_risk_edges,
        "high_risk_nodes": high_risk_nodes,
        "medium_risk_nodes": medium_risk_nodes,
        "high_risk_edges": high_risk_edges,
        "medium_risk_edges": medium_risk_edges,
        "top_problem_edges": top_problem_edges,
    }


@router.get("/hybrid")
async def get_hybrid_topology(
    time_window: str = Query("1 HOUR", description="时间窗口（如 '1 HOUR', '15 MINUTE'）"),
    namespace: Optional[str] = Query(None, description="命名空间过滤"),
    confidence_threshold: float = Query(0.3, description="置信度阈值（0.0-1.0）"),
    inference_mode: Optional[str] = Query(
        None,
        description="推断模式：rule（规则模式）| hybrid_score（混合打分模式）",
    ),
    message_target_enabled: Optional[bool] = Query(None, description="是否启用 message_target 推断"),
    message_target_patterns: Optional[str] = Query(None, description="message_target 模式（逗号分隔：url,kv,proxy,rpc）"),
    message_target_min_support: Optional[int] = Query(None, ge=1, le=20, description="message_target 最小支持数"),
    message_target_max_per_log: Optional[int] = Query(None, ge=1, le=12, description="每条日志最多提取目标数"),
) -> Dict[str, Any]:
    """
    获取混合数据源的服务拓扑图
    """
    if not _HYBRID_BUILDER:
        raise HTTPException(status_code=503, detail="Hybrid topology builder not initialized")

    try:
        topology = await build_hybrid_topology_coalesced(
            _HYBRID_BUILDER,
            time_window=time_window,
            namespace=namespace,
            confidence_threshold=confidence_threshold,
            inference_mode=inference_mode,
            message_target_enabled=message_target_enabled,
            message_target_patterns=message_target_patterns,
            message_target_min_support=message_target_min_support,
            message_target_max_per_log=message_target_max_per_log,
        )

        nodes = topology.get("nodes", [])
        edges = topology.get("edges", [])

        # 兼容输出：确保关键字段可直接访问（无需只从 metrics 读取）
        for edge in edges:
            metrics = edge.get("metrics", {})
            for field in (
                "evidence_type",
                "coverage",
                "quality_score",
                "p95",
                "p99",
                "timeout_rate",
                "pending",
                "dlq",
                "inference_method",
                "confidence_explain",
                "evidence_chain",
            ):
                if field not in edge and field in metrics:
                    edge[field] = metrics.get(field)

        issue_summary = _enrich_problem_summary(nodes, edges)

        metadata = topology.get("metadata", {}) or {}
        metadata.setdefault("data_sources", ["traces", "logs", "metrics"])
        metadata.setdefault("time_window", time_window)
        metadata.setdefault("node_count", len(nodes))
        metadata.setdefault("edge_count", len(edges))
        metadata.setdefault("generated_at", datetime.now().isoformat())
        metadata.setdefault("contract_version", "topology-schema-v1")
        metadata.setdefault("quality_version", "quality-score-v1")
        inference_quality = metadata.get("inference_quality", {}) if isinstance(metadata.get("inference_quality"), dict) else {}
        if "inference_mode" in inference_quality:
            metadata.setdefault("inference_mode", inference_quality.get("inference_mode"))
        metadata["issue_summary"] = issue_summary

        return {
            "nodes": nodes,
            "edges": edges,
            "metadata": metadata
        }

    except Exception as e:
        logger.error(f"获取混合拓扑时出错: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/enhanced")
async def get_enhanced_topology(
    time_window: str = Query("1 HOUR", description="时间窗口"),
    namespace: Optional[str] = Query(None, description="命名空间过滤"),
) -> Dict[str, Any]:
    """
    获取增强型拓扑图
    """
    if not _ENHANCED_BUILDER:
        raise HTTPException(status_code=503, detail="Enhanced topology builder not initialized")

    try:
        topology = await _run_blocking(
            _ENHANCED_BUILDER.build_topology,
            time_window=time_window,
            namespace=namespace,
        )

        nodes = topology.get("nodes", [])
        edges = topology.get("edges", [])
        issue_summary = _enrich_problem_summary(nodes, edges)

        return {
            "nodes": nodes,
            "edges": edges,
            "metadata": {
                "type": "enhanced",
                "time_window": time_window,
                "node_count": len(nodes),
                "edge_count": len(edges),
                "generated_at": datetime.now().isoformat(),
                "issue_summary": issue_summary,
            }
        }

    except Exception as e:
        logger.error(f"获取增强拓扑时出错: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/stats")
async def get_topology_stats(
    time_window: str = Query("1 HOUR", description="时间窗口")
) -> Dict[str, Any]:
    """
    获取拓扑统计信息
    """
    if not _STORAGE_ADAPTER and not _HYBRID_BUILDER:
        raise HTTPException(status_code=503, detail="Storage adapter/hybrid builder not initialized")

    try:
        safe_time_window = _sanitize_interval(time_window, default_value="1 HOUR")
        # 统一使用 hybrid builder 的窗口化结果，避免与页面主图统计口径不一致。
        if _HYBRID_BUILDER:
            topology = await build_hybrid_topology_coalesced(
                _HYBRID_BUILDER,
                time_window=safe_time_window,
                namespace=None,
                confidence_threshold=0.0,
            )
            nodes = topology.get("nodes", [])
            edges = topology.get("edges", [])
            metadata = topology.get("metadata", {}) or {}

            service_count = len({str(node.get("id") or "") for node in nodes if node.get("id")})

            return {
                "total_nodes": len(nodes),
                "total_edges": len(edges),
                "avg_confidence": metadata.get("avg_confidence", 0.0),
                "time_window": metadata.get("time_window", time_window),
                "service_count": service_count,
                "data_sources": metadata.get("source_breakdown", {}),
                "inference_quality": metadata.get("inference_quality", {}),
                "generated_at": metadata.get("generated_at"),
            }

        # 回退逻辑：仅当 hybrid builder 不可用时使用 storage 查询。
        stats = {
                "total_nodes": 0,
                "total_edges": 0,
                "avg_confidence": 0.0,
                "time_window": safe_time_window,
                "service_count": 0,
            }
        if _STORAGE_ADAPTER:
            try:
                services_query = """
                SELECT COUNT(DISTINCT service_name) as service_count
                FROM logs.logs
                PREWHERE timestamp >= now() - INTERVAL {safe_time_window}
                """.format(safe_time_window=safe_time_window)
                services_result = await _run_blocking(_STORAGE_ADAPTER.execute_query, services_query)
                if services_result:
                    stats["service_count"] = services_result[0].get("service_count", 0)
            except Exception as e:
                logger.warning(f"从 ClickHouse 获取统计失败: {e}")
        return stats

    except Exception as e:
        logger.error(f"获取拓扑统计时出错: {e}")
        return {
            "total_nodes": 0,
            "total_edges": 0,
            "avg_confidence": 0.0,
            "time_window": _sanitize_interval(time_window, default_value="1 HOUR"),
            "service_count": 0,
            "error": "internal_error"
        }


@router.get("/health")
async def topology_health() -> Dict[str, Any]:
    """
    拓扑服务健康检查
    """
    return {
        "status": "ok",
        "storage_initialized": _STORAGE_ADAPTER is not None,
        "hybrid_builder_initialized": _HYBRID_BUILDER is not None,
        "enhanced_builder_initialized": _ENHANCED_BUILDER is not None
    }
