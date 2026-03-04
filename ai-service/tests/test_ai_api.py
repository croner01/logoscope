"""
AI API 端点单元测试

测试 api/ai.py 的核心功能：
- 请求模型验证
- 分析器集成
- 错误处理
"""
import pytest
from types import SimpleNamespace
from unittest.mock import Mock, patch, AsyncMock

from api.ai import AnalyzeLogRequest, AnalyzeTraceRequest, set_storage_adapter
from storage.adapter import StorageAdapter


@pytest.fixture
def mock_storage():
    """Mock storage adapter"""
    storage = Mock(spec=StorageAdapter)
    return storage


@pytest.fixture
def mock_analyzer():
    """Mock log analyzer"""
    analyzer = Mock()
    analyzer.analyze_log = Mock(return_value={
        "overview": {
            "problem": "测试问题",
            "severity": "error",
            "description": "测试描述",
            "confidence": 0.9
        },
        "rootCauses": [],
        "solutions": [],
        "metrics": [],
        "similarCases": []
    })
    analyzer.analyze_trace = Mock(return_value={
        "overview": {
            "problem": "Trace 分析",
            "severity": "warning",
            "confidence": 0.8
        },
        "rootCauses": [],
        "solutions": [],
        "metrics": [],
        "similarCases": []
    })
    return analyzer


class TestHealthCheck:
    """测试健康检查"""

    def test_health_check_function(self):
        """测试健康检查函数"""
        from api.ai import health_check
        import asyncio

        result = asyncio.run(health_check())

        assert result["status"] == "healthy"
        assert result["service"] == "ai-service"
        assert result["analyzer"] == "ready"


class TestAnalyzeLogEndpoint:
    """测试日志分析端点"""

    def test_analyze_log_success(self, mock_analyzer):
        """测试成功的日志分析"""
        from api.ai import analyze_log

        request = AnalyzeLogRequest(
            id="test-event-123",
            timestamp="2026-02-09T12:00:00Z",
            entity={"name": "api-server"},
            event={"raw": "Database connection timeout", "level": "error"},
            context={"trace_id": "trace-123"}
        )

        with patch('api.ai.get_log_analyzer', return_value=mock_analyzer):
            import asyncio
            result = asyncio.run(analyze_log(request))

            assert "overview" in result
            assert result["overview"]["confidence"] == 0.9

            # 验证 analyzer 被调用
            mock_analyzer.analyze_log.assert_called_once()

    def test_analyze_log_with_minimal_data(self, mock_analyzer):
        """测试最小数据集"""
        from api.ai import analyze_log

        request = AnalyzeLogRequest(
            id="test-123",
            timestamp="2026-02-09T12:00:00Z",
            entity={"name": "test-service"},
            event={"raw": "Test log", "level": "info"}
        )

        with patch('api.ai.get_log_analyzer', return_value=mock_analyzer):
            import asyncio
            result = asyncio.run(analyze_log(request))

            assert result is not None

    def test_analyze_log_analyzer_error(self, mock_analyzer):
        """测试分析器异常处理"""
        from api.ai import analyze_log
        from fastapi import HTTPException

        mock_analyzer.analyze_log.side_effect = Exception("Analysis failed")

        request = AnalyzeLogRequest(
            id="test-123",
            timestamp="2026-02-09T12:00:00Z",
            entity={"name": "test-service"},
            event={"raw": "Test", "level": "info"}
        )

        with patch('api.ai.get_log_analyzer', return_value=mock_analyzer):
            import asyncio
            with pytest.raises(HTTPException) as exc_info:
                asyncio.run(analyze_log(request))

            assert exc_info.value.status_code == 500
            assert exc_info.value.detail == "Internal server error"


