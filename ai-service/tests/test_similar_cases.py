"""
相似案例模块测试

验证 context 特征提取、上下文相似度计算与推荐器候选扩展逻辑。
"""
import json
from datetime import datetime, timezone

from ai.similar_cases import Case, CaseStore, FeatureExtractor, SimilarCaseRecommender


class TestFeatureExtractor:
    """测试特征提取器"""

    def test_extract_features_with_trace_context(self):
        """测试提取 trace/链路上下文特征"""
        context = {
            "trace_id": "trace-abc123456789",
            "source_service": "frontend",
            "target_service": "query-service",
            "related_services": ["topology-service"],
            "k8s": {
                "namespace": "islap",
                "pod": "query-service-7dd9d8c5b8-abcde",
            },
        }

        features = FeatureExtractor.extract_features(
            log_content="ERROR: upstream request timeout to query-service",
            service_name="frontend",
            context=context,
        )

        assert features["trace_present"] is True
        assert features["namespace"] == "islap"
        assert features["call_edge"] == "frontend->query-service"
        assert features["pod_prefix"] == "query-service"
        assert "query-service" in features["context_services"]
        assert "topology-service" in features["context_services"]

    def test_compute_similarity_with_context_edge(self):
        """测试上下游调用边匹配会提高相似度"""
        baseline_1 = FeatureExtractor.extract_features(
            log_content="ERROR: request timeout",
            service_name="frontend",
        )
        baseline_2 = FeatureExtractor.extract_features(
            log_content="ERROR: request timeout",
            service_name="frontend",
        )
        baseline_score, _ = FeatureExtractor.compute_similarity(baseline_1, baseline_2)

        context = {"source_service": "frontend", "target_service": "payment-service", "trace_id": "trace-001"}
        context_1 = FeatureExtractor.extract_features(
            log_content="ERROR: request timeout",
            service_name="frontend",
            context=context,
        )
        context_2 = FeatureExtractor.extract_features(
            log_content="ERROR: request timeout",
            service_name="frontend",
            context=context,
        )
        context_score, matched = FeatureExtractor.compute_similarity(context_1, context_2)

        assert "call_edge" in matched
        assert context_score > baseline_score


class TestSimilarCaseRecommender:
    """测试相似案例推荐器"""

    def test_find_similar_cases_with_context_services(self):
        """测试 context 服务可扩展候选范围"""
        store = CaseStore()
        case = Case(
            id="case-context-001",
            problem_type="network",
            severity="high",
            summary="支付链路上游调用超时",
            log_content="ERROR: Upstream request timeout to payment-service",
            service_name="payment-service",
            root_causes=["下游响应慢"],
            solutions=[{"title": "优化超时配置", "description": "延长 timeout 并增加熔断", "steps": ["调整配置"]}],
        )
        case.similarity_features = FeatureExtractor.extract_features(
            case.log_content,
            case.service_name,
            context={
                "trace_id": "trace-ctx-1",
                "source_service": "api-gateway",
                "target_service": "payment-service",
                "k8s": {"namespace": "islap"},
            },
        )
        store.add_case(case)

        recommender = SimilarCaseRecommender(store)
        results = recommender.find_similar_cases(
            log_content="ERROR: api-gateway call payment-service timeout",
            service_name="",
            problem_type="network",
            context={
                "trace_id": "trace-ctx-2",
                "source_service": "api-gateway",
                "target_service": "payment-service",
                "k8s": {"namespace": "islap"},
            },
            min_similarity=0.2,
        )

        assert len(results) == 1
        assert results[0].case.id == "case-context-001"
        assert "call_edge" in results[0].matched_features


class TestCaseStorePersistence:
    """测试案例库本地持久化能力"""

    def test_add_case_persists_to_local_json(self, tmp_path):
        """新增案例后应写入本地 JSON 文件"""
        store_path = tmp_path / "ai-cases.json"
        store = CaseStore(persistence_path=str(store_path), persistence_enabled=True)

        case = Case(
            id="case-local-001",
            problem_type="network",
            severity="high",
            summary="本地持久化测试案例",
            log_content="ERROR: upstream timeout",
            service_name="gateway",
        )
        store.add_case(case)

        assert store_path.exists()
        payload = json.loads(store_path.read_text(encoding="utf-8"))
        assert isinstance(payload.get("cases"), list)
        assert any(item.get("id") == "case-local-001" for item in payload["cases"])

    def test_load_persisted_cases_restores_searchability(self, tmp_path):
        """从本地 JSON 加载后仍可按服务和类型检索"""
        store_path = tmp_path / "ai-cases.json"
        payload = {
            "version": 1,
            "cases": [
                {
                    "id": "case-local-002",
                    "problem_type": "database",
                    "severity": "critical",
                    "summary": "数据库连接池耗尽",
                    "log_content": "ERROR: connection pool exhausted",
                    "service_name": "order-service",
                    "root_causes": ["连接泄漏"],
                    "solutions": [{"title": "扩大连接池", "description": "", "steps": []}],
                    "created_at": "2026-02-28T00:00:00Z",
                    "resolved": True,
                    "resolution": "已恢复",
                    "tags": ["database"],
                    "similarity_features": {"service": "order-service"},
                }
            ],
        }
        store_path.write_text(json.dumps(payload), encoding="utf-8")

        store = CaseStore(persistence_path=str(store_path), persistence_enabled=True)
        loaded = store.load_persisted_cases()

        assert loaded == 1
        assert store.get_case("case-local-002") is not None
        assert len(store.get_cases_by_type("database")) == 1
        assert len(store.get_cases_by_service("order-service")) == 1

    def test_mark_case_resolved_updates_status(self):
        """标记已解决后应更新案例状态与时间字段"""
        store = CaseStore(persistence_enabled=False)
        case = Case(
            id="case-local-003",
            problem_type="network",
            severity="high",
            summary="待处理案例",
            log_content="ERROR: timeout",
            service_name="gateway",
        )
        store.add_case(case)

        resolved = store.mark_case_resolved("case-local-003", "手动确认已恢复")

        assert resolved is not None
        assert resolved.resolved is True
        assert resolved.resolution == "手动确认已恢复"
        assert resolved.resolved_at != ""
        assert store.get_case("case-local-003").resolved is True

    def test_delete_case_removes_case(self):
        """删除案例后不应再可查询"""
        store = CaseStore(persistence_enabled=False)
        case = Case(
            id="case-local-004",
            problem_type="database",
            severity="medium",
            summary="删除案例测试",
            log_content="ERROR: db timeout",
            service_name="order-service",
        )
        store.add_case(case)

        deleted = store.delete_case("case-local-004")

        assert deleted is True
        assert store.get_case("case-local-004") is None

    def test_to_iso_avoids_double_timezone_suffix(self):
        """时区感知时间应输出规范 ISO，不应出现 +00:00Z。"""
        value = datetime(2026, 3, 3, 2, 24, 29, 513000, tzinfo=timezone.utc)
        assert CaseStore._to_iso(value) == "2026-03-03T02:24:29.513000Z"

    def test_to_iso_normalizes_legacy_double_suffix_text(self):
        """兼容历史脏数据字符串，移除重复的 Z 后缀。"""
        assert CaseStore._to_iso("2026-03-03T02:24:29.513000+00:00Z") == "2026-03-03T02:24:29.513000+00:00"
