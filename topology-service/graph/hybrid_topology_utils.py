"""Utility helpers extracted from hybrid_topology module."""

from __future__ import annotations

import copy
import logging
import os
import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


def parse_env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = str(raw).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def parse_env_int(name: str, default: int, minimum: int = None, maximum: int = None) -> int:
    raw = os.getenv(name)
    if raw is None:
        value = default
    else:
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError):
            value = default
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def sanitize_interval(time_window: str, default_value: str = "1 HOUR") -> str:
    """Normalize INTERVAL values for SQL safety."""
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


def escape_sql_literal(value: str) -> str:
    """Escape single quotes for SQL string literals."""
    return str(value).replace("'", "''")


def parse_message_target_patterns(value: str) -> Set[str]:
    allowed = {"url", "kv", "proxy", "rpc"}
    modes = {
        token.strip().lower()
        for token in str(value or "").split(",")
        if token.strip()
    }
    selected = modes & allowed
    return selected or {"url"}


def parse_inference_mode(value: Any) -> str:
    mode = str(value or "").strip().lower()
    return mode if mode in {"rule", "hybrid_score"} else "rule"


def resolve_inference_mode_override(value: Any, default: str = "rule") -> str:
    if value is None:
        return parse_inference_mode(default)
    return parse_inference_mode(value)


def resolve_message_target_patterns_override(value: Any) -> Optional[Set[str]]:
    if value is None:
        return None
    if isinstance(value, (list, tuple, set)):
        raw = ",".join(str(item) for item in value)
    else:
        raw = str(value)
    return parse_message_target_patterns(raw)


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_service_alias_map(value: str) -> Dict[str, str]:
    """Parse service alias mapping string."""
    mapping: Dict[str, str] = {}
    raw = str(value or "").strip()
    if not raw:
        return mapping
    pairs = re.split(r"[;,]", raw)
    for pair in pairs:
        if "=" not in pair:
            continue
        alias, canonical = pair.split("=", 1)
        alias_key = alias.strip().lower()
        canonical_value = canonical.strip().lower()
        if alias_key and canonical_value:
            mapping[alias_key] = canonical_value
    return mapping


