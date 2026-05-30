"""
Tests for graph/confidence_calculator.py
"""
import os
import sys
from datetime import datetime, timezone

# 添加 topology-service 根目录到 Python 路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from graph.confidence_calculator import get_confidence_calculator
from shared_src.graph.confidence_calculator import get_confidence_calculator as get_shared_confidence_calculator


class TestConfidenceQuality:
    """质量分算法测试。"""

    def test_edge_quality_score_degrades_with_bad_metrics(self):
        """错误率、延迟、超时升高时，质量分应下降。"""
        calculator = get_confidence_calculator(datetime(2026, 2, 26, tzinfo=timezone.utc))

        good = calculator.calculate_edge_quality_score({
            "error_rate": 0.0,
            "p95": 80,
            "p99": 120,
            "timeout_rate": 0.0,
            "retries": 0,
            "pending": 0,
            "dlq": 0,
        })
        bad = calculator.calculate_edge_quality_score({
            "error_rate": 0.15,
            "p95": 1200,
            "p99": 2500,
            "timeout_rate": 0.2,
            "retries": 4,
            "pending": 3,
            "dlq": 2,
        })

        assert good["score"] > bad["score"]
        assert good["score"] <= 100.0
        assert bad["score"] >= 0.0

    def test_recalculate_topology_confidence_sets_quality_score(self):
        """recalculate 后边指标应包含 quality_score。"""
        calculator = get_confidence_calculator(datetime(2026, 2, 26, tzinfo=timezone.utc))

        topology = {
            "nodes": [{
                "id": "checkout",
                "metrics": {
                    "log_count": 100,
                    "trace_count": 10,
                    "error_count": 2,
                    "data_sources": ["traces", "logs"],
                }
            }],
            "edges": [{
                "id": "checkout-payment",
                "source": "checkout",
                "target": "payment",
                "metrics": {
                    "data_source": "traces",
                    "call_count": 20,
                    "error_rate": 0.05,
                    "p95": 240,
                    "p99": 420,
                    "timeout_rate": 0.01,
                }
            }],
            "metadata": {},
        }

        result = calculator.recalculate_topology_confidence(topology)
        edge_metrics = result["edges"][0]["metrics"]
        node_metrics = result["nodes"][0]["metrics"]

        assert "quality_score" in edge_metrics
        assert "quality_details" in edge_metrics
        assert "confidence" in edge_metrics
        assert "quality_score" in node_metrics

    def test_recalculate_topology_confidence_uses_all_edge_data_sources(self):
        """边重算应使用 data_sources 列表，确保多源加分生效。"""
        calculator = get_confidence_calculator(datetime(2026, 2, 26, tzinfo=timezone.utc))

        topology = {
            "nodes": [],
            "edges": [{
                "id": "frontend-order",
                "source": "frontend",
                "target": "order",
                "metrics": {
                    "data_source": "logs_heuristic",
                    "data_sources": ["logs_heuristic", "metrics"],
                    "confidence": 0.3,
                    "error_rate": 0.0,
                    "call_count": 0,
                }
            }],
            "metadata": {},
        }

        result = calculator.recalculate_topology_confidence(topology)
        edge_metrics = result["edges"][0]["metrics"]

        # 单源时约 0.3，多源至少应触发 +0.2 加分。
        assert edge_metrics["confidence"] >= 0.5
        assert set(edge_metrics["confidence_details"]["data_sources"]) == {"logs_heuristic", "metrics"}

    def test_recalculate_topology_confidence_calculated_at_is_valid_iso8601(self):
        """calculated_at 应为可解析 ISO8601，不能出现 +00:00Z。"""
        calculator = get_confidence_calculator(datetime(2026, 2, 26, tzinfo=timezone.utc))
        topology = {
            "nodes": [{"id": "a", "metrics": {"data_sources": ["logs"]}}],
            "edges": [{"source": "a", "target": "b", "metrics": {"data_source": "logs"}}],
            "metadata": {},
        }

        result = calculator.recalculate_topology_confidence(topology)
        node_ts = result["nodes"][0]["metrics"]["confidence_details"]["calculated_at"]
        edge_ts = result["edges"][0]["metrics"]["confidence_details"]["calculated_at"]

        assert not str(node_ts).endswith("+00:00Z")
        assert not str(edge_ts).endswith("+00:00Z")
        assert datetime.fromisoformat(str(node_ts).replace("Z", "+00:00")).tzinfo is not None
        assert datetime.fromisoformat(str(edge_ts).replace("Z", "+00:00")).tzinfo is not None

    def test_shared_confidence_calculator_calculated_at_is_valid_iso8601(self):
        """shared_src 版本也应输出合法 calculated_at。"""
        calculator = get_shared_confidence_calculator(datetime(2026, 2, 26, tzinfo=timezone.utc))
        topology = {
            "nodes": [{"id": "a", "metrics": {"data_sources": ["logs"]}}],
            "edges": [{"source": "a", "target": "b", "metrics": {"data_source": "logs"}}],
            "metadata": {},
        }

        result = calculator.recalculate_topology_confidence(topology)
        node_ts = result["nodes"][0]["metrics"]["confidence_details"]["calculated_at"]
        edge_ts = result["edges"][0]["metrics"]["confidence_details"]["calculated_at"]

        assert not str(node_ts).endswith("+00:00Z")
        assert not str(edge_ts).endswith("+00:00Z")
        assert datetime.fromisoformat(str(node_ts).replace("Z", "+00:00")).tzinfo is not None
        assert datetime.fromisoformat(str(edge_ts).replace("Z", "+00:00")).tzinfo is not None
