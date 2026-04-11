"""
统一拓扑契约（Schema v1）

M1 目标：
- Node 主键: service.namespace + service.name + env
- Edge 主键: src + dst + protocol + endpoint_pattern
- 统一质量字段: confidence / evidence_type / coverage / quality_score
"""

from __future__ import annotations

from typing import Any, Dict, Optional
import re


_UUID_OR_HASH_SEGMENT = re.compile(r"/[0-9a-fA-F-]{6,}(?=/|$)")
_NUMERIC_SEGMENT = re.compile(r"/\d+(?=/|$)")
_MULTI_SLASH = re.compile(r"/{2,}")


def _as_text(value: Any, default: str = "unknown") -> str:
    text = str(value or "").strip()
    return text if text else default


def _clean_token(value: Any, default: str = "unknown") -> str:
    token = _as_text(value, default).lower()
    token = token.replace(" ", "-")
    token = re.sub(r"[^a-z0-9_.:-]+", "-", token)
    token = re.sub(r"-{2,}", "-", token).strip("-")
    return token or default


def infer_env(namespace: Optional[str], explicit_env: Optional[str] = None) -> str:
    """从显式 env 或 namespace 推断环境标记。"""
    if explicit_env:
        return _clean_token(explicit_env, "prod")

    ns = _clean_token(namespace, "default")
    if any(token in ns for token in ("prod", "online", "release")):
        return "prod"
    if any(token in ns for token in ("staging", "stage", "pre")):
        return "staging"
    if any(token in ns for token in ("test", "qa", "sit", "uat")):
        return "test"
    if any(token in ns for token in ("dev", "local")):
        return "dev"
    return "prod"


def build_node_key(service_namespace: str, service_name: str, env: str) -> str:
    """Node 主键：service.namespace + service.name + env"""
    namespace = _clean_token(service_namespace, "default")
    name = _clean_token(service_name, "unknown")
    env_token = _clean_token(env, "prod")
    return f"{namespace}:{name}:{env_token}"


def normalize_endpoint_pattern(operation_name: Optional[str]) -> str:
    """
    将 operation/path 归一化为 endpoint pattern。

    示例:
    - "GET /api/orders/123" -> "/api/orders/:id"
    - "POST /v1/users/9f6a..." -> "/v1/users/:id"
    """
    op = _as_text(operation_name, "unknown")
    if " " in op:
        _, path = op.split(" ", 1)
    else:
        path = op

    path = path.strip()
    if not path.startswith("/"):
        path = "/" + path
    path = path.split("?", 1)[0]
    path = _UUID_OR_HASH_SEGMENT.sub("/:id", path)
    path = _NUMERIC_SEGMENT.sub("/:id", path)
    path = _MULTI_SLASH.sub("/", path)
    return path or "/unknown"


def infer_protocol(operation_name: Optional[str], default: str = "unknown") -> str:
    """从 operation_name 推断协议类型。"""
    op = _as_text(operation_name, "").lower()
    if op.startswith(("get ", "post ", "put ", "patch ", "delete ", "head ", "options ")):
        return "http"
    if "grpc" in op:
        return "grpc"
    if "mq" in op or "queue" in op or "kafka" in op:
        return "mq"
    return default


def build_edge_key(
    src_node_key: str,
    dst_node_key: str,
    protocol: str,
    endpoint_pattern: str
) -> str:
    """Edge 主键：src + dst + protocol + endpoint_pattern"""
    return "|".join([
        _clean_token(src_node_key, "unknown"),
        _clean_token(dst_node_key, "unknown"),
        _clean_token(protocol, "unknown"),
        normalize_endpoint_pattern(endpoint_pattern),
    ])


def evidence_type_from_source(data_source: Optional[str]) -> str:
    """标准化证据类型。"""
    source = _clean_token(data_source, "unknown")
    if source in {"traces", "observed", "metrics", "logs"}:
        return "observed"
    if source in {"logs_heuristic", "inferred"}:
        return "inferred"
    return "observed"


def coverage_score(
    call_count: Optional[float] = None,
    log_count: Optional[float] = None,
    trace_count: Optional[float] = None,
    data_sources: Optional[list] = None
) -> float:
    """
    计算覆盖率（0-1），用于统一 quality 维度。
    该分值不是采样覆盖率真值，而是可比较的稳定近似指标。
    """
    calls = max(0.0, float(call_count or 0.0))
    logs = max(0.0, float(log_count or 0.0))
    traces = max(0.0, float(trace_count or 0.0))
    sources = data_sources or []

    score = 0.0
    score += min(0.60, (calls / 200.0) * 0.60)
    score += min(0.25, (traces / 80.0) * 0.25)
    score += min(0.15, (logs / 500.0) * 0.15)

    source_count = len(set(sources))
    if source_count >= 2:
        score += 0.05
    if source_count >= 3:
        score += 0.05
    return round(min(1.0, score), 3)


