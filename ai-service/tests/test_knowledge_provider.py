"""
knowledge_provider contract tests.
"""

from unittest.mock import Mock, patch

import pytest

from ai.knowledge_provider import KnowledgeGateway


def _build_gateway(provider=None) -> KnowledgeGateway:
    """Create gateway with mocked local store/recommender dependencies."""
    mock_store = Mock()
    mock_recommender = Mock()
    with patch("ai.knowledge_provider.get_case_store", return_value=mock_store), patch(
        "ai.knowledge_provider.get_recommender", return_value=mock_recommender
    ):
        gateway = KnowledgeGateway(storage_adapter=None)
    gateway.provider = provider
    gateway._outbox_enabled = False
    gateway._outbox_items = []
    return gateway


class TestResolveRuntimeOptions:
    """resolve_runtime_options behavior."""

    def test_remote_disabled_forces_local_modes(self):
        gateway = _build_gateway()
        gateway.get_provider_status = Mock(
            return_value={"provider": "generic_rest", "remote_available": True, "remote_configured": True}
        )

        result = gateway.resolve_runtime_options(
            remote_enabled=False,
            retrieval_mode="hybrid",
            save_mode="local_and_remote",
        )

        assert result["effective_retrieval_mode"] == "local"
        assert result["effective_save_mode"] == "local_only"
        assert result["provider_name"] == "generic_rest"
        assert result["remote_available"] is True

    def test_remote_unavailable_fallbacks_to_local_modes(self):
        gateway = _build_gateway()
        gateway.get_provider_status = Mock(
            return_value={"provider": "generic_rest", "remote_available": False, "remote_configured": True}
        )

        result = gateway.resolve_runtime_options(
            remote_enabled=True,
            retrieval_mode="hybrid",
            save_mode="local_and_remote",
        )

        assert result["effective_retrieval_mode"] == "local"
        assert result["effective_save_mode"] == "local_only"
        assert result["remote_available"] is False
        assert "回退本地模式" in result["message"]
        assert result["warning_code"] == "KBR-007"

    def test_remote_not_configured_returns_warning_code(self):
        gateway = _build_gateway()
        gateway.get_provider_status = Mock(
            return_value={"provider": "", "remote_available": False, "remote_configured": False}
        )

        result = gateway.resolve_runtime_options(
            remote_enabled=True,
            retrieval_mode="hybrid",
            save_mode="local_and_remote",
        )

        assert result["effective_retrieval_mode"] == "local"
        assert result["effective_save_mode"] == "local_only"
        assert result["warning_code"] == "KBR-006"


class TestSearchBehavior:
    """search behavior in local/hybrid modes."""

    def test_search_local_mode_only_uses_local_results(self):
        provider = Mock()
        gateway = _build_gateway(provider=provider)
        gateway._local_search = Mock(
            return_value=[
                {
                    "id": "case-1",
                    "summary": "payment timeout",
                    "similarity_score": 0.81,
                    "source_backend": "local",
                },
                {
                    "id": "case-2",
                    "summary": "db slow query",
                    "similarity_score": 0.64,
                    "source_backend": "local",
                },
            ]
        )

        result = gateway.search(query="timeout", retrieval_mode="local", top_k=5)

        assert result["total"] == 2
        assert result["sources"] == {"local": 2, "external": 0}
        provider.search_cases.assert_not_called()

    def test_search_hybrid_merge_and_dedup(self):
        provider = Mock()
        provider.search_cases.return_value = [
            {
                "id": "case-2",
                "summary": "db slow query",
                "similarity_score": 0.92,
                "problem_type": "database",
                "service_name": "order-service",
            },
            {
                "id": "ext-1",
                "summary": "payment timeout via gateway",
                "similarity_score": 0.77,
                "problem_type": "network",
                "service_name": "api-gateway",
            },
        ]
        gateway = _build_gateway(provider=provider)
        gateway._local_search = Mock(
            return_value=[
                {"id": "case-1", "summary": "payment timeout", "similarity_score": 0.81, "source_backend": "local"},
                {"id": "case-2", "summary": "db slow query", "similarity_score": 0.64, "source_backend": "local"},
            ]
        )
        gateway.get_provider_status = Mock(return_value={"remote_available": True})

        result = gateway.search(query="timeout", retrieval_mode="hybrid", top_k=5)

        assert result["sources"] == {"local": 2, "external": 2}
        assert result["total"] == 3
        case2 = next(item for item in result["cases"] if item["id"] == "case-2")
        assert case2["similarity_score"] == pytest.approx(0.92)
        assert case2["source_backend"] == "external"