class TestAnalyzeTraceEndpoint:
    """测试 Trace 分析端点"""

    def test_analyze_trace_success(self, mock_analyzer):
        """测试成功的 Trace 分析"""
        from api.ai import analyze_trace

        request = AnalyzeTraceRequest(trace_id="trace-123-abc-456")

        with patch('api.ai.get_log_analyzer', return_value=mock_analyzer):
            import asyncio
            result = asyncio.run(analyze_trace(request))

            assert "overview" in result
            assert result["overview"]["problem"] == "Trace 分析"

            # 验证 analyzer 被调用
            mock_analyzer.analyze_trace.assert_called_once()

    def test_analyze_trace_analyzer_error(self, mock_analyzer):
        """测试分析器异常处理"""
        from api.ai import analyze_trace
        from fastapi import HTTPException

        mock_analyzer.analyze_trace.side_effect = Exception("Trace not found")

        request = AnalyzeTraceRequest(trace_id="nonexistent-trace")

        with patch('api.ai.get_log_analyzer', return_value=mock_analyzer):
            import asyncio
            with pytest.raises(HTTPException) as exc_info:
                asyncio.run(analyze_trace(request))

            assert exc_info.value.status_code == 500

    def test_analyze_trace_normalizes_legacy_response(self, mock_analyzer):
        """测试 Trace 端点会归一化 legacy 返回结构"""
        from api.ai import analyze_trace

        mock_analyzer.analyze_trace.return_value = {
            "summary": "Trace 存在明显瓶颈",
            "severity": "high",
            "confidence": 0.7,
            "root_causes": ["数据库连接池不足"],
            "recommendations": ["扩容连接池", "开启慢查询日志"],
            "similar_cases": ["历史案例 #1"],
        }

        request = AnalyzeTraceRequest(trace_id="trace-legacy-001")

        with patch('api.ai.get_log_analyzer', return_value=mock_analyzer):
            import asyncio
            result = asyncio.run(analyze_trace(request))

            assert "overview" in result
            assert "rootCauses" in result
            assert "solutions" in result
            assert "similarCases" in result
            assert result["overview"]["description"] == "Trace 存在明显瓶颈"
            assert result["rootCauses"][0]["title"] == "数据库连接池不足"
            assert result["solutions"][0]["title"] == "扩容连接池"
            assert result["similarCases"][0]["title"] == "历史案例 #1"


class TestAnalyzeTraceLLMEndpoint:
    """测试 Trace LLM 分析端点"""

    def test_analyze_trace_llm_without_config_returns_canonical_shape(self):
        """测试未配置 LLM 时仍返回统一结构"""
        from api.ai import analyze_trace_llm, LLMTraceAnalyzeRequest

        request = LLMTraceAnalyzeRequest(
            trace_id="trace-001",
            service_name="query-service",
        )

        with patch('api.ai.os.getenv', return_value=None):
            import asyncio
            result = asyncio.run(analyze_trace_llm(request))

            assert "overview" in result
            assert "rootCauses" in result
            assert "solutions" in result
            assert "similarCases" in result
            assert result["analysis_method"] == "none"
            assert result["overview"]["description"] == "请配置 LLM_API_KEY / OPENAI_API_KEY / DEEPSEEK_API_KEY / ANTHROPIC_API_KEY 环境变量以启用 LLM 分析"

    def test_analyze_trace_llm_enabled_returns_canonical_shape(self):
        """测试配置 LLM 时，trace LLM 返回结构被统一"""
        from api.ai import analyze_trace_llm, LLMTraceAnalyzeRequest

        request = LLMTraceAnalyzeRequest(
            trace_id="trace-llm-001",
            service_name="topology-service",
        )
        mock_llm_service = Mock()
        mock_llm_service.analyze_trace = AsyncMock(return_value={
            "summary": "调用链在 payment-service 存在超时风险",
            "recommendations": ["检查 payment-service 超时配置", "回看最近发布变更"],
            "similar_cases": ["case-002"],
            "confidence": 0.82,
        })

        with patch('api.ai.os.getenv', return_value="test-api-key"), patch('api.ai.get_llm_service', return_value=mock_llm_service):
            import asyncio
            result = asyncio.run(analyze_trace_llm(request))

            assert result["analysis_method"] == "llm"
            assert result["overview"]["description"] == "调用链在 payment-service 存在超时风险"
            assert result["solutions"][0]["title"] == "检查 payment-service 超时配置"
            assert result["similarCases"][0]["title"] == "case-002"
            mock_llm_service.analyze_trace.assert_called_once_with(
                trace_data="trace-llm-001",
                service_name="topology-service",
            )

    def test_analyze_trace_llm_enabled_with_deepseek_key(self):
        """测试仅配置 DEEPSEEK_API_KEY 时也会走 LLM 分析路径。"""
        from api.ai import analyze_trace_llm, LLMTraceAnalyzeRequest

        request = LLMTraceAnalyzeRequest(
            trace_id="trace-deepseek-001",
            service_name="query-service",
        )
        mock_llm_service = Mock()
        mock_llm_service.analyze_trace = AsyncMock(return_value={
            "summary": "DeepSeek 路径分析成功",
            "recommendations": ["检查 query-service 线程池"],
            "similar_cases": [],
            "confidence": 0.8,
        })

        def _fake_getenv(key, default=None):
            if key == "DEEPSEEK_API_KEY":
                return "deepseek-test-key"
            return default

        with patch('api.ai.os.getenv', side_effect=_fake_getenv), patch('api.ai.get_llm_service', return_value=mock_llm_service):
            import asyncio
            result = asyncio.run(analyze_trace_llm(request))

            assert result["analysis_method"] == "llm"
            assert result["overview"]["description"] == "DeepSeek 路径分析成功"
            mock_llm_service.analyze_trace.assert_called_once()


