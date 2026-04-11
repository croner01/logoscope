"""
KB API contract tests.
"""

import asyncio
from unittest.mock import Mock, patch, AsyncMock

import pytest
from fastapi import HTTPException

from ai.similar_cases import Case


class TestKBRuntimeOptions:
    """runtime/options endpoint tests."""

    def test_runtime_options_success(self):
        from api.ai import kb_runtime_options, KBRuntimeOptionsRequest

        request = KBRuntimeOptionsRequest(
            remote_enabled=True,
            retrieval_mode="hybrid",
            save_mode="local_and_remote",
        )
        mock_gateway = Mock()
        mock_gateway.resolve_runtime_options.return_value = {
            "effective_retrieval_mode": "local",
            "effective_save_mode": "local_only",
            "remote_available": False,
            "message": "fallback",
        }

        with patch("api.ai.get_knowledge_gateway", return_value=mock_gateway):
            result = asyncio.run(kb_runtime_options(request))

        assert result["effective_retrieval_mode"] == "local"
        assert result["effective_save_mode"] == "local_only"
        mock_gateway.resolve_runtime_options.assert_called_once()

    def test_runtime_options_warns_not_configured(self):
        from api.ai import kb_runtime_options, KBRuntimeOptionsRequest

        request = KBRuntimeOptionsRequest(
            remote_enabled=True,
            retrieval_mode="hybrid",
            save_mode="local_and_remote",
        )
        mock_gateway = Mock()
        mock_gateway.resolve_runtime_options.return_value = {
            "effective_retrieval_mode": "local",
            "effective_save_mode": "local_only",
            "remote_available": False,
            "warning_code": "KBR-006",
            "message": "未检测到远端知识库接入，已切换为本地知识库模式。",
        }

        with patch("api.ai.get_knowledge_gateway", return_value=mock_gateway):
            with pytest.raises(HTTPException) as exc_info:
                asyncio.run(kb_runtime_options(request))

        assert exc_info.value.status_code == 409
        assert exc_info.value.detail["code"] == "KBR-006"

    def test_runtime_options_warns_unavailable(self):
        from api.ai import kb_runtime_options, KBRuntimeOptionsRequest

        request = KBRuntimeOptionsRequest(
            remote_enabled=True,
            retrieval_mode="hybrid",
            save_mode="local_and_remote",
        )
        mock_gateway = Mock()
        mock_gateway.resolve_runtime_options.return_value = {
            "effective_retrieval_mode": "local",
            "effective_save_mode": "local_only",
            "remote_available": False,
            "warning_code": "KBR-007",
            "message": "远端知识库未接入，已自动回退本地模式",
        }

        with patch("api.ai.get_knowledge_gateway", return_value=mock_gateway):
            with pytest.raises(HTTPException) as exc_info:
                asyncio.run(kb_runtime_options(request))

        assert exc_info.value.status_code == 503
        assert exc_info.value.detail["code"] == "KBR-007"


