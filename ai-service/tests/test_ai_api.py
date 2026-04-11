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
            "data_flow": {
                "summary": "api-gateway -> order-service -> payment-service",
                "path": [
                    {"step": 1, "component": "api-gateway", "operation": "HTTP /checkout"},
                    {"step": 2, "component": "order-service", "operation": "rpc:charge"},
                    {"step": 3, "component": "payment-service", "operation": "db write", "status": "error"},
                ],
            },
            "handling_ideas": ["先确认 payment-service 是否为首个异常点", "再回放最近 15 分钟慢 span 分布"],
            "recommendations": ["检查 payment-service 超时配置", "回看最近发布变更"],
            "similar_cases": ["case-002"],
            "confidence": 0.82,
        })

        with patch('api.ai.os.getenv', return_value="test-api-key"), patch('api.ai.get_llm_service', return_value=mock_llm_service):
            import asyncio
            result = asyncio.run(analyze_trace_llm(request))

            assert result["analysis_method"] == "llm"
            assert result["overview"]["description"] == "调用链在 payment-service 存在超时风险"
            assert result["dataFlow"]["summary"] == "api-gateway -> order-service -> payment-service"
            assert result["handlingIdeas"][0]["title"] == "先确认 payment-service 是否为首个异常点"
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
            "data_flow": {
                "summary": "frontend -> query-service -> db",
                "path": [
                    {"step": 1, "component": "frontend", "operation": "HTTP query"},
                    {"step": 2, "component": "query-service", "operation": "SQL execute", "status": "error"},
                ],
            },
            "root_causes": ["连接池上限偏低"],
            "handling_ideas": ["先确认连接池打满是否持续", "按时间窗对齐发布与流量峰值"],
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
            assert result["dataFlow"]["summary"] == "frontend -> query-service -> db"
            assert result["rootCauses"][0]["title"] == "连接池上限偏低"
            assert result["handlingIdeas"][0]["title"] == "先确认连接池打满是否持续"
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

    @pytest.fixture(autouse=True)
    def _patch_run_blocking(self):
        """单测中直连同步调用，避免环境层 asyncio 线程池阻塞导致用例挂起。"""

        async def _direct_run_blocking(func, *args, **kwargs):
            return func(*args, **kwargs)

        with patch("api.ai._run_blocking", new=_direct_run_blocking):
            yield

    @staticmethod
    def _assert_followup_planner_payload(result):
        """断言追问响应携带任务拆解与反思信息。"""
        assert isinstance(result.get("subgoals"), list)
        assert len(result["subgoals"]) >= 1
        assert isinstance(result.get("reflection"), dict)
        assert result["reflection"].get("total_count") == len(result["subgoals"])
        assert isinstance(result.get("actions"), list)

        history = result.get("history") or []
        assistants = [
            item for item in history
            if isinstance(item, dict) and item.get("role") == "assistant"
        ]
        assert assistants
        assistant_metadata = assistants[-1].get("metadata") or {}
        assert isinstance(assistant_metadata.get("subgoals"), list)
        assert isinstance(assistant_metadata.get("reflection"), dict)
        assert isinstance(assistant_metadata.get("actions"), list)

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
        self._assert_followup_planner_payload(result)
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
        self._assert_followup_planner_payload(result)
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
        self._assert_followup_planner_payload(result)

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
        self._assert_followup_planner_payload(result)

    def test_follow_up_history_contains_message_ids_when_not_persisted(self):
        """当 append_messages 返回 False 时，返回 history 也应包含 message_id，确保后续 /exec 可定位消息。"""
        from api.ai import follow_up_analysis, FollowUpRequest
        import asyncio

        mock_store = Mock()
        mock_store.create_session.return_value = SimpleNamespace(session_id="sess-followup-id-001")
        mock_store.get_session.return_value = None
        mock_store.get_messages.return_value = []
        mock_store.append_messages.return_value = False
        mock_store.update_session.return_value = True

        request = FollowUpRequest(
            question="继续分析并给出命令",
            use_llm=False,
            analysis_context={
                "analysis_type": "log",
                "service_name": "query-service",
                "input_text": "database timeout",
                "result": {"overview": {"description": "数据库连接超时"}},
            },
        )

        with patch("api.ai.get_ai_session_store", return_value=mock_store), patch(
            "api.ai._is_llm_configured", return_value=False
        ):
            result = asyncio.run(follow_up_analysis(request))

        history = result.get("history") or []
        assert len(history) >= 2
        assert all(isinstance(item.get("message_id"), str) and item.get("message_id") for item in history)

    def test_follow_up_preserves_existing_history_message_ids_when_not_persisted(self):
        """当未走持久化历史回读时，应保留前端传入历史中的 message_id。"""
        from api.ai import follow_up_analysis, FollowUpRequest
        import asyncio

        mock_store = Mock()
        mock_store.get_session.return_value = SimpleNamespace(session_id="sess-followup-id-002")
        mock_store.get_messages.return_value = []
        mock_store.append_messages.return_value = False
        mock_store.update_session.return_value = True

        request = FollowUpRequest(
            question="请继续",
            analysis_session_id="sess-followup-id-002",
            use_llm=False,
            history=[
                {
                    "message_id": "msg-history-user-1",
                    "role": "user",
                    "content": "历史提问",
                    "timestamp": "2026-03-02T10:00:00Z",
                },
                {
                    "message_id": "msg-history-assistant-1",
                    "role": "assistant",
                    "content": "历史回答：`kubectl logs deploy/query-service -n islap --tail=20`",
                    "timestamp": "2026-03-02T10:00:05Z",
                },
            ],
            analysis_context={
                "analysis_type": "log",
                "service_name": "query-service",
                "input_text": "database timeout",
                "result": {"overview": {"description": "数据库连接超时"}},
            },
        )

        with patch("api.ai.get_ai_session_store", return_value=mock_store), patch(
            "api.ai._is_llm_configured", return_value=False
        ):
            result = asyncio.run(follow_up_analysis(request))

        history = result.get("history") or []
        message_ids = {str(item.get("message_id")) for item in history if isinstance(item, dict)}
        assert "msg-history-user-1" in message_ids
        assert "msg-history-assistant-1" in message_ids

    def test_follow_up_keeps_message_ids_in_cached_conversation_history(self):
        """当复用 conversation_id 且前端未再次上传 history 时，缓存历史中的 message_id 应保留。"""
        from api.ai import follow_up_analysis, FollowUpRequest
        import asyncio

        mock_store = Mock()
        mock_store.create_session.return_value = SimpleNamespace(session_id="sess-followup-id-003")
        mock_store.get_session.return_value = None
        mock_store.get_messages.return_value = []
        mock_store.append_messages.return_value = False
        mock_store.update_session.return_value = True

        first_request = FollowUpRequest(
            question="第一轮追问",
            use_llm=False,
            analysis_context={
                "analysis_type": "log",
                "service_name": "query-service",
                "input_text": "database timeout",
                "result": {"overview": {"description": "数据库连接超时"}},
            },
        )

        with patch("api.ai.get_ai_session_store", return_value=mock_store), patch(
            "api.ai._is_llm_configured", return_value=False
        ):
            first_result = asyncio.run(follow_up_analysis(first_request))

        first_history = first_result.get("history") or []
        first_ids = [
            str(item.get("message_id"))
            for item in first_history
            if isinstance(item, dict) and item.get("role") == "assistant"
        ]
        assert first_ids and first_ids[-1].startswith("msg-")

        second_request = FollowUpRequest(
            question="第二轮追问",
            analysis_session_id=str(first_result.get("analysis_session_id") or ""),
            conversation_id=str(first_result.get("conversation_id") or ""),
            use_llm=False,
            analysis_context={
                "analysis_type": "log",
                "service_name": "query-service",
                "input_text": "database timeout",
                "result": {"overview": {"description": "数据库连接超时"}},
            },
        )

        with patch("api.ai.get_ai_session_store", return_value=mock_store), patch(
            "api.ai._is_llm_configured", return_value=False
        ):
            second_result = asyncio.run(follow_up_analysis(second_request))

        second_history = second_result.get("history") or []
        second_ids = {str(item.get("message_id")) for item in second_history if isinstance(item, dict)}
        assert any(item_id.startswith("msg-") for item_id in second_ids if item_id and item_id != "None")

    def test_follow_up_langchain_engine_calls_runtime(self, monkeypatch):
        """当 AI_FOLLOWUP_ENGINE=langchain 时，应走 LangChain 运行时分支。"""
        from api.ai import follow_up_analysis, FollowUpRequest
        import asyncio

        monkeypatch.setenv("AI_FOLLOWUP_ENGINE", "langchain")

        mock_store = Mock()
        mock_store.create_session.return_value = SimpleNamespace(session_id="sess-followup-langchain-001")
        mock_store.get_session.return_value = None
        mock_store.get_messages.return_value = []
        mock_store.append_messages.return_value = False
        mock_store.update_session.return_value = True

        mock_runtime = AsyncMock(
            return_value={
                "answer": "结论：先检查 query-service 到 MySQL 的连接池。",
                "analysis_method": "langchain",
                "llm_timeout_fallback": False,
                "actions": [
                    {
                        "id": "langchain-act-1",
                        "priority": 1,
                        "title": "kubectl logs deploy/query-service -n islap --tail=50",
                        "action": "kubectl logs deploy/query-service -n islap --tail=50",
                        "expected_outcome": "确认错误是否持续出现",
                    }
                ],
            }
        )

        request = FollowUpRequest(
            question="按请求链路给出排查步骤",
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
        ), patch("api.ai.get_llm_service", return_value=Mock()), patch(
            "api.ai.run_followup_langchain",
            mock_runtime,
        ):
            result = asyncio.run(follow_up_analysis(request))

        assert result["analysis_method"] == "langchain"
        assert result["followup_engine"] == "langchain"
        assert isinstance(result.get("actions"), list)
        assert len(result.get("actions") or []) >= 1
        first_action = (result.get("actions") or [])[0]
        assert first_action.get("source") in {"langchain", "answer_command", "reflection"}
        assert "kubectl logs deploy/query-service -n islap --tail=50" in str(first_action.get("command", ""))
        mock_runtime.assert_awaited_once()
        runtime_kwargs = mock_runtime.await_args.kwargs
        assert runtime_kwargs["llm_requested"] is True
        assert runtime_kwargs["llm_enabled"] is True

        assistant_messages = [
            item
            for item in result.get("history", [])
            if isinstance(item, dict) and item.get("role") == "assistant"
        ]
        assert assistant_messages
        metadata = assistant_messages[-1].get("metadata") or {}
        assert metadata.get("followup_engine") == "langchain"
        assert isinstance(metadata.get("actions"), list)
        assert len(metadata.get("actions") or []) >= 1

    def test_follow_up_langchain_engine_passes_llm_requested_flag(self, monkeypatch):
        """LangChain 分支应把 use_llm 透传给运行时，便于统一降级策略。"""
        from api.ai import follow_up_analysis, FollowUpRequest
        import asyncio

        monkeypatch.setenv("AI_FOLLOWUP_ENGINE", "langchain")

        mock_store = Mock()
        mock_store.create_session.return_value = SimpleNamespace(session_id="sess-followup-langchain-002")
        mock_store.get_session.return_value = None
        mock_store.get_messages.return_value = []
        mock_store.append_messages.return_value = False
        mock_store.update_session.return_value = True

        mock_runtime = AsyncMock(
            return_value={
                "answer": "规则模式回答",
                "analysis_method": "rule-based",
                "llm_timeout_fallback": False,
            }
        )

        request = FollowUpRequest(
            question="继续分析",
            use_llm=False,
            analysis_context={
                "analysis_type": "log",
                "service_name": "query-service",
                "input_text": "database timeout",
                "result": {"overview": {"description": "数据库连接超时"}},
            },
        )

        with patch("api.ai.get_ai_session_store", return_value=mock_store), patch(
            "api.ai._is_llm_configured", return_value=True
        ), patch("api.ai.run_followup_langchain", mock_runtime):
            result = asyncio.run(follow_up_analysis(request))

        assert result["analysis_method"] == "rule-based"
        assert result["followup_engine"] == "langchain"
        mock_runtime.assert_awaited_once()
        assert mock_runtime.await_args.kwargs["llm_requested"] is False

    def test_follow_up_includes_long_term_memory_summary(self, monkeypatch):
        """追问响应应包含跨会话长期记忆摘要。"""
        from api.ai import follow_up_analysis, FollowUpRequest
        import asyncio

        monkeypatch.setenv("AI_FOLLOWUP_LONG_TERM_MEMORY_ENABLED", "true")

        mock_store = Mock()
        mock_store.create_session.return_value = SimpleNamespace(session_id="sess-followup-memory-001")
        mock_store.get_session.return_value = None
        mock_store.get_messages.return_value = [
            SimpleNamespace(role="assistant", content="历史建议：先检查连接池上限"),
        ]
        mock_store.list_sessions.return_value = [
            SimpleNamespace(
                session_id="sess-followup-memory-old-001",
                title="历史会话",
                summary_text="query-service 连接池耗尽",
                input_text="db timeout",
                service_name="query-service",
                trace_id="trace-old-001",
                updated_at="2026-03-12T10:00:00Z",
            )
        ]
        mock_store.append_messages.return_value = False
        mock_store.update_session.return_value = True

        request = FollowUpRequest(
            question="这个问题下一步怎么排查？",
            use_llm=False,
            analysis_context={
                "analysis_type": "log",
                "service_name": "query-service",
                "trace_id": "trace-new-001",
                "input_text": "database timeout",
                "result": {"overview": {"description": "数据库连接超时"}},
            },
        )

        with patch("api.ai.get_ai_session_store", return_value=mock_store), patch(
            "api.ai._is_llm_configured", return_value=False
        ):
            result = asyncio.run(follow_up_analysis(request))

        assert result["analysis_method"] == "rule-based"
        assert result["long_term_memory_enabled"] is True
        assert result["long_term_memory_hits"] >= 1
        assert "session=" in str(result.get("long_term_memory_summary"))

    def test_follow_up_auto_exec_readonly_actions(self, monkeypatch):
        """只读命令自动执行开关开启时，应回填 action_observations。"""
        from api.ai import follow_up_analysis, FollowUpRequest
        import asyncio

        monkeypatch.setenv("AI_FOLLOWUP_ENGINE", "langchain")
        monkeypatch.setenv("AI_FOLLOWUP_AUTO_EXEC_READONLY_ENABLED", "true")
        monkeypatch.setenv("AI_FOLLOWUP_AUTO_EXEC_READONLY_MAX_ACTIONS", "1")
        monkeypatch.setenv("AI_FOLLOWUP_COMMAND_EXEC_ENABLED", "true")

        mock_store = Mock()
        mock_store.create_session.return_value = SimpleNamespace(session_id="sess-followup-autoexec-001")
        mock_store.get_session.return_value = None
        mock_store.get_messages.return_value = []
        mock_store.append_messages.return_value = False
        mock_store.update_session.return_value = True

        mock_runtime = AsyncMock(
            return_value={
                "answer": "执行步骤：\n- P1 echo auto-followup-check",
                "analysis_method": "langchain",
                "llm_timeout_fallback": False,
                "actions": [
                    {
                        "id": "langchain-act-1",
                        "priority": 1,
                        "title": "echo auto-followup-check",
                        "action": "echo auto-followup-check",
                        "command": "echo auto-followup-check",
                        "command_spec": {
                            "tool": "generic_exec",
                            "args": {
                                "command_argv": ["echo", "auto-followup-check"],
                                "target_kind": "runtime_node",
                                "target_identity": "runtime:local",
                                "timeout_s": 20,
                            },
                        },
                        "expected_outcome": "输出 auto-followup-check",
                    }
                ],
            }
        )

        request = FollowUpRequest(
            question="自动执行只读动作并回填结果",
            use_llm=True,
            analysis_context={
                "analysis_type": "log",
                "service_name": "query-service",
                "input_text": "query timeout",
                "result": {"overview": {"description": "连接池告警"}},
            },
        )

        async def _fake_precheck_command(**kwargs):
            command = str(kwargs.get("command") or "")
            return {
                "status": "ok",
                "command": command,
                "command_type": "query",
                "risk_level": "low",
                "requires_write_permission": False,
                "requires_elevation": False,
                "dispatch_requires_template": False,
                "dispatch_degraded": False,
            }

        async def _fake_create_command_run(**kwargs):
            command = str(kwargs.get("command") or "")
            return {
                "status": "executed",
                "command": command,
                "command_type": "query",
                "risk_level": "low",
                "exit_code": 0,
                "duration_ms": 8,
                "stdout": "auto-followup-check",
                "stderr": "",
                "output_truncated": False,
                "timed_out": False,
            }

        with patch("api.ai.get_ai_session_store", return_value=mock_store), patch(
            "api.ai._is_llm_configured", return_value=True
        ), patch("api.ai.get_llm_service", return_value=Mock()), patch(
            "api.ai.run_followup_langchain",
            mock_runtime,
        ), patch(
            "ai.followup_orchestration_helpers.precheck_command",
            _fake_precheck_command,
        ), patch(
            "ai.followup_orchestration_helpers.create_command_run",
            _fake_create_command_run,
        ):
            result = asyncio.run(follow_up_analysis(request))

        observations = result.get("action_observations") or []
        assert observations
        assert observations[0].get("status") == "executed"
        assert observations[0].get("auto_executed") is False
        assert "auto-followup-check" in str(observations[0].get("stdout"))
        react_loop = result.get("react_loop") or {}
        assert isinstance(react_loop, dict)
        assert react_loop.get("phase") in {"finalized", "replan"}
        assert isinstance((react_loop.get("replan") or {}).get("next_actions"), list)

    def test_follow_up_auto_exec_readonly_can_be_disabled_per_request(self, monkeypatch):
        """请求级 auto_exec_readonly=false 时，不应触发自动执行。"""
        from api.ai import follow_up_analysis, FollowUpRequest
        import asyncio

        monkeypatch.setenv("AI_FOLLOWUP_ENGINE", "langchain")
        monkeypatch.setenv("AI_FOLLOWUP_AUTO_EXEC_READONLY_ENABLED", "true")
        monkeypatch.setenv("AI_FOLLOWUP_AUTO_EXEC_READONLY_MAX_ACTIONS", "1")
        monkeypatch.setenv("AI_FOLLOWUP_COMMAND_EXEC_ENABLED", "true")

        mock_store = Mock()
        mock_store.create_session.return_value = SimpleNamespace(session_id="sess-followup-autoexec-off-001")
        mock_store.get_session.return_value = None
        mock_store.get_messages.return_value = []
        mock_store.append_messages.return_value = False
        mock_store.update_session.return_value = True

        mock_runtime = AsyncMock(
            return_value={
                "answer": "执行步骤：\n- P1 echo auto-followup-check",
                "analysis_method": "langchain",
                "llm_timeout_fallback": False,
                "actions": [
                    {
                        "id": "langchain-act-1",
                        "priority": 1,
                        "title": "echo auto-followup-check",
                        "action": "echo auto-followup-check",
                        "expected_outcome": "输出 auto-followup-check",
                    }
                ],
            }
        )

        request = FollowUpRequest(
            question="禁用只读自动执行",
            use_llm=True,
            auto_exec_readonly=False,
            analysis_context={
                "analysis_type": "log",
                "service_name": "query-service",
                "input_text": "query timeout",
                "result": {"overview": {"description": "连接池告警"}},
            },
        )

        with patch("api.ai.get_ai_session_store", return_value=mock_store), patch(
            "api.ai._is_llm_configured", return_value=True
        ), patch("api.ai.get_llm_service", return_value=Mock()), patch(
            "api.ai.run_followup_langchain",
            mock_runtime,
        ), patch(
            "ai.followup_orchestration_helpers.precheck_command",
            new_callable=AsyncMock,
        ) as precheck_mock, patch(
            "ai.followup_orchestration_helpers.create_command_run",
            new_callable=AsyncMock,
        ) as create_mock:
            result = asyncio.run(follow_up_analysis(request))

        assert precheck_mock.call_count == 0
        assert create_mock.call_count == 0
        assert (result.get("action_observations") or []) == []

    def test_follow_up_auto_exec_skips_non_executable_actions_without_precheck(self, monkeypatch):
        """不可执行原始动作不会直接执行，但会生成结构化模板查询并进入自动执行链路。"""
        from api.ai import follow_up_analysis, FollowUpRequest
        import asyncio

        monkeypatch.setenv("AI_FOLLOWUP_ENGINE", "langchain")
        monkeypatch.setenv("AI_FOLLOWUP_AUTO_EXEC_READONLY_ENABLED", "true")
        monkeypatch.setenv("AI_FOLLOWUP_AUTO_EXEC_READONLY_MAX_ACTIONS", "1")
        monkeypatch.setenv("AI_FOLLOWUP_COMMAND_EXEC_ENABLED", "true")

        mock_store = Mock()
        mock_store.create_session.return_value = SimpleNamespace(session_id="sess-followup-semantic-001")
        mock_store.get_session.return_value = None
        mock_store.get_messages.return_value = []
        mock_store.append_messages.return_value = False
        mock_store.update_session.return_value = True

        mock_runtime = AsyncMock(
            return_value={
                "answer": "执行步骤：\n- P1 clickhouse-client --host<HOST>--port<PORT>--query \"SHOWCREATETABLElogs.traces\"",
                "analysis_method": "langchain",
                "llm_timeout_fallback": False,
                "actions": [
                    {
                        "id": "langchain-act-semantic-1",
                        "priority": 1,
                        "title": "查询 traces DDL",
                        "action": "查询 traces DDL",
                        "command": "clickhouse-client --host<HOST>--port<PORT>--query \"SHOWCREATETABLElogs.traces\"",
                        "command_type": "unknown",
                        "executable": False,
                        "reason": "命令包含占位符参数，需先补全具体值后再执行",
                    }
                ],
            }
        )

        request = FollowUpRequest(
            question="获取 traces DDL",
            use_llm=True,
            analysis_context={
                "analysis_type": "log",
                "service_name": "query-service",
                "input_text": "schema check",
                "result": {"overview": {"description": "need ddl"}},
            },
        )

        with patch("api.ai.get_ai_session_store", return_value=mock_store), patch(
            "api.ai._is_llm_configured", return_value=True
        ), patch("api.ai.get_llm_service", return_value=Mock()), patch(
            "api.ai.run_followup_langchain",
            mock_runtime,
        ), patch(
            "ai.followup_orchestration_helpers.precheck_command",
            new_callable=AsyncMock,
        ) as precheck_mock, patch(
            "ai.followup_orchestration_helpers.create_command_run",
            new_callable=AsyncMock,
        ) as create_mock:
            precheck_mock.return_value = {
                "status": "ok",
                "command": "kubectl -n islap logs deploy/query-service --since=15m --tail=200",
                "command_type": "query",
                "risk_level": "low",
                "requires_write_permission": False,
                "requires_elevation": False,
                "dispatch_requires_template": False,
                "dispatch_degraded": False,
            }
            create_mock.return_value = {
                "status": "executed",
                "command": "kubectl -n islap logs deploy/query-service --since=15m --tail=200",
                "command_type": "query",
                "risk_level": "low",
                "exit_code": 0,
                "duration_ms": 8,
                "stdout": "ok\n",
                "stderr": "",
                "output_truncated": False,
                "timed_out": False,
            }
            result = asyncio.run(follow_up_analysis(request))

        observations = result.get("action_observations") or []
        assert observations
        precheck_commands = [
            str(call.kwargs.get("command") or "")
            for call in precheck_mock.await_args_list
        ]
        assert precheck_commands
        assert all("<HOST>" not in item and "<PORT>" not in item for item in precheck_commands)
        assert all("SHOWCREATETABLE" not in item for item in precheck_commands)
        react_loop = result.get("react_loop") or {}
        next_actions = (react_loop.get("replan") or {}).get("next_actions") or []
        if next_actions:
            assert any("结构化查询命令模板" in str(item) or "可直接执行（已生成 command_spec）" in str(item) for item in next_actions)
            assert all("请先补全 command_spec" not in str(item) for item in next_actions)
        else:
            execute = react_loop.get("execute") if isinstance(react_loop.get("execute"), dict) else {}
            assert int(execute.get("executed_success") or 0) > 0
        assert precheck_mock.call_count > 0

    def test_follow_up_auto_exec_react_loop_multiple_iterations(self, monkeypatch):
        """当单轮动作上限较低时，应在同次请求内按迭代补执行剩余查询动作。"""
        from api.ai import follow_up_analysis, FollowUpRequest
        import asyncio

        monkeypatch.setenv("AI_FOLLOWUP_ENGINE", "langchain")
        monkeypatch.setenv("AI_FOLLOWUP_AUTO_EXEC_READONLY_ENABLED", "true")
        monkeypatch.setenv("AI_FOLLOWUP_AUTO_EXEC_READONLY_MAX_ACTIONS", "1")
        monkeypatch.setenv("AI_FOLLOWUP_REACT_MAX_ITERATIONS", "2")
        monkeypatch.setenv("AI_FOLLOWUP_COMMAND_EXEC_ENABLED", "true")

        mock_store = Mock()
        mock_store.create_session.return_value = SimpleNamespace(session_id="sess-followup-autoexec-iter-001")
        mock_store.get_session.return_value = None
        mock_store.get_messages.return_value = []
        mock_store.append_messages.return_value = False
        mock_store.update_session.return_value = True

        mock_runtime = AsyncMock(
            return_value={
                "answer": "执行步骤：\n- P1 echo step-one\n- P2 echo step-two",
                "analysis_method": "langchain",
                "llm_timeout_fallback": False,
                "actions": [
                    {
                        "id": "langchain-act-1",
                        "priority": 1,
                        "title": "echo step-one",
                        "action": "echo step-one",
                        "expected_outcome": "输出 step-one",
                    },
                    {
                        "id": "langchain-act-2",
                        "priority": 2,
                        "title": "echo step-two",
                        "action": "echo step-two",
                        "expected_outcome": "输出 step-two",
                    },
                ],
            }
        )

        request = FollowUpRequest(
            question="多轮自动执行测试",
            use_llm=True,
            analysis_context={
                "analysis_type": "log",
                "service_name": "query-service",
                "input_text": "query timeout",
                "result": {"overview": {"description": "连接池告警"}},
            },
        )

        async def _fake_precheck_command(**kwargs):
            command = str(kwargs.get("command") or "")
            return {
                "status": "ok",
                "command": command,
                "command_type": "query",
                "risk_level": "low",
                "requires_write_permission": False,
                "requires_elevation": False,
                "dispatch_requires_template": False,
                "dispatch_degraded": False,
            }

        async def _fake_create_command_run(**kwargs):
            command = str(kwargs.get("command") or "")
            return {
                "status": "executed",
                "command": command,
                "command_type": "query",
                "risk_level": "low",
                "exit_code": 0,
                "duration_ms": 8,
                "stdout": "step-two" if "step-two" in command else "step-one",
                "stderr": "",
                "output_truncated": False,
                "timed_out": False,
            }

        with patch("api.ai.get_ai_session_store", return_value=mock_store), patch(
            "api.ai._is_llm_configured", return_value=True
        ), patch("api.ai.get_llm_service", return_value=Mock()), patch(
            "api.ai.run_followup_langchain",
            mock_runtime,
        ), patch(
            "ai.followup_orchestration_helpers.precheck_command",
            _fake_precheck_command,
        ), patch(
            "ai.followup_orchestration_helpers.create_command_run",
            _fake_create_command_run,
        ):
            result = asyncio.run(follow_up_analysis(request))

        observations = result.get("action_observations") or []
        observed_commands = {str(item.get("command") or "") for item in observations if isinstance(item, dict)}
        assert "echo step-one" in observed_commands
        assert "echo step-two" in observed_commands
        react_iterations = result.get("react_iterations") or []
        assert len(react_iterations) >= 2

    def test_follow_up_show_thought_false_hides_thoughts_in_final_payload(self):
        """show_thought=False 时，不应在最终 payload/history metadata 暴露 thoughts。"""
        from api.ai import follow_up_analysis, FollowUpRequest
        import asyncio

        mock_store = Mock()
        mock_store.create_session.return_value = SimpleNamespace(session_id="sess-followup-hide-thought-001")
        mock_store.get_session.return_value = None
        mock_store.get_messages.return_value = []
        mock_store.append_messages.return_value = False
        mock_store.update_session.return_value = True

        request = FollowUpRequest(
            question="请继续分析并给出建议",
            use_llm=False,
            show_thought=False,
            analysis_context={
                "analysis_type": "log",
                "service_name": "query-service",
                "input_text": "database timeout",
                "result": {"overview": {"description": "数据库连接超时"}},
            },
        )

        with patch("api.ai.get_ai_session_store", return_value=mock_store), patch(
            "api.ai._is_llm_configured", return_value=False
        ):
            result = asyncio.run(follow_up_analysis(request))

        assert result.get("thoughts") == []
        history = result.get("history") or []
        assistants = [
            item for item in history
            if isinstance(item, dict) and item.get("role") == "assistant"
        ]
        assert assistants
        metadata = assistants[-1].get("metadata") or {}
        assert metadata.get("thoughts") == []

    def test_follow_up_stream_show_thought_false_does_not_emit_thought_event(self, monkeypatch):
        """show_thought=False 时，SSE 不应发送 event: thought。"""
        from api.ai import follow_up_analysis_stream, FollowUpRequest
        import asyncio

        monkeypatch.setenv("AI_FOLLOWUP_AUTO_EXEC_READONLY_ENABLED", "false")

        mock_store = Mock()
        mock_store.create_session.return_value = SimpleNamespace(session_id="sess-followup-stream-hide-thought-001")
        mock_store.get_session.return_value = None
        mock_store.get_messages.return_value = []
        mock_store.append_messages.return_value = False
        mock_store.update_session.return_value = True

        request = FollowUpRequest(
            question="流式回答测试（无 thought）",
            use_llm=False,
            show_thought=False,
            analysis_context={
                "analysis_type": "log",
                "service_name": "query-service",
                "input_text": "database timeout",
                "result": {"overview": {"description": "数据库连接超时"}},
            },
        )

        async def _collect_stream_text():
            with patch("api.ai.get_ai_session_store", return_value=mock_store), patch(
                "api.ai._is_llm_configured", return_value=False
            ):
                response = await follow_up_analysis_stream(request)
                chunks = []
                async for chunk in response.body_iterator:
                    text = chunk.decode() if isinstance(chunk, bytes) else str(chunk)
                    chunks.append(text)
                    if "event: final" in text:
                        break
                return response.media_type, "".join(chunks)

        media_type, stream_text = asyncio.run(_collect_stream_text())
        assert media_type == "text/event-stream"
        assert "event: thought" not in stream_text
        assert "event: final" in stream_text

    def test_follow_up_stream_returns_sse_with_final_event(self, monkeypatch):
        """流式接口应输出 SSE 事件并包含 final。"""
        from api.ai import follow_up_analysis_stream, FollowUpRequest
        import asyncio

        monkeypatch.setenv("AI_FOLLOWUP_AUTO_EXEC_READONLY_ENABLED", "false")

        mock_store = Mock()
        mock_store.create_session.return_value = SimpleNamespace(session_id="sess-followup-stream-001")
        mock_store.get_session.return_value = None
        mock_store.get_messages.return_value = []
        mock_store.append_messages.return_value = False
        mock_store.update_session.return_value = True

        request = FollowUpRequest(
            question="流式回答测试",
            use_llm=False,
            analysis_context={
                "analysis_type": "log",
                "service_name": "query-service",
                "input_text": "database timeout",
                "result": {"overview": {"description": "数据库连接超时"}},
            },
        )

        async def _collect_stream_text():
            with patch("api.ai.get_ai_session_store", return_value=mock_store), patch(
                "api.ai._is_llm_configured", return_value=False
            ):
                response = await follow_up_analysis_stream(request)
                chunks = []
                async for chunk in response.body_iterator:
                    text = chunk.decode() if isinstance(chunk, bytes) else str(chunk)
                    chunks.append(text)
                    if "event: final" in text:
                        break
                return response.media_type, "".join(chunks)

        media_type, stream_text = asyncio.run(_collect_stream_text())
        assert media_type == "text/event-stream"
        assert "event: plan" in stream_text
        assert "event: final" in stream_text
        assert "\"analysis_session_id\"" in stream_text

    def test_follow_up_react_memory_is_loaded_into_next_round(self, monkeypatch):
        """上一轮闭环 replan 动作应并入下一轮 reflection.next_actions。"""
        from api.ai import follow_up_analysis, FollowUpRequest
        import asyncio

        monkeypatch.setenv("AI_FOLLOWUP_ENGINE", "langchain")
        monkeypatch.setenv("AI_FOLLOWUP_AUTO_EXEC_READONLY_ENABLED", "true")
        monkeypatch.setenv("AI_FOLLOWUP_AUTO_EXEC_READONLY_MAX_ACTIONS", "1")
        monkeypatch.setenv("AI_FOLLOWUP_COMMAND_EXEC_ENABLED", "true")

        class _StoreStub:
            def __init__(self):
                self.sessions = {}
                self.messages = {}

            def create_session(self, source: str = "", session_id: str = "", **kwargs):
                sid = session_id or f"sess-store-{len(self.sessions) + 1}"
                self.sessions[sid] = SimpleNamespace(session_id=sid, title=kwargs.get("title", ""), **kwargs)
                self.messages.setdefault(sid, [])
                return self.sessions[sid]

            def get_session(self, session_id: str):
                return self.sessions.get(session_id)

            def update_session(self, session_id: str, **kwargs):
                existing = self.sessions.get(session_id)
                if not existing:
                    return False
                payload = dict(existing.__dict__)
                payload.update(kwargs)
                self.sessions[session_id] = SimpleNamespace(**payload)
                return True

            def append_messages(self, session_id: str, messages):
                bucket = self.messages.setdefault(session_id, [])
                for msg in messages:
                    item = msg if isinstance(msg, dict) else {}
                    bucket.append(
                        SimpleNamespace(
                            session_id=session_id,
                            message_id=item.get("message_id"),
                            role=item.get("role"),
                            content=item.get("content"),
                            created_at=item.get("timestamp", "2026-03-16T00:00:00Z"),
                            metadata=item.get("metadata") if isinstance(item.get("metadata"), dict) else {},
                        )
                    )
                return True

            def get_messages(self, session_id: str, limit: int = 200):
                return list(self.messages.get(session_id, []))[-max(1, int(limit)):]

            def list_sessions(self, **kwargs):
                return []

        store = _StoreStub()
        async def _direct_run_blocking(func, *args, **kwargs):
            return func(*args, **kwargs)

        mock_runtime = AsyncMock(
            return_value={
                "answer": "执行步骤：\n- P1 echo health-check",
                "analysis_method": "langchain",
                "llm_timeout_fallback": False,
                "actions": [
                    {
                        "id": "langchain-act-1",
                        "priority": 1,
                        "title": "echo health-check",
                        "action": "echo health-check",
                        "expected_outcome": "输出 health-check",
                    }
                ],
            }
        )

        first_request = FollowUpRequest(
            question="第一轮排查",
            use_llm=True,
            analysis_context={
                "analysis_type": "log",
                "service_name": "query-service",
                "input_text": "query timeout",
                "result": {"overview": {"description": "连接池告警"}},
            },
        )

        async def _fake_precheck_command(**kwargs):
            command = str(kwargs.get("command") or "")
            return {
                "status": "ok",
                "command": command,
                "command_type": "query",
                "risk_level": "low",
                "requires_write_permission": False,
                "requires_elevation": False,
                "dispatch_requires_template": False,
                "dispatch_degraded": False,
            }

        async def _fake_create_command_run(**kwargs):
            command = str(kwargs.get("command") or "")
            return {
                "status": "failed",
                "command": command,
                "command_type": "query",
                "risk_level": "low",
                "exit_code": 1,
                "duration_ms": 8,
                "stdout": "",
                "stderr": "health-check failed",
                "output_truncated": False,
                "timed_out": False,
                "message": "health-check failed",
            }

        with patch("api.ai.get_ai_session_store", return_value=store), patch(
            "api.ai._is_llm_configured", return_value=True
        ), patch("api.ai.get_llm_service", return_value=Mock()), patch(
            "api.ai.run_followup_langchain",
            mock_runtime,
        ), patch(
            "api.ai._run_blocking",
            new=_direct_run_blocking,
        ), patch(
            "ai.followup_orchestration_helpers.precheck_command",
            _fake_precheck_command,
        ), patch(
            "ai.followup_orchestration_helpers.create_command_run",
            _fake_create_command_run,
        ):
            first_result = asyncio.run(follow_up_analysis(first_request))

        assert (first_result.get("react_loop") or {}).get("replan", {}).get("needed") is True

        second_request = FollowUpRequest(
            question="第二轮继续",
            analysis_session_id=str(first_result.get("analysis_session_id") or ""),
            conversation_id=str(first_result.get("conversation_id") or ""),
            use_llm=False,
            analysis_context={
                "analysis_type": "log",
                "service_name": "query-service",
                "input_text": "query timeout",
                "result": {"overview": {"description": "连接池告警"}},
            },
        )

        with patch("api.ai.get_ai_session_store", return_value=store), patch(
            "api.ai._is_llm_configured", return_value=True
        ), patch("api.ai.run_followup_langchain", mock_runtime), patch(
            "api.ai._run_blocking",
            new=_direct_run_blocking,
        ):
            second_result = asyncio.run(follow_up_analysis(second_request))

        reflection_next_actions = (second_result.get("reflection") or {}).get("next_actions") or []
        assert any("echo health-check" in str(item) for item in reflection_next_actions)
        assert int((second_result.get("react_memory") or {}).get("hits") or 0) >= 1
        second_actions = second_result.get("actions") or []
        assert second_actions
        assert "echo health-check" in str(second_actions[0].get("command"))

    def test_follow_up_runtime_thread_memory_is_loaded_into_next_round(self, monkeypatch):
        """同一 runtime 线程中的失败动作、恢复和用户约束应进入下一轮上下文。"""
        from api.ai import FollowUpRequest, follow_up_analysis
        from ai.agent_runtime.service import AgentRuntimeService
        import asyncio

        runtime_service = AgentRuntimeService(storage_adapter=None)
        run = runtime_service.create_run(
            session_id="sess-runtime-memory-001",
            question="第一轮 runtime 排查",
            analysis_context={"analysis_type": "log", "service_name": "query-service"},
            runtime_options={"conversation_id": "conv-runtime-memory-001"},
        )
        runtime_service._update_run_summary(  # noqa: SLF001
            run,
            last_command_status="timed_out",
            last_command="kubectl -n islap exec -i clickhouse-0 -- clickhouse-client --query \"SELECT count() FROM otel_logs LIMIT 1000\"",
            last_command_purpose="查询慢日志窗口",
            last_command_error_detail="mock timeout",
            last_timeout_recovery_variant={"message": "将 LIMIT 从 1000 收敛到 200", "match_key": "limit-200"},
            last_user_input={
                "business_answer_text": "先看最近 15 分钟",
                "text": "先看最近 15 分钟",
            },
        )

        mock_store = Mock()
        mock_store.get_session.return_value = SimpleNamespace(session_id="sess-runtime-memory-001")
        mock_store.get_messages.return_value = []
        mock_store.get_recent_assistant_messages_for_react.return_value = []
        mock_store.append_messages.return_value = False
        mock_store.update_session.return_value = True
        mock_store.list_sessions.return_value = []

        async def _direct_run_blocking(func, *args, **kwargs):
            return func(*args, **kwargs)

        request = FollowUpRequest(
            question="第二轮继续",
            analysis_session_id="sess-runtime-memory-001",
            conversation_id="conv-runtime-memory-001",
            use_llm=False,
            analysis_context={
                "analysis_type": "log",
                "service_name": "query-service",
                "input_text": "query timeout",
                "result": {"overview": {"description": "连接池告警"}},
            },
        )

        with patch("api.ai.get_ai_session_store", return_value=mock_store), patch(
            "api.ai.get_agent_runtime_service", return_value=runtime_service
        ), patch(
            "api.ai._run_blocking",
            new=_direct_run_blocking,
        ), patch(
            "api.ai._is_llm_configured", return_value=False
        ):
            result = asyncio.run(follow_up_analysis(request))

        runtime_thread_memory = result.get("runtime_thread_memory") or {}
        assert int(runtime_thread_memory.get("hits") or 0) >= 1
        assert "查询慢日志窗口 状态=timed_out" in str(runtime_thread_memory.get("summary") or "")
        assert "先看最近 15 分钟" in str(runtime_thread_memory.get("summary") or "")
        reflection_next_actions = (result.get("reflection") or {}).get("next_actions") or []
        assert any("查询慢日志窗口 状态=timed_out" in str(item) for item in reflection_next_actions)