class TestAnalyzeLogLLMEndpoint:
    """测试日志 LLM 分析端点"""

    def test_analyze_log_llm_fallback_rule_based(self, mock_analyzer):
        """测试未配置 LLM 时自动回落规则分析"""
        from api.ai import analyze_log_llm, LLMAnalyzeRequest

        request = LLMAnalyzeRequest(
            log_content="Database connection timeout",
            service_name="query-service",
            context={"trace_id": "trace-fallback-001", "source_service": "frontend"},
            use_llm=True,
        )

        with patch('api.ai.os.getenv', return_value=None), patch('api.ai.get_log_analyzer', return_value=mock_analyzer):
            import asyncio
            result = asyncio.run(analyze_log_llm(request))

            assert result["analysis_method"] == "rule-based"
            assert "overview" in result
            called_log_data = mock_analyzer.analyze_log.call_args[0][0]
            assert called_log_data["entity"]["name"] == "query-service"
            assert called_log_data["context"]["trace_id"] == "trace-fallback-001"

    def test_analyze_log_llm_enabled_normalizes_legacy_fields(self):
        """测试 LLM 路径会归一化 legacy 字段"""
        from api.ai import analyze_log_llm, LLMAnalyzeRequest

        request = LLMAnalyzeRequest(
            log_content="Database pool exhausted",
            service_name="order-service",
            context={"trace_id": "trace-llm-100"},
            use_llm=True,
        )
        mock_llm_service = Mock()
        mock_llm_service.analyze_log = AsyncMock(return_value={
            "problem_type": "database",
            "severity": "high",
            "summary": "数据库连接池耗尽",
            "root_causes": ["连接池上限偏低"],
            "suggestions": ["调大 max_connections", "加连接池监控"],
            "similar_cases": ["订单服务连接池耗尽案例"],
            "confidence": 0.91,
            "model": "gpt-4",
            "cached": True,
            "latency_ms": 89,
        })

        with patch('api.ai.os.getenv', return_value="test-api-key"), patch('api.ai.get_llm_service', return_value=mock_llm_service):
            import asyncio
            result = asyncio.run(analyze_log_llm(request))

            assert result["analysis_method"] == "llm"
            assert result["overview"]["problem"] == "database"
            assert result["overview"]["description"] == "数据库连接池耗尽"
            assert result["rootCauses"][0]["title"] == "连接池上限偏低"
            assert result["solutions"][0]["title"] == "调大 max_connections"
            assert result["similarCases"][0]["title"] == "订单服务连接池耗尽案例"
            assert result["cached"] is True
            assert result["latency_ms"] == 89
            assert result["model"] == "gpt-4"