class TestKBSearch:
    """kb/search endpoint tests."""

    def test_kb_search_rejects_short_query(self):
        from api.ai import kb_search, KBSearchRequest

        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(kb_search(KBSearchRequest(query="a")))

        assert exc_info.value.status_code == 400
        assert exc_info.value.detail["code"] == "KBR-001"

    def test_kb_search_success(self):
        from api.ai import kb_search, KBSearchRequest

        request = KBSearchRequest(
            query="payment timeout",
            retrieval_mode="hybrid",
            top_k=5,
        )
        mock_gateway = Mock()
        mock_gateway.resolve_runtime_options.return_value = {
            "effective_retrieval_mode": "local",
            "effective_save_mode": "local_only",
            "message": "fallback",
        }
        mock_gateway.search.return_value = {
            "cases": [
                {
                    "id": "case-1",
                    "summary": "payment timeout",
                    "problem_type": "network",
                    "service_name": "payment-service",
                    "similarity_score": 0.85,
                    "source_backend": "local",
                }
            ],
            "total": 1,
            "sources": {"local": 1, "external": 0},
        }

        with patch("api.ai.get_knowledge_gateway", return_value=mock_gateway):
            result = asyncio.run(kb_search(request))

        assert result["effective_mode"] == "local"
        assert result["total"] == 1
        assert result["sources"]["local"] == 1

    def test_kb_search_top_k_is_clamped_to_20(self):
        from api.ai import kb_search, KBSearchRequest

        request = KBSearchRequest(
            query="payment timeout",
            retrieval_mode="local",
            top_k=999,
        )
        mock_gateway = Mock()
        mock_gateway.resolve_runtime_options.return_value = {
            "effective_retrieval_mode": "local",
            "effective_save_mode": "local_only",
            "message": "fallback",
        }
        mock_gateway.search.return_value = {
            "cases": [],
            "total": 0,
            "sources": {"local": 0, "external": 0},
        }

        with patch("api.ai.get_knowledge_gateway", return_value=mock_gateway):
            result = asyncio.run(kb_search(request))

        assert result["effective_mode"] == "local"
        kwargs = mock_gateway.search.call_args.kwargs
        assert kwargs["top_k"] == 20

    def test_kb_search_prefers_payload_warning_message_over_runtime_message(self):
        from api.ai import kb_search, KBSearchRequest

        request = KBSearchRequest(
            query="payment timeout",
            retrieval_mode="hybrid",
            top_k=5,
        )
        mock_gateway = Mock()
        mock_gateway.resolve_runtime_options.return_value = {
            "effective_retrieval_mode": "local",
            "effective_save_mode": "local_only",
            "message": "runtime-fallback-message",
        }
        mock_gateway.search.return_value = {
            "cases": [],
            "total": 0,
            "sources": {"local": 0, "external": 0},
            "warning_message": "payload-warning-message",
            "warning_code": "KBR-006",
        }

        with patch("api.ai.get_knowledge_gateway", return_value=mock_gateway):
            result = asyncio.run(kb_search(request))

        assert result["message"] == "payload-warning-message"
        assert result["warning_code"] == "KBR-006"




    def test_kb_search_remote_only_enables_remote_runtime_resolution(self):
        from api.ai import kb_search, KBSearchRequest

        request = KBSearchRequest(
            query="payment timeout",
            retrieval_mode="remote_only",
            top_k=5,
        )
        mock_gateway = Mock()
        mock_gateway.resolve_runtime_options.return_value = {
            "effective_retrieval_mode": "remote_only",
            "effective_save_mode": "local_only",
            "message": "ok",
        }
        mock_gateway.search.return_value = {
            "cases": [],
            "total": 0,
            "sources": {"local": 0, "external": 0},
        }

        with patch("api.ai.get_knowledge_gateway", return_value=mock_gateway):
            result = asyncio.run(kb_search(request))

        assert result["effective_mode"] == "remote_only"
        kwargs = mock_gateway.resolve_runtime_options.call_args.kwargs
        assert kwargs["remote_enabled"] is True
        assert kwargs["retrieval_mode"] == "remote_only"

