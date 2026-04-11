"""Tests for extracted value KPI service helpers."""

import os
import sys
from datetime import datetime, timezone

# 添加 query-service 根目录到 Python 路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api import value_kpi_service


def test_evaluate_value_kpi_alerts_respects_suppression():
    metrics = {
        "mttd_minutes": 18.0,
        "mttr_minutes": 45.0,
        "trace_log_correlation_rate": 0.35,
        "topology_coverage_rate": 0.41,
        "release_regression_pass_rate": 0.88,
    }
    alerts = value_kpi_service.evaluate_value_kpi_alerts(
        metrics=metrics,
        max_mttd_minutes=15.0,
        max_mttr_minutes=30.0,
        min_trace_log_correlation_rate=0.5,
        min_topology_coverage_rate=0.5,
        min_release_regression_pass_rate=0.95,
        suppressed_metrics={"trace_log_correlation_rate"},
    )
    assert len(alerts) >= 4
    correlation = next(item for item in alerts if item["metric"] == "trace_log_correlation_rate")
    assert correlation["suppressed"] is True


def test_compute_value_kpis_returns_empty_payload_without_storage():
    result = value_kpi_service.compute_value_kpis(
        storage_adapter=None,
        time_window="7 DAY",
        start_time=None,
        end_time=None,
        use_cache=True,
        sanitize_interval_fn=lambda window, default: default,
        build_cache_key_fn=lambda **_: "k",
        get_cached_fn=lambda _k: None,
        set_cached_fn=lambda _k, _v: None,
        build_time_filter_clause_fn=lambda **_: ("1=1", {}),
        safe_ratio_fn=lambda a, b: 0.0,
        compute_inference_coverage_rate_fn=lambda *_: 0.0,
        estimate_mttd_mttr_minutes_fn=lambda *_: {"mttd_minutes": 0.0, "mttr_minutes": 0.0, "incident_count": 0},
        query_release_gate_summary_fn=lambda *_: {"pass_rate": 0.0},
    )
    assert result["metrics"]["mttd_minutes"] == 0.0
    assert result["release_gate_summary"]["total"] == 0
    assert result["incident_summary"]["incident_count"] == 0


def test_build_value_kpi_weekly_csv_shape():
    def fake_compute(start_dt: datetime, end_dt: datetime):
        assert end_dt > start_dt
        return {
            "metrics": {
                "mttd_minutes": 1.2,
                "mttr_minutes": 2.3,
                "trace_log_correlation_rate": 0.45,
                "topology_coverage_rate": 0.56,
                "release_regression_pass_rate": 0.78,
            },
            "release_gate_summary": {"total": 3, "passed": 2, "failed": 1},
            "incident_summary": {"incident_count": 5},
        }

    csv_text, filename = value_kpi_service.build_value_kpi_weekly_csv(
        weeks=2,
        compute_value_kpis_fn=fake_compute,
        now_utc=datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc),
    )

    lines = [line for line in csv_text.splitlines() if line.strip()]
    assert filename.startswith("value-kpi-weekly-20260301-120000")
    assert len(lines) == 3
    assert lines[0].startswith("week_index,week_start_utc,week_end_utc")


def test_compute_value_kpis_correlation_query_requires_span_and_excludes_synthetic():
    class _FakeStorageAdapter:
        def __init__(self):
            self.queries = []

        def execute_query(self, query, params=None):
            condensed = " ".join(str(query).split())
            self.queries.append(condensed)
            if "FROM system.tables" in condensed:
                return [{"cnt": 0}]
            if "count() AS total_logs" in condensed and "AS correlated_logs" in condensed:
                return [{"total_logs": 10, "correlated_logs": 5}]
            if "AS total_services" in condensed:
                return [{"total_services": 20}]
            if "AS traced_services" in condensed:
                return [{"traced_services": 8}]
            return []

    value_kpi_service._TABLE_EXISTS_CACHE.clear()
    storage = _FakeStorageAdapter()
    result = value_kpi_service.compute_value_kpis(
        storage_adapter=storage,
        time_window="7 DAY",
        start_time=None,
        end_time=None,
        use_cache=False,
        sanitize_interval_fn=lambda window, default: window or default,
        build_cache_key_fn=lambda **_: "unused-cache-key",
        get_cached_fn=lambda _k: None,
        set_cached_fn=lambda _k, _v: None,
        build_time_filter_clause_fn=lambda **_: ("1=1", {}),
        safe_ratio_fn=lambda a, b: (a / b) if b else 0.0,
        compute_inference_coverage_rate_fn=lambda *_: 0.0,
        estimate_mttd_mttr_minutes_fn=lambda *_: {"mttd_minutes": 1.0, "mttr_minutes": 2.0, "incident_count": 3},
        query_release_gate_summary_fn=lambda *_: {"pass_rate": 1.0},
    )

    correlation_query = next(q for q in storage.queries if "AS correlated_logs" in q)
    assert "notEmpty(span_id)" in correlation_query
    assert "trace_id_source" in correlation_query
    assert "synthetic" in correlation_query
    assert result["metrics"]["trace_log_correlation_rate"] == 0.5