class TestSimilarCasesEndpoint:
    """测试相似案例 API"""

    def test_find_similar_cases_passes_context_to_recommender(self):
        """测试 context 会透传给推荐器"""
        from api.ai import SimilarCasesRequest, find_similar_cases

        request = SimilarCasesRequest(
            log_content="upstream timeout",
            service_name="api-gateway",
            problem_type="network",
            context={
                "trace_id": "trace-ctx-001",
                "source_service": "api-gateway",
                "target_service": "payment-service",
            },
            limit=3,
        )
        mock_recommender = Mock()
        mock_case = SimpleNamespace(
            id="case-ctx-001",
            problem_type="network",
            severity="high",
            summary="支付链路超时",
            service_name="payment-service",
            root_causes=["下游抖动"],
            solutions=[],
            resolved=True,
            resolution="增加重试",
            tags=["network"],
        )
        mock_recommender.find_similar_cases.return_value = [
            SimpleNamespace(
                case=mock_case,
                similarity_score=0.78,
                matched_features=["call_edge"],
                relevance_reason="匹配相同上下游调用链路",
            )
        ]
        mock_case_store = Mock()
        mock_case_store.list_case_change_history.return_value = []
        mock_case_store.count_case_change_history.return_value = 0

        with patch('ai.similar_cases.get_recommender', return_value=mock_recommender), patch(
            'ai.similar_cases.get_case_store',
            return_value=mock_case_store,
        ):
            import asyncio
            result = asyncio.run(find_similar_cases(request))

            assert result["total"] == 1
            assert result["cases"][0]["id"] == "case-ctx-001"
            assert result["cases"][0]["matched_features"] == ["call_edge"]
            mock_recommender.find_similar_cases.assert_called_once_with(
                log_content="upstream timeout",
                service_name="api-gateway",
                problem_type="network",
                context={
                    "trace_id": "trace-ctx-001",
                    "source_service": "api-gateway",
                    "target_service": "payment-service",
                },
                limit=3,
                min_similarity=0.2,
            )

    def test_save_case_extracts_features_with_context(self):
        """测试保存案例时会把 context 传给特征提取"""
        from api.ai import SaveCaseRequest, save_case

        request = SaveCaseRequest(
            problem_type="network",
            severity="high",
            summary="支付链路超时",
            log_content="ERROR: upstream request timeout",
            service_name="api-gateway",
            root_causes=["下游响应慢"],
            solutions=[{"title": "调优超时", "description": "调整超时和重试", "steps": ["修改配置"]}],
            context={
                "trace_id": "trace-save-001",
                "source_service": "api-gateway",
                "target_service": "payment-service",
            },
            tags=["network", "timeout"],
        )
        mock_case_store = Mock()

        with patch('ai.similar_cases.get_case_store', return_value=mock_case_store), patch(
            'ai.similar_cases.FeatureExtractor.extract_features',
            return_value={"call_edge": "api-gateway->payment-service"},
        ) as mock_extract_features:
            import asyncio
            result = asyncio.run(save_case(request))

            assert result["id"].startswith("case-")
            assert result["message"] == "Case saved successfully"
            mock_extract_features.assert_called_once_with(
                "ERROR: upstream request timeout",
                "api-gateway",
                context={
                    "trace_id": "trace-save-001",
                    "source_service": "api-gateway",
                    "target_service": "payment-service",
                },
            )
            added_case = mock_case_store.add_case.call_args[0][0]
            assert added_case.similarity_features["call_edge"] == "api-gateway->payment-service"

    def test_delete_case_success(self):
        """测试删除案例端点"""
        from api.ai import delete_case

        mock_case_store = Mock()
        mock_case_store.delete_case.return_value = True

        with patch('ai.similar_cases.get_case_store', return_value=mock_case_store):
            import asyncio
            result = asyncio.run(delete_case("case-delete-001"))

            assert result["status"] == "ok"
            assert result["id"] == "case-delete-001"
            mock_case_store.delete_case.assert_called_once_with("case-delete-001")

    def test_delete_case_not_found(self):
        """测试删除不存在案例返回 404"""
        from api.ai import delete_case
        from fastapi import HTTPException

        mock_case_store = Mock()
        mock_case_store.delete_case.return_value = False

        with patch('ai.similar_cases.get_case_store', return_value=mock_case_store):
            import asyncio
            with pytest.raises(HTTPException) as exc_info:
                asyncio.run(delete_case("case-delete-missing"))

            assert exc_info.value.status_code == 404

    def test_resolve_case_success(self):
        """测试标记案例已解决端点"""
        from api.ai import resolve_case, ResolveCaseRequest

        request = ResolveCaseRequest(resolution="问题已处理")
        mock_case_store = Mock()
        mock_case_store.mark_case_resolved.return_value = SimpleNamespace(
            id="case-resolve-001",
            resolved=True,
            resolution="问题已处理",
            resolved_at="2026-02-28T00:00:00Z",
        )

        with patch('ai.similar_cases.get_case_store', return_value=mock_case_store):
            import asyncio
            result = asyncio.run(resolve_case("case-resolve-001", request))

            assert result["status"] == "ok"
            assert result["resolved"] is True
            assert result["resolution"] == "问题已处理"
            mock_case_store.mark_case_resolved.assert_called_once_with("case-resolve-001", "问题已处理")