class TestKBFromAnalysisSession:
    """kb/from-analysis-session endpoint tests."""

    @staticmethod
    def _session_payload() -> dict:
        return {
            "session": {
                "session_id": "ais-001",
                "analysis_type": "log",
                "service_name": "query-service",
                "input_text": "ERROR timeout connecting to redis",
                "summary_text": "query-service timeout 异常",
                "result": {
                    "raw": {
                        "problem_type": "timeout",
                        "severity": "high",
                        "summary": "连接 Redis 超时",
                        "root_causes": ["Redis 连接池耗尽", "下游响应慢"],
                        "solutions": [
                            {"title": "增加连接池上限", "description": "按峰值扩容"},
                        ],
                    }
                },
            },
            "messages": [
                {"role": "user", "content": "请按优先级给出排查顺序"},
                {"role": "assistant", "content": "1. 查看连接池使用率\n2. 排查 Redis 慢查询"},
            ],
            "message_count": 2,
        }

    def test_kb_from_analysis_session_not_found(self):
        from api.ai import kb_from_analysis_session, KBFromAnalysisSessionRequest

        request = KBFromAnalysisSessionRequest(analysis_session_id="missing")
        mock_store = Mock()
        mock_gateway = Mock()
        mock_gateway.resolve_runtime_options.return_value = {"effective_save_mode": "local_only"}

        with patch("api.ai.get_ai_session_store", return_value=mock_store), patch(
            "api.ai.get_knowledge_gateway", return_value=mock_gateway
        ), patch("api.ai._run_blocking", return_value=None):
            with pytest.raises(HTTPException) as exc_info:
                asyncio.run(kb_from_analysis_session(request))

        assert exc_info.value.status_code == 404
        assert exc_info.value.detail["code"] == "KBR-004"

    def test_kb_from_analysis_session_llm_success(self):
        from api.ai import kb_from_analysis_session, KBFromAnalysisSessionRequest

        request = KBFromAnalysisSessionRequest(
            analysis_session_id="ais-001",
            include_followup=True,
            use_llm=True,
        )
        mock_store = Mock()
        mock_gateway = Mock()
        mock_gateway.resolve_runtime_options.return_value = {"effective_save_mode": "local_only"}
        mock_llm = Mock()
        mock_llm.chat = AsyncMock(
            return_value=(
                '{"problem_type":"resource","severity":"high","summary":"会话总结结论",'
                '"analysis_summary":"综合会话后确认连接池与慢查询叠加",'
                '"root_causes":["连接池耗尽","慢查询堆积"],'
                '"solutions":[{"title":"连接池扩容","description":"上调上限","steps":["调整参数","观察指标"]}],'
                '"manual_remediation_steps":["先扩容连接池再回归验证"],"confidence":0.93}'
            )
        )

        with patch("api.ai.get_ai_session_store", return_value=mock_store), patch(
            "api.ai.get_knowledge_gateway", return_value=mock_gateway
        ), patch("api.ai._run_blocking", return_value=self._session_payload()), patch(
            "api.ai._is_llm_configured", return_value=True
        ), patch(
            "api.ai.get_llm_service", return_value=mock_llm
        ):
            result = asyncio.run(kb_from_analysis_session(request))

        assert result["draft_method"] == "llm"
        assert result["draft_case"]["summary"] == "会话总结结论"
        assert result["draft_case"]["problem_type"] == "resource"
        assert result["draft_case"]["root_causes"] == ["连接池耗尽", "慢查询堆积"]
        assert result["llm_enabled"] is True
        assert result["llm_requested"] is True
        assert "llm_fallback_reason" not in result

    def test_kb_from_analysis_session_llm_error_fallback_rule_based(self):
        from api.ai import kb_from_analysis_session, KBFromAnalysisSessionRequest

        request = KBFromAnalysisSessionRequest(
            analysis_session_id="ais-001",
            include_followup=True,
            use_llm=True,
        )
        mock_store = Mock()
        mock_gateway = Mock()
        mock_gateway.resolve_runtime_options.return_value = {"effective_save_mode": "local_only"}
        mock_llm = Mock()
        mock_llm.chat = AsyncMock(side_effect=RuntimeError("llm unavailable"))

        with patch("api.ai.get_ai_session_store", return_value=mock_store), patch(
            "api.ai.get_knowledge_gateway", return_value=mock_gateway
        ), patch("api.ai._run_blocking", return_value=self._session_payload()), patch(
            "api.ai._is_llm_configured", return_value=True
        ), patch(
            "api.ai.get_llm_service", return_value=mock_llm
        ):
            result = asyncio.run(kb_from_analysis_session(request))

        assert result["draft_method"] == "rule-based"
        assert result["llm_fallback_reason"] == "llm_error"
        assert result["draft_case"]["problem_type"] == "timeout"
        assert result["draft_case"]["severity"] == "high"
        assert result["draft_case"]["root_causes"] == ["Redis 连接池耗尽", "下游响应慢"]

    def test_kb_from_analysis_session_include_followup_false_excludes_conversation(self):
        from api.ai import kb_from_analysis_session, KBFromAnalysisSessionRequest

        request = KBFromAnalysisSessionRequest(
            analysis_session_id="ais-001",
            include_followup=False,
            use_llm=True,
        )
        mock_store = Mock()
        mock_gateway = Mock()
        mock_gateway.resolve_runtime_options.return_value = {"effective_save_mode": "local_only"}
        mock_llm = Mock()
        mock_llm.chat = AsyncMock(
            return_value=(
                '{"problem_type":"timeout","severity":"high","summary":"不含追问的草稿",'
                '"analysis_summary":"仅依据初始分析","root_causes":["连接池耗尽"],'
                '"solutions":[{"title":"连接池扩容","description":"调优","steps":["扩容"]}],'
                '"manual_remediation_steps":[],"confidence":0.9}'
            )
        )

        with patch("api.ai.get_ai_session_store", return_value=mock_store), patch(
            "api.ai.get_knowledge_gateway", return_value=mock_gateway
        ), patch("api.ai._run_blocking", return_value=self._session_payload()), patch(
            "api.ai._is_llm_configured", return_value=True
        ), patch(
            "api.ai.get_llm_service", return_value=mock_llm
        ):
            result = asyncio.run(kb_from_analysis_session(request))

        assert result["draft_method"] == "llm"
        chat_context = mock_llm.chat.await_args.kwargs["context"]
        transcript = chat_context.get("conversation_transcript", "")
        assert "请按优先级给出排查顺序" not in transcript
        assert "查看连接池使用率" not in transcript