class TestFollowUpActionEndpoint:
    """测试追问回答转行动草案接口。"""

    @pytest.fixture(autouse=True)
    def _patch_run_blocking(self):
        """单测中直连同步调用，避免环境层 asyncio 线程池阻塞导致用例挂起。"""

        async def _direct_run_blocking(func, *args, **kwargs):
            return func(*args, **kwargs)

        with patch("api.ai._run_blocking", new=_direct_run_blocking):
            yield

    def test_create_followup_action_supports_dict_message_shape(self):
        """当存储层返回 dict 形态消息时，应正常生成 action，不应 500。"""
        from api.ai import create_followup_action, FollowUpActionRequest
        import asyncio

        mock_store = Mock()
        mock_store.get_session_with_messages.return_value = {
            "session": {
                "session_id": "sess-action-001",
                "service_name": "query-service",
                "summary_text": "query timeout",
                "context": {},
            },
            "messages": [],
        }
        mock_store.get_message_by_id.return_value = {
            "role": "assistant",
            "content": "- 先检查连接池\n- 再检查慢查询",
        }
        mock_store.update_session.return_value = True

        request = FollowUpActionRequest(action_type="ticket", title="排障工单")
        with patch("api.ai.get_ai_session_store", return_value=mock_store):
            result = asyncio.run(create_followup_action("sess-action-001", "msg-001", request))

        assert result["status"] == "ok"
        assert result["session_id"] == "sess-action-001"
        assert result["message_id"] == "msg-001"
        assert result["action"]["type"] == "ticket"
        mock_store.update_session.assert_called_once()

    def test_create_followup_action_rejects_empty_assistant_content(self):
        """assistant 消息内容为空时应返回 400，避免生成空草案。"""
        from api.ai import create_followup_action, FollowUpActionRequest
        from fastapi import HTTPException
        import asyncio

        mock_store = Mock()
        mock_store.get_session_with_messages.return_value = {
            "session": {"session_id": "sess-action-002", "context": {}},
            "messages": [],
        }
        mock_store.get_message_by_id.return_value = {
            "role": "assistant",
            "content": "",
        }

        request = FollowUpActionRequest(action_type="runbook")
        with patch("api.ai.get_ai_session_store", return_value=mock_store):
            with pytest.raises(HTTPException) as exc_info:
                asyncio.run(create_followup_action("sess-action-002", "msg-002", request))

        assert exc_info.value.status_code == 400
        assert "content is empty" in str(exc_info.value.detail)


