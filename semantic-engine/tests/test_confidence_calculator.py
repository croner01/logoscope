"""
Graph Confidence Calculator 单元测试

测试 graph/confidence_calculator.py 的核心功能：
- 边置信度计算
- 节点置信度计算
- 时间衰减因子
- 错误率惩罚
- 多数据源融合
"""
import pytest
from datetime import datetime, timedelta
from math import isclose

from graph.confidence_calculator import (
    ConfidenceCalculator,
    get_confidence_calculator
)


class TestConfidenceCalculatorInit:
    """测试 ConfidenceCalculator 初始化"""

    def test_init_with_default_time(self):
        """测试默认时间初始化"""
        calculator = ConfidenceCalculator()
        assert calculator.reference_time is not None
        assert isinstance(calculator.reference_time, datetime)

    def test_init_with_custom_time(self):
        """测试自定义时间初始化"""
        custom_time = datetime(2026, 2, 9, 12, 0, 0)
        calculator = ConfidenceCalculator(reference_time=custom_time)
        assert calculator.reference_time == custom_time


class TestTimeDecay:
    """测试时间衰减计算"""

    @pytest.fixture
    def calculator(self):
        # 使用固定时间作为参考
        return ConfidenceCalculator(reference_time=datetime(2026, 2, 9, 12, 0, 0))

    def test_time_decay_within_1_hour(self, calculator):
        """测试1小时内的时间衰减（100%）"""
        recent_time = datetime(2026, 2, 9, 11, 30, 0)  # 30分钟前

        decay = calculator._calculate_time_decay(recent_time)

        assert decay == 1.0

    def test_time_decay_1_to_6_hours(self, calculator):
        """测试1-6小时的时间衰减（80%）"""
        time_3h_ago = datetime(2026, 2, 9, 9, 0, 0)  # 3小时前

        decay = calculator._calculate_time_decay(time_3h_ago)

        assert decay == 0.8

    def test_time_decay_6_to_24_hours(self, calculator):
        """测试6-24小时的时间衰减（50%）"""
        time_12h_ago = datetime(2026, 2, 9, 0, 0, 0)  # 12小时前

        decay = calculator._calculate_time_decay(time_12h_ago)

        assert decay == 0.5

    def test_time_decay_over_24_hours(self, calculator):
        """测试超过24小时的时间衰减（20%）"""
        time_48h_ago = datetime(2026, 2, 7, 12, 0, 0)  # 48小时前

        decay = calculator._calculate_time_decay(time_48h_ago)

        assert decay == 0.2

    def test_time_decay_with_none(self, calculator):
        """测试 None 时间戳（无衰减）"""
        decay = calculator._calculate_time_decay(None)

        assert decay == 1.0


class TestErrorPenalty:
    """测试错误率惩罚计算"""

    @pytest.fixture
    def calculator(self):
        return ConfidenceCalculator()

    def test_error_penalty_zero(self, calculator):
        """测试0%错误率（无惩罚）"""
        penalty = calculator._calculate_error_penalty(0.0)
        assert penalty == 0.0

    def test_error_penalty_low(self, calculator):
        """测试1-5%错误率（-5%）"""
        penalty = calculator._calculate_error_penalty(0.03)  # 3%
        assert penalty == 0.05

    def test_error_penalty_medium(self, calculator):
        """测试5-10%错误率（-15%）"""
        penalty = calculator._calculate_error_penalty(0.07)  # 7%
        assert penalty == 0.15

    def test_error_penalty_high(self, calculator):
        """测试10%+错误率（-30%）"""
        penalty = calculator._calculate_error_penalty(0.15)  # 15%
        assert penalty == 0.30

    def test_error_penalty_boundary_1_percent(self, calculator):
        """测试1%边界"""
        penalty = calculator._calculate_error_penalty(0.01)
        # 1% 正好在边界，会落入下一个区间（1-5%），轻微惩罚
        assert penalty == 0.05

    def test_error_penalty_boundary_5_percent(self, calculator):
        """测试5%边界"""
        penalty = calculator._calculate_error_penalty(0.05)
        # 5% 正好在边界，会落入下一个区间（5-10%），中度惩罚
        assert penalty == 0.15


