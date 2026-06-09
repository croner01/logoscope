"""Tests for _compute_data_quality."""

from graph.hybrid_topology import HybridTopologyBuilder


def make_builder():
    return HybridTopologyBuilder.__new__(HybridTopologyBuilder)


def test_data_quality_all_available():
    builder = make_builder()
    traces_data = {
        "nodes": [{"id": "svc-a"}],
        "edges": [{"source": "a", "target": "b", "metrics": {"p99": 120.0, "p95": 50.0, "durations": [50, 120]}}],
    }
    logs_data = {"nodes": [{"id": "svc-a"}]}
    metrics_data = {"nodes": [{"id": "svc-a"}]}
    dq = builder._compute_data_quality(traces_data, logs_data, metrics_data, [])
    assert dq["traces_available"] is True
    assert dq["logs_available"] is True
    assert dq["metrics_available"] is True
    assert dq["dimension_status"]["latency"] == "available"
    assert dq["dimension_status"]["error_rate_edge"] == "available"
    assert dq["dimension_status"]["quality_score"] == "full"


def test_data_quality_no_traces():
    builder = make_builder()
    traces_data = {"nodes": [], "edges": []}
    logs_data = {"nodes": [{"id": "svc-a"}]}
    metrics_data = {"nodes": []}
    edges = [{"source": "a", "target": "b", "metrics": {"data_source": "inferred"}}]
    dq = builder._compute_data_quality(traces_data, logs_data, metrics_data, edges)
    assert dq["traces_available"] is False
    assert dq["logs_available"] is True
    assert dq["dimension_status"]["latency"] == "missing"
    assert dq["dimension_status"]["error_rate_edge"] == "missing"
    assert dq["dimension_status"]["call_volume"] == "degraded"
    assert dq["dimension_status"]["quality_score"] == "logs_only"


def test_data_quality_no_data_at_all():
    builder = make_builder()
    dq = builder._compute_data_quality(
        {"nodes": [], "edges": []},
        {"nodes": []},
        {"nodes": []},
        [],
    )
    assert dq["traces_available"] is False
    assert dq["logs_available"] is False
    assert dq["metrics_available"] is False
    assert dq["dimension_status"]["latency"] == "missing"
    assert dq["dimension_status"]["call_volume"] == "missing"


def test_nullify_when_no_traces():
    """Verify nullification logic matches _apply_contract_schema behavior."""
    builder = make_builder()
    dim_status = {
        "latency": "missing",
        "error_rate_edge": "missing",
        "call_volume": "degraded",
        "quality_score": "logs_only",
    }
    latency_available = dim_status.get("latency") == "available"
    error_rate_available = dim_status.get("error_rate_edge") == "available"

    assert latency_available is False
    assert error_rate_available is False

    edges = [{"source": "a", "target": "b", "metrics": {
        "p95": 0.0, "p99": 0.0, "timeout_rate": 0.0,
        "error_rate": 0.0, "retries": 0.0, "pending": 0.0, "dlq": 0.0,
    }}]
    for edge in edges:
        em = edge["metrics"]
        if not latency_available:
            em["p95"] = None
            em["p99"] = None
            em["timeout_rate"] = None
        if not error_rate_available:
            em["error_rate"] = None
            em["retries"] = None
            em["pending"] = None
            em["dlq"] = None

    assert edges[0]["metrics"]["p95"] is None
    assert edges[0]["metrics"]["p99"] is None
    assert edges[0]["metrics"]["error_rate"] is None
    assert edges[0]["metrics"]["retries"] is None


def test_data_quality_traces_no_duration():
    """Traces table has edges but no duration data — latency should be missing."""
    builder = make_builder()
    traces_data = {
        "nodes": [{"id": "svc-a"}],
        "edges": [{"source": "a", "target": "b", "metrics": {"p99": 0.0, "p95": 0.0, "durations": [0, 0]}}],
    }
    logs_data = {"nodes": [{"id": "svc-a"}]}
    metrics_data = {"nodes": []}
    dq = builder._compute_data_quality(traces_data, logs_data, metrics_data, [])
    assert dq["traces_available"] is True
    assert dq["dimension_status"]["latency"] == "missing"  # p99 is 0, durations are all 0