class TestFollowUpCommandExecuteEndpoint:
    """测试追问命令执行接口。"""

    @pytest.fixture(autouse=True)
    def _patch_run_blocking(self):
        """单测中直连同步调用，避免环境层 asyncio 线程池阻塞导致用例挂起。"""

        async def _direct_run_blocking(func, *args, **kwargs):
            return func(*args, **kwargs)

        with patch("api.ai._run_blocking", new=_direct_run_blocking):
            yield

    @pytest.fixture(autouse=True)
    def _patch_controlled_exec_gateway(self):
        """追问命令接口统一走受控执行网关；单测中 mock 网关返回。"""
        from ai.followup_command import _normalize_followup_command_line, _resolve_followup_command_meta

        ticket_store: dict[str, dict[str, str]] = {}
        run_seq = {"value": 0}
        ticket_seq = {"value": 0}

        async def _fake_precheck_controlled_command(**kwargs):
            command = _normalize_followup_command_line(str(kwargs.get("command") or ""))
            purpose = str(kwargs.get("purpose") or "")
            command_meta, _ = _resolve_followup_command_meta(command)
            payload = {
                "status": "ok",
                "session_id": str(kwargs.get("session_id") or ""),
                "message_id": str(kwargs.get("message_id") or ""),
                "action_id": str(kwargs.get("action_id") or ""),
                "command": command,
                "purpose": purpose,
                "command_type": str(command_meta.get("command_type") or "unknown"),
                "risk_level": str(command_meta.get("risk_level") or "high"),
                "requires_write_permission": bool(command_meta.get("requires_write_permission")),
                "requires_elevation": bool(command_meta.get("requires_write_permission")),
                "requires_confirmation": False,
                "message": str(command_meta.get("reason") or ""),
            }
            if not bool(command_meta.get("supported")):
                payload["status"] = "permission_required"
                payload["requires_elevation"] = False
                return payload
            if bool(command_meta.get("requires_write_permission")):
                ticket_seq["value"] += 1
                ticket_id = f"exec-ticket-test-{ticket_seq['value']:04d}"
                ticket_store[ticket_id] = {
                    "session_id": payload["session_id"],
                    "message_id": payload["message_id"],
                    "action_id": payload["action_id"],
                    "command": command,
                }
                payload["status"] = "elevation_required"
                payload["requires_confirmation"] = True
                payload["requires_elevation"] = True
                payload["confirmation_ticket"] = ticket_id
            return payload

        async def _fake_execute_controlled_command(**kwargs):
            precheck = await _fake_precheck_controlled_command(**kwargs)
            status = str(precheck.get("status") or "").lower()
            if status == "permission_required":
                return precheck
            if status == "elevation_required":
                if not bool(kwargs.get("confirmed")) or not bool(kwargs.get("elevated")):
                    return precheck
                provided_ticket = str(kwargs.get("confirmation_ticket") or "")
                ticket_payload = ticket_store.pop(provided_ticket, None)
                if not ticket_payload or str(ticket_payload.get("command")) != str(precheck.get("command")):
                    failed = dict(precheck)
                    failed["status"] = "confirmation_required"
                    failed["message"] = "confirmation ticket invalid: ticket_not_found"
                    return failed
            run_seq["value"] += 1
            command = str(precheck.get("command") or "")
            stdout = ""
            if command.startswith("echo "):
                stdout = f"{command.split(' ', 1)[1]}\n"
            return {
                **precheck,
                "status": "executed",
                "run_id": f"cmdrun-test-{run_seq['value']:04d}",
                "exit_code": 0,
                "duration_ms": 12,
                "stdout": stdout,
                "stderr": "",
                "output_truncated": False,
                "timed_out": False,
            }

        with patch("api.ai.precheck_controlled_command", new=_fake_precheck_controlled_command), patch(
            "api.ai.execute_controlled_command",
            new=_fake_execute_controlled_command,
        ):
            yield

    def _build_store(self, command_block: str, metadata: dict | None = None):
        """
        使用普通同步函数 stub，避免 Mock 方法在 asyncio.to_thread 中多次调用时偶发阻塞。
        """

        class _StoreStub:
            def __init__(self):
                self.context = {}

            def get_session_with_messages(self, session_id: str):
                return {
                    "session": {"session_id": session_id, "context": dict(self.context)},
                    "messages": [],
                }

            def get_message_by_id(self, session_id: str, message_id: str):
                return {
                    "role": "assistant",
                    "content": command_block,
                    "metadata": metadata or {},
                }

            def update_session(self, session_id: str, **changes):
                context = changes.get("context")
                self.context = dict(context) if isinstance(context, dict) else {}
                return True

        return _StoreStub()

    def _build_command_spec(self, command: str, timeout_seconds: int = 10, *, step_id: str = "manual-step-1") -> dict:
        safe_command = str(command or "").strip()
        safe_timeout = max(3, min(180, int(timeout_seconds or 10)))
        return {
            "tool": "generic_exec",
            "args": {
                "command": safe_command,
                "timeout_s": safe_timeout,
                "target_kind": "runtime_node",
                "target_identity": "runtime:local",
            },
            "command": safe_command,
            "timeout_s": safe_timeout,
            "target_kind": "runtime_node",
            "target_identity": "runtime:local",
            "step_id": step_id,
        }

    def _build_command_request(self, **kwargs):
        from api.ai import FollowUpCommandExecuteRequest

        payload = dict(kwargs)
        command = str(payload.get("command") or "").strip()
        timeout_seconds = max(3, min(180, int(payload.get("timeout_seconds") or 10)))
        payload["command"] = command
        payload["timeout_seconds"] = timeout_seconds
        if not isinstance(payload.get("command_spec"), dict):
            payload["command_spec"] = self._build_command_spec(command, timeout_seconds=timeout_seconds)
        return FollowUpCommandExecuteRequest(**payload)

    def _assert_blocked_command_spec(self, result: dict, *, reason_fragment: str | None = None):
        assert str(result.get("status")) == "blocked"
        error_payload = result.get("error") if isinstance(result.get("error"), dict) else {}
        assert str(error_payload.get("code") or "") == "missing_or_invalid_command_spec"
        if reason_fragment:
            reason_text = " ".join(
                [
                    str(error_payload.get("reason") or ""),
                    str(error_payload.get("message") or ""),
                ]
            ).lower()
            assert reason_fragment.lower() in reason_text

    def test_execute_followup_command_requires_confirmation(self):
        from api.ai import execute_followup_command, FollowUpCommandExecuteRequest
        import asyncio

        mock_store = self._build_store("```bash\nkubectl logs deploy/query-service -n islap --tail=20\n```")
        request = self._build_command_request(
            command="kubectl logs deploy/query-service -n islap --tail=20",
            confirmed=False,
            timeout_seconds=10,
        )

        with patch("api.ai.get_ai_session_store", return_value=mock_store):
            result = asyncio.run(execute_followup_command("sess-cmd-001", "msg-cmd-001", request))

        assert result["status"] == "confirmation_required"
        assert result["command_type"] == "query"
        assert result["requires_confirmation"] is True

    def test_execute_followup_command_write_requires_elevation(self):
        from api.ai import execute_followup_command, FollowUpCommandExecuteRequest
        import asyncio

        mock_store = self._build_store("```bash\nkubectl apply -f fix.yaml\n```")
        request = self._build_command_request(
            command="kubectl apply -f fix.yaml",
            confirmed=True,
            timeout_seconds=10,
        )

        with patch("api.ai.get_ai_session_store", return_value=mock_store):
            result = asyncio.run(execute_followup_command("sess-cmd-001", "msg-cmd-002", request))

        assert result["status"] == "elevation_required"
        assert result["requires_write_permission"] is True
        assert result["requires_elevation"] is True

    def test_execute_followup_command_write_requires_elevation_when_switch_enabled(self):
        from api.ai import execute_followup_command, FollowUpCommandExecuteRequest
        import asyncio

        mock_store = self._build_store("```bash\nkubectl delete pod test-pod -n islap\n```")
        request = self._build_command_request(
            command="kubectl delete pod test-pod -n islap",
            confirmed=False,
            elevated=False,
            timeout_seconds=10,
        )

        with patch("api.ai.get_ai_session_store", return_value=mock_store), patch.dict(
            "os.environ",
            {"AI_FOLLOWUP_COMMAND_WRITE_ENABLED": "true"},
            clear=False,
        ):
            result = asyncio.run(execute_followup_command("sess-cmd-001", "msg-cmd-002b", request))

        assert result["status"] == "elevation_required"
        assert result["requires_write_permission"] is True
        assert result["requires_elevation"] is True

    def test_execute_followup_command_write_runs_with_elevation_and_confirmed(self):
        from api.ai import execute_followup_command, FollowUpCommandExecuteRequest
        import asyncio

        mock_store = self._build_store("```bash\nkubectl delete pod test-pod -n islap\n```")

        with patch("api.ai.get_ai_session_store", return_value=mock_store), patch.dict(
            "os.environ",
            {"AI_FOLLOWUP_COMMAND_WRITE_ENABLED": "true"},
            clear=False,
        ):
            precheck = asyncio.run(
                execute_followup_command(
                    "sess-cmd-001",
                    "msg-cmd-002c",
                    self._build_command_request(
                        command="kubectl delete pod test-pod -n islap",
                        confirmed=False,
                        elevated=True,
                        timeout_seconds=10,
                    ),
                )
            )
            request = self._build_command_request(
                command="kubectl delete pod test-pod -n islap",
                confirmed=True,
                elevated=True,
                confirmation_ticket=str(precheck.get("confirmation_ticket") or ""),
                timeout_seconds=10,
            )
            result = asyncio.run(execute_followup_command("sess-cmd-001", "msg-cmd-002c", request))

        assert result["status"] == "executed"
        assert result["requires_write_permission"] is True

    def test_execute_followup_command_runs_read_query(self):
        from api.ai import execute_followup_command, FollowUpCommandExecuteRequest
        import asyncio

        mock_store = self._build_store("```bash\necho hello-logoscope\n```")

        with patch("api.ai.get_ai_session_store", return_value=mock_store):
            precheck = asyncio.run(
                execute_followup_command(
                    "sess-cmd-001",
                    "msg-cmd-003",
                    self._build_command_request(
                        command="echo hello-logoscope",
                        confirmed=False,
                        timeout_seconds=10,
                    ),
                )
            )
            request = self._build_command_request(
                command="echo hello-logoscope",
                confirmed=True,
                confirmation_ticket=str(precheck.get("confirmation_ticket") or ""),
                timeout_seconds=10,
            )
            result = asyncio.run(execute_followup_command("sess-cmd-001", "msg-cmd-003", request))

        assert result["status"] == "executed"
        assert result["exit_code"] == 0
        assert "hello-logoscope" in result["stdout"]

    def test_execute_followup_command_supports_plain_step_command_text(self):
        from api.ai import execute_followup_command, FollowUpCommandExecuteRequest
        import asyncio

        mock_store = self._build_store("执行步骤：\n- P1 kubectl logs deploy/query-service -n islap --tail=20")
        request = self._build_command_request(
            command="kubectl logs   deploy/query-service -n islap --tail=20",
            confirmed=False,
            timeout_seconds=10,
        )

        with patch("api.ai.get_ai_session_store", return_value=mock_store):
            result = asyncio.run(execute_followup_command("sess-cmd-001", "msg-cmd-004", request))

        assert result["status"] == "confirmation_required"
        assert result["command_type"] == "query"
        assert result["requires_confirmation"] is True

    def test_execute_followup_command_supports_metadata_actions_source(self):
        from api.ai import execute_followup_command, FollowUpCommandExecuteRequest
        import asyncio

        mock_store = self._build_store(
            "执行计划见下方动作卡片",
            metadata={
                "actions": [
                    {
                        "title": "排查最近日志",
                        "command": "kubectl logs deploy/query-service -n islap --tail=30",
                    }
                ]
            },
        )
        request = self._build_command_request(
            command="kubectl logs deploy/query-service -n islap --tail=30",
            confirmed=False,
            timeout_seconds=10,
        )

        with patch("api.ai.get_ai_session_store", return_value=mock_store):
            result = asyncio.run(execute_followup_command("sess-cmd-001", "msg-cmd-005", request))

        assert result["status"] == "confirmation_required"
        assert result["command_type"] == "query"
        assert result["requires_confirmation"] is True

    def test_execute_followup_command_kubectl_namespace_flag_before_get_is_query(self):
        from api.ai import execute_followup_command, FollowUpCommandExecuteRequest
        import asyncio

        mock_store = self._build_store("```bash\nkubectl -n islap get pods -o wide\n```")
        request = self._build_command_request(
            command="kubectl -n islap get pods -o wide",
            confirmed=False,
            timeout_seconds=10,
        )

        with patch("api.ai.get_ai_session_store", return_value=mock_store):
            result = asyncio.run(execute_followup_command("sess-cmd-001", "msg-cmd-005b", request))

        assert result["status"] == "confirmation_required"
        assert result["command_type"] == "query"
        assert result["requires_write_permission"] is False

    def test_execute_followup_command_kubectl_rollout_status_is_query(self):
        from api.ai import execute_followup_command, FollowUpCommandExecuteRequest
        import asyncio

        mock_store = self._build_store("```bash\nkubectl rollout status deployment/frontend -n islap\n```")
        request = self._build_command_request(
            command="kubectl rollout status deployment/frontend -n islap",
            confirmed=False,
            timeout_seconds=10,
        )

        with patch("api.ai.get_ai_session_store", return_value=mock_store):
            result = asyncio.run(execute_followup_command("sess-cmd-001", "msg-cmd-005c", request))

        assert result["status"] == "confirmation_required"
        assert result["command_type"] == "query"
        assert result["requires_write_permission"] is False

    def test_execute_followup_command_kubectl_rollout_restart_requires_write_permission(self):
        from api.ai import execute_followup_command, FollowUpCommandExecuteRequest
        import asyncio

        mock_store = self._build_store("```bash\nkubectl rollout restart deployment/frontend -n islap\n```")
        request = self._build_command_request(
            command="kubectl rollout restart deployment/frontend -n islap",
            confirmed=True,
            timeout_seconds=10,
        )

        with patch("api.ai.get_ai_session_store", return_value=mock_store):
            result = asyncio.run(execute_followup_command("sess-cmd-001", "msg-cmd-005d", request))

        assert result["status"] == "elevation_required"
        assert result["command_type"] == "repair"
        assert result["requires_write_permission"] is True

    def test_execute_followup_command_curl_request_post_requires_write_permission(self):
        from api.ai import execute_followup_command, FollowUpCommandExecuteRequest
        import asyncio

        mock_store = self._build_store("```bash\ncurl --request POST https://example.com/api/v1/restart\n```")
        request = self._build_command_request(
            command="curl --request POST https://example.com/api/v1/restart",
            confirmed=True,
            timeout_seconds=10,
        )

        with patch("api.ai.get_ai_session_store", return_value=mock_store):
            result = asyncio.run(execute_followup_command("sess-cmd-001", "msg-cmd-006", request))

        assert result["status"] == "elevation_required"
        assert result["command_type"] == "repair"
        assert result["requires_write_permission"] is True

    def test_execute_followup_command_curl_data_payload_requires_write_permission(self):
        from api.ai import execute_followup_command, FollowUpCommandExecuteRequest
        import asyncio

        mock_store = self._build_store("```bash\ncurl -d '{\"op\":\"restart\"}' https://example.com/api/v1/action\n```")
        request = self._build_command_request(
            command="curl -d '{\"op\":\"restart\"}' https://example.com/api/v1/action",
            confirmed=True,
            timeout_seconds=10,
        )

        with patch("api.ai.get_ai_session_store", return_value=mock_store):
            result = asyncio.run(execute_followup_command("sess-cmd-001", "msg-cmd-007", request))

        assert result["status"] == "elevation_required"
        assert result["command_type"] == "repair"
        assert result["requires_write_permission"] is True

    def test_execute_followup_command_curl_inline_xpost_requires_write_permission(self):
        from api.ai import execute_followup_command, FollowUpCommandExecuteRequest
        import asyncio

        mock_store = self._build_store("```bash\ncurl -XPOST https://example.com/api/v1/restart\n```")
        request = self._build_command_request(
            command="curl -XPOST https://example.com/api/v1/restart",
            confirmed=True,
            timeout_seconds=10,
        )

        with patch("api.ai.get_ai_session_store", return_value=mock_store):
            result = asyncio.run(execute_followup_command("sess-cmd-001", "msg-cmd-008", request))

        assert result["status"] == "elevation_required"
        assert result["command_type"] == "repair"
        assert result["requires_write_permission"] is True

    def test_execute_followup_command_curl_request_equals_requires_write_permission(self):
        from api.ai import execute_followup_command, FollowUpCommandExecuteRequest
        import asyncio

        mock_store = self._build_store("```bash\ncurl --request=DELETE https://example.com/api/v1/pod/test-pod\n```")
        request = self._build_command_request(
            command="curl --request=DELETE https://example.com/api/v1/pod/test-pod",
            confirmed=True,
            timeout_seconds=10,
        )

        with patch("api.ai.get_ai_session_store", return_value=mock_store):
            result = asyncio.run(execute_followup_command("sess-cmd-001", "msg-cmd-009", request))

        assert result["status"] == "elevation_required"
        assert result["command_type"] == "repair"
        assert result["requires_write_permission"] is True

    def test_execute_followup_command_curl_compact_data_requires_write_permission(self):
        from api.ai import execute_followup_command, FollowUpCommandExecuteRequest
        import asyncio

        compact_command = "curl -d '{\"op\":\"restart\"}' https://example.com/api/v1/action"
        mock_store = self._build_store(
            "执行计划见动作卡片",
            metadata={
                "actions": [
                    {
                        "command": compact_command,
                    }
                ]
            },
        )
        request = self._build_command_request(
            command=compact_command,
            confirmed=True,
            timeout_seconds=10,
        )

        with patch("api.ai.get_ai_session_store", return_value=mock_store):
            result = asyncio.run(execute_followup_command("sess-cmd-001", "msg-cmd-010", request))

        assert result["status"] == "elevation_required"
        assert result["command_type"] == "repair"
        assert result["requires_write_permission"] is True

    def test_execute_followup_command_curl_upload_file_requires_write_permission(self):
        from api.ai import execute_followup_command, FollowUpCommandExecuteRequest
        import asyncio

        mock_store = self._build_store("```bash\ncurl -T /tmp/fix.yaml https://example.com/api/v1/upload\n```")
        request = self._build_command_request(
            command="curl -T /tmp/fix.yaml https://example.com/api/v1/upload",
            confirmed=True,
            timeout_seconds=10,
        )

        with patch("api.ai.get_ai_session_store", return_value=mock_store):
            result = asyncio.run(execute_followup_command("sess-cmd-001", "msg-cmd-011", request))

        assert result["status"] == "elevation_required"
        assert result["command_type"] == "repair"
        assert result["requires_write_permission"] is True

    def test_execute_followup_command_curl_get_with_data_flag_is_query(self):
        from api.ai import execute_followup_command, FollowUpCommandExecuteRequest
        import asyncio

        mock_store = self._build_store("```bash\ncurl -G --data 'q=error' https://example.com/api/v1/search\n```")
        request = self._build_command_request(
            command="curl -G --data 'q=error' https://example.com/api/v1/search",
            confirmed=False,
            timeout_seconds=10,
        )

        with patch("api.ai.get_ai_session_store", return_value=mock_store):
            result = asyncio.run(execute_followup_command("sess-cmd-001", "msg-cmd-012", request))

        assert result["status"] == "confirmation_required"
        assert result["command_type"] == "query"
        assert result["requires_write_permission"] is False

    def test_execute_followup_command_rejects_semantic_variant_with_different_quoted_spaces(self):
        from api.ai import execute_followup_command, FollowUpCommandExecuteRequest
        from fastapi import HTTPException
        import asyncio

        mock_store = self._build_store("```bash\necho \"a b\"\n```")
        request = self._build_command_request(
            command="echo \"a   b\"",
            confirmed=True,
            timeout_seconds=10,
        )

        with patch("api.ai.get_ai_session_store", return_value=mock_store):
            with pytest.raises(HTTPException) as exc_info:
                asyncio.run(execute_followup_command("sess-cmd-001", "msg-cmd-013", request))

        assert exc_info.value.status_code == 400
        assert "command is not present" in str(exc_info.value.detail)

    def test_execute_followup_command_allows_pipe_operator(self):
        from api.ai import execute_followup_command, FollowUpCommandExecuteRequest
        import asyncio

        mock_store = self._build_store("```bash\necho safe | head -n 1\n```")
        request = self._build_command_request(
            command="echo safe | head -n 1",
            confirmed=False,
            timeout_seconds=10,
        )

        with patch("api.ai.get_ai_session_store", return_value=mock_store):
            result = asyncio.run(execute_followup_command("sess-cmd-001", "msg-cmd-014", request))

        self._assert_blocked_command_spec(result, reason_fragment="shell")

    def test_execute_followup_command_allows_pipe_operator_without_whitespace(self):
        from api.ai import execute_followup_command, FollowUpCommandExecuteRequest
        import asyncio

        mock_store = self._build_store("```bash\necho safe|head -n 1\n```")
        request = self._build_command_request(
            command="echo safe|head -n 1",
            confirmed=False,
            timeout_seconds=10,
        )

        with patch("api.ai.get_ai_session_store", return_value=mock_store):
            result = asyncio.run(execute_followup_command("sess-cmd-001", "msg-cmd-015", request))

        self._assert_blocked_command_spec(result, reason_fragment="shell")

    def test_execute_followup_command_allows_and_chain_operator(self):
        from api.ai import execute_followup_command, FollowUpCommandExecuteRequest
        import asyncio

        mock_store = self._build_store("```bash\necho first && echo second\n```")
        request = self._build_command_request(
            command="echo first && echo second",
            confirmed=False,
            timeout_seconds=10,
        )

        with patch("api.ai.get_ai_session_store", return_value=mock_store):
            result = asyncio.run(execute_followup_command("sess-cmd-001", "msg-cmd-015a", request))

        self._assert_blocked_command_spec(result, reason_fragment="shell")

    def test_execute_followup_command_allows_semicolon_chain_operator(self):
        from api.ai import execute_followup_command, FollowUpCommandExecuteRequest
        import asyncio

        mock_store = self._build_store("```bash\necho first ; echo second\n```")
        request = self._build_command_request(
            command="echo first ; echo second",
            confirmed=False,
            timeout_seconds=10,
        )

        with patch("api.ai.get_ai_session_store", return_value=mock_store):
            result = asyncio.run(execute_followup_command("sess-cmd-001", "msg-cmd-015b", request))

        self._assert_blocked_command_spec(result, reason_fragment="shell")

    def test_execute_followup_command_rejects_redirection_operator(self):
        from api.ai import execute_followup_command, FollowUpCommandExecuteRequest
        import asyncio

        mock_store = self._build_store("```bash\necho safe > /tmp/logoscope-test\n```")
        request = self._build_command_request(
            command="echo safe > /tmp/logoscope-test",
            confirmed=True,
            timeout_seconds=10,
        )

        with patch("api.ai.get_ai_session_store", return_value=mock_store):
            result = asyncio.run(execute_followup_command("sess-cmd-001", "msg-cmd-016", request))

        self._assert_blocked_command_spec(result, reason_fragment="shell")

    def test_execute_followup_command_rejects_background_operator(self):
        from api.ai import execute_followup_command, FollowUpCommandExecuteRequest
        import asyncio

        mock_store = self._build_store("```bash\necho safe &\n```")
        request = self._build_command_request(
            command="echo safe &",
            confirmed=True,
            timeout_seconds=10,
        )

        with patch("api.ai.get_ai_session_store", return_value=mock_store):
            result = asyncio.run(execute_followup_command("sess-cmd-001", "msg-cmd-017", request))

        self._assert_blocked_command_spec(result, reason_fragment="shell")

    def test_execute_followup_command_allows_quoted_ampersand_argument(self):
        from api.ai import execute_followup_command, FollowUpCommandExecuteRequest
        import asyncio

        mock_store = self._build_store("```bash\necho 'a&b'\n```")

        with patch("api.ai.get_ai_session_store", return_value=mock_store):
            precheck = asyncio.run(
                execute_followup_command(
                    "sess-cmd-001",
                    "msg-cmd-018",
                    self._build_command_request(
                        command="echo 'a&b'",
                        confirmed=False,
                        timeout_seconds=10,
                    ),
                )
            )
            request = self._build_command_request(
                command="echo 'a&b'",
                confirmed=True,
                confirmation_ticket=str(precheck.get("confirmation_ticket") or ""),
                timeout_seconds=10,
            )
            result = asyncio.run(execute_followup_command("sess-cmd-001", "msg-cmd-018", request))

        assert result["status"] == "executed"
        assert "a&b" in result["stdout"]

    def test_execute_followup_command_rejects_unquoted_ampersand_operator(self):
        from api.ai import execute_followup_command, FollowUpCommandExecuteRequest
        import asyncio

        mock_store = self._build_store("```bash\necho a&b\n```")
        request = self._build_command_request(
            command="echo a&b",
            confirmed=True,
            timeout_seconds=10,
        )

        with patch("api.ai.get_ai_session_store", return_value=mock_store):
            result = asyncio.run(execute_followup_command("sess-cmd-001", "msg-cmd-019", request))

        self._assert_blocked_command_spec(result, reason_fragment="shell")

    def test_execute_followup_command_allows_chained_operator_during_precheck(self):
        from api.ai import execute_followup_command, FollowUpCommandExecuteRequest
        import asyncio

        mock_store = self._build_store("```bash\necho safe | head -n 1\n```")
        request = self._build_command_request(
            command="echo safe | head -n 1",
            confirmed=False,
            timeout_seconds=10,
        )

        with patch("api.ai.get_ai_session_store", return_value=mock_store):
            result = asyncio.run(execute_followup_command("sess-cmd-001", "msg-cmd-019b", request))

        self._assert_blocked_command_spec(result, reason_fragment="shell")

    def test_execute_followup_command_operator_injection_from_equivalent_match_key_requires_confirmation(self):
        from api.ai import execute_followup_command, FollowUpCommandExecuteRequest
        import asyncio

        # 在 command_spec 强制 + 禁 shell 操作符后，该注入样式仍会先经过审批确认流程，
        # 但最终执行仍由结构化策略约束。
        mock_store = self._build_store("```bash\necho 'a|b'\n```")
        request = self._build_command_request(
            command="echo a|b",
            confirmed=False,
            timeout_seconds=10,
        )

        with patch("api.ai.get_ai_session_store", return_value=mock_store):
            result = asyncio.run(execute_followup_command("sess-cmd-001", "msg-cmd-019c", request))

        self._assert_blocked_command_spec(result, reason_fragment="shell")

    def test_execute_followup_command_rejects_redirection_family_variants_during_precheck(self):
        from api.ai import execute_followup_command, FollowUpCommandExecuteRequest
        from fastapi import HTTPException
        import asyncio

        scenarios = [
            ("echo ok 2>/tmp/logoscope-test", "msg-cmd-019d"),
            ("echo ok 2>&1", "msg-cmd-019e"),
            ("echo ok >| /tmp/logoscope-test", "msg-cmd-019f"),
            ("echo ok <<< \"payload\"", "msg-cmd-019g"),
        ]

        for command_text, message_id in scenarios:
            mock_store = self._build_store(f"```bash\n{command_text}\n```")
            request = self._build_command_request(
                command=command_text,
                confirmed=True,
                timeout_seconds=10,
            )

            with patch("api.ai.get_ai_session_store", return_value=mock_store):
                try:
                    result = asyncio.run(execute_followup_command("sess-cmd-001", message_id, request))
                except HTTPException as exc_info:
                    assert exc_info.status_code == 400
                    continue
            if str(result.get("status")) == "blocked":
                self._assert_blocked_command_spec(result, reason_fragment="shell")
            else:
                assert str(result.get("status")) == "executed"

    def test_execute_followup_command_rejects_full_blocked_operator_matrix(self):
        from api.ai import (
            execute_followup_command,
            FollowUpCommandExecuteRequest,
            _FOLLOWUP_COMMAND_BLOCKED_OPERATORS,
        )
        from fastapi import HTTPException
        import asyncio

        scenario_map = {
            "&": "echo ok &",
            ">": "echo ok > /tmp/logoscope-test",
            ">>": "echo ok >> /tmp/logoscope-test",
            "<": "cat < /tmp/logoscope-test",
            "<<": "cat << EOF",
            "<<<": "cat <<< \"payload\"",
            "<>": "echo ok <> /tmp/logoscope-test",
            "<&": "echo ok <&0",
            ">&": "echo ok >&1",
            "&>": "echo ok &> /tmp/logoscope-test",
            ">|": "echo ok >| /tmp/logoscope-test",
        }
        missing = sorted(set(_FOLLOWUP_COMMAND_BLOCKED_OPERATORS) - set(scenario_map.keys()))
        assert not missing, f"missing blocked operator test scenarios: {missing}"

        for index, operator in enumerate(sorted(_FOLLOWUP_COMMAND_BLOCKED_OPERATORS), start=1):
            command_text = scenario_map[operator]
            mock_store = self._build_store(f"```bash\n{command_text}\n```")
            request = self._build_command_request(
                command=command_text,
                confirmed=True,
                timeout_seconds=10,
            )

            with patch("api.ai.get_ai_session_store", return_value=mock_store):
                try:
                    result = asyncio.run(execute_followup_command("sess-cmd-001", f"msg-cmd-op-{index:02d}", request))
                except HTTPException as exc_info:
                    assert exc_info.status_code == 400
                    continue
            status = str(result.get("status"))
            if status == "blocked":
                self._assert_blocked_command_spec(result, reason_fragment="shell")
            else:
                assert status == "executed"

    def test_execute_followup_command_curl_mixed_case_request_requires_write_permission(self):
        from api.ai import execute_followup_command, FollowUpCommandExecuteRequest
        import asyncio

        mock_store = self._build_store("```bash\ncurl --ReQueSt pAtCh https://example.com/api/v1/restart\n```")
        request = self._build_command_request(
            command="curl --ReQueSt pAtCh https://example.com/api/v1/restart",
            confirmed=True,
            timeout_seconds=10,
        )

        with patch("api.ai.get_ai_session_store", return_value=mock_store):
            result = asyncio.run(execute_followup_command("sess-cmd-001", "msg-cmd-020", request))

        assert result["status"] == "elevation_required"
        assert result["command_type"] == "repair"
        assert result["requires_write_permission"] is True

    def test_execute_followup_command_curl_data_urlencode_without_get_requires_write_permission(self):
        from api.ai import execute_followup_command, FollowUpCommandExecuteRequest
        import asyncio

        mock_store = self._build_store(
            "```bash\ncurl --data-urlencode \"q=error timeout\" https://example.com/api/v1/search\n```"
        )
        request = self._build_command_request(
            command="curl --data-urlencode \"q=error timeout\" https://example.com/api/v1/search",
            confirmed=True,
            timeout_seconds=10,
        )

        with patch("api.ai.get_ai_session_store", return_value=mock_store):
            result = asyncio.run(execute_followup_command("sess-cmd-001", "msg-cmd-020b", request))

        assert result["status"] == "elevation_required"
        assert result["command_type"] == "repair"
        assert result["requires_write_permission"] is True

    def test_execute_followup_command_curl_mixed_request_flags_requires_write_permission(self):
        from api.ai import execute_followup_command, FollowUpCommandExecuteRequest
        import asyncio

        mock_store = self._build_store("```bash\ncurl -X GET --request DELETE https://example.com/api/v1/pods/test-pod\n```")
        request = self._build_command_request(
            command="curl -X GET --request DELETE https://example.com/api/v1/pods/test-pod",
            confirmed=True,
            timeout_seconds=10,
        )

        with patch("api.ai.get_ai_session_store", return_value=mock_store):
            result = asyncio.run(execute_followup_command("sess-cmd-001", "msg-cmd-021", request))

        assert result["status"] == "elevation_required"
        assert result["command_type"] == "repair"
        assert result["requires_write_permission"] is True

    def test_execute_followup_command_curl_nested_quotes_with_get_flag_is_query(self):
        from api.ai import execute_followup_command, FollowUpCommandExecuteRequest
        import asyncio

        mock_store = self._build_store(
            "```bash\ncurl -G --data-urlencode \"q=error 'timeout'\" \"https://example.com/api/v1/search\"\n```"
        )
        request = self._build_command_request(
            command="curl -G --data-urlencode \"q=error 'timeout'\" \"https://example.com/api/v1/search\"",
            confirmed=False,
            timeout_seconds=10,
        )

        with patch("api.ai.get_ai_session_store", return_value=mock_store):
            result = asyncio.run(execute_followup_command("sess-cmd-001", "msg-cmd-022", request))

        assert result["status"] == "confirmation_required"
        assert result["command_type"] == "query"
        assert result["requires_write_permission"] is False

    def test_execute_followup_command_confirmed_without_ticket_executes_for_readonly(self):
        from api.ai import execute_followup_command, FollowUpCommandExecuteRequest
        import asyncio

        mock_store = self._build_store("```bash\necho hello-logoscope\n```")
        request = self._build_command_request(
            command="echo hello-logoscope",
            confirmed=True,
            timeout_seconds=10,
        )

        with patch("api.ai.get_ai_session_store", return_value=mock_store):
            result = asyncio.run(execute_followup_command("sess-cmd-001", "msg-cmd-ticket-001", request))

        assert result["status"] == "executed"
        assert result["requires_write_permission"] is False

    def test_execute_followup_command_ticket_is_one_time_use(self):
        from api.ai import execute_followup_command, FollowUpCommandExecuteRequest
        import asyncio

        mock_store = self._build_store("```bash\nkubectl delete pod test-pod -n islap\n```")
        with patch("api.ai.get_ai_session_store", return_value=mock_store):
            precheck = asyncio.run(
                execute_followup_command(
                    "sess-cmd-001",
                    "msg-cmd-ticket-002",
                    self._build_command_request(
                        command="kubectl delete pod test-pod -n islap",
                        confirmed=False,
                        elevated=True,
                        timeout_seconds=10,
                    ),
                )
            )
            ticket = str(precheck.get("confirmation_ticket") or "")
            first_exec = asyncio.run(
                execute_followup_command(
                    "sess-cmd-001",
                    "msg-cmd-ticket-002",
                    self._build_command_request(
                        command="kubectl delete pod test-pod -n islap",
                        confirmed=True,
                        elevated=True,
                        confirmation_ticket=ticket,
                        timeout_seconds=10,
                    ),
                )
            )
            second_exec = asyncio.run(
                execute_followup_command(
                    "sess-cmd-001",
                    "msg-cmd-ticket-002",
                    self._build_command_request(
                        command="kubectl delete pod test-pod -n islap",
                        confirmed=True,
                        elevated=True,
                        confirmation_ticket=ticket,
                        timeout_seconds=10,
                    ),
                )
            )

        assert first_exec["status"] == "executed"
        assert second_exec["status"] == "confirmation_required"
        assert "ticket" in str(second_exec.get("message") or "").lower()

    def test_execute_followup_command_missing_ticket_is_rejected_for_write(self):
        from api.ai import execute_followup_command, FollowUpCommandExecuteRequest
        import asyncio

        mock_store = self._build_store("```bash\nkubectl delete pod test-pod -n islap\n```")
        with patch("api.ai.get_ai_session_store", return_value=mock_store):
            precheck = asyncio.run(
                execute_followup_command(
                    "sess-cmd-001",
                    "msg-cmd-ticket-003",
                    self._build_command_request(
                        command="kubectl delete pod test-pod -n islap",
                        confirmed=False,
                        elevated=True,
                        timeout_seconds=10,
                    ),
                )
            )
            result = asyncio.run(
                execute_followup_command(
                    "sess-cmd-001",
                    "msg-cmd-ticket-003",
                    self._build_command_request(
                        command="kubectl delete pod test-pod -n islap",
                        confirmed=True,
                        elevated=True,
                        confirmation_ticket="",
                        timeout_seconds=10,
                    ),
                )
            )

        assert str(precheck.get("status")) == "elevation_required"
        assert result["status"] == "confirmation_required"
        assert "ticket" in str(result.get("message") or "").lower()

    def test_execute_followup_command_sed_compact_inplace_flag_requires_write_permission(self):
        from api.ai import execute_followup_command, FollowUpCommandExecuteRequest
        import asyncio

        mock_store = self._build_store("```bash\nsed -Ei 's/error/warn/g' /tmp/app.log\n```")
        request = self._build_command_request(
            command="sed -Ei 's/error/warn/g' /tmp/app.log",
            confirmed=True,
            timeout_seconds=10,
        )

        with patch("api.ai.get_ai_session_store", return_value=mock_store):
            result = asyncio.run(execute_followup_command("sess-cmd-001", "msg-cmd-022b", request))

        assert result["status"] == "elevation_required"
        assert result["command_type"] == "repair"
        assert result["requires_write_permission"] is True


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
            "data_flow": {
                "summary": "gateway -> order-service -> mysql",
                "path": [
                    {"step": 1, "component": "gateway", "operation": "HTTP /orders"},
                    {"step": 2, "component": "order-service", "operation": "query mysql", "status": "error"},
                ],
                "evidence": ["mysql timeout"],
                "confidence": 0.79,
            },
            "root_causes": ["连接池耗尽"],
            "handling_ideas": ["先确认故障是否集中在写路径", "按 trace 采样排除偶发慢查询"],
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

        assert normalized["dataFlow"]["summary"] == "gateway -> order-service -> mysql"
        assert len(normalized["dataFlow"]["path"]) == 2
        assert normalized["rootCauses"] == [{"title": "连接池耗尽", "description": ""}]
        assert normalized["handlingIdeas"][0]["title"] == "先确认故障是否集中在写路径"
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


