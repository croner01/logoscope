"""
Tests for synthetic project metrics query behavior in alerts API.
"""
import pytest
from unittest.mock import Mock

from api import alerts


@pytest.fixture(autouse=True)
def clear_global_state():
    alerts._alert_rules.clear()
    alerts._alert_events.clear()
    alerts._SYNTHETIC_PROJECT_METRICS_CACHE.clear()
    yield
    alerts._alert_rules.clear()
    alerts._alert_events.clear()
    alerts._SYNTHETIC_PROJECT_METRICS_CACHE.clear()


class TestResolveSyntheticProjectGroupLimit:
    """Validate synthetic project metric query group limit parsing."""

    def test_invalid_value_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("ALERT_SYNTHETIC_PROJECT_GROUP_LIMIT", "invalid")
        assert alerts._resolve_synthetic_project_group_limit() == 1000

    def test_value_is_clamped_to_minimum(self, monkeypatch):
        monkeypatch.setenv("ALERT_SYNTHETIC_PROJECT_GROUP_LIMIT", "20")
        assert alerts._resolve_synthetic_project_group_limit() == 100

    def test_value_is_clamped_to_maximum(self, monkeypatch):
        monkeypatch.setenv("ALERT_SYNTHETIC_PROJECT_GROUP_LIMIT", "99999")
        assert alerts._resolve_synthetic_project_group_limit() == 5000


class TestCollectSyntheticProjectMetricsQuery:
    """Ensure synthetic trace query keeps memory-safe shape."""

    def test_trace_query_avoids_attributes_json_and_uses_group_limit(self, monkeypatch):
        mock_storage = Mock()
        mock_storage.execute_query = Mock(side_effect=[[], []])
        monkeypatch.setattr(alerts, "_STORAGE_ADAPTER", mock_storage)
        monkeypatch.setenv("ALERT_SYNTHETIC_PROJECT_GROUP_LIMIT", "1234")

        result = alerts._collect_synthetic_project_metrics(window_minutes=5)

        assert result == {}
        assert mock_storage.execute_query.call_count == 2

        logs_query = mock_storage.execute_query.call_args_list[0].args[0]
        traces_query = mock_storage.execute_query.call_args_list[1].args[0]

        assert "LIMIT 1234" in logs_query
        assert "LIMIT 1234" in traces_query
        assert "attributes_json" not in traces_query
        assert "pod_name" not in traces_query
        assert "if(length(trim(namespace)) > 0" in traces_query

    def test_collect_only_trace_metrics_runs_single_query(self, monkeypatch):
        mock_storage = Mock()
        mock_storage.execute_query = Mock(return_value=[])
        monkeypatch.setattr(alerts, "_STORAGE_ADAPTER", mock_storage)

        result = alerts._collect_synthetic_project_metrics(
            window_minutes=5,
            required_metric_names={"trace_p95_ms_5m"},
        )

        assert result == {}
        assert mock_storage.execute_query.call_count == 1
        traces_query = mock_storage.execute_query.call_args_list[0].args[0]
        assert "FROM logs.traces" in traces_query
        assert "FROM logs.logs" not in traces_query


class TestEvaluateSyntheticMetricsOptimization:
    """Ensure evaluate path skips heavy synthetic queries when unnecessary."""

    @pytest.mark.asyncio
    async def test_evaluate_skips_synthetic_query_for_non_synthetic_rule(self):
        mock_storage = Mock()
        mock_storage.get_metrics = Mock(return_value=[
            {
                "service_name": "api-server",
                "metric_name": "cpu_usage",
                "value": 95.0,
            }
        ])
        mock_storage.execute_query = Mock(return_value=[])

        alerts.set_storage_adapter(mock_storage)
        await alerts.create_alert_rule(
            alerts.AlertRule(
                name="CPU Alert",
                metric_name="cpu_usage",
                condition="gt",
                threshold=80.0,
                enabled=True,
                duration=0,
            )
        )

        result = await alerts.evaluate_alert_rules()

        assert result["status"] == "ok"
        assert mock_storage.execute_query.call_count == 0

    @pytest.mark.asyncio
    async def test_evaluate_reuses_cached_synthetic_metrics(self, monkeypatch):
        mock_storage = Mock()
        mock_storage.get_metrics = Mock(return_value=[])
        mock_storage.execute_query = Mock(return_value=[
            {
                "namespace": "prod",
                "service_name": "checkout",
                "total_traces": 10.0,
                "error_traces": 1.0,
                "p95_ms": 320.0,
            }
        ])

        alerts.set_storage_adapter(mock_storage)
        monkeypatch.setattr(alerts, "_SYNTHETIC_PROJECT_METRICS_CACHE_TTL_SECONDS", 120)
        await alerts.create_alert_rule(
            alerts.AlertRule(
                name="trace p95 alert",
                metric_name="trace_p95_ms_5m",
                service_name="checkout",
                namespace="prod",
                condition="gt",
                threshold=200.0,
                duration=0,
                enabled=True,
            )
        )

        first = await alerts.evaluate_alert_rules()
        second = await alerts.evaluate_alert_rules()

        assert first["status"] == "ok"
        assert second["status"] == "ok"
        assert mock_storage.execute_query.call_count == 1