class TestManualRemediation:
    """manual-remediation endpoint tests."""

    def test_manual_remediation_requires_steps(self):
        from api.ai import update_manual_remediation, ManualRemediationRequest

        request = ManualRemediationRequest(
            manual_remediation_steps=[],
            verification_result="pass",
            verification_notes="这是一个足够长的验证说明文本内容用于测试。",
        )

        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(update_manual_remediation("case-1", request))

        assert exc_info.value.status_code == 400
        assert exc_info.value.detail["code"] == "KBR-003"

    def test_manual_remediation_requires_notes_length(self):
        from api.ai import update_manual_remediation, ManualRemediationRequest

        request = ManualRemediationRequest(
            manual_remediation_steps=["调整 timeout 到 5s"],
            verification_result="pass",
            verification_notes="太短",
        )

        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(update_manual_remediation("case-1", request))

        assert exc_info.value.status_code == 400
        assert exc_info.value.detail["code"] == "KBR-002"

    def test_manual_remediation_requires_step_min_length(self):
        from api.ai import update_manual_remediation, ManualRemediationRequest

        request = ManualRemediationRequest(
            manual_remediation_steps=["短步"],
            verification_result="pass",
            verification_notes="这是一个足够长的验证说明文本内容用于测试。",
        )

        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(update_manual_remediation("case-1", request))

        assert exc_info.value.status_code == 400
        assert exc_info.value.detail["code"] == "KBR-003"

    def test_manual_remediation_success(self):
        from api.ai import update_manual_remediation, ManualRemediationRequest

        request = ManualRemediationRequest(
            manual_remediation_steps=["调整 timeout 到 5s", "开启指数退避重试"],
            verification_result="pass",
            verification_notes="灰度后错误率回到基线，p95 延迟恢复，链路告警消失。",
            final_resolution="完成 timeout 与重试参数调优并验证通过",
            save_mode="local_and_remote",
            remote_enabled=True,
        )

        existing = Case(
            id="case-abc123",
            problem_type="network",
            severity="high",
            summary="payment timeout",
            log_content="ERROR timeout",
            service_name="payment-service",
            llm_metadata={"knowledge_version": 1, "case_status": "archived"},
        )
        mock_store = Mock()
        mock_store.get_case.return_value = existing
        mock_store.update_case.return_value = existing

        mock_gateway = Mock()
        mock_gateway.resolve_runtime_options.return_value = {"effective_save_mode": "local_and_remote"}
        mock_gateway.upsert_remote_with_outbox.return_value = {
            "sync_status": "pending",
            "external_doc_id": "",
            "sync_error": "",
            "sync_error_code": "",
            "outbox_id": "kb-outbox-001",
        }

        with patch("ai.similar_cases.get_case_store", return_value=mock_store), patch(
            "api.ai.get_knowledge_gateway", return_value=mock_gateway
        ):
            result = asyncio.run(update_manual_remediation("case-abc123", request))

        assert result["status"] == "ok"
        assert result["case_id"] == "case-abc123"
        assert result["knowledge_version"] == 2
        assert result["sync_status"] == "pending"
        assert result["sync_error_code"] == ""
        assert result["outbox_id"] == "kb-outbox-001"
        assert result["remediation_history_count"] == 1
        assert mock_store.update_case.call_count == 1
        updated_case = mock_store.update_case.call_args[0][0]
        history = updated_case.llm_metadata.get("remediation_history")
        assert isinstance(history, list)
        assert len(history) == 1
        assert history[0]["version"] == 2