class TestFollowupAnswerStabilization:
    """测试无观察/无可执行动作时的答案降级。"""

    def test_marks_answer_as_draft_when_plan_is_non_executable(self):
        """零可执行动作时，不能宣称已经生成可执行命令。"""
        from api.ai import _stabilize_followup_answer_when_plan_is_non_executable

        answer = (
            "结论：目前怀疑 query-service 超时。\n\n"
            "已生成相应的只读查询命令来收集这些证据。\n\n"
            "执行步骤：\n"
            "1. 查看 query-service 日志"
        )
        stabilized = _stabilize_followup_answer_when_plan_is_non_executable(
            answer=answer,
            actions=[
                {
                    "id": "a1",
                    "title": "查看 query-service 日志",
                    "command": "",
                    "command_type": "query",
                    "executable": False,
                }
            ],
            action_observations=[],
            react_loop={
                "plan": {"executable_actions": 0},
                "execute": {"observed_actions": 0},
            },
        )

        assert "待证据验证的诊断草稿" in stabilized
        assert "当前还没有生成通过校验的结构化查询命令" in stabilized
        assert "以下项目仅是待验证排查草稿" in stabilized


class TestStorageAdapter:
    """测试 Storage Adapter"""

    def test_set_storage_adapter(self, mock_storage):
        """测试设置 storage adapter"""
        set_storage_adapter(mock_storage)

        from api import ai
        assert ai.storage == mock_storage

    def test_set_storage_adapter_attaches_runtime_v4_target_registry(self, mock_storage):
        """设置 storage 时同步挂载 runtime v4 target registry 存储。"""
        from ai.runtime_v4.targets import get_runtime_v4_target_registry

        set_storage_adapter(mock_storage)
        registry = get_runtime_v4_target_registry()

        assert registry.storage == mock_storage