class TestLLMRuntimeReservedEndpoints:
    """测试 LLM runtime 预留接口"""

    def test_get_llm_runtime_status(self):
        """测试获取 LLM runtime 状态"""
        from api.ai import get_llm_runtime_status
        import asyncio

        result = asyncio.run(get_llm_runtime_status())

        assert "configured_provider" in result
        assert "runtime_config_contract" in result
        assert result["runtime_config_contract"]["provider"] == "openai|claude|deepseek|local"

    def test_validate_llm_runtime_config_rejects_unknown_provider(self):
        """测试 runtime 校验拒绝未知 provider"""
        from api.ai import validate_llm_runtime_config, LLMRuntimeConfig
        from fastapi import HTTPException
        import asyncio

        request = LLMRuntimeConfig(provider="unknown", model="x")
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(validate_llm_runtime_config(request))

        assert exc_info.value.status_code == 400

    def test_update_llm_runtime_config_updates_runtime(self):
        """测试 runtime update 可更新当前进程配置并返回状态"""
        from api.ai import update_llm_runtime_config, LLMRuntimeUpdateRequest
        import asyncio

        request = LLMRuntimeUpdateRequest(
            provider="local",
            model="qwen2.5",
            api_base="http://127.0.0.1:11434/v1",
            api_key="test-key",
            local_model_path="/models/qwen2.5",
            persist_to_deployment=False,
            extra={"source": "unit-test"},
        )

        result = asyncio.run(update_llm_runtime_config(request))

        assert result["status"] == "ok"
        assert result["updated"] is True
        assert result["runtime"]["provider"] == "local"
        assert result["runtime"]["api_key_updated"] is True
        assert result["runtime"]["persist_to_deployment"] is False
        assert result["deployment_persistence"]["persisted"] is False
        assert "runtime_status" in result

    def test_update_llm_runtime_config_persists_to_deployment_file(self, tmp_path, monkeypatch):
        """测试 runtime update 可将非敏感参数持久化到部署文件。"""
        from api.ai import update_llm_runtime_config, LLMRuntimeUpdateRequest
        import asyncio

        deploy_file = tmp_path / "ai-service.yaml"
        deploy_file.write_text(
            "\n".join(
                [
                    "apiVersion: apps/v1",
                    "kind: Deployment",
                    "spec:",
                    "  template:",
                    "    spec:",
                    "      containers:",
                    "      - name: semantic-engine",
                    "        env:",
                    "        - name: APP_NAME",
                    "          value: \"semantic-engine\"",
                    "        - name: LLM_PROVIDER",
                    "          value: \"deepseek\"",
                    "        - name: LLM_MODEL",
                    "          value: \"deepseek-chat\"",
                    "        resources:",
                    "          limits:",
                    "            memory: 1Gi",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("LLM_DEPLOYMENT_FILE_PATH", str(deploy_file))

        request = LLMRuntimeUpdateRequest(
            provider="openai",
            model="gpt-4o-mini",
            api_base="https://api.openai.com/v1",
            local_model_path="/models/unused",
            persist_to_deployment=True,
            clear_api_key=True,
            extra={"source": "unit-test"},
        )

        result = asyncio.run(update_llm_runtime_config(request))

        assert result["status"] == "ok"
        assert result["deployment_persistence"]["persisted"] is True
        assert "LLM_PROVIDER" in result["deployment_persistence"]["updated_keys"]
        assert "LLM_API_BASE" in result["deployment_persistence"]["added_keys"]
        assert "LOCAL_MODEL_PATH" in result["deployment_persistence"]["added_keys"]

        updated_content = deploy_file.read_text(encoding="utf-8")
        assert '- name: LLM_PROVIDER' in updated_content
        assert 'value: "openai"' in updated_content
        assert '- name: LLM_MODEL' in updated_content
        assert 'value: "gpt-4o-mini"' in updated_content
        assert '- name: LLM_API_BASE' in updated_content
        assert 'value: "https://api.openai.com/v1"' in updated_content
        assert '- name: LOCAL_MODEL_PATH' in updated_content
        assert 'value: "/models/unused"' in updated_content

    def test_update_llm_runtime_config_persist_missing_file(self, tmp_path, monkeypatch):
        """测试部署文件缺失时，接口仍返回成功并给出持久化失败原因。"""
        from api.ai import update_llm_runtime_config, LLMRuntimeUpdateRequest
        import asyncio

        missing_file = str(tmp_path / "missing-ai-service.yaml")
        monkeypatch.setenv("LLM_DEPLOYMENT_FILE_PATH", missing_file)

        request = LLMRuntimeUpdateRequest(
            provider="deepseek",
            model="deepseek-chat",
            persist_to_deployment=True,
            clear_api_key=True,
        )

        result = asyncio.run(update_llm_runtime_config(request))

        assert result["status"] == "ok"
        assert result["updated"] is True
        assert result["deployment_persistence"]["persisted"] is False
        assert "not found" in str(result["deployment_persistence"]["error"]).lower()


class TestFollowUpEndpoint:
    """测试追问接口 LLM 开关行为。"""

    def test_follow_up_use_llm_false_forces_rule_based(self):
        """当 use_llm=False 时，即使运行时可用也应走规则回答。"""
        from api.ai import follow_up_analysis, FollowUpRequest
        import asyncio

        mock_store = Mock()
        mock_store.create_session.return_value = SimpleNamespace(session_id="sess-followup-001")
        mock_store.get_session.return_value = None
        mock_store.get_messages.return_value = []
        mock_store.append_messages.return_value = False
        mock_store.update_session.return_value = True

        mock_llm_service = Mock()
        mock_llm_service.chat = AsyncMock(return_value="这条回答不应被使用")

        request = FollowUpRequest(
            question="下一步应该如何排查？",
            use_llm=False,
            analysis_context={
                "analysis_type": "log",
                "service_name": "query-service",
                "input_text": "database timeout",
                "result": {"overview": {"description": "数据库连接超时"}},
                "llm_info": {"method": "llm", "model": "gpt-4o-mini"},
            },
        )

        with patch("api.ai.get_ai_session_store", return_value=mock_store), patch(
            "api.ai._is_llm_configured", return_value=True
        ), patch("api.ai.get_llm_service", return_value=mock_llm_service):
            result = asyncio.run(follow_up_analysis(request))

        assert result["analysis_method"] == "rule-based"
        assert result["llm_enabled"] is True
        assert result["llm_requested"] is False
        mock_llm_service.chat.assert_not_called()

    def test_follow_up_use_llm_true_calls_llm_chat(self):
        """当 use_llm=True 且 LLM 可用时，追问应调用 LLM。"""
        from api.ai import follow_up_analysis, FollowUpRequest
        import asyncio

        mock_store = Mock()
        mock_store.create_session.return_value = SimpleNamespace(session_id="sess-followup-002")
        mock_store.get_session.return_value = None
        mock_store.get_messages.return_value = []
        mock_store.append_messages.return_value = False
        mock_store.update_session.return_value = True

        mock_llm_service = Mock()
        mock_llm_service.chat = AsyncMock(return_value="建议先检查数据库连接池上限与慢查询。")

        request = FollowUpRequest(
            question="这个问题优先排查什么？",
            use_llm=True,
            analysis_context={
                "analysis_type": "log",
                "service_name": "query-service",
                "input_text": "database timeout",
                "result": {"overview": {"description": "数据库连接超时"}},
                "llm_info": {"method": "llm", "model": "gpt-4o-mini"},
            },
        )

        with patch("api.ai.get_ai_session_store", return_value=mock_store), patch(
            "api.ai._is_llm_configured", return_value=True
        ), patch("api.ai.get_llm_service", return_value=mock_llm_service):
            result = asyncio.run(follow_up_analysis(request))

        assert result["analysis_method"] == "llm"
        assert result["llm_enabled"] is True
        assert result["llm_requested"] is True
        mock_llm_service.chat.assert_called_once()

    def test_follow_up_llm_exception_fallbacks_to_rule_based(self):
        """当 LLM 抛异常（非 asyncio.TimeoutError）时，应自动降级规则模式而非抛 500。"""
        from api.ai import follow_up_analysis, FollowUpRequest
        import asyncio

        mock_store = Mock()
        mock_store.create_session.return_value = SimpleNamespace(session_id="sess-followup-err-001")
        mock_store.get_session.return_value = None
        mock_store.get_messages.return_value = []
        mock_store.append_messages.return_value = False
        mock_store.update_session.return_value = True

        mock_llm_service = Mock()
        mock_llm_service.chat = AsyncMock(side_effect=RuntimeError("upstream 504 gateway timeout"))

        request = FollowUpRequest(
            question="请继续给出下一步排查建议",
            use_llm=True,
            analysis_context={
                "analysis_type": "log",
                "service_name": "query-service",
                "input_text": "database timeout",
                "result": {"overview": {"description": "数据库连接超时"}},
            },
        )

        with patch("api.ai.get_ai_session_store", return_value=mock_store), patch(
            "api.ai._is_llm_configured", return_value=True
        ), patch("api.ai.get_llm_service", return_value=mock_llm_service):
            result = asyncio.run(follow_up_analysis(request))

        assert result["analysis_method"] == "rule-based"
        assert result["llm_timeout_fallback"] is True
        assert "规则模式" in str(result["answer"])

    def test_follow_up_merges_stored_history_when_client_only_sends_incremental_message(self):
        """当前端仅传增量 history 时，服务端应补齐会话历史并避免重复 user 消息。"""
        from api.ai import follow_up_analysis, FollowUpRequest
        import asyncio

        mock_store = Mock()
        mock_store.get_session.return_value = SimpleNamespace(session_id="sess-followup-003")
        mock_store.get_messages.return_value = [
            SimpleNamespace(role="user", content="历史问题：连接池告警", created_at="2026-03-02T10:00:00Z"),
            SimpleNamespace(role="assistant", content="历史回答：先检查连接池上限", created_at="2026-03-02T10:00:05Z"),
        ]
        mock_store.append_messages.return_value = False
        mock_store.update_session.return_value = True

        request = FollowUpRequest(
            question="请按优先级给出排查顺序（服务=query-service）",
            analysis_session_id="sess-followup-003",
            use_llm=False,
            history=[
                {
                    "role": "user",
                    "content": "请按优先级给出排查顺序（服务=query-service）",
                    "timestamp": "2026-03-02T10:01:00Z",
                }
            ],
            analysis_context={
                "analysis_type": "log",
                "service_name": "query-service",
                "input_text": "database timeout",
                "result": {"overview": {"description": "数据库连接超时"}},
            },
        )

        with patch("api.ai.get_ai_session_store", return_value=mock_store), patch(
            "api.ai._is_llm_configured", return_value=True
        ):
            result = asyncio.run(follow_up_analysis(request))

        history = result.get("history") or []
        contents = [item.get("content") for item in history if isinstance(item, dict)]
        assert any("历史问题：连接池告警" == content for content in contents)
        assert any("历史回答：先检查连接池上限" == content for content in contents)
        assert sum(
            1
            for item in history
            if isinstance(item, dict)
            and item.get("role") == "user"
            and item.get("content") == "请按优先级给出排查顺序（服务=query-service）"
        ) == 1


class TestHistoryMessageDeleteEndpoint:
    """测试单条历史消息删除接口。"""

    def test_delete_history_message_success(self):
        from api.ai import delete_ai_history_message
        import asyncio

        mock_store = Mock()
        mock_store.delete_message.return_value = True
        mock_store.get_message_count.return_value = 3

        with patch("api.ai.get_ai_session_store", return_value=mock_store):
            result = asyncio.run(delete_ai_history_message("sess-001", "msg-001"))

        assert result["status"] == "ok"
        assert result["session_id"] == "sess-001"
        assert result["message_id"] == "msg-001"
        assert result["remaining_message_count"] == 3

    def test_delete_history_message_not_found(self):
        from api.ai import delete_ai_history_message
        from fastapi import HTTPException
        import asyncio

        mock_store = Mock()
        mock_store.delete_message.return_value = False

        with patch("api.ai.get_ai_session_store", return_value=mock_store):
            with pytest.raises(HTTPException) as exc_info:
                asyncio.run(delete_ai_history_message("sess-001", "msg-missing"))

        assert exc_info.value.status_code == 404


class TestNormalizeHelpers:
    """测试 AI 返回结构归一化 helper"""

    def test_normalize_analysis_result_from_legacy_fields(self):
        """测试 legacy 字段会转换为统一字段"""
        from api.ai import _normalize_analysis_result

        legacy_result = {
            "problem_type": "database",
            "severity": "critical",
            "summary": "数据库连接超时",
            "confidence": 0.88,
            "root_causes": ["连接池耗尽"],
            "suggestions": ["提高连接池上限", "排查慢 SQL"],
            "similar_cases": ["订单服务连接池耗尽"],
            "cached": True,
            "latency_ms": "128",
            "model": "gpt-4",
        }

        normalized = _normalize_analysis_result(legacy_result, analysis_method="llm")

        assert normalized["overview"]["problem"] == "database"
        assert normalized["overview"]["severity"] == "critical"
        assert normalized["overview"]["description"] == "数据库连接超时"
        assert normalized["overview"]["confidence"] == 0.88

        assert normalized["rootCauses"] == [{"title": "连接池耗尽", "description": ""}]
        assert normalized["solutions"][0]["title"] == "提高连接池上限"
        assert normalized["similarCases"][0]["title"] == "订单服务连接池耗尽"
        assert normalized["analysis_method"] == "llm"
        assert normalized["cached"] is True
        assert normalized["latency_ms"] == 128
        assert normalized["model"] == "gpt-4"


class TestRequestModels:
    """测试请求模型"""

    def test_analyze_log_request_model(self):
        """测试 AnalyzeLogRequest 模型"""
        request = AnalyzeLogRequest(
            id="test-123",
            timestamp="2026-02-09T12:00:00Z",
            entity={"name": "test-service"},
            event={"raw": "Test", "level": "info"}
        )

        assert request.id == "test-123"
        assert request.entity["name"] == "test-service"

    def test_analyze_log_request_with_context(self):
        """测试带 context 的请求"""
        request = AnalyzeLogRequest(
            id="test-123",
            timestamp="2026-02-09T12:00:00Z",
            entity={"name": "test"},
            event={"raw": "Test", "level": "info"},
            context={"trace_id": "trace-123"}
        )

        assert request.context["trace_id"] == "trace-123"

    def test_analyze_trace_request_model(self):
        """测试 AnalyzeTraceRequest 模型"""
        request = AnalyzeTraceRequest(trace_id="trace-123")

        assert request.trace_id == "trace-123"


class TestAnalyzeTraceRequestCompatibility:
    """测试 trace 请求参数校验行为"""

    def test_analyze_trace_requires_trace_id(self):
        """测试 trace_id 为空时返回 400"""
        from api.ai import analyze_trace
        from fastapi import HTTPException

        request = AnalyzeTraceRequest(trace_id="   ")

        import asyncio
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(analyze_trace(request))

        assert exc_info.value.status_code == 400
        assert exc_info.value.detail == "trace_id is required"


class TestStorageAdapter:
    """测试 Storage Adapter"""

    def test_set_storage_adapter(self, mock_storage):
        """测试设置 storage adapter"""
        set_storage_adapter(mock_storage)

        from api import ai
        assert ai.storage == mock_storage
