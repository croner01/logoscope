"""Tests for extracted inference query service helpers."""

import os
import sys
from typing import Any, Dict, List, Tuple

# 添加 query-service 根目录到 Python 路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api import query_inference_service as inference_service


class FakeStorageAdapter:
    """Storage stub for inference service tests."""

    def __init__(self):
        self.calls: List[Dict[str, Any]] = []

    def execute_query(self, query: str, params: Dict[str, Any] = None) -> List[Dict[str, Any]]:
        condensed = " ".join(query.split())
        payload = {"query": condensed, "params": params or {}}
        self.calls.append(payload)

        if "FROM logs.logs" in condensed:
            return [
                {
                    "id": "l1",
                    "timestamp": "2026-03-01 10:00:00.000",
                    "service_name": "legacy-gateway",
                    "namespace": "prod",
                    "message": "start req-1",
                    "trace_id": "",
                    "attributes_json": "{\"request_id\":\"req-1\"}",
                },
                {
                    "id": "l2",
                    "timestamp": "2026-03-01 10:00:01.000",
                    "service_name": "legacy-order",
                    "namespace": "prod",
                    "message": "finish req-1",
                    "trace_id": "",
                    "attributes_json": "{\"request_id\":\"req-1\"}",
                },
            ]

        if "FROM logs.trace_edges_1m" in condensed:
            return [
                {"source_service": "legacy-gateway", "target_service": "legacy-order"},
            ]

        if "AS child INNER JOIN (" in condensed and "FROM logs.traces PREWHERE timestamp > now() - INTERVAL" in condensed:
            return [
                {"source_service": "legacy-gateway", "target_service": "legacy-order"},
            ]

        return []


def _sanitize_interval(value: str, default_value: str = "1 HOUR") -> str:
    raw = str(value or "").strip().upper()
    return raw if raw else default_value


def _infer_fragments(rows: List[Dict[str, Any]], _fallback_window_sec: float = 2.0) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    if not rows:
        return [], {"strategy": "request_id_first_then_time_window"}
    return (
        [
            {
                "fragment_id": "legacy-gateway->legacy-order",
                "source_service": "legacy-gateway",
                "target_service": "legacy-order",
                "inference_method": "request_id",
                "confidence": 0.91,
                "sample_size": 2,
                "last_seen": "2026-03-01T10:00:01+00:00",
            },
            {
                "fragment_id": "legacy-gateway->legacy-audit",
                "source_service": "legacy-gateway",
                "target_service": "legacy-audit",
                "inference_method": "time_window",
                "confidence": 0.55,
                "sample_size": 1,
                "last_seen": "2026-03-01T10:00:02+00:00",
            },
        ],
        {"strategy": "request_id_first_then_time_window"},
    )


def test_query_trace_lite_inferred_filters_and_sorting():
    storage = FakeStorageAdapter()

    result = inference_service.query_trace_lite_inferred(
        storage_adapter=storage,
        time_window="1 HOUR",
        source_service="legacy-gateway",
        target_service="legacy-order",
        namespace="prod",
        limit=5,
        sanitize_interval_fn=_sanitize_interval,
        infer_trace_lite_fragments_fn=_infer_fragments,
    )

    query_call = storage.calls[0]
    assert "service_name = {source_service:String}" in query_call["query"]
    assert "namespace = {namespace:String}" in query_call["query"]
    assert query_call["params"]["source_service"] == "legacy-gateway"
    assert query_call["params"]["namespace"] == "prod"
    assert result["count"] == 1
    assert result["data"][0]["target_service"] == "legacy-order"
    assert result["time_window"] == "1 HOUR"


def test_trace_lite_pilot_readiness_contract():
    storage = FakeStorageAdapter()

    result = inference_service.trace_lite_pilot_readiness(
        storage_adapter=storage,
        time_window="24 HOUR",
        min_services=2,
        sanitize_interval_fn=_sanitize_interval,
        infer_trace_lite_fragments_fn=_infer_fragments,
    )

    query_call = storage.calls[0]
    assert "LIMIT {query_limit:Int32}" in query_call["query"]
    assert query_call["params"]["query_limit"] == inference_service.TRACE_LITE_PILOT_LOG_LIMIT
    assert result["ready"] is True
    assert result["inferred_service_count"] >= 2
    assert "legacy-gateway->legacy-order" in result["sample_pairs"]


def test_inference_quality_metrics_and_alerts():
    storage = FakeStorageAdapter()

    result = inference_service.inference_quality_metrics(
        storage_adapter=storage,
        time_window="1 HOUR",
        sanitize_interval_fn=_sanitize_interval,
        infer_trace_lite_fragments_fn=_infer_fragments,
    )

    logs_query_call = storage.calls[0]
    assert "LIMIT {query_limit:Int32}" in logs_query_call["query"]
    assert logs_query_call["params"]["query_limit"] == inference_service.INFERENCE_QUALITY_LOG_LIMIT
    metrics = result["metrics"]
    assert metrics["coverage"] == 1.5
    assert metrics["observed_pairs"] == 1
    assert metrics["inferred_pairs"] == 2
    assert metrics["false_positive_count"] == 1
    if metrics["false_positive_rate_state"] == "ok":
        assert metrics["false_positive_rate"] == 0.5
    else:
        assert metrics["false_positive_rate"] == 0.0
        assert metrics["false_positive_rate_reason"] in {"insufficient_inferred_sample", "no_observed_baseline"}

    alerts = inference_service.inference_quality_alerts(
        metrics=metrics,
        time_window="1 HOUR",
        min_coverage=0.95,
        max_inferred_ratio=0.60,
        max_false_positive_rate=0.30,
        suppressed_metrics={"false_positive_rate"},
    )

    assert alerts["status"] == "ok"
    assert alerts["active_alerts"] >= 1
    false_positive_alerts = [item for item in alerts["alerts"] if item["metric"] == "false_positive_rate"]
    if metrics["false_positive_rate_state"] == "ok":
        assert len(false_positive_alerts) == 1
        assert false_positive_alerts[0]["suppressed"] is True
    else:
        assert false_positive_alerts == []
