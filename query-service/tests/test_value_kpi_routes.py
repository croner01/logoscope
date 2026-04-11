"""
Query Service value KPI 路由单元测试
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import pytest

# 添加 query-service 根目录到 Python 路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api import query_routes


class FakeValueKpiStorageAdapter:
    """value KPI 路由测试存储桩。"""

    def __init__(self):
        self.snapshot_rows: List[Dict[str, Any]] = []
        self.created_snapshot_table = False
        self.executed_queries: List[Any] = []

    def execute_query(self, query: str, params: Dict[str, Any] = None) -> List[Dict[str, Any]]:
        condensed = " ".join(query.split())
        params = params or {}
        self.executed_queries.append((query, params))

        if "CREATE TABLE IF NOT EXISTS logs.value_kpi_snapshots" in condensed:
            self.created_snapshot_table = True
            return []

        if "count() AS total_logs" in condensed and "AS correlated_logs" in condensed and "FROM logs.logs" in condensed:
            return [{"total_logs": 100, "correlated_logs": 40}]

        if "AS total_services" in condensed and "service_name" in condensed:
            return [{"total_services": 10}]

        if "AS traced_services" in condensed and "service_name" in condensed:
            return [{"traced_services": 4}]

        if "FROM logs.logs" in condensed and "level_norm AS level" in condensed:
            base = datetime(2026, 2, 27, 0, 0, 0, tzinfo=timezone.utc)
            return [
                {
                    "timestamp": base,
                    "service_name": "query-service",
                    "level": "ERROR",
                    "message": "database timeout",
                },
                {
                    "timestamp": base + timedelta(minutes=45),
                    "service_name": "query-service",
                    "level": "INFO",
                    "message": "resolved after rollback",
                },
            ]

        if "FROM logs.release_gate_reports" in condensed and "countIf(status = 'passed')" in condensed:
            return [{
                "total": 8,
                "passed": 6,
                "failed": 2,
                "bypassed": 0,
                "release_total": 8,
                "release_passed": 6,
                "release_failed": 2,
                "drill_total": 0,
                "trace_smoke_failed": 2,
                "ai_contract_failed": 1,
                "query_contract_failed": 1,
            }]

        if "FROM logs.release_gate_reports" in condensed and "ORDER BY started_at DESC" in condensed:
            return [{
                "gate_id": "gate-1",
                "started_at": "2026-02-27 00:00:00.000",
                "finished_at": "2026-02-27 00:01:00.000",
                "status": "failed",
                "candidate": "m4-rc",
                "tag": "m4-test",
                "target": "query-service",
                "trace_id": "trace-gate-1",
                "smoke_exit_code": 1,
                "trace_smoke_exit_code": 1,
                "ai_contract_exit_code": 0,
                "query_contract_exit_code": 1,
                "report_path": "/tmp/gate-report.json",
                "summary": "trace smoke failed",
            }]

        if "INSERT INTO logs.value_kpi_snapshots" in condensed:
            row = {
                "snapshot_id": params.get("snapshot_id"),
                "source": params.get("source"),
                "time_window": params.get("time_window"),
                "window_start": params.get("window_start"),
                "window_end": params.get("window_end"),
                "mttd_minutes": params.get("mttd_minutes", 0.0),
                "mttr_minutes": params.get("mttr_minutes", 0.0),
                "trace_log_correlation_rate": params.get("trace_log_correlation_rate", 0.0),
                "topology_coverage_rate": params.get("topology_coverage_rate", 0.0),
                "release_regression_pass_rate": params.get("release_regression_pass_rate", 0.0),
                "incident_count": params.get("incident_count", 0),
                "release_gate_total": params.get("release_gate_total", 0),
                "release_gate_passed": params.get("release_gate_passed", 0),
                "release_gate_failed": params.get("release_gate_failed", 0),
                "release_gate_bypassed": params.get("release_gate_bypassed", 0),
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            self.snapshot_rows.insert(0, row)
            return []

        if "FROM logs.value_kpi_snapshots" in condensed and "ORDER BY created_at DESC" in condensed:
            rows = self.snapshot_rows.copy()
            source = params.get("source")
            if source:
                rows = [item for item in rows if item.get("source") == source]
            return rows[: int(params.get("limit", 12))]

        return []


class FakeCoverageFallbackStorageAdapter(FakeValueKpiStorageAdapter):
    """用于验证 topology coverage inference fallback 的存储桩。"""

    def execute_query(self, query: str, params: Dict[str, Any] = None) -> List[Dict[str, Any]]:
        condensed = " ".join(query.split())

        if "AS total_services" in condensed and "service_name" in condensed:
            return [{"total_services": 35}]
        if "AS traced_services" in condensed and "service_name" in condensed:
            return [{"traced_services": 1}]
        if (
            "SELECT timestamp, service_name, namespace, message, trace_id, attributes_json" in condensed
            and "FROM logs.logs" in condensed
            and "LIMIT {infer_cov_log_limit:Int32}" in condensed
        ):
            base = datetime(2026, 2, 27, 0, 0, 0, tzinfo=timezone.utc)
            return [
                {
                    "id": "x1",
                    "timestamp": base,
                    "service_name": "svc-a",
                    "namespace": "prod",
                    "message": "request_id=req-100 start",
                    "trace_id": "",
                    "attributes_json": "{\"request_id\":\"req-100\"}",
                },
                {
                    "id": "x2",
                    "timestamp": base + timedelta(seconds=1),
                    "service_name": "svc-b",
                    "namespace": "prod",
                    "message": "request_id=req-100 done",
                    "trace_id": "",
                    "attributes_json": "{\"request_id\":\"req-100\"}",
                },
                {
                    "id": "x3",
                    "timestamp": base + timedelta(seconds=2),
                    "service_name": "svc-c",
                    "namespace": "prod",
                    "message": "heartbeat",
                    "trace_id": "",
                    "attributes_json": "{}",
                },
            ]

        return super().execute_query(query, params)


class FakeReleaseGateDrillAwareStorageAdapter(FakeValueKpiStorageAdapter):
    """用于验证 release pass rate 对 drill/bypass 的剔除逻辑。"""

    def execute_query(self, query: str, params: Dict[str, Any] = None) -> List[Dict[str, Any]]:
        condensed = " ".join(query.split())
        if "FROM logs.release_gate_reports" in condensed and "countIf(status = 'passed')" in condensed:
            return [{
                "total": 6,
                "passed": 4,
                "failed": 1,
                "bypassed": 1,
                "release_total": 4,
                "release_passed": 4,
                "release_failed": 0,
                "drill_total": 2,
                "trace_smoke_failed": 0,
                "ai_contract_failed": 0,
                "query_contract_failed": 0,
            }]
        return super().execute_query(query, params)


class FakeLegacyReleaseGateSchemaAdapter(FakeValueKpiStorageAdapter):
    """模拟 release_gate_reports 尚未新增三项子校验列。"""

    def execute_query(self, query: str, params: Dict[str, Any] = None) -> List[Dict[str, Any]]:
        condensed = " ".join(query.split())
        if (
            "FROM logs.release_gate_reports" in condensed
            and "countIf(status = 'passed')" in condensed
            and "trace_smoke_exit_code" in condensed
        ):
            raise RuntimeError("Unknown identifier: trace_smoke_exit_code")
        if (
            "FROM logs.release_gate_reports" in condensed
            and "ORDER BY started_at DESC" in condensed
            and "trace_smoke_exit_code" in condensed
        ):
            raise RuntimeError("Unknown identifier: trace_smoke_exit_code")
        if (
            "FROM logs.release_gate_reports" in condensed
            and "ORDER BY started_at DESC" in condensed
            and "trace_smoke_exit_code" not in condensed
        ):
            return [{
                "gate_id": "gate-1",
                "started_at": "2026-02-27 00:00:00.000",
                "finished_at": "2026-02-27 00:01:00.000",
                "status": "failed",
                "candidate": "m4-rc",
                "tag": "m4-test",
                "target": "query-service",
                "trace_id": "trace-gate-1",
                "smoke_exit_code": 1,
                "report_path": "/tmp/gate-report.json",
                "summary": "trace smoke failed",
            }]
        return super().execute_query(query, params)


@pytest.fixture(autouse=True)
def reset_state():
    query_routes.set_storage_adapter(None)
    query_routes._VALUE_KPI_ALERT_SUPPRESSIONS.clear()
    query_routes._VALUE_KPI_CACHE.clear()
    query_routes._reset_value_kpi_cache_metrics()
    yield
    query_routes.set_storage_adapter(None)
    query_routes._VALUE_KPI_ALERT_SUPPRESSIONS.clear()
    query_routes._VALUE_KPI_CACHE.clear()
    query_routes._reset_value_kpi_cache_metrics()


@pytest.mark.asyncio
async def test_value_kpi_alerts_and_suppression():
    """value KPI 告警支持阈值触发和抑制。"""
    query_routes.set_storage_adapter(FakeValueKpiStorageAdapter())

    result = await query_routes.value_kpi_alerts(
        time_window="7 DAY",
        max_mttd_minutes=0.1,
        max_mttr_minutes=30.0,
        min_trace_log_correlation_rate=0.6,
        min_topology_coverage_rate=0.6,
        min_release_regression_pass_rate=0.95,
    )

    assert result["status"] == "ok"
    assert result["active_alerts"] >= 1
    assert any(item["metric"] == "trace_log_correlation_rate" for item in result["alerts"])

    suppress = await query_routes.suppress_value_kpi_alert(
        metric="trace_log_correlation_rate",
        enabled=True,
    )
    assert suppress["suppressed"] is True

    result2 = await query_routes.value_kpi_alerts(
        time_window="7 DAY",
        max_mttd_minutes=0.1,
        max_mttr_minutes=30.0,
        min_trace_log_correlation_rate=0.6,
        min_topology_coverage_rate=0.6,
        min_release_regression_pass_rate=0.95,
    )
    correlation_alert = next(item for item in result2["alerts"] if item["metric"] == "trace_log_correlation_rate")
    assert correlation_alert["suppressed"] is True


@pytest.mark.asyncio
async def test_capture_and_list_value_kpi_snapshots():
    """应支持创建并查询 value KPI 快照。"""
    storage = FakeValueKpiStorageAdapter()
    query_routes.set_storage_adapter(storage)

    created = await query_routes.capture_value_kpi_snapshot(time_window="7 DAY", source="pytest")
    assert created["status"] == "ok"
    assert created["snapshot_id"].startswith("vkpi-")
    assert storage.created_snapshot_table is True
    assert len(storage.snapshot_rows) == 1

    listed = await query_routes.list_value_kpi_snapshots(limit=5, source="pytest")
    assert listed["status"] == "ok"
    assert listed["count"] == 1
    assert listed["data"][0]["source"] == "pytest"


def test_compute_value_kpis_uses_inference_coverage_fallback():
    """当 trace 覆盖较低时，应回退使用 inference coverage。"""
    query_routes.set_storage_adapter(FakeCoverageFallbackStorageAdapter())
    result = query_routes._compute_value_kpis(time_window="7 DAY")
    metrics = result["metrics"]

    # trace 覆盖 = 1/35≈0.0286，inference 覆盖 = 2/3≈0.6667，应取更高值
    assert metrics["topology_coverage_trace_rate"] == 0.0286
    assert metrics["topology_coverage_inference_rate"] == 0.6667
    assert metrics["topology_coverage_rate"] == 0.6667


def test_release_gate_pass_rate_excludes_drill_and_bypass():
    """发布回归通过率应仅按 release_total/release_passed 口径计算。"""
    query_routes.set_storage_adapter(FakeReleaseGateDrillAwareStorageAdapter())
    summary = query_routes._query_release_gate_summary(time_window="7 DAY")

    assert summary["total"] == 6
    assert summary["bypassed"] == 1
    assert summary["drill_total"] == 2
    assert summary["release_total"] == 4
    assert summary["release_passed"] == 4
    assert summary["release_failed"] == 0
    assert summary["pass_rate"] == 1.0
    assert summary["trace_smoke_pass_rate"] == 1.0
    assert summary["ai_contract_pass_rate"] == 1.0
    assert summary["query_contract_pass_rate"] == 1.0


def test_release_gate_summary_fallback_for_legacy_schema():
    """缺少新增列时应回退旧口径，避免接口整体失败。"""
    query_routes.set_storage_adapter(FakeLegacyReleaseGateSchemaAdapter())
    summary = query_routes._query_release_gate_summary(time_window="7 DAY")

    assert summary["total"] == 8
    assert summary["release_total"] == 8
    assert summary["release_passed"] == 6
    assert summary["release_failed"] == 2
    assert summary["trace_smoke_failed"] == 0
    assert summary["ai_contract_failed"] == 0
    assert summary["query_contract_failed"] == 0
    assert summary["last_result"]["trace_smoke_exit_code"] == 1
    assert summary["last_result"]["ai_contract_exit_code"] == 0
    assert summary["last_result"]["query_contract_exit_code"] == 0


def test_compute_value_kpis_cache_hit_reuses_previous_result():
    """相同窗口查询应命中缓存，避免重复执行重查询。"""
    storage = FakeValueKpiStorageAdapter()
    query_routes.set_storage_adapter(storage)

    first = query_routes._compute_value_kpis(time_window="7 DAY")
    second = query_routes._compute_value_kpis(time_window="7 DAY")

    assert first["metrics"] == second["metrics"]
    # 关键聚合查询（total_logs）只应执行一次
    correlation_calls = [
        " ".join(query.split())
        for query, _ in getattr(storage, "executed_queries", [])
        if "count() AS total_logs" in " ".join(query.split())
    ]
    # 兼容旧桩未记录 executed_queries 的情况
    if correlation_calls:
        assert len(correlation_calls) == 1

    cache_metrics = query_routes._build_value_kpi_cache_metrics_snapshot()
    assert cache_metrics["requests"] >= 2
    assert cache_metrics["hits"] >= 1
    assert cache_metrics["misses"] >= 1
    assert cache_metrics["writes"] >= 1
    assert cache_metrics["hit_rate"] >= 0.4


@pytest.mark.asyncio
async def test_value_kpi_cache_stats_exposes_metrics():
    """缓存状态接口应返回命中率与淘汰等统一指标。"""
    storage = FakeValueKpiStorageAdapter()
    query_routes.set_storage_adapter(storage)

    query_routes._compute_value_kpis(time_window="7 DAY")
    query_routes._compute_value_kpis(time_window="7 DAY")

    stats = await query_routes.value_kpi_cache_stats()
    metrics = stats.get("metrics", {})

    assert stats["status"] == "ok"
    assert "metrics" in stats
    assert metrics["requests"] >= 2
    assert metrics["hits"] >= 1
    assert metrics["misses"] >= 1
    assert "evictions_expired" in metrics
    assert "evictions_capacity" in metrics
    assert "manual_clears" in metrics
    assert "storage_resets" in metrics
