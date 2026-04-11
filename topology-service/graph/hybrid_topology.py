"""
混合数据源拓扑生成器

结合 logs、traces、metrics 三个数据源生成更准确的服务拓扑图
- Traces: 精确的调用关系（高置信度）
- Logs: 服务节点和启发式关系（中等置信度）
- Metrics: 验证和补充调用关系（辅助置信度）

Date: 2026-02-09
"""

import logging
import os
import json
import re
import math
from typing import Dict, List, Any, Set, Tuple, Optional
from collections import defaultdict, Counter
from datetime import datetime, timedelta, timezone

from graph.confidence_calculator import get_confidence_calculator
from graph.inference_scorer import InferenceScorer
from graph.topology_contract import (
    apply_edge_contract,
    apply_node_contract,
    infer_protocol,
    normalize_endpoint_pattern,
)
from graph import hybrid_topology_utils as hybrid_utils

logger = logging.getLogger(__name__)


class HybridTopologyBuilder:
    """
    混合数据源拓扑构建器

    核心思想：
    1. 从 traces 表获取精确的调用关系（权重 1.0）
    2. 从 logs 表获取服务节点和启发式关系（权重 0.3）
    3. 从 metrics 表验证和补充关系（权重 0.2）
    4. 合并并计算置信度
    """

    def __init__(self, storage_adapter):
        """
        初始化混合拓扑构建器

        Args:
            storage_adapter: StorageAdapter 实例
        """
        self.storage = storage_adapter

        # 置信度权重
        self.WEIGHT_TRACES = 1.0
        self.WEIGHT_LOGS = 0.3
        self.WEIGHT_METRICS = 0.2

        # 时间窗口（默认 1 小时）
        self.time_window = "1 HOUR"

        # 推断边降噪参数
        self.MAX_INFER_SAMPLE = self._parse_env_int(
            "TOPOLOGY_MAX_INFER_SAMPLE",
            12000,
            minimum=1000,
            maximum=60000,
        )
        self.MAX_INFER_SAMPLE_SHORT_WINDOW = self._parse_env_int(
            "TOPOLOGY_MAX_INFER_SAMPLE_SHORT_WINDOW",
            5000,
            minimum=1000,
            maximum=50000,
        )
        self.MAX_INFER_SAMPLE_MEDIUM_WINDOW = self._parse_env_int(
            "TOPOLOGY_MAX_INFER_SAMPLE_MEDIUM_WINDOW",
            8000,
            minimum=1000,
            maximum=60000,
        )
        self.TRACES_SCAN_LIMIT = self._parse_env_int(
            "HYBRID_TOPOLOGY_TRACES_SCAN_LIMIT",
            200000,
            minimum=1000,
            maximum=2_000_000,
        )
        self.LOGS_SCAN_LIMIT = self._parse_env_int(
            "HYBRID_TOPOLOGY_LOGS_SCAN_LIMIT",
            300000,
            minimum=1000,
            maximum=2_000_000,
        )
        self.METRICS_SCAN_LIMIT = self._parse_env_int(
            "HYBRID_TOPOLOGY_METRICS_SCAN_LIMIT",
            100000,
            minimum=1000,
            maximum=2_000_000,
        )
        self.MAX_TIME_WINDOW_DELTA_SEC = 0.8
        self.MAX_TIME_WINDOW_CANDIDATES_PER_LOG = 5
        self.MIN_SUPPORT_REQUEST_ID = 1
        self.MIN_SUPPORT_TRACE_ID = 2
        self.MIN_FALSE_POSITIVE_SAMPLE = self._parse_env_int(
            "TOPOLOGY_MIN_FALSE_POSITIVE_SAMPLE",
            5,
            minimum=1,
            maximum=5000,
        )
        self.MIN_SUPPORT_MESSAGE_TARGET = self._parse_env_int(
            "TOPOLOGY_MIN_SUPPORT_MESSAGE_TARGET",
            2,
            minimum=1,
            maximum=20,
        )
        self.MIN_SUPPORT_TIME_WINDOW = 4
        self.MAX_MESSAGE_TARGETS_PER_LOG = self._parse_env_int(
            "TOPOLOGY_MAX_MESSAGE_TARGETS_PER_LOG",
            3,
            minimum=1,
            maximum=12,
        )
        self.MAX_TIME_WINDOW_FALLBACK_RECORDS = self._parse_env_int(
            "TOPOLOGY_MAX_TIME_WINDOW_FALLBACK_RECORDS",
            2500,
            minimum=200,
            maximum=20000,
        )
        self.ENABLE_TRACES_FAST_PATH = str(
            os.getenv("TOPOLOGY_ENABLE_TRACES_FAST_PATH", "false")
        ).strip().lower() in {"1", "true", "yes", "on"}
        self.MESSAGE_TARGET_ENABLED = self._parse_env_bool(
            "TOPOLOGY_MESSAGE_TARGET_ENABLED",
            True,
        )
        self.MESSAGE_TARGET_PATTERNS = self._parse_message_target_patterns(
            os.getenv("TOPOLOGY_MESSAGE_TARGET_PATTERNS", "url,kv,proxy,rpc")
        )
        self.INFERENCE_MODE = self._parse_inference_mode(
            os.getenv("TOPOLOGY_INFERENCE_MODE", "rule")
        )
        self.MESSAGE_TARGET_EXCLUDE_HOSTS = {
            token.strip().lower()
            for token in os.getenv(
                "TOPOLOGY_MESSAGE_TARGET_EXCLUDE_HOSTS",
                "localhost,127.0.0.1,::1,0.0.0.0",
            ).split(",")
            if token.strip()
        }
        self.INFRA_SERVICE_KEYWORDS = (
            "otel", "collector", "fluent", "agent", "prometheus",
            "grafana", "jaeger", "loki", "zipkin", "kube-proxy",
            "kubelet", "apiserver",
        )
        self.inference_scorer = InferenceScorer()
        self._metrics_namespace_column_exists_cache: Optional[bool] = None
        self._traces_namespace_column_exists_cache: Optional[bool] = None

    @staticmethod
    def _parse_env_bool(name: str, default: bool) -> bool:
        return hybrid_utils.parse_env_bool(name, default)

    @staticmethod
    def _parse_env_int(name: str, default: int, minimum: int = None, maximum: int = None) -> int:
        return hybrid_utils.parse_env_int(name, default, minimum=minimum, maximum=maximum)

    @staticmethod
    def _sanitize_interval(time_window: str, default_value: str = "1 HOUR") -> str:
        """规范化 INTERVAL 参数，避免 SQL 注入。"""
        return hybrid_utils.sanitize_interval(time_window, default_value=default_value)

    @staticmethod
    def _interval_to_minutes(interval_text: str, default_minutes: int = 60) -> int:
        """Convert sanitized interval text like '1 HOUR' to minutes."""
        try:
            amount_text, unit_text = str(interval_text or "").strip().split(maxsplit=1)
            amount = max(int(amount_text), 1)
        except Exception:
            return default_minutes

        unit = unit_text.strip().upper()
        if unit == "MINUTE":
            return amount
        if unit == "HOUR":
            return amount * 60
        if unit == "DAY":
            return amount * 24 * 60
        if unit == "WEEK":
            return amount * 7 * 24 * 60
        return default_minutes

    @staticmethod
    def _escape_sql_literal(value: str) -> str:
        """转义 SQL 字符串字面量中的单引号。"""
        return hybrid_utils.escape_sql_literal(value)

    @staticmethod
    def _parse_message_target_patterns(value: str) -> Set[str]:
        return hybrid_utils.parse_message_target_patterns(value)

    @staticmethod
    def _parse_inference_mode(value: Any) -> str:
        return hybrid_utils.parse_inference_mode(value)

    @staticmethod
    def _resolve_inference_mode_override(value: Any, default: str = "rule") -> str:
        return hybrid_utils.resolve_inference_mode_override(value, default=default)

    @staticmethod
    def _resolve_message_target_patterns_override(value: Any) -> Optional[Set[str]]:
        return hybrid_utils.resolve_message_target_patterns_override(value)

    def _has_metrics_namespace_column(self) -> bool:
        """检测 logs.metrics 是否存在 metrics_namespace 列，结果做实例级缓存。"""
        cached = self._metrics_namespace_column_exists_cache
        if cached is not None:
            return bool(cached)
        try:
            rows = self.storage.execute_query(
                """
                SELECT count() AS cnt
                FROM system.columns
                WHERE database = 'logs'
                  AND table = 'metrics'
                  AND name = 'metrics_namespace'
                """
            )
            cnt = 0
            if rows:
                first = rows[0]
                if isinstance(first, dict):
                    cnt = int(first.get("cnt") or 0)
                elif isinstance(first, (list, tuple)):
                    cnt = int(first[0] or 0)
            exists = cnt > 0
        except Exception:
            exists = False
        self._metrics_namespace_column_exists_cache = exists
        return exists

    def _has_traces_namespace_column(self) -> bool:
        """检测 logs.traces 是否存在 traces_namespace 列，结果做实例级缓存。"""
        cached = self._traces_namespace_column_exists_cache
        if cached is not None:
            return bool(cached)
        try:
            rows = self.storage.execute_query(
                """
                SELECT count() AS cnt
                FROM system.columns
                WHERE database = 'logs'
                  AND table = 'traces'
                  AND name = 'traces_namespace'
                """
            )
            cnt = 0
            if rows:
                first = rows[0]
                if isinstance(first, dict):
                    cnt = int(first.get("cnt") or 0)
                elif isinstance(first, (list, tuple)):
                    cnt = int(first[0] or 0)
            exists = cnt > 0
        except Exception:
            exists = False
        self._traces_namespace_column_exists_cache = exists
        return exists

    @staticmethod
    def _to_float(value: Any, default: float = 0.0) -> float:
        return hybrid_utils.to_float(value, default=default)

    @staticmethod
    def _parse_service_alias_map(value: str) -> Dict[str, str]:
        """解析服务别名映射配置。"""
        return hybrid_utils.parse_service_alias_map(value)

    def _build_known_service_aliases(self, known_services: Dict[str, str]) -> Dict[str, str]:
        """
        构建服务别名索引，提升 host=svc / upstream=svc 的匹配准确性。
        """
        alias_index = dict(known_services)
        configured = self._parse_service_alias_map(os.getenv("TOPOLOGY_SERVICE_ALIAS_MAP", ""))

        # 先应用显式配置映射（alias -> canonical）。
        for alias, canonical in configured.items():
            resolved = known_services.get(canonical) or known_services.get(canonical.replace("_", "-"))
            if resolved:
                alias_index[alias] = resolved

        # 再自动补充常见短名别名。
        ignore_aliases = {"svc", "service", "app", "prod", "dev", "test"}
        for key, canonical_name in known_services.items():
            normalized = str(key or "").strip().lower()
            if not normalized:
                continue

            candidates = {normalized}
            if normalized.endswith("-service") and len(normalized) > len("-service") + 2:
                candidates.add(normalized[:-8])
            tokens = [token for token in re.split(r"[-_.]", normalized) if token]
            if tokens:
                first = tokens[0]
                if len(first) >= 3:
                    candidates.add(first)

            for alias in candidates:
                clean = alias.strip("-_.")
                if not clean or clean in ignore_aliases or len(clean) < 3:
                    continue
                alias_index.setdefault(clean, canonical_name)

        return alias_index

    @staticmethod
    def _normalize_namespace_token(value: Any) -> str:
        token = str(value or "").strip()
        if not token:
            return ""
        if token.lower() in {"unknown", "none", "null", "-", "n/a"}:
            return ""
        return token

    @classmethod
    def _normalize_service_name_token(cls, value: Any) -> str:
        token = str(value or "").strip()
        if not token:
            return ""
        if token.lower() in {"unknown", "none", "null", "-", "n/a"}:
            return ""
        return token

    @classmethod
    def _decode_service_identity(cls, raw_service_name: Any) -> Tuple[str, str]:
        raw = str(raw_service_name or "").strip()
        if not raw:
            return "", ""

        namespace_hint = ""
        service_token = raw
        if "::" in service_token:
            prefix, suffix = service_token.split("::", 1)
            normalized_prefix = cls._normalize_namespace_token(prefix)
            if normalized_prefix:
                namespace_hint = normalized_prefix
            service_token = suffix or service_token

        service_parts = [part.strip() for part in service_token.split(":")]
        if len(service_parts) == 3:
            namespace_part, service_part, env_part = service_parts
            env_lower = env_part.lower()
            if env_lower in {"prod", "staging", "test", "dev", "qa", "uat", "sit", "online", "release"}:
                if not namespace_hint:
                    namespace_hint = cls._normalize_namespace_token(namespace_part)
                service_token = service_part

        normalized_service = cls._normalize_service_name_token(service_token)
        return normalized_service, namespace_hint

    def _build_node_id(
        self,
        service_name: str,
        namespace_value: Any,
        namespace_filter: Optional[str] = None,
    ) -> str:
        """
        生成节点 ID：
        - 指定 namespace 过滤时：返回 service_name（便于测试与兼容旧调用）
        - 未指定 namespace 且存在有效 namespace：使用 namespace::service，避免跨命名空间冲突
        """
        service = str(service_name or "").strip() or "unknown"
        if str(namespace_filter or "").strip():
            return service

        normalized_namespace = self._normalize_namespace_token(namespace_value)
        if not normalized_namespace:
            return service
        if normalized_namespace.lower() == service.lower():
            return service
        return hybrid_utils.build_service_node_id(service, normalized_namespace)

    @staticmethod
    def _is_likely_outbound_message(text: str) -> bool:
        return hybrid_utils.is_likely_outbound_message(text)

    @staticmethod
    def _is_likely_inbound_message(text: str) -> bool:
        return hybrid_utils.is_likely_inbound_message(text)

    def _estimate_dynamic_support(
        self,
        base_support: int,
        source_volume: int,
        method: str,
        inference_mode: str,
    ) -> int:
        """
        动态支持数阈值（P0）：高流量服务提高阈值，抑制噪声边。
        """
        if inference_mode != "hybrid_score":
            return max(1, int(base_support))

        base = max(1, int(base_support))
        volume = max(0, int(source_volume or 0))
        if volume <= 0:
            return base

        if method == "message_target":
            adaptive = 1 + int(min(6, math.log10(volume + 1) * 1.8))
            return max(base, adaptive)
        if method == "time_window":
            adaptive = 2 + int(min(6, math.log10(volume + 1) * 2.1))
            return max(base, adaptive)
        return base

    @staticmethod
    def _percentile(values: List[float], percentile: float) -> float:
        """计算分位值，空数组返回 0。"""
        return hybrid_utils.percentile(values, percentile)

    @staticmethod
    def _time_window_seconds(time_window: str) -> int:
        """将 '1 HOUR' 这类窗口转换为秒数。"""
        return hybrid_utils.time_window_seconds(time_window)

    @staticmethod
    def _timestamp_to_datetime(value: Any) -> datetime:
        return hybrid_utils.timestamp_to_datetime(value)

    def _is_infrastructure_service(self, service_name: str) -> bool:
        service = str(service_name or "").strip().lower()
        if not service:
            return False
        return any(keyword in service for keyword in self.INFRA_SERVICE_KEYWORDS)

    @staticmethod
    def _dedup_service_sequence(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return hybrid_utils.dedup_service_sequence(records)

    def _match_service_from_host(self, host: str, known_services: Dict[str, str]) -> str:
        return hybrid_utils.match_service_from_host(
            host,
            known_services,
            exclude_hosts=self.MESSAGE_TARGET_EXCLUDE_HOSTS,
        )

    @staticmethod
    def _extract_host_candidates_from_token(raw_value: str) -> List[str]:
        return hybrid_utils.extract_host_candidates_from_token(raw_value)

    def _extract_message_target_services(
        self,
        message: str,
        known_services: Dict[str, str],
        enabled: Optional[bool] = None,
        patterns: Optional[Set[str]] = None,
        max_targets_per_log: Optional[int] = None,
    ) -> List[Tuple[str, str]]:
        """
        从日志消息中提取目标服务，支持多种协议/日志模式。
        """
        effective_enabled = self.MESSAGE_TARGET_ENABLED if enabled is None else bool(enabled)
        effective_patterns = patterns or set(self.MESSAGE_TARGET_PATTERNS)
        effective_max_targets = max(1, int(max_targets_per_log or self.MAX_MESSAGE_TARGETS_PER_LOG))
        return hybrid_utils.extract_message_target_services(
            message,
            known_services,
            enabled=effective_enabled,
            patterns=effective_patterns,
            max_targets_per_log=effective_max_targets,
            exclude_hosts=self.MESSAGE_TARGET_EXCLUDE_HOSTS,
        )

    def build_topology(
        self,
        time_window: str = "1 HOUR",
        namespace: str = None,
        confidence_threshold: float = 0.3,
        inference_mode: Optional[str] = None,
        message_target_enabled: Optional[bool] = None,
        message_target_patterns: Optional[Any] = None,
        message_target_min_support: Optional[int] = None,
        message_target_max_per_log: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        构建混合数据源的服务拓扑图

        ⚠️ 支持自适应时间窗口：如果指定窗口内无数据，自动扩大到 24 小时

        Args:
            time_window: 时间窗口（如 "1 HOUR", "15 MINUTE", "1 DAY"）
            namespace: 命名空间过滤
            confidence_threshold: 置信度阈值，低于此值的边将被过滤

        Returns:
            Dict[str, Any]: 拓扑图数据
            {
                "nodes": [...],
                "edges": [...],
                "metadata": {
                    "data_sources": ["traces", "logs", "metrics"],
                    "time_window": "1 HOUR",
                    "node_count": 10,
                    "edge_count": 15,
                    "avg_confidence": 0.75
                }
            }
        """
        try:
            safe_time_window = self._sanitize_interval(time_window, default_value="1 HOUR")
            logger.info(f"Building hybrid topology with time_window={safe_time_window}")

            # 1. 从三个数据源收集数据
            try:
                traces_data = self._get_traces_topology(safe_time_window, namespace)
            except Exception:
                logger.exception("Error in _get_traces_topology")
                traces_data = {"nodes": [], "edges": []}

            try:
                logs_data = self._get_logs_topology(
                    time_window=safe_time_window,
                    namespace=namespace,
                    inference_mode=inference_mode,
                    message_target_enabled=message_target_enabled,
                    message_target_patterns=message_target_patterns,
                    message_target_min_support=message_target_min_support,
                    message_target_max_per_log=message_target_max_per_log,
                )
            except Exception:
                logger.exception("Error in _get_logs_topology")
                logs_data = {"nodes": [], "edges": []}

            try:
                metrics_data = self._get_metrics_topology(safe_time_window, namespace)
            except Exception:
                logger.exception("Error in _get_metrics_topology")
                metrics_data = {"nodes": [], "edges": []}

            # ⚠️ 自适应时间窗口：如果所有数据源都为空，扩大到 24 小时
            total_nodes = (
                len(traces_data.get("nodes", [])) +
                len(logs_data.get("nodes", [])) +
                len(metrics_data.get("nodes", []))
            )

            if total_nodes == 0 and safe_time_window != "24 HOUR":
                logger.warning(f"No data found in {safe_time_window}, expanding to 24 HOUR")
                safe_time_window = "24 HOUR"
                try:
                    traces_data = self._get_traces_topology(safe_time_window, namespace)
                except Exception as e:
                    logger.error(f"Error in _get_traces_topology (24H): {e}")
                    traces_data = {"nodes": [], "edges": []}

                try:
                    logs_data = self._get_logs_topology(
                        time_window=safe_time_window,
                        namespace=namespace,
                        inference_mode=inference_mode,
                        message_target_enabled=message_target_enabled,
                        message_target_patterns=message_target_patterns,
                        message_target_min_support=message_target_min_support,
                        message_target_max_per_log=message_target_max_per_log,
                    )
                except Exception as e:
                    logger.error(f"Error in _get_logs_topology (24H): {e}")
                    logs_data = {"nodes": [], "edges": []}

                try:
                    metrics_data = self._get_metrics_topology(safe_time_window, namespace)
                except Exception as e:
                    logger.error(f"Error in _get_metrics_topology (24H): {e}")
                    metrics_data = {"nodes": [], "edges": []}

            # 2. 合并节点
            logger.debug(f"Merging nodes: traces={len(traces_data.get('nodes', []))}, logs={len(logs_data.get('nodes', []))}, metrics={len(metrics_data.get('nodes', []))}")
            try:
                merged_nodes = self._merge_nodes(
                    traces_data.get("nodes", []),
                    logs_data.get("nodes", []),
                    metrics_data.get("nodes", [])
                )
            except Exception as e:
                logger.error(f"Error in _merge_nodes: {e}")
                import traceback
                logger.error(traceback.format_exc())
                merged_nodes = []
            logger.debug(f"Merged nodes count: {len(merged_nodes)}")

            # 3. 合并边并计算置信度
            logger.debug(f"Merging edges: traces={len(traces_data.get('edges', []))}, logs={len(logs_data.get('edges', []))}, metrics={len(metrics_data.get('edges', []))}")
            try:
                merged_edges = self._merge_edges(
                    traces_data.get("edges", []),
                    logs_data.get("edges", []),
                    metrics_data.get("edges", [])
                )
            except Exception as e:
                logger.error(f"Error in _merge_edges: {e}")
                import traceback
                logger.error(traceback.format_exc())
                merged_edges = []
            logger.debug(f"Merged edges count: {len(merged_edges)}")

            # 4. 使用改进的置信度计算器重新计算
            logger.debug(f"Starting confidence calculation for {len(merged_nodes)} nodes, {len(merged_edges)} edges")
            calculator = get_confidence_calculator()
            logger.debug(f"Calculator reference_time: {calculator.reference_time}, tzinfo: {calculator.reference_time.tzinfo}")
            
            recalculated_topology = calculator.recalculate_topology_confidence({
                "nodes": merged_nodes,
                "edges": merged_edges,
                "metadata": {}
            })
            logger.debug("Confidence calculation completed")

            merged_nodes = recalculated_topology["nodes"]
            merged_edges = recalculated_topology["edges"]

            # 4.1 接入 edge RED 聚合视图（M1-04）
            self._apply_edge_red_aggregation(
                merged_edges=merged_edges,
                time_window=safe_time_window,
                namespace=namespace,
            )

            # 4.2 统一契约转换（M1-01/M1-02）
            merged_nodes, merged_edges = self._apply_contract_schema(
                nodes=merged_nodes,
                edges=merged_edges
            )

            # 5. 过滤低置信度的边
            filtered_edges = [
                edge for edge in merged_edges
                if edge.get("metrics", {}).get("confidence", 0) >= confidence_threshold
            ]

            # 6. 计算统计信息
            avg_confidence = (
                sum(e.get("metrics", {}).get("confidence", 0) for e in filtered_edges) / len(filtered_edges)
                if filtered_edges else 0
            )

            observed_pairs = {
                (edge.get("source"), edge.get("target"))
                for edge in traces_data.get("edges", [])
                if str(edge.get("source") or "").strip().lower() not in {"", "unknown"}
                and str(edge.get("target") or "").strip().lower() not in {"", "unknown"}
            }
            inferred_edges = [
                edge for edge in filtered_edges
                if edge.get("metrics", {}).get("data_source") == "inferred"
                or edge.get("evidence_type") == "inferred"
            ]
            inferred_pairs = {
                (edge.get("source"), edge.get("target"))
                for edge in inferred_edges
                if str(edge.get("source") or "").strip().lower() not in {"", "unknown"}
                and str(edge.get("target") or "").strip().lower() not in {"", "unknown"}
            }
            false_positive_edges = 0
            direction_mismatch_edges = 0
            has_observed_baseline = bool(observed_pairs)
            if has_observed_baseline:
                observed_undirected_pairs = {tuple(sorted(pair)) for pair in observed_pairs}
                for pair in inferred_pairs:
                    if pair in observed_pairs:
                        continue
                    if tuple(sorted(pair)) in observed_undirected_pairs:
                        direction_mismatch_edges += 1
                        continue
                    false_positive_edges += 1

            inferred_ratio = (len(inferred_edges) / len(filtered_edges)) if filtered_edges else 0.0
            false_positive_rate_state = "ok"
            false_positive_rate_reason = "comparable"
            if inferred_pairs:
                if has_observed_baseline:
                    if len(inferred_pairs) < int(self.MIN_FALSE_POSITIVE_SAMPLE):
                        false_positive_rate = 0.0
                        false_positive_rate_state = "unknown"
                        false_positive_rate_reason = "insufficient_inferred_sample"
                    else:
                        false_positive_rate = false_positive_edges / len(inferred_pairs)
                else:
                    false_positive_rate = 0.0
                    false_positive_rate_state = "unknown"
                    false_positive_rate_reason = "no_observed_baseline"
            else:
                false_positive_rate = 0.0
            avg_coverage = (
                sum(self._to_float(edge.get("coverage") or edge.get("metrics", {}).get("coverage"), 0.0)
                    for edge in filtered_edges) / len(filtered_edges)
                if filtered_edges else 0.0
            )

            inference_stats = (logs_data.get("metadata") or {}).get("inference_stats", {})
            inferred_services = set()
            for edge in inferred_edges:
                inferred_services.add(edge.get("source"))
                inferred_services.add(edge.get("target"))

            metadata = {
                "data_sources": self._get_data_sources(traces_data, logs_data, metrics_data),
                "time_window": safe_time_window,
                "namespace": namespace,
                "node_count": len(merged_nodes),
                "edge_count": len(filtered_edges),
                "avg_confidence": round(avg_confidence, 2),
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "contract_version": "topology-schema-v1",
                "quality_version": "quality-score-v1",
                "inference_quality": {
                    "coverage": round(avg_coverage, 3),
                    "inferred_ratio": round(inferred_ratio, 3),
                    "false_positive_rate": round(false_positive_rate, 3),
                    "false_positive_rate_state": false_positive_rate_state,
                    "false_positive_rate_reason": false_positive_rate_reason,
                    "false_positive_rate_min_sample": int(self.MIN_FALSE_POSITIVE_SAMPLE),
                    "has_observed_baseline": has_observed_baseline,
                    "direction_mismatch_edges": direction_mismatch_edges,
                    "inferred_edge_count": len(inferred_edges),
                    "inferred_service_count": len(inferred_services),
                    "request_id_groups": inference_stats.get("request_id_groups", 0),
                    "request_id_edges": inference_stats.get("request_id_edges", 0),
                    "trace_id_groups": inference_stats.get("trace_id_groups", 0),
                    "trace_id_edges": inference_stats.get("trace_id_edges", 0),
                    "message_target_edges": inference_stats.get("message_target_edges", 0),
                    "time_window_edges": inference_stats.get("time_window_edges", 0),
                    "dropped_bidirectional_edges": inference_stats.get("dropped_bidirectional_edges", 0),
                    "filtered_edges": inference_stats.get("filtered_edges", 0),
                    "message_target_enabled": inference_stats.get("message_target_enabled", self.MESSAGE_TARGET_ENABLED),
                    "inference_mode": inference_stats.get("inference_mode", self.INFERENCE_MODE),
                    "message_target_patterns": inference_stats.get("message_target_patterns", sorted(self.MESSAGE_TARGET_PATTERNS)),
                    "message_target_min_support": inference_stats.get("message_target_min_support", self.MIN_SUPPORT_MESSAGE_TARGET),
                    "message_target_max_per_log": inference_stats.get("message_target_max_per_log", self.MAX_MESSAGE_TARGETS_PER_LOG),
                    "evidence_sparse": bool(inference_stats.get("evidence_sparse", False)),
                    "avg_evidence_sufficiency_score": round(
                        self._to_float(inference_stats.get("avg_evidence_sufficiency_score"), 0.0),
                        2
                    ),
                },
                "source_breakdown": {
                    "traces": {
                        "nodes": len(traces_data.get("nodes", [])),
                        "edges": len(traces_data.get("edges", []))
                    },
                    "logs": {
                        "nodes": len(logs_data.get("nodes", [])),
                        "edges": len(logs_data.get("edges", []))
                    },
                    "metrics": {
                        "nodes": len(metrics_data.get("nodes", [])),
                        "edges": len(metrics_data.get("edges", []))
                    }
                }
            }

            logger.info(
                f"Built topology: {len(merged_nodes)} nodes, "
                f"{len(filtered_edges)} edges (avg confidence: {avg_confidence:.2f})"
            )

            return {
                "nodes": merged_nodes,
                "edges": filtered_edges,
                "metadata": metadata
            }

        except Exception as e:
            logger.exception("Error building hybrid topology")
            return {
                "nodes": [],
                "edges": [],
                "metadata": {"error": str(e)}
            }

    def _get_traces_topology(
        self,
        time_window: str,
        namespace: str = None
    ) -> Dict[str, Any]:
        """
        从 traces 表获取精确的调用关系

        Returns:
            {
                "nodes": [{id, label, type, metrics}],
                "edges": [{source, target, type, metrics}]
            }
        """
        try:
            safe_time_window = self._sanitize_interval(time_window, default_value="1 HOUR")
            if not self.storage.ch_client:
                return {"nodes": [], "edges": []}
            safe_namespace = self._escape_sql_literal(namespace) if namespace else None

            has_traces_namespace_column = self._has_traces_namespace_column()
            namespace_expr = (
                "traces_namespace"
                if has_traces_namespace_column
                else (
                    "multiIf("
                    "length(JSONExtractString(attributes_json, 'k8s.namespace.name')) > 0, "
                    "JSONExtractString(attributes_json, 'k8s.namespace.name'), "
                    "length(JSONExtractString(attributes_json, 'service_namespace')) > 0, "
                    "JSONExtractString(attributes_json, 'service_namespace'), "
                    "JSONExtractString(attributes_json, 'namespace')"
                    ")"
                )
            )
            # 无 traces_namespace 列且未指定 namespace 时，避免在主扫描 SQL 中做高开销 JSONExtract。
            span_namespace_select_expr = "traces_namespace" if has_traces_namespace_column else "''"

            # traces 表使用 timestamp 列做 PREWHERE 下推
            prewhere_conditions = [
                f"timestamp > now() - INTERVAL {safe_time_window}",
                "notEmpty(trace_id)",
                "notEmpty(span_id)",
                "notEmpty(service_name)",
            ]
            where_clause = ""
            if safe_namespace:
                if has_traces_namespace_column:
                    prewhere_conditions.append(f"traces_namespace = '{safe_namespace}'")
                else:
                    where_clause = f"WHERE {namespace_expr} = '{safe_namespace}'"
                span_namespace_select_expr = f"'{safe_namespace}'"
            prewhere_clause = "PREWHERE " + " AND ".join(prewhere_conditions)
            window_minutes = self._interval_to_minutes(safe_time_window, default_minutes=60)
            trace_scan_limit = int(self.TRACES_SCAN_LIMIT)
            if window_minutes <= 60:
                trace_scan_limit = min(trace_scan_limit, 80_000)
            elif window_minutes <= 24 * 60:
                trace_scan_limit = min(trace_scan_limit, 160_000)

            if self.ENABLE_TRACES_FAST_PATH:
                # 快速路径（可选）：若窗口内不存在 parent->child span 关系，则跳过重型明细扫描。
                has_parent_relation: Optional[bool] = None
                try:
                    relation_prewhere = "PREWHERE " + " AND ".join(prewhere_conditions + ["notEmpty(parent_span_id)"])
                    relation_query = f"""
                    SELECT parent_span_id
                    FROM logs.traces
                    {relation_prewhere}
                    {where_clause}
                    LIMIT 1
                    SETTINGS optimize_use_projections = 1
                    """
                    relation_rows = self.storage.execute_query(relation_query)
                    has_parent_relation = bool(relation_rows)
                except Exception:
                    has_parent_relation = None

                if has_parent_relation is False:
                    node_agg_query = f"""
                    SELECT
                        service_name,
                        topK(1)({namespace_expr}) AS namespace_top,
                        count() AS span_count,
                        avg(toFloat64OrZero(toString(duration_ms))) AS avg_duration,
                        countIf(lower(toString(status)) IN ('error', 'failed', 'status_code_error', '2')) AS error_count,
                        countIf(empty(parent_span_id)) AS trace_count
                    FROM logs.traces
                    {prewhere_clause}
                    {where_clause}
                    GROUP BY service_name
                    ORDER BY span_count DESC
                    LIMIT 2000
                    SETTINGS optimize_use_projections = 1
                    """
                    node_rows = self.storage.execute_query(node_agg_query)
                    nodes = []
                    for row in node_rows:
                        service_name = str(row.get("service_name") or "").strip()
                        if not service_name:
                            continue
                        namespace_top = row.get("namespace_top")
                        if isinstance(namespace_top, (list, tuple)) and namespace_top:
                            candidate_namespace = str(namespace_top[0] or "").strip()
                        else:
                            candidate_namespace = str(row.get("service_namespace") or "").strip()
                        if safe_namespace:
                            candidate_namespace = safe_namespace
                        if candidate_namespace.lower() in {"unknown", "none", "null", "-", "n/a"}:
                            candidate_namespace = ""
                        if candidate_namespace.lower() == service_name.lower():
                            candidate_namespace = ""
                        node_id = self._build_node_id(
                            service_name=service_name,
                            namespace_value=candidate_namespace,
                            namespace_filter=safe_namespace,
                        )
                        nodes.append({
                            "id": node_id,
                            "label": service_name,
                            "type": "service",
                            "name": service_name,
                            "metrics": {
                                "trace_count": int(row.get("trace_count") or 0),
                                "span_count": int(row.get("span_count") or 0),
                                "avg_duration": round(float(row.get("avg_duration") or 0.0), 2),
                                "error_count": int(row.get("error_count") or 0),
                                "namespace": candidate_namespace,
                                "service_namespace": candidate_namespace,
                                "namespace_count": 1 if candidate_namespace else 0,
                                "namespace_ambiguous": False,
                                "data_source": "traces",
                                "confidence": 1.0,
                            }
                        })
                    return {"nodes": nodes, "edges": []}

            # 查询 span 关系（通过 trace_id 和 parent_span_id）
            query = f"""
            SELECT
                trace_id,
                span_id,
                parent_span_id,
                service_name,
                operation_name,
                status,
                timestamp,
                {span_namespace_select_expr} AS span_namespace,
                toFloat64OrZero(toString(duration_ms)) AS duration_ms_norm
            FROM logs.traces
            {prewhere_clause}
            {where_clause}
            ORDER BY timestamp DESC
            LIMIT {trace_scan_limit}
            SETTINGS optimize_use_projections = 1, optimize_read_in_order = 1
            """

            result = self.storage.execute_query(query)

            def _sanitize_namespace(namespace_value: Any, service_name_value: str) -> str:
                normalized = str(namespace_value or "").strip()
                if not normalized:
                    return ""
                lowered = normalized.lower()
                if lowered in {"unknown", "none", "null", "-", "n/a"}:
                    return ""
                service_token = str(service_name_value or "").strip().lower()
                # 防止 attributes.namespace 写入 service_name 造成命名空间误判。
                if service_token and lowered == service_token:
                    return ""
                return normalized

            def _build_trace_node_id(service_name_value: str, namespace_value: str) -> str:
                return self._build_node_id(
                    service_name=service_name_value,
                    namespace_value=namespace_value,
                    namespace_filter=safe_namespace,
                )

            def _ensure_trace_node(
                registry: Dict[str, Dict[str, Any]],
                service_name_value: str,
                namespace_value: str,
            ) -> str:
                node_namespace = _sanitize_namespace(namespace_value, service_name_value)
                node_id = _build_trace_node_id(service_name_value, node_namespace)
                if node_id not in registry:
                    registry[node_id] = {
                        "id": node_id,
                        "label": service_name_value,
                        "type": "service",
                        "name": service_name_value,
                        "namespace": node_namespace,
                        "metrics": {
                            "trace_count": 0,
                            "span_count": 0,
                            "avg_duration": 0,
                            "error_count": 0,
                            "namespace": node_namespace,
                            "service_namespace": node_namespace,
                            "namespace_count": 1 if node_namespace else 0,
                            "namespace_ambiguous": False,
                            "data_source": "traces",
                            "confidence": 1.0,
                        }
                    }
                return node_id

            def _extract_numeric(attrs: Dict[str, Any], keys: Tuple[str, ...], default: float = 0.0) -> float:
                for key in keys:
                    value = attrs.get(key)
                    if value is None:
                        continue
                    try:
                        return max(float(value), 0.0)
                    except (TypeError, ValueError):
                        continue
                return default

            def _is_error_status(status_value: str) -> bool:
                normalized = str(status_value or "").strip().lower()
                return normalized in {"error", "failed", "status_code_error", "2"}

            # 构建调用关系图
            nodes = {}  # {service_name: node_data}
            service_namespace_counter: Dict[str, Counter] = defaultdict(Counter)
            edges = defaultdict(lambda: {
                "call_count": 0,
                "total_duration": 0,
                "error_count": 0,
                "durations": [],
                "timeout_count": 0,
                "retries": 0.0,
                "pending": 0.0,
                "dlq": 0.0,
                "operations": Counter(),
                "last_seen": None,
            })

            # 按 trace_id 分组
            traces_by_id = defaultdict(list)
            for row in result:
                trace_id = row.get("trace_id")
                span_id = row.get("span_id")
                parent_span_id = row.get("parent_span_id")
                service_name = row.get("service_name") or "unknown"
                operation_name = row.get("operation_name") or "unknown"
                status = str(row.get("status") or "unset").lower()
                timestamp_value = row.get("timestamp")

                attrs = {}
                span_namespace = _sanitize_namespace(row.get("span_namespace"), service_name)
                duration_ms = self._to_float(row.get("duration_ms_norm"), -1.0)
                retries = 0.0
                pending = 0.0
                dlq = 0.0
                timeout_ms = 1000.0

                # 兼容旧测试桩/旧列，必要时回退到 attributes_json 解析。
                if not span_namespace or duration_ms < 0:
                    attributes_json = row.get("attributes_json")
                    if attributes_json:
                        try:
                            attrs = json.loads(attributes_json)
                        except (TypeError, ValueError):
                            attrs = {}

                    if not span_namespace:
                        span_namespace = _sanitize_namespace(
                            attrs.get("k8s.namespace.name")
                            or attrs.get("service_namespace")
                            or attrs.get("namespace"),
                            service_name,
                        )
                    if duration_ms < 0:
                        duration_ms = _extract_numeric(attrs, ("duration_ms", "duration", "elapsed_ms"))

                if safe_namespace:
                    span_namespace = span_namespace or safe_namespace
                    if span_namespace and span_namespace != safe_namespace:
                        continue

                if span_namespace:
                    service_namespace_counter[service_name][span_namespace] += 1

                duration_ms = max(duration_ms, 0.0)
                retries = max(retries, 0.0)
                pending = max(pending, 0.0)
                dlq = max(dlq, 0.0)
                timeout_ms = max(timeout_ms, 1.0)

                if trace_id and span_id:
                    traces_by_id[trace_id].append({
                        "span_id": span_id,
                        "parent_span_id": parent_span_id,
                        "service_name": service_name,
                        "namespace": span_namespace or "",
                        "operation_name": operation_name,
                        "duration_ms": duration_ms or 0,
                        "status": status,
                        "timestamp": timestamp_value,
                        "retries": retries,
                        "pending": pending,
                        "dlq": dlq,
                        "timeout_ms": timeout_ms,
                    })

            # 分析每个 trace 的调用关系
            for trace_id, spans in traces_by_id.items():
                # 构建 span_id -> span 的映射
                spans_by_id = {span["span_id"]: span for span in spans}

                for span in spans:
                    service_name = span["service_name"]
                    span_namespace = span.get("namespace") or ""
                    node_id = _ensure_trace_node(nodes, service_name, span_namespace)

                    nodes[node_id]["metrics"]["span_count"] += 1
                    nodes[node_id]["metrics"]["avg_duration"] += span["duration_ms"]
                    if _is_error_status(span["status"]):
                        nodes[node_id]["metrics"]["error_count"] += 1

                    # 如果有 parent_span_id，找到父服务
                    parent_span_id = span["parent_span_id"]
                    if parent_span_id and parent_span_id in spans_by_id:
                        parent_span = spans_by_id[parent_span_id]
                        parent_service = parent_span["service_name"]
                        parent_namespace = parent_span.get("namespace") or ""
                        parent_node_id = _ensure_trace_node(nodes, parent_service, parent_namespace)
                        if parent_node_id == node_id:
                            # 服务粒度拓扑不保留 self-loop，避免图噪声。
                            continue

                        # 添加边
                        edge_key = (parent_node_id, node_id)
                        edges[edge_key]["call_count"] += 1
                        edges[edge_key]["total_duration"] += span["duration_ms"]
                        edges[edge_key]["source_service"] = parent_service
                        edges[edge_key]["target_service"] = service_name
                        edges[edge_key]["source_namespace"] = parent_namespace
                        edges[edge_key]["target_namespace"] = span_namespace
                        if _is_error_status(span["status"]):
                            edges[edge_key]["error_count"] += 1
                        edges[edge_key]["durations"].append(span["duration_ms"])
                        if span["duration_ms"] >= span.get("timeout_ms", 1000.0):
                            edges[edge_key]["timeout_count"] += 1
                        edges[edge_key]["retries"] += span.get("retries", 0.0)
                        edges[edge_key]["pending"] += span.get("pending", 0.0)
                        edges[edge_key]["dlq"] += span.get("dlq", 0.0)
                        edges[edge_key]["operations"][span.get("operation_name") or "unknown"] += 1
                        edge_last_seen = edges[edge_key]["last_seen"]
                        span_ts = span.get("timestamp")
                        if isinstance(span_ts, datetime):
                            if edge_last_seen is None or span_ts > edge_last_seen:
                                edges[edge_key]["last_seen"] = span_ts

            # 计算每个 trace 的根节点（无 parent 的 span）
            for trace_id, spans in traces_by_id.items():
                root_spans = [s for s in spans if not s.get("parent_span_id")]
                for root_span in root_spans:
                    service_name = root_span["service_name"]
                    span_namespace = root_span.get("namespace") or ""
                    node_id = _ensure_trace_node(nodes, service_name, span_namespace)
                    nodes[node_id]["metrics"]["trace_count"] += 1

            # 计算平均持续时间
            for node in nodes.values():
                span_count = node["metrics"]["span_count"]
                if span_count > 0:
                    node["metrics"]["avg_duration"] = round(
                        node["metrics"]["avg_duration"] / span_count, 2
                    )

            # 构建边列表
            edge_list = []
            for (source, target), data in edges.items():
                call_count = data["call_count"]
                avg_duration = data["total_duration"] / call_count if call_count > 0 else 0
                error_rate = data["error_count"] / call_count if call_count > 0 else 0
                p95 = self._percentile(data["durations"], 0.95)
                p99 = self._percentile(data["durations"], 0.99)
                timeout_rate = (data["timeout_count"] / call_count) if call_count > 0 else 0.0
                retries_avg = (data["retries"] / call_count) if call_count > 0 else 0.0
                pending_avg = (data["pending"] / call_count) if call_count > 0 else 0.0
                dlq_avg = (data["dlq"] / call_count) if call_count > 0 else 0.0
                operation_name = (
                    data["operations"].most_common(1)[0][0]
                    if data["operations"] else "unknown"
                )
                protocol = infer_protocol(operation_name, "http")
                endpoint_pattern = normalize_endpoint_pattern(operation_name)
                last_seen = data.get("last_seen")
                last_seen_value = last_seen.isoformat() if isinstance(last_seen, datetime) else None
                source_service = str(data.get("source_service") or "").strip()
                target_service = str(data.get("target_service") or "").strip()
                source_namespace = str(data.get("source_namespace") or "").strip()
                target_namespace = str(data.get("target_namespace") or "").strip()

                edge_list.append({
                    "id": f"{source}-{target}",
                    "source": source,
                    "target": target,
                    "source_service": source_service,
                    "target_service": target_service,
                    "source_namespace": source_namespace,
                    "target_namespace": target_namespace,
                    "label": "calls",
                    "type": "calls",
                    "metrics": {
                        "call_count": call_count,
                        "avg_duration": round(avg_duration, 2),
                        "error_count": data["error_count"],
                        "error_rate": round(error_rate, 4),
                        "p95": p95,
                        "p99": p99,
                        "timeout_rate": round(timeout_rate, 4),
                        "retries": round(retries_avg, 3),
                        "pending": round(pending_avg, 3),
                        "dlq": round(dlq_avg, 3),
                        "operation_name": operation_name,
                        "protocol": protocol,
                        "endpoint_pattern": endpoint_pattern,
                        "last_seen": last_seen_value,
                        "source_service": source_service,
                        "target_service": target_service,
                        "source_namespace": source_namespace,
                        "target_namespace": target_namespace,
                        "data_source": "traces",
                        "data_sources": ["traces"],
                        "confidence": 1.0  # traces 数据最可靠
                    }
                })

            return {
                "nodes": list(nodes.values()),
                "edges": edge_list
            }

        except Exception as e:
            logger.error(f"Error getting traces topology: {e}")
            return {"nodes": [], "edges": []}

    def _get_logs_topology(
        self,
        time_window: str,
        namespace: str = None,
        inference_mode: Optional[str] = None,
        message_target_enabled: Optional[bool] = None,
        message_target_patterns: Optional[Any] = None,
        message_target_min_support: Optional[int] = None,
        message_target_max_per_log: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        从 logs 表获取服务节点和启发式关系

        Returns:
            {
                "nodes": [{id, label, type, metrics}],
                "edges": [{source, target, type, metrics}]
            }
        """
        try:
            safe_time_window = self._sanitize_interval(time_window, default_value="1 HOUR")
            if not self.storage.ch_client:
                return {"nodes": [], "edges": []}

            # 查询服务统计
            prewhere_conditions = [f"timestamp > now() - INTERVAL {safe_time_window}"]
            if namespace:
                prewhere_conditions.append(f"namespace = '{self._escape_sql_literal(namespace)}'")
            prewhere_clause = "PREWHERE " + " AND ".join(prewhere_conditions)

            query = f"""
            SELECT
                service_name,
                namespace,
                COUNT(*) as log_count,
                COUNT(DISTINCT pod_name) as pod_count,
                SUM(CASE WHEN lower(level) IN ('error', 'fatal') THEN 1 ELSE 0 END) as error_count,
                MAX(timestamp) as last_seen
            FROM logs.logs
            {prewhere_clause}
            GROUP BY service_name, namespace
            ORDER BY log_count DESC
            LIMIT {int(self.LOGS_SCAN_LIMIT)}
            """

            result = self.storage.execute_query(query)
            logger.debug(f"_get_logs_topology query returned {len(result) if result else 0} rows")
            window_seconds = float(self._time_window_seconds(safe_time_window))

            # 构建节点
            nodes = []
            service_node_candidates: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
            for row in result:
                service_name = row.get("service_name")
                log_count = row.get("log_count", 0)
                pod_count = row.get("pod_count", 0)
                error_count = row.get("error_count", 0)
                last_seen = row.get("last_seen")
                resolved_namespace = str(row.get("namespace") or "").strip()
                if not resolved_namespace and namespace:
                    resolved_namespace = str(namespace).strip()

                if service_name:
                    logger.debug(f"Processing service: {service_name}, last_seen type: {type(last_seen)}, value: {last_seen}")

                    # 处理 last_seen 时区问题 - 始终使用 ISO 格式
                    if last_seen:
                        if hasattr(last_seen, 'tzinfo'):
                            if last_seen.tzinfo is None:
                                # 如果是 naive datetime，添加 UTC 时区
                                last_seen = last_seen.replace(tzinfo=timezone.utc)
                            # 使用 isoformat() 确保时区信息被保留
                            last_seen_str = last_seen.isoformat()
                        else:
                            last_seen_str = str(last_seen)
                    else:
                        last_seen_str = None

                    node_id = self._build_node_id(
                        service_name=str(service_name).strip(),
                        namespace_value=resolved_namespace,
                        namespace_filter=namespace,
                    )
                    node_payload = {
                        "id": node_id,
                        "label": service_name,
                        "type": "service",
                        "name": service_name,
                        "namespace": resolved_namespace or "",
                        "metrics": {
                            "log_count": log_count,
                            "pod_count": pod_count,
                            "error_count": error_count,
                            "error_rate": round(error_count / log_count, 4) if log_count > 0 else 0,
                            "rps": round((float(log_count) / window_seconds), 4) if window_seconds > 0 else 0.0,
                            "namespace": resolved_namespace or "",
                            "service_namespace": resolved_namespace or "",
                            "namespace_count": 1 if resolved_namespace else 0,
                            "namespace_ambiguous": False,
                            "last_seen": last_seen_str,
                            "data_source": "logs",
                            "data_sources": ["logs"],
                            "confidence": 0.5  # logs 数据中等可靠
                        }
                    }
                    nodes.append(node_payload)
                    service_node_candidates[str(service_name).strip()].append(node_payload)

            def _resolve_single_service_node(service_name_value: str) -> Optional[Dict[str, Any]]:
                candidates = service_node_candidates.get(str(service_name_value or "").strip()) or []
                if len(candidates) == 1:
                    return candidates[0]
                return None

            # 使用启发式规则构建边
            edges = []
            service_names = sorted(service_node_candidates.keys())
            service_log_counts = {
                service_name_value: sum(int(node.get("metrics", {}).get("log_count") or 0) for node in candidates)
                for service_name_value, candidates in service_node_candidates.items()
            }

            # M2: 无埋点证据推断（request_id 优先 + 时间窗回退）
            inferred_edges, inference_stats = self._infer_edges_from_logs(
                time_window=time_window,
                namespace=namespace,
                inference_mode=inference_mode,
                message_target_enabled=message_target_enabled,
                message_target_patterns=message_target_patterns,
                message_target_min_support=message_target_min_support,
                message_target_max_per_log=message_target_max_per_log,
            )
            edges.extend(inferred_edges)
            strong_evidence_pairs: Set[Tuple[str, str]] = set()
            for inferred_edge in inferred_edges:
                metrics = inferred_edge.get("metrics", {}) if isinstance(inferred_edge.get("metrics"), dict) else {}
                method = str(metrics.get("inference_method") or "").strip()
                if method not in {"request_id", "trace_id", "message_target"}:
                    continue
                source = inferred_edge.get("source_service") or inferred_edge.get("metrics", {}).get("source_service")
                target = inferred_edge.get("target_service") or inferred_edge.get("metrics", {}).get("target_service")
                if not source or not target:
                    continue
                # 强证据按无向对抑制弱启发式，避免出现相反方向噪声边。
                strong_evidence_pairs.add((source, target))
                strong_evidence_pairs.add((target, source))
            strong_evidence_edges = (
                int(inference_stats.get("request_id_edges", 0))
                + int(inference_stats.get("trace_id_edges", 0))
                + int(inference_stats.get("message_target_edges", 0))
            )

            registry_heuristic_edges = 0
            for i, source in enumerate(service_names):
                for target in service_names[i+1:]:
                    if (source, target) in strong_evidence_pairs:
                        continue
                    if self._is_service_pair_related(source, target):
                        # 推断调用方向
                        if self._should_call(source, target):
                            caller, callee = source, target
                        else:
                            caller, callee = target, source
                        reason = self._get_relation_reason(caller, callee)

                        # image_pull_pattern 保守化：仅保留少量业务服务->registry 边，避免噪声刷屏。
                        if "image_pull_pattern" in str(reason or ""):
                            if strong_evidence_edges <= 0:
                                # 无强证据时，registry 启发式噪声过高，直接跳过。
                                continue
                            if registry_heuristic_edges >= 2:
                                continue
                            if self._is_infrastructure_service(caller):
                                continue
                            if caller.lower() in {"coredns", "kubelet", "kube-proxy"}:
                                continue
                            if service_log_counts.get(caller, 0) < 50:
                                continue
                            registry_heuristic_edges += 1

                        caller_node = _resolve_single_service_node(caller)
                        callee_node = _resolve_single_service_node(callee)
                        if not caller_node or not callee_node:
                            continue

                        edges.append({
                            "id": f"{caller_node['id']}-{callee_node['id']}",
                            "source": caller_node["id"],
                            "target": callee_node["id"],
                            "source_service": caller,
                            "target_service": callee,
                            "source_namespace": caller_node.get("namespace") or caller_node.get("metrics", {}).get("service_namespace") or "",
                            "target_namespace": callee_node.get("namespace") or callee_node.get("metrics", {}).get("service_namespace") or "",
                            "label": "potential-calls",
                            "type": "calls",
                            "metrics": {
                                "call_count": None,  # logs 无法提供准确调用次数
                                "p95": 0.0,
                                "p99": 0.0,
                                "timeout_rate": 0.0,
                                "retries": 0.0,
                                "pending": 0.0,
                                "dlq": 0.0,
                                "protocol": "http",
                                "endpoint_pattern": "/unknown",
                                "confidence": 0.3,  # 启发式规则，低置信度
                                "data_source": "logs_heuristic",
                                "data_sources": ["logs_heuristic"],
                                "reason": reason,
                                "source_service": caller,
                                "target_service": callee,
                                "source_namespace": caller_node.get("namespace") or caller_node.get("metrics", {}).get("service_namespace") or "",
                                "target_namespace": callee_node.get("namespace") or callee_node.get("metrics", {}).get("service_namespace") or "",
                            }
                        })

            # 去重，优先保留 call_count 更高 / 置信度更高的边
            dedup_edges = hybrid_utils.dedup_edges_by_metric_score(edges)

            return {
                "nodes": nodes,
                "edges": dedup_edges,
                "metadata": {
                    "inference_stats": inference_stats
                }
            }

        except Exception as e:
            logger.error(f"Error getting logs topology: {e}")
            return {"nodes": [], "edges": []}

    def _get_metrics_topology(
        self,
        time_window: str,
        namespace: str = None
    ) -> Dict[str, Any]:
        """
        从 metrics 表获取服务关系验证

        主要用于验证从 traces/logs 推断的关系

        Returns:
            {
                "nodes": [{id, label, type, metrics}],
                "edges": [{source, target, type, metrics}]
            }
        """
        try:
            safe_time_window = self._sanitize_interval(time_window, default_value="1 HOUR")
            if not self.storage.ch_client:
                return {"nodes": [], "edges": []}
            has_metrics_namespace_column = self._has_metrics_namespace_column()
            namespace_expr = (
                "metrics_namespace"
                if has_metrics_namespace_column
                else (
                    "if("
                    "length(JSONExtractString(attributes_json, 'service_namespace')) > 0, "
                    "JSONExtractString(attributes_json, 'service_namespace'), "
                    "JSONExtractString(attributes_json, 'namespace')"
                    ")"
                )
            )

            # 查询服务列表
            prewhere_clause = f"PREWHERE timestamp > now() - INTERVAL {safe_time_window}"
            where_clause = ""
            if namespace:
                escaped_namespace = str(namespace).replace("'", "''")
                where_clause = f"WHERE {namespace_expr} = '{escaped_namespace}'"

            query = f"""
            SELECT
                service_name,
                topK(1)({namespace_expr}) as namespace_top,
                COUNT(*) as metric_count,
                COUNT(DISTINCT metric_name) as unique_metrics
            FROM logs.metrics
            {prewhere_clause}
            {where_clause}
            GROUP BY service_name
            ORDER BY metric_count DESC
            LIMIT {int(self.METRICS_SCAN_LIMIT)}
            """

            result = self.storage.execute_query(query)

            # 构建节点
            nodes = []
            for row in result:
                service_name = row.get("service_name")
                metric_count = row.get("metric_count", 0)
                unique_metrics = row.get("unique_metrics", 0)
                namespace_top = row.get("namespace_top")
                if isinstance(namespace_top, (list, tuple)) and namespace_top:
                    resolved_namespace = str(namespace_top[0] or "").strip()
                else:
                    resolved_namespace = str(row.get("service_namespace") or "").strip()
                if not resolved_namespace and namespace:
                    resolved_namespace = str(namespace).strip()

                if service_name:
                    nodes.append({
                        "id": self._build_node_id(
                            service_name=str(service_name).strip(),
                            namespace_value=resolved_namespace,
                            namespace_filter=namespace,
                        ),
                        "label": service_name,
                        "type": "service",
                        "name": service_name,
                        "namespace": resolved_namespace or "",
                        "metrics": {
                            "metric_count": metric_count,
                            "unique_metrics": unique_metrics,
                            "namespace": resolved_namespace or "",
                            "service_namespace": resolved_namespace or "",
                            "namespace_count": 1 if resolved_namespace else 0,
                            "namespace_ambiguous": False,
                            "data_source": "metrics",
                            "data_sources": ["metrics"],
                            "confidence": 0.4
                        }
                    })

            # Metrics 通常不包含直接的调用关系
            # 返回空边列表，节点用于验证其他数据源
            return {
                "nodes": nodes,
                "edges": []
            }

        except Exception as e:
            logger.error(f"Error getting metrics topology: {e}")
            return {"nodes": [], "edges": []}

    def _extract_request_id(self, attrs: Dict[str, Any], message: str = "") -> str:
        """
        提取 request_id，作为 M2 推断关联器的一等键。

        优先级：
        1. attributes 常见 request_id 字段
        2. message 中显式 request_id=xxx / x-request-id=xxx
        """
        return hybrid_utils.extract_request_id(attrs, message=message)

    def _infer_edges_from_logs(
        self,
        time_window: str,
        namespace: str = None,
        inference_mode: Optional[str] = None,
        message_target_enabled: Optional[bool] = None,
        message_target_patterns: Optional[Any] = None,
        message_target_min_support: Optional[int] = None,
        message_target_max_per_log: Optional[int] = None,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """
        基于日志构建 inferred edges（M2）。

        策略：
        1. request_id 优先关联（高置信）
        2. trace_id 次优关联（中置信）
        3. message target 关联（可配置，多协议日志模式）
        4. 时间窗回退（低置信，且附加降噪规则）
        """
        runtime = hybrid_utils.resolve_inference_runtime_settings(
            inference_mode=inference_mode,
            default_inference_mode=self.INFERENCE_MODE,
            message_target_enabled=message_target_enabled,
            default_message_target_enabled=self.MESSAGE_TARGET_ENABLED,
            message_target_patterns=message_target_patterns,
            default_message_target_patterns=set(self.MESSAGE_TARGET_PATTERNS),
            resolve_message_target_patterns_override_fn=self._resolve_message_target_patterns_override,
            message_target_min_support=message_target_min_support,
            default_message_target_min_support=self.MIN_SUPPORT_MESSAGE_TARGET,
            message_target_max_per_log=message_target_max_per_log,
            default_message_target_max_per_log=self.MAX_MESSAGE_TARGETS_PER_LOG,
            resolve_inference_mode_override_fn=self._resolve_inference_mode_override,
        )
        effective_inference_mode = runtime["effective_inference_mode"]
        effective_message_target_enabled = runtime["effective_message_target_enabled"]
        effective_patterns = runtime["effective_patterns"]
        effective_min_support = runtime["effective_min_support"]
        effective_max_per_log = runtime["effective_max_per_log"]
        method_name = runtime["method_name"]
        if not self.storage.ch_client:
            return [], hybrid_utils.build_inference_empty_stats(
                method_name=method_name,
                message_target_enabled=effective_message_target_enabled,
                inference_mode=effective_inference_mode,
                message_target_patterns=effective_patterns,
                message_target_min_support=effective_min_support,
                message_target_max_per_log=effective_max_per_log,
            )

        safe_time_window = self._sanitize_interval(time_window, default_value="1 HOUR")
        window_minutes = self._interval_to_minutes(safe_time_window, default_minutes=60)
        infer_sample_limit = int(self.MAX_INFER_SAMPLE)
        if window_minutes <= 60:
            infer_sample_limit = min(infer_sample_limit, int(self.MAX_INFER_SAMPLE_SHORT_WINDOW))
        elif window_minutes <= 24 * 60:
            infer_sample_limit = min(infer_sample_limit, int(self.MAX_INFER_SAMPLE_MEDIUM_WINDOW))
        prewhere_conditions = [
            f"timestamp > now() - INTERVAL {safe_time_window}",
            "notEmpty(service_name)",
        ]
        if namespace:
            prewhere_conditions.append(f"namespace = '{self._escape_sql_literal(namespace)}'")
        prewhere_clause = "PREWHERE " + " AND ".join(prewhere_conditions)

        query = f"""
        SELECT
            id,
            timestamp,
            service_name,
            namespace,
            message,
            trace_id,
            attributes_json
        FROM logs.logs
        {prewhere_clause}
        ORDER BY timestamp DESC
        LIMIT {infer_sample_limit}
        SETTINGS optimize_use_projections = 1, optimize_read_in_order = 1
        """

        rows = self.storage.execute_query(query)
        if not rows:
            # 当窗口内有日志但主查询返回空时，尝试轻量回退（不读取 attributes_json）。
            count_query = f"""
            SELECT count() AS cnt
            FROM logs.logs
            {prewhere_clause}
            SETTINGS optimize_use_projections = 1
            """
            count_rows = self.storage.execute_query(count_query)
            total_candidates = 0
            if count_rows:
                try:
                    total_candidates = int((count_rows[0] or {}).get("cnt") or 0)
                except (TypeError, ValueError):
                    total_candidates = 0

            if total_candidates > 0:
                fallback_limit = max(500, min(infer_sample_limit, 2000))
                fallback_query = f"""
                SELECT
                    id,
                    timestamp,
                    service_name,
                    namespace,
                    message,
                    trace_id
                FROM logs.logs
                {prewhere_clause}
                ORDER BY timestamp DESC
                LIMIT {fallback_limit}
                SETTINGS optimize_use_projections = 1, optimize_read_in_order = 1
                """
                rows = self.storage.execute_query(fallback_query)
                if rows:
                    logger.warning(
                        "Inference logs query fallback activated: primary empty with %s candidates, fallback_limit=%s",
                        total_candidates,
                        fallback_limit,
                    )
        if rows:
            # Query newest N rows for speed, then restore chronological order for inference logic.
            rows = list(reversed(rows))
        if not rows:
            return [], hybrid_utils.build_inference_empty_stats(
                method_name=method_name,
                message_target_enabled=effective_message_target_enabled,
                inference_mode=effective_inference_mode,
                message_target_patterns=effective_patterns,
                message_target_min_support=effective_min_support,
                message_target_max_per_log=effective_max_per_log,
            )

        def _resolve_record_namespace(
            row_data: Dict[str, Any],
            attrs_data: Dict[str, Any],
            service_name_value: str,
            namespace_hint: str = "",
        ) -> str:
            service_token = str(service_name_value or "").strip().lower()
            k8s_obj = attrs_data.get("k8s") if isinstance(attrs_data.get("k8s"), dict) else {}
            kubernetes_obj = attrs_data.get("kubernetes") if isinstance(attrs_data.get("kubernetes"), dict) else {}
            candidates = (
                row_data.get("namespace"),
                namespace_hint,
                attrs_data.get("k8s.namespace.name"),
                attrs_data.get("service_namespace"),
                attrs_data.get("kubernetes.namespace_name"),
                k8s_obj.get("namespace_name"),
                k8s_obj.get("namespace"),
                k8s_obj.get("namespaceName"),
                kubernetes_obj.get("namespace_name"),
                kubernetes_obj.get("namespace"),
                kubernetes_obj.get("namespaceName"),
                attrs_data.get("namespace"),
                namespace,
            )

            for idx, candidate in enumerate(candidates):
                token = str(candidate or "").strip()
                if not token:
                    continue
                lowered = token.lower()
                if lowered in {"unknown", "none", "null", "-", "n/a"}:
                    continue
                # attrs.namespace 兜底项（idx=10）需要避开与 service_name 同值的误标。
                if idx == 10 and service_token and lowered == service_token:
                    continue
                return token
            return "unknown"

        prepared = []
        for row in rows:
            service_name, namespace_hint = self._decode_service_identity(row.get("service_name"))
            if not service_name:
                continue

            message_text = row.get("message") or ""
            attrs: Dict[str, Any] = {}
            request_id = self._extract_request_id({}, message=message_text)
            namespace_token = str(row.get("namespace") or "").strip().lower()
            needs_namespace_fallback = (
                namespace_token in {"", "unknown", "none", "null", "-", "n/a"}
                and not self._normalize_namespace_token(namespace_hint)
            )
            should_parse_attrs = needs_namespace_fallback or not request_id
            raw_attrs = row.get("attributes_json")
            if should_parse_attrs:
                if isinstance(raw_attrs, str) and raw_attrs:
                    try:
                        attrs = json.loads(raw_attrs)
                    except Exception:
                        attrs = {}
                elif isinstance(raw_attrs, dict):
                    attrs = raw_attrs
                if not request_id:
                    request_id = self._extract_request_id(attrs, message=message_text)

            prepared.append({
                "id": row.get("id"),
                "ts": self._timestamp_to_datetime(row.get("timestamp")),
                "service_name": service_name,
                "namespace": _resolve_record_namespace(
                    row,
                    attrs,
                    service_name,
                    namespace_hint=namespace_hint,
                ),
                "message": message_text,
                "trace_id": row.get("trace_id") or "",
                "attrs": attrs,
                "request_id": request_id,
            })

        partitioned = hybrid_utils.partition_prepared_inference_records(prepared)
        request_groups = partitioned["request_groups"]
        trace_groups = partitioned["trace_groups"]
        fallback_records = partitioned["fallback_records"]
        if len(fallback_records) > self.MAX_TIME_WINDOW_FALLBACK_RECORDS:
            fallback_records = fallback_records[-self.MAX_TIME_WINDOW_FALLBACK_RECORDS:]

        edge_acc: Dict[Tuple[str, str], Dict[str, Any]] = {}
        base_known_services = {
            str(item.get("service_name") or "").strip().lower(): str(item.get("service_name") or "").strip()
            for item in prepared
            if str(item.get("service_name") or "").strip()
        }
        known_services = self._build_known_service_aliases(base_known_services)
        service_log_volume = Counter(item.get("service_name") for item in prepared if item.get("service_name"))
        normalize_namespace = lambda value: str(value or "").strip().lower()

        def add_inferred(
            source: str,
            target: str,
            source_namespace: str,
            target_namespace: str,
            evidence: Dict[str, Any],
            method: str,
            event_ts: datetime,
            weight: float = 1.0,
            namespace_match: Optional[bool] = None,
            delta_sec: Optional[float] = None,
        ) -> bool:
            if not source or not target or source == target:
                return False
            if method == "time_window" and (
                self._is_infrastructure_service(source) or self._is_infrastructure_service(target)
            ):
                # 基础设施服务在时间窗回退场景中噪声很高，默认跳过。
                return False
            normalized_source_namespace = str(source_namespace or "").strip()
            normalized_target_namespace = str(target_namespace or "").strip()
            key = (source, target, normalized_source_namespace, normalized_target_namespace)
            if key not in edge_acc:
                edge_acc[key] = {
                    "count": 0,
                    "method_counts": Counter(),
                    "evidence_chain": [],
                    "last_seen": event_ts,
                    "weighted_score": 0.0,
                    "namespace_match_total": 0,
                    "namespace_match_hits": 0,
                    "temporal_gaps": [],
                    "source_namespace": normalized_source_namespace,
                    "target_namespace": normalized_target_namespace,
                }
            edge_acc[key]["count"] += 1
            edge_acc[key]["method_counts"][method] += 1
            edge_acc[key]["weighted_score"] += max(0.05, float(weight or 0.0))
            if namespace_match is not None:
                edge_acc[key]["namespace_match_total"] += 1
                if bool(namespace_match):
                    edge_acc[key]["namespace_match_hits"] += 1
            if delta_sec is not None and delta_sec >= 0:
                temporal_gaps = edge_acc[key].setdefault("temporal_gaps", [])
                if len(temporal_gaps) < 24:
                    temporal_gaps.append(float(delta_sec))
            if event_ts and (
                edge_acc[key].get("last_seen") is None or event_ts > edge_acc[key].get("last_seen")
            ):
                edge_acc[key]["last_seen"] = event_ts
            if len(edge_acc[key]["evidence_chain"]) < 8:
                payload = dict(evidence or {})
                payload.setdefault("source_namespace", normalized_source_namespace)
                payload.setdefault("target_namespace", normalized_target_namespace)
                payload["method"] = method
                payload["weight"] = round(max(0.05, float(weight or 0.0)), 3)
                edge_acc[key]["evidence_chain"].append(payload)
            return True

        # 1) request_id 优先
        request_id_edges = hybrid_utils.accumulate_group_sequence_edges(
            groups=request_groups,
            group_field_name="request_id",
            method="request_id",
            inference_mode=effective_inference_mode,
            hybrid_weight=1.2,
            dedup_sequence_fn=self._dedup_service_sequence,
            add_inferred_fn=add_inferred,
            normalize_namespace_fn=normalize_namespace,
        )

        # 2) trace_id 关联（request_id 缺失时）
        trace_id_edges = hybrid_utils.accumulate_group_sequence_edges(
            groups=trace_groups,
            group_field_name="trace_id",
            method="trace_id",
            inference_mode=effective_inference_mode,
            hybrid_weight=1.05,
            dedup_sequence_fn=self._dedup_service_sequence,
            add_inferred_fn=add_inferred,
            normalize_namespace_fn=normalize_namespace,
        )

        # 3) message target 关联（URL host -> 服务名）
        message_target_edges = 0
        if effective_message_target_enabled:
            message_target_edges = hybrid_utils.accumulate_message_target_edges(
                prepared=prepared,
                inference_mode=effective_inference_mode,
                extract_message_target_services_fn=lambda message, enabled, patterns, max_targets_per_log: (
                    self._extract_message_target_services(
                        message=message,
                        known_services=known_services,
                        enabled=enabled,
                        patterns=patterns,
                        max_targets_per_log=max_targets_per_log,
                    )
                ),
                add_inferred_fn=add_inferred,
                patterns=effective_patterns,
                max_targets_per_log=effective_max_per_log,
            )

        # 4) 时间窗回退（仅 request_id/trace_id 都缺失）
        time_window_edges_raw = hybrid_utils.accumulate_time_window_fallback_edges(
            fallback_records=fallback_records,
            inference_mode=effective_inference_mode,
            max_candidates_per_log=self.MAX_TIME_WINDOW_CANDIDATES_PER_LOG,
            max_delta_sec=self.MAX_TIME_WINDOW_DELTA_SEC,
            is_likely_outbound_message_fn=self._is_likely_outbound_message,
            is_likely_inbound_message_fn=self._is_likely_inbound_message,
            add_inferred_fn=add_inferred,
            normalize_namespace_fn=normalize_namespace,
        )

        # 双向噪声抑制：对 time_window / trace_id 的互逆边做约束
        dropped_bidirectional = hybrid_utils.compute_dropped_bidirectional_edges(
            edge_acc,
            inference_mode=effective_inference_mode,
            min_support_time_window=self.MIN_SUPPORT_TIME_WINDOW,
        )

        inferred_edges: List[Dict[str, Any]] = []
        method_policies = hybrid_utils.build_inference_method_policies(
            min_support_request_id=self.MIN_SUPPORT_REQUEST_ID,
            min_support_trace_id=self.MIN_SUPPORT_TRACE_ID,
            min_support_message_target=effective_min_support,
            min_support_time_window=self.MIN_SUPPORT_TIME_WINDOW,
        )
        method_min_support = method_policies["min_support"]
        method_base_confidence = method_policies["base_confidence"]
        method_reason = method_policies["reason"]
        evidence_sufficiency_scores: List[float] = []
        for (source, target, source_namespace, target_namespace), item in edge_acc.items():
            if (source, target) in dropped_bidirectional:
                continue

            evaluated = hybrid_utils.evaluate_inference_edge(
                edge_acc=edge_acc,
                source=source,
                target=target,
                item=item,
                inference_mode=effective_inference_mode,
                service_log_volume=service_log_volume,
                method_min_support=method_min_support,
                method_base_confidence=method_base_confidence,
                method_reason=method_reason,
                default_min_support=self.MIN_SUPPORT_TIME_WINDOW,
                estimate_dynamic_support_fn=self._estimate_dynamic_support,
                temporal_stability_fn=self.inference_scorer.temporal_stability,
                score_hybrid_edge_fn=self.inference_scorer.score_hybrid_edge,
            )
            if evaluated is None:
                continue

            evidence_sufficiency_scores.append(evaluated["evidence_sufficiency_score"])
            payload = evaluated["payload"]
            payload["source"] = source
            payload["target"] = target
            payload["id"] = f"{payload['source']}->{payload['target']}-inferred"
            metrics = payload.setdefault("metrics", {})
            metrics["source_service"] = source
            metrics["target_service"] = target
            metrics["source_namespace"] = source_namespace
            metrics["target_namespace"] = target_namespace
            inferred_edges.append(payload)

        stats = hybrid_utils.build_inference_stats(
            total_candidates=len(prepared),
            request_id_groups=len(request_groups),
            request_id_edges=request_id_edges,
            trace_id_groups=len(trace_groups),
            trace_id_edges=trace_id_edges,
            message_target_edges=message_target_edges,
            time_window_edges=time_window_edges_raw,
            dropped_bidirectional_edges=len(dropped_bidirectional),
            filtered_edges=len(inferred_edges),
            method_name=method_name,
            message_target_enabled=effective_message_target_enabled,
            inference_mode=effective_inference_mode,
            message_target_patterns=effective_patterns,
            message_target_min_support=effective_min_support,
            message_target_max_per_log=effective_max_per_log,
            evidence_sufficiency_scores=evidence_sufficiency_scores,
        )
        return inferred_edges, stats

    def _merge_nodes(
        self,
        traces_nodes: List[Dict],
        logs_nodes: List[Dict],
        metrics_nodes: List[Dict]
    ) -> List[Dict]:
        """
        合并来自不同数据源的节点

        策略：
        1. traces 数据优先（最准确）
        2. logs 数据补充（服务节点）
        3. metrics 数据验证（服务活跃度）
        """
        return hybrid_utils.merge_nodes(
            traces_nodes=traces_nodes,
            logs_nodes=logs_nodes,
            metrics_nodes=metrics_nodes,
        )

    def _merge_edges(
        self,
        traces_edges: List[Dict],
        logs_edges: List[Dict],
        metrics_edges: List[Dict]
    ) -> List[Dict]:
        """
        合并来自不同数据源的边并计算置信度

        策略：
        1. traces 边：置信度 1.0（精确）
        2. logs 边：置信度 0.3（启发式）
        3. 如果多个数据源都支持同一关系，提升置信度
        """
        return hybrid_utils.merge_edges(
            traces_edges=traces_edges,
            logs_edges=logs_edges,
            metrics_edges=metrics_edges,
            metrics_boost=0.1,
        )

    def _apply_edge_red_aggregation(
        self,
        merged_edges: List[Dict[str, Any]],
        time_window: str,
        namespace: str = None
    ) -> None:
        """
        使用存储层 edge RED 聚合结果补全边指标。

        当存储层不可用或查询失败时静默降级，保持原有边数据。
        """
        if not merged_edges or not hasattr(self.storage, "get_edge_red_metrics"):
            return

        has_trace_edge = False
        for edge in merged_edges:
            metrics = edge.get("metrics") if isinstance(edge, dict) else None
            if not isinstance(metrics, dict):
                continue
            primary_source = str(metrics.get("data_source") or "").strip().lower()
            if primary_source == "traces":
                has_trace_edge = True
                break
            for source_item in metrics.get("data_sources") or []:
                if str(source_item or "").strip().lower() == "traces":
                    has_trace_edge = True
                    break
            if has_trace_edge:
                break

        # edge RED 指标来自 traces 父子 span 关系，当前拓扑没有 traces 边时直接跳过查询。
        if not has_trace_edge:
            return

        try:
            aggregated = self.storage.get_edge_red_metrics(
                time_window=time_window,
                namespace=namespace
            )
        except Exception as exc:
            logger.warning(f"Failed to load edge RED aggregation: {exc}")
            return

        if not aggregated:
            return
        hybrid_utils.apply_aggregated_edge_metrics(
            merged_edges=merged_edges,
            aggregated=aggregated,
        )

    def _apply_contract_schema(
        self,
        nodes: List[Dict[str, Any]],
        edges: List[Dict[str, Any]]
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """
        统一 Node/Edge 契约输出。

        保留旧字段并新增:
        - node_key / edge_key
        - service(namespace/name/env)
        - evidence_type / coverage / quality_score
        - p95 / p99 / timeout_rate
        """
        return hybrid_utils.apply_contract_schema(
            nodes=nodes,
            edges=edges,
            apply_node_contract_fn=apply_node_contract,
            apply_edge_contract_fn=apply_edge_contract,
        )

    def _is_service_pair_related(self, service1: str, service2: str) -> bool:
        """判断两个服务是否可能存在调用关系（启发式规则）"""
        return hybrid_utils.is_service_pair_related(service1, service2)

    def _should_call(self, service1: str, service2: str) -> bool:
        """判断 service1 是否应该调用 service2"""
        return hybrid_utils.should_call(service1, service2)

    def _get_relation_reason(self, caller: str, callee: str) -> str:
        """获取调用关系的理由"""
        return hybrid_utils.get_relation_reason(caller, callee)

    def _get_data_sources(
        self,
        traces_data: Dict,
        logs_data: Dict,
        metrics_data: Dict
    ) -> List[str]:
        """获取实际使用的数据源列表"""
        return hybrid_utils.get_data_sources(
            traces_data=traces_data,
            logs_data=logs_data,
            metrics_data=metrics_data,
        )


# 全局实例（延迟初始化）
_hybrid_builder = None
_hybrid_builder_storage = None


def get_hybrid_topology_builder(storage_adapter) -> HybridTopologyBuilder:
    """
    获取混合拓扑构建器实例

    Args:
        storage_adapter: StorageAdapter 实例

    Returns:
        HybridTopologyBuilder 实例
    """
    global _hybrid_builder, _hybrid_builder_storage
    
    # 如果 storage_adapter 变化了，重新创建 builder
    if _hybrid_builder is None or _hybrid_builder_storage != storage_adapter:
        _hybrid_builder = HybridTopologyBuilder(storage_adapter)
        _hybrid_builder_storage = storage_adapter
    return _hybrid_builder