def extract_request_id(attrs: Dict[str, Any], message: str = "") -> str:
    """Extract request_id from attributes or log message."""
    keys = (
        "request_id",
        "request.id",
        "requestId",
        "req_id",
        "x-request-id",
        "x_request_id",
        "http.request_id",
        "trace.request_id",
    )
    for key in keys:
        value = attrs.get(key)
        if value:
            return str(value).strip()

    text = str(message or "")
    patterns = [
        r"(?:request[_-]?id|x-request-id)\s*[:=]\s*([a-zA-Z0-9\-_.]{6,})",
        r"\b([0-9a-fA-F]{8}-[0-9a-fA-F-]{27,})\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1).strip()
    return ""


def is_likely_outbound_message(text: str) -> bool:
    value = str(text or "").lower()
    if not value:
        return False
    return bool(re.search(r"\b(call|request|upstream|dial|invoke|forward|proxy|to)\b", value))


def is_likely_inbound_message(text: str) -> bool:
    value = str(text or "").lower()
    if not value:
        return False
    return bool(re.search(r"\b(receive|received|handle|start|serve|consume|process)\b", value))


def percentile(values: List[float], percentile_value: float) -> float:
    """Compute percentile with linear interpolation."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(float(ordered[0]), 2)

    k = max(0.0, min(1.0, percentile_value)) * (len(ordered) - 1)
    lower = int(k)
    upper = min(lower + 1, len(ordered) - 1)
    weight = k - lower
    value = ordered[lower] * (1.0 - weight) + ordered[upper] * weight
    return round(float(value), 2)


def time_window_seconds(time_window: str) -> int:
    """Convert window text like '1 HOUR' into seconds."""
    try:
        amount_str, unit = str(time_window).strip().split(maxsplit=1)
        amount = max(1, int(amount_str))
        unit = unit.upper()
        if "MINUTE" in unit:
            return amount * 60
        if "HOUR" in unit:
            return amount * 3600
        if "DAY" in unit:
            return amount * 86400
    except Exception as exc:
        logger.debug("Failed to parse time_window %r, fallback to 3600s (%s)", time_window, exc)
    return 3600


def timestamp_to_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    text = str(value or "")
    if not text:
        return datetime.now(timezone.utc)
    normalized = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return datetime.now(timezone.utc)


def dedup_service_sequence(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not records:
        return []
    sequence = [records[0]]
    for record in records[1:]:
        if record.get("service_name") != sequence[-1].get("service_name"):
            sequence.append(record)
    return sequence


def extract_host_candidates_from_token(raw_value: str) -> List[str]:
    text = str(raw_value or "").strip().strip("'\"")
    if not text:
        return []

    pieces = [text]
    if "|" in text:
        pieces.extend([piece for piece in text.split("|") if piece])

    candidates: List[str] = []
    seen: Set[str] = set()
    for piece in pieces:
        token = str(piece).strip().strip("'\"")
        if not token:
            continue
        if "://" in token:
            token = token.split("://", 1)[1]
        token = token.split("/", 1)[0]
        token = token.split("?", 1)[0]
        token = token.split("#", 1)[0]
        token = token.strip(".,;)]}")
        if not token:
            continue
        if ":" in token:
            head, tail = token.rsplit(":", 1)
            if tail.isdigit():
                token = head
        token = token.strip().lower().rstrip(".")
        if not token or token in seen:
            continue
        seen.add(token)
        candidates.append(token)
    return candidates


def match_service_from_host(
    host: str,
    known_services: Dict[str, str],
    exclude_hosts: Optional[Set[str]] = None,
) -> str:
    """
    Resolve host into canonical service name.
    Prefer exact service match, then fallback to first DNS token.
    """
    value = str(host or "").strip().lower().rstrip(".")
    if not value:
        return ""
    blocked = {token.strip().lower() for token in (exclude_hosts or set()) if str(token).strip()}
    if value in blocked:
        return ""
    if ":" in value:
        value = value.split(":", 1)[0]

    candidates: List[str] = [value]
    if ".svc." in value:
        candidates.append(value.split(".svc.", 1)[0])
    if "." in value:
        candidates.append(value.split(".", 1)[0])

    for candidate in candidates:
        normalized = candidate.strip("-_.")
        if not normalized:
            continue
        if normalized in known_services:
            return known_services[normalized]
        alt = normalized.replace("_", "-")
        if alt in known_services:
            return known_services[alt]
    return ""


def build_service_node_id(service_name: Any, namespace: Any) -> str:
    """Build a stable internal node id before contract normalization."""
    normalized_service = str(service_name or "").strip()
    normalized_namespace = str(namespace or "").strip()
    return f"{normalized_namespace}::{normalized_service}"


def extract_message_target_services(
    message: str,
    known_services: Dict[str, str],
    *,
    enabled: bool = True,
    patterns: Optional[Set[str]] = None,
    max_targets_per_log: int = 3,
    exclude_hosts: Optional[Set[str]] = None,
) -> List[Tuple[str, str]]:
    """
    Extract target services from log text using url/kv/proxy/rpc hints.
    Returns list of (target_service, hint_type).
    """
    if not enabled:
        return []

    effective_patterns = patterns or {"url"}
    effective_max_targets = max(1, int(max_targets_per_log))
    text = str(message or "")
    if not text:
        return []

    host_hints: List[Tuple[str, str]] = []

    if "url" in effective_patterns:
        url_hosts = re.findall(
            r"(?:https?|grpc)://([a-zA-Z0-9][a-zA-Z0-9._-]{0,252})(?::\d{2,5})?",
            text,
            flags=re.IGNORECASE,
        )
        host_hints.extend((host, "url_host") for host in url_hosts)

    if "kv" in effective_patterns:
        kv_values = re.findall(
            r"\b(?:host|upstream|upstream_host|target|dst|destination|peer(?:_service)?|"
            r"server(?:_address)?|authority)\s*[:=]\s*['\"]?([a-zA-Z0-9][a-zA-Z0-9._:/|\-]{0,300})",
            text,
            flags=re.IGNORECASE,
        )
        for value in kv_values:
            for candidate in extract_host_candidates_from_token(value):
                host_hints.append((candidate, "kv_host"))

    if "proxy" in effective_patterns:
        envoy_clusters = re.findall(
            r"\boutbound\|\d+\|\|([a-zA-Z0-9][a-zA-Z0-9._-]{0,252})",
            text,
            flags=re.IGNORECASE,
        )
        host_hints.extend((cluster, "proxy_cluster") for cluster in envoy_clusters)

        proxy_values = re.findall(
            r"\b(?:upstream_cluster|cluster_name)\s*[:=]\s*['\"]?([a-zA-Z0-9][a-zA-Z0-9._:/|\-]{0,300})",
            text,
            flags=re.IGNORECASE,
        )
        for value in proxy_values:
            for candidate in extract_host_candidates_from_token(value):
                host_hints.append((candidate, "proxy_cluster"))

    if "rpc" in effective_patterns:
        rpc_hosts = re.findall(
            r"\bdial\s+tcp\s+([a-zA-Z0-9][a-zA-Z0-9._:-]{0,252})",
            text,
            flags=re.IGNORECASE,
        )
        host_hints.extend((host, "rpc_dial") for host in rpc_hosts)

        error_to_hosts = re.findall(
            r"\b(?:connect(?:ion)?|timeout|refused|unavailable|failed)\b.{0,40}?\bto\s+([a-zA-Z0-9][a-zA-Z0-9._:-]{0,252})",
            text,
            flags=re.IGNORECASE,
        )
        host_hints.extend((host, "rpc_error_target") for host in error_to_hosts)

    if not host_hints:
        return []

    targets: List[Tuple[str, str]] = []
    seen_services: Set[str] = set()
    for host, hint in host_hints:
        for normalized_host in extract_host_candidates_from_token(host):
            matched = match_service_from_host(
                normalized_host,
                known_services,
                exclude_hosts=exclude_hosts,
            )
            if not matched or matched in seen_services:
                continue
            seen_services.add(matched)
            targets.append((matched, hint))
            if len(targets) >= effective_max_targets:
                return targets
        if len(targets) >= effective_max_targets:
            continue

    return targets


def merge_nodes(
    traces_nodes: List[Dict[str, Any]],
    logs_nodes: List[Dict[str, Any]],
    metrics_nodes: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Merge nodes from traces/logs/metrics sources."""
    def _normalize_namespace(value: Any) -> str:
        return str(value or "").strip()

    def _normalize_service_name(value: Any) -> str:
        return str(value or "").strip().lower().replace("_", "-")

    def _namespace_quality(value: Any, service_name: str = "") -> int:
        token = _normalize_namespace(value)
        if not token:
            return 0
        lowered = token.lower()
        if lowered in {"unknown", "none", "null", "-", "n/a"}:
            return 0
        normalized_service = _normalize_service_name(service_name)
        normalized_token = lowered.replace("_", "-")
        if normalized_service and normalized_token == normalized_service:
            # 命名空间与服务名完全一致时可疑（常见于误用 attributes.namespace），降级质量。
            return 1
        if lowered == "default":
            return 1
        return 2

    def _resolve_namespace(node: Dict[str, Any]) -> Tuple[str, int]:
        metrics = node.get("metrics", {}) if isinstance(node.get("metrics"), dict) else {}
        service = node.get("service", {}) if isinstance(node.get("service"), dict) else {}
        service_name = (
            node.get("id")
            or service.get("name")
            or node.get("name")
            or node.get("label")
            or metrics.get("service_name")
            or ""
        )
        candidates = (
            node.get("namespace"),
            metrics.get("namespace"),
            metrics.get("service_namespace"),
            service.get("namespace"),
        )
        best_value = ""
        best_quality = -1
        for candidate in candidates:
            normalized = _normalize_namespace(candidate)
            quality = _namespace_quality(normalized, service_name=service_name)
            if quality > best_quality and normalized:
                best_value = normalized
                best_quality = quality
        if best_quality < 0:
            return "", 0
        return best_value, best_quality

    def _set_namespace(node: Dict[str, Any], namespace: str) -> None:
        normalized = _normalize_namespace(namespace)
        if not normalized:
            return
        node["namespace"] = normalized
        metrics = node.setdefault("metrics", {})
        metrics["namespace"] = normalized
        metrics["service_namespace"] = normalized
        service = node.get("service")
        if isinstance(service, dict):
            service["namespace"] = normalized

    def _merge_namespace_if_better(existing_node: Dict[str, Any], incoming_node: Dict[str, Any]) -> None:
        incoming_ns, incoming_quality = _resolve_namespace(incoming_node)
        if incoming_quality <= 0:
            return

        current_ns, current_quality = _resolve_namespace(existing_node)
        if incoming_quality < current_quality:
            return
        if incoming_quality == current_quality and current_ns:
            # 同等质量时保持已选命名空间稳定，避免不同来源来回覆盖。
            return

        _set_namespace(existing_node, incoming_ns)

    merged: Dict[str, Dict[str, Any]] = {}

    for node in traces_nodes:
        service_name = node["id"]
        merged[service_name] = copy.deepcopy(node)
        metrics = merged[service_name].setdefault("metrics", {})
        metrics.setdefault("data_source", "traces")
        metrics.setdefault("data_sources", ["traces"])

    for node in logs_nodes:
        service_name = node["id"]
        if service_name in merged:
            existing = merged[service_name]
            logs_metrics = node.get("metrics", {})
            for key, value in logs_metrics.items():
                if key not in existing["metrics"]:
                    existing["metrics"][key] = value
            _merge_namespace_if_better(existing, node)
            data_sources = existing["metrics"].setdefault("data_sources", [])
            if "logs" not in data_sources:
                data_sources.append("logs")
        else:
            merged[service_name] = copy.deepcopy(node)
            metrics = merged[service_name].setdefault("metrics", {})
            metrics.setdefault("data_source", "logs")
            metrics.setdefault("data_sources", ["logs"])

    for node in metrics_nodes:
        service_name = node["id"]
        if service_name in merged:
            existing = merged[service_name]
            metrics_data = node.get("metrics", {})
            for key, value in metrics_data.items():
                if key not in existing["metrics"]:
                    existing["metrics"][key] = value
            _merge_namespace_if_better(existing, node)
            data_sources = existing.get("metrics", {}).setdefault("data_sources", [])
            if "metrics" not in data_sources:
                data_sources.append("metrics")
        else:
            merged[service_name] = copy.deepcopy(node)
            metrics = merged[service_name].setdefault("metrics", {})
            metrics.setdefault("data_source", "metrics")
            metrics.setdefault("data_sources", ["metrics"])

    return list(merged.values())


def merge_edges(
    traces_edges: List[Dict[str, Any]],
    logs_edges: List[Dict[str, Any]],
    metrics_edges: List[Dict[str, Any]],
    metrics_boost: float = 0.1,
) -> List[Dict[str, Any]]:
    """Merge edges from traces/logs/metrics sources."""
    merged: Dict[Tuple[str, str], Dict[str, Any]] = {}

    for edge in traces_edges:
        key = (edge["source"], edge["target"])
        merged[key] = copy.deepcopy(edge)
        metrics = merged[key].setdefault("metrics", {})
        metrics.setdefault("data_source", "traces")
        metrics.setdefault("data_sources", ["traces"])

    for edge in logs_edges:
        key = (edge["source"], edge["target"])
        if key in merged:
            existing = merged[key]
            existing_metrics = existing.setdefault("metrics", {})
            data_sources = existing_metrics.setdefault("data_sources", [])
            if "logs_heuristic" not in data_sources:
                data_sources.append("logs_heuristic")
            if "reason" not in existing_metrics:
                existing_metrics["reason"] = edge.get("metrics", {}).get("reason")
        else:
            merged[key] = copy.deepcopy(edge)
            metrics = merged[key].setdefault("metrics", {})
            metrics.setdefault("data_source", "logs_heuristic")
            metrics.setdefault("data_sources", ["logs_heuristic"])

    for edge in metrics_edges:
        key = (edge["source"], edge["target"])
        if key in merged:
            existing = merged[key]
            existing_metrics = existing.setdefault("metrics", {})
            existing_conf = existing_metrics.get("confidence", 0)
            existing_metrics["confidence"] = min(1.0, float(existing_conf) + float(metrics_boost))
            data_sources = existing_metrics.setdefault("data_sources", [])
            if "metrics" not in data_sources:
                data_sources.append("metrics")
        else:
            merged[key] = copy.deepcopy(edge)
            metrics = merged[key].setdefault("metrics", {})
            metrics.setdefault("data_source", "metrics")
            metrics.setdefault("data_sources", ["metrics"])

    return list(merged.values())


def apply_aggregated_edge_metrics(
    merged_edges: List[Dict[str, Any]],
    aggregated: Dict[str, Dict[str, Any]],
) -> None:
    """Overlay aggregated RED metrics onto merged edges."""
    if not merged_edges or not aggregated:
        return

    for edge in merged_edges:
        key = f"{edge.get('source', '')}->{edge.get('target', '')}"
        if key not in aggregated:
            continue

        target_metrics = aggregated[key]
        metrics = edge.setdefault("metrics", {})
        for field in (
            "call_count",
            "error_count",
            "error_rate",
            "p95",
            "p99",
            "timeout_rate",
            "retries",
            "pending",
            "dlq",
        ):
            value = target_metrics.get(field)
            if value is None:
                continue
            current = metrics.get(field)
            if current is None:
                metrics[field] = value
                continue
            if isinstance(current, (int, float)) and float(current) == 0.0:
                metrics[field] = value


def apply_contract_schema(
    nodes: List[Dict[str, Any]],
    edges: List[Dict[str, Any]],
    *,
    apply_node_contract_fn: Any,
    apply_edge_contract_fn: Any,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Apply topology contract schema to nodes and edges."""
    contract_nodes: List[Dict[str, Any]] = []
    node_map: Dict[str, Dict[str, Any]] = {}
    node_map_by_service_namespace: Dict[Tuple[str, str], Dict[str, Any]] = {}
    node_map_by_service: Dict[str, List[Dict[str, Any]]] = {}

    for node in nodes:
        original_id = node.get("id")
        converted = apply_node_contract_fn(node)
        node_key = str(converted.get("node_key") or "").strip()
        if node_key:
            converted["legacy_id"] = original_id
            converted["id"] = node_key
            metrics = converted.setdefault("metrics", {})
            metrics.setdefault("legacy_id", original_id)
        contract_nodes.append(converted)
        if original_id is not None:
            node_map[original_id] = converted
        node_map[converted.get("id")] = converted
        node_key = converted.get("node_key")
        if node_key:
            node_map[node_key] = converted
        service = converted.get("service") if isinstance(converted.get("service"), dict) else {}
        service_name = str(service.get("name") or converted.get("name") or converted.get("label") or "").strip().lower()
        service_namespace = str(service.get("namespace") or converted.get("namespace") or "").strip().lower()
        if service_name:
            bucket = node_map_by_service.setdefault(service_name, [])
            bucket.append(converted)
            if service_namespace:
                node_map_by_service_namespace.setdefault((service_name, service_namespace), converted)

    contract_edges: List[Dict[str, Any]] = []
    for edge in edges:
        source_node = node_map.get(edge.get("source"))
        target_node = node_map.get(edge.get("target"))
        edge_metrics = edge.get("metrics") if isinstance(edge.get("metrics"), dict) else {}

        if source_node is None:
            source_service = str(edge.get("source_service") or edge_metrics.get("source_service") or "").strip().lower()
            source_namespace = str(edge.get("source_namespace") or edge_metrics.get("source_namespace") or "").strip().lower()
            if source_service:
                source_node = node_map_by_service_namespace.get((source_service, source_namespace))
                if source_node is None:
                    source_candidates = node_map_by_service.get(source_service) or []
                    if len(source_candidates) == 1:
                        source_node = source_candidates[0]

        if target_node is None:
            target_service = str(edge.get("target_service") or edge_metrics.get("target_service") or "").strip().lower()
            target_namespace = str(edge.get("target_namespace") or edge_metrics.get("target_namespace") or "").strip().lower()
            if target_service:
                target_node = node_map_by_service_namespace.get((target_service, target_namespace))
                if target_node is None:
                    target_candidates = node_map_by_service.get(target_service) or []
                    if len(target_candidates) == 1:
                        target_node = target_candidates[0]

        source_node_for_hook = copy.deepcopy(source_node) if isinstance(source_node, dict) else source_node
        if isinstance(source_node_for_hook, dict) and source_node_for_hook.get("legacy_id") is not None:
            source_node_for_hook["id"] = source_node_for_hook.get("legacy_id")

        target_node_for_hook = copy.deepcopy(target_node) if isinstance(target_node, dict) else target_node
        if isinstance(target_node_for_hook, dict) and target_node_for_hook.get("legacy_id") is not None:
            target_node_for_hook["id"] = target_node_for_hook.get("legacy_id")

        converted = apply_edge_contract_fn(
            edge,
            source_node=source_node_for_hook,
            target_node=target_node_for_hook,
        )
        metrics = converted.setdefault("metrics", {})
        source_key = str(
            (source_node or {}).get("id")
            or converted.get("source_node_key")
            or metrics.get("source_node_key")
            or converted.get("source")
            or ""
        ).strip()
        target_key = str(
            (target_node or {}).get("id")
            or converted.get("target_node_key")
            or metrics.get("target_node_key")
            or converted.get("target")
            or ""
        ).strip()
        if source_key:
            converted["source_node_key"] = source_key
            metrics["source_node_key"] = source_key
        if target_key:
            converted["target_node_key"] = target_key
            metrics["target_node_key"] = target_key
        source_service = str(converted.get("source_service") or metrics.get("source_service") or "").strip()
        target_service = str(converted.get("target_service") or metrics.get("target_service") or "").strip()
        converted["source"] = source_service or source_key or str(converted.get("source") or "").strip()
        converted["target"] = target_service or target_key or str(converted.get("target") or "").strip()
        converted["id"] = str(converted.get("edge_key") or converted.get("id") or "").strip() or converted.get("id")
        contract_edges.append(converted)

    return contract_nodes, contract_edges


def dedup_edges_by_metric_score(edges: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Deduplicate edges by (source, target), preferring higher call_count+confidence score.
    """
    dedup: Dict[Tuple[str, str, str, str], Dict[str, Any]] = {}

    for edge in edges:
        metrics = edge.get("metrics", {}) if isinstance(edge.get("metrics"), dict) else {}
        source_namespace = str(edge.get("source_namespace") or metrics.get("source_namespace") or "").strip()
        target_namespace = str(edge.get("target_namespace") or metrics.get("target_namespace") or "").strip()
        key = (edge.get("source"), edge.get("target"), source_namespace, target_namespace)
        if key not in dedup:
            dedup[key] = copy.deepcopy(edge)
            continue

        current = dedup[key].get("metrics", {})
        candidate = metrics
        current_score = to_float(current.get("call_count"), 0.0) + to_float(current.get("confidence"), 0.0)
        candidate_score = to_float(candidate.get("call_count"), 0.0) + to_float(candidate.get("confidence"), 0.0)
        if candidate_score > current_score:
            dedup[key] = copy.deepcopy(edge)

    return list(dedup.values())


def is_service_pair_related(service1: str, service2: str) -> bool:
    """Heuristic check whether two services are likely related."""
    service1_lower = str(service1 or "").lower()
    service2_lower = str(service2 or "").lower()

    if "frontend" in service1_lower and "backend" in service2_lower:
        return True
    if "frontend" in service2_lower and "backend" in service1_lower:
        return True

    db_keywords = ["database", "db", "mysql", "postgres", "mongodb", "clickhouse"]
    service1_is_db = any(keyword in service1_lower for keyword in db_keywords)
    service2_is_db = any(keyword in service2_lower for keyword in db_keywords)
    if service1_is_db ^ service2_is_db:
        return True

    cache_keywords = ["cache", "redis", "memcached"]
    service1_is_cache = any(keyword in service1_lower for keyword in cache_keywords)
    service2_is_cache = any(keyword in service2_lower for keyword in cache_keywords)
    if service1_is_cache ^ service2_is_cache:
        return True

    service1_is_registry = "registry" in service1_lower
    service2_is_registry = "registry" in service2_lower
    if service1_is_registry ^ service2_is_registry:
        return True

    return False


def should_call(service1: str, service2: str) -> bool:
    """Heuristic direction check whether service1 should call service2."""
    del service2  # reserved for future bidirectional rules
    service1_lower = str(service1 or "").lower()

    if "frontend" in service1_lower or "backend" in service1_lower:
        return True

    db_keywords = ["database", "db", "mysql", "postgres", "mongodb", "redis", "cache"]
    if any(keyword in service1_lower for keyword in db_keywords):
        return False

    if "registry" in service1_lower:
        return False

    return True


def get_relation_reason(caller: str, callee: str) -> str:
    """Build heuristic reason labels for an inferred relation."""
    reasons: List[str] = []
    caller_lower = str(caller or "").lower()
    callee_lower = str(callee or "").lower()

    if "frontend" in caller_lower:
        reasons.append("frontend_pattern")
    elif "backend" in caller_lower:
        reasons.append("backend_pattern")

    db_keywords = ["database", "db", "mysql", "postgres", "redis", "cache"]
    if any(keyword in callee_lower for keyword in db_keywords):
        reasons.append("data_access_pattern")

    if "registry" in callee_lower:
        reasons.append("image_pull_pattern")

    return ", ".join(reasons) if reasons else "heuristic_pattern"


def get_data_sources(
    traces_data: Dict[str, Any],
    logs_data: Dict[str, Any],
    metrics_data: Dict[str, Any],
) -> List[str]:
    """Get enabled data sources list from non-empty node/edge payloads."""
    sources: List[str] = []

    if traces_data.get("nodes") or traces_data.get("edges"):
        sources.append("traces")
    if logs_data.get("nodes") or logs_data.get("edges"):
        sources.append("logs")
    if metrics_data.get("nodes") or metrics_data.get("edges"):
        sources.append("metrics")

    return sources


def partition_prepared_inference_records(
    prepared: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Split prepared log records into request_id / trace_id / fallback buckets."""
    request_groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    trace_groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    fallback_records: List[Dict[str, Any]] = []

    for item in prepared:
        rid = str(item.get("request_id") or "").strip()
        if rid:
            request_groups[rid].append(item)
            continue

        trace_id = str(item.get("trace_id") or "").strip()
        if trace_id:
            trace_groups[trace_id].append(item)
        else:
            fallback_records.append(item)

    return {
        "request_groups": request_groups,
        "trace_groups": trace_groups,
        "fallback_records": fallback_records,
    }


def compute_dropped_bidirectional_edges(
    edge_acc: Dict[Tuple[Any, ...], Dict[str, Any]],
    *,
    inference_mode: str,
    min_support_time_window: int,
) -> Set[Tuple[str, str]]:
    """Suppress noisy reverse edges for time_window / trace_id inferred relations."""
    dropped_bidirectional: Set[Tuple[str, str]] = set()
    for key, item in edge_acc.items():
        if not isinstance(key, tuple):
            continue
        if len(key) == 4:
            source, target, source_namespace, target_namespace = key
            reverse_keys = [
                (target, source, target_namespace, source_namespace),
                (target, source),
            ]
        elif len(key) == 2:
            source, target = key
            reverse_keys = [
                (target, source),
                (target, source, "", ""),
            ]
        else:
            continue

        reverse_item = None
        for reverse_key in reverse_keys:
            if reverse_key in edge_acc:
                reverse_item = edge_acc[reverse_key]
                break

        pair_key = (source, target)
        reverse_pair_key = (target, source)
        if source == target or reverse_item is None:
            continue
        if pair_key in dropped_bidirectional or reverse_pair_key in dropped_bidirectional:
            continue

        method_counts = item.get("method_counts") or {}
        reverse_method_counts = reverse_item.get("method_counts") or {}
        method = resolve_dominant_inference_method(method_counts, default="time_window")
        reverse_method = resolve_dominant_inference_method(reverse_method_counts, default="time_window")
        if method not in {"time_window", "trace_id"} or reverse_method not in {"time_window", "trace_id"}:
            continue

        count = (
            to_float(item.get("weighted_score"), 0.0)
            if inference_mode == "hybrid_score"
            else float(item.get("count") or 0)
        )
        reverse_count = (
            to_float(reverse_item.get("weighted_score"), 0.0)
            if inference_mode == "hybrid_score"
            else float(reverse_item.get("count") or 0)
        )
        bigger = max(count, reverse_count)
        smaller = min(count, reverse_count)

        if bigger < int(min_support_time_window) and abs(count - reverse_count) <= (
            0.9 if inference_mode == "hybrid_score" else 1.0
        ):
            dropped_bidirectional.add(pair_key)
            dropped_bidirectional.add(reverse_pair_key)
            continue

        ratio_threshold = 1.35 if inference_mode == "hybrid_score" else 1.5
        if smaller > 0 and bigger / smaller >= ratio_threshold:
            if count > reverse_count:
                dropped_bidirectional.add(reverse_pair_key)
            else:
                dropped_bidirectional.add(pair_key)
    return dropped_bidirectional


def build_inference_stats(
    *,
    total_candidates: int,
    request_id_groups: int,
    request_id_edges: int,
    trace_id_groups: int,
    trace_id_edges: int,
    message_target_edges: int,
    time_window_edges: int,
    dropped_bidirectional_edges: int,
    filtered_edges: int,
    method_name: str,
    message_target_enabled: bool,
    inference_mode: str,
    message_target_patterns: Set[str],
    message_target_min_support: int,
    message_target_max_per_log: int,
    evidence_sufficiency_scores: List[float],
) -> Dict[str, Any]:
    """Build full inference stats payload with aggregate evidence metadata."""
    return {
        "total_candidates": int(total_candidates),
        "request_id_groups": int(request_id_groups),
        "request_id_edges": int(request_id_edges),
        "trace_id_groups": int(trace_id_groups),
        "trace_id_edges": int(trace_id_edges),
        "message_target_edges": int(message_target_edges),
        "time_window_edges": int(time_window_edges),
        "dropped_bidirectional_edges": int(dropped_bidirectional_edges),
        "filtered_edges": int(filtered_edges),
        "method": str(method_name or ""),
        "message_target_enabled": bool(message_target_enabled),
        "inference_mode": str(inference_mode or ""),
        "message_target_patterns": sorted(message_target_patterns),
        "message_target_min_support": int(message_target_min_support),
        "message_target_max_per_log": int(message_target_max_per_log),
        "avg_evidence_sufficiency_score": (
            round(
                sum(float(value) for value in evidence_sufficiency_scores) / len(evidence_sufficiency_scores),
                2,
            )
            if evidence_sufficiency_scores
            else 0.0
        ),
        "evidence_sparse": (
            (int(request_id_edges) + int(trace_id_edges) + int(message_target_edges) + int(time_window_edges)) == 0
        ),
    }


def build_inference_method_policies(
    *,
    min_support_request_id: int,
    min_support_trace_id: int,
    min_support_message_target: int,
    min_support_time_window: int,
) -> Dict[str, Dict[str, Any]]:
    """Build method policy maps for support thresholds, base confidence and reason."""
    return {
        "min_support": {
            "request_id": int(min_support_request_id),
            "trace_id": int(min_support_trace_id),
            "message_target": int(min_support_message_target),
            "time_window": int(min_support_time_window),
        },
        "base_confidence": {
            "request_id": 0.80,
            "trace_id": 0.66,
            "message_target": 0.74,
            "time_window": 0.35,
        },
        "reason": {
            "request_id": "request_id_correlation",
            "trace_id": "trace_id_correlation",
            "message_target": "message_endpoint_pattern",
            "time_window": "time_window_correlation",
        },
    }


def compute_directional_consistency(
    edge_acc: Dict[Tuple[str, str, str, str], Dict[str, Any]],
    *,
    source: str,
    target: str,
    support_value: float,
    inference_mode: str,
) -> float:
    """Compute directional consistency against reverse edge support."""
    reverse_item = edge_acc.get((target, source))
    if not reverse_item:
        return 1.0

    reverse_count = int(reverse_item.get("count") or 0)
    reverse_weighted = to_float(reverse_item.get("weighted_score"), float(reverse_count))
    reverse_support_value = (
        float(reverse_count) + reverse_weighted * 0.5
        if inference_mode == "hybrid_score"
        else float(reverse_count)
    )
    directional_consistency = float(support_value) / max(
        0.001,
        float(support_value) + float(reverse_support_value),
    )
    return max(0.0, min(1.0, float(directional_consistency)))


def build_inference_confidence_explain(
    *,
    method: str,
    inference_mode: str,
    support_value: float,
    min_support: float,
    dominant_ratio: float,
    namespace_consistency: float,
    temporal_stability: float,
    directional_consistency: float,
) -> str:
    """Build confidence explanation text while preserving existing contract format."""
    normalized_method = str(method or "").strip().lower()
    if normalized_method == "request_id":
        base = "request_id matched"
    elif normalized_method == "trace_id":
        base = "trace_id matched"
    elif normalized_method == "message_target":
        base = "message url host matched"
    else:
        base = "request_id/trace_id missing, fallback time-window correlation"

    if inference_mode != "hybrid_score":
        return base
    return (
        f"{base}; support={float(support_value):.2f}/{float(min_support):.2f}; "
        f"dominance={float(dominant_ratio):.2f}; ns={float(namespace_consistency):.2f}; "
        f"temporal={float(temporal_stability):.2f}; direction={float(directional_consistency):.2f}"
    )


def compute_rule_mode_evidence_sufficiency(
    *,
    count: int,
    base_support: int,
    method_count_size: int,
) -> float:
    """Compute rule-mode evidence sufficiency score with diversity bonus."""
    return min(
        100.0,
        min(1.0, float(count) / max(1.0, float(base_support))) * 75.0
        + (25.0 if int(method_count_size) > 1 else 12.0),
    )


def resolve_dominant_inference_method(method_counts: Any, default: str = "time_window") -> str:
    """Resolve dominant inference method from method count mapping."""
    counts = method_counts or {}
    if hasattr(counts, "most_common"):
        top = counts.most_common(1)
        if top:
            return str(top[0][0] or default)
    if isinstance(counts, dict) and counts:
        return str(max(counts.items(), key=lambda item: item[1])[0] or default)
    return str(default)


def compute_support_value(
    *,
    count: int,
    weighted_score: float,
    inference_mode: str,
) -> float:
    """Compute support value under rule / hybrid_score modes."""
    if str(inference_mode or "").strip().lower() == "hybrid_score":
        return float(count) + float(weighted_score) * 0.5
    return float(count)


def compute_inference_feature_ratios(
    item: Dict[str, Any],
    *,
    count: int,
    weighted_score: float,
    dominant_method: str,
) -> Dict[str, Any]:
    """Compute feature ratios used by inference confidence scoring."""
    method_counts = item.get("method_counts") or {}
    dominant_count = int(method_counts.get(dominant_method, 0))
    diversity_ratio = min(1.0, len(method_counts) / 3.0)
    dominant_ratio = min(1.0, float(dominant_count) / max(1.0, float(count)))
    namespace_total = int(item.get("namespace_match_total") or 0)
    namespace_hits = int(item.get("namespace_match_hits") or 0)
    namespace_consistency = (
        float(namespace_hits) / float(namespace_total)
        if namespace_total > 0
        else 0.55
    )
    weighted_density = float(weighted_score) / max(1.0, float(count))
    return {
        "dominant_count": dominant_count,
        "method_count_size": len(method_counts),
        "dominant_ratio": dominant_ratio,
        "diversity_ratio": diversity_ratio,
        "namespace_consistency": namespace_consistency,
        "weighted_density": weighted_density,
        "method_breakdown": dict(method_counts),
    }


def build_inferred_edge_payload(
    *,
    source: str,
    target: str,
    count: int,
    confidence: float,
    reason: str,
    dominant_method: str,
    method_breakdown: Dict[str, Any],
    evidence_chain: List[Dict[str, Any]],
    confidence_explain: str,
    evidence_sufficiency_score: float,
    inference_mode: str,
    support_value: float,
    min_support_required: float,
    namespace_consistency: float,
    temporal_stability: float,
    directional_consistency: float,
    last_seen: Any,
    source_namespace: str = "",
    target_namespace: str = "",
) -> Dict[str, Any]:
    """Build inferred edge output payload with stable contract fields."""
    return {
        "id": f"{source}-{target}-inferred",
        "source": source,
        "target": target,
        "source_service": source,
        "target_service": target,
        "source_namespace": str(source_namespace or "").strip(),
        "target_namespace": str(target_namespace or "").strip(),
        "label": "inferred-calls",
        "type": "calls",
        "metrics": {
            "source_service": source,
            "target_service": target,
            "source_namespace": str(source_namespace or "").strip(),
            "target_namespace": str(target_namespace or "").strip(),
            "call_count": count,
            "confidence": round(confidence, 3),
            "data_source": "inferred",
            "data_sources": ["inferred"],
            "reason": reason,
            "inference_method": dominant_method,
            "inference_method_breakdown": method_breakdown,
            "confidence_explain": confidence_explain,
            "evidence_chain": list(evidence_chain or []),
            "evidence_sufficiency_score": float(evidence_sufficiency_score),
            "inference_mode": str(inference_mode or ""),
            "support_value": round(float(support_value), 3),
            "min_support_required": round(float(min_support_required), 3),
            "namespace_consistency": round(float(namespace_consistency), 3),
            "temporal_stability": round(float(temporal_stability), 3),
            "directional_consistency": round(float(directional_consistency), 3),
            "last_seen": last_seen.isoformat() if isinstance(last_seen, datetime) else None,
            "p95": 0.0,
            "p99": 0.0,
            "timeout_rate": 0.0,
            "retries": 0.0,
            "pending": 0.0,
            "dlq": 0.0,
        },
    }


def compute_inference_confidence_and_evidence(
    *,
    inference_mode: str,
    dominant_method: str,
    support_value: float,
    dynamic_min_support: float,
    dominant_ratio: float,
    diversity_ratio: float,
    namespace_consistency: float,
    temporal_stability: float,
    weighted_density: float,
    directional_consistency: float,
    count: int,
    base_support: int,
    method_count_size: int,
    method_base_confidence: Dict[str, float],
    score_hybrid_edge_fn: Any = None,
) -> Dict[str, float]:
    """
    Compute confidence and evidence score for inferred edge under rule/hybrid modes.
    Keeps legacy formulas unchanged while centralizing branch logic.
    """
    mode = str(inference_mode or "").strip().lower()
    if mode == "hybrid_score":
        if score_hybrid_edge_fn is None:
            raise ValueError("score_hybrid_edge_fn is required for hybrid_score mode")
        score_result = score_hybrid_edge_fn(
            method=dominant_method,
            support_value=float(support_value),
            min_support=float(dynamic_min_support),
            dominant_ratio=float(dominant_ratio),
            diversity_ratio=float(diversity_ratio),
            namespace_consistency=float(namespace_consistency),
            temporal_stability=float(temporal_stability),
            weighted_density=float(weighted_density),
            directional_consistency=float(directional_consistency),
        )
        confidence = to_float((score_result or {}).get("confidence"), 0.0)
        evidence_sufficiency_score = to_float((score_result or {}).get("evidence_score"), 0.0)
    else:
        confidence = min(
            0.92,
            float(method_base_confidence.get(dominant_method, 0.35)) + min(0.22, int(count) * 0.015),
        )
        evidence_sufficiency_score = compute_rule_mode_evidence_sufficiency(
            count=int(count),
            base_support=int(base_support),
            method_count_size=int(method_count_size),
        )

    return {
        "confidence": float(confidence),
        "evidence_sufficiency_score": round(float(evidence_sufficiency_score), 2),
    }


def evaluate_inference_edge(
    *,
    edge_acc: Dict[Tuple[str, str], Dict[str, Any]],
    source: str,
    target: str,
    item: Dict[str, Any],
    inference_mode: str,
    service_log_volume: Dict[str, int],
    method_min_support: Dict[str, int],
    method_base_confidence: Dict[str, float],
    method_reason: Dict[str, str],
    default_min_support: int,
    estimate_dynamic_support_fn: Any,
    temporal_stability_fn: Any,
    score_hybrid_edge_fn: Any,
) -> Optional[Dict[str, Any]]:
    """
    Evaluate one inferred edge candidate.
    Returns None when support is insufficient; otherwise returns payload + evidence score.
    """
    count = int(item.get("count") or 0)
    weighted_score = to_float(item.get("weighted_score"), float(count))
    dominant_method = resolve_dominant_inference_method(
        item.get("method_counts"),
        default="time_window",
    )
    base_support = int(method_min_support.get(dominant_method, int(default_min_support)))
    dynamic_min_support = int(
        estimate_dynamic_support_fn(
            base_support=base_support,
            source_volume=int(service_log_volume.get(source, 0)),
            method=dominant_method,
            inference_mode=inference_mode,
        )
    )
    support_value = compute_support_value(
        count=count,
        weighted_score=weighted_score,
        inference_mode=inference_mode,
    )
    if float(support_value) < float(dynamic_min_support):
        return None

    feature_ratios = compute_inference_feature_ratios(
        item,
        count=count,
        weighted_score=weighted_score,
        dominant_method=dominant_method,
    )
    temporal_stability = to_float(
        temporal_stability_fn(item.get("temporal_gaps") or []),
        0.0,
    )
    directional_consistency = compute_directional_consistency(
        edge_acc,
        source=source,
        target=target,
        support_value=support_value,
        inference_mode=inference_mode,
    )
    scoring = compute_inference_confidence_and_evidence(
        inference_mode=inference_mode,
        dominant_method=dominant_method,
        support_value=support_value,
        dynamic_min_support=float(dynamic_min_support),
        dominant_ratio=feature_ratios["dominant_ratio"],
        diversity_ratio=feature_ratios["diversity_ratio"],
        namespace_consistency=feature_ratios["namespace_consistency"],
        temporal_stability=temporal_stability,
        weighted_density=feature_ratios["weighted_density"],
        directional_consistency=directional_consistency,
        count=count,
        base_support=base_support,
        method_count_size=feature_ratios["method_count_size"],
        method_base_confidence=method_base_confidence,
        score_hybrid_edge_fn=score_hybrid_edge_fn,
    )
    confidence_explain = build_inference_confidence_explain(
        method=dominant_method,
        inference_mode=inference_mode,
        support_value=support_value,
        min_support=float(dynamic_min_support),
        dominant_ratio=feature_ratios["dominant_ratio"],
        namespace_consistency=feature_ratios["namespace_consistency"],
        temporal_stability=temporal_stability,
        directional_consistency=directional_consistency,
    )
    payload = build_inferred_edge_payload(
        source=source,
        target=target,
        source_namespace=str(item.get("source_namespace") or "").strip(),
        target_namespace=str(item.get("target_namespace") or "").strip(),
        count=count,
        confidence=scoring["confidence"],
        reason=method_reason.get(dominant_method, "time_window_correlation"),
        dominant_method=dominant_method,
        method_breakdown=feature_ratios["method_breakdown"],
        evidence_chain=item.get("evidence_chain") or [],
        confidence_explain=confidence_explain,
        evidence_sufficiency_score=scoring["evidence_sufficiency_score"],
        inference_mode=inference_mode,
        support_value=support_value,
        min_support_required=float(dynamic_min_support),
        namespace_consistency=feature_ratios["namespace_consistency"],
        temporal_stability=temporal_stability,
        directional_consistency=directional_consistency,
        last_seen=item.get("last_seen"),
    )
    return {
        "payload": payload,
        "evidence_sufficiency_score": scoring["evidence_sufficiency_score"],
    }


def accumulate_group_sequence_edges(
    *,
    groups: Dict[str, List[Dict[str, Any]]],
    group_field_name: str,
    method: str,
    inference_mode: str,
    hybrid_weight: float,
    dedup_sequence_fn: Any,
    add_inferred_fn: Any,
    normalize_namespace_fn: Any,
) -> int:
    """
    Accumulate inferred edges from grouped ordered records (e.g. request_id / trace_id).
    Returns number of added inferred edges.
    """
    added_edges = 0
    for group_value, records in groups.items():
        if len(records) < 2:
            continue

        sequence = dedup_sequence_fn(records)
        for idx in range(len(sequence) - 1):
            current = sequence[idx]
            nxt = sequence[idx + 1]
            source = current.get("service_name")
            target = nxt.get("service_name")
            if not source or not target:
                continue

            evidence = {
                group_field_name: group_value,
                "source_log_id": current.get("id"),
                "target_log_id": nxt.get("id"),
                "source_ts": current.get("ts").isoformat() if current.get("ts") else "",
                "target_ts": nxt.get("ts").isoformat() if nxt.get("ts") else "",
                "source_namespace": str(current.get("namespace") or "").strip(),
                "target_namespace": str(nxt.get("namespace") or "").strip(),
            }
            delta_sec = 0.0
            if current.get("ts") and nxt.get("ts"):
                delta_sec = max(0.0, (nxt["ts"] - current["ts"]).total_seconds())

            added = add_inferred_fn(
                source=source,
                target=target,
                source_namespace=str(current.get("namespace") or "").strip(),
                target_namespace=str(nxt.get("namespace") or "").strip(),
                evidence=evidence,
                method=method,
                event_ts=nxt.get("ts"),
                weight=float(hybrid_weight) if inference_mode == "hybrid_score" else 1.0,
                namespace_match=(
                    normalize_namespace_fn(current.get("namespace"))
                    == normalize_namespace_fn(nxt.get("namespace"))
                ),
                delta_sec=delta_sec,
            )
            if added:
                added_edges += 1
    return added_edges


def accumulate_message_target_edges(
    *,
    prepared: List[Dict[str, Any]],
    inference_mode: str,
    extract_message_target_services_fn: Any,
    add_inferred_fn: Any,
    patterns: Set[str],
    max_targets_per_log: int,
) -> int:
    """Accumulate inferred edges from message target extraction."""
    added_edges = 0
    for item in prepared:
        source = str(item.get("service_name") or "").strip()
        if not source:
            continue
        targets = extract_message_target_services_fn(
            message=item.get("message") or "",
            enabled=True,
            patterns=patterns,
            max_targets_per_log=max_targets_per_log,
        )
        for target, message_hint in targets:
            if target == source:
                continue
            added = add_inferred_fn(
                source=source,
                target=target,
                source_namespace=str(item.get("namespace") or "").strip(),
                target_namespace="",
                evidence={
                    "source_log_id": item.get("id"),
                    "source_ts": item.get("ts").isoformat() if item.get("ts") else "",
                    "message_hint": message_hint,
                    "target_service": target,
                    "source_namespace": str(item.get("namespace") or "").strip(),
                    "target_namespace": "",
                },
                method="message_target",
                event_ts=item.get("ts"),
                weight=1.1 if inference_mode == "hybrid_score" else 1.0,
            )
            if added:
                added_edges += 1
    return added_edges


def accumulate_time_window_fallback_edges(
    *,
    fallback_records: List[Dict[str, Any]],
    inference_mode: str,
    max_candidates_per_log: int,
    max_delta_sec: float,
    is_likely_outbound_message_fn: Any,
    is_likely_inbound_message_fn: Any,
    add_inferred_fn: Any,
    normalize_namespace_fn: Any,
) -> int:
    """Accumulate fallback inferred edges using time-window correlation rules."""
    added_edges = 0
    namespace_buckets: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for record in fallback_records:
        namespace_key = str(record.get("namespace") or "unknown")
        namespace_buckets[namespace_key].append(record)

    for records in namespace_buckets.values():
        if len(records) < 2:
            continue
        if inference_mode == "hybrid_score":
            for idx, current in enumerate(records):
                upper = min(len(records), idx + 1 + int(max_candidates_per_log))
                rank = 0
                for candidate_index in range(idx + 1, upper):
                    nxt = records[candidate_index]
                    if current.get("service_name") == nxt.get("service_name"):
                        continue

                    delta = (nxt["ts"] - current["ts"]).total_seconds()
                    if delta < 0:
                        continue
                    if delta > float(max_delta_sec):
                        break

                    rank += 1
                    proximity = max(0.0, 1.0 - (delta / max(float(max_delta_sec), 0.001)))
                    lexical_boost = 0.0
                    if is_likely_outbound_message_fn(current.get("message", "")):
                        lexical_boost += 0.14
                    if is_likely_inbound_message_fn(nxt.get("message", "")):
                        lexical_boost += 0.08
                    rank_penalty = max(0.0, (rank - 1) * 0.09)
                    score = max(0.05, 0.30 + proximity * 0.60 + lexical_boost - rank_penalty)
                    if score < 0.32:
                        continue

                    added = add_inferred_fn(
                        source=current.get("service_name"),
                        target=nxt.get("service_name"),
                        source_namespace=str(current.get("namespace") or "").strip(),
                        target_namespace=str(nxt.get("namespace") or "").strip(),
                        evidence={
                            "time_window_sec": round(delta, 3),
                            "source_log_id": current.get("id"),
                            "target_log_id": nxt.get("id"),
                            "source_ts": current.get("ts").isoformat() if current.get("ts") else "",
                            "target_ts": nxt.get("ts").isoformat() if nxt.get("ts") else "",
                            "source_namespace": str(current.get("namespace") or "").strip(),
                            "target_namespace": str(nxt.get("namespace") or "").strip(),
                            "candidate_rank": rank,
                            "window_score": round(score, 3),
                        },
                        method="time_window",
                        event_ts=nxt.get("ts"),
                        weight=score,
                        namespace_match=(
                            normalize_namespace_fn(current.get("namespace"))
                            == normalize_namespace_fn(nxt.get("namespace"))
                        ),
                        delta_sec=delta,
                    )
                    if added:
                        added_edges += 1
        else:
            for idx in range(len(records) - 1):
                current = records[idx]
                nxt = records[idx + 1]
                if current.get("service_name") == nxt.get("service_name"):
                    continue

                delta = (nxt["ts"] - current["ts"]).total_seconds()
                if delta < 0 or delta > float(max_delta_sec):
                    continue

                added = add_inferred_fn(
                    source=current.get("service_name"),
                    target=nxt.get("service_name"),
                    source_namespace=str(current.get("namespace") or "").strip(),
                    target_namespace=str(nxt.get("namespace") or "").strip(),
                    evidence={
                        "time_window_sec": round(delta, 3),
                        "source_log_id": current.get("id"),
                        "target_log_id": nxt.get("id"),
                        "source_ts": current.get("ts").isoformat() if current.get("ts") else "",
                        "target_ts": nxt.get("ts").isoformat() if nxt.get("ts") else "",
                        "source_namespace": str(current.get("namespace") or "").strip(),
                        "target_namespace": str(nxt.get("namespace") or "").strip(),
                    },
                    method="time_window",
                    event_ts=nxt.get("ts"),
                    weight=1.0,
                    namespace_match=(
                        normalize_namespace_fn(current.get("namespace"))
                        == normalize_namespace_fn(nxt.get("namespace"))
                    ),
                    delta_sec=delta,
                )
                if added:
                    added_edges += 1
    return added_edges


def resolve_inference_runtime_settings(
    *,
    inference_mode: Optional[str],
    default_inference_mode: str,
    message_target_enabled: Optional[bool],
    default_message_target_enabled: bool,
    message_target_patterns: Optional[Any],
    default_message_target_patterns: Set[str],
    resolve_message_target_patterns_override_fn: Any,
    message_target_min_support: Optional[int],
    default_message_target_min_support: int,
    message_target_max_per_log: Optional[int],
    default_message_target_max_per_log: int,
    resolve_inference_mode_override_fn: Any,
) -> Dict[str, Any]:
    """Resolve runtime options for log inference logic."""
    effective_inference_mode = resolve_inference_mode_override_fn(
        inference_mode,
        default_inference_mode,
    )
    effective_message_target_enabled = (
        default_message_target_enabled if message_target_enabled is None else bool(message_target_enabled)
    )
    effective_patterns = (
        resolve_message_target_patterns_override_fn(message_target_patterns)
        or set(default_message_target_patterns)
    )

    effective_min_support = int(default_message_target_min_support)
    if message_target_min_support is not None:
        try:
            effective_min_support = max(1, min(20, int(message_target_min_support)))
        except (TypeError, ValueError):
            effective_min_support = int(default_message_target_min_support)

    effective_max_per_log = int(default_message_target_max_per_log)
    if message_target_max_per_log is not None:
        try:
            effective_max_per_log = max(1, min(12, int(message_target_max_per_log)))
        except (TypeError, ValueError):
            effective_max_per_log = int(default_message_target_max_per_log)

    method_name_base = (
        "request_id_then_trace_id_then_message_target_then_time_window"
        if effective_message_target_enabled
        else "request_id_then_trace_id_then_time_window"
    )
    method_name = (
        f"{method_name_base}_hybrid_score"
        if effective_inference_mode == "hybrid_score"
        else method_name_base
    )

    return {
        "effective_inference_mode": effective_inference_mode,
        "effective_message_target_enabled": effective_message_target_enabled,
        "effective_patterns": effective_patterns,
        "effective_min_support": effective_min_support,
        "effective_max_per_log": effective_max_per_log,
        "method_name": method_name,
    }


def build_inference_empty_stats(
    *,
    method_name: str,
    message_target_enabled: bool,
    inference_mode: str,
    message_target_patterns: Set[str],
    message_target_min_support: int,
    message_target_max_per_log: int,
) -> Dict[str, Any]:
    """Build empty inference stats payload with stable contract fields."""
    return {
        "total_candidates": 0,
        "request_id_groups": 0,
        "request_id_edges": 0,
        "trace_id_groups": 0,
        "trace_id_edges": 0,
        "message_target_edges": 0,
        "time_window_edges": 0,
        "method": method_name,
        "message_target_enabled": bool(message_target_enabled),
        "inference_mode": str(inference_mode or ""),
        "message_target_patterns": sorted(message_target_patterns),
        "message_target_min_support": int(message_target_min_support),
        "message_target_max_per_log": int(message_target_max_per_log),
    }