class TestKBSolutionOptimize:
    """kb/solutions/optimize endpoint tests."""

    def test_optimize_solution_fallback_rule_based_when_llm_disabled(self):
        from api.ai import optimize_kb_solution_content, KBSolutionOptimizeRequest

        request = KBSolutionOptimizeRequest(
            content="扩容连接池\n1. 调整连接上限\n2. 观察错误率",
            summary="连接池告警",
            service_name="query-service",
            problem_type="timeout",
            severity="high",
            use_llm=True,
        )
        with patch("api.ai._is_llm_configured", return_value=False):
            result = asyncio.run(optimize_kb_solution_content(request))

        assert result["method"] == "rule-based"
        assert "【处理步骤】" in result["optimized_text"]
        assert result["llm_enabled"] is False
        assert result["llm_fallback_reason"] == "llm_unavailable"

    def test_optimize_solution_llm_success(self):
        from api.ai import optimize_kb_solution_content, KBSolutionOptimizeRequest

        request = KBSolutionOptimizeRequest(
            content="优化数据库连接",
            service_name="query-service",
            use_llm=True,
        )
        mock_llm_service = Mock()
        mock_llm_service.chat = AsyncMock(
            return_value=(
                "【目标】\n恢复服务\n【问题上下文】\n...\n【处理步骤】\n1. 执行\n"
                "【验证方式】\n...\n【回滚方案】\n...\n【风险与注意】\n..."
            )
        )

        with patch("api.ai._is_llm_configured", return_value=True), patch(
            "api.ai.get_llm_service",
            return_value=mock_llm_service,
        ):
            result = asyncio.run(optimize_kb_solution_content(request))

        assert result["method"] == "llm"
        assert result["llm_enabled"] is True
        assert "【目标】" in result["optimized_text"]
        assert result["applied_style"] == "standard_kb_solution_v1"


