"""
AI 分析模块单元测试

测试内容：
- LLM 服务
- 相似案例推荐
- Trace 分析
- 告警规则引擎
"""

import pytest
import asyncio
from datetime import datetime
from unittest.mock import Mock, patch, AsyncMock

from ai.llm_service import LLMConfig, LLMResponse, OpenAIProvider, LLMService
from ai.similar_cases import (
    Case, CaseStore, SimilarCaseRecommender, FeatureExtractor
)
from ai.trace_analyzer import Span, TraceAnalyzer
from alerting.engine import (
    AlertRule, Alert, AlertManager, AlertSeverity, AlertState, RuleEvaluator
)


class TestFeatureExtractor:
    """特征提取器测试"""

    def test_extract_features_database_error(self):
        """测试数据库错误特征提取"""
        log_content = "ERROR: Database connection timeout. Connection pool exhausted."
        features = FeatureExtractor.extract_features(log_content, "order-service")

        assert "database" in features["problem_types"]
        assert "timeout" in features["keywords"]
        assert features["service"] == "order-service"

    def test_extract_features_network_error(self):
        """测试网络错误特征提取"""
        log_content = "ERROR: Connection refused to upstream service. Network unreachable."
        features = FeatureExtractor.extract_features(log_content, "api-gateway")

        assert "network" in features["problem_types"]

    def test_extract_features_memory_error(self):
        """测试内存错误特征提取"""
        log_content = "FATAL: Out of memory error. Java heap space exhausted."
        features = FeatureExtractor.extract_features(log_content, "data-processor")

        assert "memory" in features["problem_types"]
        assert "fatal" in features["keywords"]

    def test_compute_similarity_same_problem(self):
        """测试相同问题类型的相似度计算"""
        features1 = FeatureExtractor.extract_features(
            "ERROR: Database connection timeout", "service-a"
        )
        features2 = FeatureExtractor.extract_features(
            "ERROR: Database pool exhausted", "service-b"
        )

        similarity, matched = FeatureExtractor.compute_similarity(features1, features2)

        assert similarity > 0
        assert "problem_type" in matched

    def test_compute_similarity_same_service(self):
        """测试相同服务的相似度计算"""
        features1 = FeatureExtractor.extract_features(
            "ERROR: Something went wrong", "order-service"
        )
        features2 = FeatureExtractor.extract_features(
            "WARN: Another issue", "order-service"
        )

        similarity, matched = FeatureExtractor.compute_similarity(features1, features2)

        assert "service" in matched


class TestCaseStore:
    """案例存储测试"""

    def test_add_case(self):
        """测试添加案例"""
        store = CaseStore()
        case = Case(
            id="test-001",
            problem_type="database",
            severity="high",
            summary="Test case",
            log_content="Test log",
            service_name="test-service",
        )

        store.add_case(case)

        assert store.get_case("test-001") == case

    def test_get_cases_by_type(self):
        """测试按类型获取案例"""
        store = CaseStore()
        case1 = Case(
            id="test-001",
            problem_type="database",
            severity="high",
            summary="DB case",
            log_content="DB error",
            service_name="service-a",
        )
        case2 = Case(
            id="test-002",
            problem_type="network",
            severity="medium",
            summary="Network case",
            log_content="Network error",
            service_name="service-b",
        )

        store.add_case(case1)
        store.add_case(case2)

        db_cases = store.get_cases_by_type("database")

        assert len(db_cases) == 1
        assert db_cases[0].id == "test-001"


class TestSimilarCaseRecommender:
    """相似案例推荐器测试"""

    def test_find_similar_cases(self):
        """测试查找相似案例"""
        store = CaseStore()
        
        case = Case(
            id="test-001",
            problem_type="database",
            severity="high",
            summary="Database connection timeout",
            log_content="ERROR: Database connection timeout",
            service_name="order-service",
            root_causes=["连接池配置过小"],
            solutions=[{"title": "增加连接池", "steps": []}],
        )
        case.similarity_features = FeatureExtractor.extract_features(
            case.log_content, case.service_name
        )
        store.add_case(case)

        recommender = SimilarCaseRecommender(store)
        results = recommender.find_similar_cases(
            log_content="ERROR: Database pool exhausted",
            service_name="order-service",
            limit=5
        )

        assert len(results) > 0
        assert results[0].case.problem_type == "database"