class TestEdgeConfidence:
    """测试边置信度计算"""

    @pytest.fixture
    def calculator(self):
        return ConfidenceCalculator()

    def test_edge_confidence_basic(self, calculator):
        """测试基本边置信度"""
        edge = {
            "metrics": {
                "confidence": 0.7,
                "data_source": "traces"
            }
        }

        confidence = calculator.calculate_edge_confidence(
            edge=edge,
            data_sources=["traces"]
        )

        assert 0.0 <= confidence <= 1.0
        assert confidence >= 0.7  # traces 数据源应该提升置信度

    def test_edge_confidence_with_logs_source(self, calculator):
        """测试 logs 数据源的边置信度"""
        edge = {
            "metrics": {
                "confidence": 0.5,
                "data_source": "logs"
            }
        }

        confidence = calculator.calculate_edge_confidence(
            edge=edge,
            data_sources=["logs"]
        )

        # logs 基础权重较低
        assert 0.0 <= confidence <= 1.0

    def test_edge_confidence_with_error_rate(self, calculator):
        """测试带错误率的边置信度"""
        edge = {
            "metrics": {
                "confidence": 0.8,
                "error_rate": 0.1  # 10% 错误率
            }
        }

        confidence = calculator.calculate_edge_confidence(
            edge=edge,
            data_sources=["traces"]
        )

        # 应该有错误率惩罚
        assert confidence < 0.8

    def test_edge_confidence_with_call_count(self, calculator):
        """测试带调用次数的边置信度"""
        edge1 = {
            "metrics": {
                "confidence": 0.5,
                "call_count": 10
            }
        }

        edge2 = {
            "metrics": {
                "confidence": 0.5,
                "call_count": 1000
            }
        }

        confidence1 = calculator.calculate_edge_confidence(
            edge=edge1,
            data_sources=["traces"]
        )

        confidence2 = calculator.calculate_edge_confidence(
            edge=edge2,
            data_sources=["traces"]
        )

        # 高调用次数应该略微提升置信度
        assert confidence2 > confidence1

    def test_edge_confidence_multi_source(self, calculator):
        """测试多数据源融合"""
        # 使用 logs 数据源（权重较低，0.4）
        edge_single = {
            "metrics": {
                "confidence": 0.4,
                "data_source": "logs",
                "call_count": 0  # 无调用次数加成
            }
        }

        # 单一数据源
        confidence_single = calculator.calculate_edge_confidence(
            edge=edge_single,
            data_sources=["logs"]
        )

        # 多数据源（相同基础）
        edge_multi = {
            "metrics": {
                "confidence": 0.4,
                "call_count": 0  # 无调用次数加成
            }
        }
        confidence_multi = calculator.calculate_edge_confidence(
            edge=edge_multi,
            data_sources=["traces", "logs", "metrics"]
        )

        # 多数据源应该有加成
        assert confidence_multi > confidence_single

    def test_edge_confidence_clamping(self, calculator):
        """测试置信度限制在 [0, 1] 范围"""
        # 极低置信度
        edge_low = {
            "metrics": {
                "confidence": 0.1,
                "error_rate": 0.9  # 90% 错误率
            }
        }

        confidence_low = calculator.calculate_edge_confidence(
            edge=edge_low,
            data_sources=[]
        )

        assert 0.0 <= confidence_low <= 1.0

        # 极高置信度（通过多源加成）
        edge_high = {
            "metrics": {
                "confidence": 1.0,
                "call_count": 1000000
            }
        }

        confidence_high = calculator.calculate_edge_confidence(
            edge=edge_high,
            data_sources=["traces", "logs", "metrics"]
        )

        assert confidence_high <= 1.0