class TestUpdateCaseContent:
    """cases/{case_id} content update endpoint tests."""

    def test_update_case_content_requires_at_least_one_field(self):
        from api.ai import update_case_content, UpdateCaseContentRequest

        existing = Case(
            id="case-1",
            problem_type="timeout",
            severity="high",
            summary="旧摘要",
            log_content="ERROR timeout",
            service_name="query-service",
            llm_metadata={"knowledge_version": 1, "case_status": "archived"},
        )
        mock_store = Mock()
        mock_store.get_case.return_value = existing

        request = UpdateCaseContentRequest()
        with patch("ai.similar_cases.get_case_store", return_value=mock_store):
            with pytest.raises(HTTPException) as exc_info:
                asyncio.run(update_case_content("case-1", request))

        assert exc_info.value.status_code == 400
        assert exc_info.value.detail["code"] == "KBR-003"

    def test_update_case_content_success(self):
        from api.ai import update_case_content, UpdateCaseContentRequest

        existing = Case(
            id="case-kb-001",
            problem_type="timeout",
            severity="high",
            summary="旧摘要",
            log_content="ERROR timeout",
            service_name="query-service",
            root_causes=["旧根因"],
            solutions=[{"title": "旧方案", "description": "旧描述"}],
            llm_metadata={"knowledge_version": 3, "case_status": "archived"},
        )
        mock_store = Mock()
        mock_store.get_case.return_value = existing
        mock_store.update_case.return_value = existing
        mock_store.append_case_change_history.return_value = {
            "event_id": "chg-1",
            "case_id": "case-kb-001",
            "event_type": "content_update",
            "version": 4,
            "editor": "manual_content",
            "changed_fields": ["summary", "root_causes", "solutions", "analysis_summary"],
            "changes": {"summary": {"before": "旧摘要", "after": "新摘要：连接池告警"}},
            "effective_save_mode": "local_only",
            "sync_status": "pending",
            "sync_error_code": "",
            "note": "manual_content_update",
            "source": "api:/ai/cases/update",
            "updated_at": "2026-01-01T00:00:00Z",
        }
        mock_store.count_case_change_history.return_value = 1
        mock_gateway = Mock()
        mock_gateway.resolve_runtime_options.return_value = {"effective_save_mode": "local_only"}
        mock_gateway.upsert_remote_with_outbox.return_value = {
            "sync_status": "pending",
            "external_doc_id": "",
            "sync_error": "",
            "sync_error_code": "",
            "outbox_id": "kb-outbox-003",
        }

        request = UpdateCaseContentRequest(
            summary="新摘要：连接池告警",
            root_causes=["连接池耗尽", "慢查询堆积"],
            solutions=[{"title": "连接池扩容", "description": "提高上限", "steps": ["扩容", "观察"]}],
            analysis_summary="更新后的知识库总结",
            save_mode="local_only",
            remote_enabled=False,
        )

        with patch("ai.similar_cases.get_case_store", return_value=mock_store), patch(
            "api.ai.get_knowledge_gateway", return_value=mock_gateway
        ):
            result = asyncio.run(update_case_content("case-kb-001", request))

        assert result["status"] == "ok"
        assert result["case_id"] == "case-kb-001"
        assert result["knowledge_version"] == 4
        assert result["sync_status"] == "pending"
        assert result["outbox_id"] == "kb-outbox-003"
        assert "friendly_message" in result
        assert isinstance(result.get("updated_fields"), list)
        assert result.get("content_update_history_count") == 1
        assert mock_store.update_case.call_count == 1
        assert mock_store.append_case_change_history.call_count == 1
        assert mock_store.count_case_change_history.call_count >= 1
        updated_case = mock_store.update_case.call_args[0][0]
        assert updated_case.summary == "新摘要：连接池告警"
        assert updated_case.root_causes == ["连接池耗尽", "慢查询堆积"]
        assert len(updated_case.solutions) == 1
        assert updated_case.llm_metadata.get("analysis_summary") == "更新后的知识库总结"
        assert updated_case.llm_metadata.get("knowledge_version") == 4
        assert result.get("history_entry", {}).get("version") == 4

    def test_update_case_content_no_effective_change_records_requested_fields(self):
        from api.ai import update_case_content, UpdateCaseContentRequest

        existing = Case(
            id="case-kb-002",
            problem_type="timeout",
            severity="high",
            summary="旧摘要",
            log_content="ERROR timeout",
            service_name="query-service",
            root_causes=["连接池耗尽"],
            solutions=[{"title": "扩容", "description": "提高上限", "steps": ["扩容"]}],
            llm_metadata={"knowledge_version": 5, "case_status": "archived", "analysis_summary": "旧摘要"},
        )
        mock_store = Mock()
        mock_store.get_case.return_value = existing
        mock_store.update_case.return_value = existing
        mock_store.count_case_change_history.return_value = 1

        def _append_history(*args, **kwargs):
            event = kwargs.get("event")
            if event is None and len(args) >= 2:
                event = args[1]
            case_id = kwargs.get("case_id")
            if case_id is None and len(args) >= 1:
                case_id = args[0]
            row = dict(event or {})
            row["event_id"] = "chg-noop-1"
            row["case_id"] = str(case_id or "")
            return row

        mock_store.append_case_change_history.side_effect = _append_history
        mock_gateway = Mock()
        mock_gateway.resolve_runtime_options.return_value = {"effective_save_mode": "local_only"}
        mock_gateway.upsert_remote_with_outbox.return_value = {
            "sync_status": "not_requested",
            "external_doc_id": "",
            "sync_error": "",
            "sync_error_code": "",
            "outbox_id": "",
        }

        request = UpdateCaseContentRequest(
            summary="旧摘要",
            save_mode="local_only",
            remote_enabled=False,
        )

        with patch("ai.similar_cases.get_case_store", return_value=mock_store), patch(
            "api.ai.get_knowledge_gateway", return_value=mock_gateway
        ):
            result = asyncio.run(update_case_content("case-kb-002", request))

        assert result["status"] == "ok"
        assert result["updated_fields"] == []
        assert result["requested_fields"] == ["summary"]
        assert result["unchanged_requested_fields"] == ["summary"]
        assert result["no_effective_change_reason"] == "submitted_values_equivalent_after_normalization"
        assert "等效" in result.get("friendly_message", "")

        history_entry = result.get("history_entry", {})
        assert history_entry.get("note") == "manual_content_update_no_effective_change"
        assert history_entry.get("requested_fields") == ["summary"]
        assert history_entry.get("unchanged_requested_fields") == ["summary"]
        assert history_entry.get("no_effective_change_reason") == "submitted_values_equivalent_after_normalization"

    def test_update_case_content_solution_line_change_is_detected(self):
        from api.ai import update_case_content, UpdateCaseContentRequest

        existing_steps = [f"步骤{i}" for i in range(1, 13)]
        updated_steps_text = "\n".join([f"{idx}. {text}" for idx, text in enumerate(existing_steps + ["新增步骤13"], start=1)])
        solutions_text = (
            "方案1: 连接池处理\n"
            "说明: 调整连接池参数并验证\n"
            "步骤:\n"
            f"{updated_steps_text}"
        )

        existing = Case(
            id="case-kb-003",
            problem_type="timeout",
            severity="high",
            summary="连接池告警",
            log_content="ERROR timeout",
            service_name="query-service",
            root_causes=["连接池耗尽"],
            solutions=[{"title": "连接池处理", "description": "调整连接池参数并验证", "steps": existing_steps}],
            llm_metadata={"knowledge_version": 7, "case_status": "archived"},
        )
        mock_store = Mock()
        mock_store.get_case.return_value = existing
        mock_store.update_case.return_value = existing
        mock_store.count_case_change_history.return_value = 2

        def _append_history(*args, **kwargs):
            event = kwargs.get("event")
            if event is None and len(args) >= 2:
                event = args[1]
            case_id = kwargs.get("case_id")
            if case_id is None and len(args) >= 1:
                case_id = args[0]
            row = dict(event or {})
            row["event_id"] = "chg-solutions-1"
            row["case_id"] = str(case_id or "")
            return row

        mock_store.append_case_change_history.side_effect = _append_history
        mock_gateway = Mock()
        mock_gateway.resolve_runtime_options.return_value = {"effective_save_mode": "local_only"}
        mock_gateway.upsert_remote_with_outbox.return_value = {
            "sync_status": "not_requested",
            "external_doc_id": "",
            "sync_error": "",
            "sync_error_code": "",
            "outbox_id": "",
        }

        request = UpdateCaseContentRequest(
            solutions_text=solutions_text,
            save_mode="local_only",
            remote_enabled=False,
        )

        with patch("ai.similar_cases.get_case_store", return_value=mock_store), patch(
            "api.ai.get_knowledge_gateway", return_value=mock_gateway
        ):
            result = asyncio.run(update_case_content("case-kb-003", request))

        assert result["status"] == "ok"
        assert "solutions" in result.get("updated_fields", [])
        assert result.get("no_effective_change_reason") == ""
        history_entry = result.get("history_entry", {})
        assert history_entry.get("note") == "manual_content_update"
        assert "solutions" in history_entry.get("changed_fields", [])


