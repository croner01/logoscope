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
import traceback
from typing import Dict, List, Any, Set, Tuple
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from graph.confidence_calculator import get_confidence_calculator

logger = logging.getLogger(__name__)


def _read_positive_int_env(
    name: str,
    default_value: int,
    min_value: int = 1000,
    max_value: int = 2_000_000,
) -> int:
    """读取正整数环境变量，并限制范围，避免异常配置放大查询压力。"""
    raw = os.getenv(name, str(default_value))
    try:
        parsed = int(str(raw).strip())
    except Exception:
        parsed = default_value
    return max(min_value, min(parsed, max_value))


def _escape_sql_literal(value: str) -> str:
    """转义 SQL 字符串字面量中的单引号。"""
    return str(value).replace("'", "''")


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
        self.TRACES_SCAN_LIMIT = _read_positive_int_env("HYBRID_TOPOLOGY_TRACES_SCAN_LIMIT", 200000)
        self.LOGS_SCAN_LIMIT = _read_positive_int_env("HYBRID_TOPOLOGY_LOGS_SCAN_LIMIT", 300000)
        self.METRICS_SCAN_LIMIT = _read_positive_int_env("HYBRID_TOPOLOGY_METRICS_SCAN_LIMIT", 100000)

    def build_topology(
        self,
        time_window: str = "1 HOUR",
        namespace: str = None,
        confidence_threshold: float = 0.3
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
            print(f"[DEBUG] Starting build_topology with time_window={time_window}", flush=True)
            logger.info(f"Building hybrid topology with time_window={time_window}")

            # 1. 从三个数据源收集数据
            try:
                traces_data = self._get_traces_topology(time_window, namespace)
                print(f"[DEBUG] traces_data: {len(traces_data.get('nodes', []))} nodes", flush=True)
            except Exception as e:
                logger.error(f"Error in _get_traces_topology: {e}")
                import traceback
                print(f"[TRACEBACK_TRACES] {traceback.format_exc()}", flush=True)
                traces_data = {"nodes": [], "edges": []}

            try:
                logs_data = self._get_logs_topology(time_window, namespace)
                print(f"[DEBUG] logs_data: {len(logs_data.get('nodes', []))} nodes", flush=True)
            except Exception as e:
                logger.error(f"Error in _get_logs_topology: {e}")
                import traceback
                print(f"[TRACEBACK_LOGS] {traceback.format_exc()}", flush=True)
                logs_data = {"nodes": [], "edges": []}

            try:
                metrics_data = self._get_metrics_topology(time_window, namespace)
                print(f"[DEBUG] metrics_data: {len(metrics_data.get('nodes', []))} nodes", flush=True)
            except Exception as e:
                logger.error(f"Error in _get_metrics_topology: {e}")
                import traceback
                print(f"[TRACEBACK_METRICS] {traceback.format_exc()}", flush=True)
                metrics_data = {"nodes": [], "edges": []}

            # ⚠️ 自适应时间窗口：如果所有数据源都为空，扩大到 24 小时
            total_nodes = (
                len(traces_data.get("nodes", [])) +
                len(logs_data.get("nodes", [])) +
                len(metrics_data.get("nodes", []))
            )

            if total_nodes == 0 and time_window != "24 HOUR":
                logger.warning(f"No data found in {time_window}, expanding to 24 HOUR")
                time_window = "24 HOUR"
                try:
                    traces_data = self._get_traces_topology(time_window, namespace)
                except Exception as e:
                    logger.error(f"Error in _get_traces_topology (24H): {e}")
                    traces_data = {"nodes": [], "edges": []}

                try:
                    logs_data = self._get_logs_topology(time_window, namespace)
                except Exception as e:
                    logger.error(f"Error in _get_logs_topology (24H): {e}")
                    logs_data = {"nodes": [], "edges": []}

                try:
                    metrics_data = self._get_metrics_topology(time_window, namespace)
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

            metadata = {
                "data_sources": self._get_data_sources(traces_data, logs_data, metrics_data),
                "time_window": time_window,
                "namespace": namespace,
                "node_count": len(merged_nodes),
                "edge_count": len(filtered_edges),
                "avg_confidence": round(avg_confidence, 2),
                "generated_at": datetime.now(timezone.utc).isoformat(),
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
            import traceback as tb
            error_trace = tb.format_exc()
            logger.error(f"Error building hybrid topology: {e}")
            logger.error(f"Traceback: {error_trace}")
            print(f"[FULL_TRACEBACK] {error_trace}", flush=True)
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
            if not self.storage.ch_client:
                return {"nodes": [], "edges": []}

            # 查询 traces 表获取调用链
            prewhere_conditions = [f"timestamp > now() - INTERVAL {time_window}"]
            if namespace:
                prewhere_conditions.append(f"namespace = '{namespace}'")
            prewhere_clause = "PREWHERE " + " AND ".join(prewhere_conditions)

            # 查询 span 关系（通过 trace_id 和 parent_span_id）
            query = f"""
            SELECT
                trace_id,
                span_id,
                parent_span_id,
                service_name,
                operation_name,
                duration_ms,
                status
            FROM logs.traces
            {prewhere_clause}
            ORDER BY timestamp DESC
            LIMIT {int(self.TRACES_SCAN_LIMIT)}
            """

            result = self.storage.execute_query(query)

            # 构建调用关系图
            nodes = {}  # {service_name: node_data}
            edges = defaultdict(lambda: {
                "call_count": 0,
                "total_duration": 0,
                "error_count": 0,
                "spans": []
            })

            # 按 trace_id 分组
            traces_by_id = defaultdict(list)
            for row in result:
                if len(row) >= 7:
                    trace_id, span_id, parent_span_id, service_name, operation_name, duration_ms, status = row
                    traces_by_id[trace_id].append({
                        "span_id": span_id,
                        "parent_span_id": parent_span_id,
                        "service_name": service_name,
                        "operation_name": operation_name,
                        "duration_ms": duration_ms or 0,
                        "status": status
                    })

            # 分析每个 trace 的调用关系
            for trace_id, spans in traces_by_id.items():
                # 构建 span_id -> span 的映射
                spans_by_id = {span["span_id"]: span for span in spans}

                for span in spans:
                    service_name = span["service_name"]

                    # 添加节点
                    if service_name not in nodes:
                        nodes[service_name] = {
                            "id": service_name,
                            "label": service_name,
                            "type": "service",
                            "name": service_name,
                            "metrics": {
                                "trace_count": 0,
                                "span_count": 0,
                                "avg_duration": 0,
                                "error_count": 0,
                                "data_source": "traces",
                                "confidence": 1.0  # traces 数据最可靠
                            }
                        }

                    nodes[service_name]["metrics"]["span_count"] += 1
                    nodes[service_name]["metrics"]["avg_duration"] += span["duration_ms"]
                    if span["status"].lower() == "error":
                        nodes[service_name]["metrics"]["error_count"] += 1

                    # 如果有 parent_span_id，找到父服务
                    parent_span_id = span["parent_span_id"]
                    if parent_span_id and parent_span_id in spans_by_id:
                        parent_service = spans_by_id[parent_span_id]["service_name"]

                        # 确保父服务节点存在
                        if parent_service not in nodes:
                            nodes[parent_service] = {
                                "id": parent_service,
                                "label": parent_service,
                                "type": "service",
                                "name": parent_service,
                                "metrics": {
                                    "trace_count": 0,
                                    "span_count": 0,
                                    "avg_duration": 0,
                                    "error_count": 0,
                                    "data_source": "traces",
                                    "confidence": 1.0
                                }
                            }

                        # 添加边
                        edge_key = (parent_service, service_name)
                        edges[edge_key]["call_count"] += 1
                        edges[edge_key]["total_duration"] += span["duration_ms"]
                        if span["status"].lower() == "error":
                            edges[edge_key]["error_count"] += 1
                        edges[edge_key]["spans"].append(span)

            # 计算每个 trace 的根节点（无 parent 的 span）
            for trace_id, spans in traces_by_id.items():
                root_spans = [s for s in spans if not s.get("parent_span_id")]
                for root_span in root_spans:
                    service_name = root_span["service_name"]
                    if service_name in nodes:
                        nodes[service_name]["metrics"]["trace_count"] += 1

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

                edge_list.append({
                    "id": f"{source}-{target}",
                    "source": source,
                    "target": target,
                    "label": "calls",
                    "type": "calls",
                    "metrics": {
                        "call_count": call_count,
                        "avg_duration": round(avg_duration, 2),
                        "error_count": data["error_count"],
                        "error_rate": round(error_rate, 2),
                        "data_source": "traces",
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
        namespace: str = None
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
            if not self.storage.ch_client:
                return {"nodes": [], "edges": []}

            # 查询服务统计
            prewhere_conditions = [f"timestamp > now() - INTERVAL {time_window}"]
            if namespace:
                prewhere_conditions.append(f"namespace = '{namespace}'")
            prewhere_clause = "PREWHERE " + " AND ".join(prewhere_conditions)

            query = f"""
            SELECT
                service_name,
                COUNT(*) as log_count,
                COUNT(DISTINCT pod_name) as pod_count,
                SUM(CASE WHEN level = 'error' THEN 1 ELSE 0 END) as error_count,
                MAX(timestamp) as last_seen
            FROM logs.logs
            {prewhere_clause}
            GROUP BY service_name
            ORDER BY log_count DESC
            LIMIT {int(self.LOGS_SCAN_LIMIT)}
            """

            result = self.storage.execute_query(query)
            logger.debug(f"_get_logs_topology query returned {len(result) if result else 0} rows")

            # 构建节点
            nodes = []
            for row in result:
                if len(row) >= 5:
                    service_name, log_count, pod_count, error_count, last_seen = row
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

                    nodes.append({
                        "id": service_name,
                        "label": service_name,
                        "type": "service",
                        "name": service_name,
                        "metrics": {
                            "log_count": log_count,
                            "pod_count": pod_count,
                            "error_count": error_count,
                            "error_rate": round(error_count / log_count, 2) if log_count > 0 else 0,
                            "last_seen": last_seen_str,
                            "data_source": "logs",
                            "confidence": 0.5  # logs 数据中等可靠
                        }
                    })

            # 使用启发式规则构建边
            edges = []
            service_names = [node["id"] for node in nodes]

            for i, source in enumerate(service_names):
                for target in service_names[i+1:]:
                    if self._is_service_pair_related(source, target):
                        # 推断调用方向
                        if self._should_call(source, target):
                            caller, callee = source, target
                        else:
                            caller, callee = target, source

                        edges.append({
                            "id": f"{caller}-{callee}",
                            "source": caller,
                            "target": callee,
                            "label": "potential-calls",
                            "type": "calls",
                            "metrics": {
                                "call_count": None,  # logs 无法提供准确调用次数
                                "confidence": 0.3,  # 启发式规则，低置信度
                                "data_source": "logs_heuristic",
                                "reason": self._get_relation_reason(caller, callee)
                            }
                        })

            return {
                "nodes": nodes,
                "edges": edges
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
            if not self.storage.ch_client:
                return {"nodes": [], "edges": []}

            # 查询服务列表
            prewhere_conditions = [f"timestamp > now() - INTERVAL {time_window}"]
            prewhere_clause = "PREWHERE " + " AND ".join(prewhere_conditions)
            where_clause = ""
            if namespace:
                safe_namespace = _escape_sql_literal(namespace)
                where_clause = (
                    "WHERE "
                    "("
                    f"JSONExtractString(attributes_json, 'service_namespace') = '{safe_namespace}' "
                    f"OR JSONExtractString(attributes_json, 'namespace') = '{safe_namespace}'"
                    ")"
                )

            query = f"""
            SELECT
                service_name,
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
                if len(row) >= 3:
                    service_name, metric_count, unique_metrics = row

                    nodes.append({
                        "id": service_name,
                        "label": service_name,
                        "type": "service",
                        "name": service_name,
                        "metrics": {
                            "metric_count": metric_count,
                            "unique_metrics": unique_metrics,
                            "data_source": "metrics",
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
        merged = {}  # {service_name: merged_node}

        # 1. 先添加 traces 节点（优先级最高）
        for node in traces_nodes:
            service_name = node["id"]
            merged[service_name] = node.copy()

        # 2. 合并 logs 节点
        for node in logs_nodes:
            service_name = node["id"]
            if service_name in merged:
                # 合并 metrics
                existing = merged[service_name]
                logs_metrics = node.get("metrics", {})

                # 更新或添加 metrics
                for key, value in logs_metrics.items():
                    if key not in existing["metrics"]:
                        existing["metrics"][key] = value
            else:
                merged[service_name] = node.copy()

        # 3. 合并 metrics 节点（主要用于验证）
        for node in metrics_nodes:
            service_name = node["id"]
            if service_name in merged:
                # 添加 metrics 相关信息
                existing = merged[service_name]
                metrics_data = node.get("metrics", {})

                # 合并数据源标记
                data_sources = existing.get("metrics", {}).get("data_sources", [])
                if "traces" not in data_sources:
                    data_sources.append("traces")
                if "logs" not in data_sources and service_name in [n["id"] for n in logs_nodes]:
                    data_sources.append("logs")
                if "metrics" not in data_sources:
                    data_sources.append("metrics")

                existing["metrics"]["data_sources"] = data_sources
            else:
                merged[service_name] = node.copy()

        return list(merged.values())

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
        merged = {}  # {(source, target): merged_edge}

        # 1. 添加 traces 边（优先级最高）
        for edge in traces_edges:
            key = (edge["source"], edge["target"])
            merged[key] = edge.copy()

        # 2. 合并 logs 边
        for edge in logs_edges:
            key = (edge["source"], edge["target"])
            if key in merged:
                # 边已存在，可能是 traces 的精确数据
                # 不需要合并，保留 traces 的数据
                pass
            else:
                # 新边，使用 logs 的数据
                merged[key] = edge.copy()

        # 3. 合并 metrics 边（如果有）
        for edge in metrics_edges:
            key = (edge["source"], edge["target"])
            if key in merged:
                # 提升 confidence
                existing = merged[key]
                existing_conf = existing.get("metrics", {}).get("confidence", 0)
                boost = 0.1  # metrics 验证加分
                existing["metrics"]["confidence"] = min(1.0, existing_conf + boost)
            else:
                merged[key] = edge.copy()

        return list(merged.values())

    def _is_service_pair_related(self, service1: str, service2: str) -> bool:
        """判断两个服务是否可能存在调用关系（启发式规则）"""
        # 规则1: frontend -> backend 模式
        if "frontend" in service1.lower() and "backend" in service2.lower():
            return True
        if "frontend" in service2.lower() and "backend" in service1.lower():
            return True

        # 规则2: 服务 -> 数据库模式
        db_keywords = ["database", "db", "mysql", "postgres", "mongodb", "clickhouse"]
        if any(keyword in service2.lower() for keyword in db_keywords):
            return True

        # 规则3: 服务 -> 缓存模式
        cache_keywords = ["cache", "redis", "memcached"]
        if any(keyword in service2.lower() for keyword in cache_keywords):
            return True

        # 规则4: registry 常被调用
        if "registry" in service2.lower() and "registry" not in service1.lower():
            return True

        return False

    def _should_call(self, service1: str, service2: str) -> bool:
        """判断 service1 是否应该调用 service2"""
        # frontend/backend 通常调用其他服务
        if "frontend" in service1.lower() or "backend" in service1.lower():
            return True

        # 数据库/缓存通常不主动调用其他服务
        db_keywords = ["database", "db", "mysql", "postgres", "mongodb", "redis", "cache"]
        if any(keyword in service1.lower() for keyword in db_keywords):
            return False

        # registry 通常不主动调用
        if "registry" in service1.lower():
            return False

        return True

    def _get_relation_reason(self, caller: str, callee: str) -> str:
        """获取调用关系的理由"""
        reasons = []

        if "frontend" in caller.lower():
            reasons.append("frontend_pattern")
        elif "backend" in caller.lower():
            reasons.append("backend_pattern")

        db_keywords = ["database", "db", "mysql", "postgres", "redis", "cache"]
        if any(keyword in callee.lower() for keyword in db_keywords):
            reasons.append("data_access_pattern")

        if "registry" in callee.lower():
            reasons.append("image_pull_pattern")

        return ", ".join(reasons) if reasons else "heuristic_pattern"

    def _get_data_sources(
        self,
        traces_data: Dict,
        logs_data: Dict,
        metrics_data: Dict
    ) -> List[str]:
        """获取实际使用的数据源列表"""
        sources = []

        if traces_data.get("nodes") or traces_data.get("edges"):
            sources.append("traces")

        if logs_data.get("nodes") or logs_data.get("edges"):
            sources.append("logs")

        if metrics_data.get("nodes") or metrics_data.get("edges"):
            sources.append("metrics")

        return sources


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
