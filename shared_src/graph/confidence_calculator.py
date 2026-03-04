"""
改进的拓扑置信度计算模块

特性：
1. 时间衰减因子 - 旧数据权重随时间降低
2. 错误率权重 - 高错误率关系降低置信度
3. 多数据源融合优化 - 综合考虑数据源数量和质量

Date: 2026-02-09
"""

import logging
from typing import Dict, List, Any, Optional
from datetime import datetime, timedelta, timezone
from collections import defaultdict
import math

logger = logging.getLogger(__name__)


class ConfidenceCalculator:
    """
    改进的置信度计算器

    计算规则：
    1. **时间衰减**：数据越旧，权重越低
       - 最近 1 小时：100% 权重
       - 1-6 小时：80% 权重
       - 6-24 小时：50% 权重
       - 24+ 小时：20% 权重

    2. **错误率惩罚**：高错误率降低置信度
       - 错误率 0%：无惩罚
       - 错误率 1-5%：轻微惩罚 (-5%)
       - 错误率 5-10%：中度惩罚 (-15%)
       - 错误率 10%+：重度惩罚 (-30%)

    3. **数据源融合**：多源验证提升置信度
       - 单一数据源：基础置信度
       - 两个数据源：+20%
       - 三个数据源：+35%
    """

    # 时间衰减阈值（小时）
    TIME_DECAY_THRESHOLDS = {
        1: 1.0,      # 1 小时内：100%
        6: 0.8,      # 1-6 小时：80%
        24: 0.5,     # 6-24 小时：50%
        'default': 0.2  # 24+ 小时：20%
    }

    # 错误率惩罚
    ERROR_RATE_PENALTIES = {
        (0.0, 0.01): 0.0,      # 0-1%：无惩罚
        (0.01, 0.05): 0.05,    # 1-5%：-5%
        (0.05, 0.10): 0.15,    # 5-10%：-15%
        (0.10, 1.0): 0.30      # 10%+：-30%
    }

    # 数据源融合加成
    MULTI_SOURCE_BOOST = {
        1: 0.0,    # 单一源：无加成
        2: 0.20,   # 两源：+20%
        3: 0.35    # 三源：+35%
    }

    # 数据源基础权重
    SOURCE_BASE_WEIGHTS = {
        'traces': 1.0,      # Traces 最可靠
        'logs': 0.4,        # Logs 中等
        'metrics': 0.3      # Metrics 辅助
    }

    def __init__(self, reference_time: Optional[datetime] = None):
        """
        初始化置信度计算器

        Args:
            reference_time: 参考时间（默认当前时间，使用 UTC 时区）
        """
        if reference_time:
            # 确保 reference_time 有时区信息
            if reference_time.tzinfo is None:
                self.reference_time = reference_time.replace(tzinfo=timezone.utc)
            else:
                self.reference_time = reference_time
        else:
            self.reference_time = datetime.now(timezone.utc)

    def calculate_edge_confidence(
        self,
        edge: Dict[str, Any],
        data_sources: List[str],
        timestamp: Optional[datetime] = None
    ) -> float:
        """
        计算边的置信度

        Args:
            edge: 边数据，包含 metrics
            data_sources: 数据源列表（如 ['traces', 'logs']）
            timestamp: 边的时间戳（用于时间衰减）

        Returns:
            float: 置信度 (0.0 - 1.0)
        """
        edge_metrics = edge.get("metrics", {})

        # 1. 获取基础置信度
        base_confidence = edge_metrics.get("confidence") or 0.5
        source_type = edge_metrics.get("data_source") or "unknown"

        # 如果指定了源类型，使用该源的基础权重
        if source_type in self.SOURCE_BASE_WEIGHTS:
            base_confidence = max(base_confidence, self.SOURCE_BASE_WEIGHTS[source_type])

        # 2. 应用时间衰减
        time_decay_factor = self._calculate_time_decay(timestamp or self.reference_time)
        confidence = base_confidence * time_decay_factor

        # 3. 应用错误率惩罚
        error_rate = edge_metrics.get("error_rate") or 0.0
        error_penalty = self._calculate_error_penalty(error_rate)
        confidence = confidence * (1.0 - error_penalty)

        # 4. 应用多源融合加成
        source_count = len(data_sources)
        multi_source_boost = self.MULTI_SOURCE_BOOST.get(source_count, 0.0)
        confidence = confidence + multi_source_boost

        # 5. 考虑调用次数（调用越多越可靠）
        call_count = edge_metrics.get("call_count") or 0
        if call_count > 0:
            # 使用对数缩放，避免过多调用导致置信度爆炸
            call_boost = min(0.15, math.log10(call_count + 1) * 0.05)
            confidence = confidence + call_boost

        # 确保在 [0.0, 1.0] 范围内
        return max(0.0, min(1.0, confidence))

    def calculate_node_confidence(
        self,
        node: Dict[str, Any],
        data_sources: List[str],
        last_seen: Optional[datetime] = None
    ) -> float:
        """
        计算节点的置信度

        节点置信度基于：
- 数据活跃度（log_count, trace_count）
- 最近活动时间
- 数据源数量

        Args:
            node: 节点数据，包含 metrics
            data_sources: 数据源列表
            last_seen: 最后活动时间

        Returns:
            float: 置信度 (0.0 - 1.0)
        """
        node_metrics = node.get("metrics", {})

        # 基础置信度
        base_confidence = 0.5

        # 1. 活跃度加分
        log_count = node_metrics.get("log_count") or 0
        trace_count = node_metrics.get("trace_count") or 0

        # 使用对数缩放
        activity_score = (
            math.log10(log_count + 1) * 0.15 +
            math.log10(trace_count + 1) * 0.25
        )
        base_confidence = min(0.8, base_confidence + activity_score)

        # 2. 时间衰减
        logger.debug(f"Calculating time decay: last_seen={last_seen}, reference_time={self.reference_time}")
        if last_seen:
            logger.debug(f"last_seen type: {type(last_seen)}, tzinfo: {last_seen.tzinfo}")
            logger.debug(f"reference_time type: {type(self.reference_time)}, tzinfo: {self.reference_time.tzinfo}")
        time_decay = self._calculate_time_decay(last_seen or self.reference_time)
        confidence = base_confidence * time_decay

        # 3. 错误率惩罚
        error_count = node_metrics.get("error_count") or 0
        total_count = max(1, log_count + trace_count)
        error_rate = error_count / total_count

        error_penalty = self._calculate_error_penalty(error_rate)
        confidence = confidence * (1.0 - error_penalty)

        # 4. 多源加成
        source_count = len(data_sources)
        multi_source_boost = self.MULTI_SOURCE_BOOST.get(source_count, 0.0)
        confidence = confidence + multi_source_boost

        return max(0.0, min(1.0, confidence))

    def _calculate_time_decay(self, timestamp: datetime) -> float:
        """
        计算时间衰减因子

        Args:
            timestamp: 数据时间戳

        Returns:
            float: 衰减因子 (0.0 - 1.0)
        """
        if not timestamp:
            return 1.0

        ref_time = self.reference_time
        ts = timestamp

        logger.debug(f"_calculate_time_decay: ref_time={ref_time}, ts={ts}")
        logger.debug(f"_calculate_time_decay: ref_time.tzinfo={ref_time.tzinfo}, ts.tzinfo={ts.tzinfo}")

        try:
            if ref_time.tzinfo is None and ts.tzinfo is not None:
                logger.debug(f"Setting ref_time tzinfo to UTC")
                ref_time = ref_time.replace(tzinfo=timezone.utc)
            elif ref_time.tzinfo is not None and ts.tzinfo is None:
                logger.debug(f"Setting ts tzinfo to UTC")
                ts = ts.replace(tzinfo=timezone.utc)

            logger.debug(f"After adjustment: ref_time.tzinfo={ref_time.tzinfo}, ts.tzinfo={ts.tzinfo}")
            time_diff = (ref_time - ts).total_seconds() / 3600
            logger.debug(f"time_diff={time_diff} hours")
        except (TypeError, AttributeError) as e:
            logger.debug(f"Error in time decay calculation: {e}, ref_time={ref_time}, ts={ts}")
            return 1.0

        # 确保 time_diff 是有效数值
        if time_diff is None or not isinstance(time_diff, (int, float)):
            return 1.0

        # 查找对应的衰减因子（仅排序数值类型的键，跳过 'default'）
        for threshold, decay in sorted(
            (k, v) for k, v in self.TIME_DECAY_THRESHOLDS.items() if isinstance(k, (int, float))
        ):
            if time_diff <= threshold:
                return decay

        # 超过所有阈值，使用默认值
        return self.TIME_DECAY_THRESHOLDS['default']

    def _calculate_error_penalty(self, error_rate: float) -> float:
        """
        计算错误率惩罚

        Args:
            error_rate: 错误率 (0.0 - 1.0)

        Returns:
            float: 惩罚因子 (0.0 - 1.0)
        """
        for (min_rate, max_rate), penalty in self.ERROR_RATE_PENALTIES.items():
            if min_rate <= error_rate < max_rate:
                return penalty

        # 超过所有范围，使用最大惩罚
        return self.ERROR_RATE_PENALTIES[(0.10, 1.0)]

    def recalculate_topology_confidence(
        self,
        topology: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        重新计算整个拓扑的置信度

        Args:
            topology: 原始拓扑数据

        Returns:
            Dict: 更新后的拓扑数据
        """
        nodes = topology.get("nodes", [])
        edges = topology.get("edges", [])

        # 重新计算节点置信度
        for node in nodes:
            node_metrics = node.get("metrics", {})
            data_sources = node_metrics.get("data_sources", [])

            # 提取最后活动时间
            last_seen_str = node_metrics.get("last_seen")
            last_seen = None
            if last_seen_str:
                try:
                    # 处理多种时间格式
                    last_seen_str = str(last_seen_str)
                    if last_seen_str.endswith('Z'):
                        last_seen_str = last_seen_str[:-1] + '+00:00'
                    elif '+' not in last_seen_str and '-' not in last_seen_str[10:]:
                        # 没有 timezone 信息，添加 UTC
                        last_seen_str = last_seen_str + '+00:00'
                    
                    last_seen = datetime.fromisoformat(last_seen_str)
                    logger.debug(f"Parsed last_seen: {last_seen}, tzinfo: {last_seen.tzinfo}")
                except Exception as e:
                    logger.debug(f"Failed to parse last_seen '{last_seen_str}': {e}")
                    pass

            try:
                new_confidence = self.calculate_node_confidence(
                    node=node,
                    data_sources=data_sources,
                    last_seen=last_seen
                )
            except Exception as e:
                logger.error(f"Error calculating node confidence for {node.get('id')}: {e}")
                new_confidence = 0.5

            node_metrics["confidence"] = round(new_confidence, 3)
            node_metrics["confidence_details"] = {
                "error_rate": node_metrics.get("error_rate", 0),
                "data_sources": data_sources,
                "calculated_at": self.reference_time.isoformat() + "Z"
            }

        # 重新计算边置信度
        for edge in edges:
            edge_metrics = edge.get("metrics", {})

            # 确定数据源
            data_source = edge_metrics.get("data_source", "unknown")
            data_sources = [data_source] if data_source != "unknown" else []

            new_confidence = self.calculate_edge_confidence(
                edge=edge,
                data_sources=data_sources
            )

            edge_metrics["confidence"] = round(new_confidence, 3)
            edge_metrics["confidence_details"] = {
                "error_rate": edge_metrics.get("error_rate", 0),
                "call_count": edge_metrics.get("call_count", 0),
                "data_sources": data_sources,
                "calculated_at": self.reference_time.isoformat() + "Z"
            }

        # 重新计算平均置信度
        avg_confidence = (
            sum(e.get("metrics", {}).get("confidence", 0) for e in edges) / len(edges)
            if edges else 0
        )

        # 更新 metadata
        metadata = topology.get("metadata", {})
        metadata["avg_confidence"] = round(avg_confidence, 3)
        metadata["confidence_algorithm"] = "improved_v2"
        metadata["confidence_features"] = [
            "time_decay",
            "error_rate_penalty",
            "multi_source_boost",
            "call_count_boost"
        ]

        return {
            "nodes": nodes,
            "edges": edges,
            "metadata": metadata
        }


def get_confidence_calculator(reference_time: Optional[datetime] = None) -> ConfidenceCalculator:
    """
    获取置信度计算器实例

    Args:
        reference_time: 参考时间（默认当前时间）

    Returns:
        ConfidenceCalculator: 计算器实例
    """
    return ConfidenceCalculator(reference_time)