class TestRuleEvaluator:
    """规则评估器测试"""

    def test_evaluate_simple_condition_eq(self):
        """测试简单相等条件"""
        log = {"level": "error", "message": "Something went wrong"}
        condition = {"type": "simple", "field": "level", "operator": "eq", "value": "error"}

        result = RuleEvaluator.evaluate_log_rule(log, condition)
        assert result is True

    def test_evaluate_simple_condition_neq(self):
        """测试简单不等条件"""
        log = {"level": "info", "message": "Normal log"}
        condition = {"type": "simple", "field": "level", "operator": "neq", "value": "error"}

        result = RuleEvaluator.evaluate_log_rule(log, condition)
        assert result is True

    def test_evaluate_pattern_condition(self):
        """测试模式匹配条件"""
        log = {"message": "ERROR: Connection timeout occurred"}
        condition = {"type": "pattern", "pattern": "timeout", "fields": ["message"]}

        result = RuleEvaluator.evaluate_log_rule(log, condition)
        assert result is True

    def test_evaluate_compound_condition_and(self):
        """测试复合 AND 条件"""
        log = {"level": "error", "message": "Database connection failed"}
        condition = {
            "type": "compound",
            "logic": "and",
            "conditions": [
                {"type": "simple", "field": "level", "operator": "eq", "value": "error"},
                {"type": "pattern", "pattern": "database", "fields": ["message"]},
            ]
        }

        result = RuleEvaluator.evaluate_log_rule(log, condition)
        assert result is True

    def test_evaluate_compound_condition_or(self):
        """测试复合 OR 条件"""
        log = {"level": "warn", "message": "Connection timeout"}
        condition = {
            "type": "compound",
            "logic": "or",
            "conditions": [
                {"type": "simple", "field": "level", "operator": "eq", "value": "error"},
                {"type": "pattern", "pattern": "timeout", "fields": ["message"]},
            ]
        }

        result = RuleEvaluator.evaluate_log_rule(log, condition)
        assert result is True


class TestAlertManager:
    """告警管理器测试"""

    def test_get_all_rules(self):
        """测试获取所有规则"""
        manager = AlertManager()
        rules = manager.get_all_rules()

        assert len(rules) > 0

    def test_get_active_alerts(self):
        """测试获取活跃告警"""
        manager = AlertManager()
        alerts = manager.get_active_alerts()

        assert isinstance(alerts, list)

    def test_get_alert_stats(self):
        """测试获取告警统计"""
        manager = AlertManager()
        stats = manager.get_alert_stats()

        assert "total" in stats
        assert "firing" in stats
        assert "by_severity" in stats


class TestTraceAnalyzer:
    """Trace 分析器测试"""

    def test_analyze_trace(self):
        """测试 Trace 分析"""
        analyzer = TraceAnalyzer()
        result = analyzer.analyze_trace("test-trace-001")

        assert result.trace_id == "test-trace-001"
        assert result.span_count > 0
        assert len(result.recommendations) > 0

    def test_get_trace_visualization_data(self):
        """测试获取可视化数据"""
        analyzer = TraceAnalyzer()
        result = analyzer.get_trace_visualization_data("test-trace-001")

        assert "nodes" in result
        assert "edges" in result
        assert "waterfall" in result


class TestLLMService:
    """LLM 服务测试"""

    def test_config_defaults(self):
        """测试配置默认值"""
        config = LLMConfig()

        assert config.provider == "openai"
        assert config.model == "gpt-4"
        assert config.cache_enabled is True

    @pytest.mark.asyncio
    async def test_analyze_log_fallback(self):
        """测试日志分析（无 API Key 时回退）"""
        with patch.dict("os.environ", {}, clear=True):
            service = LLMService(LLMConfig(provider="local"))
            result = await service.analyze_log("Test error message", "test-service")

            assert "error" in result or "problem_type" in result


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