class TestNodeConfidence:
    """测试节点置信度计算"""

    @pytest.fixture
    def calculator(self):
        return ConfidenceCalculator()

    def test_node_confidence_basic(self, calculator):
        """测试基本节点置信度"""
        node = {
            "metrics": {
                "log_count": 100,
                "trace_count": 50,
                "error_count": 5
            }
        }

        confidence = calculator.calculate_node_confidence(
            node=node,
            data_sources=["logs", "traces"]
        )

        assert 0.0 <= confidence <= 1.0

    def test_node_confidence_activity_score(self, calculator):
        """测试活跃度对置信度的影响"""
        node_low_activity = {
            "metrics": {
                "log_count": 1,
                "trace_count": 1,
                "error_count": 0
            }
        }

        node_high_activity = {
            "metrics": {
                "log_count": 1000,
                "trace_count": 500,
                "error_count": 10
            }
        }

        confidence_low = calculator.calculate_node_confidence(
            node=node_low_activity,
            data_sources=["logs"]
        )

        confidence_high = calculator.calculate_node_confidence(
            node=node_high_activity,
            data_sources=["logs"]
        )

        # 高活跃度应该提升置信度
        assert confidence_high > confidence_low

    def test_node_confidence_with_errors(self, calculator):
        """测试带错误的节点置信度"""
        node = {
            "metrics": {
                "log_count": 100,
                "trace_count": 50,
                "error_count": 20  # 20个错误
            }
        }

        confidence = calculator.calculate_node_confidence(
            node=node,
            data_sources=["logs", "traces"]
        )

        # 错误率 = 20 / 150 ≈ 13%，应该有惩罚
        assert 0.0 <= confidence <= 1.0

    def test_node_confidence_no_data(self, calculator):
        """测试无数据的节点"""
        node = {
            "metrics": {
                "log_count": 0,
                "trace_count": 0,
                "error_count": 0
            }
        }

        confidence = calculator.calculate_node_confidence(
            node=node,
            data_sources=[]
        )

        # 应该有基础置信度
        assert 0.0 <= confidence <= 1.0


class TestTopologyRecalculation:
    """测试拓扑置信度重新计算"""

    @pytest.fixture
    def calculator(self):
        return ConfidenceCalculator()

    @pytest.fixture
    def sample_topology(self):
        """示例拓扑数据"""
        # 使用 naive datetime 避免时区问题
        return {
            "nodes": [
                {
                    "id": "service-a",
                    "metrics": {
                        "log_count": 100,
                        "trace_count": 50,
                        "error_count": 5,
                        "data_sources": ["logs", "traces"],
                        "last_seen": "2026-02-09T11:00:00"
                    }
                },
                {
                    "id": "service-b",
                    "metrics": {
                        "log_count": 200,
                        "trace_count": 100,
                        "error_count": 10,
                        "data_sources": ["traces"]
                    }
                }
            ],
            "edges": [
                {
                    "id": "service-a-service-b",
                    "source": "service-a",
                    "target": "service-b",
                    "metrics": {
                        "call_count": 1000,
                        "error_count": 50,
                        "data_source": "traces"
                    }
                }
            ],
            "metadata": {}
        }

    def test_recalculate_topology(self, calculator, sample_topology):
        """测试重新计算拓扑置信度"""
        result = calculator.recalculate_topology_confidence(sample_topology)

        assert "nodes" in result
        assert "edges" in result
        assert "metadata" in result

        assert len(result["nodes"]) == 2
        assert len(result["edges"]) == 1

    def test_recalculate_updates_node_confidence(self, calculator, sample_topology):
        """测试节点置信度更新"""
        result = calculator.recalculate_topology_confidence(sample_topology)

        for node in result["nodes"]:
            metrics = node["metrics"]
            assert "confidence" in metrics
            assert isinstance(metrics["confidence"], float)
            assert 0.0 <= metrics["confidence"] <= 1.0
            assert "confidence_details" in metrics

    def test_recalculate_updates_edge_confidence(self, calculator, sample_topology):
        """测试边置信度更新"""
        result = calculator.recalculate_topology_confidence(sample_topology)

        for edge in result["edges"]:
            metrics = edge["metrics"]
            assert "confidence" in metrics
            assert isinstance(metrics["confidence"], float)
            assert 0.0 <= metrics["confidence"] <= 1.0
            assert "confidence_details" in metrics

    def test_recalculate_updates_metadata(self, calculator, sample_topology):
        """测试元数据更新"""
        result = calculator.recalculate_topology_confidence(sample_topology)

        metadata = result["metadata"]
        assert "avg_confidence" in metadata
        assert "confidence_algorithm" in metadata
        assert metadata["confidence_algorithm"] == "improved_v2"
        assert "confidence_features" in metadata

    def test_recalculate_empty_topology(self, calculator):
        """测试空拓扑"""
        empty_topology = {
            "nodes": [],
            "edges": [],
            "metadata": {}
        }

        result = calculator.recalculate_topology_confidence(empty_topology)

        assert result["nodes"] == []
        assert result["edges"] == []
        assert result["metadata"]["avg_confidence"] == 0


