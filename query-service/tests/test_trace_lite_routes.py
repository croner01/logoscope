"""
Query Service trace-lite 路由单元测试
"""
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List

import pytest

# 添加 query-service 根目录到 Python 路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api import query_routes


class FakeStorageAdapter:
    """trace-lite 测试存储桩。"""

    def __init__(self):
        self.calls: List[str] = []

    def execute_query(self, query: str, params: Dict[str, Any] = None) -> List[Dict[str, Any]]:
        condensed = " ".join(query.split())
        self.calls.append(condensed)

        if "FROM logs.logs" in condensed:
            return [
                {
                    "id": "l1",
                    "timestamp": datetime(2026, 2, 26, 16, 0, 0, tzinfo=timezone.utc),
                    "service_name": "legacy-gateway",
                    "namespace": "prod",
                    "message": "request_id=req-1 access start",
                    "trace_id": "",
                    "attributes_json": "{\"request_id\":\"req-1\"}",
                },
                {
                    "id": "l2",
                    "timestamp": datetime(2026, 2, 26, 16, 0, 1, tzinfo=timezone.utc),
                    "service_name": "legacy-order",
                    "namespace": "prod",
                    "message": "request_id=req-1 handle order",
                    "trace_id": "",
                    "attributes_json": "{\"request_id\":\"req-1\"}",
                },
                {
                    "id": "l3",
                    "timestamp": datetime(2026, 2, 26, 16, 0, 2, tzinfo=timezone.utc),
                    "service_name": "legacy-audit",
                    "namespace": "ops",
                    "message": "periodic heartbeat",
                    "trace_id": "",
                    "attributes_json": "{}",
                },
            ]

        if "AS child INNER JOIN (" in condensed and "FROM logs.traces PREWHERE timestamp > now() - INTERVAL" in condensed:
            return [
                {"source_service": "legacy-gateway", "target_service": "legacy-order"},
            ]

        return []


@pytest.fixture(autouse=True)
def reset_state():
    query_routes.set_storage_adapter(None)
    query_routes._INFERENCE_ALERT_SUPPRESSIONS.clear()
    yield
    query_routes.set_storage_adapter(None)
    query_routes._INFERENCE_ALERT_SUPPRESSIONS.clear()


@pytest.mark.asyncio
async def test_query_trace_lite_inferred_returns_fragments():
    """trace-lite 接口应返回 inferred 调用片段。"""
    query_routes.set_storage_adapter(FakeStorageAdapter())
    result = await query_routes.query_trace_lite_inferred(
        time_window="1 HOUR",
        source_service=None,
        target_service=None,
        namespace=None,
        limit=50,
    )

    assert result["count"] >= 1
    item = result["data"][0]
    assert item["source_service"] == "legacy-gateway"
    assert item["target_service"] == "legacy-order"
    assert item["inference_method"] in {"request_id", "time_window"}
    assert item["confidence"] > 0
    assert "confidence_explain" in item


@pytest.mark.asyncio
async def test_inference_quality_alerts_with_suppression():
    """告警抑制开关应生效。"""
    query_routes.set_storage_adapter(FakeStorageAdapter())

    # 默认不抑制
    alerts = await query_routes.inference_quality_alerts(
        time_window="1 HOUR",
        min_coverage=0.95,  # 刻意设高，触发 coverage 告警
        max_inferred_ratio=0.99,
        max_false_positive_rate=0.99,
    )
    assert len(alerts["alerts"]) >= 1
    assert any(a["metric"] == "coverage" for a in alerts["alerts"])

    suppress = await query_routes.suppress_inference_alert(metric="coverage", enabled=True)
    assert suppress["suppressed"] is True

    alerts2 = await query_routes.inference_quality_alerts(
        time_window="1 HOUR",
        min_coverage=0.95,
        max_inferred_ratio=0.99,
        max_false_positive_rate=0.99,
    )
    coverage_alert = next(a for a in alerts2["alerts"] if a["metric"] == "coverage")
    assert coverage_alert["suppressed"] is True
