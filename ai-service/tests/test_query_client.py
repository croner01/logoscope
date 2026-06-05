"""Tests for QueryServiceClient."""
from __future__ import annotations

from unittest.mock import patch, MagicMock
from ai.agent_runtime.query_client import QueryServiceClient, QueryServiceClientError


class TestQueryServiceClient:
    def test_builds_log_query_params_from_metadata(self):
        client = QueryServiceClient(base_url="http://query-service:8092")
        params = client._build_log_params(
            service_name="semantic-engine",
            namespace="islap",
            pod_name="semantic-engine-abc123",
            trace_id=None,
            start_time="2026-06-05T10:00:00Z",
            end_time="2026-06-05T10:30:00Z",
            level="ERROR",
            search=None,
            limit=200,
        )
        assert params["service_name"] == "semantic-engine"
        assert params["namespace"] == "islap"
        assert params["pod_name"] == "semantic-engine-abc123"
        assert params["start_time"] == "2026-06-05T10:00:00Z"
        assert params["end_time"] == "2026-06-05T10:30:00Z"
        assert params["level"] == "ERROR"
        assert params["limit"] == 200

    def test_builds_minimal_params_without_optionals(self):
        client = QueryServiceClient()
        params = client._build_log_params(limit=100)
        assert params["limit"] == 100
        assert "service_name" not in params
        assert "pod_name" not in params

    def test_translates_simple_select_to_query_params(self):
        client = QueryServiceClient()
        spec = {
            "tool": "kubectl_clickhouse_query",
            "args": {
                "query": "SELECT * FROM logs.events WHERE service_name='api-gateway' AND level='ERROR' ORDER BY timestamp DESC LIMIT 50",
                "target_kind": "clickhouse_cluster",
                "target_identity": "database:logs",
            },
        }
        params = client.translate_clickhouse_spec(spec)
        assert params is not None
        assert params["service_name"] == "api-gateway"
        assert params["level"] == "ERROR"
        assert params["limit"] == 50

    def test_returns_none_for_complex_sql(self):
        client = QueryServiceClient()
        spec = {
            "tool": "kubectl_clickhouse_query",
            "args": {
                "query": "SELECT service_name, COUNT(*) as cnt FROM logs.events GROUP BY service_name HAVING cnt > 100",
                "target_kind": "clickhouse_cluster",
            },
        }
        params = client.translate_clickhouse_spec(spec)
        assert params is None  # complex aggregation → route to remote

    def test_returns_none_for_join_sql(self):
        client = QueryServiceClient()
        spec = {
            "tool": "kubectl_clickhouse_query",
            "args": {
                "query": "SELECT a.* FROM logs.events a JOIN logs.events b ON a.trace_id = b.trace_id",
                "target_kind": "clickhouse_cluster",
            },
        }
        params = client.translate_clickhouse_spec(spec)
        assert params is None

    def test_unified_result_matches_exec_service_shape(self):
        client = QueryServiceClient()
        result = client._to_command_result(
            events=[{"id": "a1", "message": "error", "timestamp": "2026-06-05T10:00:00Z"}],
            total_count=1,
            duration_ms=45,
        )
        assert result["status"] == "completed"
        assert result["exit_code"] == 0
        assert result["total_count"] == 1
        assert result["duration_ms"] == 45
        assert result["command_type"] == "query"
        assert result["risk_level"] == "low"