class TestKBRemoteRuntimeConfig:
    """kb/runtime* endpoint tests."""

    def test_validate_kb_runtime_config_success(self):
        from api.ai import validate_kb_runtime_config, KBRemoteRuntimeConfig

        request = KBRemoteRuntimeConfig(
            provider="ragflow",
            base_url="http://ragflow:9380",
            dataset_id="dataset-001",
            timeout_seconds=8,
            search_path="/api/v1/retrieval",
            upsert_path="/api/v1/datasets/{dataset_id}/documents",
        )

        result = asyncio.run(validate_kb_runtime_config(request))

        assert result["status"] == "ok"
        assert result["validated"] is True
        assert result["runtime"]["provider"] == "ragflow"
        assert result["runtime"]["base_url"] == "http://ragflow:9380"
        assert result["runtime"]["dataset_id"] == "dataset-001"

    def test_validate_kb_runtime_requires_base_url_when_enabled(self):
        from api.ai import validate_kb_runtime_config, KBRemoteRuntimeConfig

        request = KBRemoteRuntimeConfig(provider="ragflow", base_url="")
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(validate_kb_runtime_config(request))

        assert exc_info.value.status_code == 400
        assert "base_url is required" in str(exc_info.value.detail)

    def test_validate_kb_runtime_requires_dataset_id_when_ragflow(self):
        from api.ai import validate_kb_runtime_config, KBRemoteRuntimeConfig

        request = KBRemoteRuntimeConfig(provider="ragflow", base_url="http://ragflow:9380")
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(validate_kb_runtime_config(request))

        assert exc_info.value.status_code == 400
        assert "dataset_id is required" in str(exc_info.value.detail)

    def test_update_kb_runtime_config_success(self):
        from api.ai import update_kb_runtime_config, KBRemoteRuntimeConfig

        request = KBRemoteRuntimeConfig(
            provider="ragflow",
            base_url="http://ragflow:9380",
            dataset_id="dataset-001",
            timeout_seconds=10,
            persist_to_deployment=False,
        )
        mock_gateway = Mock()
        mock_gateway.start_outbox_worker.return_value = True

        with patch("api.ai.reload_knowledge_gateway", return_value=mock_gateway), patch(
            "api.ai._build_kb_runtime_status",
            return_value={"configured_provider": "ragflow", "provider_status": {"remote_available": False}},
        ):
            result = asyncio.run(update_kb_runtime_config(request))

        assert result["status"] == "ok"
        assert result["updated"] is True
        assert result["runtime"]["provider"] == "ragflow"
        assert result["runtime"]["base_url"] == "http://ragflow:9380"
        assert result["runtime"]["dataset_id"] == "dataset-001"
        mock_gateway.start_outbox_worker.assert_called_once()