class TestMultiSourceBoost:
    """测试多数据源加成"""

    @pytest.fixture
    def calculator(self):
        return ConfidenceCalculator()

    def test_single_source_no_boost(self, calculator):
        """测试单一数据源无加成"""
        edge = {
            "metrics": {"confidence": 0.5, "call_count": 0}
        }

        confidence = calculator.calculate_edge_confidence(
            edge=edge,
            data_sources=["traces"]
        )

        # 应该有基础置信度，无多源加成
        assert confidence >= 0.5

    def test_double_source_boost(self, calculator):
        """测试双数据源加成"""
        edge1 = {
            "metrics": {"confidence": 0.5, "call_count": 0}
        }

        confidence_single = calculator.calculate_edge_confidence(
            edge=edge1,
            data_sources=["traces"]
        )

        edge2 = {
            "metrics": {"confidence": 0.5, "call_count": 0}
        }

        confidence_double = calculator.calculate_edge_confidence(
            edge=edge2,
            data_sources=["traces", "logs"]
        )

        # 双数据源应该有约 20% 加成
        assert isclose(confidence_double, confidence_single + 0.20, rel_tol=0.1)

    def test_triple_source_boost(self, calculator):
        """测试三数据源加成"""
        edge = {
            "metrics": {"confidence": 0.5, "call_count": 0}
        }

        confidence = calculator.calculate_edge_confidence(
            edge=edge,
            data_sources=["traces", "logs", "metrics"]
        )

        # 三数据源应该有约 35% 加成，但可能被限制在 1.0
        assert confidence >= 0.85


class TestConvenienceFunction:
    """测试便捷函数"""

    def test_get_confidence_calculator_default(self):
        """测试获取默认计算器"""
        calculator = get_confidence_calculator()

        assert isinstance(calculator, ConfidenceCalculator)
        assert calculator.reference_time is not None

    def test_get_confidence_calculator_custom_time(self):
        """测试获取自定义时间计算器"""
        custom_time = datetime(2026, 2, 9, 12, 0, 0)
        calculator = get_confidence_calculator(reference_time=custom_time)

        assert calculator.reference_time == custom_time


class TestEdgeCases:
    """测试边界情况"""

    @pytest.fixture
    def calculator(self):
        return ConfidenceCalculator()

    def test_edge_with_empty_metrics(self, calculator):
        """测试空指标的边"""
        edge = {"metrics": {}}

        confidence = calculator.calculate_edge_confidence(
            edge=edge,
            data_sources=[]
        )

        # 应该使用默认基础置信度
        assert 0.0 <= confidence <= 1.0

    def test_node_with_empty_metrics(self, calculator):
        """测试空指标的节点"""
        node = {"metrics": {}}

        confidence = calculator.calculate_node_confidence(
            node=node,
            data_sources=[]
        )

        # 应该使用默认基础置信度
        assert 0.0 <= confidence <= 1.0

    def test_negative_error_rate(self, calculator):
        """测试负错误率（异常情况）"""
        penalty = calculator._calculate_error_penalty(-0.1)

        # 应该使用最大惩罚
        assert penalty == 0.30

    def test_error_rate_over_100_percent(self, calculator):
        """测试超过100%的错误率"""
        penalty = calculator._calculate_error_penalty(1.5)

        # 应该使用最大惩罚
        assert penalty == 0.30