class TestUpsertRemoteModes:
    """upsert_remote_if_needed behavior."""

    def test_upsert_not_requested_when_local_only(self):
        provider = Mock()
        gateway = _build_gateway(provider=provider)

        result = gateway.upsert_remote_if_needed({"id": "case-1"}, save_mode="local_only")

        assert result["sync_status"] == "not_requested"
        assert result["sync_error_code"] == ""
        provider.upsert_case.assert_not_called()

    def test_upsert_fails_when_provider_disabled(self):
        gateway = _build_gateway(provider=None)

        result = gateway.upsert_remote_if_needed({"id": "case-1"}, save_mode="local_and_remote")

        assert result["sync_status"] == "failed"
        assert result["sync_error"] == "remote provider disabled"
        assert result["sync_error_code"] == "KBR-006"

    def test_upsert_fails_when_remote_unavailable(self):
        provider = Mock()
        gateway = _build_gateway(provider=provider)
        gateway.get_provider_status = Mock(return_value={"remote_available": False})

        result = gateway.upsert_remote_if_needed({"id": "case-1"}, save_mode="local_and_remote")

        assert result["sync_status"] == "failed"
        assert result["sync_error"] == "remote provider unavailable"
        assert result["sync_error_code"] == "KBR-007"

    def test_upsert_synced_when_remote_success(self):
        provider = Mock()
        provider.upsert_case.return_value = {"doc_id": "ext-doc-1"}
        gateway = _build_gateway(provider=provider)
        gateway.get_provider_status = Mock(return_value={"remote_available": True})

        result = gateway.upsert_remote_if_needed({"id": "case-1"}, save_mode="local_and_remote")

        assert result["sync_status"] == "synced"
        assert result["external_doc_id"] == "ext-doc-1"
        assert result["sync_error_code"] == ""

    def test_upsert_failed_when_remote_exception(self):
        provider = Mock()
        provider.upsert_case.side_effect = RuntimeError("remote service error")
        gateway = _build_gateway(provider=provider)
        gateway.get_provider_status = Mock(return_value={"remote_available": True})

        result = gateway.upsert_remote_if_needed({"id": "case-1"}, save_mode="local_and_remote")

        assert result["sync_status"] == "failed"
        assert "remote service error" in result["sync_error"]
        assert result["sync_error_code"] == "KBR-008"


class TestOutboxAsyncModes:
    """outbox async sync behavior."""

    def test_upsert_with_outbox_enqueue_pending(self):
        gateway = _build_gateway(provider=None)
        gateway._outbox_enabled = True
        gateway.start_outbox_worker = Mock(return_value=True)
        gateway.enqueue_remote_sync = Mock(return_value="kb-outbox-001")

        result = gateway.upsert_remote_with_outbox(
            {"id": "case-1", "knowledge_version": 2},
            save_mode="local_and_remote",
        )

        assert result["sync_status"] == "pending"
        assert result["outbox_id"] == "kb-outbox-001"
        gateway.enqueue_remote_sync.assert_called_once()

    def test_upsert_with_outbox_disabled_fallback_sync(self):
        gateway = _build_gateway(provider=None)
        gateway._outbox_enabled = False

        result = gateway.upsert_remote_with_outbox(
            {"id": "case-1", "knowledge_version": 2},
            save_mode="local_and_remote",
        )

        assert result["sync_status"] == "failed"
        assert result["sync_error"] == "remote provider disabled"

    def test_process_outbox_retry_then_failed(self):
        provider = Mock()
        provider.upsert_case.side_effect = RuntimeError("remote boom")
        gateway = _build_gateway(provider=provider)
        gateway._outbox_enabled = True
        gateway._outbox_max_attempts = 2
        gateway._persist_outbox_items = Mock()
        gateway._apply_case_sync_result = Mock()
        gateway.get_provider_status = Mock(return_value={"remote_available": True})
        gateway._outbox_items = [
            {
                "outbox_id": "kb-outbox-001",
                "case_id": "case-1",
                "payload": {"id": "case-1", "knowledge_version": 2},
                "status": "pending",
                "attempts": 0,
                "max_attempts": 2,
                "next_retry_at": 0.0,
                "created_at": "2026-03-02T00:00:00Z",
                "updated_at": "2026-03-02T00:00:00Z",
                "last_error": "",
                "last_result": {},
            }
        ]

        first = gateway.process_outbox_once()
        assert first["processed"] == 1
        assert gateway._outbox_items[0]["status"] == "pending"
        assert gateway._outbox_items[0]["attempts"] == 1

        gateway._outbox_items[0]["next_retry_at"] = 0.0
        second = gateway.process_outbox_once()
        assert second["processed"] == 1
        assert gateway._outbox_items[0]["status"] == "failed"
        assert gateway._outbox_items[0]["attempts"] == 2
        assert gateway._apply_case_sync_result.call_count == 2

    def test_process_outbox_success_dequeues_item(self):
        provider = Mock()
        provider.upsert_case.return_value = {"doc_id": "ext-doc-1"}
        gateway = _build_gateway(provider=provider)
        gateway._outbox_enabled = True
        gateway._persist_outbox_items = Mock()
        gateway._apply_case_sync_result = Mock()
        gateway.get_provider_status = Mock(return_value={"remote_available": True})
        gateway._outbox_items = [
            {
                "outbox_id": "kb-outbox-001",
                "case_id": "case-1",
                "payload": {"id": "case-1", "knowledge_version": 2},
                "status": "pending",
                "attempts": 0,
                "max_attempts": 2,
                "next_retry_at": 0.0,
                "created_at": "2026-03-02T00:00:00Z",
                "updated_at": "2026-03-02T00:00:00Z",
                "last_error": "",
                "last_result": {},
            }
        ]

        result = gateway.process_outbox_once()

        assert result["processed"] == 1
        assert gateway._outbox_items == []
        gateway._apply_case_sync_result.assert_called_once()


class TestProviderSelection:
    """provider selection behavior."""

    def test_build_provider_supports_ragflow_alias(self):
        with patch("ai.knowledge_provider.get_case_store", return_value=Mock()), patch(
            "ai.knowledge_provider.get_recommender", return_value=Mock()
        ), patch.dict(
            "os.environ",
            {
                "KB_REMOTE_PROVIDER": "ragflow",
                "KB_REMOTE_BASE_URL": "http://ragflow:9380",
            },
            clear=False,
        ):
            gateway = KnowledgeGateway(storage_adapter=None)
            assert gateway.provider is not None
            assert gateway.provider.name == "ragflow"