def apply_node_contract(node: Dict[str, Any]) -> Dict[str, Any]:
    """将节点转换为统一契约字段（保留兼容字段）。"""
    metrics = node.setdefault("metrics", {})
    service_name = _as_text(node.get("name") or node.get("label") or node.get("id"), "unknown")
    namespace = _as_text(
        metrics.get("namespace")
        or node.get("namespace")
        or metrics.get("service_namespace"),
        "unknown",
    )
    env = infer_env(namespace, metrics.get("env") or node.get("env"))
    node_key = build_node_key(namespace, service_name, env)

    data_source = metrics.get("data_source", "unknown")
    data_sources = metrics.get("data_sources")
    if not data_sources:
        data_sources = [data_source] if data_source else []
    evidence_type = evidence_type_from_source(data_source)
    coverage = coverage_score(
        log_count=metrics.get("log_count"),
        trace_count=metrics.get("trace_count"),
        data_sources=data_sources,
    )

    quality_score = float(metrics.get("quality_score") or 0.0)
    if quality_score <= 0:
        confidence = max(0.0, float(metrics.get("confidence") or 0.0))
        error_rate = max(0.0, float(metrics.get("error_rate") or 0.0))
        quality_score = max(0.0, min(100.0, confidence * 100.0 - error_rate * 20.0))

    node["service"] = {
        "namespace": namespace,
        "name": service_name,
        "env": env,
    }
    node["node_key"] = node_key
    node["display_name"] = service_name
    node["evidence_type"] = evidence_type
    node["coverage"] = coverage
    node["quality_score"] = round(quality_score, 2)

    metrics["service_namespace"] = namespace
    metrics["service_name"] = service_name
    metrics["env"] = env
    metrics["node_key"] = node_key
    metrics["evidence_type"] = evidence_type
    metrics["coverage"] = coverage
    metrics["quality_score"] = round(quality_score, 2)
    metrics["data_sources"] = list(dict.fromkeys(data_sources))
    return node


def apply_edge_contract(
    edge: Dict[str, Any],
    source_node: Optional[Dict[str, Any]] = None,
    target_node: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """将边转换为统一契约字段（保留兼容字段）。"""
    metrics = edge.setdefault("metrics", {})

    source_service = _as_text(
        edge.get("source_service")
        or (edge.get("metrics") or {}).get("source_service")
        or (source_node or {}).get("service", {}).get("name")
        or edge.get("source")
    )
    target_service = _as_text(
        edge.get("target_service")
        or (edge.get("metrics") or {}).get("target_service")
        or (target_node or {}).get("service", {}).get("name")
        or edge.get("target")
    )
    source_namespace = _as_text(
        edge.get("source_namespace")
        or (edge.get("metrics") or {}).get("source_namespace")
        or (source_node or {}).get("service", {}).get("namespace")
        or "default"
    )
    target_namespace = _as_text(
        edge.get("target_namespace")
        or (edge.get("metrics") or {}).get("target_namespace")
        or (target_node or {}).get("service", {}).get("namespace")
        or "default"
    )

    source_key = (source_node or {}).get("node_key")
    target_key = (target_node or {}).get("node_key")
    if not source_key:
        source_key = build_node_key(source_namespace, source_service, "prod")
    if not target_key:
        target_key = build_node_key(target_namespace, target_service, "prod")

    operation_name = metrics.get("operation_name") or edge.get("label")
    protocol = _as_text(edge.get("protocol") or metrics.get("protocol"), "")
    if not protocol:
        protocol = infer_protocol(operation_name, "unknown")
    endpoint_pattern = normalize_endpoint_pattern(
        edge.get("endpoint_pattern") or metrics.get("endpoint_pattern") or operation_name
    )
    edge_key = build_edge_key(source_key, target_key, protocol, endpoint_pattern)

    data_source = metrics.get("data_source", "unknown")
    data_sources = metrics.get("data_sources")
    if not data_sources:
        data_sources = [data_source] if data_source else []
    evidence_type = evidence_type_from_source(data_source)

    call_count = metrics.get("call_count")
    source_log_count = (source_node or {}).get("metrics", {}).get("log_count")
    source_trace_count = (source_node or {}).get("metrics", {}).get("trace_count")
    coverage = coverage_score(
        call_count=call_count,
        log_count=source_log_count,
        trace_count=source_trace_count,
        data_sources=data_sources,
    )

    quality_score = float(metrics.get("quality_score") or 0.0)
    if quality_score <= 0:
        confidence = max(0.0, float(metrics.get("confidence") or 0.0))
        error_rate = max(0.0, float(metrics.get("error_rate") or 0.0))
        quality_score = max(0.0, min(100.0, confidence * 100.0 - error_rate * 50.0))

    p95 = float(metrics.get("p95") or 0.0)
    p99 = float(metrics.get("p99") or 0.0)
    timeout_rate = float(metrics.get("timeout_rate") or 0.0)

    edge["edge_key"] = edge_key
    edge["protocol"] = protocol
    edge["endpoint_pattern"] = endpoint_pattern
    edge["evidence_type"] = evidence_type
    edge["coverage"] = round(coverage, 3)
    edge["quality_score"] = round(quality_score, 2)
    edge["p95"] = round(p95, 2)
    edge["p99"] = round(p99, 2)
    edge["timeout_rate"] = round(timeout_rate, 4)
    edge["source_service"] = source_service
    edge["target_service"] = target_service
    edge["source_namespace"] = source_namespace
    edge["target_namespace"] = target_namespace
    edge["source_node_key"] = source_key
    edge["target_node_key"] = target_key

    metrics["edge_key"] = edge_key
    metrics["protocol"] = protocol
    metrics["endpoint_pattern"] = endpoint_pattern
    metrics["evidence_type"] = evidence_type
    metrics["coverage"] = round(coverage, 3)
    metrics["quality_score"] = round(quality_score, 2)
    metrics["p95"] = round(p95, 2)
    metrics["p99"] = round(p99, 2)
    metrics["timeout_rate"] = round(timeout_rate, 4)
    metrics["source_service"] = source_service
    metrics["target_service"] = target_service
    metrics["source_namespace"] = source_namespace
    metrics["target_namespace"] = target_namespace
    metrics["source_node_key"] = source_key
    metrics["target_node_key"] = target_key
    metrics["data_sources"] = list(dict.fromkeys(data_sources))
    return edge
