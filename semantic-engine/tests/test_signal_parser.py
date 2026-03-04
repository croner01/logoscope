"""
msgqueue.signal_parser 单元测试
"""

from msgqueue.signal_parser import (
    infer_data_type,
    parse_metrics_points,
    parse_trace_spans,
)


def test_infer_data_type_prefers_header():
    assert infer_data_type("logs.raw", {"data_type": "metrics"}) == "metrics"


def test_infer_data_type_fallback_to_subject():
    assert infer_data_type("traces.raw", {}) == "traces"
    assert infer_data_type("metrics.raw", None) == "metrics"
    assert infer_data_type("logs.raw", None) == "logs"


def test_infer_data_type_unknown_header_is_not_forced_to_logs():
    assert infer_data_type("unknown.raw", {"data_type": "unknown"}) == "unknown"


def test_parse_metrics_points_from_wrapped_payload():
    message_body = {
        "signal_type": "metrics",
        "payload": {
            "resourceMetrics": [
                {
                    "resource": {
                        "attributes": [
                            {"key": "service.name", "value": {"stringValue": "api-service"}},
                            {"key": "env", "value": {"stringValue": "prod"}},
                        ]
                    },
                    "scopeMetrics": [
                        {
                            "metrics": [
                                {
                                    "name": "http.server.duration",
                                    "histogram": {
                                        "dataPoints": [
                                            {
                                                "timeUnixNano": 1738892346000000000,
                                                "sum": 5200.0,
                                                "count": 100,
                                                "attributes": [
                                                    {"key": "http.method", "value": {"stringValue": "GET"}}
                                                ],
                                            }
                                        ]
                                    },
                                },
                                {
                                    "name": "process.cpu.utilization",
                                    "gauge": {
                                        "dataPoints": [
                                            {
                                                "timeUnixNano": 1738892346000000000,
                                                "asDouble": 0.82,
                                                "attributes": [],
                                            }
                                        ]
                                    },
                                },
                            ]
                        }
                    ],
                }
            ]
        },
    }

    points = parse_metrics_points(message_body)
    assert len(points) == 2
    assert points[0]["service_name"] == "api-service"
    assert points[0]["attributes"]["env"] == "prod"
    assert points[0]["attributes"]["http.method"] == "GET"
    assert points[1]["metric_type"] == "gauge"


def test_parse_trace_spans_from_wrapped_payload():
    message_body = {
        "signal_type": "traces",
        "payload": {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            {"key": "service.name", "value": {"stringValue": "checkout-service"}}
                        ]
                    },
                    "scopeSpans": [
                        {
                            "spans": [
                                {
                                    "traceId": "4bf92f3577b34da6a3ce929d0e0e4736",
                                    "spanId": "00f067aa0ba902b7",
                                    "parentSpanId": "",
                                    "name": "POST /api/checkout",
                                    "kind": "SERVER",
                                    "startTimeUnixNano": 1738892346000000000,
                                    "endTimeUnixNano": 1738892346123000000,
                                    "status": {"code": 2},
                                    "attributes": [
                                        {"key": "http.status_code", "value": {"intValue": "500"}}
                                    ],
                                }
                            ]
                        }
                    ],
                }
            ]
        },
    }

    spans = parse_trace_spans(message_body)
    assert len(spans) == 1
    span = spans[0]
    assert span["service_name"] == "checkout-service"
    assert span["status_code"] == "STATUS_CODE_ERROR"
    assert span["trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert span["start_time"].endswith("Z")
    assert span["duration_ns"] == 123000000
    assert span["duration_ms"] == 123.0
    assert span["tags"]["duration_ns"] == 123000000
    assert span["tags"]["http.status_code"] == "500"


def test_parse_trace_spans_normalizes_base64_ids():
    message_body = {
        "signal_type": "traces",
        "payload": {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            {"key": "service.name", "value": {"stringValue": "payment-service"}}
                        ]
                    },
                    "scopeSpans": [
                        {
                            "spans": [
                                {
                                    "traceId": "S_kvNXezTaajzpKdDg5HNg",
                                    "spanId": "APBnqgupArc",
                                    "parentSpanId": "AQIDBAUGBwg",
                                    "name": "POST /api/pay",
                                    "kind": "SERVER",
                                    "startTimeUnixNano": 1738892346000000000,
                                    "endTimeUnixNano": 1738892346025000000,
                                    "status": {"code": 1},
                                    "attributes": [],
                                }
                            ]
                        }
                    ],
                }
            ]
        },
    }

    spans = parse_trace_spans(message_body)
    assert len(spans) == 1
    span = spans[0]
    assert span["trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert span["span_id"] == "00f067aa0ba902b7"
    assert span["parent_span_id"] == "0102030405060708"
    assert span["duration_ms"] == 25.0


def test_parse_trace_spans_supports_iso_start_end_time_fields():
    message_body = {
        "signal_type": "traces",
        "payload": {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            {"key": "service.name", "value": {"stringValue": "legacy-trace-service"}}
                        ]
                    },
                    "scopeSpans": [
                        {
                            "spans": [
                                {
                                    "traceId": "4bf92f3577b34da6a3ce929d0e0e4736",
                                    "spanId": "00f067aa0ba902b7",
                                    "name": "legacy/span",
                                    "start_time": "2026-03-04T10:00:00.000Z",
                                    "end_time": "2026-03-04T10:00:00.015Z",
                                    "status": {"code": 1},
                                    "attributes": [],
                                }
                            ]
                        }
                    ],
                }
            ]
        },
    }

    spans = parse_trace_spans(message_body)
    assert len(spans) == 1
    span = spans[0]
    assert span["service_name"] == "legacy-trace-service"
    assert span["duration_ns"] == 15000000
    assert span["duration_ms"] == 15.0
