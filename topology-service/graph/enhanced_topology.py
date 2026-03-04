"""
Logoscope 增强服务拓扑构建器

核心特性：
1. 多模态数据融合（traces + logs + metrics）
2. 不强依赖trace ID，支持时间戳关联
3. 置信度加权，可手动调整
4. 成为项目亮点

Date: 2026-02-11
"""

import logging
import os
from typing import Dict, List, Any, Set, Tuple, Optional
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import json
import re

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


class EnhancedTopologyBuilder:
    """
    增强服务拓扑构建器

    核心思想：
    1. 多数据源融合：traces (1.0) + logs (0.5) + metrics (0.3)
    2. 时间关联：当traces不可用时，使用时间戳窗口关联日志
    3. 启发式规则：服务命名模式、调用模式推断
    4. 可调整性：支持手动添加/删除节点和边
    """

    def __init__(self, storage_adapter):
        """
        初始化增强拓扑构建器

        Args:
            storage_adapter: StorageAdapter 实例
        """
        self.storage = storage_adapter

        # 数据源权重
        self.WEIGHT_TRACES = 1.0      # 最可靠
        self.WEIGHT_LOGS_CORRELATED = 0.6  # 时间关联的日志
        self.WEIGHT_LOGS_HEURISTIC = 0.3  # 启发式推断
        self.WEIGHT_METRICS = 0.3     # 指标验证
        self.WEIGHT_MANUAL = 1.0      # 手动配置，最高优先级

        # 时间关联窗口（秒）
        self.CORRELATION_WINDOW = 5  # 5秒内的日志视为相关
        self.TRACES_SCAN_LIMIT = _read_positive_int_env("ENHANCED_TOPOLOGY_TRACES_SCAN_LIMIT", 200000)
        self.LOGS_SCAN_LIMIT = _read_positive_int_env("ENHANCED_TOPOLOGY_LOGS_SCAN_LIMIT", 300000)
        self.METRICS_SCAN_LIMIT = _read_positive_int_env("ENHANCED_TOPOLOGY_METRICS_SCAN_LIMIT", 100000)

        # 图结构
        self.nodes = {}  # {node_id: node_data}
        self.edges = {}  # {(source, target): edge_data}

        # 手动配置的节点和边
        self.manual_nodes = set()  # 手动添加的节点
        self.manual_edges = set()  # 手动添加的边
        self.suppressed_edges = set()  # 手动禁用的边
        self._metrics_namespace_column_exists_cache: Optional[bool] = None

    @staticmethod
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

    @staticmethod
    def _escape_sql_literal(value: str) -> str:
        """转义 SQL 字符串字面量中的单引号。"""
        return str(value).replace("'", "''")

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

    def build_topology(
        self,
        time_window: str = "1 HOUR",
        namespace: str = None,
        confidence_threshold: float = 0.3,
        enable_time_correlation: bool = True,
        enable_heuristics: bool = True
    ) -> Dict[str, Any]:
        """
        构建增强的服务拓扑图

        Args:
            time_window: 时间窗口
            namespace: 命名空间过滤
            confidence_threshold: 置信度阈值
            enable_time_correlation: 是否启用时间戳关联
            enable_heuristics: 是否启用启发式规则

        Returns:
            拓扑图数据
        """
        try:
            safe_time_window = self._sanitize_interval(time_window, default_value="1 HOUR")
            logger.info(
                f"Building enhanced topology: time_window={safe_time_window}, "
                f"enable_time_correlation={enable_time_correlation}"
            )

            # 1. 从多个数据源收集数据
            traces_data = self._get_traces_topology(safe_time_window, namespace)
            logs_data = self._get_logs_topology(safe_time_window, namespace, enable_time_correlation)
            metrics_data = self._get_metrics_topology(safe_time_window, namespace)

            # 2. 合并节点
            merged_nodes = self._merge_nodes(
                traces_data.get("nodes", []),
                logs_data.get("nodes", []),
                metrics_data.get("nodes", [])
            )

            # 3. 合并边并计算置信度
            merged_edges = self._merge_edges_with_confidence(
                traces_data.get("edges", []),
                logs_data.get("edges", []),
                metrics_data.get("edges", [])
            )

            # 4. 应用手动配置
            merged_nodes, merged_edges = self._apply_manual_configurations(
                merged_nodes, merged_edges
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

            metadata = {
                "data_sources": self._get_data_sources(traces_data, logs_data, metrics_data),
                "time_window": safe_time_window,
                "namespace": namespace,
                "node_count": len(merged_nodes),
                "edge_count": len(filtered_edges),
                "avg_confidence": round(avg_confidence, 2),
                "manual_nodes": len(self.manual_nodes),
                "manual_edges": len(self.manual_edges),
                "suppressed_edges": len(self.suppressed_edges),
                "correlation_enabled": enable_time_correlation,
                "heuristics_enabled": enable_heuristics,
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
            logger.error(f"Error building enhanced topology: {e}")
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

        使用 parent_span_id 构建调用链
        """
        try:
            safe_time_window = self._sanitize_interval(time_window, default_value="1 HOUR")
            if not self.storage.ch_client:
                return {"nodes": [], "edges": []}

            prewhere_clause = f"PREWHERE timestamp > now() - INTERVAL {safe_time_window}"
            # namespace 列不存在于 traces 表，从 attributes_json 中提取

            # 查询 span 调用链 - 使用正确的列名
            query = f"""
            SELECT
                trace_id,
                span_id,
                parent_span_id,
                service_name,
                operation_name,
                status,
                timestamp,
                attributes_json
            FROM logs.traces
            {prewhere_clause}
            ORDER BY timestamp DESC
            LIMIT {int(self.TRACES_SCAN_LIMIT)}
            """

            result = self.storage.execute_query(query)

            # 构建调用关系
            nodes = {}
            edges = defaultdict(lambda: {
                "call_count": 0,
                "total_duration": 0,
                "error_count": 0,
                "data_sources": set()
            })

            # 按 trace_id 分组
            traces_by_id = defaultdict(list)
            for row in result:
                if len(row) >= 7:
                    trace_id, span_id, parent_span_id, service_name, operation_name, status, timestamp, attributes_json = row
                    # 从 attributes_json 中提取 duration_ms（如果存在）
                    duration_ms = 0
                    try:
                        if attributes_json:
                            import json
                            attrs = json.loads(attributes_json)
                            duration_ms = attrs.get('duration_ms', 0) or attrs.get('duration', 0) or 0
                    except:
                        pass
                    traces_by_id[trace_id].append({
                        "span_id": span_id,
                        "parent_span_id": parent_span_id,
                        "service_name": service_name,
                        "operation_name": operation_name,
                        "duration_ms": duration_ms,
                        "status": status,
                        "timestamp": timestamp
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
                                "span_count": 0,
                                "trace_count": 0,
                                "avg_duration": 0,
                                "error_count": 0,
                                "data_source": "traces",
                                "confidence": 1.0
                            }
                        }

                    nodes[service_name]["metrics"]["span_count"] += 1
                    nodes[service_name]["metrics"]["avg_duration"] += span["duration_ms"]
                    if span["status"].lower() in ["error", "failed"]:
                        nodes[service_name]["metrics"]["error_count"] += 1

                    # 如果有 parent_span_id，找到父服务
                    parent_span_id = span.get("parent_span_id")
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
                                    "span_count": 0,
                                    "trace_count": 0,
                                    "avg_duration": 0,
                                    "error_count": 0,
                                    "data_source": "traces",
                                    "confidence": 1.0
                                }
                            }
                            nodes[parent_service]["metrics"]["span_count"] += 1

                        # 添加边
                        edge_key = (parent_service, service_name)
                        edges[edge_key]["call_count"] += 1
                        edges[edge_key]["total_duration"] += span["duration_ms"]
                        if span["status"].lower() in ["error", "failed"]:
                            edges[edge_key]["error_count"] += 1
                        edges[edge_key]["data_sources"].add("traces")

            # 计算每个 trace 的根节点
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
                        "data_sources": list(data["data_sources"]),
                        "confidence": 1.0,  # traces 数据最可靠
                        "reason": "trace_chain"
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
        enable_time_correlation: bool = True
    ) -> Dict[str, Any]:
        """
        从 logs 表获取服务节点和时间关联关系

        核心亮点：不依赖 trace_id，使用时间戳关联
        """
        try:
            safe_time_window = self._sanitize_interval(time_window, default_value="1 HOUR")
            if not self.storage.ch_client:
                return {"nodes": [], "edges": []}

            prewhere_conditions = [f"timestamp > now() - INTERVAL {safe_time_window}"]
            if namespace:
                prewhere_conditions.append(f"namespace = '{self._escape_sql_literal(namespace)}'")
            prewhere_clause = "PREWHERE " + " AND ".join(prewhere_conditions)

            # 查询日志和trace_id
            query = f"""
            SELECT
                service_name,
                pod_name,
                namespace,
                timestamp,
                level,
                trace_id,
                span_id,
                COUNT(*) as log_count
            FROM logs.logs
            {prewhere_clause}
            GROUP BY service_name, pod_name, namespace, timestamp, level, trace_id, span_id
            ORDER BY timestamp DESC
            LIMIT {int(self.LOGS_SCAN_LIMIT)}
            """

            result = self.storage.execute_query(query)

            # 构建节点
            nodes = {}
            service_logs = defaultdict(list)  # {service_name: [log_entries]}

            for row in result:
                if len(row) >= 7:
                    service_name, pod_name, ns, timestamp, level, trace_id, span_id, log_count = row
                    service_logs[service_name].append({
                        "pod_name": pod_name,
                        "namespace": ns,
                        "timestamp": timestamp,
                        "level": level,
                        "trace_id": trace_id,
                        "span_id": span_id,
                        "log_count": log_count
                    })

                    # 添加节点
                    if service_name not in nodes:
                        nodes[service_name] = {
                            "id": service_name,
                            "label": service_name,
                            "type": "service",
                            "name": service_name,
                            "metrics": {
                                "log_count": 0,
                                "error_count": 0,
                                "pod_count": 0,
                                "data_source": "logs",
                                "confidence": 0.5,
                                "has_traces": False
                            }
                        }

                    nodes[service_name]["metrics"]["log_count"] += log_count
                    if level and level.lower() in ["error", "fatal", "critical"]:
                        nodes[service_name]["metrics"]["error_count"] += log_count

                    if trace_id:
                        nodes[service_name]["metrics"]["has_traces"] = True
                        nodes[service_name]["metrics"]["confidence"] = 0.6  # 有trace_id的日志置信度更高

                    # 统计pod数量
                    if pod_name:
                        nodes[service_name]["metrics"]["pod_count"] = len(set(
                            log["pod_name"] for log in service_logs[service_name]
                        ))

            # 时间关联算法（核心亮点）
            edges = []
            if enable_time_correlation:
                edges = self._build_time_correlated_edges(service_logs)

            # 启发式规则（补充）
            if enable_heuristics:
                heuristic_edges = self._build_heuristic_edges(list(nodes.keys()))
                edges.extend(heuristic_edges)

            return {
                "nodes": list(nodes.values()),
                "edges": edges
            }

        except Exception as e:
            logger.error(f"Error getting logs topology: {e}")
            return {"nodes": [], "edges": []}

    def _build_time_correlated_edges(
        self,
        service_logs: Dict[str, List[Dict]]
    ) -> List[Dict]:
        """
        基于时间戳的边关联算法

        核心思想：
        1. 查找同一时间窗口内的服务对
        2. 分析服务间的时间先后关系
        3. 使用启发式规则推断调用方向
        """
        try:
            edges_dict = defaultdict(lambda: {
                "occurrence_count": 0,
                "avg_time_diff": 0,
                "time_diffs": [],
                "examples": []
            })

            service_names = list(service_logs.keys())

            # 遍历所有服务对
            for i, source_svc in enumerate(service_names):
                for target_svc in service_names[i+1:]:
                    # 检查时间相关性
                    correlations = self._find_time_correlations(
                        service_logs[source_svc],
                        service_logs[target_svc]
                    )

                    if correlations["significant_count"] > 0:
                        edge_key = (source_svc, target_svc)
                        edges_dict[edge_key]["occurrence_count"] = correlations["significant_count"]
                        edges_dict[edge_key]["avg_time_diff"] = correlations["avg_time_diff"]
                        edges_dict[edge_key]["time_diffs"] = correlations["time_diffs"]
                        edges_dict[edge_key]["examples"] = correlations["examples"][:5]  # 最多5个例子

            # 构建边列表
            edge_list = []
            for (source, target), data in edges_dict.items():
                if data["occurrence_count"] >= 2:  # 至少出现2次
                    # 计算置信度
                    confidence = min(0.6, data["occurrence_count"] * 0.1)

                    edge_list.append({
                        "id": f"{source}-{target}",
                        "source": source,
                        "target": target,
                        "label": "correlated",
                        "type": "calls",
                        "metrics": {
                            "occurrence_count": data["occurrence_count"],
                            "avg_time_diff_ms": round(data["avg_time_diff"] * 1000, 2),
                            "data_sources": ["logs_time_correlation"],
                            "confidence": confidence,
                            "reason": "time_correlation",
                            "examples": data["examples"]
                        }
                    })

            logger.info(f"Time correlation found {len(edge_list)} edges")
            return edge_list

        except Exception as e:
            logger.error(f"Error building time-correlated edges: {e}")
            return []

    def _find_time_correlations(
        self,
        source_logs: List[Dict],
        target_logs: List[Dict],
        window_seconds: int = 5
    ) -> Dict[str, Any]:
        """
        查找两组日志之间的时间相关性

        Args:
            source_logs: 源服务日志
            target_logs: 目标服务日志
            window_seconds: 时间窗口（秒）

        Returns:
            相关性统计
        """
        correlations = {
            "significant_count": 0,
            "avg_time_diff": 0,
            "time_diffs": [],
            "examples": []
        }

        # 遍历源日志
        for src_log in source_logs[:100]:  # 限制处理数量
            src_time = src_log.get("timestamp")
            if not src_time:
                continue

            # 在目标日志中查找时间窗口内的日志
            for tgt_log in target_logs[:200]:
                tgt_time = tgt_log.get("timestamp")
                if not tgt_time:
                    continue

                # 计算时间差（处理时区问题）
                try:
                    st = src_time
                    tt = tgt_time
                    if hasattr(st, 'tzinfo') and hasattr(tt, 'tzinfo'):
                        if st.tzinfo is None and tt.tzinfo is not None:
                            st = st.replace(tzinfo=timezone.utc)
                        elif st.tzinfo is not None and tt.tzinfo is None:
                            tt = tt.replace(tzinfo=timezone.utc)
                    time_diff = (tt - st).total_seconds()
                except TypeError:
                    continue

                # 如果在时间窗口内且目标在源之后
                if 0 < time_diff <= window_seconds:
                    correlations["significant_count"] += 1
                    correlations["time_diffs"].append(time_diff)
                    correlations["avg_time_diff"] += time_diff

                    # 收集示例
                    if len(correlations["examples"]) < 5:
                        correlations["examples"].append({
                            "source_time": str(src_time),
                            "target_time": str(tgt_time),
                            "time_diff_ms": round(time_diff * 1000, 2),
                            "source_pod": src_log.get("pod_name"),
                            "target_pod": tgt_log.get("pod_name")
                        })
                    break  # 找到一个匹配就停止

        # 计算平均时间差
        if correlations["significant_count"] > 0:
            correlations["avg_time_diff"] = (
                correlations["avg_time_diff"] / correlations["significant_count"]
            )

        return correlations

    def _build_heuristic_edges(
        self,
        service_names: List[str]
    ) -> List[Dict]:
        """
        基于启发式规则构建边

        规则：
        1. frontend/backend -> 其他服务
        2. 服务 -> 数据库/缓存
        3. registry 常被调用
        """
        edges = []

        for i, source in enumerate(service_names):
            for target in service_names[i+1:]:
                if self._is_service_pair_related(source, target):
                    # 推断调用方向
                    caller, callee = self._infer_call_direction(source, target)

                    edges.append({
                        "id": f"{caller}-{callee}",
                        "source": caller,
                        "target": callee,
                        "label": "potential-calls",
                        "type": "calls",
                        "metrics": {
                            "data_sources": ["logs_heuristic"],
                            "confidence": 0.3,
                            "reason": self._get_relation_reason(caller, callee)
                        }
                    })

        return edges

    def _is_service_pair_related(self, service1: str, service2: str) -> bool:
        """判断两个服务是否可能存在调用关系"""
        # 规则1: frontend -> backend 模式
        if ("frontend" in service1.lower() and "backend" in service2.lower()) or \
           ("frontend" in service2.lower() and "backend" in service1.lower()):
            return True

        # 规则2: 服务 -> 数据库模式
        db_keywords = ["database", "db", "mysql", "postgres", "mongodb", "clickhouse", "redis", "cache"]
        if any(keyword in service2.lower() for keyword in db_keywords):
            return True

        # 规则3: otel-collector/log-generator -> 其他服务
        collector_keywords = ["collector", "generator"]
        if any(keyword in service1.lower() for keyword in collector_keywords):
            return True

        # 规则4: registry 常被调用
        if "registry" in service2.lower() and "registry" not in service1.lower():
            return True

        return False

    def _infer_call_direction(self, service1: str, service2: str) -> Tuple[str, str]:
        """推断调用方向：service1 是否调用 service2"""
        # frontend/backend 通常调用其他服务
        if "frontend" in service1.lower() or "backend" in service1.lower():
            return service1, service2

        # collector/Generator 发送数据
        collector_keywords = ["collector", "generator"]
        if any(keyword in service1.lower() for keyword in collector_keywords):
            return service1, service2

        # 数据库/缓存通常不主动调用
        db_keywords = ["database", "db", "mysql", "postgres", "redis", "cache"]
        if any(keyword in service1.lower() for keyword in db_keywords):
            return service2, service1  # 反向

        # registry 通常不主动调用
        if "registry" in service1.lower():
            return service2, service1

        return service1, service2

    def _get_relation_reason(self, caller: str, callee: str) -> str:
        """获取调用关系的理由"""
        reasons = []

        if "frontend" in caller.lower():
            reasons.append("frontend_pattern")
        elif "backend" in caller.lower() or "collector" in caller.lower():
            reasons.append("data_collector_pattern")

        db_keywords = ["database", "db", "mysql", "postgres", "redis", "cache", "clickhouse"]
        if any(keyword in callee.lower() for keyword in db_keywords):
            reasons.append("data_access_pattern")

        if "registry" in callee.lower():
            reasons.append("image_pull_pattern")

        return ", ".join(reasons) if reasons else "heuristic_pattern"

    def _get_metrics_topology(
        self,
        time_window: str,
        namespace: str = None
    ) -> Dict[str, Any]:
        """
        从 metrics 表获取服务验证信息
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

            prewhere_clause = f"PREWHERE timestamp > now() - INTERVAL {safe_time_window}"
            where_clause = ""
            if namespace:
                escaped_namespace = str(namespace).replace("'", "''")
                where_clause = f"WHERE {namespace_expr} = '{escaped_namespace}'"

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

            return {
                "nodes": nodes,
                "edges": []  # metrics 通常不包含直接调用关系
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
        """
        merged = {}  # {service_name: merged_node}

        # 1. 先添加 traces 节点（优先级最高）
        for node in traces_nodes:
            service_name = node["id"]
            merged[service_name] = node.copy()
            merged[service_name]["metrics"]["data_sources"] = ["traces"]

        # 2. 合并 logs 节点
        for node in logs_nodes:
            service_name = node["id"]
            if service_name in merged:
                # 合并 metrics
                existing = merged[service_name]
                logs_metrics = node.get("metrics", {})

                for key, value in logs_metrics.items():
                    if key == "data_sources":
                        existing["metrics"]["data_sources"].append("logs")
                    elif key not in existing["metrics"]:
                        existing["metrics"][key] = value

                # 提升置信度（有logs数据）
                if "logs" not in existing["metrics"]["data_sources"]:
                    existing["metrics"]["data_sources"].append("logs")
                    existing["metrics"]["confidence"] = max(
                        existing["metrics"].get("confidence", 0),
                        0.5
                    )
            else:
                merged[service_name] = node.copy()
                merged[service_name]["metrics"]["data_sources"] = ["logs"]

        # 3. 合并 metrics 节点（主要用于验证）
        for node in metrics_nodes:
            service_name = node["id"]
            if service_name in merged:
                # 添加 metrics 数据源标记
                existing = merged[service_name]
                if "metrics" not in existing["metrics"]["data_sources"]:
                    existing["metrics"]["data_sources"].append("metrics")
            else:
                merged[service_name] = node.copy()

        return list(merged.values())

    def _merge_edges_with_confidence(
        self,
        traces_edges: List[Dict],
        logs_edges: List[Dict],
        metrics_edges: List[Dict]
    ) -> List[Dict]:
        """
        合并边并计算加权置信度
        """
        merged = {}  # {(source, target): merged_edge}

        # 1. 添加 traces 边（权重 1.0）
        for edge in traces_edges:
            key = (edge["source"], edge["target"])
            merged[key] = edge.copy()
            merged[key]["metrics"]["confidence"] = self.WEIGHT_TRACES

        # 2. 合并 logs 边
        for edge in logs_edges:
            key = (edge["source"], edge["target"])
            if key in merged:
                # 边已存在，可能提升置信度
                existing = merged[key]
                existing_conf = existing["metrics"]["confidence"]

                # 如果logs边提供了新证据，提升置信度
                if "logs" not in existing.get("metrics", {}).get("data_sources", []):
                    existing["metrics"]["data_sources"].append("logs")
                    existing["metrics"]["confidence"] = min(1.0, existing_conf + 0.2)
            else:
                # 新边，使用logs置信度
                merged[key] = edge.copy()
                merged[key]["metrics"]["confidence"] = self.WEIGHT_LOGS_HEURISTIC

        # 3. 合并 metrics 边（验证提升）
        for edge in metrics_edges:
            key = (edge["source"], edge["target"])
            if key in merged:
                # 提升置信度
                existing = merged[key]
                existing_conf = existing["metrics"]["confidence"]
                existing["metrics"]["confidence"] = min(1.0, existing_conf + 0.1)
            else:
                merged[key] = edge.copy()

        return list(merged.values())

    def _apply_manual_configurations(
        self,
        nodes: List[Dict],
        edges: List[Dict]
    ) -> Tuple[List[Dict], List[Dict]]:
        """
        应用手动配置（节点和边的增删改）
        """
        # 添加手动节点
        manual_node_list = [
            {"id": node_id, "label": node_id, "type": "service", "name": node_id,
             "metrics": {"data_source": "manual", "confidence": 1.0}}
            for node_id in self.manual_nodes
            if not any(n["id"] == node_id for n in nodes)
        ]
        nodes.extend(manual_node_list)

        # 过滤被禁用的边
        filtered_edges = [
            edge for edge in edges
            if (edge["source"], edge["target"]) not in self.suppressed_edges
        ]

        # 添加手动边
        manual_edge_list = [
            {"id": f"{s}-{t}", "source": s, "target": t, "label": "manual-calls",
             "type": "calls", "metrics": {"data_source": "manual", "confidence": 1.0}}
            for (s, t) in self.manual_edges
            if (s, t) not in [(e["source"], e["target"]) for e in filtered_edges]
        ]
        filtered_edges.extend(manual_edge_list)

        return nodes, filtered_edges

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

        if metrics_data.get("nodes"):
            sources.append("metrics")

        if "manual" in [self.manual_nodes, self.manual_edges]:
            sources.append("manual")

        return sources

    # ==================== 手动配置API ====================

    def add_manual_node(self, node_id: str, node_type: str = "service") -> Dict[str, Any]:
        """
        手动添加节点

        Args:
            node_id: 节点ID（通常是服务名）
            node_type: 节点类型（service, database, cache等）

        Returns:
            操作结果
        """
        self.manual_nodes.add(node_id)
        return {
            "status": "success",
            "action": "add_node",
            "node_id": node_id,
            "node_type": node_type
        }

    def remove_manual_node(self, node_id: str) -> Dict[str, Any]:
        """
        手动移除节点

        Args:
            node_id: 节点ID

        Returns:
            操作结果
        """
        if node_id in self.manual_nodes:
            self.manual_nodes.discard(node_id)
            # 同时移除相关边
            edges_to_remove = [
                (s, t) for s, t in self.manual_edges
                if s == node_id or t == node_id
            ]
            for edge in edges_to_remove:
                self.manual_edges.discard(edge)

            return {
                "status": "success",
                "action": "remove_node",
                "node_id": node_id,
                "removed_edges": len(edges_to_remove)
            }
        else:
            return {
                "status": "error",
                "action": "remove_node",
                "node_id": node_id,
                "error": "node_not_found"
            }

    def add_manual_edge(
        self,
        source: str,
        target: str,
        edge_type: str = "calls",
        confidence: float = 1.0,
        reason: str = "manual"
    ) -> Dict[str, Any]:
        """
        手动添加边

        Args:
            source: 源服务
            target: 目标服务
            edge_type: 边类型
            confidence: 置信度
            reason: 原因说明

        Returns:
            操作结果
        """
        edge_key = (source, target)
        self.manual_edges.add(edge_key)

        return {
            "status": "success",
            "action": "add_edge",
            "source": source,
            "target": target,
            "edge_type": edge_type,
            "confidence": confidence
        }

    def remove_manual_edge(self, source: str, target: str) -> Dict[str, Any]:
        """
        手动移除边

        Args:
            source: 源服务
            target: 目标服务

        Returns:
            操作结果
        """
        edge_key = (source, target)
        if edge_key in self.manual_edges:
            self.manual_edges.discard(edge_key)
            return {
                "status": "success",
                "action": "remove_edge",
                "source": source,
                "target": target
            }
        else:
            return {
                "status": "error",
                "action": "remove_edge",
                "source": source,
                "target": target,
                "error": "edge_not_found"
            }

    def suppress_edge(self, source: str, target: str) -> Dict[str, Any]:
        """
        禁用某条边（不删除，只是临时隐藏）

        Args:
            source: 源服务
            target: 目标服务

        Returns:
            操作结果
        """
        edge_key = (source, target)
        self.suppressed_edges.add(edge_key)

        return {
            "status": "success",
            "action": "suppress_edge",
            "source": source,
            "target": target
        }

    def unsuppress_edge(self, source: str, target: str) -> Dict[str, Any]:
        """
        取消禁用边

        Args:
            source: 源服务
            target: 目标服务

        Returns:
            操作结果
        """
        edge_key = (source, target)
        if edge_key in self.suppressed_edges:
            self.suppressed_edges.discard(edge_key)
            return {
                "status": "success",
                "action": "unsuppress_edge",
                "source": source,
                "target": target
            }
        else:
            return {
                "status": "error",
                "action": "unsuppress_edge",
                "source": source,
                "target": target,
                "error": "edge_not_suppressed"
            }

    def get_manual_configurations(self) -> Dict[str, Any]:
        """
        获取所有手动配置

        Returns:
            手动配置列表
        """
        return {
            "manual_nodes": list(self.manual_nodes),
            "manual_edges": [
                {"source": s, "target": t}
                for s, t in self.manual_edges
            ],
            "suppressed_edges": [
                {"source": s, "target": t}
                for s, t in self.suppressed_edges
            ]
        }

    def clear_manual_configurations(self) -> Dict[str, Any]:
        """
        清除所有手动配置
        """
        node_count = len(self.manual_nodes)
        edge_count = len(self.manual_edges)
        suppressed_count = len(self.suppressed_edges)

        self.manual_nodes.clear()
        self.manual_edges.clear()
        self.suppressed_edges.clear()

        return {
            "status": "success",
            "action": "clear_all",
            "cleared_nodes": node_count,
            "cleared_edges": edge_count,
            "cleared_suppressed": suppressed_count
        }


# 全局实例（延迟初始化）
_enhanced_builder = None


def get_enhanced_topology_builder(storage_adapter) -> EnhancedTopologyBuilder:
    """
    获取增强拓扑构建器实例

    Args:
        storage_adapter: StorageAdapter 实例

    Returns:
        EnhancedTopologyBuilder 实例
    """
    global _enhanced_builder
    if _enhanced_builder is None:
        _enhanced_builder = EnhancedTopologyBuilder(storage_adapter)
    return _enhanced_builder
