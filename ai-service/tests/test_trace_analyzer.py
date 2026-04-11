"""
Trace analyzer regression tests for timestamp parse fallback.
"""

from ai.trace_analyzer import Span, TraceAnalysisResult, TraceAnalyzer


def _build_analysis(trace_id: str) -> TraceAnalysisResult:
    return TraceAnalysisResult(
        trace_id=trace_id,
        total_duration_ms=100,
        service_count=1,
        span_count=2,
        root_cause_spans=[],
        bottleneck_spans=[],
        error_spans=[],
        recommendations=[],
        service_timeline=[],
        critical_path=[],
    )


def test_visualization_fallback_logs_warning_for_invalid_start_time(caplog, monkeypatch):
    analyzer = TraceAnalyzer()
    spans = [
        Span(
            span_id="span-invalid",
            trace_id="trace-1",
            parent_span_id=None,
            operation_name="op-invalid",
            service_name="svc",
            start_time="0000-invalid",
            duration_ms=20,
            status="ok",
        ),
        Span(
            span_id="span-valid",
            trace_id="trace-1",
            parent_span_id="span-invalid",
            operation_name="op-valid",
            service_name="svc",
            start_time="2026-03-01T00:00:00",
            duration_ms=30,
            status="ok",
        ),
    ]

    monkeypatch.setattr(analyzer, "_get_trace_spans", lambda _trace_id: spans)
    monkeypatch.setattr(analyzer, "analyze_trace", lambda trace_id: _build_analysis(trace_id))
    caplog.set_level("WARNING")

    result = analyzer.get_trace_visualization_data("trace-1")

    assert result["trace_id"] == "trace-1"
    assert len(result["waterfall"]) == 2
    assert any(
        "Failed to parse base span start_time" in record.message
        for record in caplog.records
    )
    assert any(
        "Failed to parse span start_time" in record.message
        for record in caplog.records
    )
